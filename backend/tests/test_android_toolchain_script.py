"""Trimmed Android toolchain helper contract."""

import importlib.util
import io
from pathlib import Path
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "android_toolchain.py"


def _load():
  spec = importlib.util.spec_from_file_location("android_toolchain_script", SCRIPT)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)
  return module


class _Response:
  def __init__(self, body: bytes, headers: dict[str, str]):
    self.body = body
    self.headers = headers

  def read(self):
    return self.body


def _serve_ranges(monkeypatch, module, archive: bytes, requested: list[str]):
  def urlopen(request):
    if request.get_method() == "HEAD":
      return _Response(b"", {"Accept-Ranges": "bytes", "Content-Length": str(len(archive))})
    start, end = request.get_header("Range").removeprefix("bytes=").split("-")
    requested.append(f"{start}-{end}")
    return _Response(archive[int(start):int(end) + 1], {})

  monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)


def test_remote_archive_fetches_only_requested_members(monkeypatch, tmp_path):
  module = _load()
  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
    archive.writestr("android-36/android.jar", b"jar-bytes")
    archive.writestr("android-36/data/fonts.xml", b"x" * 6_000_000)
  ranges: list[str] = []
  _serve_ranges(monkeypatch, module, buffer.getvalue(), ranges)

  target = tmp_path / "android.jar"
  module._extract_remote("https://example.test/platform.zip", {"android.jar": target})

  assert target.read_bytes() == b"jar-bytes"
  fetched = sum(int(end) - int(start) + 1 for start, end in (r.split("-") for r in ranges))
  assert fetched < len(buffer.getvalue()) / 2


def test_remote_archive_reports_a_missing_member(monkeypatch, tmp_path):
  module = _load()
  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, "w") as archive:
    archive.writestr("top/other", b"")
  _serve_ranges(monkeypatch, module, buffer.getvalue(), [])

  with pytest.raises(SystemExit, match="d8 is missing"):
    module._extract_remote("https://example.test/tools.zip", {"d8": tmp_path / "d8"})


def test_jdk_unpack_keeps_only_what_jlink_needs():
  module = _load()

  assert module._jdk_member_needed("bin/jlink")
  assert module._jdk_member_needed("lib/modules")
  assert module._jdk_member_needed("jmods/java.base.jmod")
  assert module._jdk_member_needed("jmods/java.compiler.jmod")
  assert not module._jdk_member_needed("jmods/java.desktop.jmod")
  assert not module._jdk_member_needed("lib/src.zip")
  assert not module._jdk_member_needed("man/man1/java.1")
  assert set(module.JLINK_MODULES) <= set(module.JMOD_CLOSURE)


def test_build_requires_setup_first(tmp_path):
  module = _load()

  with pytest.raises(SystemExit, match="run `setup` first"):
    module.build(tmp_path, tmp_path / "missing", tmp_path / "test.env", "1", "0.1.0")
