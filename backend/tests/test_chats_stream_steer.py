"""Mid-turn steering on the send path (feature 087).

Möbius normally appends every send-while-running to `pending_messages`
and drains it at turn-end. For chats with `steer_enabled` set (DEFAULT
OFF), an ordinary send that arrives while a turn is streaming is steered
into the live provider handle. `direct_steer` explicitly requests the same
durable reserve-and-steer operation for a new composer row regardless of
that flag; `force_steer` converts already-queued rows by stable `cid`. Every
path reserves the row durably before delivery. Both live handles admit those
rows immediately and own the eventual provider acknowledgement plus transcript
cut outside the route lock.

These tests pin the provider-gated branch in
`routes/chats_stream.send_message`:

  1. provider + running + flag-on + live turn → steer is called, the
     message remains reserved until the runner-owned cut, a
     `steered_into_turn` event is broadcast at that cut, and the response is
     `{"status": "steered", "cut_deferred": true}`.
  2. flag OFF → falls back to the queue (the default; deploy-safe).
  3. Claude with the flag on uses its live-client fallback.
  4. steer returns False (no live turn / closed-turn race) → queue.
  5. steer raises → queue (a steer failure must never break a send).
  6. direct_steer reserves a new cid before delivery; success converts it
     without a queue round-trip, while failure returns the reserved row as
     the ordinary queued fallback.

The steering primitive itself (the SDK `TurnHandle.steer()` wrapper) is
covered by `test_codex_sdk_runner.py`; here we only exercise the wiring.
"""

import asyncio
from concurrent.futures import Future

import pytest

from app import models, questions
from app.broadcast import create_broadcast, get_broadcast
from app.chat_writer import cid_of
from app.database import SessionLocal
from app.pending_questions import PendingQuestion
from app.runner_registry import RunnerKind, registry
from app.memory_recall import EMPTY_RECALL_BINDING


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


def _patch_codex_steer(monkeypatch, steer) -> None:
  """Replace provider I/O while synchronously settling route-wiring tests.

  The real ActiveCodexTurn owns this commit in a background task; these tests
  exercise route selection rather than task scheduling, so settle through the
  same chat-owned helper before returning to keep their DB assertions direct.
  """
  async def _handle_steer(
    self, message, user_msgs=None, consume_pending_cids=None,
  ):
    accepted = await steer(self.chat_id, message)
    if accepted and user_msgs and consume_pending_cids:
      from app.chat import commit_steer_cut
      await commit_steer_cut(
        self.chat_id, user_msgs, consume_pending_cids,
      )
    return accepted

  monkeypatch.setattr(
    "app.codex_sdk_runner.ActiveCodexTurn.steer", _handle_steer,
  )


def _patch_claude_steer(monkeypatch, steer) -> None:
  """Replace Claude's handle-owned steer method for route wiring tests."""
  async def _handle_steer(
    self, message, user_msgs=None, consume_pending_cids=None,
  ):
    return await steer(
      self.chat_id, message, user_msgs, consume_pending_cids,
    )

  monkeypatch.setattr(
    "app.claude_sdk_runner.ActiveClaudeClient.steer", _handle_steer,
  )


def _make_codex_chat(chat_id: str, *, steer_enabled: bool) -> None:
  """Persist a Codex chat with one assistant partial mid-turn.

  The assistant message is the in-progress turn's partial; a steered
  user message must land just before it so the runner's snapshot /
  finalize writes keep targeting the assistant as `messages[-1]`.
  """
  settings = {"model": "gpt-5.6-sol"}
  if steer_enabled:
    settings["steer_enabled"] = True
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
      agent_settings_json={
        "model": "claude-opus-4-8",
        **({"steer_enabled": True} if steer_enabled else {}),
      },
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

  async def _fake_steer(cid, message, *_durable):
    reserved = _read_chat(chat_id).pending_messages
    assert [row["content"] for row in reserved] == ["actually use blue"]
    steered_calls.append((cid, message))
    return True

  _patch_codex_steer(monkeypatch, _fake_steer)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "actually use blue"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  # The SDK steer was invoked with the message content.
  assert steered_calls == [(chat_id, "actually use blue")]

  # The message landed in the TRANSCRIPT, not the pending queue, and at the
  # END (no live sink in this wiring test → the fallback append-at-end path).
  chat = _read_chat(chat_id)
  assert chat.pending_messages in (None, [])
  roles = [m["role"] for m in chat.messages]
  # The steered user row lands at the END (start-user, assistant-partial,
  # steered-user). The split that seals A1 and re-orders to Q1/A1/Q2/A2 is
  # driven by the live sink — exercised in
  # test_steer_splits_assistant_turn_for_reload_order; here no sink is
  # registered, so the fallback simply appends the user row.
  assert roles == ["user", "assistant", "user"]
  assert chat.messages[-1]["content"] == "actually use blue"
  assert chat.messages[-1]["role"] == "user"
  assert chat.messages[-1]["steered"] is True

  # A `steered_into_turn` event was broadcast for the inline render.
  bc = get_broadcast(chat_id)
  steered_events = [
    e for e in bc.event_log if e.get("type") == "steered_into_turn"
  ]
  assert len(steered_events) == 1
  assert steered_events[0]["content"] == "actually use blue"
  assert steered_events[0]["messages"] == [
    {
      "role": "user",
      "ts": chat.messages[-1]["ts"],
      "cid": cid_of(chat.messages[-1]),
      "content": "actually use blue",
      "steered": True,
    }
  ]


def test_hidden_control_message_queues_even_when_auto_steer_is_enabled(
  client, auth, monkeypatch,
):
  """Product control carriers must retain their next-turn boundary."""
  chat_id = "hiddencontrolqueues"
  message_cid = "hidden-control-cid"
  _make_codex_chat(chat_id, steer_enabled=True)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _must_not_steer(*_args, **_kwargs):
    raise AssertionError("hidden control messages must not auto-steer")

  _patch_codex_steer(monkeypatch, _must_not_steer)
  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "/goal Finish and verify the migration",
      "cid": message_cid,
      "hidden": True,
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "queued"
  chat = _read_chat(chat_id)
  assert [cid_of(row) for row in chat.pending_messages] == [message_cid]
  assert chat.pending_messages[0]["hidden"] is True
  assert chat.pending_messages[0]["content"].startswith("/goal ")


def test_direct_steer_reserves_and_converts_new_codex_message_in_one_request(
  client, auth, monkeypatch,
):
  """Cmd/Ctrl+Enter must not require a visible queue acknowledgement first."""
  chat_id = "codexdirectsteer"
  message_cid = "direct-steer-cid"
  _make_codex_chat(chat_id, steer_enabled=False)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)
  steered_calls = []

  async def _fake_steer(cid, message, *_durable):
    reserved = _read_chat(chat_id).pending_messages
    assert [cid_of(row) for row in reserved] == [message_cid]
    assert [row["content"] for row in reserved] == ["change course now"]
    steered_calls.append((cid, message))
    return True

  _patch_codex_steer(monkeypatch, _fake_steer)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "change course now",
      "cid": message_cid,
      "direct_steer": True,
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  assert steered_calls == [(chat_id, "change course now")]
  chat = _read_chat(chat_id)
  assert chat.pending_messages in (None, [])
  assert [cid_of(row) for row in chat.messages].count(message_cid) == 1
  assert chat.messages[-1]["content"] == "change course now"


def test_direct_steer_failure_reveals_single_reserved_queue_fallback(
  client, auth, monkeypatch,
):
  """A closed-turn race preserves the message without a second POST."""
  chat_id = "codexdirectfallback"
  message_cid = "direct-fallback-cid"
  _make_codex_chat(chat_id, steer_enabled=False)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _closed_turn(_chat_id, _message, *_durable):
    return False

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _closed_turn,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "keep this even if steering races",
      "cid": message_cid,
      "direct_steer": True,
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  body = res.json()
  assert body["status"] == "queued"
  assert body["position"] == 1
  assert cid_of(body["pending_message"]) == message_cid
  chat = _read_chat(chat_id)
  assert [cid_of(row) for row in chat.pending_messages] == [message_cid]
  assert not [row for row in chat.messages if cid_of(row) == message_cid]


def test_direct_claude_steer_keeps_reserve_until_deferred_cut(
  client, auth,
):
  """The one-request contract also preserves Claude's real cut boundary."""
  chat_id = "claudedirectsteer"
  message_cid = "claude-direct-cid"
  _make_claude_chat(chat_id, steer_enabled=False)
  handle = _make_active_claude_client(chat_id)
  registry.register(handle)
  create_broadcast(chat_id)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "change Claude course now",
      "cid": message_cid,
      "direct_steer": True,
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  assert res.json()["cut_deferred"] is True
  chat = _read_chat(chat_id)
  assert [cid_of(row) for row in chat.pending_messages] == [message_cid]
  assert [cid_of(row) for row in handle._steer_user_msgs] == [message_cid]
  assert handle._steer_consume_cids == [message_cid]


def test_pending_question_refuses_force_steer_without_holding_queue(
  client, auth, monkeypatch,
):
  """A synchronous question owns the provider control channel.

  Force-steer must fail before it calls that blocked channel or takes the
  transition/queue locks; the durable queued row and live question remain
  available for Answer or Stop.
  """
  chat_id = "questionsteer"
  question_id = "q-open"
  _make_codex_chat(chat_id, steer_enabled=False)
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    chat.messages[-1]["blocks"] = [{
      "type": "question",
      "question_id": question_id,
      "questions": [{"question": "Keep going?", "options": [{"label": "Yes"}]}],
    }]
    chat.pending_messages = [{
      "role": "user",
      "content": "Skip that and use the default",
      "ts": 10,
      "cid": "question-steer-cid",
    }]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(chat, "messages")
    db.commit()
  finally:
    db.close()

  waiting = Future()
  questions.register(chat_id, PendingQuestion(
    question_id=question_id,
    questions=[],
    future=waiting,
  ))
  registry.register(_make_active_codex_turn(chat_id))

  async def _fail_if_called(_cid, _message, *_durable):
    raise AssertionError("pending QA must block provider steer")

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fail_if_called,
  )
  monkeypatch.setattr(
    "app.chat_queue.get_transition_lock",
    lambda _chat_id: (_ for _ in ()).throw(
      AssertionError("force-steer refusal must happen before chat locks")
    ),
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "Skip that and use the default",
      "force_steer": True,
      "consume_pending_cids": ["question-steer-cid"],
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "not_steered"
  assert not waiting.done()
  assert questions.get(chat_id) is not None
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == [
    "Skip that and use the default",
  ]
  assert chat.messages[-1]["role"] == "assistant"
  questions.cancel(chat_id)


def test_question_registered_during_append_blocks_ordinary_auto_steer(
  client, auth, monkeypatch,
):
  """Re-check after AppendPending closes the registration race."""
  chat_id = "questionautosteer"
  _make_codex_chat(chat_id, steer_enabled=True)
  waiting = Future()
  registry.register(_make_active_codex_turn(chat_id))

  from app.routes import chats_stream
  original_append = chats_stream._append_to_pending

  async def _append_then_register(*args, **kwargs):
    stored = await original_append(*args, **kwargs)
    questions.register(chat_id, PendingQuestion(
      question_id="q-auto",
      questions=[],
      future=waiting,
    ))
    return stored

  monkeypatch.setattr(chats_stream, "_append_to_pending", _append_then_register)

  async def _fail_if_called(_cid, _message, *_durable):
    raise AssertionError("pending QA must block provider auto-steer")

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fail_if_called,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "queue this instead", "cid": "qa-queued-cid"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "queued"
  assert not waiting.done()
  assert [m["content"] for m in _read_chat(chat_id).pending_messages] == [
    "queue this instead",
  ]
  questions.cancel(chat_id)


def _register_sink_with_partial(chat_id: str, run_token: str, text: str):
  """Register a live `_ChatEventSink` mid-turn carrying `text` as A1.

  Mirrors production: the runner's sink owns `assistant_blocks` and is
  reachable from the steer route via the per-chat sink registry, so the
  route can split the turn (seal A1, append the steered user message,
  reset for A2). The sink is built inside a short-lived loop because the
  writer-actor commands it submits resolve their acks on whichever loop
  runs them; the route drives it on its own request loop.
  """
  from app.chat import _ChatEventSink, register_active_sink
  from app.events import process_event

  bc = create_broadcast(chat_id)
  sink = _ChatEventSink(bc, chat_id, run_token=run_token, recall_binding=EMPTY_RECALL_BINDING)
  process_event({"type": "text", "content": text}, sink.assistant_blocks)
  register_active_sink(chat_id, sink)
  return sink


def test_steer_drops_empty_pre_steer_partial(client, auth, monkeypatch):
  """A steer landing before any real output must not seal a stray empty A1.

  Card 166: when only a whitespace/empty token streamed before the steer cut
  over, the old seal committed an empty assistant message (A1) between Q1 and
  Q2 — a stray orphaned fragment on reload. The fix skips the seal when the
  pre-steer segment has no renderable content, so the transcript stays Q1, Q2
  (no empty assistant row) and the post-steer continuation (A2) lands as the
  turn's first real assistant message. A single REAL token would still seal —
  this only drops the empty/whitespace case.
  """
  chat_id = "emptysteer"
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id,
      title="Codex chat",
      provider="codex",
      messages=[{"role": "user", "content": "Q1", "ts": 1}],
      agent_settings_json={"model": "gpt-5.6-sol", "steer_enabled": True},
    ))
    db.commit()
  finally:
    db.close()
  registry.register(_make_active_codex_turn(chat_id))
  run_token = "run-empty"
  # Only a whitespace token streamed before the steer landed.
  sink = _register_sink_with_partial(chat_id, run_token, " ")

  async def _fake_steer(cid, message, *_durable):
    return True

  _patch_codex_steer(monkeypatch, _fake_steer)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "Q2"},
    headers=auth,
  )
  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"

  # No stray empty assistant row was sealed between Q1 and Q2.
  chat = _read_chat(chat_id)
  assert [(m["role"], m.get("content")) for m in chat.messages] == [
    ("user", "Q1"),
    ("user", "Q2"),
  ]

  # The post-steer continuation lands as the turn's first real assistant
  # message, after Q2 — not merged into a phantom empty A1.
  async def _stream_a2():
    sink.publish({"type": "text", "content": "A2"})
    await sink.finalize()

  asyncio.run(_stream_a2())

  chat = _read_chat(chat_id)
  assert [(m["role"], m.get("content")) for m in chat.messages] == [
    ("user", "Q1"),
    ("user", "Q2"),
    ("assistant", "A2"),
  ]


def test_steer_splits_assistant_turn_for_reload_order(
  client, auth, monkeypatch,
):
  """Persisted order after a steer is Q1, A1, Q2, A2 — A1 and A2 are
  SEPARATE assistant messages with the steered user message between them.

  Before the split fix the route inserted Q2 just before a single merged
  A1A2 assistant message, so a reload showed Q1, Q2, A1A2 (mis-ordered);
  the live view was correct but the transcript was not. The runner-/route-
  serialized split seals A1 as the trailing assistant, appends Q2 at the
  END, and resets the sink so the post-steer continuation (A2) accumulates
  into a fresh assistant message.
  """
  chat_id = "codexsplit"
  # Seed the transcript with only the user turn + the in-progress assistant
  # partial (A1). The sink, not the seed, owns A1's blocks.
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id,
      title="Codex chat",
      provider="codex",
      messages=[
        {"role": "user", "content": "Q1", "ts": 1},
        {"role": "assistant", "content": "A1", "ts": 2, "blocks": [
          {"type": "text", "content": "A1"},
        ]},
      ],
      agent_settings_json={"model": "gpt-5.6-sol", "steer_enabled": True},
    ))
    db.commit()
  finally:
    db.close()
  registry.register(_make_active_codex_turn(chat_id))
  run_token = "run-split"
  sink = _register_sink_with_partial(chat_id, run_token, "A1")

  async def _fake_steer(cid, message, *_durable):
    return True

  _patch_codex_steer(monkeypatch, _fake_steer)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "Q2"},
    headers=auth,
  )
  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"

  # After the split the transcript is Q1, A1, Q2 — A1 sealed as its own
  # assistant message, Q2 appended at the END (not inserted before A1).
  chat = _read_chat(chat_id)
  assert [(m["role"], m.get("content")) for m in chat.messages] == [
    ("user", "Q1"),
    ("assistant", "A1"),
    ("user", "Q2"),
  ]

  # The sink reset its blocks, so the post-steer continuation accumulates
  # fresh (publish() runs process_event) and lands as a NEW assistant
  # message rather than merging into A1.
  async def _stream_a2():
    sink.publish({"type": "text", "content": "A2"})
    await sink.finalize()

  asyncio.run(_stream_a2())

  chat = _read_chat(chat_id)
  assert [(m["role"], m.get("content")) for m in chat.messages] == [
    ("user", "Q1"),
    ("assistant", "A1"),
    ("user", "Q2"),
    ("assistant", "A2"),
  ]


def test_steer_enabled_honors_global_flag():
  """A GLOBAL `steer_enabled` in /data/shared/agent-settings.json enables
  steering.

  Regression: `_steer_enabled` read through `effective_agent_settings`, whose
  file layer only carries model/effort/effort_by_provider, so it silently
  DROPPED a global `steer_enabled: true` — steering stayed off despite the
  owner opting in ("not sure if steering works"). It now reads the flag
  directly, like `skills_enabled`.
  """
  import json
  import os
  from pathlib import Path

  from app.routes.chats_stream import _steer_enabled

  shared = Path(os.environ["DATA_DIR"]) / "shared"
  shared.mkdir(parents=True, exist_ok=True)
  gf = shared / "agent-settings.json"
  chat = models.Chat(
    id="gsteer", provider="claude",
    agent_settings_json={"model": "claude-opus-4-8"},
  )

  # No global flag → steering off (default).
  gf.write_text(json.dumps({"model": "claude-opus-4-8"}))
  assert _steer_enabled(chat) is False

  # Global flag on → steering ON even with no per-chat override.
  gf.write_text(json.dumps(
    {"model": "claude-opus-4-8", "steer_enabled": True}
  ))
  assert _steer_enabled(chat) is True

  # Per-chat override still works (and wins) with no global flag.
  gf.write_text(json.dumps({"model": "claude-opus-4-8"}))
  chat.agent_settings_json = {"steer_enabled": True}
  assert _steer_enabled(chat) is True


def test_seal_steer_split_retains_buffer_on_failure_and_delta_clears():
  """Adversarial hardening for `_seal_steer_split`:

  - a split FAILURE leaves the buffer intact so the turn-end finally retries
    (the client was already told 202 — the row must not be silently dropped);
  - on SUCCESS only the rows actually sealed are removed, so a second steer
    that lands during the (up to 30s) actor round-trip survives for the next
    call rather than being wiped by a wholesale reset.
  """
  from app.claude_sdk_runner import _seal_steer_split

  # Build the handle OUTSIDE the async body — `_make_active_claude_client`
  # itself calls asyncio.run, which can't nest inside asyncio.run(_run()).
  handle = _make_active_claude_client("sealunit")

  async def _run():
    handle._steer_user_msgs = [
      {"role": "user", "content": "Q2", "ts": 10, "cid": "c-q2"}
    ]
    handle._steer_consume_cids = []

    # 1) A failing split must NOT clear the buffer.
    class _FailBc:
      async def split_for_steer(self, rows, consume):
        raise RuntimeError("writer down")

    await _seal_steer_split(_FailBc(), handle, "sealunit")
    assert [m["content"] for m in handle._steer_user_msgs] == ["Q2"]

    # 2) A successful split removes only the sealed row; a steer that lands
    #    DURING the await survives.
    class _OkBc:
      def __init__(self):
        self.seen = None

      async def split_for_steer(self, rows, consume):
        self.seen = [m["content"] for m in rows]
        # A concurrent steer arrives while we await the writer acks.
        handle._steer_user_msgs.append(
          {"role": "user", "content": "Q3", "ts": 11}
        )

    bc = _OkBc()
    await _seal_steer_split(bc, handle, "sealunit")
    assert bc.seen == ["Q2"]
    assert [m["content"] for m in handle._steer_user_msgs] == ["Q3"]

  asyncio.run(_run())


def test_seal_publishes_the_cut_on_the_sinks_own_broadcast():
  """The cut goes to the broadcast the SINK holds, never to a fresh lookup.

  `_seal_steer_split` also runs from the turn-end `finally`, by which point a
  successor turn can already have registered a NEW broadcast for the same chat.
  Resolving by chat_id there would strand the cut in an event log no client is
  reading: A1's blocks live in the old log, so the client would never re-base
  and would paint the continuation onto the sealed segment for the rest of the
  turn. Also covers a leaner writer ack (no `stored_messages`): the cut still
  names the buffered rows rather than going out empty.
  """
  from app.broadcast import create_broadcast, get_broadcast
  from app.claude_sdk_runner import _seal_steer_split

  chat_id = "sealbroadcast"
  handle = _make_active_claude_client(chat_id)
  turn_bc = create_broadcast(chat_id)

  async def _run():
    handle._steer_user_msgs = [
      {"role": "user", "content": "Q2", "ts": 10, "cid": "c-q2"}
    ]
    handle._steer_consume_cids = ["c-q2"]

    class _SinkLike:
      """Mirrors `_ChatEventSink`: holds the broadcast it was built with."""

      def __init__(self, bc):
        self.bc = bc

      async def split_for_steer(self, rows, consume):
        # The writer ack shape without the echoed rows.
        return {"pending": []}

    # A successor turn registers its own broadcast before this seal runs.
    successor_bc = create_broadcast(chat_id)
    assert get_broadcast(chat_id) is successor_bc
    assert successor_bc is not turn_bc

    await _seal_steer_split(_SinkLike(turn_bc), handle, chat_id)

    assert [e.get("type") for e in successor_bc.event_log] == []
    cuts = [e for e in turn_bc.event_log if e.get("type") == "steered_into_turn"]
    assert len(cuts) == 1
    assert [m["content"] for m in cuts[0]["messages"]] == ["Q2"]
    assert [m["cid"] for m in cuts[0]["messages"]] == ["c-q2"]

  asyncio.run(_run())


def test_writer_dedup_still_publishes_the_committed_cut():
  """An empty stored-row echo does not undo the split that just committed.

  `split_for_steer` seals A1 and resets the sink BEFORE the writer appends the
  steered row. `stored_messages: []` means only that cid dedup found the row
  already in the durable transcript; it does not mean the A1/A2 boundary was
  skipped. Suppressing the cut here would leave the client appending A2 to the
  segment the server has already sealed. The handed row still supplies the cid
  that retires its tray entry and identifies the already-durable user turn.
  """
  from app.broadcast import create_broadcast
  from app.claude_sdk_runner import _seal_steer_split

  chat_id = "sealdedup"
  handle = _make_active_claude_client(chat_id)
  turn_bc = create_broadcast(chat_id)

  async def _run():
    row = {"role": "user", "content": "Q2", "ts": 10, "cid": "c-q2"}
    handle._steer_user_msgs = [row]
    handle._steer_consume_cids = ["c-q2"]

    class _SinkLike:
      def __init__(self, bc):
        self.bc = bc

      async def split_for_steer(self, rows, consume):
        assert rows == [row]
        assert consume == ["c-q2"]
        # The durable writer already has this cid, but the sink-side split still
        # sealed A1 and reset its accumulator for A2.
        return {"stored_messages": []}

    await _seal_steer_split(_SinkLike(turn_bc), handle, chat_id)

    cuts = [e for e in turn_bc.event_log if e.get("type") == "steered_into_turn"]
    assert len(cuts) == 1
    assert cuts[0]["messages"][0]["cid"] == "c-q2"
    assert handle._steer_user_msgs == []
    assert handle._steer_consume_cids == []

  asyncio.run(_run())


def test_a_failing_publisher_cannot_escape_the_seal():
  """Announcing the cut must never raise out of `_seal_steer_split`.

  The turn-end `finally` awaits this function BEFORE it unregisters the handle
  and disconnects the client, so an escaping exception would strand a live
  handle in the registry and leave the chat looking permanently busy. The split
  has already COMMITTED by the time the cut is published, so a broken publisher
  is a notification loss, not a durability one — exactly the asymmetry the
  missing-publisher branch already takes. Swallow it, log it, and still consume
  the sealed rows so the turn-end retry does not double-append them.
  """
  from app.claude_sdk_runner import _seal_steer_split

  chat_id = "sealpublishfail"
  handle = _make_active_claude_client(chat_id)

  async def _run():
    handle._steer_user_msgs = [
      {"role": "user", "content": "Q2", "ts": 10, "cid": "c-q2"}
    ]
    handle._steer_consume_cids = ["c-q2"]

    class _ExplodingBroadcast:
      def publish(self, event):
        raise RuntimeError("broadcast is gone")

    class _SinkLike:
      def __init__(self, bc):
        self.bc = bc
        self.splits = 0

      async def split_for_steer(self, rows, consume):
        self.splits += 1
        return {"stored_messages": list(rows)}

    sink = _SinkLike(_ExplodingBroadcast())
    await _seal_steer_split(sink, handle, chat_id)

    assert sink.splits == 1
    # The rows were committed, so they must not be re-appended by the retry.
    assert handle._steer_user_msgs == []
    assert handle._steer_consume_cids == []

  asyncio.run(_run())


def test_sink_commit_publish_failure_does_not_reclassify_committed_cut():
  """A committed Codex cut stays successful if only its broadcast is gone.

  Provider settlement treats a raised commit as "row remains queued". Once
  the writer has consumed that row, letting a publish exception escape would
  send the opposite outcome and invite a duplicate retry.
  """
  from app.chat import _ChatEventSink

  class _ExplodingBroadcast:
    def publish(self, event):
      raise RuntimeError("broadcast is gone")

  async def _run():
    row = {"role": "user", "content": "Q2", "ts": 10, "cid": "c-q2"}
    sink = _ChatEventSink(
      _ExplodingBroadcast(), "commit-publish-fail",
      recall_binding=EMPTY_RECALL_BINDING,
    )

    async def _committed_split(rows, consume):
      assert rows == [row]
      assert consume == ["c-q2"]
      return {"stored_messages": [row], "pending": []}

    sink.split_for_steer = _committed_split
    result = await sink.commit_steer_cut([row], ["c-q2"])
    assert result["stored_messages"] == [row]

  asyncio.run(_run())


def test_stop_drops_the_buffered_steer_instead_of_appending_it():
  """A hard Stop abandons a deferred steer ENTIRELY.

  Stop's contract: `/chat/stop` clears `chat.pending_messages`, reports the
  cleared cids, and the client re-sends exactly them as one fresh turn. A
  deferred steer's row is still IN that queue (its split never ran), so if the
  runner kept the row buffered, the turn-end seal appended it to the turn Stop
  had just killed while the client re-sent it — the same message twice, once
  interrupted and once answered. `interrupt()` therefore clears the
  transcript-side buffer too, which makes the finally's seal a no-op.
  """
  from app.claude_sdk_runner import ActiveClaudeClient, _seal_steer_split

  class _Client:
    async def interrupt(self):
      return None

  async def _run():
    # Built inside THIS loop: interrupt() waits on `_finished`, which is
    # loop-bound, so a handle constructed in a throwaway loop cannot be awaited
    # here. mark_finished() stands in for the runner's own teardown.
    handle = ActiveClaudeClient(_Client(), chat_id="stopsteer")
    handle.mark_finished()
    handle.pending_steer = ["Q2"]
    handle._steer_user_msgs = [
      {"role": "user", "content": "Q2", "ts": 10, "cid": "c-q2"}
    ]
    handle._steer_consume_cids = ["c-q2"]

    await handle.interrupt()

    assert handle.pending_steer == []
    assert handle._steer_user_msgs == []
    assert handle._steer_consume_cids == []

    # The turn-end catch-all now has nothing to append.
    class _Bc:
      def __init__(self):
        self.splits = 0

      async def split_for_steer(self, rows, consume):
        self.splits += 1

    bc = _Bc()
    await _seal_steer_split(bc, handle, "stopsteer")
    assert bc.splits == 0

  asyncio.run(_run())


def test_claude_force_steer_defers_to_runner_and_reorders(client, auth):
  """A Claude fast-forward (force_steer) defers its split to the runner, same
  as an ordinary steer, so the fast-forwarded rows land AFTER the sealed
  pre-interrupt A1 (reload Q1, A1, Q2, A2) instead of merging.

  Deferring moves the queued-row consume to the runner: at the route the rows
  stay in pending and are BUFFERED on the handle; the runner seals A1, appends
  them, and consumes them at the interrupt boundary. Because the rows remain in
  pending until then, a crash before the boundary drains them normally rather
  than dropping them."""
  from app.chat import _ChatEventSink, register_active_sink
  from app.events import process_event

  chat_id = "claudeforce"
  db = SessionLocal()
  try:
    chat = models.Chat(
      id=chat_id, title="Claude", provider="claude",
      messages=[{"role": "user", "content": "Q1", "ts": 1}],
      agent_settings_json={
        "model": "claude-opus-4-8",
      },  # auto-steer OFF — force_steer overrides.
    )
    chat.pending_messages = [
      {"role": "user", "content": "use blue", "ts": 10, "cid": "legacy-10"}
    ]
    db.add(chat)
    db.commit()
  finally:
    db.close()
  handle = _make_active_claude_client(chat_id)
  registry.register(handle)
  bc = create_broadcast(chat_id)
  sink = _ChatEventSink(bc, chat_id, run_token="rt", recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink(chat_id, sink)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "use blue", "force_steer": True,
      "consume_pending_cids": ["legacy-10"],
    },
    headers=auth,
  )
  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  # The route did NOT split: the transcript is still Q1 and the row is still in
  # pending (durable) — the runner owns the append + consume.
  chat = _read_chat(chat_id)
  assert [(m["role"], m.get("content")) for m in chat.messages] == [
    ("user", "Q1"),
  ]
  assert [m["content"] for m in (chat.pending_messages or [])] == ["use blue"]
  # The steered row is buffered on the handle for the runner.
  assert [m["content"] for m in handle._steer_user_msgs] == ["use blue"]

  async def _drive_runner():
    sink.publish({"type": "text", "content": "A1 pre-interrupt"})
    await _seal_steer_split(sink, handle, chat_id)
    sink.publish({"type": "text", "content": "A2 answer"})
    await sink.finalize()

  from app.claude_sdk_runner import _seal_steer_split
  asyncio.run(_drive_runner())

  # Reload order Q1, A1, Q2, A2 — and the queued row is consumed from pending.
  chat = _read_chat(chat_id)
  assert [(m["role"], m.get("content")) for m in chat.messages] == [
    ("user", "Q1"),
    ("assistant", "A1 pre-interrupt"),
    ("user", "use blue"),
    ("assistant", "A2 answer"),
  ]
  assert chat.pending_messages in (None, [])


def test_split_gates_snapshots_so_continuation_cannot_clobber_a1(
  client, auth, monkeypatch,
):
  """A continuation delta arriving DURING the split must not overwrite the
  pre-steer assistant text.

  While `split_for_steer` is in flight the steered append hasn't committed,
  so `chat.messages[-1]` is still A1. A snapshot submitted in that window
  would replace A1 with continuation text. The sink gates snapshot
  submission on `_steering`, so publish() accumulates the continuation into
  fresh blocks but writes nothing until the split's transcript writes land.
  This pins the gate directly: a publish() while steering accumulates but
  submits no snapshot.
  """
  from app.chat import _ChatEventSink

  submitted = []

  class _Bus:
    def __init__(self):
      self.chat_id = "gate"
      self.run_token = "rt"

    def publish(self, event):
      submitted.append(("broadcast", event))

  sink = _ChatEventSink(
    _Bus(), "gate", run_token="rt", recall_binding=EMPTY_RECALL_BINDING,
  )
  monkeypatch.setattr(
    sink, "_submit_fire_and_forget",
    lambda cmd: submitted.append(("writer", cmd)),
  )
  # Seed A1 into the sink's blocks. An immediate-save type (tool_start) is
  # used so the throttle can't suppress the snapshot — outside the steering
  # window a snapshot IS submitted.
  sink.publish({"type": "tool_start", "tool": "Bash", "input": "ls"})
  assert [s for s in submitted if s[0] == "writer"], (
    "a snapshot is submitted outside the steering window"
  )
  submitted.clear()
  # Now enter the steering window: a continuation delta must broadcast +
  # accumulate but submit NO writer-actor snapshot.
  sink._steering = True
  sink.publish({"type": "tool_start", "tool": "Bash", "input": "pwd"})
  assert not [s for s in submitted if s[0] == "writer"], (
    "no snapshot may be submitted while _steering"
  )
  # The continuation was still broadcast live AND accumulated into the
  # blocks so the post-split snapshot/finalize carries it.
  assert ("broadcast", {"type": "tool_start", "tool": "Bash", "input": "pwd"}) \
    in submitted
  assert any(
    b.get("type") == "tool" for b in sink.assistant_blocks
  )


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
      {"role": "user", "content": "use blue", "ts": 10, "cid": "legacy-10"},
      {"role": "user", "content": "also square", "ts": 11, "cid": "legacy-11"},
      {"role": "user", "content": "later", "ts": 12, "cid": "legacy-12"},
    ]
    db.commit()
  finally:
    db.close()
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  steered_calls = []

  async def _fake_steer(cid, message, *_durable):
    steered_calls.append((cid, message))
    return True

  _patch_codex_steer(monkeypatch, _fake_steer)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "use blue\n\nalso square",
      "force_steer": True,
      "consume_pending_cids": ["legacy-10", "legacy-11"],
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  assert steered_calls == [(chat_id, "use blue\n\nalso square")]
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["later"]
  # No live sink in this wiring test → the steered row is appended at the END.
  # Each consumed queued row is stored SEPARATELY (rebuilt from the
  # server-owned pending rows), not one combined \n\n message.
  assert [m["content"] for m in chat.messages[-2:]] == ["use blue", "also square"]
  bc = get_broadcast(chat_id)
  steered_events = [
    e for e in bc.event_log if e.get("type") == "steered_into_turn"
  ]
  assert len(steered_events) == 1
  assert [m["content"] for m in steered_events[0]["messages"]] == [
    "use blue", "also square"
  ]


def test_api_send_without_cid_can_be_force_steered(
  client, auth, monkeypatch,
):
  """An API-originated mid-turn send gets a server cid before queueing.

  The returned identity must name the durable row and remain selectable by a
  later fast-forward request; otherwise force_steer becomes an inert
  `not_steered` response for non-browser callers that omit cid.
  """
  chat_id = "servercidforce"
  _make_codex_chat(chat_id, steer_enabled=False)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  queued = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "use the queued instruction"},
    headers=auth,
  )
  assert queued.status_code == 202, queued.text
  queued_body = queued.json()
  assert queued_body["status"] == "queued"
  server_cid = queued_body["pending_message"]["cid"]
  assert server_cid.startswith("server-")
  assert cid_of(_read_chat(chat_id).pending_messages[0]) == server_cid

  steered_calls = []

  async def _fake_steer(cid, message, *_durable):
    steered_calls.append((cid, message))
    return True

  _patch_codex_steer(monkeypatch, _fake_steer)
  steered = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "use the queued instruction",
      "force_steer": True,
      "consume_pending_cids": [server_cid],
    },
    headers=auth,
  )
  assert steered.status_code == 202, steered.text
  assert steered.json()["status"] == "steered"
  assert steered_calls == [(chat_id, "use the queued instruction")]
  chat = _read_chat(chat_id)
  assert chat.pending_messages in (None, [])
  assert chat.messages[-1]["cid"] == server_cid
  assert chat.messages[-1]["content"] == "use the queued instruction"


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

  async def _fake_steer(_cid, _message, *_durable):
    return False

  _patch_codex_steer(monkeypatch, _fake_steer)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "use blue",
      "force_steer": True,
      "consume_pending_cids": ["legacy-10"],
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "not_steered"
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["use blue"]


def test_force_steer_requires_known_cids(
  client, auth, monkeypatch,
):
  """Forced steer selects queued rows by cid; a consume list naming a cid
  that isn't in the queue selects nothing → not_steered (the whole batch
  must resolve, so a partial/unknown selection is refused). This replaces
  the old content byte-match guard, which cid selection makes unnecessary."""
  chat_id = "codexforceguard"
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

  async def _fail_if_called(_cid, _message, *_durable):
    raise AssertionError("forced steer should require matching queue rows")

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fail_if_called,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "use blue",
      "force_steer": True,
      "consume_pending_cids": ["legacy-999"],
    },
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "not_steered"
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["use blue"]


def test_ordinary_steer_does_not_jump_existing_queue(
  client, auth, monkeypatch,
):
  """A new send cannot steer ahead of older queued user intent."""
  chat_id = "codexsteerqueued"
  _make_codex_chat(chat_id, steer_enabled=True)
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    chat.pending_messages = [
      {"role": "user", "content": "older queued", "ts": 10},
    ]
    db.commit()
  finally:
    db.close()
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _fail_if_called(_cid, _message, *_durable):
    raise AssertionError("ordinary steer must not skip older pending messages")

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fail_if_called,
  )

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "newer send"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "queued"
  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == [
    "older queued",
    "newer send",
  ]


def test_falls_back_to_queue_when_flag_off(client, auth, monkeypatch):
  """Flag OFF (the default): a steerable Codex turn still queues —
  deploying the feature changes nothing until the owner opts in."""
  chat_id = "codexnoflag"
  _make_codex_chat(chat_id, steer_enabled=False)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)

  async def _fail_if_called(cid, message, *_durable):
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


def test_steers_into_live_claude_turn_reserves_durable_pending(
  client, auth,
):
  """Claude buffers only a row already committed to pending."""
  chat_id = "claudechat"
  _make_claude_chat(chat_id, steer_enabled=True)
  handle = _make_active_claude_client(chat_id)
  registry.register(handle)
  create_broadcast(chat_id)

  # No monkeypatch: the real steer_into_active_turn buffers onto the handle.
  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "actually use blue"},
    headers=auth,
  )

  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  # The split is deferred, so the response says so and echoes the row as the
  # still-queued row it is; the client keeps showing it until the cut.
  assert res.json()["cut_deferred"] is True
  assert [m["content"] for m in res.json()["pending_messages"]] == [
    "actually use blue"
  ]

  chat = _read_chat(chat_id)
  assert [m["content"] for m in chat.pending_messages] == ["actually use blue"]
  reserved_cid = cid_of(chat.pending_messages[0])
  assert [m["role"] for m in chat.messages] == ["user", "assistant"]

  assert [m["content"] for m in handle._steer_user_msgs] == [
    "actually use blue"
  ]
  assert cid_of(handle._steer_user_msgs[0]) == reserved_cid
  assert handle._steer_consume_cids == [reserved_cid]

  # NO event at HTTP arrival on the deferred path. The 202's own
  # `pending_messages` (asserted above) is the single signal that keeps the row
  # visible; the CUT (`steered_into_turn`) belongs to the runner's seal — see
  # test_claude_steer_cut_event_is_published_at_the_seal_not_at_http_arrival.
  # A second "accepted" event reconciling the same tray would be a parallel
  # channel racing the response it duplicates.
  bc = get_broadcast(chat_id)
  assert bc.event_log == []


def test_claude_runner_splits_steer_at_boundary_not_http_arrival(
  client, auth,
):
  """The Claude steer split runs when the interrupted turn ENDS (A1 complete),
  not at HTTP arrival (A1 still empty).

  Reproduces the prod merge (chats 37ab92a1, 99b57536): a steer that lands
  before A1 has streamed used to seal an empty A1 at the route, append the
  steered row, and then the real A1 streamed in and merged with A2 AFTER the
  row — reloading as Q1, Q2, A1\\n\\nA2. With the split deferred to the runner
  (where A1 is complete) the durable order is Q1, A1, Q2, A2."""
  from app.broadcast import create_broadcast
  from app.chat import _ChatEventSink, register_active_sink
  from app.claude_sdk_runner import _seal_steer_split

  chat_id = "claudeboundary"
  # Seed only Q1: the assistant turn is in progress and A1 has NOT streamed.
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id, title="Claude chat", provider="claude",
      messages=[{"role": "user", "content": "Q1", "ts": 1}],
      agent_settings_json={
        "model": "claude-opus-4-8", "steer_enabled": True,
      },
    ))
    db.commit()
  finally:
    db.close()
  handle = _make_active_claude_client(chat_id)
  registry.register(handle)
  bc = create_broadcast(chat_id)
  sink = _ChatEventSink(bc, chat_id, run_token="run-boundary", recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink(chat_id, sink)

  # The steer arrives BEFORE A1 has streamed — the exact prod race.
  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "Q2"}, headers=auth,
  )
  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"
  # The route sealed no empty A1 and appended no row — transcript is still Q1.
  assert [(m["role"], m.get("content")) for m in _read_chat(chat_id).messages] == [
    ("user", "Q1"),
  ]

  async def _drive_runner():
    # A1 streams AFTER the steer (the timing the route-side split got wrong).
    sink.publish({"type": "text", "content": "A1 pre-interrupt"})
    # The interrupted turn ends: the runner seals A1, appends Q2, resets. In
    # production the runner's `bc` IS the sink (chat.py passes `bc=sink`), so
    # the split runs against the live sink here too.
    await _seal_steer_split(sink, handle, chat_id)
    # The requery's answer (A2) streams into the fresh sink and finalizes.
    sink.publish({"type": "text", "content": "A2 answer"})
    await sink.finalize()

  asyncio.run(_drive_runner())

  # Q1, A1, Q2, A2 — A1 and A2 are SEPARATE messages with the steered row
  # between them, NOT Q1, Q2, A1\\n\\nA2.
  assert [(m["role"], m.get("content")) for m in _read_chat(chat_id).messages] == [
    ("user", "Q1"),
    ("assistant", "A1 pre-interrupt"),
    ("user", "Q2"),
    ("assistant", "A2 answer"),
  ]
  # The runner consumed the buffered payload (no double-split on turn end).
  assert handle._steer_user_msgs == []
  assert _read_chat(chat_id).pending_messages in (None, [])


def test_claude_steer_cut_event_is_published_at_the_seal_not_at_http_arrival(
  client, auth,
):
  """`steered_into_turn` is the client's only "cut the live stream here" signal,
  so on the deferred (Claude) path it must be published by the runner at the
  seal — AFTER every block that belongs to A1 — and never by the route.

  The regression this pins: the route published the cut at HTTP arrival while
  the split stayed at the runner's interrupt boundary seconds later. Everything
  Claude streamed in the gap was accumulated into the sealed A1 AND kept at the
  head of the client's freshly re-based stream, so it painted twice for the rest
  of the turn. The window is never empty — the AssistantMessage that triggers
  the boundary interrupt is dispatched to the broadcast before the interrupt
  check runs — so this duplicated on EVERY Claude steer.
  """
  from app.chat import _ChatEventSink, register_active_sink
  from app.claude_sdk_runner import _seal_steer_split

  chat_id = "claudecutorder"
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id, title="Claude chat", provider="claude",
      messages=[{"role": "user", "content": "Q1", "ts": 1}],
      agent_settings_json={
        "model": "claude-opus-4-8", "steer_enabled": True,
      },
    ))
    db.commit()
  finally:
    db.close()
  handle = _make_active_claude_client(chat_id)
  registry.register(handle)
  bc = create_broadcast(chat_id)
  sink = _ChatEventSink(bc, chat_id, run_token="run-cut-order", recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink(chat_id, sink)

  # A1's first block is already on the wire when the steer POST arrives.
  sink.publish({"type": "text", "content": "A1 first"})

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "Q2"}, headers=auth,
  )
  assert res.status_code == 202, res.text
  assert res.json()["status"] == "steered"

  # HTTP arrival publishes NOTHING on the deferred path: the response body is
  # the display signal. Publishing the cut here re-based the client's stream
  # too early.
  assert [e.get("type") for e in bc.event_log] == ["text"]

  async def _drive_runner():
    # The rest of A1 streams AFTER arrival — the duplication window.
    sink.publish({"type": "text", "content": " A1 rest"})
    await _seal_steer_split(sink, handle, chat_id)
    # A2's first block follows the seal. It exists here so the cut's position
    # is pinned from BOTH sides: a cut that slipped in front of a continuation
    # block would fold A2's head into the sealed A1 and re-base after it —
    # the mirror image of the fixed bug, and invisible to a lower bound alone.
    sink.publish({"type": "text", "content": "A2 head"})

  asyncio.run(_drive_runner())

  # The cut is published exactly once, at the seal: after every A1 block and
  # before every A2 block.
  cut_positions = [
    i for i, e in enumerate(bc.event_log)
    if e.get("type") == "steered_into_turn"
  ]
  assert len(cut_positions) == 1
  # The sink coalesces contiguous text into one event per segment, so the whole
  # of A1 is one event and A2's head is another. Both bounds matter: after the
  # last A1 event (the fixed bug) AND before the first A2 event (its mirror
  # image, which would fold A2's head into the sealed A1).
  text_positions = [
    i for i, e in enumerate(bc.event_log) if e.get("type") == "text"
  ]
  assert [bc.event_log[i].get("content") for i in text_positions] == [
    "A1 first A1 rest", "A2 head",
  ]
  assert text_positions[0] < cut_positions[0] < text_positions[1]
  # The cut names the DURABLE rows the split committed, so the client inserts
  # the same identity the transcript now holds.
  cut = bc.event_log[cut_positions[0]]
  steered_row = [m for m in _read_chat(chat_id).messages if m["role"] == "user"][-1]
  assert cut["messages"] == [{
    "role": "user",
    "ts": steered_row["ts"],
    "cid": cid_of(steered_row),
    "content": "Q2",
    "steered": True,
  }]
  assert steered_row["steered"] is True
  # And A1 really was sealed at the boundary the cut names.
  assert [(m["role"], m.get("content")) for m in _read_chat(chat_id).messages] == [
    ("user", "Q1"),
    ("assistant", "A1 first A1 rest"),
    ("user", "Q2"),
  ]


def test_codex_steer_publishes_cut_from_handle_owned_settlement(
  client, auth, monkeypatch,
):
  """The handle-owned settlement commits and publishes the authoritative cut."""
  chat_id = "codexcutroute"
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id, title="Codex chat", provider="codex",
      messages=[{"role": "user", "content": "Q1", "ts": 1}],
      agent_settings_json={"model": "gpt-5.6-sol", "steer_enabled": True},
    ))
    db.commit()
  finally:
    db.close()
  registry.register(_make_active_codex_turn(chat_id))
  sink = _register_sink_with_partial(chat_id, "run-codex-cut", "A1")

  async def _fake_steer(cid, message, *_durable):
    return True

  _patch_codex_steer(monkeypatch, _fake_steer)

  res = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "Q2"}, headers=auth,
  )
  assert res.status_code == 202, res.text
  body = res.json()
  assert body["status"] == "steered"
  assert body["cut_deferred"] is True
  # This route-wiring double settles synchronously, so its echoed queue already
  # reflects the committed cut. The real handle cannot settle under the
  # route-held queue lock; the never-ack regression below pins that deferred
  # production window.
  assert body["pending_messages"] == []

  bc = get_broadcast(chat_id)
  assert [e.get("type") for e in bc.event_log] == ["steered_into_turn"]
  cut = [e for e in bc.event_log if e.get("type") == "steered_into_turn"][0]
  assert [m["content"] for m in cut["messages"]] == ["Q2"]
  assert [m["steered"] for m in cut["messages"]] == [True]
  # The owning sink sealed A1 and appended Q2 before publishing.
  assert [(m["role"], m.get("content")) for m in _read_chat(chat_id).messages] == [
    ("user", "Q1"),
    ("assistant", "A1"),
    ("user", "Q2"),
  ]
  assert _read_chat(chat_id).messages[-1]["steered"] is True
  assert sink.assistant_blocks == []


def test_claude_reserved_row_survives_process_loss_and_sweep(
  client, auth, monkeypatch,
):
  from datetime import UTC, datetime

  from sqlalchemy.orm.attributes import flag_modified

  from app import chat as chat_mod

  chat_id = "claude-process-loss"
  message_cid = "claude-process-loss-cid"
  _make_claude_chat(chat_id, steer_enabled=True)
  db = SessionLocal()
  try:
    db.add(models.ChatRun(
      id=f"run-{chat_id}",
      chat_id=chat_id,
      status="running",
      provider="claude",
      started_at=datetime.now(UTC),
    ))
    db.commit()
  finally:
    db.close()

  handle = _make_active_claude_client(chat_id)
  registry.register(handle)
  bc = create_broadcast(chat_id)
  response = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "survive restart", "cid": message_cid},
    headers=auth,
  )
  assert response.status_code == 202
  assert [cid_of(row) for row in _read_chat(chat_id).pending_messages] == [
    message_cid,
  ]
  assert [cid_of(row) for row in handle._steer_user_msgs] == [message_cid]

  registry.reset_for_tests()
  bc.mark_completed()
  db = SessionLocal()
  try:
    assert chat_mod.reconcile_interrupted_chats(db) == [chat_id]
    chat = db.get(models.Chat, chat_id)
    pending = list(chat.pending_messages or [])
    pending[0]["ts"] -= 180_000
    chat.pending_messages = pending
    flag_modified(chat, "pending_messages")
    db.commit()
  finally:
    db.close()

  scheduled = []
  monkeypatch.setattr(
    chat_mod,
    "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs) or True,
  )
  db = SessionLocal()
  try:
    assert asyncio.run(chat_mod.sweep_idle_pending_chats(db)) == [chat_id]
  finally:
    db.close()

  chat = _read_chat(chat_id)
  assert chat.pending_messages in (None, [])
  assert len([
    row for row in chat.messages if cid_of(row) == message_cid
  ]) == 1
  assert len(scheduled) == 1


def test_claude_falls_back_to_queue_when_flag_off(
  client, auth, monkeypatch,
):
  """Claude steering is deploy-safe: no flag means normal queueing."""
  chat_id = "claudenoflag"
  _make_claude_chat(chat_id, steer_enabled=False)
  registry.register(_make_active_claude_client(chat_id))
  create_broadcast(chat_id)

  async def _fail_if_called(cid, message, *_durable):
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

  async def _steer_raises(cid, message, *args):
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

  async def _steer_false(cid, message, *_durable):
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

  async def _steer_raises(cid, message, *_durable):
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


def test_codex_deferred_admission_keeps_one_reserved_row(
  client, auth, monkeypatch,
):
  chat_id = "codex-split-failure"
  message_cid = "split-failure-cid"
  _make_codex_chat(chat_id, steer_enabled=True)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)
  steer_calls = []

  async def _admit_without_settling(
    self, message, user_msgs=None, consume_pending_cids=None,
  ):
    del self, user_msgs, consume_pending_cids
    steer_calls.append(message)
    return True

  monkeypatch.setattr(
    "app.codex_sdk_runner.ActiveCodexTurn.steer",
    _admit_without_settling,
  )

  # Admission returns immediately while provider settlement is outstanding.
  # The exact row remains durably queued, and a same-cid retry dedups to it.
  first = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "keep this", "cid": message_cid},
    headers=auth,
  )
  retry = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "keep this", "cid": message_cid},
    headers=auth,
  )

  assert first.status_code == 202
  assert first.json()["status"] == "steered"
  assert first.json()["cut_deferred"] is True
  assert retry.status_code == 202
  assert retry.json()["status"] == "queued"
  assert steer_calls == ["keep this"]
  chat = _read_chat(chat_id)
  assert [cid_of(row) for row in chat.pending_messages] == [message_cid]
  assert not [
    row for row in chat.messages if cid_of(row) == message_cid
  ]


def test_codex_never_acknowledged_steer_does_not_lock_send_or_stop():
  """A wedged provider control RPC stays outside the per-chat queue lock."""
  from app import chat as chat_mod
  from app import schemas
  from app.codex_sdk_runner import ActiveCodexTurn
  from app.deps import Principal
  from app.routes import chats_stream

  chat_id = "codex-never-ack-steer"
  message_cid = "never-ack-cid"
  _make_codex_chat(chat_id, steer_enabled=True)
  db0 = SessionLocal()
  try:
    chat = db0.query(models.Chat).filter(models.Chat.id == chat_id).first()
    chat.pending_messages = [{
      "role": "user",
      "content": "change course",
      "ts": 10,
      "cid": message_cid,
    }]
    db0.commit()
  finally:
    db0.close()
  create_broadcast(chat_id)

  async def _run():
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    class _NeverAckTurn:
      async def steer(self, _message):
        provider_entered.set()
        await release_provider.wait()

      async def interrupt(self):
        return None

    handle = ActiveCodexTurn(
      thread=object(),
      turn=_NeverAckTurn(),
      chat_id=chat_id,
    )
    registry.register(handle)
    first_db = SessionLocal()
    second_db = SessionLocal()
    try:
      owner = first_db.query(models.Owner).first()
      principal = Principal(owner=owner, app_id=None)
      first = await asyncio.wait_for(
        chats_stream.send_message(
          schemas.SendMessage(
            content="change course",
            force_steer=True,
            consume_pending_cids=[message_cid],
          ),
          chat_id,
          principal,
          first_db,
        ),
        timeout=1,
      )
      assert first.status_code == 202
      assert b'"cut_deferred":true' in first.body
      await asyncio.wait_for(provider_entered.wait(), timeout=1)

      # The provider call above is still waiting, yet an ordinary send acquires
      # the same queue lock and durably queues without waiting behind it.
      second = await asyncio.wait_for(
        chats_stream.send_message(
          schemas.SendMessage(
            content="also preserve this",
            cid="second-cid",
          ),
          chat_id,
          principal,
          second_db,
        ),
        timeout=1,
      )
      assert second.status_code == 202
      assert b'"status":"queued"' in second.body

      async def _bounded_stop(*_args, **_kwargs):
        handle.mark_finished()
        return True

      handle.stop = _bounded_stop
      stopped, _cleared = await asyncio.wait_for(
        chat_mod.stop_chat_for(chat_id), timeout=1,
      )
      assert stopped is True
    finally:
      release_provider.set()
      attempt = handle._steer_attempt
      if attempt is not None and attempt.task is not None:
        await asyncio.wait_for(attempt.task, timeout=1)
      registry.unregister(chat_id, RunnerKind.CODEX_SDK)
      first_db.close()
      second_db.close()

  asyncio.run(_run())


def test_request_cancellation_after_reserve_keeps_pending(
  client, auth, monkeypatch,
):
  from app import schemas
  from app.deps import Principal
  from app.routes import chats_stream

  chat_id = "cancel-after-reserve"
  message_cid = "cancel-after-reserve-cid"
  _make_codex_chat(chat_id, steer_enabled=True)
  registry.register(_make_active_codex_turn(chat_id))
  create_broadcast(chat_id)
  provider_entered = asyncio.Event()

  async def _blocked_steer(_chat_id, _content, *_durable):
    provider_entered.set()
    await asyncio.Event().wait()

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _blocked_steer,
  )

  async def _run():
    db = SessionLocal()
    try:
      owner = db.query(models.Owner).first()
      task = asyncio.create_task(chats_stream.send_message(
        schemas.SendMessage(content="durable", cid=message_cid),
        chat_id,
        Principal(owner=owner, app_id=None),
        db,
      ))
      await asyncio.wait_for(provider_entered.wait(), timeout=2)
      task.cancel()
      with pytest.raises(asyncio.CancelledError):
        await task
    finally:
      db.close()

  asyncio.run(_run())
  chat = _read_chat(chat_id)
  assert [cid_of(row) for row in chat.pending_messages] == [message_cid]
  assert not [row for row in chat.messages if cid_of(row) == message_cid]


def test_stop_wins_steer_race_send_rechecks_idle(
  client, auth, monkeypatch,
):
  from app import chat as chat_mod
  from app import chat_queue, schemas
  from app.deps import Principal
  from app.routes import chats_stream

  chat_id = "stop-wins-steer"
  message_cid = "stop-wins-steer-cid"
  _make_codex_chat(chat_id, steer_enabled=True)
  handle = _make_active_codex_turn(chat_id)
  registry.register(handle)
  create_broadcast(chat_id)
  scheduled = asyncio.Event()

  async def _stop(*_args, **_kwargs):
    return True

  async def _must_not_steer(*_args):
    raise AssertionError("the stopped handle must not receive a steer")

  async def _run_chat(*_args, **_kwargs):
    scheduled.set()

  handle.stop = _stop
  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _must_not_steer,
  )
  monkeypatch.setattr(chats_stream, "run_chat", _run_chat)

  async def _run():
    db = SessionLocal()
    lock = chat_queue.get_lock(chat_id)
    await lock.acquire()
    try:
      stop = asyncio.create_task(chat_mod.stop_chat_for(chat_id))
      await asyncio.sleep(0)
      owner = db.query(models.Owner).first()
      send = asyncio.create_task(chats_stream.send_message(
        schemas.SendMessage(content="start fresh", cid=message_cid),
        chat_id,
        Principal(owner=owner, app_id=None),
        db,
      ))
      await asyncio.sleep(0)
    finally:
      lock.release()
    try:
      _, response = await asyncio.gather(stop, send)
      assert response.status_code == 202
      await asyncio.wait_for(scheduled.wait(), timeout=2)
    finally:
      db.close()

  asyncio.run(_run())
  chat = _read_chat(chat_id)
  durable = [
    row for row in list(chat.messages or []) + list(chat.pending_messages or [])
    if cid_of(row) == message_cid
  ]
  assert len(durable) == 1
  assert durable[0] in chat.messages
