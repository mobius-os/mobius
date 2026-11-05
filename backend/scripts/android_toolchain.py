#!/usr/bin/env python3
"""Install a trimmed Android toolchain and build Gradle-free Java APKs.

  python3 -I android_toolchain.py setup [--prefix DIR]
  python3 -I android_toolchain.py build PROJECT [--version-code N --version-name X]

`setup` installs about 111 MB: a jlink'd Java 21 runtime with javac and keytool,
`android.jar` from platform 36 and the five build tools the build uses. Android
files are read straight out of Google's archives with HTTP range requests, and
the JDK archive is streamed and checksum-verified, so the full kits are never
stored (peak disk about 300 MB). Each part is built in a staging folder and moved
into place only when complete, so an interrupted run is repaired by rerunning it.
It also creates a per-instance test signing key under the projects directory when
none exists, and never replaces an existing one. x86-64 hosts only: Google
publishes no Linux ARM64 build tools.

`build` turns PROJECT (AndroidManifest.xml, res/, src/, optional libs/*.jar) into
PROJECT/build/<name>.apk: aapt2 compile and link, javac, d8, zipalign, apksigner.

Only the Python standard library is used. Download sources are pinned below.
"""

import argparse
import hashlib
import io
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_PREFIX = Path("/opt/apk")
DEFAULT_SIGNING = Path("/data/native-apps/.signing")
TEST_KEY_DIR = "test"
# Google publishes the Linux build tools (aapt2, zipalign) for x86-64 only.
SUPPORTED_MACHINES = frozenset({"x86_64", "amd64"})

ANDROID_REPOSITORY = "https://dl.google.com/android/repository/"
PLATFORM_ZIP = ANDROID_REPOSITORY + "platform-36_r02.zip"
BUILD_TOOLS_ZIP = ANDROID_REPOSITORY + "build-tools_r36_linux.zip"
PLATFORM_DIR = "sdk/platforms/android-36"
BUILD_TOOLS_DIR = "sdk/build-tools/36.0.0"
BUILD_TOOL_FILES = (
  "aapt2", "zipalign", "d8", "apksigner", "core-lambda-stubs.jar",
  "source.properties", "lib/d8.jar", "lib/apksigner.jar",
  "lib64/libc++.so", "lib64/libc++.so.1",
)
ADOPTIUM_JDK = (
  "https://api.adoptium.net/v3/assets/latest/21/hotspot"
  "?architecture=x64&image_type=jdk&os=linux&vendor=eclipse"
)
JLINK_MODULES = (
  "java.base", "jdk.compiler", "java.xml", "java.logging", "jdk.zipfs",
  "java.management", "jdk.crypto.ec", "java.naming", "jdk.unsupported",
)
# jlink reads the jmod of every module in the resolved graph, not only the roots.
JMOD_CLOSURE = JLINK_MODULES + ("java.compiler", "jdk.internal.opt", "java.security.sasl")
MIN_SDK = "24"
TARGET_SDK = "36"
HEADERS = {"User-Agent": "mobius-android-toolchain"}


class _RangeReader(io.RawIOBase):
  """Seekable view of a remote file, fetched by HTTP Range on demand."""

  def __init__(self, url: str) -> None:
    head = urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=HEADERS))
    if head.headers.get("Accept-Ranges") != "bytes":
      raise SystemExit(f"{url} does not support range requests")
    self.url = url
    self.size = int(head.headers["Content-Length"])
    self.pos = 0
    self.fetched = 0

  def readable(self) -> bool:
    return True

  def seekable(self) -> bool:
    return True

  def tell(self) -> int:
    return self.pos

  def seek(self, offset: int, whence: int = 0) -> int:
    self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
    return self.pos

  def readinto(self, buffer) -> int:
    count = min(len(buffer), self.size - self.pos)
    if count <= 0:
      return 0
    request = urllib.request.Request(
      self.url, headers={**HEADERS, "Range": f"bytes={self.pos}-{self.pos + count - 1}"},
    )
    data = urllib.request.urlopen(request).read()
    buffer[:len(data)] = data
    self.pos += len(data)
    self.fetched += len(data)
    return len(data)


def _extract_remote(url: str, members: dict[str, Path]) -> None:
  """Copy archive members (paths below the zip's top folder) to local paths."""
  reader = _RangeReader(url)
  archive = zipfile.ZipFile(io.BufferedReader(reader, buffer_size=1 << 20))
  by_rel = {name.split("/", 1)[1]: name for name in archive.namelist() if "/" in name}
  for rel, target in members.items():
    if rel not in by_rel:
      raise SystemExit(f"{rel} is missing from {url}")
    info = archive.getinfo(by_rel[rel])
    mode = info.external_attr >> 16
    data = archive.read(info)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
      target.unlink()
    if mode & 0o170000 == 0o120000:
      target.symlink_to(data.decode())
    else:
      target.write_bytes(data)
      target.chmod((mode & 0o777) or 0o644)
  name = url.rsplit("/", 1)[1]
  print(f"  {name}: fetched {reader.fetched / 1e6:.0f} of {reader.size / 1e6:.0f} MB")


def _publish(staging: Path, target: Path) -> None:
  """Move a completely built folder into place, replacing an incomplete one."""
  shutil.rmtree(target, ignore_errors=True)
  staging.rename(target)


def _android_complete(prefix: Path) -> bool:
  required = [prefix / PLATFORM_DIR / "android.jar"]
  required += [prefix / BUILD_TOOLS_DIR / rel for rel in BUILD_TOOL_FILES]
  return all(path.exists() for path in required)


def _install_android(prefix: Path) -> None:
  staging = prefix / ".sdk-staging"
  shutil.rmtree(staging, ignore_errors=True)
  try:
    print("Android platform 36: android.jar")
    _extract_remote(PLATFORM_ZIP, {"android.jar": staging / "platforms/android-36/android.jar"})
    print("Android build tools 36.0.0")
    _extract_remote(
      BUILD_TOOLS_ZIP, {rel: staging / "build-tools/36.0.0" / rel for rel in BUILD_TOOL_FILES},
    )
    _publish(staging, prefix / "sdk")
  finally:
    shutil.rmtree(staging, ignore_errors=True)


class _HashingReader(io.RawIOBase):
  def __init__(self, raw) -> None:
    self.raw = raw
    self.digest = hashlib.sha256()

  def readable(self) -> bool:
    return True

  def readinto(self, buffer) -> int:
    data = self.raw.read(len(buffer))
    self.digest.update(data)
    buffer[:len(data)] = data
    return len(data)


def _jdk_member_needed(rel: str) -> bool:
  if rel.startswith("jmods/"):
    return Path(rel).stem in JMOD_CLOSURE
  return rel.startswith(("bin/", "lib/", "conf/", "release")) and rel not in (
    "lib/src.zip", "lib/ct.sym",
  )


def _java_complete(prefix: Path) -> bool:
  return all((prefix / "jre" / "bin" / tool).exists() for tool in ("java", "javac", "keytool"))


def _install_java(prefix: Path) -> None:
  request = urllib.request.Request(ADOPTIUM_JDK, headers=HEADERS)
  package = json.load(urllib.request.urlopen(request))[0]["binary"]["package"]
  print(f"Java: streaming {package['name']} ({package['size'] / 1e6:.0f} MB)")
  scratch = prefix / ".jdk-download"
  runtime = prefix / ".jre-staging"
  shutil.rmtree(scratch, ignore_errors=True)
  shutil.rmtree(runtime, ignore_errors=True)
  try:
    source = _HashingReader(
      urllib.request.urlopen(urllib.request.Request(package["link"], headers=HEADERS)),
    )
    with tarfile.open(fileobj=io.BufferedReader(source, 1 << 20), mode="r|gz") as tar:
      for member in tar:
        rel = member.name.partition("/")[2]
        if rel and _jdk_member_needed(rel):
          member.name = rel
          tar.extract(member, scratch, filter="tar")
    while source.read(1 << 20):
      pass
    if source.digest.hexdigest() != package["checksum"]:
      raise SystemExit("JDK checksum mismatch; nothing installed")
    subprocess.run(
      [
        str(scratch / "bin" / "jlink"), "--add-modules", ",".join(JLINK_MODULES),
        "--strip-java-debug-attributes", "--no-man-pages", "--no-header-files",
        "--compress=zip-9", "--output", str(runtime),
      ],
      check=True,
    )
    _publish(runtime, prefix / "jre")
  finally:
    shutil.rmtree(scratch, ignore_errors=True)
    shutil.rmtree(runtime, ignore_errors=True)


def _ensure_test_key(prefix: Path, signing: Path) -> None:
  """Create the test key and its settings as one folder, or keep the existing one."""
  key_dir = signing / TEST_KEY_DIR
  if key_dir.exists():
    if not ((key_dir / "test.jks").exists() and (key_dir / "signing.env").exists()):
      raise SystemExit(
        f"{key_dir} exists but lacks test.jks or signing.env; it may hold a signing "
        "identity, so it is left untouched. Restore it, or move it aside and rerun setup.",
      )
    return
  signing.mkdir(parents=True, exist_ok=True)
  signing.chmod(0o700)
  staging = signing / f".{TEST_KEY_DIR}-staging"
  shutil.rmtree(staging, ignore_errors=True)
  staging.mkdir(mode=0o700)
  try:
    password = secrets.token_urlsafe(18)
    subprocess.run(
      [
        str(prefix / "jre" / "bin" / "keytool"), "-genkeypair",
        "-keystore", str(staging / "test.jks"), "-storetype", "PKCS12", "-alias", "test",
        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
        "-dname", "CN=Mobius test key",
        "-storepass:env", "ANDROID_KS_PASS", "-keypass:env", "ANDROID_KS_PASS",
      ],
      check=True, env={**os.environ, "ANDROID_KS_PASS": password},
      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    (staging / "test.jks").chmod(0o600)
    settings = staging / "signing.env"
    settings.write_text(
      f"KS={key_dir / 'test.jks'}\nKS_ALIAS=test\nKS_PASS={password}\n", encoding="utf-8",
    )
    settings.chmod(0o600)
    staging.rename(key_dir)
  finally:
    shutil.rmtree(staging, ignore_errors=True)
  print(f"Created a test signing key in {key_dir}")


def _require_supported_host() -> None:
  machine = platform.machine().lower()
  if machine not in SUPPORTED_MACHINES:
    raise SystemExit(
      f"this host is {machine}; the Android build tools are published for Linux x86-64 "
      "only, so this toolchain cannot run here. Nothing was downloaded.",
    )


def setup(prefix: Path, signing: Path) -> None:
  _require_supported_host()
  try:
    prefix.mkdir(parents=True, exist_ok=True)
  except PermissionError:
    raise SystemExit(
      f"cannot create {prefix}; run `sudo mkdir -p {prefix} && sudo chown "
      f"$(id -u):$(id -g) {prefix}` or pass --prefix",
    ) from None
  # Java first: its temporary unpack is the disk peak, so the SDK is not there yet.
  if not _java_complete(prefix):
    _install_java(prefix)
  if not _android_complete(prefix):
    _install_android(prefix)
  _ensure_test_key(prefix, signing)
  size = sum(p.stat().st_size for p in prefix.rglob("*") if p.is_file() and not p.is_symlink())
  print(f"Toolchain ready in {prefix} ({size / 1e6:.0f} MB)")


def _read_env(path: Path) -> dict[str, str]:
  values = {}
  for line in path.read_text(encoding="utf-8").splitlines():
    key, sep, value = line.partition("=")
    if sep and not key.startswith("#"):
      values[key.strip()] = value.strip()
  return values


def build(project: Path, prefix: Path, signing_env: Path, code: str, name: str) -> Path:
  java_bin = prefix / "jre" / "bin"
  tools = prefix / BUILD_TOOLS_DIR
  android_jar = prefix / PLATFORM_DIR / "android.jar"
  if not (_java_complete(prefix) and _android_complete(prefix)):
    raise SystemExit(f"no toolchain in {prefix}; run `setup` first")
  if not signing_env.exists():
    raise SystemExit(f"no signing settings at {signing_env}; run `setup` first")
  key = _read_env(signing_env)
  out = project / "build"
  shutil.rmtree(out, ignore_errors=True)
  (out / "classes").mkdir(parents=True)
  (out / "dex").mkdir()
  env = {
    **os.environ, "JAVA_HOME": str(prefix / "jre"),
    "PATH": f"{java_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    "ANDROID_KS_PASS": key["KS_PASS"], "ANDROID_KEY_PASS": key.get("KEY_PASS", key["KS_PASS"]),
  }

  def run(*command: object) -> None:
    subprocess.run([str(part) for part in command], check=True, env=env)

  link_inputs = []
  if (project / "res").is_dir():
    run(tools / "aapt2", "compile", "--dir", project / "res", "-o", out / "res.zip")
    link_inputs.append(out / "res.zip")
  run(
    tools / "aapt2", "link", "-o", out / "base.apk", "-I", android_jar,
    "--manifest", project / "AndroidManifest.xml", "--java", out / "gen",
    "--min-sdk-version", MIN_SDK, "--target-sdk-version", TARGET_SDK,
    "--version-code", code, "--version-name", name, *link_inputs,
  )
  sources = sorted((project / "src").rglob("*.java")) + sorted((out / "gen").rglob("*.java"))
  libraries = sorted((project / "libs").glob("*.jar")) if (project / "libs").is_dir() else []
  run(
    java_bin / "javac", "-J-Xmx256m", "-source", "8", "-target", "8", "-encoding", "UTF-8",
    "-Xlint:-options", "-bootclasspath", f"{android_jar}{os.pathsep}{tools / 'core-lambda-stubs.jar'}",
    "-classpath", os.pathsep.join([str(android_jar), *map(str, libraries)]),
    "-d", out / "classes", *sources,
  )
  run(
    tools / "d8", "--release", "--min-api", MIN_SDK, "--lib", android_jar,
    "--output", out / "dex", *sorted((out / "classes").rglob("*.class")), *libraries,
  )
  with zipfile.ZipFile(out / "base.apk", "a", zipfile.ZIP_DEFLATED) as apk:
    for dex in sorted((out / "dex").glob("classes*.dex")):
      apk.write(dex, dex.name)
  run(tools / "zipalign", "-p", "-f", "4", out / "base.apk", out / "aligned.apk")
  apk_path = out / f"{project.resolve().name}.apk"
  run(
    tools / "apksigner", "sign", "--ks", key["KS"], "--ks-key-alias", key["KS_ALIAS"],
    "--ks-pass", "env:ANDROID_KS_PASS", "--key-pass", "env:ANDROID_KEY_PASS",
    "--out", apk_path, out / "aligned.apk",
  )
  print(f"Built {apk_path} ({apk_path.stat().st_size / 1e3:.0f} KB)")
  return apk_path


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX, help="toolchain folder")
  commands = parser.add_subparsers(dest="command", required=True)
  setup_parser = commands.add_parser("setup", help="install the toolchain and a test key")
  setup_parser.add_argument("--signing-dir", type=Path, default=DEFAULT_SIGNING)
  build_parser = commands.add_parser("build", help="build and sign PROJECT's APK")
  build_parser.add_argument("project", type=Path)
  build_parser.add_argument("--version-code", default="1")
  build_parser.add_argument("--version-name", default="0.1.0")
  build_parser.add_argument(
    "--signing-env", type=Path, default=DEFAULT_SIGNING / TEST_KEY_DIR / "signing.env",
    help="KS, KS_ALIAS, KS_PASS[, KEY_PASS] file",
  )
  args = parser.parse_args()
  if args.command == "setup":
    setup(args.prefix, args.signing_dir)
  else:
    build(args.project, args.prefix, args.signing_env, args.version_code, args.version_name)


if __name__ == "__main__":
  sys.exit(main())
