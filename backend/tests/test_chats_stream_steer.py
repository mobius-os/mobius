"""Mid-turn steering on the send path (feature 087).

Möbius normally appends every send-while-running to `pending_messages`
and drains it at turn-end. For chats with `steer_enabled` set (DEFAULT
OFF), a send that arrives while a turn is streaming is steered into the
live provider handle. Codex uses true SDK injection; Claude interrupts
and re-prompts on the same connected SDK client.

These tests pin the provider-gated branch in
`routes/chats_stream.send_message`:

  1. provider + running + flag-on + live turn → steer is called, the
     message is appended to the TRANSCRIPT (not pending), a
     `steered_into_turn` event is broadcast, and the response is
     `{"status": "steered"}`.
  2. flag OFF → falls back to the queue (the default; deploy-safe).
  3. Claude with the flag on uses its live-client fallback.
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


def _make_active_claude_client(chat_id: str):
  """Builds a real `ActiveClaudeClient` so the route gate passes."""
  from app.claude_sdk_runner import ActiveClaudeClient

  class _Client:
    async def interrupt(self):
      return None

  async def _build():
    return ActiveClaudeClient(_Client(), chat_id=chat_id)

  return asyncio.run(_build())


def _make_codex_chat(
  chat_id: str, *, steer_enabled: bool, legacy_flag: bool = False,
) -> None:
  """Persist a Codex chat with one assistant partial mid-turn.

  The assistant message is the in-progress turn's partial; a steered
  user message must land just before it so the runner's snapshot /
  finalize writes keep targeting the assistant as `messages[-1]`.
  """
  settings = {}
  if steer_enabled:
    key = "codex_steer_enabled" if legacy_flag else "steer_enabled"
    settings = {key: True}
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
      agent_settings_json=settings,
    )
    db.add(chat)
    db.commit()
  finally:
    db.close()


def _make_claude_chat(chat_id: str, *, steer_enabled: bool) -> None:
  """Persist a Claude chat with one assistant partial mid-turn."""
  db = SessionLocal()
  try:
    chat = models.Chat(
      id=chat_id,
      title="Claude chat",
      provider="claude",
      messages=[
        {"role": "user", "content": "start", "ts": 1},
        {"role": "assistant", "content": "working", "ts": 2, "blocks": []},
      ],
      agent_settings_json=(
        {"steer_enabled": True} if steer_enabled else {}
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


def test_force_steer_consumes_existing_queued_messages(
  client, auth, monkeypatch,
):
  """Stop can collapse queued rows into a steer even when the normal
  steer flag is off, and only the named queued rows are consumed."""
  chat_id = "codexforcesteer"
  _make_codex_chat(chat_id, steer_enabled=False)
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    chat.pending_messages = [
      {"role": "user", "content": "use blue", "ts": 10},
      {"role": "user", "content": "also square", "ts": 11},
      {"role": "user", "content": "later", "ts": 12},
    ]
    db.commit()
  finally:
    db.close()
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
    json={
      "content": "use blue\nalso square",
      "force_steer": True,
      "consume_pending_ts": [10, 11],
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  assert steered_calls == [(chat_id, "use blue\nalso square")]
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["later"]
  assert chat.messages[-2]["content"] == "use blue\nalso square"


def test_force_steer_failure_does_not_append_duplicate_queue(
  client, auth, monkeypatch,
):
  """A forced steer attempt is a conversion attempt, not a new queue send."""
  chat_id = "codexforcenope"
  _make_codex_chat(chat_id, steer_enabled=False)
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    chat.pending_messages = [
      {"role": "user", "content": "use blue", "ts": 10},
    ]
    db.commit()
  finally:
    db.close()
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _fake_steer(_cid, _message):
    return False

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fake_steer,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "use blue",
      "force_steer": True,
      "consume_pending_ts": [10],
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "not_steered"
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["use blue"]


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


def test_legacy_codex_steer_flag_still_enables_steering(
  client, auth, monkeypatch,
):
  """Existing `codex_steer_enabled` opt-ins keep working after rename."""
  chat_id = "codexlegacy"
  _make_codex_chat(chat_id, steer_enabled=True, legacy_flag=True)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _fake_steer(cid, message):
    return cid == chat_id and message == "legacy flag"

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fake_steer,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "legacy flag"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"


def test_steers_into_live_claude_turn_when_flag_on(
  client, auth, monkeypatch,
):
  """claude + running + flag-on + live client: steer called, transcript
  append, `steered_into_turn` broadcast, response status `steered`."""
  chat_id = "claudechat"
  _make_claude_chat(chat_id, steer_enabled=True)
  registry.register(_make_active_claude_client(chat_id))
  create_broadcast(chat_id)

  steered_calls = []

  async def _fake_steer(cid, message):
    steered_calls.append((cid, message))
    return True

  monkeypatch.setattr(
    "app.claude_sdk_runner.steer_into_active_turn", _fake_steer,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "actually use blue"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  assert steered_calls == [(chat_id, "actually use blue")]
  chat = _read_chat(chat_id)
  assert chat.pending_messages in (None, [])
  assert [m["role"] for m in chat.messages] == [
    "user", "user", "assistant",
  ]
  assert chat.messages[-2]["content"] == "actually use blue"
  bc = get_broadcast(chat_id)
  steered_events = [
    e for e in bc.event_log if e.get("type") == "steered_into_turn"
  ]
  assert len(steered_events) == 1
  assert steered_events[0]["content"] == "actually use blue"


def test_claude_falls_back_to_queue_when_flag_off(
  client, auth, monkeypatch,
):
  """Claude steering is deploy-safe: no flag means normal queueing."""
  chat_id = "claudenoflag"
  _make_claude_chat(chat_id, steer_enabled=False)
  registry.register(_make_active_claude_client(chat_id))
  create_broadcast(chat_id)

  async def _fail_if_called(cid, message):
    raise AssertionError("steer must not be called when the flag is off")

  monkeypatch.setattr(
    "app.claude_sdk_runner.steer_into_active_turn", _fail_if_called,
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


def test_claude_falls_back_to_queue_when_steer_raises(
  client, auth, monkeypatch,
):
  """Claude steer failure is best-effort and falls back to the queue."""
  chat_id = "clauderaise"
  _make_claude_chat(chat_id, steer_enabled=True)
  registry.register(_make_active_claude_client(chat_id))
  create_broadcast(chat_id)

  async def _steer_raises(cid, message):
    raise RuntimeError("SDK blew up")

  monkeypatch.setattr(
    "app.claude_sdk_runner.steer_into_active_turn", _steer_raises,
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
