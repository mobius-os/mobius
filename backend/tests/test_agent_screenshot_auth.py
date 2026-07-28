"""Regression coverage for authenticated screenshot readiness checks."""

from pathlib import Path
import os
import shutil
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent-screenshot.sh"
PREVIEW_APP = Path(__file__).parents[1] / "scripts" / "preview_app.sh"
SHELL = Path(__file__).parents[2] / "frontend" / "src" / "components" / "Shell" / "Shell.jsx"
STANDALONE = Path(__file__).parents[1] / "app" / "routes" / "standalone.py"
SHELL_ENTRY = "index-test-fixture.js"


def _fixture_script(tmp_path: Path) -> Path:
  """Copy the helper beside a minimal built-shell fixture.

  The backend CI job intentionally does not build the frontend. Keeping the
  fixture under tmp_path makes the helper's dist dependency explicit without
  mutating the checkout or weakening its production freshness check.
  """
  root = tmp_path / "fixture"
  script = root / "backend" / "scripts" / SCRIPT.name
  script.parent.mkdir(parents=True)
  shutil.copy2(SCRIPT, script)
  dist = root / "frontend" / "dist"
  dist.mkdir(parents=True)
  (dist / "index.html").write_text(
    f'<script type="module" src="/assets/{SHELL_ENTRY}"></script>',
    encoding="utf-8",
  )
  return script


def _fake_browser(tmp_path: Path) -> tuple[Path, Path]:
  marker = tmp_path / "screenshot-called"
  browser = tmp_path / "agent-browser"
  browser.write_text(
    "#!/bin/sh\n"
    "printf '%s\\n' \"$*\" >> \"$FAKE_BROWSER_LOG\"\n"
    "case \"$1\" in\n"
    "  eval)\n"
    "    if [ \"$2\" = \"--stdin\" ]; then cat >/dev/null; exit 0; fi\n"
    "    case \"$2\" in\n"
    "      *src.split*) printf '%s\\n' \"${FAKE_LOADED_ASSET:-none}\" ;;\n"
    "      *serviceWorker*) printf '%s\\n' true ;;\n"
    "      *) printf '%s\\n' \"${FAKE_AUTH_OK:-false}\" ;;\n"
    "    esac\n"
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
  viewport_width: int | float | str = 412,
  viewport_height: int | float | str = 915,
  content_only: bool = False,
  preserve_cache: bool = False,
  loaded_asset: str | None = None,
) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
  _, marker = _fake_browser(tmp_path)
  script = _fixture_script(tmp_path)
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
    "FAKE_LOADED_ASSET": loaded_asset or SHELL_ENTRY,
    "FAKE_BROWSER_LOG": str(browser_log),
    "FAKE_SCREENSHOT_MARKER": str(marker),
  }
  args = ["bash", str(script)]
  if content_only:
    args.append("--content-only")
  if preserve_cache:
    args.append("--preserve-cache")
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


def test_default_capture_detaches_pwa_state_and_verifies_current_shell(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  reset_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("eval ") and "serviceWorker" in command
  )
  target_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("open http://mobius.test/chat/example?__mobius_capture=")
  )
  build_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("eval ") and "src.split" in command
  )
  screenshot_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("screenshot ")
  )
  detach_index = commands.index("open about:blank")
  assert reset_index < detach_index < target_index < build_index < screenshot_index


def test_stale_shell_fails_instead_of_capturing_misleading_evidence(tmp_path: Path):
  result, output, marker, _ = _run_helper(
    tmp_path,
    auth_ok=True,
    loaded_asset="index-stale.js",
  )

  assert result.returncode != 0
  assert "stale shell loaded" in result.stderr
  assert not output.exists()
  assert not marker.exists()


def test_preserve_cache_mode_is_explicit_and_skips_freshness_reset(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    preserve_cache=True,
    loaded_asset="index-stale.js",
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert "open http://mobius.test/chat/example" in commands
  assert not any("serviceWorker" in command for command in commands)
  assert not any("src.split" in command for command in commands)


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


def test_fractional_shell_viewport_is_rounded_for_agent_browser(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    viewport_width=1680,
    viewport_height=956.6666870117188,
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  assert "set viewport 1680 957" in browser_log.read_text(
    encoding="utf-8",
  ).splitlines()


def test_cold_capture_opens_browser_before_configuring_viewport(tmp_path: Path):
  result, _, _, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert commands.index("open http://mobius.test/") < commands.index(
    "set viewport 412 915",
  )


def test_invalid_manual_viewport_fails_before_browser_launch(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    viewport_width="nan",
  )

  assert result.returncode != 0
  assert "must be positive numbers" in result.stderr
  assert not output.exists()
  assert not marker.exists()
  assert not browser_log.exists()


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


def test_content_only_mode_is_set_before_target_navigation(tmp_path: Path):
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
  visual_mode_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("eval ")
    and "sessionStorage.setItem('mobius:visual-content-only', '1')" in command
  )
  target_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("open http://mobius.test/app/42?__mobius_capture=")
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
  assert visual_mode_index < target_index < readiness_index < screenshot_index


def test_default_mode_clears_prior_visual_mode_before_navigation(tmp_path: Path):
  _, _, _, browser_log = _run_helper(tmp_path, auth_ok=True)

  commands = browser_log.read_text(encoding="utf-8").splitlines()
  clear_index = next(
    i for i, command in enumerate(commands)
    if "sessionStorage.removeItem('mobius:visual-content-only')" in command
  )
  target_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("open http://mobius.test/chat/example?__mobius_capture=")
  )
  assert clear_index < target_index


def test_content_mode_suppresses_modals_without_dom_surgery():
  helper = SCRIPT.read_text(encoding="utf-8")
  shell = SHELL.read_text(encoding="utf-8")
  standalone = STANDALONE.read_text(encoding="utf-8")

  assert "querySelectorAll('.wt__overlay, #install-backdrop')" not in helper
  assert "const showWalkthrough = !visualContentOnly" in shell
  assert "if (visualContentOnly) return;" in standalone


def test_app_preview_requests_ephemeral_content_only_mode():
  source = PREVIEW_APP.read_text(encoding="utf-8")

  assert 'ROUTE="/app/${APP_ID}"' in source
  assert 'agent-screenshot.sh" --content-only "${ROUTE}"' in source


def test_standalone_app_preview_keeps_numeric_id_as_input():
  source = PREVIEW_APP.read_text(encoding="utf-8")

  assert "preview_app.sh [--standalone] <app_id>" in source
  assert 'f"{base}/api/apps/{app_id}"' in source
  assert 'ROUTE="/apps/${SLUG}/"' in source
  assert "app_id must be numeric" in source


def test_standalone_preview_resolves_slug_without_exposing_it_to_caller(
  tmp_path: Path,
):
  preview = tmp_path / "preview_app.sh"
  shutil.copy2(PREVIEW_APP, preview)
  fake_python = tmp_path / "python3"
  fake_python.write_text(
    "#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' 'duplicate-name-2'\n",
    encoding="utf-8",
  )
  fake_python.chmod(0o755)
  fake_screenshot = tmp_path / "agent-screenshot.sh"
  fake_screenshot.write_text(
    "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$PREVIEW_LOG\"\n",
    encoding="utf-8",
  )
  fake_screenshot.chmod(0o755)
  log = tmp_path / "preview.log"

  result = subprocess.run(
    [
      "bash", str(preview), "--standalone", "42",
      str(tmp_path / "standalone.png"),
    ],
    env={
      **os.environ,
      "PATH": f"{tmp_path}:{os.environ['PATH']}",
      "AGENT_TOKEN": "test-token",
      "API_BASE_URL": "http://mobius.test",
      "PREVIEW_LOG": str(log),
    },
    text=True,
    capture_output=True,
    check=False,
  )

  assert result.returncode == 0, result.stderr
  assert log.read_text(encoding="utf-8").strip() == (
    f"/apps/duplicate-name-2/ {tmp_path / 'standalone.png'}"
  )
