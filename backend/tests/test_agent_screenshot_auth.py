"""Regression coverage for authenticated screenshot readiness checks."""

from pathlib import Path
import os
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent-screenshot.sh"
PREVIEW_APP = Path(__file__).parents[1] / "scripts" / "preview_app.sh"


def _fake_browser(tmp_path: Path) -> tuple[Path, Path]:
  marker = tmp_path / "screenshot-called"
  browser = tmp_path / "agent-browser"
  browser.write_text(
    "#!/bin/sh\n"
    "printf '%s\\n' \"$*\" >> \"$FAKE_BROWSER_LOG\"\n"
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


def _run_helper(
  tmp_path: Path, *, auth_ok: bool, route: str = "/chat/example",
  viewport_width: int = 412, viewport_height: int = 915,
  content_only: bool = False,
) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
  _, marker = _fake_browser(tmp_path)
  output = tmp_path / "shot.png"
  browser_log = tmp_path / "browser.log"
  env = {
    **os.environ,
    "PATH": f"{tmp_path}:{os.environ['PATH']}",
    "AGENT_TOKEN": "test-token",
    "API_BASE_URL": "http://mobius.test",
    "VIEWPORT_WIDTH": str(viewport_width),
    "VIEWPORT_HEIGHT": str(viewport_height),
    "FAKE_AUTH_OK": "true" if auth_ok else "false",
    "FAKE_BROWSER_LOG": str(browser_log),
    "FAKE_SCREENSHOT_MARKER": str(marker),
  }
  args = ["bash", str(SCRIPT)]
  if content_only:
    args.append("--content-only")
  args.extend([route, str(output)])
  result = subprocess.run(
    args,
    env=env,
    text=True,
    capture_output=True,
    check=False,
  )
  return result, output, marker, browser_log


def test_helper_refuses_to_capture_when_protected_request_rejects_token(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=False)

  assert result.returncode != 0
  assert "authentication failed" in result.stderr
  assert "/api/chats" in browser_log.read_text(encoding="utf-8")
  assert not output.exists()
  assert not marker.exists()


def test_helper_captures_after_authentication_is_confirmed(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()

  commands = browser_log.read_text(encoding="utf-8").splitlines()
  settle_index = commands.index("wait 300")
  auth_index = next(i for i, command in enumerate(commands) if "/api/chats" in command)
  screenshot_index = next(i for i, command in enumerate(commands) if command.startswith("screenshot "))
  assert settle_index < auth_index < screenshot_index
  assert all("test-token" not in command for command in commands)


def test_app_capture_waits_for_frame_mounted_state(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path, auth_ok=True, route="/app/42",
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()

  commands = browser_log.read_text(encoding="utf-8").splitlines()
  drawer_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("wait --fn ")
    and ".drawer-overlay--blocking" in command
  )
  readiness_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("wait --fn ")
    and 'iframe[data-app-id="42"]' in command
    and ".canvas-loading" in command
  )
  screenshot_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("screenshot ")
  )
  assert drawer_index < readiness_index < screenshot_index


def test_desktop_capture_does_not_wait_for_modal_drawer(tmp_path: Path):
  _, _, _, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    route="/chat/example",
    viewport_width=1200,
    viewport_height=800,
  )

  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert not any(
    command.startswith("wait --fn ")
    and ".drawer-overlay--blocking" in command
    for command in commands
  )


def test_non_app_capture_skips_frame_readiness_wait(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path, auth_ok=True, route="/chat/example",
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert not any(
    command.startswith("wait --fn ")
    and "iframe[data-app-id=" in command
    for command in commands
  )


def test_content_only_mode_removes_product_overlays_before_capture(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    route="/app/42",
    content_only=True,
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  overlay_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("eval ")
    and ".wt__overlay, #install-backdrop" in command
  )
  readiness_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("wait --fn ")
    and 'iframe[data-app-id="42"]' in command
  )
  screenshot_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("screenshot ")
  )
  assert overlay_index < readiness_index < screenshot_index


def test_default_mode_preserves_product_overlays(tmp_path: Path):
  _, _, _, browser_log = _run_helper(tmp_path, auth_ok=True)

  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert not any(
    command.startswith("eval ")
    and ".wt__overlay, #install-backdrop" in command
    for command in commands
  )


def test_app_preview_requests_ephemeral_content_only_mode():
  source = PREVIEW_APP.read_text(encoding="utf-8")

  assert 'agent-screenshot.sh" --content-only "/app/${APP_ID}"' in source
