"""Security invariants for publication ownership and artifact persistence."""

import json
import os
import shutil
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from app import models
from app import publication
from app.artifact_data import (
  MAX_ARTIFACT_TOTAL_BYTES,
  MAX_ARTIFACT_VALUE_BYTES,
  canonical_json,
)
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


def test_registry_parent_directory_is_fsynced_after_create_and_replace(
  monkeypatch,
):
  """Both registry directory-entry mutations must be crash-durable."""
  settings = get_settings()
  record = publication.new_publication_record(
    "d" * 32, 42, "e" * 32, "durable",
  )
  real_fsync = publication.os.fsync
  directory_fsyncs = []

  def track_fsync(fd):
    if stat.S_ISDIR(os.fstat(fd).st_mode):
      directory_fsyncs.append(fd)
    real_fsync(fd)

  monkeypatch.setattr(publication.os, "fsync", track_fsync)

  publication.create_publication_record(settings, record)
  assert len(directory_fsyncs) == 1

  publication.replace_publication_record(settings, record, "active")
  assert len(directory_fsyncs) == 2


@pytest.mark.parametrize("operation", ["create", "replace"])
def test_registry_parent_fsync_failure_is_reported(
  monkeypatch, operation,
):
  """A mutation is not successful when its directory entry is not durable."""
  settings = get_settings()
  record = publication.new_publication_record(
    "f" * 32, 43, "a" * 32, "strict-fsync",
  )
  if operation == "replace":
    publication.create_publication_record(settings, record)

  real_fsync = publication.os.fsync

  def fail_directory_fsync(fd):
    if stat.S_ISDIR(os.fstat(fd).st_mode):
      raise OSError("simulated registry directory fsync failure")
    real_fsync(fd)

  monkeypatch.setattr(publication.os, "fsync", fail_directory_fsync)

  with pytest.raises(OSError, match="directory fsync failure"):
    if operation == "create":
      publication.create_publication_record(settings, record)
    else:
      publication.replace_publication_record(settings, record, "active")


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


def test_publish_never_adopts_an_unregistered_token_from_a_hint(
  client, auth, db,
):
  """A pre-registry snapshot is inert: publishing mints a fresh token.

  publish-token.txt lives in app-writable storage, so adopting the token it
  names would let any app claim a public URL it does not own. The registry is
  the sole ownership authority, so an unrecognized hint is ignored and the
  untouched legacy snapshot keeps serving its old content.
  """
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

  minted = _publish(client, auth, app_id, project_id)

  assert minted != token, "an unregistered hint must not be adopted"
  assert read_publication_record(get_settings(), token) is None
  assert (snapshot / "index.html").read_text() == "legacy generation"
  row = db.query(models.App).filter(models.App.id == app_id).one()
  fresh = read_publication_record(get_settings(), minted)
  assert fresh.binding() == (app_id, row.token_nonce, project_id)
  assert fresh.state == "active"


def test_unregistered_hint_cannot_hijack_another_apps_public_url(client, auth):
  """App B must not take over a pre-registry URL published by app A."""
  victim_token = "a" * 32
  snapshot = Path(get_settings().data_dir) / "published" / victim_token
  snapshot.mkdir(parents=True, exist_ok=True)
  (snapshot / "index.html").write_text("VICTIM CONTENT", encoding="utf-8")

  attacker = _create_app(client, auth, "attacker-app")
  _seed_site(attacker, "evil", "ATTACKER CONTENT")
  (_build_dir(attacker, "evil") / "publish-token.txt").write_text(
    victim_token, encoding="utf-8",
  )

  minted = _publish(client, auth, attacker, "evil")

  assert minted != victim_token
  assert (snapshot / "index.html").read_text() == "VICTIM CONTENT"
  record = read_publication_record(get_settings(), victim_token)
  assert record is None, "attacker must not reserve the victim's token"


def test_unregistered_hint_cannot_destroy_another_apps_snapshot(client, auth):
  """Tearing down app B must not delete app A's pre-registry snapshot."""
  victim_token = "b" * 32
  snapshot = Path(get_settings().data_dir) / "published" / victim_token
  snapshot.mkdir(parents=True, exist_ok=True)
  (snapshot / "index.html").write_text("VICTIM CONTENT", encoding="utf-8")

  attacker = _create_app(client, auth, "wiper-app")
  _seed_site(attacker, "evil", "unused")
  (_build_dir(attacker, "evil") / "publish-token.txt").write_text(
    victim_token, encoding="utf-8",
  )

  # The app-data wipe is the cheapest teardown route that scans hint files.
  assert client.delete(
    f"/api/apps/{attacker}/data", headers=auth,
  ).status_code in (200, 204)

  assert snapshot.is_dir(), "victim snapshot destroyed by an unrelated app"
  assert (snapshot / "index.html").read_text() == "VICTIM CONTENT"
  assert read_publication_record(get_settings(), victim_token) is None


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


@pytest.mark.parametrize("failure_point", ["hint", "registry"])
def test_failed_republish_restores_the_previous_generation(
  client, auth, monkeypatch, failure_point,
):
  """A post-exchange failure must put the old snapshot back at the live URL."""
  app_id = _create_app(client, auth)
  project_id = "tip-rollback"
  _seed_site(app_id, project_id, "previous generation")
  token = _publish(client, auth, app_id, project_id)

  site = _build_dir(app_id, project_id) / "site"
  (site / "index.html").write_text("failed generation", encoding="utf-8")

  def fail_write(*_args, **_kwargs):
    raise OSError("simulated hint write failure")

  def fail_registry(*_args, **_kwargs):
    raise OSError("simulated registry write failure")

  if failure_point == "hint":
    monkeypatch.setattr(apps_route, "atomic_write", fail_write)
  else:
    monkeypatch.setattr(
      apps_route, "replace_publication_record", fail_registry,
    )

  with pytest.raises(OSError, match="simulated .* write failure"):
    client.post(
      f"/api/apps/{app_id}/publish",
      headers=auth,
      json={"project_id": project_id},
    )

  response = client.get(f"/sites/{token}/")
  assert response.status_code == 200
  assert response.text == "previous generation"


def test_failed_republish_preserves_old_copy_if_rollback_fails(
  client, auth, monkeypatch,
):
  """A failed rollback must not rmtree the only prior-generation copy."""
  app_id = _create_app(client, auth)
  project_id = "tip-preserve"
  _seed_site(app_id, project_id, "recoverable generation")
  _publish(client, auth, app_id, project_id)

  site = _build_dir(app_id, project_id) / "site"
  (site / "index.html").write_text("failed generation", encoding="utf-8")
  real_promote = apps_route.atomic_promote_directory
  promotions = 0

  def fail_rollback(stage, destination):
    nonlocal promotions
    promotions += 1
    if promotions == 1:
      return real_promote(stage, destination)
    raise OSError("simulated rollback failure")

  def fail_write(*_args, **_kwargs):
    raise OSError("simulated hint write failure")

  monkeypatch.setattr(
    apps_route, "atomic_promote_directory", fail_rollback,
  )
  monkeypatch.setattr(apps_route, "atomic_write", fail_write)

  with pytest.raises(OSError, match="simulated rollback failure"):
    client.post(
      f"/api/apps/{app_id}/publish",
      headers=auth,
      json={"project_id": project_id},
    )

  staging = Path(get_settings().data_dir) / "published" / ".staging"
  prior_copies = list(staging.glob("*/index.html"))
  assert len(prior_copies) == 1
  assert prior_copies[0].read_text() == "recoverable generation"


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


def test_registered_token_is_revoked_when_its_app_is_deleted(client, auth):
  """Deleting an app revokes the tokens the REGISTRY says it owns."""
  app_id = _create_app(client, auth)
  _seed_site(app_id, "tip-owned", "owned app")
  token = _publish(client, auth, app_id, "tip-owned")

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  record = read_publication_record(get_settings(), token)
  assert record is not None and record.state == "revoked"
  assert client.get(f"/sites/{token}/").status_code == 404


def test_deleting_an_app_leaves_an_unregistered_snapshot_alone(client, auth):
  """A pre-registry snapshot survives its app's deletion, by design.

  Only publish-token.txt — app-writable storage — names such a token, so
  honoring it would let any app delete another's snapshot (and any app can
  plant a token it does not own). We accept that a snapshot published before
  the registry existed outlives its app rather than reopening that hole;
  removing one is an explicit owner action. Instances carrying pre-registry
  snapshots need a one-time reconciliation pass to adopt them into the
  registry, which is where that cleanup belongs.
  """
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
  assert read_publication_record(get_settings(), token) is None
  assert snapshot.is_dir()


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


def test_artifact_data_total_cap_serializes_competing_puts(client, auth):
  app_id = _create_app(client, auth)
  artifact_id = "tip-atomic-cap"
  payload = "x" * (MAX_ARTIFACT_VALUE_BYTES - 2)
  assert len(canonical_json(payload)) == MAX_ARTIFACT_VALUE_BYTES

  prefill_count = MAX_ARTIFACT_TOTAL_BYTES // MAX_ARTIFACT_VALUE_BYTES - 1
  for index in range(prefill_count):
    response = _write_value(
      client, auth, app_id, artifact_id, f"prefill-{index}", payload,
    )
    assert response.status_code == 204, response.text

  artifact = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "artifact-data" / artifact_id
  )
  prefilled_total = sum(path.stat().st_size for path in artifact.iterdir())
  assert prefilled_total == MAX_ARTIFACT_TOTAL_BYTES - MAX_ARTIFACT_VALUE_BYTES

  winner = _write_value(
    client, auth, app_id, artifact_id, "contender-a", payload,
  )
  loser = _write_value(
    client, auth, app_id, artifact_id, "contender-b", payload,
  )

  assert winner.status_code == 204, winner.text
  assert loser.status_code == 413, loser.text
  assert (artifact / "contender-a.json").is_file()
  assert not (artifact / "contender-b.json").exists()
  assert sum(
    path.stat().st_size for path in artifact.iterdir()
  ) == MAX_ARTIFACT_TOTAL_BYTES


def test_authed_artifact_data_rejects_traversal_and_symlink_escapes(
  client, auth,
):
  app_id = _create_app(client, auth)
  data = Path(get_settings().data_dir)
  app_root = data / "apps" / str(app_id)
  artifact_data = app_root / "artifact-data"
  outside = data / "artifact-data-escape-targets"
  outside.mkdir(parents=True)
  sentinel = outside / "sentinel.json"
  sentinel_body = json.dumps({"secret": "must-not-escape"})
  sentinel.write_text(sentinel_body, encoding="utf-8")
  protected = [sentinel]

  def assert_rejected_for_every_method(url):
    for method in ("GET", "PUT", "DELETE"):
      kwargs = {"json": {"attack": method}} if method == "PUT" else {}
      response = client.request(method, url, headers=auth, **kwargs)
      assert response.status_code in (400, 404), response.text
      assert "must-not-escape" not in response.text
      assert all(
        path.read_text(encoding="utf-8") == sentinel_body
        for path in protected
      )

  app_root.mkdir(parents=True, exist_ok=True)
  linked_data_target = outside / "linked-data"
  linked_data_target.mkdir()
  linked_data_sentinel = linked_data_target / "linked-aid" / "probe.json"
  linked_data_sentinel.parent.mkdir()
  linked_data_sentinel.write_text(sentinel_body, encoding="utf-8")
  protected.append(linked_data_sentinel)
  artifact_data.symlink_to(linked_data_target, target_is_directory=True)
  assert_rejected_for_every_method(
    f"/api/apps/{app_id}/artifact-data/linked-aid/probe",
  )
  assert linked_data_sentinel.read_text(encoding="utf-8") == sentinel_body
  artifact_data.unlink()

  artifact_data.mkdir()
  artifact_escape = app_root / "outside-artifact" / "probe.json"
  artifact_escape.parent.mkdir()
  artifact_escape.write_text(sentinel_body, encoding="utf-8")
  key_escape = artifact_data / "outside-key.json"
  key_escape.write_text(sentinel_body, encoding="utf-8")
  protected.extend((artifact_escape, key_escape))
  traversal_urls = (
    f"/api/apps/{app_id}/artifact-data/../probe",
    f"/api/apps/{app_id}/artifact-data/%2e%2e/probe",
    f"/api/apps/{app_id}/artifact-data/..%2foutside-artifact/probe",
    f"/api/apps/{app_id}/artifact-data/%252e%252e/probe",
    f"/api/apps/{app_id}/artifact-data/safe/..",
    f"/api/apps/{app_id}/artifact-data/safe/..%2foutside-key",
  )
  for url in traversal_urls:
    assert_rejected_for_every_method(url)

  linked_artifact_target = outside / "linked-artifact"
  linked_artifact_target.mkdir()
  linked_artifact_sentinel = linked_artifact_target / "probe.json"
  linked_artifact_sentinel.write_text(sentinel_body, encoding="utf-8")
  protected.append(linked_artifact_sentinel)
  (artifact_data / "linked-aid").symlink_to(
    linked_artifact_target, target_is_directory=True,
  )
  assert_rejected_for_every_method(
    f"/api/apps/{app_id}/artifact-data/linked-aid/probe",
  )
  assert linked_artifact_sentinel.read_text(encoding="utf-8") == sentinel_body

  safe_artifact = artifact_data / "safe"
  safe_artifact.mkdir()
  (safe_artifact / "linked.json").symlink_to(sentinel)
  assert_rejected_for_every_method(
    f"/api/apps/{app_id}/artifact-data/safe/linked",
  )
  assert sentinel.read_text(encoding="utf-8") == sentinel_body


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


def test_deeply_nested_value_is_rejected_not_a_server_error(client, auth):
  """A nested body must 400 like other malformed JSON, never 500.

  json.loads raises RecursionError (a RuntimeError, not a ValueError) on deep
  nesting, so it would otherwise escape the decoder's except clause.
  """
  app_id = _create_app(client, auth)
  payload = "[" * 20000 + "]" * 20000
  response = client.put(
    f"/api/apps/{app_id}/artifact-data/deep/nested",
    headers={**auth, "Content-Type": "application/json"},
    content=payload,
  )
  assert response.status_code == 400, response.text


def test_artifact_data_write_respects_the_per_app_storage_backstop(
  client, auth, monkeypatch,
):
  """artifact_id is caller-chosen, so per-artifact caps alone bound nothing.

  Inventing namespaces multiplies the 1 MB/100-key caps; the per-app limit
  every other storage write honors is what actually bounds the tree.
  """
  from app import storage_io

  app_id = _create_app(client, auth)
  assert _write_value(
    client, auth, app_id, "ns-one", "seed", {"v": "x" * 500},
  ).status_code == 204

  monkeypatch.setattr(storage_io, "MAX_APP_STORAGE_BYTES", 256)
  response = _write_value(
    client, auth, app_id, "ns-two", "overflow", {"v": "y" * 500},
  )
  assert response.status_code == 413, response.text
  assert "per-app limit" in response.text


def test_public_data_limiter_buckets_per_site_not_per_url():
  """One bucket per published site, not per requested key.

  slowapi's default key_style="url" folds the path into the bucket, which
  handed a caller a fresh 60/minute budget for every key it invented. The key
  stays the token rather than the client address because public traffic
  arrives through the proxy and the app refuses to trust X-Forwarded-For, so
  a client-keyed limit would collapse into a single global bucket.
  """
  from app.routes import published as published_mod

  limiter = published_mod._public_data_limiter
  assert limiter._key_style == "endpoint"
  assert limiter._key_func is published_mod._public_token_key

  class _Req:
    path_params = {"token": "a" * 32}

  assert published_mod._public_token_key(_Req()) == "a" * 32


def test_revoking_one_project_leaves_a_sibling_projects_publication(
  client, auth,
):
  """A stray hint in project A must not revoke project B's live URL.

  Hints live in app-writable storage and name only a token, so the registry
  binding — not the hint — decides which publication a teardown may touch.
  """
  app_id = _create_app(client, auth)
  _seed_site(app_id, "keep-me", "<h1>sibling</h1>")
  kept = _publish(client, auth, app_id, "keep-me")

  # Project "drop-me" carries a hint pointing at the SIBLING's token.
  _seed_site(app_id, "drop-me", "<h1>doomed</h1>")
  (_build_dir(app_id, "drop-me") / "publish-token.txt").write_text(
    kept, encoding="utf-8",
  )

  response = client.delete(
    f"/api/apps/{app_id}/publish?project_id=drop-me", headers=auth,
  )
  assert response.status_code in (200, 204), response.text

  record = read_publication_record(get_settings(), kept)
  assert record is not None and record.state == "active"
  assert client.get(f"/sites/{kept}/").status_code == 200


def test_unpublish_reports_failure_when_the_url_stays_live(
  client, auth, monkeypatch,
):
  """A failed revocation must not answer ok while the page is still public.

  Reporting success here — and dropping the token hint on the way out — would
  tell the owner their artifact was unshared while anyone holding the link
  could still read it.
  """
  app_id = _create_app(client, auth)
  _seed_site(app_id, "still-live", "<h1>public</h1>")
  token = _publish(client, auth, app_id, "still-live")

  def _fail(*_args, **_kwargs):
    raise OSError("simulated registry write failure")

  monkeypatch.setattr(apps_route, "replace_publication_record", _fail)
  response = client.delete(
    f"/api/apps/{app_id}/publish?project_id=still-live", headers=auth,
  )

  assert response.status_code == 500, response.text
  # Still serving, and the record still says so — no silent half-revoke.
  record = read_publication_record(get_settings(), token)
  assert record is not None and record.state == "active"
  assert client.get(f"/sites/{token}/").status_code == 200
  assert (
    _build_dir(app_id, "still-live") / "publish-token.txt"
  ).is_file(), "the hint must survive so the live URL stays reachable"


def test_symlinked_publish_path_does_not_block_registry_revocation(
  client, auth, monkeypatch,
):
  """A symlink in app-writable publish paths must not keep a registered URL live.

  Revocation is registry-authoritative; the registry lives outside app storage.
  If validating the app-writable build/site or hint gates the revoke, an app job
  that left one of those a symlink could keep its public URL serving.
  """
  app_id = _create_app(client, auth)
  _seed_site(app_id, "sneaky", "<h1>live</h1>")
  token = _publish(client, auth, app_id, "sneaky")
  assert client.get(f"/sites/{token}/").status_code == 200

  # Make the built-site path a symlink, exactly what the path validator rejects.
  build = _build_dir(app_id, "sneaky")
  shutil.rmtree(build / "site")
  (build / "site").symlink_to("/tmp")

  response = client.delete(
    f"/api/apps/{app_id}/publish?project_id=sneaky", headers=auth,
  )
  assert response.status_code in (200, 204), response.text
  record = read_publication_record(get_settings(), token)
  assert record is not None and record.state == "revoked"
  assert client.get(f"/sites/{token}/").status_code == 404


def test_deeply_nested_storage_put_is_rejected_not_a_server_error(client, auth):
  """A deeply nested JSON storage PUT must 400 like other malformed bodies.

  json.loads raises RecursionError (a RuntimeError, not JSONDecodeError) on deep
  nesting, so it would otherwise escape as an unhandled 500.
  """
  app_id = _create_app(client, auth)
  payload = "[" * 20000 + "]" * 20000
  response = client.put(
    f"/api/storage/apps/{app_id}/deep.json",
    headers={**auth, "Content-Type": "application/json"},
    content=payload,
  )
  assert response.status_code == 400, response.text


def test_list_with_content_skips_deeply_nested_json_instead_of_500(client, auth):
  """A deeply nested blob stored as raw bytes must not 500 a content listing.

  json.loads raises RecursionError there too; the read path must skip the entry
  (like other decode failures), not surface an unhandled server error.
  """
  app_id = _create_app(client, auth)
  payload = ("[" * 20000 + "]" * 20000).encode()
  assert client.put(
    f"/api/storage/apps/{app_id}/deep.json",
    headers={**auth, "Content-Type": "application/octet-stream"},
    content=payload,
  ).status_code == 204
  response = client.get(
    f"/api/storage/apps-list/{app_id}/?include_content=true", headers=auth,
  )
  assert response.status_code == 200, response.text


def test_unpublish_revokes_the_registry_even_if_the_hint_read_errors(
  client, auth, monkeypatch,
):
  """An OS error reading the app-writable hint must not block the revoke.

  The registry is authoritative and lives outside app storage, so a filesystem
  fault in the legacy publish-token path must not leave a registered URL live.
  """
  app_id = _create_app(client, auth)
  _seed_site(app_id, "resilient", "<h1>live</h1>")
  token = _publish(client, auth, app_id, "resilient")
  assert client.get(f"/sites/{token}/").status_code == 200

  def _raise(*_a, **_k):
    raise OSError("simulated hint read failure")

  monkeypatch.setattr(apps_route, "_read_publish_token_hint", _raise)
  response = client.delete(
    f"/api/apps/{app_id}/publish?project_id=resilient", headers=auth,
  )
  assert response.status_code in (200, 204), response.text
  record = read_publication_record(get_settings(), token)
  assert record is not None and record.state == "revoked"
  assert client.get(f"/sites/{token}/").status_code == 404


def test_artifact_keys_are_derived_from_the_directory(client, auth):
  """Enumeration is server-side so no client index can desync.

  Two tabs maintaining an index file would each read the old list, write their
  own key, and the second index write would drop the first — leaving a value
  that exists but cannot be listed. The directory cannot disagree with itself.
  """
  app_id = _create_app(client, auth)
  for key in ("beta", "alpha", "gamma"):
    assert _write_value(client, auth, app_id, "deck", key, {"k": key}).status_code == 204

  response = client.get(f"/api/apps/{app_id}/artifact-data/deck", headers=auth)
  assert response.status_code == 200, response.text
  assert response.json()["keys"] == ["alpha", "beta", "gamma"]

  # A delete is reflected immediately — nothing to keep in sync.
  assert client.delete(
    f"/api/apps/{app_id}/artifact-data/deck/beta", headers=auth,
  ).status_code == 204
  assert client.get(
    f"/api/apps/{app_id}/artifact-data/deck", headers=auth,
  ).json()["keys"] == ["alpha", "gamma"]

  # An artifact that was never written enumerates empty rather than 404ing.
  assert client.get(
    f"/api/apps/{app_id}/artifact-data/never-used", headers=auth,
  ).json()["keys"] == []


def test_artifact_keys_reject_a_bad_id_and_another_apps_token(client, auth):
  app_id = _create_app(client, auth)
  assert client.get(
    f"/api/apps/{app_id}/artifact-data/..%2Fescape", headers=auth,
  ).status_code in (400, 404)
  assert client.get(
    f"/api/apps/{app_id}/artifact-data/deck",
  ).status_code == 401


def test_published_page_lists_keys_through_its_own_capability(client, auth, db):
  """The public listing is generation-bound exactly like the per-key read."""
  app_id = _create_app(client, auth)
  _seed_site(app_id, "shared-deck", "<h1>live</h1>")
  token = _publish(client, auth, app_id, "shared-deck")
  for key in ("score", "config"):
    assert _write_value(
      client, auth, app_id, "shared-deck", key, {"k": key},
    ).status_code == 204

  listing = client.get(f"/api/published-sites/{token}/data")
  assert listing.status_code == 200, listing.text
  assert listing.json()["keys"] == ["config", "score"]

  # Revoking the publication takes the listing down with it.
  assert client.delete(
    f"/api/apps/{app_id}/publish?project_id=shared-deck", headers=auth,
  ).status_code in (200, 204)
  assert client.get(f"/api/published-sites/{token}/data").status_code == 404


def test_published_listing_is_read_only_and_unknown_tokens_404(client, auth):
  assert client.get(
    "/api/published-sites/" + "a" * 32 + "/data",
  ).status_code == 404
  # No public write surface is introduced alongside the listing.
  assert client.put(
    "/api/published-sites/" + "a" * 32 + "/data", json={"keys": []},
  ).status_code in (404, 405)


def test_key_listing_ignores_symlinks_and_non_value_files(client, auth):
  """A planted symlink or stray file must never appear as a stored key."""
  from app.artifact_data import list_artifact_keys

  app_id = _create_app(client, auth)
  assert _write_value(
    client, auth, app_id, "deck", "real", {"v": 1},
  ).status_code == 204
  root = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "artifact-data" / "deck"
  )
  (root / "evil.json").symlink_to("/etc/passwd")
  (root / "notes.txt").write_text("not a value", encoding="utf-8")
  (root / "sub").mkdir()

  assert list_artifact_keys(root) == ["real"]
  assert client.get(
    f"/api/apps/{app_id}/artifact-data/deck", headers=auth,
  ).json()["keys"] == ["real"]


def test_key_listing_is_bounded_against_a_flooded_directory(client, auth):
  """Public enumeration must not become an unbounded scan.

  The 100-key cap is enforced by the artifact-data ROUTE, but the generic
  app-storage API can drop arbitrarily many .json files into the same directory.
  An unauthenticated published listing must stay bounded regardless.
  """
  from app.artifact_data import (
    MAX_ARTIFACT_KEYS,
    MAX_ARTIFACT_SCAN_ENTRIES,
    list_artifact_keys,
  )

  app_id = _create_app(client, auth)
  assert _write_value(client, auth, app_id, "flood", "real", {"v": 1}).status_code == 204
  root = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "artifact-data" / "flood"
  )
  for i in range(MAX_ARTIFACT_SCAN_ENTRIES + 250):
    (root / f"k{i:05d}.json").write_text("{}", encoding="utf-8")

  keys = list_artifact_keys(root)
  assert len(keys) <= MAX_ARTIFACT_KEYS, "listing must be capped"
  response = client.get(f"/api/apps/{app_id}/artifact-data/flood", headers=auth)
  assert response.status_code == 200
  assert len(response.json()["keys"]) <= MAX_ARTIFACT_KEYS


def test_key_listing_hides_values_the_per_key_read_cannot_serve(client, auth):
  """A key that always 404s on read must not be advertised by the listing."""
  from app.artifact_data import MAX_ARTIFACT_READ_BYTES, list_artifact_keys

  app_id = _create_app(client, auth)
  assert _write_value(client, auth, app_id, "deck", "real", {"v": 1}).status_code == 204
  root = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "artifact-data" / "deck"
  )
  # Larger than the read cap: read_json_file refuses it, so listing it would
  # advertise a key that can never be fetched.
  (root / "huge.json").write_text("x" * (MAX_ARTIFACT_READ_BYTES + 10), encoding="utf-8")

  assert list_artifact_keys(root) == ["real"]
  assert client.get(
    f"/api/apps/{app_id}/artifact-data/deck/huge", headers=auth,
  ).status_code == 404


def test_public_listing_and_value_reads_share_one_throttle_budget():
  """Both public reads spend from ONE per-token budget, not one each."""
  from app.routes import published as published_mod

  limiter = published_mod._public_data_limiter
  assert limiter._key_style == "endpoint"
  # `limit()` scopes per handler; `shared_limit(scope=...)` is what pins both
  # public reads to ONE bucket, so applying the same decorator to both is the
  # contract under test.
  assert published_mod._PUBLIC_DATA_SCOPE
  source = Path(published_mod.__file__).read_text(encoding="utf-8")
  assert "shared_limit(" in source
  assert source.count("@_public_data_limit\n") == 2
