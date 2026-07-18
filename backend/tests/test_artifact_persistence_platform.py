"""Security invariants for publication ownership and artifact persistence."""

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from app import models
from app.config import get_settings
from app.publication import read_publication_record, registry_path
from app.routes import apps as apps_route
from app.timeutil import now_naive_utc


@pytest.fixture(autouse=True)
def _clean_publication_roots():
  data = Path(get_settings().data_dir)
  for name in ("published", "published-meta", "published-data"):
    shutil.rmtree(data / name, ignore_errors=True)


def _create_app(client, auth, name="artifact-platform") -> int:
  response = client.post("/api/apps/", headers=auth, json={
    "name": name,
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
  })
  assert response.status_code == 201, response.text
  return response.json()["id"]


def _seed_app_row(db, app_id: int, name: str) -> models.App:
  app = models.App(
    id=app_id,
    name=name,
    description="test",
    jsx_source="export default function App() { return null }",
    compiled_path="",
    slug=name,
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _build_dir(app_id: int, project_id: str) -> Path:
  return (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "projects" / project_id / "build"
  )


def _seed_site(app_id: int, project_id: str, body: str) -> None:
  site = _build_dir(app_id, project_id) / "site"
  site.mkdir(parents=True, exist_ok=True)
  (site / "index.html").write_text(body, encoding="utf-8")


def _publish(client, auth, app_id: int, project_id: str) -> str:
  response = client.post(
    f"/api/apps/{app_id}/publish",
    headers=auth,
    json={"project_id": project_id},
  )
  assert response.status_code == 200, response.text
  return response.json()["token"]


def _write_value(client, auth, app_id, artifact_id, key, value):
  return client.put(
    f"/api/apps/{app_id}/artifact-data/{artifact_id}/{key}",
    headers=auth,
    json=value,
  )


def test_publish_registry_binds_live_generation_and_serves(client, auth, db):
  app_id = _create_app(client, auth)
  _seed_site(app_id, "tip-a", "<h1>registered</h1>")
  token = _publish(client, auth, app_id, "tip-a")

  row = db.query(models.App).filter(models.App.id == app_id).one()
  record = read_publication_record(get_settings(), token)
  assert record is not None
  assert record.binding() == (app_id, row.token_nonce, "tip-a")
  assert record.state == "active"
  assert client.get(f"/sites/{token}/").status_code == 200


def test_publish_token_hint_cannot_hijack_another_app(client, auth):
  app_a = _create_app(client, auth, "publisher-a")
  app_b = _create_app(client, auth, "publisher-b")
  _seed_site(app_a, "tip-a", "site-a")
  token_a = _publish(client, auth, app_a, "tip-a")

  _seed_site(app_b, "tip-b", "site-b")
  token_file = _build_dir(app_b, "tip-b") / "publish-token.txt"
  token_file.write_text(token_a, encoding="utf-8")
  token_b = _publish(client, auth, app_b, "tip-b")

  assert token_b != token_a
  record_a = read_publication_record(get_settings(), token_a)
  assert record_a is not None and record_a.app_id == app_a
  assert client.get(f"/sites/{token_a}/").status_code == 200


def test_revoked_token_is_permanently_reserved(client, auth):
  app_a = _create_app(client, auth, "reservation-a")
  app_b = _create_app(client, auth, "reservation-b")
  _seed_site(app_a, "tip-a", "site-a")
  old_token = _publish(client, auth, app_a, "tip-a")
  assert client.delete(
    f"/api/apps/{app_a}/publish?project_id=tip-a", headers=auth,
  ).status_code == 200
  assert read_publication_record(get_settings(), old_token).state == "revoked"

  _seed_site(app_b, "tip-b", "site-b")
  (_build_dir(app_b, "tip-b") / "publish-token.txt").write_text(
    old_token, encoding="utf-8",
  )
  token_b = _publish(client, auth, app_b, "tip-b")
  token_a2 = _publish(client, auth, app_a, "tip-a")

  assert token_b != old_token
  assert token_a2 != old_token
  reservation = read_publication_record(get_settings(), old_token)
  assert reservation.app_id == app_a and reservation.state == "revoked"


def test_publish_backfills_a_legacy_snapshot(client, auth, db):
  app_id = _create_app(client, auth)
  project_id = "tip-backfill"
  token = "3" * 32
  _seed_site(app_id, project_id, "new generation")
  (_build_dir(app_id, project_id) / "publish-token.txt").write_text(
    token, encoding="utf-8",
  )
  snapshot = Path(get_settings().data_dir) / "published" / token
  snapshot.mkdir(parents=True, exist_ok=True)
  (snapshot / "index.html").write_text("legacy generation", encoding="utf-8")

  assert _publish(client, auth, app_id, project_id) == token
  row = db.query(models.App).filter(models.App.id == app_id).one()
  record = read_publication_record(get_settings(), token)
  assert record.binding() == (app_id, row.token_nonce, project_id)
  assert record.state == "active"
  assert (snapshot / "index.html").read_text() == "new generation"


def test_failed_first_publish_never_activates_orphan(
  client, auth, monkeypatch,
):
  app_id = _create_app(client, auth)
  _seed_site(app_id, "tip-fail", "complete but not active")

  def fail_promote(_stage, _destination):
    raise OSError("simulated atomic promote failure")

  monkeypatch.setattr(apps_route, "atomic_promote_directory", fail_promote)
  with pytest.raises(OSError, match="simulated atomic promote failure"):
    client.post(
      f"/api/apps/{app_id}/publish",
      headers=auth,
      json={"project_id": "tip-fail"},
    )
  records = apps_route._registry_records_for_app(get_settings(), app_id)
  assert len(records) == 1 and records[0].state == "revoked"
  assert client.get(f"/sites/{records[0].token}/").status_code == 404


def test_generation_mismatch_uniformly_404s_site_and_data(
  client, auth, db,
):
  app_id = _create_app(client, auth)
  _seed_site(app_id, "tip-gen", "generation one")
  assert _write_value(
    client, auth, app_id, "tip-gen", "shared", {"owner": "old"},
  ).status_code == 204
  token = _publish(client, auth, app_id, "tip-gen")

  row = db.query(models.App).filter(models.App.id == app_id).one()
  row.token_nonce = "f" * 32
  db.commit()

  assert client.get(f"/sites/{token}/").status_code == 404
  assert client.get(
    f"/api/published-sites/{token}/data/shared",
  ).status_code == 404


def test_legacy_snapshot_without_registry_still_serves(client, caplog):
  token = "1" * 32
  root = Path(get_settings().data_dir) / "published" / token
  root.mkdir(parents=True, exist_ok=True)
  (root / "index.html").write_text("legacy", encoding="utf-8")
  assert not os.path.lexists(registry_path(get_settings(), token))

  assert client.get(f"/sites/{token}/").status_code == 200
  assert client.get(f"/sites/{token}/").status_code == 200
  assert caplog.text.count(token) <= 1
  assert client.get(
    f"/api/published-sites/{token}/data/key",
  ).status_code == 404


def test_revoke_is_fail_closed_when_snapshot_rmtree_fails(
  client, auth, monkeypatch,
):
  app_id = _create_app(client, auth)
  _seed_site(app_id, "tip-revoke", "must go dark")
  token = _publish(client, auth, app_id, "tip-revoke")
  snapshot = Path(get_settings().data_dir) / "published" / token
  real_rmtree = shutil.rmtree

  def fail_snapshot(path, *args, **kwargs):
    if Path(path) == snapshot:
      raise OSError("simulated cleanup failure")
    return real_rmtree(path, *args, **kwargs)

  monkeypatch.setattr(apps_route.shutil, "rmtree", fail_snapshot)
  response = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert response.status_code == 204, response.text
  assert snapshot.is_dir()
  assert read_publication_record(get_settings(), token).state == "revoked"
  assert client.get(f"/sites/{token}/").status_code == 404


@pytest.mark.parametrize("teardown", ["soft", "wipe", "hard"])
def test_every_teardown_path_revokes_before_cleanup(
  client, auth, db, teardown,
):
  app_id = _create_app(client, auth, f"teardown-{teardown}")
  project_id = f"tip-{teardown}"
  _seed_site(app_id, project_id, teardown)
  token = _publish(client, auth, app_id, project_id)

  if teardown == "soft":
    response = client.delete(f"/api/apps/{app_id}", headers=auth)
  elif teardown == "wipe":
    response = client.delete(f"/api/apps/{app_id}/data", headers=auth)
  else:
    row = db.query(models.App).filter(models.App.id == app_id).one()
    row.deleted_at = now_naive_utc() - apps_route.APP_SOFT_DELETE_TTL - timedelta(
      seconds=1,
    )
    db.commit()
    response = client.get("/api/apps/", headers=auth)

  assert response.status_code in (200, 204), response.text
  record = read_publication_record(get_settings(), token)
  assert record is not None and record.state == "revoked"
  assert client.get(f"/sites/{token}/").status_code == 404


def test_legacy_token_fallback_is_reserved_and_revoked(client, auth):
  app_id = _create_app(client, auth)
  project_id = "tip-legacy"
  token = "2" * 32
  _seed_site(app_id, project_id, "legacy app")
  token_file = _build_dir(app_id, project_id) / "publish-token.txt"
  token_file.write_text(token, encoding="utf-8")
  snapshot = Path(get_settings().data_dir) / "published" / token
  snapshot.mkdir(parents=True, exist_ok=True)
  (snapshot / "index.html").write_text("legacy app", encoding="utf-8")

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  record = read_publication_record(get_settings(), token)
  assert record is not None and record.state == "revoked"
  assert client.get(f"/sites/{token}/").status_code == 404


def test_id_reuse_cannot_expose_replacement_app_data(client, auth, db):
  old = _seed_app_row(db, 7, "reuse-old")
  _seed_site(old.id, "tip-reuse", "old site")
  assert _write_value(
    client, auth, old.id, "tip-reuse", "private", {"app": "old"},
  ).status_code == 204
  token = _publish(client, auth, old.id, "tip-reuse")
  old_nonce = old.token_nonce

  old.deleted_at = now_naive_utc() - apps_route.APP_SOFT_DELETE_TTL - timedelta(
    seconds=1,
  )
  db.commit()
  assert client.get("/api/apps/", headers=auth).status_code == 200
  assert db.query(models.App).filter(models.App.id == 7).first() is None

  replacement = _seed_app_row(db, 7, "reuse-new")
  assert replacement.token_nonce != old_nonce
  assert _write_value(
    client, auth, 7, "tip-reuse", "private", {"app": "replacement"},
  ).status_code == 204

  assert client.get(f"/sites/{token}/").status_code == 404
  response = client.get(f"/api/published-sites/{token}/data/private")
  assert response.status_code == 404
  assert "replacement" not in response.text


def test_artifact_data_owner_and_own_app_only(client, auth):
  app_a = _create_app(client, auth, "data-owner-a")
  app_b = _create_app(client, auth, "data-owner-b")
  token_a = client.post(
    "/api/auth/app-token", headers=auth, json={"app_id": app_a},
  ).json()["token"]
  token_b = client.post(
    "/api/auth/app-token", headers=auth, json={"app_id": app_b},
  ).json()["token"]

  own = {"Authorization": f"Bearer {token_a}", "Origin": "null"}
  other = {"Authorization": f"Bearer {token_b}", "Origin": "null"}
  assert _write_value(
    client, own, app_a, "tip-auth", "key", {"ok": True},
  ).status_code == 204
  assert client.get(
    f"/api/apps/{app_a}/artifact-data/tip-auth/key", headers=own,
  ).json() == {"ok": True}
  assert _write_value(
    client, other, app_a, "tip-auth", "key", {"stolen": True},
  ).status_code == 403


def test_artifact_data_value_total_and_key_caps(client, auth):
  app_id = _create_app(client, auth)
  too_large = "x" * (64 * 1024)
  assert _write_value(
    client, auth, app_id, "tip-caps", "large", too_large,
  ).status_code == 413

  payload = "x" * 65000
  for index in range(16):
    response = _write_value(
      client, auth, app_id, "tip-total", f"k{index}", payload,
    )
    assert response.status_code == 204, response.text
  assert _write_value(
    client, auth, app_id, "tip-total", "overflow", payload,
  ).status_code == 413

  for index in range(100):
    response = _write_value(
      client, auth, app_id, "tip-count", f"k{index}", index,
    )
    assert response.status_code == 204, response.text
  assert _write_value(
    client, auth, app_id, "tip-count", "overflow", True,
  ).status_code == 400


def test_public_data_is_get_only_confined_and_generation_bound(
  client, auth, db,
):
  app_a = _create_app(client, auth, "public-data-a")
  app_b = _create_app(client, auth, "public-data-b")
  _seed_site(app_a, "tip-public", "public")
  assert _write_value(
    client, auth, app_a, "tip-public", "visible", {"scope": "right"},
  ).status_code == 204
  assert _write_value(
    client, auth, app_a, "tip-other", "secret", {"scope": "artifact"},
  ).status_code == 204
  assert _write_value(
    client, auth, app_b, "tip-public", "secret", {"scope": "app"},
  ).status_code == 204
  token = _publish(client, auth, app_a, "tip-public")
  url = f"/api/published-sites/{token}/data/visible"

  response = client.get(url)
  assert response.status_code == 200
  assert response.json() == {"scope": "right"}
  assert response.headers["cache-control"] == "no-cache"
  assert response.headers["x-content-type-options"] == "nosniff"
  assert client.put(url, json={"scope": "write"}).status_code in (404, 405)
  assert client.delete(url).status_code in (404, 405)
  assert client.post(url, json={}).status_code in (404, 405)
  assert client.get(
    f"/api/published-sites/{token}/data/secret",
  ).status_code == 404
  assert client.get(
    f"/api/published-sites/{token}/data/..%2Fsecret",
  ).status_code == 404

  row = db.query(models.App).filter(models.App.id == app_a).one()
  row.token_nonce = "e" * 32
  db.commit()
  assert client.get(url).status_code == 404


def test_public_data_rejects_symlink_and_oversized_file(client, auth):
  app_id = _create_app(client, auth)
  _seed_site(app_id, "tip-files", "files")
  token = _publish(client, auth, app_id, "tip-files")
  artifact = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "artifact-data" / "tip-files"
  )
  artifact.mkdir(parents=True, exist_ok=True)
  outside = artifact.parent / "outside.json"
  outside.write_text(json.dumps({"leak": True}), encoding="utf-8")
  (artifact / "linked.json").symlink_to(outside)
  (artifact / "huge.json").write_bytes(b" " * (1024 * 1024 + 1))

  assert client.get(
    f"/api/published-sites/{token}/data/linked",
  ).status_code == 404
  assert client.get(
    f"/api/published-sites/{token}/data/huge",
  ).status_code == 404
