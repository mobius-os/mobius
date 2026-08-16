import os

from app.app_footprint import app_footprint_bytes


def test_app_footprint_counts_owned_lanes_without_following_symlinks(tmp_path):
  source = tmp_path / "apps" / "catalog-source"
  storage = tmp_path / "apps" / "7"
  secrets = tmp_path / "app-secrets" / "7"
  compiled = tmp_path / "compiled" / "7.js"
  outside = tmp_path / "outside"
  for directory in (source, storage, secrets, compiled.parent, outside):
    directory.mkdir(parents=True, exist_ok=True)
  (source / "index.jsx").write_bytes(b"x" * 8_192)
  (storage / "data.json").write_bytes(b"d" * 8_192)
  (secrets / "config").write_bytes(b"s" * 8_192)
  compiled.write_bytes(b"c" * 8_192)
  (outside / "large.bin").write_bytes(b"o" * 1_000_000)
  os.symlink(outside, source / "external")

  measured = app_footprint_bytes(tmp_path, {
    "id": 7,
    "source_dir": str(source),
    "compiled_path": str(compiled),
  })

  assert measured >= 4 * 8_192
  assert measured < 1_000_000


def test_app_footprint_rejects_paths_outside_owned_lanes(tmp_path):
  outside = tmp_path / "outside"
  outside.mkdir()
  (outside / "large.bin").write_bytes(b"o" * 1_000_000)

  assert app_footprint_bytes(tmp_path, {
    "id": 9,
    "source_dir": str(outside),
    "compiled_path": str(outside / "large.bin"),
  }) == 0
