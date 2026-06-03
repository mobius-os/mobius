"""Mid-turn Codex steering on the send path (feature 087).

Möbius normally appends every send-while-running to `pending_messages`
and drains it at turn-end. For Codex chats with `codex_steer_enabled`
set (DEFAULT OFF), a send that arrives while a turn is streaming is
instead injected INTO the live turn via the SDK's `steer()` — the user
message lands in the transcript (not the queue) and a
`steered_into_turn` event tells the client to render it inline.

These tests pin the provider-gated branch in
`routes/chats_stream.send_message`:

  1. codex + running + flag-on + live turn → steer is called, the
     message is appended to the TRANSCRIPT (not pending), a
     `steered_into_turn` event is broadcast, and the response is
     `{"status": "steered"}`.
  2. flag OFF → falls back to the queue (the default; deploy-safe).
  3. provider != codex → falls back to the queue (Claude keeps
     turn-end drain even with the flag on).
  4. steer returns False (no live turn / closed-turn race) → queue.
  5. steer raises → queue (a steer failure must never break a send).

The steering primitive itself (the SDK `TurnHandle.steer()` wrapper) is
covered by `test_codex_sdk_runner.py`; here we only exercise the wiring.
"""

import asyncio

from app import models
from app.broadcast import create_broadcast, get_broadcast
from app.database import SessionLocal
from app.runner_registry import RunnerKind, registry


def _make_active_codex_turn(chat_id: str):
  """Builds a real `ActiveCodexTurn` so the route's isinstance gate passes.

  `ActiveCodexTurn.__init__` creates a loop-bound `_finished` future, so
  it must be constructed inside a running loop. The route only reads
  `.turn` (never `_finished`), so the object stays valid after the
  short-lived construction loop closes.
  """
  from app.codex_sdk_runner import ActiveCodexTurn

  async def _build():
    return ActiveCodexTurn(thread=object(), turn=object(), chat_id=chat_id)

  return asyncio.run(_build())


def _make_codex_chat(chat_id: str, *, steer_enabled: bool) -> None:
  """Persist a Codex chat with one assistant partial mid-turn.

  The assistant message is the in-progress turn's partial; a steered
  user message must land just before it so the runner's snapshot /
  finalize writes keep targeting the assistant as `messages[-1]`.
  """
  db = SessionLocal()
  try:
    chat = models.Chat(
      id=chat_id,
      title="Codex chat",
      provider="codex",
      messages=[
        {"role": "user", "content": "start", "ts": 1},
        {"role": "assistant", "content": "working", "ts": 2, "blocks": []},
      ],
      agent_settings_json=(
        {"codex_steer_enabled": True} if steer_enabled else {}
      ),
    )
    db.add(chat)
    db.commit()
  finally:
    db.close()


def _read_chat(chat_id: str) -> models.Chat:
  db = SessionLocal()
  try:
    return db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  finally:
    db.close()


def test_steers_into_live_codex_turn_when_flag_on(
  client, auth, monkeypatch,
):
  """codex + running + flag-on + live turn: steer called, transcript
  append, `steered_into_turn` broadcast, response status `steered`."""
  chat_id = "codexsteer"
  _make_codex_chat(chat_id, steer_enabled=True)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  steered_calls = []

  async def _fake_steer(cid, message):
    steered_calls.append((cid, message))
    return True

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fake_steer,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "actually use blue"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  # The SDK steer was invoked with the message content.
  assert steered_calls == [(chat_id, "actually use blue")]

  # The message landed in the TRANSCRIPT, not the pending queue, and just
  # BEFORE the trailing assistant partial (so the assistant stays last).
  chat = _read_chat(chat_id)
  assert chat.pending_messages in (None, [])
  roles = [m["role"] for m in chat.messages]
  # The steered user row lands just before the trailing assistant partial
  # (start-user, steered-user, assistant-partial), keeping the assistant
  # last so the runner's snapshot / finalize writes still target it.
  assert roles == ["user", "user", "assistant"]
  assert chat.messages[-2]["content"] == "actually use blue"
  assert chat.messages[-1]["role"] == "assistant"

  # A `steered_into_turn` event was broadcast for the inline render.
  bc = get_broadcast(chat_id)
  steered_events = [
    e for e in bc.event_log if e.get("type") == "steered_into_turn"
  ]
  assert len(steered_events) == 1
  assert steered_events[0]["content"] == "actually use blue"


def test_falls_back_to_queue_when_flag_off(client, auth, monkeypatch):
  """Flag OFF (the default): a steerable Codex turn still queues —
  deploying the feature changes nothing until the owner opts in."""
  chat_id = "codexnoflag"
  _make_codex_chat(chat_id, steer_enabled=False)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _fail_if_called(cid, message):
    raise AssertionError("steer must not be called when the flag is off")

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fail_if_called,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "queued please"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "queued"
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["queued please"]


def test_falls_back_to_queue_for_non_codex(client, auth, db, monkeypatch):
  """provider != codex: Claude keeps turn-end drain even with the flag
  on and a (notionally) live turn — steer is Codex-only."""
  chat_id = "claudechat"
  chat = models.Chat(
    id=chat_id,
    title="Claude chat",
    provider="claude",
    messages=[
      {"role": "user", "content": "start", "ts": 1},
      {"role": "assistant", "content": "working", "ts": 2, "blocks": []},
    ],
    agent_settings_json={"codex_steer_enabled": True},
  )
  db.add(chat)
  db.commit()
  registry.mark_starting(chat_id)  # makes is_chat_running True
  create_broadcast(chat_id)

  async def _fail_if_called(cid, message):
    raise AssertionError("steer must not be called for a Claude chat")

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fail_if_called,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "queued please"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "queued"
  assert [m["content"] for m in _read_chat(chat_id).pending_messages] == [
    "queued please"
  ]


def test_falls_back_to_queue_when_steer_returns_false(
  client, auth, monkeypatch,
):
  """steer() returns False (no live turn / closed-turn race): the send
  falls through to the existing queue rather than being lost."""
  chat_id = "codexfalse"
  _make_codex_chat(chat_id, steer_enabled=True)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _steer_false(cid, message):
    return False

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _steer_false,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "queued please"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "queued"
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["queued please"]
  # No steered event was broadcast.
  bc = get_broadcast(chat_id)
  assert not [
    e for e in bc.event_log if e.get("type") == "steered_into_turn"
  ]


def test_falls_back_to_queue_when_steer_raises(client, auth, monkeypatch):
  """steer() raising must NEVER break a send: it falls back to the
  queue (steering is best-effort)."""
  chat_id = "codexraise"
  _make_codex_chat(chat_id, steer_enabled=True)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _steer_raises(cid, message):
    raise RuntimeError("SDK blew up")

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _steer_raises,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "queued please"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "queued"
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["queued please"]
