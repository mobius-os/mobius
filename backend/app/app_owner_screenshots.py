"""Internal owner-browser capture for explicitly permitted app requests."""
import os
import subprocess
import time
import uuid
from pathlib import Path
from app.config import get_settings

_OWNER_SCREENSHOT_READY_SECS = 75


def _valid_screenshot_png(path: Path) -> bool:
  try:
    if path.stat().st_size <= 1024:
      return False
    with path.open("rb") as image:
      return image.read(8) == b"\x89PNG\r\n\x1a\n"
  except OSError:
    return False


def _run_screenshot_command(
  command: list[str],
  *,
  env: dict[str, str],
  timeout: float,
) -> None:
  completed = subprocess.run(
    command,
    env=env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    timeout=timeout,
    check=False,
  )
  if completed.returncode != 0:
    detail = (completed.stderr or completed.stdout or "capture failed").strip()
    raise RuntimeError(detail[-500:])


def capture_owner_shell(
  *,
  app_id: int,
  target_chat_id: str,
  output_chat_id: str,
  owner_token: str,
  warm_only: bool,
) -> str | None:
  """Capture one owner shell without exposing the owner token to the app.

  The first request authenticates a dedicated, server-owned browser profile.
  The scheduled bridge keeps that live page warm; later screenshot requests
  take a direct raster capture without starting an agent or navigating again.
  """
  settings = get_settings()
  data_dir = Path(settings.data_dir)
  profile = data_dir / "agent-browser-profiles" / f"app-owner-{app_id}"
  marker = Path(str(profile) + ".ready")
  env = os.environ.copy()
  env.update({
    "AGENT_TOKEN": owner_token,
    "API_BASE_URL": os.environ.get("API_BASE_URL", "http://localhost:8080"),
    "CHAT_ID": target_chat_id,
    "VIEWPORT_WIDTH": "1440",
    "VIEWPORT_HEIGHT": "1000",
    "VIEWPORT_PIXEL_RATIO": "1",
    "AGENT_BROWSER_SESSION": f"app-owner-{app_id}",
    "AGENT_BROWSER_PROFILE": str(profile),
  })

  marker_fresh = False
  try:
    marker_fresh = (
      marker.read_text().strip() == target_chat_id
      and time.time() - marker.stat().st_mtime < _OWNER_SCREENSHOT_READY_SECS
    )
  except OSError:
    pass

  if warm_only and marker_fresh:
    return None

  output_dir = data_dir / "chats" / output_chat_id / "media"
  output_dir.mkdir(parents=True, exist_ok=True)
  filename = f"owner-shell-{uuid.uuid4().hex}.png"
  output = output_dir / filename

  if marker_fresh:
    try:
      _run_screenshot_command(
        ["agent-browser", "screenshot", str(output)],
        env=env,
        timeout=5,
      )
      if _valid_screenshot_png(output):
        return None if warm_only else filename
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
      pass
    output.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)

  script = Path(__file__).resolve().parents[1] / "scripts" / "agent-screenshot.sh"
  cold_output = output
  if warm_only:
    cold_output = Path("/tmp") / f"app-owner-warm-{app_id}-{uuid.uuid4().hex}.png"
  try:
    _run_screenshot_command(
      [
        "bash", str(script), "--preserve-cache",
        f"/chat/{target_chat_id}", str(cold_output),
      ],
      env=env,
      timeout=35,
    )
    if not _valid_screenshot_png(cold_output):
      raise RuntimeError("capture did not produce a valid PNG")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(target_chat_id)
    return None if warm_only else filename
  except (OSError, RuntimeError, subprocess.TimeoutExpired):
    if not warm_only:
      cold_output.unlink(missing_ok=True)
    raise
  finally:
    if warm_only:
      cold_output.unlink(missing_ok=True)
