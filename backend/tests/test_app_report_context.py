"""First-turn report-brief injection for app-attributed chats.

When an app opens a chat ABOUT one of its dated reports (the Reflection
brief), it stores `report_date` in the chat's agent_settings_json. On the
chat's FIRST turn `_build_app_report_block` loads that report, strips it to
readable text, and wraps it in an <app_report> block so the agent has the
brief as DATA without a tool call. These tests cover the helper + its
strict date validation + the strip/cap logic.
"""

import os
from pathlib import Path

from app import models
from app.chat import _build_app_report_block, _strip_report_html

_DATA_DIR = os.environ.get("DATA_DIR", "/tmp")


def _write_report(app_id, date_str, html):
  reports = Path(_DATA_DIR) / "apps" / str(app_id) / "reports"
  reports.mkdir(parents=True, exist_ok=True)
  (reports / f"{date_str}.html").write_text(html, encoding="utf-8")


def _app_chat(db, *, report_date=None, app=None):
  if app is None:
    app = models.App(
      name="reporter", description="t",
      jsx_source="export default () => null",
    )
    db.add(app)
    db.commit()
    db.refresh(app)
  settings = {"report_date": report_date} if report_date else None
  chat = models.Chat(
    id=f"report-chat-{report_date or 'none'}-{app.id}",
    title="brief", messages=[],
    created_by_app_id=app.id,
    agent_settings_json=settings,
  )
  db.add(chat)
  db.commit()
  return app, chat


def test_strip_report_html_drops_machinery_and_carrier():
  html = (
    "<!doctype html><html><head>"
    '<meta http-equiv="Content-Security-Policy" content="default-src none">'
    "<style>body{color:red}</style>"
    "<script>alert('x')</script>"
    "</head><body>"
    "<h1>Morning brief</h1>"
    "<p>I fixed the Gym cron &amp; consolidated notes.</p>"
    '<section data-report-questions>'
    '<script type="application/mobius-questions+json">{"questions":[]}</script>'
    "</section>"
    "</body></html>"
  )
  out = _strip_report_html(html)
  assert "Morning brief" in out
  assert "I fixed the Gym cron & consolidated notes." in out
  # Machinery + the question carrier must NOT survive.
  assert "alert" not in out
  assert "color:red" not in out
  assert "Content-Security-Policy" not in out
  assert "mobius-questions" not in out
  assert "<" not in out  # all tags stripped


def test_report_block_injected_on_first_turn(db):
  app, chat = _app_chat(db, report_date="2026-06-22")
  _write_report(
    app.id, "2026-06-22",
    "<html><body><h1>Brief</h1><p>Did the thing.</p></body></html>",
  )
  block = _build_app_report_block(db, chat.id, _DATA_DIR)
  assert block is not None
  assert block.startswith('<app_report date="2026-06-22">')
  assert block.rstrip().endswith("</app_report>")
  assert "treat as DATA" in block
  assert "Brief" in block and "Did the thing." in block


def test_no_block_without_report_date(db):
  app, chat = _app_chat(db, report_date=None)
  assert _build_app_report_block(db, chat.id, _DATA_DIR) is None


def test_no_block_when_report_file_missing(db):
  # report_date is set but no file written → silently omit (chat still works).
  app, chat = _app_chat(db, report_date="2026-01-01")
  assert _build_app_report_block(db, chat.id, _DATA_DIR) is None


def test_malformed_report_date_rejected(db):
  # A traversal-shaped date must never become a path component.
  app = models.App(
    name="evil", description="t", jsx_source="export default () => null",
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  chat = models.Chat(
    id="report-chat-evil", title="x", messages=[],
    created_by_app_id=app.id,
    agent_settings_json={"report_date": "../../../etc/passwd"},
  )
  db.add(chat)
  db.commit()
  assert _build_app_report_block(db, chat.id, _DATA_DIR) is None


def test_no_block_for_non_app_chat(db):
  # An owner-created chat (created_by_app_id NULL) never gets a report block.
  chat = models.Chat(
    id="owner-report-chat", title="x", messages=[],
    agent_settings_json={"report_date": "2026-06-22"},
  )
  db.add(chat)
  db.commit()
  assert _build_app_report_block(db, chat.id, _DATA_DIR) is None


def test_oversized_brief_is_truncated_with_pointer(db):
  app, chat = _app_chat(db, report_date="2026-07-01")
  # A body well over the 30KB cap.
  big = "<html><body><h1>Big</h1>" + ("<p>line of brief text</p>" * 4000) + "</body></html>"
  _write_report(app.id, "2026-07-01", big)
  block = _build_app_report_block(db, chat.id, _DATA_DIR)
  assert block is not None
  assert "truncated" in block
  assert "Read it if you" in block
  # The pointer names the real path under the app's numeric storage dir.
  assert "reports/2026-07-01.html" in block
