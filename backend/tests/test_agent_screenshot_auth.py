"""Regression coverage for authenticated screenshot readiness checks."""

from pathlib import Path
import os
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent-screenshot.sh"


def _fake_browser(tmp_path: Path) -> tuple[Path, Path]:
  marker = tmp_path / "screenshot-called"
  browser = tmp_path / "agent-browser"
  browser.write_text(
    "#!/bin/sh\n"
    "case \"$1\" in\n"
    "  eval)\n"
    "    if [ \"$2\" = \"--stdin\" ]; then cat >/dev/null; exit 0; fi\n"
    "    printf '%s\\n' \"${FAKE_AUTH_OK:-false}\"\n"
    "    ;;\n"
    "  screenshot)\n"
    "    : > \"$2\"\n"
    "    : > \"$FAKE_SCREENSHOT_MARKER\"\n"
    "    ;;\n"
    "  *) exit 0 ;;\n"
    "esac\n",
    encoding="utf-8",
  )
  browser.chmod(0o755)
  return browser, marker


def _run_helper(tmp_path: Path, *, auth_ok: bool) -> tuple[subprocess.CompletedProcess, Path, Path]:
  _, marker = _fake_browser(tmp_path)
  output = tmp_path / "shot.png"
  env = {
    **os.environ,
    "PATH": f"{tmp_path}:{os.environ['PATH']}",
    "AGENT_TOKEN": "test-token",
    "API_BASE_URL": "http://mobius.test",
    "VIEWPORT_WIDTH": "412",
    "VIEWPORT_HEIGHT": "915",
    "FAKE_AUTH_OK": "true" if auth_ok else "false",
    "FAKE_SCREENSHOT_MARKER": str(marker),
  }
  result = subprocess.run(
    ["bash", str(SCRIPT), "/chat/example", str(output)],
    env=env,
    text=True,
    capture_output=True,
    check=False,
  )
  return result, output, marker


def test_helper_refuses_to_capture_login_wall(tmp_path: Path):
  result, output, marker = _run_helper(tmp_path, auth_ok=False)

  assert result.returncode != 0
  assert "authentication failed" in result.stderr
  assert not output.exists()
  assert not marker.exists()


def test_helper_captures_after_authentication_is_confirmed(tmp_path: Path):
  result, output, marker = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
