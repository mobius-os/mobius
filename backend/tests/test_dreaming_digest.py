"""Tests for the Dreaming digest's app_error classification (#1A).

Locks in: app_error events from activity.jsonl bucket per-app + shell;
stacks are dropped; shell == no app_id; recent window is capped.
"""

from app.dreaming_digest import summarize_app_errors


def _err(app_id=None, message="boom", where="render", ts="2026-06-16T00:00:00", stack=None):
  ev = {"ev": "app_error", "ts": ts, "message": message, "where": where}
  if app_id is not None:
    ev["app_id"] = app_id
  if stack is not None:
    ev["stack"] = stack
  return ev


def test_buckets_per_app_and_counts():
  events = [_err(app_id=7), _err(app_id=7), _err(app_id=9)]
  out = summarize_app_errors(events)
  assert out["by_app"]["7"]["count"] == 2
  assert out["by_app"]["9"]["count"] == 1
  assert out["shell"]["count"] == 0


def test_no_app_id_is_a_shell_error():
  out = summarize_app_errors([_err(app_id=None), _err(app_id=5)])
  assert out["shell"]["count"] == 1
  assert "5" in out["by_app"]
  # The shell error must NOT leak into any app bucket.
  assert out["by_app"]["5"]["count"] == 1


def test_stacks_are_dropped_messages_capped():
  big_stack = "x" * 9000
  long_msg = "m" * 1000
  out = summarize_app_errors([_err(app_id=1, message=long_msg, stack=big_stack)])
  entry = out["by_app"]["1"]["recent"][0]
  assert "stack" not in entry, "stacks must be dropped to keep the digest compact"
  assert len(entry["message"]) == 500, "message capped at 500 chars"
  assert entry["where"] == "render"


def test_recent_window_capped_keeps_most_recent():
  events = [_err(app_id=1, message=f"e{i}", ts=f"t{i}") for i in range(8)]
  out = summarize_app_errors(events, recent_per_app=5)
  recent = out["by_app"]["1"]["recent"]
  assert out["by_app"]["1"]["count"] == 8  # count is the full total
  assert len(recent) == 5  # but only the last 5 are kept
  assert [e["message"] for e in recent] == ["e3", "e4", "e5", "e6", "e7"]


def test_non_app_error_events_ignored():
  events = [
    {"ev": "app_open", "app_id": 1},
    {"ev": "chat_sent", "chat_id": "c1"},
    _err(app_id=1),
    "not-a-dict",
  ]
  out = summarize_app_errors(events)
  assert out["by_app"]["1"]["count"] == 1
  assert out["shell"]["count"] == 0
