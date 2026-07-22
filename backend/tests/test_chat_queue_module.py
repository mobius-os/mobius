"""Unit tests for the chat_queue module (ticket 033).

Locks in the lock-storage invariants and the drain_and_release
composite primitive so anything that swaps the body around still has
to satisfy the same contract: lock is per-chat, atomic get-or-create,
no await in the get path, and the drain critical section calls the
injected discard_starting + forget_chat exactly once when (and only
when) the queue is empty.

C2: promote_pending_messages_locked / drain_and_release now route the
JSON-blob RMW through the writer actor's PromotePending command (the
actor is the sole runtime mutator), so they are async and take a
`run_token`. The conftest fresh_db fixture starts the actor per test, so
these drive the real actor against the test DB.
"""

import asyncio

import pytest

from app import chat_queue, models


def _record_clear(sink: list):
  """Build an async `clear_run_status_strict` stub recording its chat_id.

  drain_and_release awaits this strict clear INSIDE the lock for the
  empty-queue case (clear-before-forget) and never for a promoted
  continuation, so the recorded list pins which disposition fired.
  """
  async def _clear(chat_id, run_token="", terminal_status="completed"):
    sink.append(chat_id)

  return _clear


async def _noop_clear(
  _chat_id, _run_token="", _terminal_status="completed",
):
  """An async clear stub that records nothing (for paths that don't clear)."""
  return None


def test_get_lock_returns_same_instance_per_chat_id():
  async def go():
    a = chat_queue.get_lock("chat-a")
    a_again = chat_queue.get_lock("chat-a")
    b = chat_queue.get_lock("chat-b")
    assert a is a_again
    assert a is not b

  asyncio.run(go())


def test_get_lock_is_sync_no_await_inside():
  """get_lock MUST be sync — calling it does not need an await.
  This is the atomic get-or-create invariant: an await mid-method
  would let two callers receive two different locks for the same
  chat_id, breaking serialization."""
  assert not asyncio.iscoroutinefunction(chat_queue.get_lock)
  lock = chat_queue.get_lock("inline-chat")
  assert isinstance(lock, asyncio.Lock)


def test_reset_for_tests_drops_lock_identity():
  """After reset_for_tests, the next get_lock returns a fresh lock —
  even for the same chat_id. The old caller holding the original
  lock instance is not affected (lock identity doesn't leak)."""
  async def go():
    first = chat_queue.get_lock("reset-test")
    chat_queue.reset_for_tests()
    second = chat_queue.get_lock("reset-test")
    assert first is not second

  asyncio.run(go())


def test_promote_pending_messages_locked_collapses_queue(db):
  chat = models.Chat(
    id="cq-head-test",
    title="t",
    messages=[],
    pending_messages=[
      {"role": "user", "content": "first", "ts": 1},
      {"role": "user", "content": "second", "ts": 2},
    ],
    session_id="sess-cq",
  )
  db.add(chat)
  db.commit()

  async def go():
    return await chat_queue.promote_pending_messages_locked(
      db, "cq-head-test", "rt-cq",
    )

  next_msgs, promoted, sid = asyncio.run(go())

  assert promoted is not None
  assert promoted["content"] == "first\nsecond"
  assert promoted["ts"] == 1
  assert sid == "sess-cq"
  assert [m.content for m in next_msgs] == ["first\nsecond"]
  db.refresh(chat)
  assert chat.pending_messages == []


def test_promote_pending_messages_locked_returns_none_on_empty_queue(db):
  chat = models.Chat(
    id="cq-empty",
    title="t",
    messages=[],
    pending_messages=[],
    session_id="sess-empty",
  )
  db.add(chat)
  db.commit()

  async def go():
    return await chat_queue.promote_pending_messages_locked(
      db, "cq-empty", "rt-cq",
    )

  next_msgs, head, sid = asyncio.run(go())
  assert head is None
  assert next_msgs == []
  assert sid == "sess-empty"


def test_drain_and_release_promotes_then_holds_starting(db):
  """When the queue has a head, drain_and_release returns it and
  does NOT call discard_starting / forget_chat (the next turn owns
  the starting claim)."""
  chat = models.Chat(
    id="cq-drain-with-head",
    title="t",
    messages=[],
    pending_messages=[{"role": "user", "content": "go", "ts": 1}],
    session_id="sess-dwh",
  )
  db.add(chat)
  db.commit()

  discarded: list[str] = []
  forgotten: list[str] = []
  cleared: list[str] = []

  async def go():
    return await chat_queue.drain_and_release(
      db, "cq-drain-with-head", run_gen=7, run_token="rt-cq",
      discard_starting=discarded.append,
      forget_chat=forgotten.append,
      clear_run_status_strict=_record_clear(cleared),
      current_generation=lambda _cid: 7,  # still ours → owns the turn
    )

  head, next_messages, sid, disposition = asyncio.run(go())
  assert head is not None
  assert head["content"] == "go"
  assert sid == "sess-dwh"
  assert discarded == []
  assert forgotten == []
  # A promoted continuation must NOT clear the marker — it stays set for
  # the next turn.
  assert cleared == []
  assert disposition is chat_queue.TerminalDisposition.CONTINUATION_PROMOTED


def test_drain_and_release_releases_when_queue_empty(db):
  """When the queue is empty, drain_and_release calls discard_starting
  AND forget_chat atomically under the lock."""
  chat = models.Chat(
    id="cq-drain-empty",
    title="t",
    messages=[],
    pending_messages=[],
    session_id="sess-de",
  )
  db.add(chat)
  db.commit()

  discarded: list[str] = []
  forgotten: list[str] = []
  cleared: list[str] = []

  async def go():
    return await chat_queue.drain_and_release(
      db, "cq-drain-empty", run_gen=7, run_token="rt-cq",
      discard_starting=discarded.append,
      forget_chat=forgotten.append,
      clear_run_status_strict=_record_clear(cleared),
      current_generation=lambda _cid: 7,  # still ours → owns the turn
    )

  head, _, _, disposition = asyncio.run(go())
  assert head is None
  # Clear-before-forget ordering: the marker is cleared, THEN _starting is
  # released, THEN the chat is forgotten — all under the one lock.
  assert cleared == ["cq-drain-empty"]
  assert discarded == ["cq-drain-empty"]
  assert forgotten == ["cq-drain-empty"]
  assert disposition is chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED


def test_drain_and_release_no_op_when_not_owning_generation(db):
  """When a Stop has bumped the generation, drain_and_release reads the
  current generation UNDER its lock, finds it no longer matches run_gen,
  and must NOT promote, discard, or forget. The newer owner of the
  generation is responsible for those."""
  chat = models.Chat(
    id="cq-not-owner",
    title="t",
    messages=[],
    pending_messages=[{"role": "user", "content": "x", "ts": 1}],
    session_id="sess-no",
  )
  db.add(chat)
  db.commit()

  discarded: list[str] = []
  forgotten: list[str] = []
  cleared: list[str] = []

  async def go():
    return await chat_queue.drain_and_release(
      db, "cq-not-owner", run_gen=7, run_token="rt-cq",
      discard_starting=discarded.append,
      forget_chat=forgotten.append,
      clear_run_status_strict=_record_clear(cleared),
      current_generation=lambda _cid: 8,  # Stop bumped past run_gen → stale
    )

  head, msgs, sid, disposition = asyncio.run(go())
  assert head is None
  assert msgs == []
  assert sid is None
  assert discarded == []
  assert forgotten == []
  # A newer generation owns the chat — we must not clear its marker.
  assert cleared == []
  assert disposition is chat_queue.TerminalDisposition.STALE_NO_ACTION
  # The pending queue is intact — Stop's cleanup hasn't run, so the
  # message must still be visible to a subsequent claim.
  db.refresh(chat)
  assert len(chat.pending_messages) == 1


def test_drain_serializes_with_concurrent_lock_holder(db):
  """A concurrent holder of the same chat lock blocks drain_and_release
  from reading until release. Locks the per-chat serialization
  invariant — without it, the late-drain critical section would race
  a concurrent POST append."""
  chat = models.Chat(
    id="cq-serialize",
    title="t",
    messages=[],
    pending_messages=[],
    session_id="sess-s",
  )
  db.add(chat)
  db.commit()

  observed_order: list[str] = []

  async def slow_holder(release_event: asyncio.Event):
    async with chat_queue.get_lock("cq-serialize"):
      observed_order.append("holder-acquired")
      await release_event.wait()
      observed_order.append("holder-releasing")

  async def drain_run():
    head, _, _, _ = await chat_queue.drain_and_release(
      db, "cq-serialize", run_gen=7, run_token="rt-cq",
      discard_starting=lambda _cid: None,
      forget_chat=lambda _cid: None,
      clear_run_status_strict=_noop_clear,
      current_generation=lambda _cid: 7,  # still ours → owns the turn
    )
    observed_order.append("drain-finished")
    return head

  async def go():
    release_event = asyncio.Event()
    h_task = asyncio.create_task(slow_holder(release_event))
    # Let the holder acquire first.
    await asyncio.sleep(0)
    d_task = asyncio.create_task(drain_run())
    # Briefly yield so the drain blocks on the lock.
    await asyncio.sleep(0)
    release_event.set()
    return await asyncio.gather(h_task, d_task)

  asyncio.run(go())
  # The drain finished only after the holder released — proving the
  # per-chat lock serializes them.
  assert observed_order == [
    "holder-acquired",
    "holder-releasing",
    "drain-finished",
  ]
