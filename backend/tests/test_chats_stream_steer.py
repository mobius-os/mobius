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
from app.chat_writer import cid_of
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


def _make_codex_chat(chat_id: str, *, steer_enabled: bool) -> None:
  """Persist a Codex chat with one assistant partial mid-turn.

  The assistant message is the in-progress turn's partial; a steered
  user message must land just before it so the runner's snapshot /
  finalize writes keep targeting the assistant as `messages[-1]`.
  """
  settings = {"steer_enabled": True} if steer_enabled else {}
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
    }
  ]


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
  sink = _ChatEventSink(bc, chat_id, run_token=run_token)
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
      agent_settings_json={"steer_enabled": True},
    ))
    db.commit()
  finally:
    db.close()
  registry.register(_make_active_codex_turn(chat_id))
  run_token = "run-empty"
  # Only a whitespace token streamed before the steer landed.
  sink = _register_sink_with_partial(chat_id, run_token, " ")

  async def _fake_steer(cid, message):
    return True

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fake_steer,
  )

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
      agent_settings_json={"steer_enabled": True},
    ))
    db.commit()
  finally:
    db.close()
  registry.register(_make_active_codex_turn(chat_id))
  run_token = "run-split"
  sink = _register_sink_with_partial(chat_id, run_token, "A1")

  async def _fake_steer(cid, message):
    return True

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fake_steer,
  )

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
      agent_settings_json={},  # auto-steer OFF — force_steer overrides.
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
  sink = _ChatEventSink(bc, chat_id, run_token="rt")
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

  sink = _ChatEventSink(_Bus(), "gate", run_token="rt")
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

  async def _fake_steer(cid, message):
    steered_calls.append((cid, message))
    return True

  monkeypatch.setattr(
    "app.codex_sdk_runner.steer_into_active_turn", _fake_steer,
  )

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

  async def _fail_if_called(_cid, _message):
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

  async def _fail_if_called(_cid, _message):
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


def test_steers_into_live_claude_turn_when_flag_on(
  client, auth,
):
  """claude + running + flag-on + live client: the steer payload is BUFFERED
  on the handle, the response is `steered`, and a `steered_into_turn` event
  renders it inline. The route deliberately does NOT write the transcript for
  Claude — the runner seals A1 and appends the steered row at the interrupt
  boundary (see test_claude_runner_splits_steer_at_boundary_not_http_arrival).
  Writing it here at HTTP arrival, before A1 had streamed, sealed an empty A1
  and merged the real A1 with A2 after the steered row on reload."""
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
  # The steer payload is buffered for the runner to split at the boundary.
  assert [m["content"] for m in handle._steer_user_msgs] == [
    "actually use blue"
  ]
  # The route did NOT touch the transcript — the runner owns the Claude split.
  chat = _read_chat(chat_id)
  assert [m["role"] for m in chat.messages] == ["user", "assistant"]
  assert chat.pending_messages in (None, [])
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
      "ts": handle._steer_user_msgs[0]["ts"],
      "cid": cid_of(handle._steer_user_msgs[0]),
      "content": "actually use blue",
    }
  ]


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
      agent_settings_json={"steer_enabled": True},
    ))
    db.commit()
  finally:
    db.close()
  handle = _make_active_claude_client(chat_id)
  registry.register(handle)
  bc = create_broadcast(chat_id)
  sink = _ChatEventSink(bc, chat_id, run_token="run-boundary")
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
