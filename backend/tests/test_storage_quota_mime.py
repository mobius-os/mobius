"""Storage card 085: per-app write quota + backend MIME sidecar.

Two deferred-from-083 tails:

  1. A per-app on-disk byte quota so one mini-app can't fill `/data` on
     the memory/disk-tight host. Enforced at PUT time against the app's
     current usage; an over-quota write is rejected before it lands.

  2. A MIME sidecar so a COLD read (cache miss / fresh device) of an
     extensionless or custom-MIME blob serves the Content-Type the app
     set, instead of the server's filename guess (which defaults to
     text/plain). The sidecar is written on PUT, read by the serve path,
     deleted on file delete, and reported in the listing's `mime_type` —
     all while living OUTSIDE the app's user-visible storage tree so it
     never leaks into listings or agent edits.
"""

import os
from pathlib import Path

import pytest

from app import storage_io
from app.config import get_settings


# A small per-app cap for quota tests so they exercise the limit without
# writing hundreds of MB. The production cap (storage_io.MAX_APP_STORAGE_BYTES)
# is far larger; the enforcement logic is identical at any cap.
_TEST_APP_CAP = 64 * 1024


@pytest.fixture
def small_app_cap(monkeypatch):
  """Shrinks the per-app quota so over-cap behavior is cheap to test.

  The route reads the cap through `storage_io.app_storage_cap()`, so
  patching the module constant is enough — no env or settings plumbing.
  """
  monkeypatch.setattr(storage_io, "MAX_APP_STORAGE_BYTES", _TEST_APP_CAP)
  return _TEST_APP_CAP


def _make_app(client, owner_token, name="store-test"):
  r = client.post("/api/apps/", json={
    "name": name,
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  return r.json()["id"]


# --- MIME sidecar ---------------------------------------------------------


def test_extensionless_blob_cold_read_serves_stored_mime(
  client, auth, owner_token
):
  """A PNG stored at an extensionless path round-trips image/png.

  The filename has no extension, so the server's mimetypes guess can't
  recover the type — only the sidecar written from the PUT Content-Type
  can. This is the headline cold-read fix.
  """
  app_id = _make_app(client, owner_token)
  png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

  r = client.put(
    f"/api/storage/apps/{app_id}/avatar",
    data=png,
    headers={**auth, "Content-Type": "image/png"},
  )
  assert r.status_code == 204

  r = client.get(f"/api/storage/apps/{app_id}/avatar", headers=auth)
  assert r.status_code == 200
  assert r.content == png
  assert r.headers["content-type"].split(";")[0] == "image/png"


def test_custom_mime_overrides_extension_guess(client, auth, owner_token):
  """A stored MIME wins over the extension guess on read.

  The path ends in `.bin` (guesses application/octet-stream), but the
  app declared a custom type — the sidecar must override the guess.
  """
  app_id = _make_app(client, owner_token)
  data = b"glTF-ish bytes"

  r = client.put(
    f"/api/storage/apps/{app_id}/model.bin",
    data=data,
    headers={**auth, "Content-Type": "model/gltf-binary"},
  )
  assert r.status_code == 204

  r = client.get(f"/api/storage/apps/{app_id}/model.bin", headers=auth)
  assert r.headers["content-type"].split(";")[0] == "model/gltf-binary"


def test_listing_reports_sidecar_mime(client, auth, owner_token):
  """The directory listing surfaces the stored MIME, not the guess."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/media/clip",
    data=b"\x00\x01\x02",
    headers={**auth, "Content-Type": "audio/webm"},
  )

  r = client.get(f"/api/storage/apps-list/{app_id}/media", headers=auth)
  assert r.status_code == 200
  entry = next(e for e in r.json()["entries"] if e["name"] == "clip")
  assert entry["mime_type"] == "audio/webm"


def test_sidecar_not_listed_in_app_tree(client, auth, owner_token):
  """The sidecar lives outside the app's storage tree, so a listing of
  the app root never advertises a `.json` metadata file."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/avatar",
    data=b"\x89PNG",
    headers={**auth, "Content-Type": "image/png"},
  )

  r = client.get(f"/api/storage/apps-list/{app_id}/", headers=auth)
  names = [e["name"] for e in r.json()["entries"]]
  assert "avatar" in names
  assert all(not n.endswith(".json") for n in names)
  # And nothing resembling a meta dir leaks in.
  assert ".storage-meta" not in names


def test_delete_removes_sidecar(client, auth, owner_token):
  """Deleting the file removes its sidecar, so a later same-path write of
  a DIFFERENT type isn't shadowed by a stale stored MIME."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/asset",
    data=b"\x89PNG",
    headers={**auth, "Content-Type": "image/png"},
  )
  # The sidecar must physically vanish on delete.
  meta = (
    os.path.join(
      get_settings().data_dir, ".storage-meta", "apps",
      str(app_id), "asset.json",
    )
  )
  assert os.path.exists(meta)

  r = client.delete(f"/api/storage/apps/{app_id}/asset", headers=auth)
  assert r.status_code == 204
  assert not os.path.exists(meta)

  # Re-create the same path with a different type — no stale shadow.
  client.put(
    f"/api/storage/apps/{app_id}/asset",
    data=b"%PDF-1.4",
    headers={**auth, "Content-Type": "application/pdf"},
  )
  r = client.get(f"/api/storage/apps/{app_id}/asset", headers=auth)
  assert r.headers["content-type"].split(";")[0] == "application/pdf"


def test_text_path_still_guesses_without_sidecar(client, auth, owner_token):
  """A plain text write with no explicit binary type keeps serving as
  text — the sidecar must not regress the extension-guess path that
  already worked for extensioned files."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/note.txt",
    json={"content": "hello"},
    headers=auth,
  )
  r = client.get(f"/api/storage/apps/{app_id}/note.txt", headers=auth)
  assert r.text == "hello"
  assert r.headers["content-type"].split(";")[0] == "text/plain"


def test_move_folder_onto_stale_meta_dest_does_not_nest_sidecars(tmp_path):
  """A folder move must REPLACE a stale dest sidecar subtree, not nest into it.

  The data move 409s on an existing destination, but the meta tree is DERIVED
  and can still hold a stale sidecar dir at the dest (a prior occupant whose
  data was deleted but whose meta lingered). `shutil.move` of a dir ONTO an
  existing dir NESTS it (`dst/<src-name>/...`), which buries every moved
  sidecar one level too deep — `read_content_type` at the moved file's path
  then misses, dropping the blob back to extension-guess MIME. The source is
  authoritative, so the move must replace the stale dest (card 085).
  """
  scope = Path("apps") / "7"
  data_dir = str(tmp_path)
  # Source folder `src/` holds one extensionless blob with a custom sidecar.
  storage_io.write_content_type(data_dir, scope, "src/blob", "image/png")
  assert storage_io.read_content_type(data_dir, scope, "src/blob") == "image/png"
  # A STALE sidecar dir already sits at the destination path `dst/` — a former
  # occupant's meta the data side never cleaned up. Its content differs so a
  # merge-without-replace would be detectable too.
  storage_io.write_content_type(data_dir, scope, "dst/old", "text/plain")

  storage_io.move_content_type(data_dir, scope, "src", "dst")

  # The moved blob's sidecar resolves at its NEW path — proving it landed at
  # `dst/blob`, not the nested `dst/src/blob` a bare shutil.move would produce.
  assert storage_io.read_content_type(data_dir, scope, "dst/blob") == "image/png"
  # The stale occupant is gone (source authoritative), and nothing nested.
  meta_root = tmp_path / ".storage-meta" / scope
  assert not (meta_root / "dst" / "src").exists()
  assert not (meta_root / "src").exists()
  assert not (meta_root / "dst" / "old.json").exists()


# --- Per-app quota --------------------------------------------------------


def _put_blob(client, auth, app_id, name, nbytes):
  return client.put(
    f"/api/storage/apps/{app_id}/{name}",
    data=b"a" * nbytes,
    headers={**auth, "Content-Type": "application/octet-stream"},
  )


def test_write_within_quota_succeeds(
  client, auth, owner_token, small_app_cap
):
  """A modest write well under the per-app cap is accepted."""
  app_id = _make_app(client, owner_token)
  r = _put_blob(client, auth, app_id, "small.bin", small_app_cap // 4)
  assert r.status_code == 204


def test_write_over_app_quota_rejected(
  client, auth, owner_token, small_app_cap
):
  """A write that would push the app over its cap is rejected with 413
  and nothing is persisted past the cap."""
  app_id = _make_app(client, owner_token)
  # Fill most of the budget legally.
  assert _put_blob(
    client, auth, app_id, "fill.bin", small_app_cap - 1024
  ).status_code == 204
  # A blob bigger than the remaining 1024 bytes overflows.
  r = _put_blob(client, auth, app_id, "overflow.bin", 4096)
  assert r.status_code == 413
  # The overflow file must not exist (rejected before landing).
  base = os.path.join(get_settings().data_dir, "apps", str(app_id))
  assert not os.path.exists(os.path.join(base, "overflow.bin"))


def test_quota_is_per_app(client, auth, owner_token, small_app_cap):
  """App A's usage does not count against app B's quota."""
  a = _make_app(client, owner_token, name="quota-a")
  b = _make_app(client, owner_token, name="quota-b")
  assert _put_blob(
    client, auth, a, "fill.bin", small_app_cap
  ).status_code == 204
  # App B is untouched — a full-cap write must still succeed.
  r = _put_blob(client, auth, b, "ok.bin", small_app_cap)
  assert r.status_code == 204


def test_delete_frees_quota(client, auth, owner_token, small_app_cap):
  """Removing a file frees its bytes so a subsequent write fits again."""
  app_id = _make_app(client, owner_token)
  assert _put_blob(
    client, auth, app_id, "fill.bin", small_app_cap
  ).status_code == 204
  # At the brink: any further blob overflows.
  assert _put_blob(
    client, auth, app_id, "extra.bin", 4096
  ).status_code == 413
  # Free the big file, then the same write fits.
  client.delete(f"/api/storage/apps/{app_id}/fill.bin", headers=auth)
  r = _put_blob(client, auth, app_id, "extra.bin", 4096)
  assert r.status_code == 204


def test_overwrite_counts_only_delta(
  client, auth, owner_token, small_app_cap
):
  """Overwriting an existing file charges only its size change, not the
  full new size on top of the old — otherwise an app that repeatedly
  rewrites the same key would falsely exhaust its quota."""
  app_id = _make_app(client, owner_token)
  assert _put_blob(
    client, auth, app_id, "doc.bin", small_app_cap
  ).status_code == 204
  # Rewriting the SAME key with the SAME size must stay legal at the brink
  # — the write replaces, it doesn't add on top of the old bytes.
  r = _put_blob(client, auth, app_id, "doc.bin", small_app_cap)
  assert r.status_code == 204
