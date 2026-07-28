"""Image producers put viewable output directly in protected chat media."""

from pathlib import Path
import os
import shutil
import subprocess


PREVIEW_SHELL = Path(__file__).parents[1] / "scripts" / "preview_shell.sh"
RENDER_MINIAPP = Path(__file__).parents[2] / "scripts" / "render-miniapp.sh"


def test_shell_preview_delegates_its_chat_default_to_the_media_owner(
  tmp_path: Path,
):
  preview = tmp_path / "preview_shell.sh"
  shutil.copy2(PREVIEW_SHELL, preview)
  fake_screenshot = tmp_path / "agent-screenshot.sh"
  fake_screenshot.write_text(
    "#!/bin/sh\nprintf '%s|%s\\n' \"${CHAT_ID:-}\" \"$*\" > \"$PREVIEW_LOG\"\n",
    encoding="utf-8",
  )
  fake_screenshot.chmod(0o755)
  log = tmp_path / "preview.log"

  env = {**os.environ, "PREVIEW_LOG": str(log)}
  env.pop("CHAT_ID", None)
  result = subprocess.run(
    ["bash", str(preview), "chat-example"],
    env=env,
    text=True,
    capture_output=True,
    check=False,
  )

  assert result.returncode == 0, result.stderr
  assert log.read_text(encoding="utf-8").strip() == (
    "chat-example|/chat/chat-example"
  )


def test_shell_preview_preserves_explicit_output_and_non_chat_fallback(
  tmp_path: Path,
):
  preview = tmp_path / "preview_shell.sh"
  shutil.copy2(PREVIEW_SHELL, preview)
  fake_screenshot = tmp_path / "agent-screenshot.sh"
  fake_screenshot.write_text(
    "#!/bin/sh\nprintf '%s|%s\\n' \"${CHAT_ID:-}\" \"$*\" > \"$PREVIEW_LOG\"\n",
    encoding="utf-8",
  )
  fake_screenshot.chmod(0o755)
  log = tmp_path / "preview.log"
  env = {**os.environ, "PREVIEW_LOG": str(log)}
  env.pop("CHAT_ID", None)

  explicit = tmp_path / "chosen.png"
  subprocess.run(
    ["bash", str(preview), "chat-example", str(explicit)],
    env=env,
    check=True,
  )
  assert log.read_text(encoding="utf-8").strip() == (
    f"chat-example|/chat/chat-example {explicit}"
  )

  subprocess.run(["bash", str(preview)], env=env, check=True)
  assert log.read_text(encoding="utf-8").strip() == (
    "|/ /tmp/shell-preview.png"
  )


def test_test_renderer_writes_once_to_chat_media_when_chat_is_available():
  source = RENDER_MINIAPP.read_text(encoding="utf-8")

  assert 'elif [[ -n "${CHAT_ID:-}" ]]; then' in source
  assert 'MEDIA_DIR="${DATA_DIR:-/data}/chats/${CHAT_ID}/media"' in source
  assert 'OUT="${MEDIA_DIR}/render-${SLUG}-$(date +%s%N).png"' in source
