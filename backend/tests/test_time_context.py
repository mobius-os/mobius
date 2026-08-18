"""Per-turn time context injected into the agent's user message.

Locks in the contract that the agent gets a clock every turn (issue: the
agent only ever saw an IANA timezone NAME, and only on turn 1).
"""

import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from app import models, schemas
from app.chat_context import (
  CLI_SLASH_COMMANDS,
  _build_time_context,
  _chat_has_goal_intent,
  _goal_clear_requested,
  _goal_objective,
  _goal_resume_requested,
  _human_elapsed,
  _is_cli_slash_command,
  _latest_goal_objective,
  _last_user_message_elapsed,
)
from app.database import SessionLocal


def _frontend_slash_command_names(source: str) -> set[str]:
  """Read every command name from the top-level frontend registry."""
  match = re.search(
    r"export const SLASH_COMMANDS\s*=\s*\[(?P<commands>.*?)^\]",
    source,
    flags=re.DOTALL | re.MULTILINE,
  )
  assert match, "could not find the frontend slash-command registry"
  return {
    f"/{name}"
    for name in re.findall(r"name:\s*'([^']+)'", match.group("commands"))
  }


def test_includes_timezone_label_and_clock():
  out = _build_time_context("Europe/London")
  assert out.startswith("[Context — current time:")
  assert "(Europe/London)" in out
  # A HH:MM clock is present.
  assert ":" in out


def test_distinct_zones_render_distinct_local_times():
  london = _build_time_context("Europe/London")
  tokyo = _build_time_context("Asia/Tokyo")
  # Same instant, different wall-clock — the strings must differ.
  assert london != tokyo


def test_missing_timezone_falls_back_to_utc():
  out = _build_time_context(None)
  assert "(UTC)" in out


def test_invalid_timezone_does_not_raise_and_keeps_label():
  out = _build_time_context("Not/AZone")
  assert "(Not/AZone)" in out


def test_human_elapsed_buckets_and_quiet_window():
  # Under ~2 minutes → quiet (None), so back-to-back turns stay clean.
  assert _human_elapsed(5) is None
  assert _human_elapsed(None) is None
  assert _human_elapsed(60 * 5) == "5 minutes ago"
  assert _human_elapsed(3600 * 3) == "3 hours ago"
  assert _human_elapsed(86400 * 3) == "3 days ago"
  assert "weeks ago" in _human_elapsed(86400 * 21)
  assert "months ago" in _human_elapsed(86400 * 90)


def test_elapsed_clause_only_when_present():
  assert "user's last message was" not in _build_time_context("UTC", None)
  out = _build_time_context("UTC", "3 days ago")
  assert "user's last message was 3 days ago" in out


def test_elapsed_ignores_automatic_continuation_marker(monkeypatch):
  now = 1_800_000_000.0
  monkeypatch.setattr(time, "time", lambda: now)
  cid = f"time-context-{uuid.uuid4()}"
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=cid,
      title="time context",
      messages=[
        {
          "role": "user", "content": "owner message",
          "ts": (now - 3 * 86400) * 1000,
        },
        {"role": "assistant", "content": "reply"},
        {
          "role": "user", "content": "continue",
          "kind": "auto_continuation",
          "ts": (now - 5 * 60) * 1000,
        },
        {"role": "assistant", "content": "automatic reply"},
        {"role": "user", "content": "current owner message", "ts": now * 1000},
      ],
    ))
    db.commit()

    assert _last_user_message_elapsed(db, cid) == "3 days ago"
  finally:
    db.close()


def test_goal_slash_command_is_detected_without_matching_paths():
  assert _is_cli_slash_command("/goal say PONG")
  assert _is_cli_slash_command("\n/goal clear")
  assert not _is_cli_slash_command("")
  assert not _is_cli_slash_command("\n\n")
  assert not _is_cli_slash_command("/")
  assert not _is_cli_slash_command("/data/apps/x is broken")
  assert not _is_cli_slash_command("please run /goal later")


def test_goal_objective_is_extracted_from_clean_persisted_message():
  assert _goal_objective("/goal repair every failing test") == (
    "repair every failing test"
  )
  assert _goal_objective("\n/goal\n  inspect first\nthen fix  ") == (
    "inspect first\nthen fix"
  )
  assert _goal_objective("/goal") is None
  assert _goal_objective("/goal clear") is None
  assert _goal_objective("/goal CLEAR") is None
  assert _goal_clear_requested("/goal clear")
  assert not _goal_clear_requested("/goal clear the backlog")
  assert _goal_objective("please /goal later") is None
  assert _goal_objective(" /goal indented is prose") is None
  assert _goal_objective("\t/goal indented is prose") is None


def test_goal_intent_uses_the_native_command_boundary():
  def messages(content):
    return [schemas.ChatMessage(role="user", content=content)]

  assert _chat_has_goal_intent(messages("/goal ship it"))
  assert _chat_has_goal_intent(messages("\n/goal\nship it"))
  assert not _chat_has_goal_intent(messages(" /goal indented is prose"))
  assert not _chat_has_goal_intent(messages("\t/goal indented is prose"))
  assert not _chat_has_goal_intent(messages("please run /goal later"))


def test_goal_intent_and_latest_objective_scan_durable_history():
  messages = [
    schemas.ChatMessage(role="user", content="hello"),
    schemas.ChatMessage(role="assistant", content="hi"),
    schemas.ChatMessage(role="user", content="/goal first objective"),
    schemas.ChatMessage(role="user", content="continue"),
  ]
  assert _chat_has_goal_intent(messages)
  assert _latest_goal_objective(messages) == "first objective"

  cleared = messages + [
    schemas.ChatMessage(role="user", content="/goal clear"),
    schemas.ChatMessage(role="user", content="continue"),
  ]
  assert _latest_goal_objective(cleared) is None

  changed_subject = messages[:-1] + [
    schemas.ChatMessage(role="user", content="different request"),
    schemas.ChatMessage(role="user", content="continue"),
  ]
  assert _latest_goal_objective(changed_subject) is None


def test_legacy_goal_migration_requires_a_durable_resume_intent():
  assert _goal_resume_requested(SimpleNamespace(messages=[
    {"role": "assistant", "blocks": [{"type": "error", "resumable": True}]},
    {"role": "user", "content": "continue"},
  ]), "continue")
  assert _goal_resume_requested(SimpleNamespace(messages=[
    {"role": "user", "content": "continue", "kind": "auto_continuation"},
  ]), "continue")
  assert not _goal_resume_requested(SimpleNamespace(messages=[
    {"role": "assistant", "content": "ordinary reply"},
    {"role": "user", "content": "continue"},
  ]), "continue")


def test_textless_send_is_not_a_slash_command():
  """An attachment-only send has no text; this used to raise IndexError."""
  for textless in ("", "   ", "\n\n", None):
    assert not _is_cli_slash_command(textless)


def test_slash_command_registry_parity():
  """The composer's "/" menu offers exactly what the backend dispatches.

  These two lists live in different languages and cannot import each other, so
  nothing but this test stops them drifting. Drift is silent in the worst
  direction: a command offered in the menu but unknown here still SENDS — it
  just stops being a command and becomes prose, with no error anywhere.
  """
  registry = (
    Path(__file__).resolve().parents[2]
    / "frontend/src/components/ChatView/slashCommands.js"
  )
  source = registry.read_text(encoding="utf-8")
  offered = _frontend_slash_command_names(source)

  assert offered, f"no commands parsed from {registry} — did its shape change?"
  assert offered == set(CLI_SLASH_COMMANDS), (
    "composer menu and backend dispatch disagree: "
    f"menu={sorted(offered)} backend={sorted(CLI_SLASH_COMMANDS)}"
  )


def test_slash_command_registry_parser_reads_commands_after_nested_arrays():
  source = """
export const SLASH_COMMANDS = [
  { name: 'goal', providers: ['claude'] },
  { name: 'review', providers: ['claude', 'codex'] },
]
"""

  assert _frontend_slash_command_names(source) == {"/goal", "/review"}
