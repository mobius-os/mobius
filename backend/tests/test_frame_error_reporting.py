"""Mini-app frame failures expose a safe, actionable recovery report."""

import re
from pathlib import Path


def test_frame_error_report_redacts_module_token():
  frame = (
    Path(__file__).resolve().parents[2]
    / "frontend" / "public" / "app-frame.html"
  ).read_text(encoding="utf-8")

  assert "function redactErrorCredentials" in frame
  assert "detail = redactErrorCredentials(detail)" in frame
  assert "'$1[redacted]'" in frame
  assert re.search(
    r"function handleFrameError\(title, detail, source\)\s*\{\s*"
    r"[\s\S]{0,260}?detail = redactErrorCredentials\(detail\)"
    r"[\s\S]{0,120}?if \(window\.__frameMounted\)",
    frame,
  ), "runtime error details must be redacted before any console branch"
  report_block = frame.split("function reportAppError", 1)[1].split(
    "window.addEventListener('error'", 1
  )[0]
  assert "const safeMessage = redactErrorCredentials(message)" in report_block
  assert "const safeStack = stack ? redactErrorCredentials(stack)" in report_block
  assert "const safeUrl = redactErrorCredentials(location.href)" in report_block
  assert "message: safeMessage.slice" in report_block
  assert "stack: safeStack ? safeStack.slice" in report_block
  assert frame.count("e.preventDefault()") >= 2
  assert "user-select: text" in frame
  assert "Report to agent" in frame
