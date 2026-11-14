"""Store listing readiness and owner edits of an app's listing."""

import asyncio
import base64
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import (
  app_apply, app_git, app_python_env, community_publish, fs_locks, models, schemas,
  store_listing_source,
)
from app.community_publish import public_store_listing, store_listing_review
from app.config import get_settings
from app.routes import apps as app_routes

import pytest

from app.community_publish import CommunityPublicationError
from tests.cancel_helpers import BlockedWorker, cancel_while_worker_blocked


class _HeldCall(BlockedWorker):
  """Block a real call until released, then run it with its own arguments."""

  def __init__(self, fn):
    super().__init__()
    self.fn = fn

  def __call__(self, *args, **kwargs):
    self.then = lambda: self.fn(*args, **kwargs)
    return super().__call__(*args, **kwargs)


def _content_name(content: bytes) -> str:
  """The name the editor gives art holding ``content``."""
  import hashlib

  return hashlib.sha256(content).hexdigest()[:16]


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
    "license": "MIT",
    **overrides,
  }


def test_review_reports_every_missing_listing_item_at_once():
  manifest = json.loads(_MANIFEST)
  manifest.pop("icon")
  review = store_listing_review([_file("mobius.json", json.dumps(manifest).encode())])

  missing = {item["id"] for item in review["checklist"] if not item["done"]}
  assert missing == {"icon", "tagline", "description", "screenshots", "license"}
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


def test_review_keeps_the_draft_of_a_manifest_missing_publication_fields():
  manifest = json.loads(_MANIFEST)
  manifest.pop("entry")
  manifest["store"] = {
    "tagline": "Kept.", "description": "Still here.",
    "screenshots": [{"src": "static/store/a.png", "alt": "A"}],
  }
  review = store_listing_review([
    _file("mobius.json", json.dumps(manifest).encode()),
    _file("icon.png"), _file("static/store/a.png"),
  ])

  details = next(item for item in review["checklist"] if item["id"] == "details")
  assert details["done"] is False and details["code"] == "invalid_manifest"
  assert review["ready"] is False
  assert review["draft"]["tagline"] == "Kept."
  assert review["draft"]["description"] == "Still here."
  assert review["draft"]["icon"] == "icon.png"
  assert review["draft"]["screenshots"][0]["src"] == "static/store/a.png"


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
    "tagline", "description", "screenshots", "license",
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


@pytest.mark.parametrize("edit", ["code", "manifest"])
def test_saving_refuses_to_accept_committed_but_unapplied_edits(client, auth, db, edit):
  source = _source()
  app_id = _create(client, auth, source)
  accepted = app_git.head_sha(source, "HEAD")
  if edit == "code":
    (source / "index.jsx").write_text("export default function App() { return 'unreviewed' }\n")
  else:
    manifest = json.loads(_MANIFEST)
    manifest["permissions"] = {"network": True}
    (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  app_git._run(source, "commit", "-qam", "committed, never applied")
  head = app_git.head_sha(source, "HEAD")

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409, refused.text
  assert refused.json()["detail"]["code"] == "source_has_draft"
  assert app_git.head_sha(source, "HEAD") == head
  assert app_git._run(source, "status", "--porcelain").stdout == ""
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.source_commit == accepted


def test_committed_listing_only_changes_may_ride_along(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  # What an interrupted save leaves: its own hash-named art and the PNG icon
  # the manifest now declares.
  manifest["store"] = {"tagline": "Committed by hand."}
  manifest["icon"] = "icon.png"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "static" / "store").mkdir(parents=True)
  art = _png((3, 3, 3))
  (source / "static" / "store" / f"{_content_name(art)}.png").write_bytes(art)
  (source / "icon.png").write_bytes(_png((4, 4, 4)))
  app_git._run(source, "add", "-A")
  app_git._run(source, "commit", "-qm", "listing files only")

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text


def test_a_store_app_listing_save_explains_what_it_changes(client, auth, monkeypatch):
  source = _source()
  app_id = _create(client, auth, source)
  real_apply = app_routes._apply_source_or_http

  async def store_apply(*args, **kwargs):
    result = await real_apply(*args, **kwargs)
    return app_apply.ApplyResult(
      app=result.app, mode=result.mode,
      warnings=(app_apply.STORE_LOCAL_PACKAGE_DIVERGED,),
    )

  monkeypatch.setattr(app_routes, "_apply_source_or_http", store_apply)

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text
  assert saved.json()["warnings"] == [app_routes._STORE_APP_LISTING_SAVED]


def test_an_oversized_image_gets_a_clear_message_and_a_full_listing_fits():
  from app.main import _MAX_REQUEST_BODY_BYTES
  from app.manifest_contract import STORE_LISTING_IMAGE_MAX_BYTES
  from pydantic import ValidationError

  largest = "A" * (-(-STORE_LISTING_IMAGE_MAX_BYTES // 3) * 4)
  body = schemas.StoreListingIn(
    tagline="t" * 1000, description="d" * 20_000,
    icon={"data_base64": largest}, hero={"data_base64": largest},
    screenshots=[
      {"data_base64": largest, "alt": "a" * 2000, "label": "l" * 1000}
    ] * 5,
  )
  assert len(body.model_dump_json().encode()) < _MAX_REQUEST_BODY_BYTES
  with pytest.raises(ValidationError, match="at most 6 MB"):
    schemas.StoreListingImageIn(data_base64=largest + "AAAA")


def _commit_accepted(db, source: Path, app_id: int, message: str) -> None:
  """Commit the worktree and record it as accepted, bypassing apply's checks."""
  app_git._run(source, "add", "-A")
  app_git._run(source, "commit", "-q", "-m", message)
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.source_commit = app_git.head_sha(source, "HEAD")
  db.commit()


def _item(preview: dict, item_id: str) -> dict:
  return next(item for item in preview["checklist"] if item["id"] == item_id)


def test_one_image_rule_for_readiness_preview_and_save(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  gif = io.BytesIO()
  Image.new("RGB", (16, 16), (9, 9, 9)).save(gif, format="GIF")
  (source / "static" / "store").mkdir(parents=True)
  (source / "static" / "store" / "a.png").write_bytes(gif.getvalue())
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {
    "tagline": "t", "description": "d",
    "screenshots": [{"src": "static/store/a.png", "alt": "A"}],
  }
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  _commit_accepted(db, source, app_id, "gif listing")

  preview = _preview(client, auth, app_id)

  assert preview["ready"] is False
  assert _item(preview, "screenshots")["code"] == "listing_image_invalid"
  assert client.get(
    preview["asset_root"] + "static/store/a.png", headers=auth,
  ).status_code == 404
  refused = _save(client, auth, app_id, **_complete_listing(
    screenshots=[{"path": "static/store/a.png", "alt": "A"}],
  ))
  assert refused.status_code == 422
  assert refused.json()["detail"]["code"] == "listing_image_invalid"
  assert "Use a PNG, JPEG, or WebP" in refused.json()["detail"]["message"]


def test_images_over_the_pixel_cap_are_refused_plainly():
  from app import icon_assets
  from app.community_publish import CommunityPublicationError, check_listing_image

  raw = io.BytesIO()
  Image.new("L", (8192, 8192)).save(raw, format="PNG")
  with pytest.raises(CommunityPublicationError) as refused:
    check_listing_image(raw.getvalue(), "banner image")

  assert refused.value.code == "listing_image_too_large"
  assert f"{icon_assets.MAX_IMAGE_PIXELS // 1_000_000} million pixels" in refused.value.detail


def _png_chunk(kind: bytes, data: bytes) -> bytes:
  import struct
  import zlib

  return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _with_png_chunk(png: bytes, kind: bytes, data: bytes) -> bytes:
  """``png`` with one more chunk just before its IEND."""
  end = png.rindex(b"IEND") - 4
  return png[:end] + _png_chunk(kind, data) + png[end:]


def test_uploaded_photos_lose_their_location_and_camera_data(client, auth):
  import hashlib

  source = _source()
  app_id = _create(client, auth, source)
  exif = Image.Exif()
  exif[0x010F] = "Camera maker"
  exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
  photo = io.BytesIO()
  Image.new("RGB", (64, 40), (200, 120, 40)).save(
    photo, format="JPEG", exif=exif, comment=b"JPEG comment",
  )
  assert Image.open(io.BytesIO(photo.getvalue())).getexif().get_ifd(0x8825)
  plain = _png((9, 90, 9))
  # An unknown provenance chunk, and bytes hidden after the image ends.
  png = _with_png_chunk(plain, b"caBX", b"C2PA manifest") + b"TRAILING"
  assert Image.open(io.BytesIO(png)).size == (64, 40)

  saved = _save(client, auth, app_id, **_complete_listing(
    screenshots=[
      {"data_base64": _b64(photo.getvalue()), "alt": "A photo."},
      {"data_base64": _b64(png), "alt": "A chart."},
    ],
  ))

  assert saved.status_code == 200, saved.text
  shots = json.loads((source / "mobius.json").read_text())["store"]["screenshots"]
  stored = [(source / shot["src"]).read_bytes() for shot in shots]
  assert not Image.open(io.BytesIO(stored[0])).getexif()
  assert b"Camera maker" not in stored[0] and b"JPEG comment" not in stored[0]
  assert stored[1] == plain
  for shot, content in zip(shots, stored):
    assert Path(shot["src"]).stem == hashlib.sha256(content).hexdigest()[:16]
  assert _preview(client, auth, app_id)["ready"] is True


def test_a_multi_picture_jpeg_is_saved_as_one_plain_jpeg(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  frames = [Image.new("RGB", (48, 32), (40 * index, 80, 120)) for index in range(2)]
  mpo = io.BytesIO()
  frames[0].save(mpo, format="MPO", save_all=True, append_images=frames[1:])
  assert Image.open(io.BytesIO(mpo.getvalue())).format == "MPO"

  saved = _save(client, auth, app_id, **_complete_listing(
    screenshots=[{"data_base64": _b64(mpo.getvalue()), "alt": "An HDR photo."}],
  ))

  assert saved.status_code == 200, saved.text
  src = json.loads((source / "mobius.json").read_text())["store"]["screenshots"][0]["src"]
  stored = Image.open(source / src)
  assert src.endswith(".jpg") and stored.format == "JPEG"
  assert getattr(stored, "n_frames", 1) == 1
  assert _preview(client, auth, app_id)["ready"] is True


def test_committed_art_with_metadata_is_flagged_and_cleaned_by_a_save(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  exif = Image.Exif()
  exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}
  photo = io.BytesIO()
  Image.new("RGB", (64, 40), (1, 120, 40)).save(photo, format="JPEG", exif=exif)
  (source / "static" / "store").mkdir(parents=True)
  (source / "static" / "store" / "shot.jpg").write_bytes(photo.getvalue())
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {
    "tagline": "t", "description": "d",
    "screenshots": [{"src": "static/store/shot.jpg", "alt": "A"}],
  }
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  _commit_accepted(db, source, app_id, "agent-committed photo")

  preview = _preview(client, auth, app_id)

  assert preview["ready"] is False
  assert _item(preview, "screenshots")["code"] == "listing_image_metadata"
  saved = _save(client, auth, app_id, **_complete_listing(
    screenshots=[{"path": "static/store/shot.jpg", "alt": "A"}],
  ))
  assert saved.status_code == 200, saved.text
  # Cleaned into a content-named copy, which a retry after a failed accept
  # can carry; the author's own file is left for them to remove.
  src = json.loads((source / "mobius.json").read_text())["store"]["screenshots"][0]["src"]
  assert src != "static/store/shot.jpg" and src.endswith(".jpg")
  assert not Image.open(source / src).getexif()
  # The original is still published with the source, so the listing stays
  # unready, and the file is left for an agent, never deleted by the save.
  assert (source / "static" / "store" / "shot.jpg").is_file()
  preview = _preview(client, auth, app_id)
  details = _item(preview, "details")
  assert preview["ready"] is False
  assert details["code"] == "listing_image_metadata"
  assert "static/store/shot.jpg" in details["message"]
  # Publish reads the same review, so it refuses the same way.
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  _, files = community_publish.build_public_snapshot(row)
  with pytest.raises(CommunityPublicationError) as refused:
    community_publish.public_store_listing(files)
  assert refused.value.code == "listing_image_metadata"

  (source / "static" / "store" / "shot.jpg").unlink()
  _commit_accepted(db, source, app_id, "agent removes the original photo")

  assert _preview(client, auth, app_id)["ready"] is True


@pytest.mark.parametrize("marker", [0xE3, 0xEB, 0xEF])
def test_any_non_drawing_jpeg_segment_or_trailing_bytes_count_as_metadata(
  client, auth, db, marker,
):
  source = _source()
  app_id = _create(client, auth, source)
  plain = io.BytesIO()
  Image.new("RGB", (64, 40), (1, 120, 40)).save(plain, format="JPEG")
  payload = b"c2pa provenance manifest"
  segment = bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload
  for shot in (
    plain.getvalue()[:2] + segment + plain.getvalue()[2:],
    plain.getvalue() + b"appended after the image",
  ):
    (source / "static" / "store").mkdir(parents=True, exist_ok=True)
    (source / "static" / "store" / "shot.jpg").write_bytes(shot)
    manifest = json.loads((source / "mobius.json").read_text())
    manifest["store"] = {
      "tagline": "t", "description": "d",
      "screenshots": [{"src": "static/store/shot.jpg", "alt": "A"}],
    }
    (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
    _commit_accepted(db, source, app_id, "agent-committed art")

    assert _item(_preview(client, auth, app_id), "screenshots")["code"] == (
      "listing_image_metadata"
    )
    saved = _save(client, auth, app_id, **_complete_listing(
      screenshots=[{"path": "static/store/shot.jpg", "alt": "A"}],
    ))
    assert saved.status_code == 200, saved.text
    src = json.loads((source / "mobius.json").read_text())["store"]["screenshots"][0]["src"]
    stored = (source / src).read_bytes()
    assert payload not in stored and b"appended" not in stored
    assert _preview(client, auth, app_id)["ready"] is False
    (source / "static" / "store" / "shot.jpg").unlink()
    _commit_accepted(db, source, app_id, "agent removes the original")
    assert _preview(client, auth, app_id)["ready"] is True


def test_an_icon_with_location_data_is_flagged_and_cleaned_by_a_save(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  exif = Image.Exif()
  exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}
  icon = io.BytesIO()
  Image.new("RGB", (64, 64), (200, 40, 40)).save(icon, format="PNG", exif=exif)
  (source / "icon.png").write_bytes(icon.getvalue())
  _commit_accepted(db, source, app_id, "icon with GPS")

  item = _item(_preview(client, auth, app_id), "icon")

  assert item["code"] == "listing_image_metadata" and item["automatic"] is True
  saved = _save(client, auth, app_id, **_complete_listing())
  assert saved.status_code == 200, saved.text
  assert not Image.open(source / "icon.png").getexif()
  assert _preview(client, auth, app_id)["ready"] is True


@pytest.mark.parametrize("style", ["inline", "compact"])
def test_saving_changes_only_the_listing_in_the_manifest_text(client, auth, style):
  source = _source()
  manifest = json.loads(_MANIFEST)
  manifest["categories"] = ["tools", "focus"]
  manifest["permissions"] = {"notifications": True}
  if style == "inline":
    text = json.dumps(manifest, indent=2, ensure_ascii=False)
    text = text.replace(
      '"categories": [\n    "tools",\n    "focus"\n  ]', '"categories": ["tools", "focus"]',
    ).replace(
      '"permissions": {\n    "notifications": true\n  }', '"permissions": {"notifications": true}',
    ) + "\n"
    assert '"categories": ["tools", "focus"]' in text
  else:
    text = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)
  (source / "mobius.json").write_text(text, encoding="utf-8")
  app_id = _create(client, auth, source)

  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200

  saved = (source / "mobius.json").read_text(encoding="utf-8")
  body = text.rstrip().removesuffix("}").rstrip()
  assert saved.startswith(body)
  assert json.loads(saved)["store"]["tagline"] == "Small and exact."
  if style == "compact":
    assert "\n" not in saved and ", " not in saved and '": ' not in saved


def _gps_jpeg(color=(1, 120, 40), size=(64, 40)) -> bytes:
  exif = Image.Exif()
  exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}
  photo = io.BytesIO()
  Image.new("RGB", size, color).save(photo, format="JPEG", exif=exif)
  return photo.getvalue()


def test_an_original_banner_outside_the_art_folder_keeps_the_listing_unready(
  client, auth, db,
):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "listing").mkdir()
  (source / "listing" / "hero.jpg").write_bytes(_gps_jpeg())
  _commit_accepted(db, source, app_id, "banner photo with GPS")

  saved = _save(client, auth, app_id, **_complete_listing(hero={"path": "listing/hero.jpg"}))

  assert saved.status_code == 200, saved.text
  hero = json.loads((source / "mobius.json").read_text())["store"]["hero"]
  assert hero.startswith("static/store/") and not Image.open(source / hero).getexif()
  preview = _preview(client, auth, app_id)
  assert preview["ready"] is False
  assert "listing/hero.jpg" in _item(preview, "details")["message"]

  (source / "listing" / "hero.jpg").unlink()
  _commit_accepted(db, source, app_id, "agent removes the original")
  assert _preview(client, auth, app_id)["ready"] is True


def test_an_icon_photo_is_cleaned_in_place_keeping_its_size(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  manifest["icon"] = "icon.jpg"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "icon.png").unlink()
  (source / "icon.jpg").write_bytes(_gps_jpeg((200, 40, 40), (64, 64)))
  _commit_accepted(db, source, app_id, "icon photo with GPS")

  icon = _item(_preview(client, auth, app_id), "icon")

  assert icon["code"] == "listing_image_metadata" and icon["automatic"] is True
  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200
  assert json.loads((source / "mobius.json").read_text())["icon"] == "icon.jpg"
  cleaned = Image.open(source / "icon.jpg")
  assert cleaned.format == "JPEG" and cleaned.size == (64, 64) and not cleaned.getexif()
  assert _preview(client, auth, app_id)["ready"] is True


def test_cleaning_icon_metadata_keeps_a_wide_icon_uncropped(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  exif = Image.Exif()
  exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0)}
  wide = io.BytesIO()
  Image.new("RGB", (1600, 400), (30, 60, 90)).save(wide, format="PNG", exif=exif)
  (source / "icon.png").write_bytes(wide.getvalue())
  _commit_accepted(db, source, app_id, "wide icon with GPS")

  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200

  cleaned = Image.open(source / "icon.png")
  assert cleaned.size == (1600, 400) and not cleaned.getexif()


def test_an_icon_sharing_editor_art_says_its_original_must_go_too(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "static" / "store").mkdir(parents=True)
  (source / "static" / "store" / "logo.jpg").write_bytes(_gps_jpeg((9, 9, 9), (64, 64)))
  manifest = json.loads(_MANIFEST)
  manifest["icon"] = "static/store/logo.jpg"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  _commit_accepted(db, source, app_id, "icon kept with the art")

  icon = _item(_preview(client, auth, app_id), "icon")

  assert icon["code"] == "listing_image_metadata" and "automatic" not in icon
  assert "static/store/logo.jpg" in icon["message"] and "copy" in icon["message"]


def test_a_skip_worktree_edit_cannot_ride_along(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  app_git._run(source, "update-index", "--skip-worktree", "index.jsx")
  (source / "index.jsx").write_text("export default function App() { return 'hidden' }\n")
  assert app_git._run(source, "status", "--porcelain").stdout == ""

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "source_has_draft"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.source_commit == app_git.head_sha(source, "HEAD")
  assert "hidden" not in app_git._run(source, "show", "HEAD:index.jsx").stdout


def test_an_edit_after_the_check_is_refused_by_apply(client, auth, db, monkeypatch):
  source = _source()
  app_id = _create(client, auth, source)
  accepted = db.query(models.App).filter_by(id=app_id).one().source_commit
  real_apply = app_routes._apply_source_or_http

  async def edit_then_apply(*args, **kwargs):
    # A concurrent edit lands after the candidate tree was checked.
    (source / "index.jsx").write_text("export default function App() { return 'late' }\n")
    return await real_apply(*args, **kwargs)

  monkeypatch.setattr(app_routes, "_apply_source_or_http", edit_then_apply)

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "source_changed"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.source_commit == accepted
  assert "late" not in app_git._run(source, "show", f"{accepted}:index.jsx").stdout
  assert "store" not in json.loads((source / "mobius.json").read_text())


def test_the_icon_is_not_fixed_by_a_save_that_invalid_json_refuses(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "icon.png").unlink()
  (source / "mobius.json").write_text("{not json")
  _commit_accepted(db, source, app_id, "broken manifest")
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.icon_png = _png((10, 200, 10), (32, 32))
  db.commit()

  assert "automatic" not in _item(_preview(client, auth, app_id), "icon")
  refused = _save(client, auth, app_id, **_complete_listing())
  assert refused.json()["detail"]["code"] == "manifest_invalid"


def test_an_author_file_that_only_looks_like_editor_art_survives(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "static" / "store").mkdir(parents=True)
  author = source / "static" / "store" / "0123456789abcdef.png"
  author.write_bytes(_png((8, 8, 8)))
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {"hero": "static/store/0123456789abcdef.png"}
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  _commit_accepted(db, source, app_id, "author file with a hash-like name")

  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200

  assert "hero" not in json.loads((source / "mobius.json").read_text())["store"]
  assert author.read_bytes() == _png((8, 8, 8))


def test_a_save_with_no_icon_anywhere_leaves_the_icon_unset(client, auth, db):
  source = _source()
  manifest = json.loads(_MANIFEST)
  manifest.pop("icon")
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "icon.png").unlink()
  app_id = _create(client, auth, source)
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.icon_png is None and row.icon_override_png is None

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text
  assert "icon" not in json.loads((source / "mobius.json").read_text())


def test_a_png_timestamp_alone_does_not_hold_the_listing_back(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  icon = (source / "icon.png").read_bytes()
  stamped = _with_png_chunk(icon, b"tIME", bytes([7, 234, 10, 14, 1, 2, 3]))
  (source / "icon.png").write_bytes(stamped)
  _commit_accepted(db, source, app_id, "icon with a timestamp")
  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200

  assert _preview(client, auth, app_id)["ready"] is True


def test_every_image_with_metadata_is_named_in_one_item(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200
  (source / "photos").mkdir()
  for index in range(7):
    (source / "photos" / f"p{index}.jpg").write_bytes(_gps_jpeg((index, 9, 9)))
  _commit_accepted(db, source, app_id, "seven photos with GPS")

  details = _item(_preview(client, auth, app_id), "details")

  assert details["code"] == "listing_image_metadata"
  assert details["message"].startswith("7 images in the source")
  assert all(f"photos/p{index}.jpg" in details["message"] for index in range(5))
  assert "and 2 more" in details["message"]


def test_a_manifest_with_a_byte_order_mark_keeps_it(client, auth):
  source = _source()
  (source / "mobius.json").write_bytes(b"\xef\xbb\xbf" + _MANIFEST.encode())
  app_id = _create(client, auth, source)

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text
  raw = (source / "mobius.json").read_bytes()
  assert raw.startswith(b"\xef\xbb\xbf" + _MANIFEST.encode().rstrip().rstrip(b"}").rstrip())
  assert json.loads(raw.decode("utf-8-sig"))["store"]["tagline"] == "Small and exact."


def test_a_jfif_thumbnail_counts_as_metadata():
  from app.community_publish import listing_image_has_metadata

  plain = io.BytesIO()
  Image.new("RGB", (16, 16), (5, 5, 5)).save(plain, format="JPEG")
  content = plain.getvalue()
  assert not listing_image_has_metadata(content)
  start = content.index(b"JFIF\0")
  thumb = bytes([1, 1]) + bytes(3)  # 1x1 thumbnail, three RGB bytes.
  payload = content[start:start + 12] + thumb
  segment = b"\xff\xe0" + (len(payload) + 2).to_bytes(2, "big") + payload
  old_length = int.from_bytes(content[start - 2:start], "big")
  with_thumb = content[:start - 4] + segment + content[start - 2 + old_length:]

  assert listing_image_has_metadata(with_thumb)


def test_an_app_accepted_with_older_ignore_rules_can_save_a_listing(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  older = "# An older managed ignore file.\nnode_modules/\n"
  (source / ".gitignore").write_text(older)
  _commit_accepted(db, source, app_id, "older ignore rules")

  _preview(client, auth, app_id)

  # A read-only preview never rewrites the app's ignore rules.
  assert (source / ".gitignore").read_text() == older
  saved = _save(client, auth, app_id, **_complete_listing())
  assert saved.status_code == 200, saved.text
  # The save's accepted revision carries apply's own ignore migration.
  assert app_git._run(source, "show", "HEAD:.gitignore").stdout == app_git._GITIGNORE


def test_undo_leaves_a_file_someone_edited_since_the_save(tmp_path):
  changes = store_listing_source._SourceChanges(tmp_path)
  (tmp_path / "mobius.json").write_text("original")
  changes.write("mobius.json", b"saved")
  changes.write("static/store/new.png", b"art")
  (tmp_path / "mobius.json").write_text("an agent's edit")

  changes.undo()

  assert (tmp_path / "mobius.json").read_text() == "an agent's edit"
  assert not (tmp_path / "static" / "store" / "new.png").exists()


def test_an_unapplied_icon_redirect_cannot_replace_a_tracked_file(client, auth, db):
  source = _source()
  (source / "notes.txt").write_text("Plain notes.\n")
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  manifest["icon"] = "notes.txt"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "notes.txt").write_bytes(_png((1, 2, 3), (64, 64)))

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "source_has_draft"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert app_git._run(source, "show", f"{row.source_commit}:notes.txt").stdout == "Plain notes.\n"


def test_the_public_preview_serves_draft_art_without_its_metadata(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "static" / "store").mkdir(parents=True)
  (source / "static" / "store" / "shot.jpg").write_bytes(_gps_jpeg())
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {"screenshots": [{"src": "static/store/shot.jpg", "alt": "A"}]}
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  _commit_accepted(db, source, app_id, "an incomplete draft with a GPS photo")
  preview = _preview(client, auth, app_id)
  assert preview["ready"] is False

  served = client.get(preview["asset_root"] + "static/store/shot.jpg")

  assert served.status_code == 200
  assert served.headers["content-type"] == "image/jpeg"
  image = Image.open(io.BytesIO(served.content))
  assert image.size == (64, 40) and not image.getexif()


def _truncated_jpeg() -> bytes:
  import random

  noise = Image.frombytes(
    "RGB", (128, 96), bytes(random.Random(1).getrandbits(8) for _ in range(128 * 96 * 3)),
  )
  photo = io.BytesIO()
  noise.save(photo, format="JPEG", quality=95)
  data = photo.getvalue()
  scan = data.index(b"\xff\xda")
  return data[:scan + (len(data) - scan) // 2]  # Cut inside the image data.


def test_a_broken_jpeg_upload_is_refused_not_a_crash(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  broken = _truncated_jpeg()
  assert Image.open(io.BytesIO(broken)).size == (128, 96)  # The header reads fine.

  refused = _save(client, auth, app_id, **_complete_listing(
    screenshots=[{"data_base64": _b64(broken), "alt": "A broken photo."}],
  ))

  assert refused.status_code == 422, refused.text
  assert refused.json()["detail"]["code"] == "listing_image_invalid"
  assert (source / "mobius.json").read_text(encoding="utf-8") == _MANIFEST


def test_a_committed_broken_jpeg_is_not_ready_and_not_served(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "static" / "store").mkdir(parents=True)
  (source / "static" / "store" / "shot.jpg").write_bytes(_truncated_jpeg())
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {
    "tagline": "t", "description": "d",
    "screenshots": [{"src": "static/store/shot.jpg", "alt": "A"}],
  }
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  _commit_accepted(db, source, app_id, "a broken photo")

  preview = _preview(client, auth, app_id)

  assert preview["ready"] is False
  assert _item(preview, "screenshots")["code"] == "listing_image_invalid"
  assert client.get(preview["asset_root"] + "static/store/shot.jpg").status_code == 404


@pytest.mark.parametrize("edit", ["icon", "store_flag"])
def test_unreviewed_manifest_edits_cannot_ride_along(client, auth, db, edit):
  source = _source()
  (source / "other.png").write_bytes(_png((4, 5, 6), (64, 64)))
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  if edit == "icon":
    manifest["icon"] = "other.png"  # Another tracked, clean, unchanged image.
  else:
    manifest["store"] = {"unreviewed_flag": True}
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "source_has_draft"


def test_a_clean_jpeg_upload_is_stored_byte_for_byte(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  photo = io.BytesIO()
  Image.new("RGB", (64, 40), (90, 30, 200)).save(photo, format="JPEG", quality=70)

  saved = _save(client, auth, app_id, **_complete_listing(
    screenshots=[{"data_base64": _b64(photo.getvalue()), "alt": "A clean photo."}],
  ))

  assert saved.status_code == 200, saved.text
  src = json.loads((source / "mobius.json").read_text())["store"]["screenshots"][0]["src"]
  assert (source / src).read_bytes() == photo.getvalue()


def test_a_byte_order_mark_never_hides_the_declared_icon(client, auth, db):
  source = _source()
  (source / "assets").mkdir()
  logo = _png((70, 80, 90), (64, 64))
  (source / "assets" / "logo.png").write_bytes(logo)
  manifest = json.loads(_MANIFEST)
  manifest["icon"] = "assets/logo.png"
  (source / "mobius.json").write_bytes(
    b"\xef\xbb\xbf" + json.dumps(manifest, indent=2).encode(),
  )
  (source / "icon.png").unlink()
  app_id = _create(client, auth, source)
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.icon_override_png = _png((250, 0, 0), (32, 32))  # The owner's own override.
  db.commit()

  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200

  assert (source / "assets" / "logo.png").read_bytes() == logo
  assert not (source / "icon.png").exists()


def test_a_tracked_runtime_file_changing_does_not_block_a_save(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  (source / ".gitignore").write_text("# Older rules that did not ignore logs.\n")
  (source / "run.log").write_text("first run\n")
  _commit_accepted(db, source, app_id, "older rules tracking a log")
  (source / "run.log").write_text("first run\nsecond run\n")  # Changes at runtime.
  accepted = db.query(models.App).filter_by(id=app_id).one().source_commit

  assert store_listing_source.has_unapplied_changes(source, accepted) is False
  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text
  # Apply untracked it, as its own snapshot always does.
  assert "run.log" not in app_git._run(source, "ls-files").stdout.split()


@pytest.mark.parametrize("value", [
  b"Editor 1.0",
  b"A. Person, 12 Example Street, +1 555 0100",
  b"a.person@example.com",
])
def test_png_software_text_counts_as_metadata_and_strips_away(value):
  import zlib

  from app.community_publish import (
    listing_image_has_metadata, strip_listing_image_metadata,
  )

  plain = _png()
  # Every text form PNG has, with no carve-out for the Software key.
  for kind, data in (
    (b"tEXt", b"Software\0" + value),
    (b"zTXt", b"Software\0\0" + zlib.compress(value)),
    (b"iTXt", b"Software\0\0\0\0\0" + value),
    (b"iTXt", b"Software\0\1\0\0\0" + zlib.compress(value)),
  ):
    png = _with_png_chunk(plain, kind, data)
    assert listing_image_has_metadata(png) is True
    assert strip_listing_image_metadata(png) == plain


def test_an_unexpected_image_error_is_a_checklist_item_not_a_crash(monkeypatch):
  from app import community_publish

  def broken(content, what):
    raise KeyError("surprise")

  monkeypatch.setattr(community_publish, "check_listing_image", broken)
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {
    "tagline": "t", "description": "d",
    "screenshots": [{"src": "static/store/a.png", "alt": "A"}],
  }
  review = store_listing_review([
    _file("mobius.json", json.dumps(manifest).encode()),
    _file("icon.png", _png()), _file("static/store/a.png", _png()),
  ])

  assert review["ready"] is False
  assert _item(review, "screenshots")["code"] == "listing_check_failed"


def test_saving_keeps_crlf_and_escaped_manifests_as_they_are(client, auth):
  source = _source()
  manifest = json.loads(_MANIFEST)
  text = json.dumps(manifest, indent=2).replace("\n", "\r\n") + "\r\n"
  assert "\\u00e9" in text
  (source / "mobius.json").write_bytes(text.encode())
  app_id = _create(client, auth, source)

  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200

  saved = (source / "mobius.json").read_bytes()
  assert saved.startswith(text.encode().rstrip().rstrip(b"}").rstrip())
  assert b"\n" not in saved.replace(b"\r\n", b"")
  assert "\\u00e9".encode() in saved


@pytest.mark.parametrize("problem", ["no_entry", "bad_json", "secret"])
def test_preview_reports_publication_problems_as_checklist_items(
  client, auth, db, problem,
):
  source = _source()
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {"tagline": "Kept.", "description": "Still here."}
  if problem == "no_entry":
    manifest.pop("entry")
  (source / "mobius.json").write_text(
    "{not json" if problem == "bad_json" else json.dumps(manifest, indent=2),
  )
  if problem == "secret":
    (source / "config.js").write_text("export const key = 'ghp_" + "A" * 36 + "'\n")
  _commit_accepted(db, source, app_id, problem)

  preview = _preview(client, auth, app_id)

  details = _item(preview, "details")
  assert preview["ready"] is False and details["done"] is False
  assert details["code"] == {
    "no_entry": "invalid_manifest", "bad_json": "invalid_manifest",
    "secret": "secret_detected",
  }[problem]
  if problem != "bad_json":
    assert preview["draft"]["tagline"] == "Kept."


def test_a_save_interrupted_before_its_commit_recovers(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {"tagline": "Left behind."}
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "static" / "store").mkdir(parents=True)
  art = _png((3, 3, 3))
  (source / "static" / "store" / f"{_content_name(art)}.png").write_bytes(art)

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_removing_listing_art_keeps_files_the_editor_did_not_create(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "static" / "store").mkdir(parents=True)
  (source / "static" / "store" / "hero.png").write_bytes(_png((8, 8, 8)))
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {"hero": "static/store/hero.png"}
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  _commit_accepted(db, source, app_id, "hand-made hero")

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text
  assert "hero" not in json.loads((source / "mobius.json").read_text())["store"]
  assert (source / "static" / "store" / "hero.png").is_file()


def test_icon_is_not_automatic_while_unapplied_edits_block_the_save(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  manifest.pop("icon")
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "icon.png").unlink()
  _commit_accepted(db, source, app_id, "icon on the record only")
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.icon_png = _png((10, 200, 10), (32, 32))
  db.commit()
  assert _item(_preview(client, auth, app_id), "icon")["automatic"] is True

  (source / "index.jsx").write_text("export default function App() { return null }\n")

  assert "automatic" not in _item(_preview(client, auth, app_id), "icon")


def test_saving_keeps_the_manifest_layout(client, auth):
  source = _source()
  manifest_text = json.dumps(json.loads(_MANIFEST), ensure_ascii=False, indent=4) + "\n"
  (source / "mobius.json").write_text(manifest_text, encoding="utf-8")
  app_id = _create(client, auth, source)

  assert _save(client, auth, app_id, **_complete_listing()).status_code == 200

  saved = (source / "mobius.json").read_text(encoding="utf-8")
  assert saved.startswith(manifest_text.rstrip().rstrip("}").rstrip())
  assert '\n    "store": {\n        "tagline"' in saved
  assert saved.endswith("}\n")
  assert list(json.loads(saved)) == [*json.loads(manifest_text), "store", "license"]


def test_saving_needs_an_accepted_revision_to_tell_what_was_reviewed(client, auth, db):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "index.jsx").write_text("export default function App() { return 'unreviewed' }\n")
  app_git._run(source, "commit", "-qam", "never applied")
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.source_commit = None
  db.commit()

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "source_has_draft"


def test_a_save_interrupted_with_an_uppercase_png_icon_recovers(client, auth):
  source = _source()
  (source / "icon.png").rename(source / "icon.PNG")
  manifest = json.loads(_MANIFEST)
  manifest["icon"] = "icon.PNG"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  app_id = _create(client, auth, source)
  # What an interrupted save leaves: the new icon written, not yet accepted.
  (source / "icon.PNG").write_bytes(_png((1, 99, 1), (64, 64)))

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text


def test_editor_named_art_must_hold_the_bytes_its_name_hashes(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "static" / "store").mkdir(parents=True)
  (source / "static" / "store" / "0123456789abcdef.png").write_bytes(_png((3, 3, 3)))

  refused = _save(client, auth, app_id, **_complete_listing())

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "source_has_draft"


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


def _save_task(app_id, db, **listing):
  return lambda: app_routes.save_store_listing(
    app_id, schemas.StoreListingIn(**_complete_listing(**listing)), db, None,
  )


@pytest.mark.asyncio
async def test_cancel_during_git_commit_waits_for_coherent_accept(
  client, auth, db, monkeypatch,
):
  source = _source()
  app_id = _create(client, auth, source)
  published = []
  monkeypatch.setattr(
    app_routes, "_publish_apply_events",
    lambda result, chat_id: published.append((result.mode, chat_id)),
  )
  commit = _HeldCall(app_git.commit_worktree_tree)
  monkeypatch.setattr(app_git, "commit_worktree_tree", commit)

  outcome = await cancel_while_worker_blocked(
    fs_locks.source_dir_lock(str(source)), _save_task(app_id, db), commit,
  )

  assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.source_commit == app_git.head_sha(source, "HEAD")
  assert app_git._run(source, "status", "--porcelain").stdout == ""
  assert "store" in json.loads((source / "mobius.json").read_text())
  # Nobody awaits the response, but the accepted revision is still announced.
  assert published == [("updated", None)]


@pytest.mark.asyncio
async def test_cancel_during_listing_write_holds_the_source_lock(
  client, auth, db, monkeypatch,
):
  source = _source()
  app_id = _create(client, auth, source)
  write = _HeldCall(store_listing_source.write_listing)
  monkeypatch.setattr(store_listing_source, "write_listing", write)

  outcome = await cancel_while_worker_blocked(
    fs_locks.source_dir_lock(str(source)), _save_task(app_id, db), write,
  )

  assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
  # The write it waited for was accepted as one coherent revision.
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.source_commit == app_git.head_sha(source, "HEAD")
  assert app_git._run(source, "status", "--porcelain").stdout == ""


@pytest.mark.asyncio
async def test_cancel_during_undo_restores_source_before_releasing_lock(
  client, auth, db, monkeypatch,
):
  source = _source()
  app_id = _create(client, auth, source)
  real_write = store_listing_source.write_listing
  undo = BlockedWorker()

  def write(*args):
    undo.then = real_write(*args)
    return undo

  monkeypatch.setattr(store_listing_source, "write_listing", write)
  monkeypatch.setattr(
    app_apply, "apply_source_revision",
    _raises(app_apply.AppApplyError("boom", "Apply failed.")),
  )

  outcome = await cancel_while_worker_blocked(
    fs_locks.source_dir_lock(str(source)), _save_task(app_id, db), undo,
    while_blocked=lambda: "store" in json.loads((source / "mobius.json").read_text()),
  )

  assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
  assert (source / "mobius.json").read_text(encoding="utf-8") == _MANIFEST
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def _raises(error):
  async def fail(*args, **kwargs):
    raise error
  return fail


def test_ignored_worktree_images_cannot_become_listing_art(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  (source / "static").mkdir()
  (source / "static" / "private.png").write_bytes(_png((7, 7, 7)))
  assert app_git._run(source, "status", "--porcelain").stdout == ""

  for field in ("screenshots", "hero", "icon"):
    image = {"path": "static/private.png"}
    body = (
      _complete_listing(screenshots=[{**image, "alt": "Private."}])
      if field == "screenshots" else _complete_listing(**{field: image})
    )
    refused = _save(client, auth, app_id, **body)

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "listing_image_missing"
  assert (source / "mobius.json").read_text(encoding="utf-8") == _MANIFEST
  assert not [path for path in source.rglob("static/store/*") if path.is_file()]
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_removing_listing_art_keeps_the_icon_that_shares_it(client, auth):
  source = _source()
  app_id = _create(client, auth, source)
  assert _save(client, auth, app_id, **_complete_listing(
    hero={"data_base64": _b64(_png((1, 2, 3)))},
  )).status_code == 200
  hero = _preview(client, auth, app_id)["listing"]["hero"]["path"]
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["icon"] = hero
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "icon.png").unlink()
  applied = client.post("/api/apps/apply", json={"source_dir": str(source)}, headers=auth)
  assert applied.status_code == 200, applied.text

  saved = _save(client, auth, app_id, **_complete_listing())

  assert saved.status_code == 200, saved.text
  assert (source / hero).read_bytes() == _png((1, 2, 3))
  assert json.loads((source / "mobius.json").read_text())["icon"] == hero
  assert _preview(client, auth, app_id)["ready"] is True


def test_a_new_icon_never_overwrites_listing_art_it_shared(client, auth):
  source = _source()
  original_icon = (source / "icon.png").read_bytes()
  app_id = _create(client, auth, source)
  assert _save(client, auth, app_id, **_complete_listing(
    hero={"data_base64": _b64(_png((1, 2, 3)))},
  )).status_code == 200
  hero = _preview(client, auth, app_id)["listing"]["hero"]["path"]
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["icon"] = hero
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  assert client.post(
    "/api/apps/apply", json={"source_dir": str(source)}, headers=auth,
  ).status_code == 200

  saved = _save(client, auth, app_id, **_complete_listing(
    hero={"path": hero}, icon={"data_base64": _b64(_png((5, 5, 5), (64, 64)))},
  ))

  assert saved.status_code == 200, saved.text
  assert (source / hero).read_bytes() == _png((1, 2, 3))
  icon = json.loads((source / "mobius.json").read_text())["icon"]
  # A new icon gets its own content-named file, never another file's path.
  assert icon != hero and icon.startswith("static/store/")
  assert (source / "icon.png").read_bytes() == original_icon


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
  # The save is refused until an agent adds details, so it fixes no icon.
  assert icon["done"] is False and "automatic" not in icon

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
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  row.source_commit = app_git.head_sha(source, "HEAD")
  db.commit()

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
      listing._accepted_file(root, None, "static/image.png")
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


def test_only_editor_art_and_the_declared_icon_ride_along_unreviewed(tmp_path):
  """A listing save may carry its own hash-named art and declared PNG icon
  only; any other file under the art folder, or an undeclared icon.png, is an
  unreviewed edit."""
  root = tmp_path / "app"
  root.mkdir()
  app_git.ensure_repo(root)  # As real app sources are: managed ignore rules on main.
  (root / "mobius.json").write_text(json.dumps({"id": "demo", "icon": "assets/logo.svg"}))
  subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
  subprocess.run(
    ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
     "commit", "-q", "-m", "accepted"], check=True,
  )
  accepted = app_git.head_sha(root, "HEAD")
  art = root / "static" / "store"
  art.mkdir(parents=True)

  clean = _png((5, 6, 7))
  (art / f"{_content_name(clean)}.png").write_bytes(clean)
  assert store_listing_source.has_unapplied_changes(root, accepted) is False
  # Arbitrary bytes, or an image with hidden metadata, cannot pass as editor
  # art even under the name their content hashes to.
  for content in (b"not an image", _png((5, 6, 7)) + b"trailing"):
    (art / f"{_content_name(content)}.png").write_bytes(content)
    assert store_listing_source.has_unapplied_changes(root, accepted) is True
    (art / f"{_content_name(content)}.png").unlink()

  (art / "loader.js").write_text("alert(1)")
  assert store_listing_source.has_unapplied_changes(root, accepted) is True
  (art / "loader.js").unlink()

  (root / "icon.png").write_bytes(b"png")
  assert store_listing_source.has_unapplied_changes(root, accepted) is True


def test_a_malformed_image_anywhere_never_takes_down_the_review(monkeypatch):
  """A truncated WebP chunk is read safely, and an image the scan can't parse
  is named on the checklist rather than turning the review into a 500."""
  from app import community_publish
  from app.community_publish import _published_images_with_metadata

  broken = b"RIFF" + (12).to_bytes(4, "little") + b"WEBP" + b"VP8X" + (0).to_bytes(4, "little")
  files = {"assets/broken.webp": {
    "path": "assets/broken.webp", "content_base64": base64.b64encode(broken).decode(),
  }}
  assert community_publish.listing_image_has_metadata(broken) is False
  assert _published_images_with_metadata(files, set()) == []

  def unparseable(content):
    raise IndexError("truncated chunk")

  monkeypatch.setattr(community_publish, "listing_image_has_metadata", unparseable)
  problems = _published_images_with_metadata(files, set())
  assert [p.code for p in problems] == ["listing_image_metadata"]
  assert "assets/broken.webp" in problems[0].detail


def test_a_declared_icon_rides_along_only_as_a_clean_image(tmp_path):
  """A changed icon passes the guard only at a path the save could have
  written, and only as a clean listing image, so pointing the icon at a
  file cannot get arbitrary bytes accepted."""
  root = tmp_path / "app"
  root.mkdir()
  app_git.ensure_repo(root)  # As real app sources are: managed ignore rules on main.
  (root / "mobius.json").write_text(json.dumps({"id": "demo", "icon": "assets/logo.svg"}))
  subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
  subprocess.run(
    ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
     "commit", "-q", "-m", "accepted"], check=True,
  )
  accepted = app_git.head_sha(root, "HEAD")
  (root / "mobius.json").write_text(json.dumps({"id": "demo", "icon": "payload.png"}))
  (root / "payload.png").write_text("<script>alert(1)</script>")
  assert store_listing_source.has_unapplied_changes(root, accepted) is True
  # An unreviewed redirect of the icon is refused even with clean bytes:
  # only a path the save itself could have written may ride along.
  (root / "payload.png").write_bytes(_png((1, 2, 3)))
  assert store_listing_source.has_unapplied_changes(root, accepted) is True
  (root / "payload.png").unlink()
  clean = _png((1, 2, 3))
  named = f"static/store/{_content_name(clean)}.png"
  (root / "static" / "store").mkdir(parents=True)
  (root / named).write_bytes(clean)
  (root / "mobius.json").write_text(json.dumps({"id": "demo", "icon": named}))
  assert store_listing_source.has_unapplied_changes(root, accepted) is False


# --- License: publishing requires one of the free licenses the Store offers.

def _ready_files(**manifest_fields) -> list[dict]:
  manifest = json.loads(_MANIFEST)
  manifest["store"] = {
    "tagline": "t", "description": "d",
    "screenshots": [{"src": "static/store/a.png", "alt": "A"}],
  }
  manifest.update(manifest_fields)
  return [
    _file("mobius.json", json.dumps(manifest).encode()),
    _file("icon.png", _png()), _file("static/store/a.png", _png()),
  ]


@pytest.mark.parametrize("listed", [False, True])
@pytest.mark.parametrize("declared", [None, "", "Proprietary", "MIT AND CC-BY-3.0-IGO"])
def test_publishing_requires_an_offered_license(declared, listed):
  """Being listed already keeps only another free license, never these."""
  fields = {} if declared is None else {"license": declared}
  files = _ready_files(**fields) + [_file("LICENSE", b"All rights reserved.")]

  review = store_listing_review(files, listed=listed)

  item = next(item for item in review["checklist"] if item["id"] == "license")
  assert item["done"] is False and item["optional"] is False
  assert item["message"] == (
    "Choose a free license so people know how they may use and share your app."
  )
  assert review["ready"] is False and review["draft"]["license"] == ""
  with pytest.raises(CommunityPublicationError) as refused:
    public_store_listing(files, listed=listed)
  assert refused.value.code == "listing_incomplete"


@pytest.mark.parametrize("listed", [False, True])
def test_an_offered_license_also_needs_its_text(listed):
  without_text = store_listing_review(_ready_files(license="Apache-2.0"), listed=listed)
  item = next(item for item in without_text["checklist"] if item["id"] == "license")
  assert item["done"] is False and "LICENSE" in item["message"]
  assert without_text["draft"]["license"] == "Apache-2.0"

  with_text = store_listing_review(
    _ready_files(license="Apache-2.0") + [_file("license.md", b"Apache text")],
    listed=listed,
  )
  assert with_text["ready"] is True
  assert with_text["listing"]["license"] == "Apache-2.0"


def test_a_listed_app_keeps_publishing_under_its_other_free_license():
  """An app listed under BSD before the rule can still publish updates; the
  Host checks the license is the listed revision's own."""
  files = _ready_files(license="BSD-3-Clause") + [_file("LICENSE", b"BSD text")]

  review = store_listing_review(files, listed=True)

  assert review["ready"] is True
  assert _item(review, "license") == {
    "id": "license", "label": "License", "optional": False,
    "done": True, "code": "", "message": "",
  }
  assert review["listing"]["license"] == "BSD-3-Clause"
  # The picker still offers only the three and selects none of them.
  assert [item["spdx"] for item in review["license_options"]] == [
    "MIT", "Apache-2.0", "GPL-3.0-or-later",
  ]
  assert review["draft"]["license"] == ""
  assert review["draft"]["declared_license"] == "BSD-3-Clause"
  assert public_store_listing(files, listed=True)["license"] == "BSD-3-Clause"

  without_text = store_listing_review(_ready_files(license="BSD-3-Clause"), listed=True)
  assert "LICENSE" in _item(without_text, "license")["message"]


def test_a_first_listing_cannot_use_another_free_license():
  files = _ready_files(license="BSD-3-Clause") + [_file("LICENSE", b"BSD text")]

  review = store_listing_review(files)

  assert review["ready"] is False
  assert _item(review, "license")["message"] == (
    "Choose a free license the Store offers. Only an app the Store already "
    "lists under BSD-3-Clause can keep publishing under it."
  )
  assert review["draft"]["declared_license"] == "BSD-3-Clause"
  with pytest.raises(CommunityPublicationError) as refused:
    public_store_listing(files)
  assert refused.value.code == "listing_incomplete"


@pytest.mark.parametrize(
  ("declared", "offered"),
  [(None, ""), (" BSD-3-Clause ", ""), ("Apache-2.0", "Apache-2.0")],
)
def test_the_review_reports_the_offered_licenses_and_the_declared_one(declared, offered):
  """The Store's picker renders the platform's options, recommended first,
  and can tell the owner that choosing one relicenses what the app declares."""
  from app import store_licenses

  fields = {} if declared is None else {"license": declared}
  review = store_listing_review(_ready_files(**fields))

  assert review["license_options"] == [
    {"spdx": item.spdx, "name": item.name} for item in store_licenses.OFFERED_LICENSES
  ]
  assert review["license_options"][0]["spdx"] == "MIT"
  assert review["draft"]["license"] == offered
  assert review["draft"]["declared_license"] == (declared or "").strip()


def test_the_preview_carries_the_license_options(client, auth):
  source = _source()
  app_id = _create(client, auth, source)

  preview = _preview(client, auth, app_id)

  assert [item["spdx"] for item in preview["license_options"]] == [
    "MIT", "Apache-2.0", "GPL-3.0-or-later",
  ]
  assert preview["draft"]["declared_license"] == ""


def test_saving_a_license_writes_the_choice_and_its_full_text(client, auth):
  source = _source()
  app_id = _create(client, auth, source)

  assert _save(client, auth, app_id, **_complete_listing(license="MIT")).status_code == 200

  manifest = json.loads((source / "mobius.json").read_text(encoding="utf-8"))
  assert manifest["license"] == "MIT"
  text = (source / "LICENSE").read_text(encoding="utf-8")
  assert text.startswith("MIT License\n\nCopyright (c) The Démo authors\n")
  preview = _preview(client, auth, app_id)
  assert preview["ready"] is True and preview["draft"]["license"] == "MIT"
  assert _item(preview, "license")["done"] is True

  assert _save(client, auth, app_id, **_complete_listing(license="GPL-3.0-or-later")).status_code == 200
  assert (source / "LICENSE").read_text(encoding="utf-8").lstrip().startswith(
    "GNU GENERAL PUBLIC LICENSE",
  )
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_keeping_the_same_license_keeps_the_authors_own_text(client, auth, db):
  source = _source()
  manifest = json.loads(_MANIFEST)
  manifest["license"] = "MIT"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026 Example Author\n")
  app_id = _create(client, auth, source)

  assert _save(client, auth, app_id, **_complete_listing(license="MIT")).status_code == 200
  assert "2026 Example Author" in (source / "LICENSE").read_text()
  # An older client that sends no license leaves it as it is.
  body = _complete_listing()
  body.pop("license")
  assert _save(client, auth, app_id, **body).status_code == 200
  assert json.loads((source / "mobius.json").read_text())["license"] == "MIT"


@pytest.mark.parametrize(
  ("declared", "old_text"),
  [(None, "LICENSE"), ("MIT", "LICENSE"), ("GPL-3.0-or-later", "COPYING"),
   ("MIT", "LICENSE.md"), ("BSD-3-Clause", "license")],
)
def test_changing_the_license_never_replaces_someone_elses_text(
  client, auth, declared, old_text,
):
  """A remix carries its upstream author's notice, which its license may
  require it to keep. Only the source can change that text, keeping it."""
  source = _source()
  manifest = json.loads(_MANIFEST)
  if declared is not None:
    manifest["license"] = declared
  original_manifest = json.dumps(manifest, indent=2)
  (source / "mobius.json").write_text(original_manifest)
  upstream = "MIT License\n\nCopyright (c) 2026 Upstream Author\n"
  (source / old_text).write_text(upstream)
  app_id = _create(client, auth, source)
  new_license = "Apache-2.0" if declared == "MIT" else "MIT"

  refused = _save(client, auth, app_id, **_complete_listing(license=new_license))

  assert refused.status_code == 409, refused.text
  detail = refused.json()["detail"]
  assert detail["code"] == "listing_license_text_exists"
  assert f"({old_text})" in detail["message"]
  assert (source / old_text).read_text() == upstream
  assert (source / "mobius.json").read_text() == original_manifest
  assert app_git._run(source, "status", "--porcelain").stdout == ""


def test_a_generated_license_text_beside_another_is_kept(client, auth):
  from app import store_licenses

  source = _source()
  manifest = json.loads(_MANIFEST)
  manifest["license"] = "MIT"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "LICENSE").write_bytes(store_licenses.license_text("MIT", manifest))
  (source / "COPYING").write_text("Portions: Copyright (c) 2026 Upstream Author\n")
  app_id = _create(client, auth, source)

  refused = _save(client, auth, app_id, **_complete_listing(license="Apache-2.0"))

  assert refused.status_code == 409, refused.text
  assert refused.json()["detail"]["message"].startswith(
    "This app already has license text (COPYING, LICENSE).",
  )
  assert "Upstream Author" in (source / "COPYING").read_text()


def test_keeping_the_license_keeps_an_alternate_license_text(client, auth):
  source = _source()
  manifest = json.loads(_MANIFEST)
  manifest["license"] = "MIT"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "COPYING").write_text("MIT License\n\nCopyright (c) 2026 Example Author\n")
  app_id = _create(client, auth, source)

  assert _save(client, auth, app_id, **_complete_listing(license="MIT")).status_code == 200
  assert "2026 Example Author" in (source / "COPYING").read_text()
  assert not (source / "LICENSE").exists()


def test_only_an_offered_license_can_be_saved(client, auth):
  source = _source()
  app_id = _create(client, auth, source)

  refused = _save(client, auth, app_id, **_complete_listing(license="Proprietary"))

  assert refused.status_code == 422, refused.text
  assert refused.json()["detail"]["code"] == "listing_license_invalid"
  assert (source / "mobius.json").read_text(encoding="utf-8") == _MANIFEST
  assert not (source / "LICENSE").exists()


@pytest.mark.parametrize(
  "edit", ["license_text", "license_value", "license_deleted", "license_switched"],
)
def test_unreviewed_license_edits_cannot_ride_along(client, auth, edit):
  source = _source()
  author_text = "MIT License\n\nCopyright (c) 2026 Example Author\n"
  if edit in ("license_deleted", "license_switched"):
    manifest = json.loads(_MANIFEST)
    manifest["license"] = "MIT"
    (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
    (source / "LICENSE").write_text(author_text)
  app_id = _create(client, auth, source)
  if edit == "license_deleted":
    # A save never deletes the license text.
    (source / "LICENSE").unlink()
  elif edit == "license_switched":
    # Only the manifest names the new license; the old MIT text stays, so
    # saving the same new choice would keep it and publish both licenses.
    manifest["license"] = "Apache-2.0"
  elif edit == "license_text":
    # Anything but the exact text a save writes was never reviewed.
    (source / "LICENSE").write_text("import os; os.system('x')\n")
    manifest = json.loads(_MANIFEST)
    manifest["license"] = "MIT"
  else:
    manifest = json.loads(_MANIFEST)
    manifest["license"] = "Proprietary"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  app_git._run(source, "add", "-A")
  app_git._run(source, "commit", "-qm", "committed, never applied")

  saved_license = "Apache-2.0" if edit == "license_switched" else "MIT"
  refused = _save(client, auth, app_id, **_complete_listing(license=saved_license))

  assert refused.status_code == 409, refused.text
  assert refused.json()["detail"]["code"] == "source_has_draft"
  if edit == "license_switched":
    assert (source / "LICENSE").read_text() == author_text


@pytest.mark.parametrize("old_text", ["generated", "LICENSE", "COPYING"])
def test_a_license_switch_rides_along_only_over_a_generated_text(tmp_path, old_text):
  """A changed license choice counts as a listing save only as a save makes
  it: the new generated LICENSE over the one generated for the old choice,
  never over anyone else's text, in the worktree and in the exact tree apply
  accepts alike."""
  from app import store_licenses

  root = tmp_path / "app"
  root.mkdir()
  app_git.ensure_repo(root)
  manifest = {"id": "demo", "name": "Demo", "license": "MIT"}
  (root / "mobius.json").write_text(json.dumps(manifest))
  if old_text == "generated":
    (root / "LICENSE").write_bytes(store_licenses.license_text("MIT", manifest))
  else:
    (root / old_text).write_text("MIT License\n\nCopyright (c) 2026 Example Author\n")
  subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
  subprocess.run(
    ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
     "commit", "-q", "-m", "accepted"], check=True,
  )
  accepted = app_git.head_sha(root, "HEAD")

  def rides_along() -> bool:
    in_worktree = store_listing_source.changes_beyond_listing(root, accepted)
    tree = app_git.snapshot_worktree(root).tree_oid
    assert store_listing_source.changes_beyond_listing(root, accepted, tree) is in_worktree
    return not in_worktree

  manifest["license"] = "Apache-2.0"
  (root / "mobius.json").write_text(json.dumps(manifest))
  assert rides_along() is False
  (root / "LICENSE").write_bytes(store_licenses.license_text("Apache-2.0", manifest))
  assert rides_along() is (old_text == "generated")
  if old_text == "COPYING":
    # Nor may the author's text simply go.
    (root / "COPYING").unlink()
    assert rides_along() is False


@pytest.mark.parametrize("license_written", [True, False])
def test_removing_an_old_license_text_never_rides_along(
  client, auth, license_written,
):
  from app import store_licenses

  source = _source()
  (source / "COPYING").write_text("Old terms\n")
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  manifest["license"] = "Apache-2.0"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  if license_written:
    (source / "LICENSE").write_bytes(store_licenses.license_text("Apache-2.0", manifest))
  (source / "COPYING").unlink()
  app_git._run(source, "add", "-A")
  app_git._run(source, "commit", "-qm", "license change only")

  saved = _save(client, auth, app_id, **_complete_listing(license="Apache-2.0"))

  assert saved.status_code == 409, saved.text
  assert saved.json()["detail"]["code"] == "source_has_draft"


def test_a_license_an_interrupted_save_wrote_may_ride_along(client, auth):
  from app import store_licenses

  source = _source()
  app_id = _create(client, auth, source)
  manifest = json.loads(_MANIFEST)
  manifest["license"] = "Apache-2.0"
  (source / "mobius.json").write_text(json.dumps(manifest, indent=2))
  (source / "LICENSE").write_bytes(store_licenses.license_text("Apache-2.0", manifest))
  app_git._run(source, "add", "-A")
  app_git._run(source, "commit", "-qm", "license files only")

  saved = _save(client, auth, app_id, **_complete_listing(license="Apache-2.0"))

  assert saved.status_code == 200, saved.text
