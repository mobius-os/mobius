"""Regression coverage for authenticated screenshot readiness checks."""

import fcntl
import math
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent-screenshot.sh"
SESSION_RESET = (
  Path(__file__).parents[1] / "scripts" / "agent_browser_session_reset.py"
)
PREVIEW_APP = Path(__file__).parents[1] / "scripts" / "preview_app.sh"
SHELL = Path(__file__).parents[2] / "frontend" / "src" / "components" / "Shell" / "Shell.jsx"
STANDALONE = (
  Path(__file__).parents[2]
  / "frontend"
  / "src"
  / "components"
  / "StandaloneApp"
  / "StandaloneApp.jsx"
)
SHELL_ENTRY = "index-test-fixture.js"


def _physical_pixels(css_pixels: int | float | str, pixel_ratio: float) -> int:
  return math.floor(float(css_pixels) * pixel_ratio + 0.5)


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
  shutil.copy2(SESSION_RESET, script.with_name(SESSION_RESET.name))
  dist = root / "frontend" / "dist"
  dist.mkdir(parents=True)
  (dist / "index.html").write_text(
    f'<script type="module" src="/assets/{SHELL_ENTRY}"></script>',
    encoding="utf-8",
  )
  return script


def _fake_browser(tmp_path: Path) -> tuple[Path, Path]:
  marker = tmp_path / "screenshot-called"
  sleep = tmp_path / "sleep"
  sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
  sleep.chmod(0o755)
  png_writer = tmp_path / "fake-png.py"
  png_writer.write_text(
    "import struct, sys\n"
    "path, width, height, size = sys.argv[1], *map(int, sys.argv[2:])\n"
    "header = (b'\\x89PNG\\r\\n\\x1a\\n' + struct.pack('>I', 13) + b'IHDR' "
    "+ struct.pack('>II', width, height))\n"
    "with open(path, 'wb') as handle:\n"
    "  handle.write(header + bytes(max(0, size - len(header))))\n",
    encoding="utf-8",
  )
  browser = tmp_path / "agent-browser"
  browser.write_text(
    "#!/bin/sh\n"
    "printf '%s\\n' \"$*\" >> \"$FAKE_BROWSER_LOG\"\n"
    "printf '%s|%s|%s|%s\\n' "
    "\"${AGENT_BROWSER_SESSION-}\" \"${AGENT_BROWSER_PROFILE-}\" "
    "\"${AGENT_BROWSER_ARGS-}\" \"${AGENT_BROWSER_DEFAULT_TIMEOUT-}\" "
    ">> \"$FAKE_BROWSER_IDENTITY_LOG\"\n"
    "if [ -e /proc/$$/fd/9 ]; then : > \"$FAKE_CAPTURE_LOCK_INHERITED\"; fi\n"
    "if [ \"$1\" = eval ] && [ \"${2:-}\" != --stdin ]; then\n"
    "  case \"${FAKE_TIMEOUT_EVAL_MODE:-}\" in\n"
    "    always) exit 124 ;;\n"
    "    once)\n"
    "      if [ ! -e \"$FAKE_TIMEOUT_EVAL_MARKER\" ]; then\n"
    "        : > \"$FAKE_TIMEOUT_EVAL_MARKER\"\n"
    "        exit 124\n"
    "      fi\n"
    "      ;;\n"
    "  esac\n"
    "fi\n"
    "case \"$1\" in\n"
    "  open)\n"
    "    if [ \"${FAKE_BOOTSTRAP_INTERCEPT_ONCE:-0}\" = 1 ] "
    "&& [ \"$2\" = http://mobius.test/api/browser-bootstrap ] "
    "&& [ ! -e \"$FAKE_BOOTSTRAP_INTERCEPT_MARKER\" ]; then\n"
    "      : > \"$FAKE_BOOTSTRAP_INTERCEPT_MARKER\"\n"
    "      printf '%s\\n' http://mobius.test/shell/ > \"$FAKE_BROWSER_URL_FILE\"\n"
    "    else\n"
    "      printf '%s\\n' \"$2\" > \"$FAKE_BROWSER_URL_FILE\"\n"
    "    fi\n"
    "    if [ -n \"${FAKE_CANONICAL_TARGET_URL:-}\" ]; then\n"
    "      case \"$2\" in\n"
    "        */chat/*) printf '%s\\n' \"$FAKE_CANONICAL_TARGET_URL\" > \"$FAKE_BROWSER_NEXT_URL_FILE\" ;;\n"
    "      esac\n"
    "    fi\n"
    "    ;;\n"
    "  get)\n"
    "    if [ \"$2\" = url ]; then\n"
    "      cat \"$FAKE_BROWSER_URL_FILE\"\n"
    "      if [ -s \"$FAKE_BROWSER_NEXT_URL_FILE\" ]; then\n"
    "        mv \"$FAKE_BROWSER_NEXT_URL_FILE\" \"$FAKE_BROWSER_URL_FILE\"\n"
    "      fi\n"
    "    fi\n"
    "    ;;\n"
    "  eval)\n"
    "    if [ \"$2\" = \"--stdin\" ]; then cat > \"$FAKE_BROWSER_STDIN_LOG\"; exit 0; fi\n"
    "    case \"$2\" in\n"
    "      *body\\ \\>\\ iframe#app*) printf '%s\\n' \"${FAKE_PUBLIC_APP:-false}\" ;;\n"
    "      *src.split*) printf '%s\\n' \"${FAKE_LOADED_ASSET:-none}\" ;;\n"
    "      *serviceWorker*) printf '%s\\n' true ;;\n"
    "      *) printf '%s\\n' \"${FAKE_AUTH_OK:-false}\" ;;\n"
    "    esac\n"
    "    ;;\n"
    "  set)\n"
    "    if [ \"${FAKE_VIEWPORT_FAIL_ONCE:-0}\" = 1 ] && [ ! -e \"$FAKE_VIEWPORT_MARKER\" ]; then\n"
    "      : > \"$FAKE_VIEWPORT_MARKER\"\n"
    "      exit 1\n"
    "    fi\n"
    "    ;;\n"
    "  wait)\n"
    "    if [ \"${FAKE_WAIT_ERROR:-0}\" = 1 ]; then\n"
    "      printf '%s\\n' 'renderer disconnected' >&2\n"
    "      exit 1\n"
    "    fi\n"
    "    ;;\n"
    "  screenshot)\n"
    "    count=$(cat \"$FAKE_SCREENSHOT_COUNT_FILE\" 2>/dev/null || printf 0)\n"
    "    count=$((count + 1))\n"
    "    printf '%s\\n' \"$count\" > \"$FAKE_SCREENSHOT_COUNT_FILE\"\n"
    "    if [ \"${FAKE_SCREENSHOT_FAIL_AFTER_WARMUP:-0}\" = 1 ] && [ \"$count\" -gt 1 ]; then\n"
    "      printf partial > \"$2\"\n"
    "      exit 1\n"
    "    fi\n"
    "    if [ \"${FAKE_SCREENSHOT_FAIL_ONCE:-0}\" = 1 ] && [ ! -e \"$FAKE_SCREENSHOT_RETRY_MARKER\" ]; then\n"
    "      : > \"$FAKE_SCREENSHOT_RETRY_MARKER\"\n"
    "      exit 1\n"
    "    fi\n"
    "    if [ \"${FAKE_SCREENSHOT_TINY_ONCE:-0}\" = 1 ] && [ ! -e \"$FAKE_SCREENSHOT_TINY_MARKER\" ]; then\n"
    "      : > \"$FAKE_SCREENSHOT_TINY_MARKER\"\n"
    "      python3 \"$FAKE_PNG_WRITER\" \"$2\" \"$FAKE_PNG_WIDTH\" \"$FAKE_PNG_HEIGHT\" 100\n"
    "    else\n"
    "      python3 \"$FAKE_PNG_WRITER\" \"$2\" \"$FAKE_PNG_WIDTH\" \"$FAKE_PNG_HEIGHT\" 9000\n"
    "    fi\n"
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
  viewport_pixel_ratio: int | float | str = 1,
  content_only: bool = False,
  preserve_cache: bool = False,
  current_page: bool = False,
  loaded_asset: str | None = None,
  viewport_fail_once: bool = False,
  screenshot_fail_once: bool = False,
  screenshot_fail_after_warmup: bool = False,
  canonical_target_url: str | None = None,
  screenshot_tiny_once: bool = False,
  wait_error: bool = False,
  timeout_eval_mode: str = "",
  bootstrap_intercept_once: bool = False,
  public_app: bool = False,
  existing_output: bytes | None = None,
  profile_locked: bool = False,
  subprocess_timeout: float | None = None,
  profile_lock_target: str | None = None,
  profile_lock_artifacts: tuple[str, ...] = (
    "SingletonLock", "SingletonCookie", "SingletonSocket",
  ),
) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
  _, marker = _fake_browser(tmp_path)
  script = _fixture_script(tmp_path)
  output = tmp_path / "shot.png"
  browser_log = tmp_path / "browser.log"
  browser_profile = tmp_path / "browser-profile"
  browser_profile.mkdir()
  if existing_output is not None:
    output.write_bytes(existing_output)
  if profile_lock_target is not None:
    for artifact in profile_lock_artifacts:
      (browser_profile / artifact).symlink_to(profile_lock_target)
  try:
    normalized_ratio = min(4.0, max(0.5, float(viewport_pixel_ratio)))
    fake_png_width = _physical_pixels(viewport_width, normalized_ratio)
    fake_png_height = _physical_pixels(viewport_height, normalized_ratio)
  except (TypeError, ValueError):
    fake_png_width = fake_png_height = 1
  env = {
    **os.environ,
    "PATH": f"{tmp_path}:{os.environ['PATH']}",
    "TMPDIR": str(tmp_path),
    "AGENT_TOKEN": "test-token",
    "API_BASE_URL": "http://mobius.test",
    "VIEWPORT_WIDTH": str(viewport_width),
    "VIEWPORT_HEIGHT": str(viewport_height),
    "VIEWPORT_PIXEL_RATIO": str(viewport_pixel_ratio),
    "AGENT_BROWSER_SESSION": "test-session",
    "AGENT_BROWSER_PROFILE": str(browser_profile),
    "AGENT_BROWSER_ARGS": "--test-daemon-identity",
    "AGENT_BROWSER_DEFAULT_TIMEOUT": "",
    "FAKE_AUTH_OK": "true" if auth_ok else "false",
    "FAKE_LOADED_ASSET": loaded_asset or SHELL_ENTRY,
    "FAKE_BROWSER_LOG": str(browser_log),
    "FAKE_BROWSER_IDENTITY_LOG": str(tmp_path / "browser-identity.log"),
    "FAKE_CAPTURE_LOCK_INHERITED": str(tmp_path / "capture-lock-inherited"),
    "FAKE_SCREENSHOT_MARKER": str(marker),
    "FAKE_VIEWPORT_FAIL_ONCE": "1" if viewport_fail_once else "0",
    "FAKE_VIEWPORT_MARKER": str(tmp_path / "viewport-ready"),
    "FAKE_SCREENSHOT_FAIL_ONCE": "1" if screenshot_fail_once else "0",
    "FAKE_SCREENSHOT_FAIL_AFTER_WARMUP": (
      "1" if screenshot_fail_after_warmup else "0"
    ),
    "FAKE_SCREENSHOT_COUNT_FILE": str(tmp_path / "screenshot-count"),
    "FAKE_SCREENSHOT_RETRY_MARKER": str(tmp_path / "screenshot-retried"),
    "FAKE_SCREENSHOT_TINY_ONCE": "1" if screenshot_tiny_once else "0",
    "FAKE_SCREENSHOT_TINY_MARKER": str(tmp_path / "screenshot-tiny"),
    "FAKE_PNG_WRITER": str(tmp_path / "fake-png.py"),
    "FAKE_PNG_WIDTH": str(fake_png_width),
    "FAKE_PNG_HEIGHT": str(fake_png_height),
    "FAKE_BROWSER_URL_FILE": str(tmp_path / "browser-url"),
    "FAKE_BROWSER_NEXT_URL_FILE": str(tmp_path / "browser-next-url"),
    "FAKE_CANONICAL_TARGET_URL": canonical_target_url or "",
    "FAKE_BROWSER_STDIN_LOG": str(tmp_path / "browser-stdin.log"),
    "FAKE_WAIT_ERROR": "1" if wait_error else "0",
    "FAKE_TIMEOUT_EVAL_MODE": timeout_eval_mode,
    "FAKE_TIMEOUT_EVAL_MARKER": str(tmp_path / "timeout-eval-once"),
    "FAKE_BOOTSTRAP_INTERCEPT_ONCE": "1" if bootstrap_intercept_once else "0",
    "FAKE_BOOTSTRAP_INTERCEPT_MARKER": str(tmp_path / "bootstrap-intercepted"),
    "FAKE_PUBLIC_APP": "true" if public_app else "false",
  }
  args = ["bash", str(script)]
  if content_only:
    args.append("--content-only")
  if preserve_cache:
    args.append("--preserve-cache")
  if current_page:
    args.append("--current-page")
  args.extend([route, str(output)])
  lock_handle = None
  try:
    if profile_locked:
      lock_handle = Path(f"{browser_profile}.capture.lock").open("w")
      fcntl.flock(lock_handle, fcntl.LOCK_EX)
    result = subprocess.run(
      args,
      env=env,
      text=True,
      capture_output=True,
      check=False,
      timeout=subprocess_timeout,
    )
  finally:
    if lock_handle is not None:
      fcntl.flock(lock_handle, fcntl.LOCK_UN)
      lock_handle.close()
  return result, output, marker, browser_log


def test_stale_foreign_container_profile_lock_is_repaired_before_launch(tmp_path: Path):
  profile = tmp_path / "browser-profile"
  result, output, marker, _ = _run_helper(
    tmp_path,
    auth_ok=True,
    profile_lock_target="previous-container-999999",
    profile_lock_artifacts=("SingletonLock",),
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  assert not any(
    (profile / artifact).is_symlink()
    for artifact in ("SingletonLock", "SingletonCookie", "SingletonSocket")
  )


def test_live_local_profile_lock_is_preserved(tmp_path: Path):
  profile = tmp_path / "browser-profile"
  result, output, marker, _ = _run_helper(
    tmp_path,
    auth_ok=True,
    profile_lock_target=f"{socket.gethostname()}-{os.getpid()}",
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  assert all(
    (profile / artifact).is_symlink()
    for artifact in ("SingletonLock", "SingletonCookie", "SingletonSocket")
  )


def test_unfamiliar_profile_lock_is_preserved(tmp_path: Path):
  profile = tmp_path / "browser-profile"
  result, output, marker, _ = _run_helper(
    tmp_path,
    auth_ok=True,
    profile_lock_target="unexpected-owner-format",
  )

  assert result.returncode == 0, result.stderr
  assert "unfamiliar owner" in result.stderr
  assert output.exists()
  assert marker.exists()
  assert all(
    (profile / artifact).is_symlink()
    for artifact in ("SingletonLock", "SingletonCookie", "SingletonSocket")
  )


def test_helper_refuses_to_capture_when_protected_request_rejects_token(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=False)

  assert result.returncode != 0
  assert "authentication failed" in result.stderr
  assert "/api/chats" in browser_log.read_text(encoding="utf-8")
  assert not output.exists()
  assert not marker.exists()


def test_browser_bootstrap_is_inert_same_origin_html(client):
  response = client.get("/api/browser-bootstrap")

  assert response.status_code == 200
  assert response.headers["content-type"].startswith("text/html")
  assert response.headers["cache-control"] == "no-store"
  assert "<script" not in response.text
  assert "Möbius browser bootstrap" in response.text


def test_helper_captures_after_authentication_is_confirmed(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()

  commands = browser_log.read_text(encoding="utf-8").splitlines()
  target_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("open http://mobius.test/chat/example?__mobius_capture=")
  )
  auth_index = next(i for i, command in enumerate(commands) if "/api/chats" in command)
  screenshot_index = next(i for i, command in enumerate(commands) if command.startswith("screenshot "))
  assert target_index < auth_index < screenshot_index
  assert not any(
    command.startswith("wait ") and "input[type=password]" in command
    for command in commands
  )
  assert all("test-token" not in command for command in commands)


def test_auth_check_targets_login_surface_not_unrelated_password_fields():
  source = SCRIPT.read_text(encoding="utf-8")

  assert "[data-auth-surface=login]" in source
  assert "input[type=password]" not in source


def test_helper_accepts_a_stable_canonical_shell_route(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    canonical_target_url="http://mobius.test/shell/",
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  target_opens = [
    command for command in commands
    if command.startswith("open http://mobius.test/chat/example?__mobius_capture=")
  ]
  assert len(target_opens) == 1, "a stable canonical redirect must not be retried"


def test_default_capture_detaches_pwa_state_and_verifies_current_shell(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  seed_index = commands.index("eval --stdin")
  seed = (tmp_path / "browser-stdin.log").read_text(encoding="utf-8")
  assert "serviceWorker" in seed
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
  detach_indexes = [
    i for i, command in enumerate(commands) if command == "open about:blank"
  ]
  assert any(index < seed_index for index in detach_indexes)
  assert any(seed_index < index < target_index for index in detach_indexes)
  assert target_index < build_index < screenshot_index


def test_default_capture_detaches_a_stale_controller_before_requiring_bootstrap(
  tmp_path: Path,
):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  first_bootstrap = commands.index("open http://mobius.test/api/browser-bootstrap")
  preflight_unregister = next(
    i for i, command in enumerate(commands)
    if command.startswith("eval ") and "serviceWorker" in command
  )
  first_detach = commands.index("open about:blank")
  second_bootstrap = next(
    i for i, command in enumerate(commands[first_detach + 1:], first_detach + 1)
    if command == "open http://mobius.test/api/browser-bootstrap"
  )
  assert first_bootstrap < preflight_unregister < first_detach < second_bootstrap


def test_stale_worker_bootstrap_interception_recovers_before_authentication(
  tmp_path: Path,
):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    bootstrap_intercept_once=True,
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  assert (tmp_path / "bootstrap-intercepted").exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  first_detach = commands.index("open about:blank")
  authenticated_bootstrap = next(
    i for i, command in enumerate(commands[first_detach + 1:], first_detach + 1)
    if command == "open http://mobius.test/api/browser-bootstrap"
  )
  assert first_detach < authenticated_bootstrap


def test_timeout_stops_queueing_commands_and_restarts_the_one_profile(
  tmp_path: Path,
):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    timeout_eval_mode="once",
    subprocess_timeout=10,
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  assert "retained browser-state cleanup timed out" in result.stderr
  assert "restarting this chat's isolated browser session once" in result.stderr
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  timed_eval = next(
    i for i, command in enumerate(commands)
    if command.startswith("eval ") and "serviceWorker" in command
  )
  # No navigation/eval/close request is queued behind the poisoned daemon. The
  # process-scoped reset is invisible to this command log; capture starts fresh.
  assert commands[timed_eval + 1] == "open http://mobius.test/api/browser-bootstrap"


def test_second_timeout_reports_the_failed_phase_without_an_infinite_restart(
  tmp_path: Path,
):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    timeout_eval_mode="always",
    subprocess_timeout=10,
  )

  assert result.returncode != 0
  assert "retained browser-state cleanup timed out again" in result.stderr
  assert "after one isolated browser-session restart" in result.stderr
  assert not output.exists()
  assert not marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert "close" not in commands


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


def test_public_app_host_uses_its_mounted_frame_readiness_contract(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    route="/ra-event-map",
    public_app=True,
    loaded_asset="index-stale.js",
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert not any("src.split" in command for command in commands)
  assert any("#status.is-ready" in command for command in commands)


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
  seed = (tmp_path / "browser-stdin.log").read_text(encoding="utf-8")
  assert "open http://mobius.test/chat/example" in commands
  assert "serviceWorker" not in seed
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
  assert "set viewport 1680 957 1" in browser_log.read_text(
    encoding="utf-8",
  ).splitlines()


def test_capture_matches_chromium_pixel_rounding_without_changing_css_viewport(
  tmp_path: Path,
):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    viewport_width=426,
    viewport_height=860,
    viewport_pixel_ratio=2.25,
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  assert "set viewport 426 860 2.25" in browser_log.read_text(
    encoding="utf-8",
  ).splitlines()
  header = output.read_bytes()[:24]
  assert int.from_bytes(header[16:20], "big") == 959
  assert int.from_bytes(header[20:24], "big") == 1935


def test_cold_capture_opens_browser_before_configuring_viewport(tmp_path: Path):
  result, _, _, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert commands.index("open http://mobius.test/api/browser-bootstrap") < commands.index(
    "set viewport 412 915 1",
  )
  assert "open http://mobius.test/" not in commands


def test_current_page_capture_preserves_document_but_reuses_verified_boundary(
  tmp_path: Path,
):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    current_page=True,
    viewport_pixel_ratio=3,
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert not any(command.startswith("open ") for command in commands)
  assert "set viewport 412 915 3" in commands
  assert any(
    command.startswith("eval ") and "__mobiusFontReadiness" in command
    for command in commands
  )
  assert not any("Toggle navigation" in command for command in commands)
  assert not any(".drawer-overlay--blocking" in command for command in commands)
  assert any(command.startswith("screenshot ") for command in commands)


def test_browser_commands_keep_one_daemon_identity(tmp_path: Path):
  result, _, _, _ = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  identities = (tmp_path / "browser-identity.log").read_text(
    encoding="utf-8",
  ).splitlines()
  expected = (
    f"test-session|{tmp_path / 'browser-profile'}|--test-daemon-identity|"
  )
  assert identities
  assert set(identities) == {expected}


def test_browser_daemon_does_not_inherit_the_capture_transaction_lock(
  tmp_path: Path,
):
  result, _, _, _ = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert not (tmp_path / "capture-lock-inherited").exists()


def test_browser_failure_includes_last_command_detail(tmp_path: Path):
  result, output, marker, _ = _run_helper(
    tmp_path,
    auth_ok=True,
    wait_error=True,
  )

  assert result.returncode != 0
  assert "target document did not finish its initial paint" in result.stderr
  assert "agent-browser: renderer disconnected" in result.stderr
  assert not output.exists()
  assert not marker.exists()


def test_cold_capture_retries_viewport_until_browser_socket_is_ready(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    viewport_fail_once=True,
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert commands.count("set viewport 412 915 1") == 5
  assert commands.index("set viewport 412 915 1") < max(
    i for i, command in enumerate(commands)
    if command == "open http://mobius.test/api/browser-bootstrap"
  )


def test_final_target_reapplies_the_requested_viewport_at_capture_boundary(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  target_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("open http://mobius.test/chat/example?__mobius_capture=")
  )
  final_viewport_index = max(
    i for i, command in enumerate(commands)
    if command == "set viewport 412 915 1"
  )
  final_screenshot_index = max(
    i for i, command in enumerate(commands)
    if command.startswith("screenshot ")
  )
  final_capture = Path(commands[final_screenshot_index].split(maxsplit=1)[1])
  assert final_capture.parent == output.parent
  assert final_capture.name.startswith(".mobius-screenshot.")
  assert final_capture.suffix == ".png"
  target_viewports = [
    i for i, command in enumerate(commands)
    if i > target_index and command == "set viewport 412 915 1"
  ]
  assert len(target_viewports) == 3
  assert target_index < final_viewport_index < final_screenshot_index


def test_capture_retries_when_busy_renderer_rejects_first_screenshot(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    screenshot_fail_once=True,
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  assert sum(command.startswith("screenshot ") for command in commands) == 3


def test_capture_primes_the_compositor_before_keeping_evidence(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(tmp_path, auth_ok=True)

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  screenshots = [
    command for command in commands if command.startswith("screenshot ")
  ]
  assert len(screenshots) == 2
  assert "/mobius-screenshot-warmup." in screenshots[0]
  final_capture = Path(screenshots[1].split(maxsplit=1)[1])
  assert final_capture.parent == output.parent
  assert final_capture.name.startswith(".mobius-screenshot.")
  assert final_capture.suffix == ".png"
  warmup_index = commands.index(screenshots[0])
  post_warmup_frame = next(
    i for i, command in enumerate(commands[warmup_index + 1:], warmup_index + 1)
    if command.startswith("eval ") and "requestAnimationFrame" in command
  )
  assert warmup_index < post_warmup_frame < commands.index(screenshots[1])


def test_failed_final_capture_preserves_last_known_good_output(tmp_path: Path):
  existing = b"last-known-good"
  result, output, _, _ = _run_helper(
    tmp_path,
    auth_ok=True,
    existing_output=existing,
    screenshot_fail_after_warmup=True,
  )

  assert result.returncode != 0
  assert "remained too busy to capture" in result.stderr
  assert output.read_bytes() == existing
  assert not list(tmp_path.glob(".mobius-screenshot.*.png"))
  assert not list(tmp_path.glob("mobius-screenshot-warmup.*.png"))
  assert not list(tmp_path.glob("mobius-agent-browser-error.*"))


def test_shared_profile_transaction_waits_before_browser_commands(tmp_path: Path):
  with pytest.raises(subprocess.TimeoutExpired):
    _run_helper(
      tmp_path,
      auth_ok=True,
      profile_locked=True,
      subprocess_timeout=0.5,
    )

  assert not (tmp_path / "browser.log").exists()


def test_malformed_app_route_is_rejected_before_capture(tmp_path: Path):
  result, output, marker, _ = _run_helper(
    tmp_path,
    auth_ok=True,
    route="/app/42oops",
  )

  assert result.returncode != 0
  assert "require a numeric app id" in result.stderr
  assert not output.exists()
  assert not marker.exists()


def test_shell_capture_retries_a_solid_background_frame(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    screenshot_tiny_once=True,
  )

  assert result.returncode == 0, result.stderr
  assert output.stat().st_size == 9000
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  screenshots = [
    command for command in commands if command.startswith("screenshot ")
  ]
  assert len(screenshots) == 3, "tiny warm-up, retry, then kept capture"


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


def test_invalid_manual_pixel_density_fails_before_browser_launch(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path,
    auth_ok=True,
    viewport_pixel_ratio="nan",
  )

  assert result.returncode != 0
  assert "pixel ratio must be positive" in result.stderr
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


def test_shell_capture_waits_for_visual_ownership_and_rendered_fonts(tmp_path: Path):
  result, output, marker, browser_log = _run_helper(
    tmp_path, auth_ok=True, route="/chat/example",
  )

  assert result.returncode == 0, result.stderr
  assert output.exists()
  assert marker.exists()
  commands = browser_log.read_text(encoding="utf-8").splitlines()
  settle_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("wait --fn ")
    and "data-workspace-visual-state" in command
    and "first-contentful-paint" in command
  )
  settle_command = commands[settle_index]
  assert "shell__chat-view--staging" not in settle_command
  assert "shell__chat-view--held" not in settle_command
  assert "data-mode-motion" not in settle_command
  frame_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("eval ") and "requestAnimationFrame" in command
  )
  fonts_index = max(
    i for i, command in enumerate(commands)
    if command.startswith("eval ")
    and "__mobiusFontReadiness" in command
  )
  font_command = commands[fonts_index]
  assert "settleCapture(document)" in font_command
  viewport_index = max(
    i for i, command in enumerate(commands)
    if command == "set viewport 412 915 1"
  )
  screenshot_index = max(
    i for i, command in enumerate(commands) if command.startswith("screenshot ")
  )
  assert settle_index < frame_index < viewport_index < fonts_index < screenshot_index


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
  seed_index = commands.index("eval --stdin")
  seed = (tmp_path / "browser-stdin.log").read_text(encoding="utf-8")
  assert 'sessionStorage.setItem("mobius:visual-content-only", "1")' in seed
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
  assert seed_index < target_index < readiness_index < screenshot_index


def test_default_mode_clears_prior_visual_mode_before_navigation(tmp_path: Path):
  _, _, _, browser_log = _run_helper(tmp_path, auth_ok=True)

  commands = browser_log.read_text(encoding="utf-8").splitlines()
  seed_index = commands.index("eval --stdin")
  seed = (tmp_path / "browser-stdin.log").read_text(encoding="utf-8")
  assert 'sessionStorage.removeItem("mobius:visual-content-only")' in seed
  target_index = next(
    i for i, command in enumerate(commands)
    if command.startswith("open http://mobius.test/chat/example?__mobius_capture=")
  )
  assert seed_index < target_index


def test_content_mode_suppresses_modals_without_dom_surgery():
  helper = SCRIPT.read_text(encoding="utf-8")
  shell = SHELL.read_text(encoding="utf-8")
  standalone = STANDALONE.read_text(encoding="utf-8")

  assert "querySelectorAll('.wt__overlay, #install-backdrop')" not in helper
  assert "const showWalkthrough = !visualContentOnly" in shell
  assert "!visualContentOnly && (" in standalone


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
