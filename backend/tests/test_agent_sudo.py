from __future__ import annotations

import os
import subprocess
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_sudo.sh"


def _configure(tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    [
      "/bin/sh", "-c",
      '. "$1"; configure_agent_sudo "$2" "$3" /bin/true',
      "test-agent-sudo", str(_SCRIPT), mode, str(tmp_path),
    ],
    text=True,
    capture_output=True,
    check=False,
  )


def test_sudo_disabled_removes_every_agent_rule(tmp_path):
  (tmp_path / "mobius-apt").write_text("legacy")
  (tmp_path / "mobius-agent").write_text("enabled")
  result = _configure(tmp_path, "0")
  assert result.returncode == 0
  assert list(tmp_path.iterdir()) == []


def test_sudo_enabled_is_full_root_and_not_misleading_apt_scope(tmp_path):
  result = _configure(tmp_path, "1")
  assert result.returncode == 0
  rule = tmp_path / "mobius-agent"
  assert rule.read_text() == "mobius ALL=(root) NOPASSWD: ALL\n"
  assert os.stat(rule).st_mode & 0o777 == 0o440
  assert not (tmp_path / "mobius-apt").exists()


def test_sudo_mode_fails_closed_on_unknown_value(tmp_path):
  result = _configure(tmp_path, "yes")
  assert result.returncode == 64
  assert "must be 0 or 1" in result.stderr
  assert list(tmp_path.iterdir()) == []


def test_recovery_mode_precedes_data_initialization_and_sudo_configuration():
  entrypoint = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text()
  recovery_exec = entrypoint.index("exec python3 -I /app/recovery-target/targetd.py")
  sudo_config = entrypoint.index("configure_agent_sudo")
  data_init = entrypoint.index("mkdir -p /data/db")
  assert recovery_exec < sudo_config < data_init
