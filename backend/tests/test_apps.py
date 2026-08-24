"""App registry lifecycle tests."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from app import app_git, install, models
from app.config import get_settings
from app.database import engine
from sqlalchemy import event
from test_app_fixtures import create_local_app


def _service_auth():
  token = (Path(get_settings().data_dir) / "service-token.txt").read_text()
  return {"Authorization": f"Bearer {token}"}


def _published_candidate(row: models.App) -> install.InstallCandidate:
  tree = app_git.read_ref_tree(row.source_dir, row.source_commit)
  manifest = json.loads(tree["mobius.json"])
  return install.InstallCandidate(
    manifest=manifest,
    raw_base="https://raw.githubusercontent.com/example/app/main/",
    entry_bytes=tree[manifest["entry"]],
    icon_processed=None,
    icon_warning=None,
    bundled_job=None,
    static_assets={},
    source_files={},
    seeds={},
    capability_contract={},
    capability_digest="test-capabilities",
    candidate_digest="test-candidate",
    source_review_digest="test-source",
  )


def test_package_content_digest_from_tree_covers_declared_package_inputs():
  manifest = {
    "id": "package-digest",
    "name": "Package digest",
    "version": "1.0.0",
    "description": "test",
    "entry": "index.jsx",
    "permissions": {},
    "capabilities": {},
    "source_files": ["helper.js"],
    "schedule": {"job": "job.sh"},
    "static_assets": {"logo.txt": "assets/logo.txt"},
    "storage_seeds": {
      "settings.json": {"enabled": True},
      "prompt.md": "prompt.md",
    },
  }
  tree = {
    "mobius.json": json.dumps(manifest).encode(),
    "index.jsx": b"export default 1\n",
    "helper.js": b"export const helper = 1\n",
    "job.sh": b"#!/bin/sh\n",
    "assets/logo.txt": b"logo\n",
    "prompt.md": b"prompt\n",
  }

  manifest_id, digest = install.package_content_digest_from_tree(tree)

  assert manifest_id == "package-digest"
  assert digest == install.package_content_digest(
    manifest=manifest,
    entry_bytes=tree["index.jsx"],
    icon_processed=None,
    bundled_job=tree["job.sh"],
    static_assets={"logo.txt": tree["assets/logo.txt"]},
    source_files={"helper.js": tree["helper.js"]},
    seeds={
      "settings.json": b'{"enabled":true}',
      "prompt.md": tree["prompt.md"],
    },
  )
  changed_tree = {**tree, "assets/logo.txt": b"different\n"}
  assert install.package_content_digest_from_tree(changed_tree)[1] != digest


def test_apply_app_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/apps/apply",
    json={"source_dir": str(Path(get_settings().data_dir) / "apps" / "blocked-app")},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_apply_app_publishes_lifecycle_then_live_preview_relationship(client, auth):
  with patch("app.routes.apps.get_system_broadcast") as mock_get_broadcast:
    app = create_local_app(
      client, auth, name="Trip planner", description="test",
      chat_id="building-chat",
    )

  assert mock_get_broadcast.return_value.publish.call_args_list == [
    call({
      "type": "app_created",
      "appId": str(app["id"]),
      "chatId": "building-chat",
    }),
    call({
      "type": "app_preview_ready",
      "appId": str(app["id"]),
      "chatId": "building-chat",
    }),
  ]


def test_update_app_rejects_cross_site_request(client, auth):
  cross = client.patch(
    "/api/apps/1",
    json={"name": "blocked-app"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_update_app_attaches_distribution_manifest_without_changing_install_identity(
  client, auth, db,
):
  app = create_local_app(
    client, auth, name="Published later", description="test",
  )
  distribution_url = "https://raw.githubusercontent.com/example/app/main/mobius.json"
  row = db.query(models.App).filter(models.App.id == app["id"]).one()
  candidate = _published_candidate(row)

  fetch = AsyncMock(return_value=candidate)
  with patch("app.install.fetch_install_candidate", new=fetch):
    response = client.patch(
      f"/api/apps/{app['id']}",
      json={"published_manifest_url": distribution_url},
      headers=auth,
    )

    assert response.status_code == 200, response.text
    assert response.json()["distribution_manifest"] == {
      "id": app["slug"], "url": distribution_url, "kind": "published",
    }
    assert response.json()["manifest_url"] is None
    db.refresh(row)
    assert row.published_manifest_url == distribution_url
    assert row.manifest_url is None

    cleared = client.patch(
      f"/api/apps/{app['id']}",
      json={"published_manifest_url": ""},
      headers=auth,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["distribution_manifest"] is None
  fetch.assert_awaited_once_with(distribution_url)


def test_update_app_rejects_distribution_package_that_is_not_the_accepted_revision(
  client, auth, db,
):
  app = create_local_app(
    client, auth, name="Stale publication", description="test",
  )
  row = db.query(models.App).filter(models.App.id == app["id"]).one()
  candidate = _published_candidate(row)
  candidate = replace(candidate, entry_bytes=b"export default 'stale'\n")

  with patch(
    "app.install.fetch_install_candidate",
    new=AsyncMock(return_value=candidate),
  ):
    response = client.patch(
      f"/api/apps/{app['id']}",
      json={
        "published_manifest_url": (
          "https://raw.githubusercontent.com/example/app/main/mobius.json"
        ),
      },
      headers=auth,
    )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "distribution_package_mismatch"
  db.expire_all()
  assert (
    db.query(models.App).filter(models.App.id == app["id"]).one()
    .published_manifest_url
  ) is None


def test_update_app_rejects_distribution_manifest_for_a_different_app(
  client, auth, db,
):
  app = create_local_app(
    client, auth, name="Identity publication", description="test",
  )
  row = db.query(models.App).filter(models.App.id == app["id"]).one()
  candidate = _published_candidate(row)
  candidate = replace(
    candidate,
    manifest={**candidate.manifest, "id": "another-app"},
  )

  with patch(
    "app.install.fetch_install_candidate",
    new=AsyncMock(return_value=candidate),
  ):
    response = client.patch(
      f"/api/apps/{app['id']}",
      json={
        "published_manifest_url": (
          "https://raw.githubusercontent.com/example/app/main/mobius.json"
        ),
      },
      headers=auth,
    )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "distribution_identity_mismatch"


def test_update_app_rejects_non_public_distribution_manifest(client, auth):
  app = create_local_app(
    client, auth, name="Private share", description="test",
  )
  response = client.patch(
    f"/api/apps/{app['id']}",
    json={"published_manifest_url": "http://localhost/mobius.json"},
    headers=auth,
  )
  assert response.status_code == 422


def test_owner_can_accept_deleted_chat_scope_without_replacing_store_contract(
  client, auth, db,
):
  app = create_local_app(
    client, auth, name="Reflector", description="test",
  )
  row = db.query(models.App).filter(models.App.id == app["id"]).one()
  reviewed = deepcopy(row.capability_contract)
  reviewed["agent"]["skills"] = ["reflection.md"]
  reviewed["background"] = {
    "job": "fetch.sh", "mode": "scheduled", "cron": "0 6 * * *",
    "user_configurable": True, "initialize_on_install": False,
  }
  row.manifest_url = "https://store.example/reflection/mobius.json"
  row.capability_contract = reviewed
  db.commit()

  response = client.patch(
    f"/api/apps/{app['id']}",
    json={"chat_log_access": "summary_with_deleted"},
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert response.json()["chat_log_access"] == "summary_with_deleted"
  row = db.query(models.App).populate_existing().filter_by(id=app["id"]).one()
  assert row.chat_log_access == "summary_with_deleted"
  assert row.capability_contract["agent"] == reviewed["agent"]
  assert row.capability_contract["background"] == reviewed["background"]
  assert row.capability_contract["data"]["chat_logs"] == {
    "requested": "summary_with_deleted",
    "effective": "summary_with_deleted",
    "redaction": "structural",
  }


def test_owner_chat_log_scope_rejects_retired_full_value(client, auth):
  app = create_local_app(client, auth, name="No raw logs", description="test")

  response = client.patch(
    f"/api/apps/{app['id']}",
    json={"chat_log_access": "full"},
    headers=auth,
  )

  assert response.status_code == 422


def test_owner_chat_log_scope_backfills_a_legacy_store_contract(
  client, auth, db,
):
  app = create_local_app(
    client, auth, name="Legacy reflector", description="test",
  )
  row = db.query(models.App).filter(models.App.id == app["id"]).one()
  row.manifest_url = "https://store.example/legacy/mobius.json"
  row.capability_contract = None
  db.commit()

  response = client.patch(
    f"/api/apps/{app['id']}",
    json={"chat_log_access": "summary_with_deleted"},
    headers=auth,
  )

  assert response.status_code == 200, response.text
  chat_logs = response.json()["capability_contract"]["data"]["chat_logs"]
  assert chat_logs == {
    "requested": "summary_with_deleted",
    "effective": "summary_with_deleted",
    "redaction": "structural",
  }


def test_list_apps_does_not_hydrate_source_or_icon_payloads(client, auth, db):
  app = models.App(
    source_dir="/tmp/mobius-tests/heavy-metadata-test",
    name="Heavy metadata test",
    description="drawer row",
    jsx_source="x" * 1_000_000,
    icon_png=b"x" * 1_000_000,
    slug="heavy-metadata-test",
  )
  db.add(app)
  db.commit()
  statements = []

  def capture(_conn, _cursor, statement, _parameters, _context, _many):
    if "FROM apps" in statement and "ORDER BY apps.pinned_at" in statement:
      statements.append(statement)

  event.listen(engine, "before_cursor_execute", capture)
  try:
    response = client.get("/api/apps/", headers=auth)
  finally:
    event.remove(engine, "before_cursor_execute", capture)

  assert response.status_code == 200
  payload = response.json()
  heavy = next(item for item in payload if item["id"] == app.id)
  assert heavy["icon_url"].startswith(f"/api/apps/{app.id}/icon?v=")
  assert "has_custom_icon" not in heavy
  assert "has_icon" not in heavy
  assert len(statements) == 1
  projection = statements[0].split("FROM apps", 1)[0]
  assert "apps.jsx_source" not in projection
  # The projection may contain icon IS NOT NULL predicates; it must never
  # select either blob itself into an ORM attribute.
  assert "apps.icon_png AS apps_icon_png" not in projection
  assert "apps.icon_override_png AS apps_icon_override_png" not in projection


def test_app_footprint_is_a_fast_manage_apps_projection(client, auth):
  app = create_local_app(client, auth, name="Measured app")
  with patch("app.app_footprint.app_footprint_bytes", return_value=123_456):
    response = client.get(f"/api/apps/{app['id']}/footprint", headers=auth)

  assert response.status_code == 200, response.text
  assert response.json() == {"app_id": app["id"], "bytes": 123_456}


def test_list_apps_exposes_one_fetchable_source_manifest_contract(
  client, auth, db,
):
  app = models.App(
    source_dir="/tmp/mobius-tests/source-manifest-contract",
    name="Published app",
    description="Installed from a published manifest",
    jsx_source="export default function App() { return null }",
    slug="published-app",
    manifest_url=(
      "https://raw.githubusercontent.com/example/app/main"
      "#manifest-id=published-app"
    ),
  )
  db.add(app)
  db.commit()

  response = client.get("/api/apps/", headers=auth)

  assert response.status_code == 200
  payload = next(item for item in response.json() if item["id"] == app.id)
  assert payload["source_manifest"] == {
    "id": "published-app",
    "url": "https://raw.githubusercontent.com/example/app/main/mobius.json",
  }


def test_delete_then_purge_removes_non_slug_source_dir(client, auth, db):
  """Delete is soft (the source tree survives for recovery); the TTL purge
  removes it, using the stored source_dir rather than the display-name slug.
  Feature 110."""
  from datetime import datetime, timedelta, UTC
  from app import models
  source_dir = Path(get_settings().data_dir) / "apps" / "My App (draft)"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )

  app = models.App(
    name="My App (draft)",
    description="legacy non-slug source",
    jsx_source="export default function App() { return <div/> }",
    source_dir=str(source_dir),
    slug="my-app-draft",
  )
  db.add(app)
  db.commit()
  app_id = app.id

  # Soft delete tombstones the app but preserves its source tree.
  r = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert r.status_code == 204
  assert source_dir.exists()

  # Age the tombstone past the TTL; the next list call purges it, resolving the
  # tree via the stored source_dir (not the "My App (draft)" display name).
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()
  client.get("/api/apps/", headers=auth)
  assert not source_dir.exists()


def test_delete_then_purge_preserves_platform_core_source(client, auth, db):
  """A legacy row may point at /data/platform/core-apps; TTL purge must not
  remove source outside /data/apps."""
  from datetime import datetime, timedelta, UTC
  from app import models
  data_dir = Path(get_settings().data_dir)
  source_dir = data_dir / "platform" / "core-apps" / "memory"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )
  app = models.App(
    name="Memory",
    description="legacy platform row",
    jsx_source="export default function App() { return <div/> }",
    source_dir=str(source_dir),
    slug="memory",
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()
  app_id = app.id

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()
  client.get("/api/apps/", headers=auth)

  assert source_dir.exists()
  assert (source_dir / "index.jsx").exists()


def test_delete_scheduled_app_disables_own_cron_replay(
  client, auth,
):
  """Deleting a scheduled app tombstones the replay script in its source tree."""
  data_dir = Path(get_settings().data_dir)
  source_dir = data_dir / "apps" / "reflection"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )
  replay = source_dir / "init-cron.sh"
  replay.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

  app_id = create_local_app(
    client, auth, name="Reflection", description="test",
    source_dir=source_dir,
  )["id"]

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204

  assert not replay.exists()
  assert (source_dir / "init-cron.sh.tombstoned").exists()
  assert source_dir.exists()




def test_app_token_can_update_own_schedule_only(client, auth, monkeypatch):
  calls = []

  def fake_register(slug, schedule_expr, job_path, app_id=None):
    calls.append((slug, schedule_expr, job_path.name, app_id))

  monkeypatch.setattr("app.app_cron.register_cron", fake_register)
  source_dir = Path(get_settings().data_dir) / "apps" / "news"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")

  app_id = create_local_app(
    client, auth, name="News", description="test", source_dir=source_dir,
  )["id"]

  token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=auth,
  ).json()["token"]
  app_auth = {"Authorization": f"Bearer {token}"}

  r = client.post(
    f"/api/apps/{app_id}/schedule",
    json={"cron": "15 7 * * *", "job": "fetch.sh"},
    headers=app_auth,
  )
  assert r.status_code == 200, r.text
  assert calls == [("news", "15 7 * * *", "fetch.sh", app_id)]

  r = client.post(
    f"/api/apps/{app_id + 1}/schedule",
    json={"cron": "15 8 * * *", "job": "fetch.sh"},
    headers=app_auth,
  )
  assert r.status_code == 403


def test_schedule_update_with_timezone_materializes_and_declares(
  client, auth, monkeypatch,
):
  """A timezone-owned schedule registers the truthful every-minute gate plus
  its durable identity; invalid contracts never reach registration."""
  calls = []

  def fake_register(slug, schedule_expr, job_path, app_id=None,
                    timezone=None, zone_cron=None):
    calls.append((slug, schedule_expr, job_path.name, app_id,
                  timezone, zone_cron))

  monkeypatch.setattr("app.app_cron.register_cron", fake_register)
  monkeypatch.setattr(
    "app.cron_tz.materialize_zone_cron",
    lambda zone_cron, tz_name: "* * * * *",
  )
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  app_id = create_local_app(
    client, auth, name="Memory", description="test", source_dir=source_dir,
  )["id"]

  r = client.post(
    f"/api/apps/{app_id}/schedule",
    json={"cron": "0 5 * * *", "job": "fetch.sh",
          "timezone": "Europe/Belgrade"},
    headers=auth,
  )
  assert r.status_code == 200, r.text
  assert r.json() == {
    "cron": "* * * * *", "job": "fetch.sh",
    "timezone": "Europe/Belgrade", "zone_cron": "0 5 * * *",
  }
  assert calls == [
    ("memory", "* * * * *", "fetch.sh", app_id,
     "Europe/Belgrade", "0 5 * * *"),
  ]

  r = client.post(
    f"/api/apps/{app_id}/schedule",
    json={"cron": "0 5 * * *", "timezone": "Not/AZone"},
    headers=auth,
  )
  assert r.status_code == 400
  r = client.post(
    f"/api/apps/{app_id}/schedule",
    json={"cron": "0 5 * * 1", "timezone": "Europe/Belgrade"},
    headers=auth,
  )
  assert r.status_code == 400
  assert len(calls) == 1


def test_reconcile_restores_zone_schedule_as_wall_clock_gate(client, auth, db):
  """Reconciliation restores the gate from the durable IANA identity."""
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "init-cron.sh").write_text(
    f'ENTRY="* * * * * {source_dir}/fetch.sh 56"\n'
    'SCHEDULE_TZ="Europe/Belgrade"\n'
    'SCHEDULE_SOURCE="0 5 * * *"\n',
    encoding="utf-8",
  )
  app_id = create_local_app(
    client, _service_auth(), name="Memory", description="test",
    source_dir=source_dir,
  )["id"]

  from app.routes import app_schedules as apps_module
  calls = []

  def fake_register(slug, schedule_expr, job_path, app_id=None,
                    timezone=None, zone_cron=None):
    calls.append((slug, schedule_expr, timezone, zone_cron))

  with patch("app.app_cron.register_cron", fake_register), \
       patch("app.cron_tz.materialize_zone_cron",
             lambda zone_cron, tz_name: "* * * * *"):
    count, warnings = apps_module.reconcile_app_cron_supervision(db)

  assert warnings == []
  assert count == 1
  assert calls == [("memory", "* * * * *", "Europe/Belgrade", "0 5 * * *")]


def test_reconcile_fails_closed_on_malformed_zone_declaration(
  client, auth, db,
):
  """A damaged declaration cannot turn the gate into an every-minute job."""
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "init-cron.sh").write_text(
    f'ENTRY="* * * * * {source_dir}/fetch.sh 56"\n'
    'SCHEDULE_TZ="Europe/Belgrade"\n',
    encoding="utf-8",
  )
  create_local_app(
    client, _service_auth(), name="Memory", description="test",
    source_dir=source_dir,
  )

  from app.routes import app_schedules as apps_module
  with patch("app.app_cron.register_cron") as register:
    count, warnings = apps_module.reconcile_app_cron_supervision(db)

  assert count == 0
  assert len(warnings) == 1
  assert "Incomplete IANA wall-clock schedule declaration" in warnings[0]
  register.assert_not_called()


def test_app_schedules_expose_zone_declaration(client, auth):
  """A schedule owned in an IANA zone surfaces its durable identity."""
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "init-cron.sh").write_text(
    f'ENTRY="* * * * * {source_dir}/fetch.sh 56"\n'
    "# Zone-aware schedule identity (platform-managed).\n"
    'SCHEDULE_TZ="Europe/Belgrade"\n'
    'SCHEDULE_SOURCE="0 5 * * *"\n',
    encoding="utf-8",
  )
  create_local_app(
    client, _service_auth(), name="Memory", description="test",
    source_dir=source_dir,
  )
  r = client.get("/api/apps/schedules", headers=auth)
  assert r.status_code == 200, r.text
  rows = r.json()
  assert [(j["cron"], j["timezone"], j["zone_cron"]) for j in rows] == [
    ("* * * * *", "Europe/Belgrade", "0 5 * * *"),
  ]


def test_renamed_job_debris_does_not_shadow_the_real_schedule(client, auth, db):
  """A crontab line for a job the app no longer ships is not a schedule.

  Registration is add-only, so renaming a job (news-2: job.sh -> fetch.sh)
  strands the old supervised entry. Because discovery took the first matching
  line, the stranded one won on sort order alone: the schedules API reported
  the phantom job at its every-minute gate cadence, and reconciliation then
  failed the app entirely on its own is_file() guard.
  """
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "init-cron.sh").write_text(
    f'ENTRY="0 10 * * * {source_dir}/fetch.sh 56"\n',
    encoding="utf-8",
  )
  create_local_app(
    client, _service_auth(), name="Memory", description="test",
    source_dir=source_dir,
  )
  runner = "/data/platform/backend/scripts/app-job-runner.py"
  # The stranded entry sorts BEFORE the live one, exactly as observed.
  live = (
    f"* * * * * python3 {runner} 56 {source_dir}/job.sh\n"
    f"0 10 * * * python3 {runner} 56 {source_dir}/fetch.sh\n"
  )

  from app.routes import app_schedules as apps_module
  with patch("app.app_cron.read_crontab", lambda: live):
    r = client.get("/api/apps/schedules", headers=auth)
    assert r.status_code == 200, r.text
    assert [(j["cron"], j["job"]) for j in r.json()] == [("0 10 * * *", "fetch.sh")]

    with patch("app.app_cron.register_cron") as register, \
         patch("app.app_cron.write_crontab", return_value=True) as write:
      count, warnings = apps_module.reconcile_app_cron_supervision(db)

  assert warnings == []
  assert count == 1
  assert register.call_args.args[2] == source_dir / "fetch.sh"
  # The dead line is retired; the live one survives untouched.
  written = write.call_args.args[0]
  assert f"{source_dir}/job.sh" not in written
  assert f"{source_dir}/fetch.sh" in written


def test_prune_spares_unsupervised_and_unreadable_crontabs(client, auth, db):
  """Pruning only ever retires a supervised entry whose job is really gone."""
  from app.routes import app_schedules as apps_module
  apps_root = Path(get_settings().data_dir) / "apps"
  apps_root.mkdir(parents=True, exist_ok=True)
  runner = "/data/platform/backend/scripts/app-job-runner.py"

  # An owner's own line naming a missing script is NOT platform-supervised.
  assert not apps_module._is_orphaned_supervised_entry(
    f"* * * * * {apps_root}/_self-reminders/job.sh", apps_root,
  )
  # Neither are comments or env assignments.
  assert not apps_module._is_orphaned_supervised_entry("# a comment", apps_root)
  assert not apps_module._is_orphaned_supervised_entry("PATH=/usr/bin", apps_root)
  # A supervised entry whose job is gone is debris.
  assert apps_module._is_orphaned_supervised_entry(
    f"* * * * * python3 {runner} 61 {apps_root}/gone/job.sh", apps_root,
  )
  # A failed crontab READ must never trigger a rewrite from a partial view.
  with patch("app.app_cron.read_crontab", lambda: None), \
       patch("app.app_cron.write_crontab") as write:
    assert apps_module._prune_orphaned_supervised_entries(apps_root) == []
  write.assert_not_called()


def test_platform_source_patch_rejected_and_store_identity_preserved(client, auth, db):
  from app import models
  data_dir = Path(get_settings().data_dir)
  old_source = data_dir / "apps" / "memory"
  old_source.mkdir(parents=True, exist_ok=True)
  source_dir = data_dir / "platform" / "core-apps" / "memory"
  source_dir.mkdir(parents=True, exist_ok=True)
  (source_dir / "index.jsx").write_text(
    "export default function App() { return <div/> }",
    encoding="utf-8",
  )
  app = models.App(
    name="Memory",
    description="store-managed core",
    jsx_source="export default function App() { return <div/> }",
    source_dir=str(old_source),
    slug="memory",
    manifest_url="https://raw.githubusercontent.com/mobius-os/app-memory/main/mobius.json",
    version="1.2.3",
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()
  app_id = app.id

  r = client.patch(
    f"/api/apps/{app_id}",
    json={"source_dir": str(source_dir)},
    headers=_service_auth(),
  )
  assert r.status_code == 422, r.text
  db.refresh(app)
  assert app.source_dir == str(old_source)
  assert app.manifest_url == "https://raw.githubusercontent.com/mobius-os/app-memory/main/mobius.json"
  assert app.version == "1.2.3"


def test_app_schedules_are_readable_by_app_tokens(client, auth):
  source_dir = Path(get_settings().data_dir) / "apps" / "news"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "mobius.json").write_text(
    '{"schedule":{"default":"0 10 * * *","job":"fetch.sh"}}',
    encoding="utf-8",
  )

  create_local_app(
    client, auth, name="News", description="test", source_dir=source_dir,
    manifest_extra={"schedule": {"default": "0 10 * * *", "job": "fetch.sh"}},
  )

  tasks = create_local_app(client, auth, name="Tasks", description="test")
  token = client.post(
    "/api/auth/app-token", json={"app_id": tasks["id"]}, headers=auth,
  ).json()["token"]

  r = client.get(
    "/api/apps/schedules",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert r.status_code == 200, r.text
  from app import cron_tz
  assert r.json() == [{
    "id": 1,
    "name": "News",
    "slug": "news",
    "cron": "0 10 * * *",
    "job": "fetch.sh",
    "next_run": None,
    "timezone": None,
    "zone_cron": None,
    "server_timezone": cron_tz.server_timezone_name(),
  }]


def test_app_schedules_prefer_init_cron_over_manifest(client, auth):
  source_dir = Path(get_settings().data_dir) / "apps" / "reflection"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (source_dir / "mobius.json").write_text(
    '{"schedule":{"default":"0 10 * * *","job":"fetch.sh"}}',
    encoding="utf-8",
  )
  (source_dir / "init-cron.sh").write_text(
    f'ENTRY="0 6 * * * {source_dir}/fetch.sh 56"\n',
    encoding="utf-8",
  )

  create_local_app(
    client, _service_auth(), name="Reflection", description="test",
    source_dir=source_dir,
    manifest_extra={"schedule": {"default": "0 10 * * *", "job": "fetch.sh"}},
  )

  r = client.get("/api/apps/schedules", headers=auth)
  assert r.status_code == 200, r.text
  assert [(job["cron"], job["job"]) for job in r.json()] == [
    ("0 6 * * *", "fetch.sh")
  ]


def test_app_schedules_resolve_supervised_runner_job(client, auth):
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  (source_dir / "fetch.sh").write_text("#!/bin/sh\n", encoding="utf-8")

  app_id = create_local_app(
    client, _service_auth(), name="Memory", description="test",
    source_dir=source_dir,
  )["id"]

  from app.routes import app_schedules as apps_module
  supervised = (
    "15 4 * * * python3 /app/scripts/app-job-runner.py "
    f"{app_id} {source_dir}/fetch.sh"
  )
  with patch.object(apps_module, "_read_live_crontab", return_value=supervised):
    r = client.get("/api/apps/schedules", headers=auth)

  assert r.status_code == 200, r.text
  assert [(job["cron"], job["job"]) for job in r.json()] == [
    ("15 4 * * *", "fetch.sh")
  ]


def test_boot_reconciles_legacy_direct_cron_through_runner(client, db):
  source_dir = Path(get_settings().data_dir) / "apps" / "memory"
  source_dir.mkdir(parents=True)
  job = source_dir / "fetch.sh"
  job.write_text("#!/bin/sh\n", encoding="utf-8")
  app = models.App(
    name="Memory",
    slug="memory",
    description="test",
    jsx_source="export default function App() { return <div/> }",
    source_dir=str(source_dir),
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  from app.routes import app_schedules as apps_module
  direct = f"15 4 * * * {source_dir}/fetch.sh {app.id}"
  with patch.object(apps_module, "_read_live_crontab", return_value=direct), \
       patch("app.app_cron.register_cron") as register:
    count, warnings = apps_module.reconcile_app_cron_supervision(db)

  assert count == 1
  assert warnings == []
  register.assert_called_once_with(
    "memory", "15 4 * * *", job.resolve(), app.id,
  )


def _make_icon_app(client, auth, db):
  """An app row whose `icon_png` is a large (512px) PNG, so a ?size= variant
  is provably smaller. Returns the app id."""
  import io
  from PIL import Image
  from app import models
  app_id = create_local_app(
    client, auth, name="Iconic", description="test",
  )["id"]
  buf = io.BytesIO()
  # RGBA with varied pixels so optimize=True can't collapse it to a few bytes.
  img = Image.new("RGBA", (512, 512))
  img.putdata([
    ((x * 7) % 256, (y * 5) % 256, (x + y) % 256, 255)
    for y in range(512) for x in range(512)
  ])
  img.save(buf, format="PNG")
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.icon_png = buf.getvalue()
  db.commit()
  return app_id


def test_get_icon_size_returns_smaller_cached_variant(client, auth, db):
  """?size= serves a Pillow-downscaled PNG (fewer bytes) with the size folded
  into the ETag and a long cache header; default (no size) is unchanged."""
  app_id = _make_icon_app(client, auth, db)
  _CC = "public, max-age=3600, stale-while-revalidate=86400"

  full = client.get(f"/api/apps/{app_id}/icon")
  assert full.status_code == 200
  assert full.headers["Cache-Control"] == _CC
  full_etag = full.headers["ETag"]

  small = client.get(f"/api/apps/{app_id}/icon", params={"size": 64})
  assert small.status_code == 200
  assert small.headers["Content-Type"] == "image/png"
  assert small.headers["Cache-Control"] == _CC
  # The downscaled variant is strictly smaller than the full-res icon.
  assert len(small.content) < len(full.content)
  # Its ETag folds the size in, so it caches independently of the full-res one.
  assert small.headers["ETag"] != full_etag
  assert small.headers["ETag"].endswith('-64"')

  # The variant ETag round-trips to a 304 with the same cache header.
  again = client.get(
    f"/api/apps/{app_id}/icon",
    params={"size": 64},
    headers={"If-None-Match": small.headers["ETag"]},
  )
  assert again.status_code == 304
  assert again.headers["Cache-Control"] == _CC

  # A URL carrying the app's exact updated_at is content-addressed from the
  # browser's perspective: any icon-changing update advances updated_at and
  # therefore produces a new URL. Keep this response indefinitely so reopening
  # the App Store never re-downloads unchanged icons.
  row = db.query(models.App).filter(models.App.id == app_id).first()
  versioned = client.get(
    f"/api/apps/{app_id}/icon",
    params={"size": 128, "v": row.updated_at.isoformat()},
  )
  assert versioned.status_code == 200
  assert versioned.headers["Cache-Control"] == "public, max-age=31536000, immutable"

  # A guessed/stale version must not earn immutable caching.
  stale_version = client.get(
    f"/api/apps/{app_id}/icon",
    params={"size": 128, "v": "stale"},
  )
  assert stale_version.status_code == 200
  assert stale_version.headers["Cache-Control"] == _CC


def test_get_icon_rejects_unsupported_size(client, auth, db):
  """An unsupported ?size= is a 400 so the variant cache can't be flooded."""
  app_id = _make_icon_app(client, auth, db)
  r = client.get(f"/api/apps/{app_id}/icon", params={"size": 999})
  assert r.status_code == 400


def test_icon_override_is_separate_and_zero_body_returns_to_package(
  client, auth, db,
):
  """Home-screen customization never overwrites the accepted package icon."""
  import io
  from PIL import Image
  from app import icon_assets, models

  app_id = _make_icon_app(client, auth, db)
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  package_icon = row.icon_png

  raw = io.BytesIO()
  Image.new("RGB", (40, 24), (240, 80, 60)).save(raw, format="PNG")
  expected_override = icon_assets.normalize_icon(raw.getvalue())
  uploaded = client.put(
    f"/api/apps/{app_id}/icon", content=raw.getvalue(), headers=auth,
  )

  assert uploaded.status_code == 204, uploaded.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.icon_png == package_icon
  assert row.icon_override_png == expected_override
  assert client.get(f"/api/apps/{app_id}/icon").content == expected_override

  reset = client.put(f"/api/apps/{app_id}/icon", content=b"", headers=auth)

  assert reset.status_code == 204, reset.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.icon_override_png is None
  assert client.get(f"/api/apps/{app_id}/icon").content == package_icon


def test_get_icon_variant_is_byte_identical_and_cached_on_disk(client, auth, db):
  """A ?size= variant is deterministic: two fetches return identical bytes, and
  the second is served from the icon_cache (RAM/disk) rather than recomputed.
  Asserting on the on-disk cache file proves the downscale is memoized, not
  re-run per request — the fix for the staggered icon trickle."""
  from app import icon_cache
  from app.config import get_settings
  import pathlib

  app_id = _make_icon_app(client, auth, db)

  first = client.get(f"/api/apps/{app_id}/icon", params={"size": 128})
  assert first.status_code == 200
  second = client.get(f"/api/apps/{app_id}/icon", params={"size": 128})
  assert second.status_code == 200
  # Deterministic render → byte-identical across requests.
  assert first.content == second.content

  # The downscaled bytes were written to the on-disk cache and match the
  # response body exactly (so a warm hit serves these bytes with no Pillow).
  cache_dir = pathlib.Path(get_settings().data_dir) / "compiled" / "icons"
  files = list(cache_dir.glob(f"{app_id}-embed-128-*"))
  assert files, f"expected a cached icon variant under {cache_dir}"
  assert files[0].read_bytes() == first.content


def test_get_icon_variant_cache_busts_on_app_update(client, auth, db):
  """Changing the stored icon advances app.updated_at, which changes the cache
  key — so the new icon is served, never the stale cached variant."""
  import io
  from PIL import Image
  from app import models

  app_id = _make_icon_app(client, auth, db)
  before = client.get(f"/api/apps/{app_id}/icon", params={"size": 64})
  assert before.status_code == 200

  # Replace the stored icon with a visibly different image and bump updated_at
  # the way a real icon upload does.
  buf = io.BytesIO()
  img = Image.new("RGBA", (512, 512))
  img.putdata([
    ((x * 3) % 256, (y * 11) % 256, (x * y) % 256, 255)
    for y in range(512) for x in range(512)
  ])
  img.save(buf, format="PNG")
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.icon_png = buf.getvalue()
  row.updated_at = row.updated_at.replace(microsecond=(row.updated_at.microsecond + 1) % 1000000)
  db.commit()

  after = client.get(f"/api/apps/{app_id}/icon", params={"size": 64})
  assert after.status_code == 200
  assert after.content != before.content, "stale cached variant served after update"
