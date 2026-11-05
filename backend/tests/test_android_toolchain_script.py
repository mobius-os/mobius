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


def test_setup_refuses_arm64_before_downloading(monkeypatch, tmp_path):
  module = _load()
  monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")

  def no_network(*_args, **_kwargs):
    raise AssertionError("setup downloaded on an unsupported host")

  monkeypatch.setattr(module.urllib.request, "urlopen", no_network)

  with pytest.raises(SystemExit, match="x86-64 only"):
    module.setup(tmp_path / "apk", tmp_path / "signing")
  assert not (tmp_path / "apk").exists()


def _fake_extract(fail_on: str | None = None):
  def extract(url, members):
    for rel, target in members.items():
      if rel == fail_on:
        raise ConnectionResetError("connection reset")
      target.parent.mkdir(parents=True, exist_ok=True)
      target.write_bytes(b"tool")

  return extract


def test_interrupted_android_install_is_redone_on_rerun(monkeypatch, tmp_path):
  module = _load()
  monkeypatch.setattr(module, "_extract_remote", _fake_extract(fail_on="apksigner"))

  with pytest.raises(ConnectionResetError):
    module._install_android(tmp_path)
  assert not module._android_complete(tmp_path)
  assert not (tmp_path / "sdk").exists()
  assert not (tmp_path / ".sdk-staging").exists()

  monkeypatch.setattr(module, "_extract_remote", _fake_extract())
  module._install_android(tmp_path)
  assert module._android_complete(tmp_path)


def test_partial_sdk_is_not_mistaken_for_a_complete_install(tmp_path):
  module = _load()
  d8 = tmp_path / module.BUILD_TOOLS_DIR / "d8"
  d8.parent.mkdir(parents=True)
  d8.write_bytes(b"tool")

  assert not module._android_complete(tmp_path)


def _fake_keytool(interrupt: bool = False):
  def run(command, **_kwargs):
    Path(command[command.index("-keystore") + 1]).write_bytes(b"keystore")
    if interrupt:
      raise KeyboardInterrupt

  return run


def test_interrupted_key_creation_leaves_no_half_key(monkeypatch, tmp_path):
  module = _load()
  signing = tmp_path / "signing"
  monkeypatch.setattr(module.subprocess, "run", _fake_keytool(interrupt=True))

  with pytest.raises(KeyboardInterrupt):
    module._ensure_test_key(tmp_path, signing)
  assert sorted(p.name for p in signing.iterdir()) == []

  monkeypatch.setattr(module.subprocess, "run", _fake_keytool())
  module._ensure_test_key(tmp_path, signing)
  settings = module._read_env(signing / "test" / "signing.env")
  assert settings["KS"] == str(signing / "test" / "test.jks")
  assert (signing / "test" / "test.jks").exists()


def test_existing_signing_identity_is_never_replaced(monkeypatch, tmp_path):
  module = _load()
  key_dir = tmp_path / "signing" / "test"
  key_dir.mkdir(parents=True)
  (key_dir / "test.jks").write_bytes(b"owner key")

  def keytool(*_args, **_kwargs):
    raise AssertionError("an existing key was regenerated")

  monkeypatch.setattr(module.subprocess, "run", keytool)

  with pytest.raises(SystemExit, match="left untouched"):
    module._ensure_test_key(tmp_path, tmp_path / "signing")
  assert (key_dir / "test.jks").read_bytes() == b"owner key"

  (key_dir / "signing.env").write_text("KS=x\nKS_ALIAS=test\nKS_PASS=p\n")
  module._ensure_test_key(tmp_path, tmp_path / "signing")
  assert (key_dir / "test.jks").read_bytes() == b"owner key"
