"""Valid storage names must leave room for internal atomic/MIME filenames."""

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from app import storage_io
from app.config import get_settings
from test_app_fixtures import create_local_app


def _filename(size: int, multibyte: bool) -> str:
  name = "界" * (size // 3) + "a" * (size % 3) if multibyte else "a" * size
  assert len(os.fsencode(name)) == size
  return name


@pytest.mark.parametrize("size", [241, 242, 250, 251, 255])
@pytest.mark.parametrize("multibyte", [False, True], ids=["ascii", "utf8"])
def test_atomic_write_valid_byte_boundary_overwrites_without_temps(
  tmp_path, monkeypatch, size, multibyte,
):
  target = tmp_path / _filename(size, multibyte)
  replace = storage_io.os.replace
  observed = []

  def inspect_replace(src, dst):
    temp = Path(src)
    assert temp.parent == target.parent
    assert len(os.fsencode(temp.name)) <= 255
    assert storage_io.is_atomic_write_temp_name(temp.name)
    observed.append(temp.name)
    replace(src, dst)

  monkeypatch.setattr(storage_io.os, "replace", inspect_replace)
  for body in ("first", b"second\x00\xff"):
    storage_io.atomic_write(target, body)
    assert target.read_bytes() == (body.encode() if isinstance(body, str) else body)
    assert list(tmp_path.iterdir()) == [target]
  assert len(observed) == 2
  assert observed[0] != observed[1]


def test_atomic_write_long_name_cleans_temp_after_failed_replace(tmp_path, monkeypatch):
  target = tmp_path / _filename(255, True)
  target.write_bytes(b"original")

  def fail_replace(src, dst):
    assert storage_io.is_atomic_write_temp_name(Path(src).name)
    raise OSError(errno.EIO, "synthetic replacement failure")

  monkeypatch.setattr(storage_io.os, "replace", fail_replace)
  with pytest.raises(OSError) as exc:
    storage_io.atomic_write(target, b"replacement")
  assert exc.value.errno == errno.EIO
  assert target.read_bytes() == b"original"
  assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("size", [241, 242, 250, 251, 255])
@pytest.mark.parametrize("multibyte", [False, True], ids=["ascii", "utf8"])
def test_storage_put_long_name_round_trips_bytes_and_mime(
  client, auth, size, multibyte,
):
  app_id = create_local_app(client, auth, name="long-name-storage")["id"]
  name = _filename(size, multibyte)
  folder = f"long-names-{size}-{'utf8' if multibyte else 'ascii'}"
  rel = f"{folder}/{name}"
  url = f"/api/storage/apps/{app_id}/{rel}"
  for body, mime in ((b"first\x00", "application/x-first"),
                     (b"second\xff", "application/x-second")):
    response = client.put(url, content=body, headers={**auth, "Content-Type": mime})
    assert response.status_code == 204, response.text
    response = client.get(url, headers=auth)
    assert response.status_code == 200, response.text
    assert response.content == body
    assert response.headers["content-type"] == mime
  root = Path(get_settings().data_dir)
  assert [p.name for p in (root / "apps" / str(app_id) / folder).iterdir()] == [name]
  meta_files = list((root / ".storage-meta/apps" / str(app_id) / folder).iterdir())
  assert len(meta_files) == 1
  assert not any(storage_io.is_atomic_write_temp_name(p.name) for p in meta_files)


@pytest.mark.parametrize("size", [6, 250])
@pytest.mark.parametrize("multibyte", [False, True], ids=["ascii", "utf8"])
def test_mime_sidecars_keep_preexisting_owner_metadata(tmp_path, size, multibyte):
  scope = Path("apps/7")
  rel = "folder/" + _filename(size, multibyte)
  meta = tmp_path / ".storage-meta" / scope / (rel + ".json")
  meta.parent.mkdir(parents=True)
  meta.write_text(json.dumps({"content_type": "application/x-legacy"}))
  assert storage_io.read_content_type(str(tmp_path), scope, rel) == "application/x-legacy"
  storage_io.write_content_type(str(tmp_path), scope, rel, "application/x-updated")
  assert json.loads(meta.read_text())["content_type"] == "application/x-updated"
  storage_io.delete_content_type(str(tmp_path), scope, rel)
  assert not meta.exists()


def test_long_mime_sidecars_do_not_collide_and_follow_moves_and_deletes(tmp_path):
  data_dir = str(tmp_path)
  scope = Path("shared")
  first = _filename(255, False)
  second = first[:-1] + "b"
  # An ordinary owner filename matching the bounded sidecar's stem must
  # retain independent MIME (legacy sidecars always end in '.json').
  short = hashlib.sha256(first.encode()).hexdigest() + ".mime"
  for rel, mime in ((f"source/{first}", "application/x-first"),
                    (f"source/{second}", "application/x-second"),
                    (f"source/{short}", "application/x-short")):
    storage_io.write_content_type(data_dir, scope, rel, mime)
    assert storage_io.read_content_type(data_dir, scope, rel) == mime

  storage_io.move_content_type(data_dir, scope, "source", "moved")
  assert storage_io.read_content_type(data_dir, scope, f"source/{first}") is None
  assert storage_io.read_content_type(data_dir, scope, f"moved/{first}") == "application/x-first"
  assert storage_io.read_content_type(data_dir, scope, f"moved/{short}") == "application/x-short"
  storage_io.move_content_type(data_dir, scope, f"moved/{first}", "moved/short")
  assert storage_io.read_content_type(data_dir, scope, f"moved/{first}") is None
  assert storage_io.read_content_type(data_dir, scope, "moved/short") == "application/x-first"
  storage_io.move_content_type(data_dir, scope, "moved/short", f"moved/{first}")
  assert storage_io.read_content_type(data_dir, scope, f"moved/{first}") == "application/x-first"
  storage_io.write_content_type(data_dir, scope, f"moved/{first}", None)
  assert storage_io.read_content_type(data_dir, scope, f"moved/{first}") is None
  assert storage_io.read_content_type(data_dir, scope, f"moved/{second}") == "application/x-second"
  storage_io.delete_content_type(data_dir, scope, f"moved/{second}")
  assert storage_io.read_content_type(data_dir, scope, f"moved/{second}") is None
  storage_io.write_content_type(data_dir, scope, f"moved/{first}", "application/x-final")
  storage_io.delete_content_type_tree(data_dir, scope, "moved")
  assert storage_io.read_content_type(data_dir, scope, f"moved/{first}") is None


def test_long_mime_sidecar_does_not_alias_an_owner_folder_or_file_move(tmp_path):
  data_dir = str(tmp_path)
  scope = Path("apps/7")
  long_name = _filename(255, False)
  ordinary = hashlib.sha256(long_name.encode()).hexdigest() + ".mime"
  storage_io.write_content_type(data_dir, scope, long_name, "application/x-long")
  storage_io.write_content_type(data_dir, scope, f"{ordinary}/child", "application/x-child")
  assert storage_io.read_content_type(data_dir, scope, f"{ordinary}/child") == "application/x-child"
  storage_io.move_content_type(data_dir, scope, ordinary, "renamed-folder")
  assert storage_io.read_content_type(data_dir, scope, "renamed-folder/child") == "application/x-child"
  assert storage_io.read_content_type(data_dir, scope, long_name) == "application/x-long"
  storage_io.delete_content_type_tree(data_dir, scope, "renamed-folder")
  storage_io.move_content_type(data_dir, scope, ordinary, "renamed-file")
  assert storage_io.read_content_type(data_dir, scope, long_name) == "application/x-long"
