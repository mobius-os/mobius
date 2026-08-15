"""Explicit mini-app source application."""

import io
import json
from pathlib import Path
from unittest.mock import call, patch

import pytest

from app import app_apply, app_git, icon_assets, models
from app.config import get_settings


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


def _apply(client, auth, source: Path, chat_id: str | None = None):
  body = {"source_dir": str(source)}
  if chat_id is not None:
    body["chat_id"] = chat_id
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
  assert mock_get_broadcast.return_value.publish.call_args_list == [
    call({"type": "app_updated", "appId": str(app_id)}),
    call({
      "type": "app_preview_ready",
      "appId": str(app_id),
      "chatId": "editing-chat",
    }),
  ]


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

  retry = _apply(client, auth, source)

  assert retry.status_code == 200, retry.text
  assert retry.json()["mode"] == "updated"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path != previous_bundle
  assert "accepted-ahead" in row.jsx_source
  assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == accepted_head


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


def test_store_local_apply_accepts_installer_managed_tree_without_manifest(
  client, auth, db,
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
  app_id = row.id
  (source / "index.jsx").write_text(
    "export default function App() { return <div>local edit</div> }\n"
  )

  updated = _apply(client, auth, source)

  assert updated.status_code == 200, updated.text
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert "local edit" in row.jsx_source
  assert row.source_commit != installed_head
  assert row.name == "Reviewed name"
  assert row.capability_contract == {"schema": 2, "reviewed": "store"}
  assert "mobius.json" not in app_git.read_ref_tree(
    source, app_git.LOCAL_BRANCH,
  )
  assert app_git._run(source, "status", "--porcelain").stdout == ""


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
