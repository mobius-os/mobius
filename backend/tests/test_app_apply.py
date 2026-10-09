"""Explicit mini-app source application."""
from app.chat_writer import create_chat

import io
import json
from pathlib import Path
from unittest.mock import call, patch

import pytest

from app import app_apply, app_git, icon_assets, models
from app.config import get_settings
from app.database import SessionLocal


def _source(slug: str = "demo") -> Path:
  root = Path(get_settings().data_dir) / "apps" / slug
  root.mkdir(parents=True)
  (root / "mobius.json").write_text(json.dumps({
    "id": slug,
    "name": "Demo",
    "version": "0.1.0",
    "description": "A focused demo.",
    "entry": "index.jsx",
    "offline_capable": True,
    "permissions": {},
    "source_files": [],
  }))
  (root / "index.jsx").write_text(
    "export default function App() { return <div>first</div> }\n"
  )
  return root


def _apply(
  client, auth, source: Path, chat_id: str | None = None,
  *, accept_local_package: bool = False,
):
  body = {"source_dir": str(source)}
  if chat_id is not None:
    body["chat_id"] = chat_id
  if accept_local_package:
    body["accept_local_package"] = True
  return client.post("/api/apps/apply", json=body, headers=auth)


def _icon_bytes(color: tuple[int, int, int]) -> bytes:
  from PIL import Image
  output = io.BytesIO()
  Image.new("RGB", (30, 18), color).save(output, format="PNG")
  return output.getvalue()


def _declare_icon(source: Path, raw: bytes, name: str = "icon.png") -> None:
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["icon"] = name
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / name).write_bytes(raw)


def _declare_schedule(source: Path) -> None:
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["schedule"] = {
    "default": "*/10 * * * *",
    "user_configurable": False,
    "job": "job.sh",
  }
  (source / "mobius.json").write_text(json.dumps(manifest))
  job = source / "job.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  job.chmod(0o755)


def _declare_model_provider(source: Path, **changes) -> dict:
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["model_provider"] = {
    "name": "Example Models", "base_url": "https://models.example.com/v1",
    "secret_name": "api_key", "default_model": "example/model-a",
    "models": [{"id": "example/model-a", "label": "Model A"}],
    **changes,
  }
  (source / "mobius.json").write_text(json.dumps(manifest))
  return manifest["model_provider"]


def test_apply_creates_from_manifest_and_commits_exact_source(
  client, auth, db, chat,
):
  source = _source()

  response = _apply(client, auth, source, chat.id)

  assert response.status_code == 200, response.text
  body = response.json()
  assert body["mode"] == "created"
  assert body["app"]["name"] == "Demo"
  assert body["app"]["slug"] == "demo"
  assert body["app"]["offline_capable"] is True
  assert body["app"]["chat_id"] == chat.id
  row = db.query(models.App).populate_existing().one()
  assert row.jsx_source.endswith("<div>first</div> }\n")
  assert Path(row.compiled_path).is_file()
  assert row.source_commit == app_git.head_sha(
    source, app_git.LOCAL_BRANCH,
  )
  assert app_git.read_ref_tree(source, app_git.LOCAL_BRANCH)["index.jsx"] == (
    source / "index.jsx"
  ).read_bytes()
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_new_app_compile_does_not_hold_sqlite_write_lock(
  client, auth, monkeypatch,
):
  """A slow first build must not block unrelated durable owner work."""
  source = _source()
  concurrent_chat_id = "chat-created-during-app-compile"

  async def compile_while_chat_is_created(
    _source, *, out_path, source_path=None,
  ):
    del source_path
    concurrent = SessionLocal()
    try:
      # A regression should fail promptly instead of waiting out the live
      # five-second busy timeout. WAL permits this write while the apply owns
      # only its preflight read transaction; an early App INSERT does not.
      concurrent.connection().exec_driver_sql("PRAGMA busy_timeout=50")
      concurrent.add(create_chat(
        id=concurrent_chat_id,
        title="Concurrent chat",
        messages=[],
        pending_messages=[],
      ))
      concurrent.commit()
    finally:
      concurrent.close()
    Path(out_path).write_text("export default function App(){}\n")
    return str(out_path)

  monkeypatch.setattr(app_apply, "compile_jsx", compile_while_chat_is_created)

  response = _apply(client, auth, source)

  assert response.status_code == 200, response.text
  verify = SessionLocal()
  try:
    assert verify.get(models.Chat, concurrent_chat_id) is not None
  finally:
    verify.close()


@pytest.mark.parametrize("mode", ["created", "updated"])
def test_apply_holds_no_sqlite_write_lock_across_its_awaits(
  client, auth, monkeypatch, mode,
):
  """No awaited apply phase may run inside the row's write transaction.

  An async route that commits waits on SQLite inside the event loop, so a
  write lock held across an await stalls both requests until the busy timeout
  fails the unrelated one. Environment preparation is the last awaited phase,
  after compilation, the Git commit and runtime staging.
  """
  source = _source()
  if mode == "updated":
    assert _apply(client, auth, source).status_code == 200
    (source / "index.jsx").write_text(
      "export default function App() { return <div>second</div> }\n"
    )
  concurrent_chat_id = f"chat-created-during-app-{mode}"
  prepare_env = app_apply.app_python_env.prepare_env
  write_errors = []

  def prepare_env_while_chat_is_created(*args, **kwargs):
    concurrent = SessionLocal()
    try:
      # Fail promptly instead of waiting out the live five-second timeout.
      concurrent.connection().exec_driver_sql("PRAGMA busy_timeout=50")
      concurrent.add(create_chat(
        id=concurrent_chat_id,
        title="Concurrent chat",
        messages=[],
        pending_messages=[],
      ))
      concurrent.commit()
    except Exception as exc:  # recorded so the assertion names the lock
      write_errors.append(repr(exc))
    finally:
      concurrent.close()
    return prepare_env(*args, **kwargs)

  monkeypatch.setattr(
    app_apply.app_python_env, "prepare_env", prepare_env_while_chat_is_created,
  )

  response = _apply(client, auth, source)

  assert response.status_code == 200, response.text
  assert response.json()["mode"] == mode
  assert write_errors == []


def test_apply_updates_multifile_revision_once(client, auth, db):
  source = _source()
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  app_id = created.json()["app"]["id"]
  first_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "helper.js").write_text("export const label = 'second'\n")
  (source / "index.jsx").write_text(
    "import { label } from './helper.js'\n"
    "export default function App() { return <div>{label}</div> }\n"
  )

  with patch("app.routes.apps.get_system_broadcast") as mock_get_broadcast:
    updated = _apply(client, auth, source, "editing-chat")

  assert updated.status_code == 200, updated.text
  assert updated.json()["mode"] == "updated"
  second_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  assert second_head != first_head
  tree = app_git.read_ref_tree(source, app_git.LOCAL_BRANCH)
  assert tree["helper.js"] == b"export const label = 'second'\n"
  assert b"import { label }" in tree["index.jsx"]
  assert app_git._run(
    source, "rev-list", "--count", f"{first_head}..{second_head}",
  ).stdout.strip() == "1"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert "import { label }" in row.jsx_source
  artifact = db.get(models.ChatAppArtifact, ("editing-chat", app_id))
  assert artifact is not None
  assert artifact.touched_at == row.updated_at
  assert mock_get_broadcast.return_value.publish.call_args_list == [
    call({"type": "app_updated", "appId": str(app_id)}),
    call({
      "type": "app_preview_ready",
      "appId": str(app_id),
      "chatId": "editing-chat",
    }),
  ]


def test_shell_shortcuts_are_on_unless_the_manifest_opts_out(client, auth, db):
  source = _source()
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  assert created.json()["app"]["shell_shortcuts"] is True
  app_id = created.json()["app"]["id"]

  manifest = json.loads((source / "mobius.json").read_text())
  manifest["shell_shortcuts"] = False
  (source / "mobius.json").write_text(json.dumps(manifest))
  opted_out = _apply(client, auth, source)
  assert opted_out.status_code == 200, opted_out.text
  assert opted_out.json()["app"]["shell_shortcuts"] is False
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.shell_shortcuts is False

  # Removing the declaration restores the default rather than keeping the
  # previous value.
  del manifest["shell_shortcuts"]
  (source / "mobius.json").write_text(json.dumps(manifest))
  restored = _apply(client, auth, source)
  assert restored.status_code == 200, restored.text
  assert restored.json()["app"]["shell_shortcuts"] is True

  manifest["shell_shortcuts"] = "no"
  (source / "mobius.json").write_text(json.dumps(manifest))
  rejected = _apply(client, auth, source)
  assert rejected.status_code >= 400
  assert "shell_shortcuts" in rejected.text


def test_apply_probes_the_implicit_service_identity_it_will_keep(
  client, auth, db,
):
  source = _source("shared-service-2")
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  target = db.get(models.App, created.json()["app"]["id"])
  target.manifest_url = (
    "https://example.test/second#manifest-id=shared-service"
  )
  target.service_id = "shared-service-2"
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["id"] = "shared-service"
  manifest["source_files"] = ["service.py"]
  manifest["service"] = {"entry": "service.py"}
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "service.py").write_text(
    'import json, sys\njson.dump({"status": 200}, sys.stdout)\n'
  )
  owner = models.App(
    name="First", slug="shared-service", description="",
    source_dir=str(Path(get_settings().data_dir) / "apps" / "first"),
    jsx_source="export default () => null", service_id="shared-service",
  )
  db.add(owner)
  db.commit()

  response = _apply(client, auth, source)

  assert response.status_code == 200, response.text
  db.refresh(target)
  assert target.service_id == "shared-service-2"


def test_local_apply_materializes_versioned_static_assets(client, auth):
  source = _source()
  listing_source = source / "listing-assets" / "screen.png"
  listing_source.parent.mkdir()
  listing_source.write_bytes(b"accepted-screen-v1")
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {
    "listing/screen.png": "listing-assets/screen.png",
  }
  (source / "mobius.json").write_text(json.dumps(manifest))

  created = _apply(client, auth, source)

  assert created.status_code == 200, created.text
  served = source / "static" / "listing" / "screen.png"
  assert served.read_bytes() == b"accepted-screen-v1"
  assert app_git._run(source, "status", "--porcelain").stdout == ""

  listing_source.write_bytes(b"accepted-screen-v2")
  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  assert served.read_bytes() == b"accepted-screen-v2"

  manifest = json.loads((source / "mobius.json").read_text())
  manifest.pop("static_assets")
  (source / "mobius.json").write_text(json.dumps(manifest))
  removed = _apply(client, auth, source)

  assert removed.status_code == 200, removed.text
  assert not served.exists()


def test_local_apply_has_no_static_asset_count_cap(client, auth):
  """Local apply once capped static assets at 256 files. The manifest's byte
  cap already bounds how many can be listed, and the package byte bound limits
  their size."""
  source = _source()
  asset_sources = source / "listing-assets"
  asset_sources.mkdir()
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {}
  for index in range(300):
    name = f"item-{index}.txt"
    (asset_sources / name).write_text(str(index))
    manifest["static_assets"][f"listing/{name}"] = f"listing-assets/{name}"
  (source / "mobius.json").write_text(json.dumps(manifest))

  accepted = _apply(client, auth, source)

  assert accepted.status_code == 200, accepted.text
  assert len(list((source / "static" / "listing").iterdir())) == 300


def test_local_apply_refuses_an_oversized_package_before_reading_it(
  client, auth, monkeypatch,
):
  """Local apply bounds the same declared files install downloads, from their
  sizes on disk, before it materializes anything."""
  from app import app_apply
  from app.manifest_contract import package_bytes_on_disk

  source = _source()
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  accepted_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "data.bin").write_bytes(b"x" * 64)
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {"data.bin": "data.bin"}
  (source / "mobius.json").write_text(json.dumps(manifest))
  monkeypatch.setattr(
    app_apply, "PACKAGE_MAX_BYTES", package_bytes_on_disk(source, manifest) - 1,
  )

  def read_assets(*_args):
    raise AssertionError("static assets were read before the size check")

  monkeypatch.setattr(app_apply, "_snapshot_static_assets", read_assets)

  rejected = _apply(client, auth, source)

  assert rejected.status_code == 422, rejected.text
  assert rejected.json()["detail"]["code"] == "package_too_large"
  assert "MiB app package limit" in rejected.json()["detail"]["message"]
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == accepted_head
  assert not (source / "static" / "data.bin").exists()


@pytest.mark.parametrize("symlink_component", ["source", "parent"])
def test_local_apply_rejects_symlinked_static_asset_sources_before_read(
  client, auth, symlink_component,
):
  source = _source()
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  accepted_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  outside = source.parent / "private-static-source.txt"
  outside.write_text("private bytes must not publish")
  asset_sources = source / "listing-assets"
  source_name = "screen.txt"
  if symlink_component == "source":
    asset_sources.mkdir()
    (asset_sources / source_name).symlink_to(outside)
  else:
    asset_sources.symlink_to(source.parent, target_is_directory=True)
    source_name = outside.name
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {
    "listing/screen.txt": f"listing-assets/{source_name}",
  }
  (source / "mobius.json").write_text(json.dumps(manifest))

  rejected = _apply(client, auth, source)

  assert rejected.status_code == 422, rejected.text
  assert rejected.json()["detail"]["code"] == "static_asset_symlink"
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == accepted_head
  assert outside.read_text() == "private bytes must not publish"
  assert not (source / "static" / "listing" / "screen.txt").exists()


@pytest.mark.parametrize("symlink_component", ["static", "parent"])
def test_local_apply_rejects_symlinked_static_destinations_before_write(
  client, auth, symlink_component,
):
  source = _source()
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  accepted_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  asset_sources = source / "listing-assets"
  asset_sources.mkdir()
  (asset_sources / "screen.txt").write_text("safe app asset")
  outside = source.parent / "escaped-static-output"
  outside.mkdir()
  static_root = source / "static"
  if symlink_component == "static":
    static_root.symlink_to(outside, target_is_directory=True)
  else:
    static_root.mkdir()
    (static_root / "listing").symlink_to(outside, target_is_directory=True)
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {
    "listing/screen.txt": "listing-assets/screen.txt",
  }
  (source / "mobius.json").write_text(json.dumps(manifest))

  rejected = _apply(client, auth, source)

  assert rejected.status_code == 422, rejected.text
  assert rejected.json()["detail"]["code"] == "static_asset_symlink"
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == accepted_head
  assert not (outside / "screen.txt").exists()


@pytest.mark.parametrize(
  "destination",
  [".mobius-static-assets.json", ".mobius-static-assets.json/screen.txt"],
)
def test_static_asset_backups_do_not_collide_with_metadata_on_rollback(
  client, auth, monkeypatch, destination,
):
  source = _source()
  asset = source / "listing-assets" / "screen.txt"
  asset.parent.mkdir()
  asset.write_bytes(b"accepted-screen-v1")
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {destination: "listing-assets/screen.txt"}
  (source / "mobius.json").write_text(json.dumps(manifest))

  created = _apply(client, auth, source)

  assert created.status_code == 200, created.text
  served = source / "static" / destination
  metadata = source / ".mobius-static-assets.json"
  previous_metadata = metadata.read_bytes()
  asset.write_bytes(b"accepted-screen-v2")
  original_commit = app_apply.Session.commit
  calls = 0

  def fail_once(session):
    nonlocal calls
    calls += 1
    if calls == 1:
      raise RuntimeError("simulated database failure")
    return original_commit(session)

  monkeypatch.setattr(app_apply.Session, "commit", fail_once)
  with pytest.raises(RuntimeError, match="simulated database failure"):
    _apply(client, auth, source)

  assert served.read_bytes() == b"accepted-screen-v1"
  assert metadata.read_bytes() == previous_metadata
  assert not (source.parent / ".demo.mobius-static-bak").exists()

  retry = _apply(client, auth, source)

  assert retry.status_code == 200, retry.text
  assert served.read_bytes() == b"accepted-screen-v2"
  assert not (source.parent / ".demo.mobius-static-bak").exists()


def test_local_apply_converges_schedule_creation_and_removal(client, auth):
  source = _source()
  _declare_schedule(source)

  with patch("app.app_cron.register_cron") as register:
    created = _apply(client, auth, source)

  assert created.status_code == 200, created.text
  app_id = created.json()["app"]["id"]
  assert register.call_args.args[:4] == (
    "demo", "*/10 * * * *", source / "job.sh", app_id,
  )

  # Stand in for the durable declaration written by the real scaffold.
  init_cron = source / "init-cron.sh"
  init_cron.write_text("#!/bin/sh\nexit 0\n")
  manifest = json.loads((source / "mobius.json").read_text())
  manifest.pop("schedule")
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "job.sh").unlink()

  with patch("app.install._unregister_cron") as unregister:
    updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  assert updated.json()["mode"] == "updated"
  unregister.assert_called_once_with(source)
  assert not init_cron.exists()
  assert updated.json()["warnings"] == []


def test_local_apply_keeps_the_owner_schedule_until_its_contract_changes(
  client, auth,
):
  source = _source()
  _declare_schedule(source)
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["schedule"]["default"] = "30 5 * * *"
  (source / "mobius.json").write_text(json.dumps(manifest))
  assert client.put(
    "/api/owner/timezone", json={"timezone": "Asia/Tokyo"}, headers=auth,
  ).status_code == 200

  def registered(register):
    args, kwargs = register.call_args
    return args[1], args[2].name, kwargs.get("timezone"), kwargs.get("zone_cron")

  with patch("app.app_cron.register_cron") as register, \
       patch("app.cron_tz.server_timezone_name", return_value="UTC"):
    created = _apply(client, auth, source)
    assert created.status_code == 200, created.text
    app_id = created.json()["app"]["id"]
    assert registered(register) == (
      "* * * * *", "job.sh", "Asia/Tokyo", "30 5 * * *",
    )

    chosen = client.post(
      f"/api/apps/{app_id}/schedule",
      json={"cron": "45 4 * * *", "job": "job.sh", "timezone": "Asia/Tokyo"},
      headers=auth,
    )
    assert chosen.status_code == 200, chosen.text

    manifest["version"] = "0.2.0"
    (source / "mobius.json").write_text(json.dumps(manifest))
    with patch("app.install._unregister_cron") as unregister:
      kept = _apply(client, auth, source)
    assert kept.json()["mode"] == "updated", kept.text
    unregister.assert_not_called()
    assert registered(register) == (
      "* * * * *", "job.sh", "Asia/Tokyo", "45 4 * * *",
    )

    # A new scheduled job is a new contract: its default applies again.
    (source / "job.sh").rename(source / "refresh.sh")
    manifest["version"] = "0.3.0"
    manifest["schedule"]["job"] = "refresh.sh"
    (source / "mobius.json").write_text(json.dumps(manifest))
    with patch("app.install._unregister_cron") as unregister:
      reset = _apply(client, auth, source)
    assert reset.json()["mode"] == "updated", reset.text
    unregister.assert_called_once_with(source)
    assert registered(register) == (
      "* * * * *", "refresh.sh", "Asia/Tokyo", "30 5 * * *",
    )


def test_local_apply_accepts_a_scheduled_job_without_execute_permission(
  client, auth,
):
  """The runner launches the shebang's interpreter, so file mode is irrelevant."""
  source = _source()
  _declare_schedule(source)
  (source / "job.sh").chmod(0o644)

  applied = _apply(client, auth, source)

  assert applied.status_code == 200, applied.text


def test_unchanged_local_reapply_retries_failed_schedule_sync(client, auth):
  source = _source()
  _declare_schedule(source)
  with patch(
    "app.app_cron.register_cron", side_effect=RuntimeError("cron unavailable"),
  ):
    created = _apply(client, auth, source)

  assert created.status_code == 200, created.text
  assert created.json()["mode"] == "created"
  assert created.json()["warnings"] == [
    "cron: registration failed — RuntimeError('cron unavailable')"
  ]

  with patch("app.app_cron.register_cron") as register:
    repeated = _apply(client, auth, source)

  assert repeated.status_code == 200, repeated.text
  assert repeated.json()["mode"] == "unchanged"
  assert repeated.json()["warnings"] == []
  register.assert_called_once()


def test_apply_refreshes_manifest_declared_skill_on_create_and_update(
  client, auth,
):
  source = _source()
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["skills"] = ["guide.md"]
  manifest["source_files"] = ["guide.md"]
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "guide.md").write_text("# First guidance\n")

  created = _apply(client, auth, source)

  assert created.status_code == 200, created.text
  shared = Path(get_settings().data_dir) / "shared" / "skills" / "guide.md"
  assert shared.read_text() == "# First guidance\n"
  assert created.json()["warnings"] == []

  (source / "guide.md").write_text("# Revised guidance\n")
  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  assert shared.read_text() == "# Revised guidance\n"
  assert updated.json()["warnings"] == []


def test_store_managed_apply_refreshes_only_previously_approved_skills(
  client, auth, db,
):
  source = _source()
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["skills"] = ["guide.md"]
  manifest["source_files"] = ["guide.md"]
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "guide.md").write_text("# Installed guidance\n")
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.manifest_url = "https://example.test/demo/mobius.json"
  contract = dict(row.capability_contract or {})
  contract["agent"] = {**(contract.get("agent") or {}), "skills": ["guide.md"]}
  row.capability_contract = contract
  db.commit()
  (source / "guide.md").write_text("# Locally revised guidance\n")

  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  shared = Path(get_settings().data_dir) / "shared" / "skills" / "guide.md"
  assert shared.read_text() == "# Locally revised guidance\n"
  assert updated.json()["warnings"] == []


def test_store_managed_apply_keeps_every_member_of_an_approved_folder_skill(
  client, auth, db,
):
  """A local apply supplies no manifest authority, so an approved `<id>/`
  skill takes its members from the accepted source folder rather than
  retiring everything but SKILL.md."""
  source = _source()
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["skills"] = ["guide/"]
  manifest["source_files"] = ["guide/SKILL.md", "guide/publish.md"]
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "guide").mkdir()
  (source / "guide" / "SKILL.md").write_text("# Core\n")
  (source / "guide" / "publish.md").write_text("# Publish\n")
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.manifest_url = "https://example.test/demo/mobius.json"
  contract = dict(row.capability_contract or {})
  contract["agent"] = {**(contract.get("agent") or {}), "skills": ["guide/"]}
  row.capability_contract = contract
  db.commit()
  (source / "guide" / "publish.md").write_text("# Publish, revised\n")
  (source / "guide" / "notes.md").write_text("# Notes\n")

  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  assert updated.json()["warnings"] == []
  folder = Path(get_settings().data_dir) / "shared" / "skills" / "guide"
  assert (folder / "SKILL.md").read_text() == "# Core\n"
  assert (folder / "publish.md").read_text() == "# Publish, revised\n"
  assert (folder / "notes.md").read_text() == "# Notes\n"


def test_startup_retires_integrated_app_provenance(client, auth, db):
  source = _source()
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  upstream = app_git.head_sha(source, app_git.UPSTREAM_BRANCH)

  with patch.object(
    app_git, "retire_landed_equivalent_changes", return_value=2,
  ) as retire:
    retired, warnings = app_apply.retire_integrated_app_provenance(db)

  assert retired == 2
  assert warnings == []
  retire.assert_called_once_with(source, upstream)


def test_local_manifest_icon_is_materialized_with_its_accepted_revision(
  client, auth, db,
):
  """Create, replace, and remove package artwork through one apply boundary."""
  from app import icon_assets

  source = _source()
  first_raw = _icon_bytes((40, 90, 180))
  _declare_icon(source, first_raw)

  created = _apply(client, auth, source)

  assert created.status_code == 200, created.text
  app_id = created.json()["app"]["id"]
  assert created.json()["app"]["icon_url"].startswith(
    f"/api/apps/{app_id}/icon?v="
  )
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.icon_png == icon_assets.normalize_icon(first_raw)
  assert client.get(f"/api/apps/{app_id}/icon").content == row.icon_png

  override_raw = _icon_bytes((220, 70, 80))
  override = client.put(
    f"/api/apps/{app_id}/icon", content=override_raw, headers=auth,
  )
  assert override.status_code == 204, override.text

  second_raw = _icon_bytes((50, 180, 100))
  (source / "icon.png").write_bytes(second_raw)
  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.icon_png == icon_assets.normalize_icon(second_raw)
  assert row.icon_override_png == icon_assets.normalize_icon(override_raw)
  assert client.get(f"/api/apps/{app_id}/icon").content == row.icon_override_png

  cleared = client.put(f"/api/apps/{app_id}/icon", content=b"", headers=auth)
  assert cleared.status_code == 204, cleared.text
  assert client.get(f"/api/apps/{app_id}/icon").content == row.icon_png

  manifest = json.loads((source / "mobius.json").read_text())
  manifest.pop("icon")
  (source / "mobius.json").write_text(json.dumps(manifest))
  removed = _apply(client, auth, source)

  assert removed.status_code == 200, removed.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.icon_png is None
  assert row.icon_override_png is None
  assert removed.json()["app"]["icon_url"] is None
  assert client.get(f"/api/apps/{app_id}/icon").status_code == 404


def test_invalid_local_manifest_icon_keeps_previous_revision(
  client, auth, db,
):
  source = _source()
  _declare_icon(source, _icon_bytes((40, 90, 180)))
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  previous_icon = row.icon_png
  previous_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "icon.png").write_bytes(b"not an image")

  failed = _apply(client, auth, source)

  assert failed.status_code == 422
  assert failed.json()["detail"]["code"] == "icon_invalid"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.icon_png == previous_icon
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == previous_head








def test_compile_failure_keeps_previous_live_revision(client, auth, db):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  previous_bundle = row.compiled_path
  previous_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "index.jsx").write_text("export default function App( {\n")

  failed = _apply(client, auth, source)

  assert failed.status_code == 422
  assert failed.json()["detail"]["code"] == "compile_failed"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert "first" in row.jsx_source
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == previous_head


def test_invalid_manifest_keeps_previous_live_revision(client, auth, db):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  previous_bundle = row.compiled_path
  previous_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "mobius.json").write_text('{"id":"demo"}')

  failed = _apply(client, auth, source)

  assert failed.status_code == 422
  assert failed.json()["detail"]["code"] == "manifest_invalid"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == previous_head


def test_git_failure_keeps_previous_live_revision(
  client, auth, db, monkeypatch,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  previous_bundle = row.compiled_path
  previous_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "index.jsx").write_text(
    "export default function App() { return <div>draft</div> }\n"
  )

  def fail_commit(*_args, **_kwargs):
    raise RuntimeError("simulated Git failure")

  monkeypatch.setattr(app_git, "commit_worktree_tree", fail_commit)
  failed = _apply(client, auth, source)

  assert failed.status_code == 409
  assert failed.json()["detail"]["code"] == "source_repository_error"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == previous_head


def test_database_failure_after_git_commit_is_retryable(
  client, auth, db, monkeypatch,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  previous_bundle = row.compiled_path
  previous_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "index.jsx").write_text(
    "export default function App() { return <div>accepted-ahead</div> }\n"
  )
  asset = source / "listing-assets" / "screen.png"
  asset.parent.mkdir()
  asset.write_bytes(b"accepted-screen")
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {"listing/screen.png": "listing-assets/screen.png"}
  (source / "mobius.json").write_text(json.dumps(manifest))
  served = source / "static" / "listing" / "screen.png"

  original_commit = app_apply.Session.commit
  calls = 0

  def fail_once(session):
    nonlocal calls
    calls += 1
    if calls == 1:
      raise RuntimeError("simulated database failure")
    return original_commit(session)

  monkeypatch.setattr(app_apply.Session, "commit", fail_once)
  with pytest.raises(RuntimeError, match="simulated database failure"):
    _apply(client, auth, source)

  accepted_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  assert accepted_head != previous_head
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert not served.exists()

  retry = _apply(client, auth, source)

  assert retry.status_code == 200, retry.text
  assert retry.json()["mode"] == "updated"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path != previous_bundle
  assert "accepted-ahead" in row.jsx_source
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == accepted_head
  assert served.read_bytes() == b"accepted-screen"


def test_database_failure_during_create_is_retryable_without_orphan_row(
  client, auth, db, monkeypatch,
):
  source = _source()
  original_commit = app_apply.Session.commit
  calls = 0

  def fail_once(session):
    nonlocal calls
    calls += 1
    if calls == 1:
      raise RuntimeError("simulated database failure")
    return original_commit(session)

  monkeypatch.setattr(app_apply.Session, "commit", fail_once)
  with pytest.raises(RuntimeError, match="simulated database failure"):
    _apply(client, auth, source)

  accepted_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  assert db.query(models.App).filter_by(source_dir=str(source)).first() is None
  assert not list(app_apply._compiled_dir().glob("app-*-*.js"))
  assert not list(app_apply._compiled_dir().glob("*.js.staging"))

  retry = _apply(client, auth, source)

  assert retry.status_code == 200, retry.text
  assert retry.json()["mode"] == "created"
  row = db.query(models.App).populate_existing().filter_by(
    source_dir=str(source),
  ).one()
  assert row.source_commit == accepted_head
  assert Path(row.compiled_path).is_file()
  assert not list(app_apply._compiled_dir().glob("*.js.staging"))
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == accepted_head


def test_refresh_failure_after_commit_keeps_durable_publication(
  client, auth, monkeypatch,
):
  source = _source()
  asset = source / "listing-assets" / "screen.png"
  asset.parent.mkdir()
  asset.write_bytes(b"accepted-screen-v1")
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {
    "listing/screen.png": "listing-assets/screen.png",
  }
  (source / "mobius.json").write_text(json.dumps(manifest))
  created = _apply(client, auth, source)
  assert created.status_code == 200, created.text
  app_id = created.json()["app"]["id"]
  previous_bundle = Path(created.json()["app"]["compiled_path"])

  asset.write_bytes(b"accepted-screen-v2")
  (source / "index.jsx").write_text(
    "export default function App() { return <div>durable-v2</div> }\n"
  )
  original_refresh = app_apply.Session.refresh
  calls = 0

  def fail_once(session, *args, **kwargs):
    nonlocal calls
    calls += 1
    if calls == 1:
      raise RuntimeError("simulated post-commit refresh failure")
    return original_refresh(session, *args, **kwargs)

  monkeypatch.setattr(app_apply.Session, "refresh", fail_once)
  with pytest.raises(RuntimeError, match="simulated post-commit refresh failure"):
    _apply(client, auth, source)

  verify = SessionLocal()
  try:
    row = verify.get(models.App, app_id)
    assert row is not None
    published = Path(row.compiled_path)
    assert published != previous_bundle
    assert published.is_file()
    assert "durable-v2" in row.jsx_source
    assert row.source_commit == app_git.head_sha(source, app_git.LOCAL_BRANCH)
  finally:
    verify.close()
  assert not previous_bundle.exists()
  assert (source / "static" / "listing" / "screen.png").read_bytes() == (
    b"accepted-screen-v2"
  )
  assert not (source.parent / ".demo.mobius-static-bak").exists()

  retry = _apply(client, auth, source)

  assert retry.status_code == 200, retry.text
  assert Path(retry.json()["app"]["compiled_path"]).is_file()


def test_edit_without_apply_remains_a_dirty_invisible_draft(client, auth, db):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  previous_bundle = row.compiled_path
  previous_updated_at = row.updated_at
  previous_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)

  (source / "index.jsx").write_text(
    "export default function App() { return <div>not-live</div> }\n"
  )

  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert row.updated_at == previous_updated_at
  assert "first" in row.jsx_source
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == previous_head
  assert app_git._run(source, "status", "--porcelain").stdout


@pytest.mark.asyncio
async def test_bundle_recovery_uses_accepted_commit_without_touching_draft(
  client, auth, db,
):
  from app.compiler import reconcile_missing_bundles

  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  accepted_commit = row.source_commit
  old_bundle = Path(row.compiled_path)
  old_bundle.unlink()
  draft = (
    "export default function App() { return <div>unapplied draft</div> }\n"
  )
  (source / "index.jsx").write_text(draft)

  healed = await reconcile_missing_bundles(db)

  assert healed == [app_id]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.source_commit == accepted_commit
  assert "first" in row.jsx_source
  assert Path(row.compiled_path).is_file()
  assert "first" in Path(row.compiled_path).read_text(encoding="utf-8")
  assert (source / "index.jsx").read_text() == draft
  assert app_git._run(source, "status", "--porcelain").stdout


def test_source_change_during_compile_is_retryable(
  client, auth, db, monkeypatch,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  previous_bundle = row.compiled_path
  previous_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "index.jsx").write_text(
    "export default function App() { return <div>candidate</div> }\n"
  )
  original_compile = app_apply.compile_jsx

  async def compile_then_edit(*args, **kwargs):
    result = await original_compile(*args, **kwargs)
    (source / "index.jsx").write_text(
      "export default function App() { return <div>later</div> }\n"
    )
    return result

  monkeypatch.setattr(app_apply, "compile_jsx", compile_then_edit)

  failed = _apply(client, auth, source)

  assert failed.status_code == 409
  assert failed.json()["detail"]["code"] == "source_changed"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == previous_head
  assert "later" in (source / "index.jsx").read_text()


def test_reapply_unchanged_source_has_no_commit_or_timestamp_change(
  client, auth,
):
  source = _source()
  created = _apply(client, auth, source)
  before = created.json()["app"]
  head = app_git.head_sha(source, app_git.LOCAL_BRANCH)

  repeated = _apply(client, auth, source)

  assert repeated.status_code == 200, repeated.text
  assert repeated.json()["mode"] == "unchanged", (before, repeated.json())
  assert repeated.json()["app"]["updated_at"] == before["updated_at"]
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == head


def test_local_source_revision_clears_previously_verified_distribution_manifest(
  client, auth, db,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.published_manifest_url = (
    "https://raw.githubusercontent.com/example/demo/main/mobius.json"
  )
  db.commit()
  (source / "index.jsx").write_text(
    "export default function App() { return <div>second</div> }\n"
  )

  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  assert updated.json()["mode"] == "updated"
  assert updated.json()["app"]["distribution_manifest"] is None
  db.refresh(row)
  assert row.published_manifest_url is None


def test_local_manifest_identity_is_immutable(client, auth, db):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  previous_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["id"] = "different-app"
  (source / "mobius.json").write_text(json.dumps(manifest))

  failed = _apply(client, auth, source)

  assert failed.status_code == 422
  assert failed.json()["detail"]["code"] == "manifest_id_mismatch"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.slug == "demo"
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == previous_head


def test_local_apply_updates_runtime_capabilities_with_source(
  client, auth, db,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["offline_capable"] = False
  manifest["capabilities"] = {
    "media.microphone.capture": {
      "version": 1,
      "reason": "Record a short voice note.",
      "limits": {"max_duration_ms": 12_000},
    },
  }
  (source / "mobius.json").write_text(json.dumps(manifest))

  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.offline_capable is False
  microphone = row.capability_contract["runtime"]["media.microphone.capture"]
  assert microphone["reason"] == "Record a short voice note."
  assert microphone["limits"]["max_duration_ms"] == 12_000
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_local_manifest_does_not_grant_live_server_permissions(
  client, auth, db,
):
  source = _source()
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["permissions"] = {
    "cross_app_access": "read",
    "share_with_apps": "write",
    "chat_log_access": "summary_with_deleted",
    "manage_apps": True,
    "manage_skills": True,
    "github_access": True,
    "github_connect": True,
    "filesystem_access": True,
    "connections_manage": True,
    "connect_manage": True,
  }
  (source / "mobius.json").write_text(json.dumps(manifest))

  created = _apply(client, auth, source)

  assert created.status_code == 200, created.text
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.cross_app_access == "none"
  assert row.share_with_apps == "none"
  assert row.chat_log_access == "none"
  assert row.manage_apps is False
  assert row.manage_skills is False
  assert row.github_access is False
  assert row.github_connect is False
  assert row.filesystem_access is False
  assert row.connections_manage is False
  assert row.connect_manage is False


def test_local_package_flag_requires_an_installed_store_app(client, auth, db):
  source = _source()

  rejected = _apply(client, auth, source, accept_local_package=True)

  assert rejected.status_code == 422
  assert rejected.json()["detail"]["code"] == (
    "local_package_requires_store_app"
  )
  assert db.query(models.App).filter_by(source_dir=str(source)).first() is None


def test_store_local_apply_preserves_reviewed_manifest_authority(
  client, auth, db,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  reviewed_contract = {"schema": 2, "reviewed": "store"}
  row.manifest_url = "https://store.example/demo/mobius.json"
  row.name = "Reviewed name"
  row.description = "Reviewed description"
  row.offline_capable = True
  row.capability_contract = reviewed_contract
  db.commit()

  manifest = json.loads((source / "mobius.json").read_text())
  manifest["name"] = "Unreviewed local name"
  manifest["description"] = "Unreviewed local description"
  manifest["offline_capable"] = False
  manifest["capabilities"] = {
    "media.microphone.capture": {"version": 1},
  }
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "index.jsx").write_text(
    "export default function App() { return <div>local code edit</div> }\n"
  )

  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert "local code edit" in row.jsx_source
  assert row.name == "Reviewed name"
  assert row.description == "Reviewed description"
  assert row.offline_capable is True
  assert row.capability_contract == reviewed_contract
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_store_ordinary_apply_warns_when_local_package_declarations_diverge(
  client, auth, db,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  # Store-managed, with a reviewed contract that matches the current local
  # manifest (no declared tools). An ordinary apply that only edits code must
  # stay quiet — the local package hasn't diverged.
  row.manifest_url = "https://store.example/demo/mobius.json"
  row.capability_contract = app_apply.contract_from_manifest(
    json.loads((source / "mobius.json").read_text())
  )
  db.commit()

  (source / "index.jsx").write_text(
    "export default function App() { return <div>code only</div> }\n"
  )
  quiet = _apply(client, auth, source)
  assert quiet.status_code == 200, quiet.text
  assert quiet.json()["mode"] == "updated"
  assert quiet.json()["warnings"] == []

  # Declaring a new service-backed tool locally diverges from the reviewed
  # package. Ordinary apply must warn AND must not silently adopt the tool.
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["service"] = {"entry": "service.py"}
  manifest["source_files"] = ["service.py"]
  manifest["tools"] = [{
    "name": "search",
    "description": "Search things.",
    "input_schema": {"type": "object", "properties": {}},
  }]
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "service.py").write_text("# local service\n")
  (source / "index.jsx").write_text(
    "export default function App() { return <div>tool added</div> }\n"
  )

  diverged = _apply(client, auth, source)
  assert diverged.status_code == 200, diverged.text
  assert diverged.json()["warnings"] == [
    app_apply._STORE_LOCAL_PACKAGE_DIVERGED
  ]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert not (row.capability_contract.get("agent") or {}).get("tools")


@pytest.mark.parametrize("edit", [
  lambda source: _declare_model_provider(source),
  lambda source: _edit_manifest(source, permissions={"manage_apps": True}),
  lambda source: _edit_manifest(source, name="Renamed locally"),
])
def test_store_ordinary_apply_warns_on_any_dropped_manifest_edit(
  client, auth, db, edit,
):
  """Every local mobius.json edit ordinary Store apply drops is reported."""
  source = _source()
  app_id = _apply(client, auth, source).json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.manifest_url = "https://store.example/demo/mobius.json"
  db.commit()

  edit(source)
  applied = _apply(client, auth, source)

  assert applied.status_code == 200, applied.text
  assert applied.json()["warnings"] == [
    app_apply._STORE_LOCAL_PACKAGE_DIVERGED
  ]


def _edit_manifest(source: Path, **changes) -> None:
  manifest = json.loads((source / "mobius.json").read_text())
  (source / "mobius.json").write_text(json.dumps({**manifest, **changes}))


def test_store_local_package_apply_explicitly_accepts_manifest_authority(
  client, auth, db,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  store_manifest_url = "https://store.example/demo/mobius.json"
  row.manifest_url = store_manifest_url
  row.name = "Reviewed name"
  row.description = "Reviewed description"
  row.project_templates_json = None
  db.commit()

  manifest = json.loads((source / "mobius.json").read_text())
  manifest["name"] = "Local package name"
  manifest["description"] = "Local package description"
  manifest["version"] = "2.3.4"
  manifest["theme_color"] = "#223344"
  manifest["background_color"] = "#101820"
  manifest["display"] = "fullscreen"
  manifest["embeds_agent"] = True
  manifest["permissions"] = {
    "cross_app_access": "read",
    "share_with_apps": "write",
    "shared_memory": "write",
    "chat_log_access": "summary",
    "manage_apps": True,
    "manage_skills": True,
    "github_access": True,
    "github_connect": True,
    "filesystem_access": True,
    "connections_manage": True,
    "connect_manage": True,
  }
  manifest["skills"] = ["guide.md"]
  manifest["source_files"] = ["guide.md"]
  manifest["schedule"] = {
    "default": "*/10 * * * *",
    "user_configurable": False,
    "job": "job.sh",
  }
  manifest["project_templates"] = [{
    "id": "document", "name": "Document", "files": {},
  }]
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "guide.md").write_text("# Local package guidance\n")
  job = source / "job.sh"
  job.write_text("#!/bin/sh\nexit 0\n")
  job.chmod(0o755)
  (source / "index.jsx").write_text(
    "export default function App() { return <div>local package</div> }\n"
  )

  with patch("app.app_cron.register_cron"):
    updated = _apply(
      client, auth, source, accept_local_package=True,
    )

  assert updated.status_code == 200, updated.text
  assert updated.json()["app"]["name"] == "Local package name"
  assert updated.json()["warnings"] == [
    "Local package declarations are active, including permissions, schedules, "
    "and skills; a future reviewed Store update may replace them."
  ]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.manifest_url == store_manifest_url
  assert row.description == "Local package description"
  assert row.version == "2.3.4"
  assert row.theme_color == "#223344"
  assert row.background_color == "#101820"
  assert row.display == "fullscreen"
  assert row.embeds_agent is True
  assert row.cross_app_access == "read"
  assert row.share_with_apps == "write"
  assert row.chat_log_access == "summary"
  assert row.manage_apps is True
  assert row.manage_skills is True
  assert row.github_access is True
  assert row.github_connect is True
  assert row.filesystem_access is True
  assert row.connections_manage is True
  assert row.connect_manage is True
  assert row.capability_contract["data"]["shared_memory"] == "write"
  assert row.capability_contract["agent"]["skills"] == ["guide.md"]
  assert row.capability_contract["background"]["job"] == "job.sh"
  assert row.capability_contract["background"]["cron"] == "*/10 * * * *"
  assert row.project_templates_json == [{
    "id": "document", "name": "Document", "files": {},
  }]
  assert "local package" in row.jsx_source

  with patch("app.app_cron.register_cron"):
    repeated = _apply(client, auth, source, accept_local_package=True)

  assert repeated.status_code == 200, repeated.text
  assert repeated.json()["mode"] == "unchanged"
  assert repeated.json()["warnings"] == [
    "Local package declarations are active, including permissions, schedules, "
    "and skills; a future reviewed Store update may replace them."
  ]

  # Ordinary Store apply must recover the approved skill list from the durable
  # contract rather than treating the absent manifest authority as no skills.
  (source / "guide.md").write_text("# Locally revised guidance\n")
  ordinary = _apply(client, auth, source)

  assert ordinary.status_code == 200, ordinary.text
  assert ordinary.json()["mode"] == "updated"
  assert ordinary.json()["warnings"] == []
  shared = Path(get_settings().data_dir) / "shared" / "skills" / "guide.md"
  assert shared.read_text() == "# Locally revised guidance\n"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.capability_contract["agent"]["skills"] == ["guide.md"]


def test_store_local_package_revokes_omitted_privileged_permissions(
  client, auth, db,
):
  source = _source()
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.manifest_url = "https://store.example/demo/mobius.json"
  row.manage_apps = True
  row.manage_skills = True
  row.github_access = True
  row.github_connect = True
  row.filesystem_access = True
  row.connections_manage = True
  row.connect_manage = True
  row.capability_contract = {
    "schema": 2,
    "data": {"shared_memory": "write"},
  }
  db.commit()

  updated = _apply(client, auth, source, accept_local_package=True)

  assert updated.status_code == 200, updated.text
  assert updated.json()["mode"] == "updated"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.manage_apps is False
  assert row.manage_skills is False
  assert row.github_access is False
  assert row.github_connect is False
  assert row.filesystem_access is False
  assert row.connections_manage is False
  assert row.connect_manage is False
  assert row.capability_contract["data"]["shared_memory"] == "none"


def test_store_local_package_uses_canonical_manifest_identity_when_slug_differs(
  client, auth, db,
):
  source = _source("app-store")
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.manifest_url = (
    "https://raw.githubusercontent.com/mobius-os/app-store/main"
    "#manifest-id=store"
  )
  db.commit()

  manifest = json.loads((source / "mobius.json").read_text())
  manifest["id"] = "store"
  manifest["permissions"] = {"manage_apps": True, "github_access": True}
  (source / "mobius.json").write_text(json.dumps(manifest))

  updated = _apply(client, auth, source, accept_local_package=True)

  assert updated.status_code == 200, updated.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.slug == "app-store"
  assert row.github_access is True
  assert row.manifest_url.endswith("#manifest-id=store")


@pytest.mark.parametrize("draft_manifest", [False, True])
def test_store_local_apply_accepts_installer_managed_tree_without_manifest(
  client, auth, db, draft_manifest,
):
  """Installed app repos intentionally exclude the reviewed mobius.json."""
  source = Path(get_settings().data_dir) / "apps" / "store-demo"
  source.mkdir(parents=True)
  (source / "index.jsx").write_text(
    "export default function App() { return <div>installed</div> }\n"
  )
  app_git.ensure_repo(source)
  app_git.commit_local(source, "install source")
  installed_head = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  row = models.App(
    name="Reviewed name",
    description="Reviewed description",
    jsx_source=(source / "index.jsx").read_text(),
    compiled_path="",
    source_dir=str(source),
    source_commit=installed_head,
    slug="store-demo",
    manifest_url="https://store.example/store-demo/mobius.json",
    offline_capable=True,
    capability_contract={"schema": 2, "reviewed": "store"},
  )
  db.add(row)
  db.commit()
  from app.applied_app_runtime import bootstrap_legacy_runtimes, runtime_root
  assert bootstrap_legacy_runtimes(db) == (1, [])
  app_id = row.id
  (source / "index.jsx").write_text(
    "export default function App() { return <div>local edit</div> }\n"
  )

  if draft_manifest:
    (source / "mobius.json").write_text('{"name":"unaccepted package manifest"}')

  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert "local edit" in row.jsx_source
  assert row.source_commit != installed_head
  assert row.name == "Reviewed name"
  assert row.capability_contract == {"schema": 2, "reviewed": "store"}
  assert ("mobius.json" in app_git.read_ref_tree(
    source, app_git.LOCAL_BRANCH,
  )) is draft_manifest
  assert not (runtime_root(row) / "mobius.json").exists()
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_local_apply_without_manifest_remains_invalid(client, auth):
  source = Path(get_settings().data_dir) / "apps" / "local-no-manifest"
  source.mkdir(parents=True)
  (source / "index.jsx").write_text(
    "export default function App() { return <div>local</div> }\n"
  )

  failed = _apply(client, auth, source)

  assert failed.status_code == 422
  assert failed.json()["detail"]["code"] == "manifest_missing"


def test_legacy_inline_source_mutation_routes_are_retired(client, auth):
  source = _source()

  old_create = client.post(
    "/api/apps/",
    headers=auth,
    json={
      "name": "Legacy",
      "jsx_source": "export default function App(){return <div />}",
    },
  )
  assert old_create.status_code == 404

  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  old_patch = client.patch(
    f"/api/apps/{app_id}",
    headers=auth,
    json={
      "jsx_source": "export default function App(){return <div>bypass</div>}",
    },
  )
  assert old_patch.status_code == 422


def test_editing_app_files_does_not_change_static_or_job_runtime_until_apply(client, auth, db):
  from app.applied_app_runtime import runtime_root

  source = _source()
  (source / "assets").mkdir()
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {"page.txt": "assets/page.txt"}
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "assets" / "page.txt").write_text("accepted-one")
  (source / "job.sh").write_text("#!/bin/sh\ncat sibling.txt\n")
  (source / "sibling.txt").write_text("accepted sibling")
  response = _apply(client, auth, source)
  assert response.status_code == 200, response.text
  app_id = response.json()["app"]["id"]
  row = db.get(models.App, app_id)
  old_runtime = runtime_root(row)

  (source / "assets" / "page.txt").write_text("draft-two")
  (source / "job.sh").write_text("#!/bin/sh\nexit 99\n")
  (source / "sibling.txt").write_text("draft sibling")
  assert client.get(f"/app-assets/by-id/{app_id}/page.txt").text == "accepted-one"
  assert (old_runtime / "job.sh").read_text() == "#!/bin/sh\ncat sibling.txt\n"
  assert (old_runtime / "sibling.txt").read_text() == "accepted sibling"
  context = client.get(f"/api/apps/{app_id}/job-context", headers=auth).json()
  assert context["runtime_dir"] == str(old_runtime)

  applied = _apply(client, auth, source)
  assert applied.status_code == 200, applied.text
  db.refresh(row)
  assert runtime_root(row) != old_runtime
  assert client.get(f"/app-assets/by-id/{app_id}/page.txt").text == "draft-two"
  # A path pinned by an already-running job never changes under its feet.
  assert (old_runtime / "sibling.txt").read_text() == "accepted sibling"


def test_database_apply_failure_does_not_publish_new_runtime(client, auth, db, monkeypatch):
  from app.applied_app_runtime import runtime_root

  source = _source()
  (source / "assets").mkdir()
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["static_assets"] = {"page.txt": "assets/page.txt"}
  (source / "mobius.json").write_text(json.dumps(manifest))
  (source / "assets" / "page.txt").write_text("accepted")
  created = _apply(client, auth, source)
  app_id = created.json()["app"]["id"]
  row = db.get(models.App, app_id)
  old_runtime = runtime_root(row)
  (source / "assets" / "page.txt").write_text("not published")
  original = app_apply.Session.commit

  def fail(session):
    raise RuntimeError("runtime publication database failure")

  monkeypatch.setattr(app_apply.Session, "commit", fail)
  with pytest.raises(RuntimeError, match="runtime publication database failure"):
    _apply(client, auth, source)
  monkeypatch.setattr(app_apply.Session, "commit", original)
  db.refresh(row)
  assert runtime_root(row) == old_runtime
  assert client.get(f"/app-assets/by-id/{app_id}/page.txt").text == "accepted"


def test_runtime_bootstrap_uses_recorded_commit_not_dirty_worktree(client, auth, db):
  import shutil
  from app.applied_app_runtime import runtime_root

  source = _source()
  (source / "job.sh").write_text("accepted job")
  created = _apply(client, auth, source)
  row = db.get(models.App, created.json()["app"]["id"])
  previous = runtime_root(row)
  shutil.rmtree(previous)
  (source / "job.sh").write_text("dirty job")
  rebuilt = runtime_root(row)
  assert (rebuilt / "job.sh").read_text() == "accepted job"


@pytest.mark.asyncio
async def test_accepted_update_syncs_every_member_of_a_folder_skill(tmp_path, monkeypatch):
  # A resolved Store update can add folder-skill files the remote manifest's
  # file list does not name; the accepted source owns which files exist.
  from types import SimpleNamespace

  from app import app_apply, install

  folder = tmp_path / "contributing"
  folder.mkdir()
  for name in ("SKILL.md", "cycle.md", "adapter-mobius.md"):
    (folder / name).write_text(f"# {name}\n")
  captured = {}

  async def capture(_db, _app, manifest, _warnings):
    captured.update(manifest)

  monkeypatch.setattr(install, "_sync_app_skills", capture)
  app = SimpleNamespace(source_dir=str(tmp_path), capability_contract={})
  remote = {
    "version": "1.2.3", "skills": ["contributing/"],
    "source_files": ["contributing/SKILL.md", "contributing/review-merge.md"],
  }

  assert await app_apply._sync_accepted_app_skills(None, app, remote) == ()

  assert captured["version"] == "1.2.3"
  assert captured["source_files"] == [
    "contributing/SKILL.md", "contributing/adapter-mobius.md", "contributing/cycle.md",
  ]


def test_owner_built_local_app_model_provider_joins_registry_until_removed(
  client, auth, db,
):
  from app import providers

  source = _source()
  declared = _declare_model_provider(source)
  data_dir = get_settings().data_dir

  created = _apply(client, auth, source)

  assert created.status_code == 200, created.text
  app_id = created.json()["app"]["id"]
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.capability_contract["model_provider"] == declared
  try:
    providers.sync_app_model_providers(data_dir, force=True)
    assert providers.provider_of_model("example/model-a") == f"app-{app_id}"

    # An owner grant change re-projects the local contract from app state; it
    # must keep the declaration the last source apply accepted.
    patched = client.patch(
      f"/api/apps/{app_id}", json={"cross_app_access": "read"}, headers=auth,
    )
    assert patched.status_code == 200, patched.text
    row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
    assert row.capability_contract["model_provider"] == declared

    manifest = json.loads((source / "mobius.json").read_text())
    manifest.pop("model_provider")
    (source / "mobius.json").write_text(json.dumps(manifest))
    removed = _apply(client, auth, source)

    assert removed.status_code == 200, removed.text
    row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
    assert "model_provider" not in row.capability_contract
    providers.sync_app_model_providers(data_dir, force=True)
    assert providers.provider_of_model("example/model-a") is None
  finally:
    providers.invalidate_model_cache()


def test_local_apply_refuses_the_protected_broker_model_transport(
  client, auth, db,
):
  source = _source("identity")
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["permissions"] = {"identity_manage": True}
  (source / "mobius.json").write_text(json.dumps(manifest))
  _declare_model_provider(
    source, transport="identity_broker", base_url="http://127.0.0.1:8765/v1",
  )
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["model_provider"].pop("secret_name")
  (source / "mobius.json").write_text(json.dumps(manifest))

  refused = _apply(client, auth, source)

  assert refused.status_code == 422, refused.text
  assert refused.json()["detail"]["code"] == "local_model_broker"
  assert db.query(models.App).count() == 0
