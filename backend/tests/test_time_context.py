"""Per-turn time context injected into the agent's user message.

Locks in the contract that the agent gets a clock every turn (issue: the
agent only ever saw an IANA timezone NAME, and only on turn 1).
"""

import re
import time
import uuid
from pathlib import Path

from app import models
from app.chat import (
  CLI_SLASH_COMMANDS,
  _build_time_context,
  _human_elapsed,
  _is_cli_slash_command,
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
