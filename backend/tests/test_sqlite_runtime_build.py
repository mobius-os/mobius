"""Downloaded SQLite source must be authenticated before it is executed."""
import os
import subprocess
from pathlib import Path


BUILD = Path(__file__).resolve().parents[1] / "sqlite_runtime" / "build.sh"


def test_sqlite_build_rejects_unverified_source_before_unpacking(tmp_path):
  commands = tmp_path / "bin"
  commands.mkdir()
  download = commands / "curl"
  download.write_text("#!/bin/sh\nprintf 'unverified bytes' > source.zip\n")
  download.chmod(0o755)
  unpack = commands / "unzip"
  marker = tmp_path / "unpacked"
  unpack.write_text('#!/bin/sh\ntouch "$UNPACK_MARKER"\n')
  unpack.chmod(0o755)
  staging = tmp_path / "staging"
  env = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
         "TMPDIR": str(tmp_path), "UNPACK_MARKER": str(marker)}
  result = subprocess.run(["bash", str(BUILD), str(staging)], env=env,
                          capture_output=True, text=True)
  assert result.returncode != 0
  assert "SQLite source checksum mismatch" in result.stderr
  assert not marker.exists()
  assert not staging.exists()
  assert sorted(path.name for path in tmp_path.iterdir()) == ["bin"]


def test_sqlite_build_script_has_valid_bash_syntax():
  subprocess.run(["bash", "-n", str(BUILD)], check=True)
