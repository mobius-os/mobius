"""Store listing readiness and owner edits of an app's listing."""

import asyncio
import base64
import io
import json
from pathlib import Path
import threading
from unittest.mock import patch

from PIL import Image

from app import app_apply, app_git, app_python_env, models, schemas
from app.community_publish import public_store_listing, store_listing_review
from app.config import get_settings
from app.routes import apps as app_routes

import pytest

from app.community_publish import CommunityPublicationError


def _png(color=(40, 120, 200), size=(64, 40)) -> bytes:
  output = io.BytesIO()
  Image.new("RGB", size, color).save(output, format="PNG")
  return output.getvalue()


def _b64(raw: bytes) -> str:
  return base64.b64encode(raw).decode("ascii")


def _file(path: str, content: bytes = b"x") -> dict:
  return {"path": path, "mode": "100644", "content_base64": _b64(content)}


# Saving a listing preserves existing manifest fields.
_MANIFEST = """{
  "id": "demo",
  "name": "Démo",
  "version": "0.1.0",
  "description": "A focused demo.",
  "entry": "index.jsx",
  "icon": "icon.png",
  "offline_capable": true,
  "permissions": {},
  "source_files": []
}
"""


def _source(slug: str = "demo") -> Path:
  root = Path(get_settings().data_dir) / "apps" / slug
  root.mkdir(parents=True)
  (root / "mobius.json").write_text(_MANIFEST.replace('"demo"', f'"{slug}"'), encoding="utf-8")
  (root / "index.jsx").write_text("export default function App() { return <div>hi</div> }\n")
  (root / "icon.png").write_bytes(_png((200, 40, 40)))
  return root


def _create(client, auth, source: Path) -> int:
  response = client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)
  assert response.status_code == 200, response.text
  return response.json()["app"]["id"]


def _preview(client, auth, app_id: int) -> dict:
  response = client.get(
    f"/api/community/publications/github/preview?app_id={app_id}", headers=auth,
  )
  assert response.status_code == 200, response.text
  return response.json()


def _save(client, auth, app_id: int, **body):
  return client.put(f"/api/apps/{app_id}/store-listing", json=body, headers=auth)


def _complete_listing(**overrides) -> dict:
  return {
    "tagline": "Small and exact.",
    "description": "A calm demo for the things that matter.",
    "screenshots": [{"data_base64": _b64(_png()), "alt": "The demo's main screen."}],
    **overrides,
  }


def test_review_reports_every_missing_listing_item_at_once():
  manifest = json.loads(_MANIFEST)
  manifest.pop("icon")
  review = store_listing_review([_file("mobius.json", json.dumps(manifest).encode())])

  missing = {item["id"] for item in review["checklist"] if not item["done"]}
  assert missing == {"icon", "tagline", "description", "screenshots"}
  assert review["ready"] is False and review["listing"] is None
  hero = next(item for item in review["checklist"] if item["id"] == "hero")
  assert hero["optional"] is True and hero["done"] is True
  # The publish gate refuses with the first unmet item of the same review.
  with pytest.raises(CommunityPublicationError) as refused:
    public_store_listing([_file("mobius.json", json.dumps(manifest).encode())])
  assert refused.value.code == "listing_incomplete"
  assert refused.value.detail == "Add an app icon."


def test_review_of_an_app_without_details_still_lists_what_it_needs():
  review = store_listing_review([_file("index.jsx")])

  details = next(item for item in review["checklist"] if item["id"] == "details")
  assert details["done"] is False and details["code"] == "invalid_manifest"
  assert review["draft"]["screenshots"] == []


def test_listing_art_outside_static_store_is_flagged_but_still_editable():
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {
    "tagline": "t", "description": "d", "hero": "listing/hero.png",
    "screenshots": [{"src": "static/store/a.png", "alt": "A"}],
  }
  review = store_listing_review([
    _file("mobius.json", json.dumps(manifest).encode()),
    _file("icon.png"), _file("listing/hero.png"), _file("static/store/a.png"),
  ])

  hero = next(item for item in review["checklist"] if item["id"] == "hero")
  assert hero["done"] is False and "static/store/" in hero["message"]
  assert review["draft"]["hero"] == "listing/hero.png"


def test_saving_a_listing_makes_the_app_publish_ready(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  before = _preview(client, auth, app_id)
  assert before["ready"] is False
  assert {i["id"] for i in before["checklist"] if not i["done"]} == {
    "tagline", "description", "screenshots",
  }

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text
  after = _preview(client, auth, app_id)
  assert after["ready"] is True
  shot = after["listing"]["screenshots"][0]
  assert shot["src"].startswith("static/store/") and shot["alt"] == "The demo's main screen."
  manifest_text = (source / "mobius.json").read_text(encoding="utf-8")
  assert manifest_text.startswith(_MANIFEST.rstrip().rstrip("}").rstrip())
  assert app_git._run(source, "status", "--porcelain").stdout == ""
  asset = client.get(after["asset_root"] + shot["src"], headers=auth)
  assert asset.status_code == 200 and asset.content == _png()


def test_saving_again_keeps_art_and_removes_art_no_longer_listed(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  assert _save(client, auth, app_id, **_complete_listing(
    hero={"data_base64": _b64(_png((1, 2, 3)))},
  )).status_code == 200
  first = _preview(client, auth, app_id)["listing"]
  hero_path = first["hero"]["path"]

  kept = _save(client, auth, app_id, **_complete_listing(
    screenshots=[{"path": first["screenshots"][0]["src"], "alt": "Renamed."}],
  ))

  assert kept.status_code == 200, kept.text
  listing = _preview(client, auth, app_id)["listing"]
  assert listing["hero"] is None
  assert listing["screenshots"][0]["alt"] == "Renamed."
  assert not (source / hero_path).exists()


def test_art_from_an_older_listing_layout_moves_into_static_store(client, auth):
  source = _source()
  (source / "listing").mkdir()
  (source / "listing" / "hero.png").write_bytes(_png((9, 9, 9)))
  app_id = _create(client, auth, source)

  saved = _save(client, auth, app_id, **_complete_listing(hero={"path": "listing/hero.png"}))

  assert saved.status_code == 200, saved.text
  preview = _preview(client, auth, app_id)
  assert preview["ready"] is True
  assert preview["listing"]["hero"]["path"].startswith("static/store/")


def test_saving_refuses_to_sweep_in_unapplied_edits(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "index.jsx").write_text("export default function App() { return null }\n")

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "source_has_draft"
  assert (source / "mobius.json").read_text(encoding="utf-8") == _MANIFEST
  assert not (source / "static").exists()


def test_saving_rejects_replaced_source_root(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  moved = source.with_name("moved-source")
  source.rename(moved)
  source.symlink_to(moved, target_is_directory=True)

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "source_identity_changed"
  assert "store" not in json.loads((moved / "mobius.json").read_text())


def test_a_failed_accept_restores_every_file_the_save_touched(client, auth):
  source = _source()
  app_id = _create(client, auth, source)

  with patch.object(
    app_apply, "apply_source_revision",
    side_effect=app_apply.AppApplyError("boom", "Apply failed."),
  ):
    failed = _save(client, auth, app_id, **_complete_listing())

  assert failed.status_code == 422
  assert (source / "mobius.json").read_text(encoding="utf-8") == _MANIFEST
  assert not [path for path in source.rglob("static/**/*") if path.is_file()]
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_failure_after_git_commit_keeps_clean_retry_revision(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  old_head = app_git.head_sha(source, "HEAD")

  with patch.object(
    app_python_env, "prepare_env",
    side_effect=app_python_env.PythonEnvBuildError("failed after Git commit"),
  ):
    failed = _save(client, auth, app_id, **_complete_listing())

  assert failed.status_code == 422
  assert app_git.head_sha(source, "HEAD") != old_head
  assert app_git._run(source, "status", "--porcelain").stdout == ""
  assert "store" in json.loads((source / "mobius.json").read_text())
  retried = _save(client, auth, app_id, **_complete_listing())
  assert retried.status_code == 200, retried.text
  assert _preview(client, auth, app_id)["ready"] is True


def test_failure_after_durable_accept_never_undoes_listing(client, auth):
  source = _source()
  app_id = _create(client, auth, source)

  with patch.object(app_apply, "_sync_accepted_app_side_effects", side_effect=RuntimeError("late")):
    with pytest.raises(RuntimeError, match="late"):
      _save(client, auth, app_id, **_complete_listing())

  assert app_git._run(source, "status", "--porcelain").stdout == ""
  assert _preview(client, auth, app_id)["ready"] is True


@pytest.mark.asyncio
async def test_cancel_during_git_commit_waits_for_coherent_accept(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  entered = threading.Event()
  release = threading.Event()
  original_commit = app_git.commit_worktree_tree

  def held_commit(*args):
    entered.set()
    assert release.wait(10), "test did not release commit"
    return original_commit(*args)

  with patch.object(app_git, "commit_worktree_tree", side_effect=held_commit):
    task = asyncio.create_task(app_routes.save_store_listing(
      app_id, schemas.StoreListingIn(**_complete_listing()), db, None,
    ))
    try:
      assert await asyncio.to_thread(entered.wait, 10)
      task.cancel()
      await asyncio.sleep(0)
      assert not task.done()  # lock and transaction remain owned while Git runs
    finally:
      release.set()
    with pytest.raises(asyncio.CancelledError):
      await task

  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.source_commit == app_git.head_sha(source, "HEAD")
  assert app_git._run(source, "status", "--porcelain").stdout == ""
  assert "store" in json.loads((source / "mobius.json").read_text())


def test_invalid_images_are_refused_before_anything_is_written(client, auth):
  source = _source()
  app_id = _create(client, auth, source)

  refused = _save(client, auth, app_id, **_complete_listing(
    screenshots=[{"data_base64": _b64(b"not an image"), "alt": "x"}],
  ))

  assert refused.status_code == 422
  assert refused.json()["detail"]["code"] == "listing_image_invalid"
  assert (source / "mobius.json").read_text(encoding="utf-8") == _MANIFEST


def _make_older_app(client, auth, db, slug: str) -> tuple[Path, int]:
  """An app from before manifests: tracked index.jsx only, icon on its record."""
  source = _source(slug)
  app_id = _create(client, auth, source)
  app_git._run(source, "rm", "-q", "mobius.json", "icon.png")
  app_git._run(source, "commit", "-q", "-m", "older app layout")
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.source_commit = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  row.icon_png = None
  row.icon_override_png = _png((10, 200, 10), (32, 32))
  row.manage_apps = True
  db.commit()
  return source, app_id


def test_an_older_app_requires_agent_migration_without_resetting_runtime(client, auth, db):
  source, app_id = _make_older_app(client, auth, db, "older")
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.embeds_agent = True
  db.commit()
  preview = _preview(client, auth, app_id)
  details = next(i for i in preview["checklist"] if i["id"] == "details")
  assert details["done"] is False and details["automatic"] is False
  icon = next(i for i in preview["checklist"] if i["id"] == "icon")
  assert icon["done"] is False and icon["automatic"] is True

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 409
  assert saved.json()["detail"]["code"] == "details_need_agent"
  assert not (source / "mobius.json").exists()
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.manage_apps is True
  assert row.embeds_agent is True


def test_an_older_app_with_a_scheduled_job_is_left_to_an_agent(client, auth, db):
  source, app_id = _make_older_app(client, auth, db, "older-cron")
  (source / "job.sh").write_text("#!/bin/sh\nexit 0\n")
  app_git._run(source, "add", "job.sh")
  app_git._run(source, "commit", "-q", "-m", "job")

  details = next(
    i for i in _preview(client, auth, app_id)["checklist"] if i["id"] == "details"
  )
  refused = _save(client, auth, app_id, **_complete_listing())

  assert details["automatic"] is False and "agent" in details["message"]
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "details_need_agent"
  assert not (source / "mobius.json").exists()


def test_saving_keeps_listing_fields_the_editor_does_not_manage(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["store"]["featured"] = True
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)
  shot = _preview(client, auth, app_id)["listing"]["screenshots"][0]["src"]

  saved = _save(client, auth, app_id, **_complete_listing(
    tagline="New line.", screenshots=[{"path": shot, "alt": "A"}],
  ))

  assert saved.status_code == 200, saved.text
  store = json.loads((source / "mobius.json").read_text())["store"]
  assert store["featured"] is True and store["tagline"] == "New line."


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_listing_paths_cannot_cross_symlinked_parents(tmp_path, operation):
  from app import store_listing_source as listing

  root = tmp_path / "source"
  root.mkdir()
  outside = tmp_path / "outside"
  outside.mkdir()
  target = outside / "image.png"
  target.write_bytes(_png())
  (root / "static").symlink_to(outside, target_is_directory=True)
  changes = listing._SourceChanges(root)

  with pytest.raises(listing.ListingEditError):
    if operation == "read":
      listing._source_file(root, "static/image.png")
    elif operation == "write":
      changes.write("static/image.png", b"changed")
    else:
      changes.delete("static/image.png")
  assert target.read_bytes() == _png()


@pytest.mark.parametrize("path", ["../outside.png", "/outside.png", ".git/config"])
def test_listing_writes_stay_inside_editable_source(tmp_path, path):
  from app import store_listing_source as listing

  with pytest.raises(listing.ListingEditError):
    listing._SourceChanges(tmp_path).write(path, b"changed")
