"""Per-call receipt timing, without provider or database I/O."""

from app.chat_event_sink import ChatEventSink
from app.events import build_assistant_message, finalize_blocks


class Broadcast:
  def publish(self, event):
    pass


def sink(monkeypatch):
  clock = [0.0]
  monkeypatch.setattr("app.chat_event_sink.time.monotonic", lambda: clock[0])
  return ChatEventSink(Broadcast(), "", None), clock


def test_interleaved_exact_ids_and_explicit_outcomes(monkeypatch):
  owner, clock = sink(monkeypatch)
  owner.publish({"type": "tool_start", "tool": "Bash", "tool_use_id": "a", "input": "SECRET"})
  clock[0] = 1
  owner.publish({"type": "tool_start", "tool": "MCP", "tool_use_id": "b"})
  clock[0] = 2
  owner.publish({"type": "tool_output", "tool_use_id": "b", "content": "PRIVATE",
                 "output_complete": True, "output_exit_code": 0})
  clock[0] = 3
  owner.publish({"type": "tool_end", "tool_use_id": "b"})
  clock[0] = 4
  owner.publish({"type": "tool_output", "tool_use_id": "a", "content": "failed",
                 "output_complete": True, "output_exit_code": 7})
  clock[0] = 5
  owner.publish({"type": "tool_end", "tool_use_id": "a"})
  blocks = build_assistant_message(owner.assistant_blocks)["blocks"]
  assert blocks[0]["observation"] == {"observed_duration_ms": 5000, "outcome": "failed"}
  assert blocks[1]["observation"] == {"observed_duration_ms": 2000, "outcome": "succeeded"}
  assert all("PRIVATE" not in str(block["observation"]) and
             "SECRET" not in str(block["observation"]) for block in blocks)


def test_unknown_missing_start_duplicate_end_and_explicit_error(monkeypatch):
  owner, clock = sink(monkeypatch)
  owner.publish({"type": "tool_end", "tool_use_id": "missing"})
  owner.publish({"type": "tool_start", "tool": "Read", "tool_use_id": "r"})
  clock[0] = 1
  owner.publish({"type": "tool_end", "tool_use_id": "r"})
  assert owner.assistant_blocks[0]["observation"] == {
    "observed_duration_ms": 1000, "outcome": "unknown",
  }
  clock[0] = 9
  owner.publish({"type": "tool_end", "tool_use_id": "r"})
  assert owner.assistant_blocks[0]["observation"]["observed_duration_ms"] == 1000
  owner.publish({"type": "tool_start", "tool": "MCP", "tool_use_id": "e"})
  owner.publish({"type": "tool_output", "tool_use_id": "e", "content": "private",
                 "is_error": True})
  owner.publish({"type": "tool_end", "tool_use_id": "e"})
  assert owner.assistant_blocks[1]["observation"]["outcome"] == "failed"


def test_quiet_and_stop_do_not_manufacture_observation(monkeypatch):
  owner, clock = sink(monkeypatch)
  owner.publish({"type": "tool_start", "tool": "checkpoint_chat",
                 "tool_use_id": "q", "delivery": "quiet"})
  clock[0] = 2
  owner.publish({"type": "tool_end", "tool_use_id": "q"})
  assert "observation" not in owner.assistant_blocks[0]
  owner.publish({"type": "tool_start", "tool": "Bash", "tool_use_id": "open"})
  finalize_blocks(owner.assistant_blocks)
  assert owner.assistant_blocks[1]["status"] == "done"
  assert "observation" not in owner.assistant_blocks[1]


def test_legacy_idless_and_boolean_exit_are_not_guessed(monkeypatch):
  owner, clock = sink(monkeypatch)
  owner.publish({"type": "tool_start", "tool": "legacy"})
  clock[0] = 0.5
  owner.publish({"type": "tool_output", "content": "ok", "output_exit_code": False})
  owner.publish({"type": "tool_end"})
  assert owner.assistant_blocks[0]["observation"] == {
    "observed_duration_ms": 500, "outcome": "unknown",
  }


def test_observation_survives_actor_persistence_and_open_tool_remains_unknown(chat, db, monkeypatch):
  import asyncio
  from app import models
  from tests.test_agent_write_sink import sink_for

  async def scenario():
    owner = sink_for(chat, monkeypatch, [])
    owner.publish({"type": "tool_start", "tool": "MCP", "tool_use_id": "ended"})
    owner.publish({"type": "tool_output", "tool_use_id": "ended", "content": "ok",
                   "output_complete": True, "output_exit_code": 0})
    owner.publish({"type": "tool_end", "tool_use_id": "ended"})
    owner.publish({"type": "tool_start", "tool": "MCP", "tool_use_id": "interrupted"})
    owner.end_provider_stream()
    await owner.finalize()
    db.expire_all()
    messages = db.get(models.Chat, chat.id).messages
    blocks = [b for m in messages for b in m.get("blocks", []) if b.get("type") == "tool"]
    assert blocks[0]["observation"]["outcome"] == "succeeded"
    assert blocks[0]["observation"]["observed_duration_ms"] >= 0
    assert "observation" not in blocks[1]
  asyncio.run(scenario())
