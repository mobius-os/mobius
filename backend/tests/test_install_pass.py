"""Opaque one-time passes bridge iOS's per-web-app storage partition.

The browser-visible value is only a random reference. Its digest, app binding,
owner revocation epoch, expiry, and consumption state live durably in the
database; redemption atomically spends the row before minting a fresh short
owner session. No owner bearer is embedded in a URL or cacheable document.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import auth as auth_lib, models
from app.shell_install_pass import hash_secret as _shell_install_hash
import app.routes.standalone as standalone_routes
from app.routes.auth import _install_pass_hash
from app.timeutil import now_naive_utc
from test_app_fixtures import create_local_app


def _create_app(client, auth_header, name="Notes", *, offline_capable=False):
  return create_local_app(
    client,
    auth_header,
    name=name,
    offline_capable=offline_capable,
  )


def _mint(client, auth_header, slug):
  return client.post(
    "/api/auth/install-pass",
    json={"slug": slug},
    headers=auth_header,
  )


def _redeem(client, secret, slug):
  return client.post(
    "/api/auth/install-pass/redeem",
    json={"install_pass": secret, "slug": slug},
  )


def _grant_for(db, secret):
  digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
  return db.query(models.InstallPassGrant).filter(
    models.InstallPassGrant.token_hash == digest,
  ).one()


def test_pass_is_opaque_and_redeems_into_a_normal_owner_session(
  client, auth, db,
):
  app_row = _create_app(client, auth)
  slug = app_row["slug"]

  minted = _mint(client, auth, slug)
  assert minted.status_code == 200
  secret = minted.json()["install_pass"]
  assert minted.headers["cache-control"] == "no-store"

  # The URL carries no JWT and the database stores no plaintext copy.
  assert auth_lib.decode_access_token(secret) is None
  grant = _grant_for(db, secret)
  assert grant.token_hash != secret
  assert grant.app_id == app_row["id"]

  redeemed = _redeem(client, secret, slug)
  assert redeemed.status_code == 200
  access_token = redeemed.json()["access_token"]
  assert redeemed.headers["cache-control"] == "no-store"

  payload = auth_lib.decode_access_token(access_token)
  assert payload["sub"] == "test"
  assert "scope" not in payload
  expires_at = datetime.fromtimestamp(payload["exp"], UTC)
  assert expires_at >= datetime.now(UTC) + timedelta(days=29)
  assert expires_at <= datetime.now(UTC) + timedelta(days=31)
  me = client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {access_token}"}
  )
  assert me.status_code == 200


def test_minting_requires_the_session_being_handed_over(client, auth):
  app_row = _create_app(client, auth)
  assert client.post(
    "/api/auth/install-pass", json={"slug": app_row["slug"]}
  ).status_code == 401


def test_a_pass_is_durably_spent_on_first_use(client, auth, db):
  app_row = _create_app(client, auth)
  secret = _mint(client, auth, app_row["slug"]).json()["install_pass"]

  assert _redeem(client, secret, app_row["slug"]).status_code == 200
  db.expire_all()
  assert _grant_for(db, secret).consumed_at is not None

  # Replay checks the durable row; there is no process-local map a restart can
  # clear or a capacity eviction can remove.
  replay = _redeem(client, secret, app_row["slug"])
  assert replay.status_code == 401
  assert "invalid or has expired" in replay.json()["detail"]


def test_a_pass_is_bound_to_one_app_without_burning_on_mismatch(
  client, auth,
):
  first = _create_app(client, auth, name="Notes")
  second = _create_app(client, auth, name="Timer")
  secret = _mint(client, auth, first["slug"]).json()["install_pass"]

  assert _redeem(client, secret, second["slug"]).status_code == 401
  assert _redeem(client, secret, first["slug"]).status_code == 200


def test_two_passes_for_one_app_are_distinct_and_independent(client, auth):
  app_row = _create_app(client, auth)
  slug = app_row["slug"]
  first = _mint(client, auth, slug).json()["install_pass"]
  second = _mint(client, auth, slug).json()["install_pass"]
  assert first != second

  assert _redeem(client, first, slug).status_code == 200
  assert _redeem(client, second, slug).status_code == 200


def test_expired_unknown_and_non_pass_bearers_are_refused(
  client, auth, db,
):
  app_row = _create_app(client, auth)
  slug = app_row["slug"]
  secret = _mint(client, auth, slug).json()["install_pass"]
  grant = _grant_for(db, secret)
  grant.expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  assert _redeem(client, secret, slug).status_code == 401

  assert _redeem(client, "not-an-install-pass", slug).status_code == 401
  ordinary_bearer = auth_lib.create_access_token(
    {"sub": "test"}, expires_delta=timedelta(minutes=5), token_epoch=0,
  )
  assert _redeem(client, ordinary_bearer, slug).status_code == 401


def test_owner_epoch_revokes_an_unspent_pass(client, auth, db):
  app_row = _create_app(client, auth)
  secret = _mint(client, auth, app_row["slug"]).json()["install_pass"]
  owner = db.query(models.Owner).one()
  owner.token_epoch += 1
  db.commit()

  assert _redeem(client, secret, app_row["slug"]).status_code == 401


def test_manifest_forwards_a_pass_into_start_url_without_caching_it(
  client, auth,
):
  app_row = _create_app(client, auth)
  slug = app_row["slug"]
  base = f"/apps/{slug}/"

  plain = client.get(f"{base}manifest.json")
  assert plain.status_code == 200
  assert plain.json()["start_url"] == base
  assert plain.headers["cache-control"] == "no-cache, must-revalidate"

  carried = client.get(f"{base}manifest.json", params={"pass": "opaque"})
  assert carried.status_code == 200
  assert carried.json()["start_url"] == f"{base}?pass=opaque"
  assert carried.json()["scope"] == base
  assert carried.headers["cache-control"] == "no-store"


def test_pass_document_is_not_opted_into_even_an_old_service_worker_cache(
  client, auth, monkeypatch, tmp_path,
):
  source = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
  index = tmp_path / "standalone-index.html"
  index.write_text(
    source.read_text(encoding="utf-8").replace(
      "/src/main.jsx", "/assets/index-test.js"
    ),
    encoding="utf-8",
  )
  monkeypatch.setattr(standalone_routes, "_frontend_index_path", lambda: index)
  app_row = _create_app(client, auth, offline_capable=True)
  base = f"/apps/{app_row['slug']}/"

  plain = client.get(base)
  assert plain.headers["x-mobius-offline"] == "1"

  carried = client.get(base, params={"pass": "opaque"})
  assert carried.headers["cache-control"] == "no-store"
  assert "x-mobius-offline" not in carried.headers
  assert "pass=opaque" in carried.text


def test_manifest_never_mints_a_pass_for_an_anonymous_fetch(client, auth):
  app_row = _create_app(client, auth)
  body = client.get(f"/apps/{app_row['slug']}/manifest.json").json()
  assert "pass" not in body["start_url"]


def test_shell_install_cookie_is_opaque_copied_and_durably_one_use(
  client, auth, db,
):
  prepared = client.post("/api/auth/shell-install-pass", headers=auth)
  assert prepared.status_code == 204
  assert prepared.headers["cache-control"] == "no-store"
  set_cookie = prepared.headers["set-cookie"]
  assert "mobius_shell_install=" in set_cookie
  assert "HttpOnly" in set_cookie
  assert "SameSite=strict" in set_cookie
  assert "Path=/api/auth/shell-install-pass/redeem" in set_cookie

  secret = client.cookies.get("mobius_shell_install")
  assert secret
  assert auth_lib.decode_access_token(secret) is None
  digest = _shell_install_hash(secret)
  grant = db.query(models.ShellInstallPassGrant).filter(
    models.ShellInstallPassGrant.token_hash == digest,
  ).one()
  assert grant.token_hash != secret

  blocked = client.post(
    "/api/auth/shell-install-pass/redeem",
    headers={"Sec-Fetch-Site": "cross-site"},
  )
  assert blocked.status_code == 403

  redeemed = client.post("/api/auth/shell-install-pass/redeem")
  assert redeemed.status_code == 200
  payload = auth_lib.decode_access_token(redeemed.json()["access_token"])
  assert payload["sub"] == "test"
  assert datetime.fromtimestamp(payload["exp"], UTC) >= (
    datetime.now(UTC) + timedelta(days=29)
  )
  db.expire_all()
  assert grant.consumed_at is not None

  client.cookies.set(
    "mobius_shell_install",
    secret,
    path="/api/auth/shell-install-pass/redeem",
  )
  assert client.post("/api/auth/shell-install-pass/redeem").status_code == 401


def test_shell_install_preparation_requires_owner_session(client):
  assert client.post("/api/auth/shell-install-pass").status_code == 401


def test_shell_install_logout_revokes_a_cookie_already_copied_to_an_app(
  client, auth, db,
):
  prepared = client.post("/api/auth/shell-install-pass", headers=auth)
  assert prepared.status_code == 204
  copied_secret = client.cookies.get("mobius_shell_install")
  grant = db.query(models.ShellInstallPassGrant).filter(
    models.ShellInstallPassGrant.token_hash == _shell_install_hash(copied_secret),
  ).one()

  revoked = client.post("/api/auth/shell-install-pass/revoke", headers=auth)
  assert revoked.status_code == 204
  assert revoked.headers["cache-control"] == "no-store"
  assert "mobius_shell_install=" in revoked.headers["set-cookie"]
  assert "Max-Age=0" in revoked.headers["set-cookie"]
  assert "Path=/api/auth/shell-install-pass/redeem" in revoked.headers["set-cookie"]
  db.expire_all()
  assert grant.consumed_at is not None

  # The Home Screen container may already hold its own copied cookie when the
  # browser signs out. Restoring that copy must not restore the owner session.
  client.cookies.set(
    "mobius_shell_install",
    copied_secret,
    path="/api/auth/shell-install-pass/redeem",
  )
  assert client.post("/api/auth/shell-install-pass/redeem").status_code == 401


def test_shell_install_logout_requires_auth_and_same_site_request(
  client, auth, db,
):
  client.post("/api/auth/shell-install-pass", headers=auth)
  grant = db.query(models.ShellInstallPassGrant).one()

  assert client.post("/api/auth/shell-install-pass/revoke").status_code == 401
  blocked = client.post(
    "/api/auth/shell-install-pass/revoke",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert blocked.status_code == 403
  db.expire_all()
  assert grant.consumed_at is None


def test_shell_install_honors_owner_epoch(client, auth, db):
  client.post("/api/auth/shell-install-pass", headers=auth)
  owner = db.query(models.Owner).one()
  owner.token_epoch += 1
  db.commit()
  assert client.post("/api/auth/shell-install-pass/redeem").status_code == 401
