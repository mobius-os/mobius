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


def test_sudo_defaults_to_full_root_when_mode_is_unset(tmp_path):
  result = _configure(tmp_path, "")
  assert result.returncode == 0
  assert (tmp_path / "mobius-agent").read_text() == (
    "mobius ALL=(root) NOPASSWD: ALL\n"
  )


def test_sudo_mode_fails_closed_on_unknown_value(tmp_path):
  result = _configure(tmp_path, "yes")
  assert result.returncode == 64
  assert "must be 0 or 1" in result.stderr
  assert list(tmp_path.iterdir()) == []
