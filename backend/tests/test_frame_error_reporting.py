"""Mini-app frame failures expose a safe, actionable recovery report."""

from pathlib import Path


def test_frame_error_report_redacts_module_token():
  frame = (
    Path(__file__).resolve().parents[2]
    / "frontend" / "public" / "app-frame.html"
  ).read_text(encoding="utf-8")

  assert "function redactErrorCredentials" in frame
  assert "detail = redactErrorCredentials(detail)" in frame
  assert "'$1[redacted]'" in frame
  assert "user-select: text" in frame
  assert "Report to agent" in frame
