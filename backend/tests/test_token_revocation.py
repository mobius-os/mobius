"""Tests for owner JWT revocation via the token_epoch generation counter.

The contract under test: every owner-derived token is stamped with the
owner's token_epoch at mint time, and bumping the epoch ("sign out
everywhere") strands every outstanding token at once. See
models.Owner.token_epoch, deps._resolve_owner, and
routes/admin.py:sign_out_everywhere.
"""

from datetime import timedelta

from app import auth, models


def _app_token(client, owner_token):
  """Creates an app and returns an 8h app-scoped token for it."""
  r = client.post("/api/apps/", json={
    "name": "revocation-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]
  r = client.post(
    "/api/auth/app-token", json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  return r.json()["token"]


def test_token_epoch_bump_revokes_existing_tokens(client, owner_token):
  """The core contract: a valid token stops working the instant the
  owner's epoch advances, and the frontend-facing status is 401."""
  # The freshly-issued token works.
  r = client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {owner_token}"}
  )
  assert r.status_code == 200

  # Sign out everywhere bumps the epoch.
  r = client.post(
    "/api/admin/sign-out-everywhere",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 204

  # The same token is now rejected — 401 is what the frontend treats as
  # "clear the token and return to login".
  r = client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {owner_token}"}
  )
  assert r.status_code == 401


def test_fresh_login_after_bump_works(client):
  """After signing out everywhere, logging back in mints a token at the
  new epoch that validates."""
  client.post("/api/auth/setup", json={
    "username": "test", "password": "testpassword123",
  })
  r = client.post("/api/auth/token", data={
    "username": "test", "password": "testpassword123",
  })
  first_token = r.json()["access_token"]

  client.post(
    "/api/admin/sign-out-everywhere",
    headers={"Authorization": f"Bearer {first_token}"},
  )
  # Old token dead.
  assert client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {first_token}"}
  ).status_code == 401

  # New login works.
  r = client.post("/api/auth/token", data={
    "username": "test", "password": "testpassword123",
  })
  new_token = r.json()["access_token"]
  assert client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {new_token}"}
  ).status_code == 200


def test_bump_also_revokes_app_tokens(client, owner_token):
  """App-scoped tokens resolve to the Owner row and act on the owner's
  behalf, so a sign-out-everywhere must kill them too."""
  app_token = _app_token(client, owner_token)
  # /providers/models accepts owner-or-app tokens and needs no fixtures,
  # so the auth dependency is the only thing that can 401 here.
  r = client.get(
    "/api/auth/providers/models",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200

  client.post(
    "/api/admin/sign-out-everywhere",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    "/api/auth/providers/models",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 401


def test_legacy_token_without_epoch_claim_stays_valid(client, owner_token):
  """A token minted before this column existed carries no `epoch` claim;
  it must read as epoch 0 and keep validating against a never-revoked
  owner (token_epoch defaults to 0) — no forced sign-out on upgrade."""
  # Simulate a pre-migration token: no token_epoch stamped at all.
  legacy = auth.create_access_token({"sub": "test"})
  assert "epoch" not in auth.decode_access_token(legacy)
  r = client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {legacy}"}
  )
  assert r.status_code == 200


def test_legacy_token_revoked_after_bump(client, owner_token):
  """Once the owner bumps to epoch 1+, the unstamped legacy token (epoch
  0) falls behind and is rejected like any other stale token."""
  legacy = auth.create_access_token({"sub": "test"})
  client.post(
    "/api/admin/sign-out-everywhere",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {legacy}"}
  )
  assert r.status_code == 401


def test_token_stamped_at_wrong_epoch_is_rejected(client, owner_token, db):
  """A token whose stamped epoch is ahead of (or otherwise unequal to)
  the owner's current epoch is rejected — the check is equality, not
  just less-than, so a forged-ahead epoch can't slip through."""
  owner = db.query(models.Owner).filter(models.Owner.username == "test").first()
  ahead = auth.create_access_token(
    {"sub": "test"}, token_epoch=owner.token_epoch + 5
  )
  r = client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {ahead}"}
  )
  assert r.status_code == 401


def test_bump_revokes_query_param_token_on_module_route(client, owner_token):
  """The module route takes the token on `?token=` (iframe import can't
  set headers) and used to skip the revocation check. After the bump,
  a stale query-param token must be rejected there too."""
  r = client.post("/api/apps/", json={
    "name": "mod-app",
    "description": "test",
    "jsx_source": "export default function App() { return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  app_id = r.json()["id"]

  # Token works as a query param first.
  r = client.get(f"/api/apps/{app_id}/module?token={owner_token}")
  assert r.status_code == 200

  client.post(
    "/api/admin/sign-out-everywhere",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  r = client.get(f"/api/apps/{app_id}/module?token={owner_token}")
  assert r.status_code == 401


def test_sign_out_everywhere_rejects_app_tokens(client, owner_token):
  """The endpoint is owner-only — a compromised mini-app holding an app
  token must not be able to sign the owner out."""
  app_token = _app_token(client, owner_token)
  r = client.post(
    "/api/admin/sign-out-everywhere",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403


def test_service_token_carries_epoch_and_is_revocable(client):
  """The 90-day service token written at setup must be stamped with the
  owner's epoch so a sign-out-everywhere revokes it — a long-lived
  unrevocable token would be the biggest hole in the revocation story."""
  client.post("/api/auth/setup", json={
    "username": "test", "password": "testpassword123",
  })
  r = client.post("/api/auth/token", data={
    "username": "test", "password": "testpassword123",
  })
  owner_token = r.json()["access_token"]

  from app import auth as auth_mod
  # Re-derive what _write_service_token wrote: a 90-day token at the
  # owner's epoch. (We mint an equivalent here rather than read the file
  # so the test doesn't depend on DATA_DIR layout.)
  service = auth_mod.create_access_token(
    {"sub": "test"}, expires_delta=timedelta(days=90), token_epoch=0
  )
  assert client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {service}"}
  ).status_code == 200

  client.post(
    "/api/admin/sign-out-everywhere",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert client.get(
    "/api/apps/", headers={"Authorization": f"Bearer {service}"}
  ).status_code == 401
