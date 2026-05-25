"""Unit tests for the questions module (ticket 033).

Locks in the AskUserQuestion lifecycle primitives so the no-timeout
contract is observable: only `deliver_answer` (user-side resolution)
and `cancel` (stop-side) can complete the future. Anything else that
appears to time out is a leak and must be investigated.
"""

import asyncio
from uuid import uuid4

from app import questions
from app.pending_questions import PendingQuestion


def _make_pending() -> PendingQuestion:
  """Builds a fresh PendingQuestion with a live future on the running loop."""
  return PendingQuestion(
    question_id=str(uuid4()),
    questions=[{"id": "q1", "question": "Pick one", "options": ["a", "b"]}],
    future=asyncio.get_event_loop().create_future(),
  )


def test_register_and_get_round_trip():
  async def go():
    pending = _make_pending()
    questions.register("chat-a", pending)
    assert questions.get("chat-a") is pending
    # Cleanup so other tests aren't affected (the autouse fixture
    # also clears, but explicit here for clarity in the unit test).
    questions.cancel("chat-a")

  asyncio.run(go())


def test_register_replaces_existing_entry():
  async def go():
    first = _make_pending()
    second = _make_pending()
    questions.register("chat-b", first)
    questions.register("chat-b", second)
    assert questions.get("chat-b") is second
    # The replaced future is NOT auto-cancelled — register documents
    # itself as a plain replacement; callers cancel explicitly if
    # they want both branches resolved.
    assert not first.future.done()
    questions.cancel("chat-b")
    first.future.cancel()

  asyncio.run(go())


def test_register_no_op_on_empty_chat_id():
  async def go():
    pending = _make_pending()
    questions.register("", pending)
    # Empty chat_id is a defensive no-op — the registry stays empty
    # and the caller's future is left for them to manage.
    assert questions.get("") is None
    pending.future.cancel()

  asyncio.run(go())


def test_deliver_answer_resolves_future_and_returns_true():
  async def go():
    pending = _make_pending()
    questions.register("chat-c", pending)
    delivered = questions.deliver_answer("chat-c", {"q1": "a"})
    assert delivered is True
    assert pending.future.done()
    assert pending.future.result() == {"q1": "a"}
    # Entry stays — `claim` is what removes; deliver_answer only
    # resolves. This mirrors the chats_stream.py path which calls
    # claim + sets the result inline.
    assert questions.get("chat-c") is pending
    questions.cancel("chat-c")

  asyncio.run(go())


def test_deliver_answer_returns_false_when_no_pending():
  async def go():
    assert questions.deliver_answer("nope", {"x": "y"}) is False

  asyncio.run(go())


def test_deliver_answer_idempotent_on_done_future():
  async def go():
    pending = _make_pending()
    pending.future.set_result({"first": "round"})
    questions.register("chat-d", pending)
    # Future already done — deliver_answer must NOT raise InvalidState
    # and MUST return True (entry exists, even if the resolution is
    # a no-op).
    assert questions.deliver_answer("chat-d", {"second": "round"}) is True
    assert pending.future.result() == {"first": "round"}
    questions.cancel("chat-d")

  asyncio.run(go())


def test_claim_atomically_pops_and_returns():
  async def go():
    pending = _make_pending()
    questions.register("chat-e", pending)
    claimed = questions.claim("chat-e")
    assert claimed is pending
    assert questions.get("chat-e") is None
    # A second claim returns None — atomic remove means no other
    # caller can re-process the same future.
    assert questions.claim("chat-e") is None
    pending.future.cancel()

  asyncio.run(go())


def test_cancel_cancels_future_and_removes_entry():
  async def go():
    pending = _make_pending()
    questions.register("chat-f", pending)
    questions.cancel("chat-f")
    assert questions.get("chat-f") is None
    assert pending.future.cancelled()

  asyncio.run(go())


def test_cancel_is_idempotent_on_missing_entry():
  # No registration — cancel must NOT raise.
  questions.cancel("never-registered")


def test_cancel_skips_already_done_future():
  async def go():
    pending = _make_pending()
    pending.future.set_result({"already": "done"})
    questions.register("chat-g", pending)
    questions.cancel("chat-g")
    assert questions.get("chat-g") is None
    # The future was already done; cancel must not flip it to cancelled.
    assert not pending.future.cancelled()
    assert pending.future.result() == {"already": "done"}

  asyncio.run(go())


def test_no_timeout_sla_future_outlives_register_window():
  """No automatic timer touches the future — it stays pending until
  the caller explicitly delivers or cancels. This locks in the
  no-timeout SLA documented at the top of questions.py."""

  async def go():
    pending = _make_pending()
    questions.register("chat-h", pending)
    # Several event-loop ticks must not resolve the future on their
    # own — proves no background timer is wired up.
    for _ in range(5):
      await asyncio.sleep(0)
    assert not pending.future.done()
    questions.cancel("chat-h")

  asyncio.run(go())
