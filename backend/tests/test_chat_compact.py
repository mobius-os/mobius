"""Compaction endpoint: POST /api/chats/{id}/compact.

The summarize turn is the one step that talks to a provider, so every test
here stubs `app.compaction.summarize_chat` (the route imports it lazily, so
patching the module attribute takes effect) — no live SDK, no subprocess.
The route's contract under test: a successful summarize STORES a recognizable
compaction block via the writer actor and RETURNS the summary; a failed
summarize stores NOTHING and surfaces a non-2xx.
"""

import pytest

from app import compaction


def _make_chat_with_messages(client, auth, messages):
  """Create a chat and seed it with `messages` via the transcript PUT."""
  chat_id = client.post(
    "/api/chats", json={"title": "Compact me"}, headers=auth
  ).json()["id"]
  client.put(
    f"/api/chats/{chat_id}",
    json={"messages": messages},
    headers=auth,
  )
  return chat_id


def test_compact_stores_and_returns_summary(client, auth, monkeypatch):
  """A successful summarize stores a compaction block and returns the text."""
  async def _stub(messages, *, data_dir):
    return "Goal: build X. Done: scaffolded. Next: wire the API."

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(
    client,
    auth,
    [
      {"role": "user", "content": "Build me an app"},
      {"role": "assistant", "content": "Sure, scaffolded it."},
    ],
  )

  r = client.post(f"/api/chats/{chat_id}/compact", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert body["ok"] is True
  assert "Goal: build X" in body["summary"]
  assert body["stored"]["kind"] == "compaction"
  # The stored block carries a ts so it has a stable React key.
  assert isinstance(body["stored"].get("ts"), int)

  # The block is durable in the transcript as its own assistant message.
  chat = client.get(
    f"/api/chats/{chat_id}?limit=50", headers=auth
  ).json()
  compaction_msgs = [
    m for m in chat["messages"] if m.get("kind") == "compaction"
  ]
  assert len(compaction_msgs) == 1
  assert compaction_msgs[-1]["content"] == body["summary"]
  # The prior turns are still present — compaction APPENDS, it does not wipe.
  assert any(
    m.get("content") == "Build me an app" for m in chat["messages"]
  )


def test_compact_does_not_store_on_failed_summarize(
  client, auth, monkeypatch
):
  """A summarize that raises CompactionError stores nothing and 422s."""
  async def _stub(messages, *, data_dir):
    raise compaction.CompactionError("Nothing to compact.")

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(
    client, auth, [{"role": "user", "content": "hi"}]
  )

  r = client.post(f"/api/chats/{chat_id}/compact", headers=auth)
  assert r.status_code == 422
  # No compaction block was written — the transcript is unchanged.
  chat = client.get(
    f"/api/chats/{chat_id}?limit=50", headers=auth
  ).json()
  assert not any(
    m.get("kind") == "compaction" for m in chat["messages"]
  )


def test_compact_maps_unexpected_summarize_error_to_502(
  client, auth, monkeypatch
):
  """An unexpected summarize crash maps to 502 and stores nothing."""
  async def _stub(messages, *, data_dir):
    raise RuntimeError("provider exploded")

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  chat_id = _make_chat_with_messages(
    client, auth, [{"role": "user", "content": "hi"}]
  )

  r = client.post(f"/api/chats/{chat_id}/compact", headers=auth)
  assert r.status_code == 502
  chat = client.get(
    f"/api/chats/{chat_id}?limit=50", headers=auth
  ).json()
  assert not any(
    m.get("kind") == "compaction" for m in chat["messages"]
  )


def test_compact_unknown_chat_404(client, auth, monkeypatch):
  """Compacting a non-existent chat is a 404 before any summarize runs."""
  async def _stub(messages, *, data_dir):  # pragma: no cover - never called
    raise AssertionError("summarize should not run for a missing chat")

  monkeypatch.setattr(compaction, "summarize_chat", _stub)
  r = client.post("/api/chats/does-not-exist/compact", headers=auth)
  assert r.status_code == 404


def test_build_transcript_text_drops_empty_and_tail_caps():
  """The transcript builder skips empty messages and keeps the tail."""
  msgs = [
    {"role": "user", "content": "first"},
    {"role": "assistant", "content": "   "},  # whitespace-only → dropped
    {"role": "assistant", "content": "second"},
  ]
  text = compaction.build_transcript_text(msgs)
  assert "USER: first" in text
  assert "ASSISTANT: second" in text
  assert text.count("ASSISTANT:") == 1  # the blank one was dropped

  # Tail-cap: a transcript past the cap keeps only the most recent chars.
  big = [{"role": "user", "content": "x" * (compaction._MAX_TRANSCRIPT_CHARS + 100)}]
  capped = compaction.build_transcript_text(big)
  assert len(capped) == compaction._MAX_TRANSCRIPT_CHARS
