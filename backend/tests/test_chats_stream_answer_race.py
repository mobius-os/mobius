"""Grace-period retry for the AskUserQuestion answer-submit path.

The frontend renders the question card the instant the `question`
SSE event lands, but the bridge handler that inserts into
`questions._pending` runs in a separate coroutine (the SDK callback
for Claude, the `run_coroutine_threadsafe`-marshaled `park_question`
for Codex). A user who taps an answer chip in the tens-of-ms window
between "event published" and "registry populated" used to hit a
410. The route now polls with a short grace period before deciding
the question is stale.

These tests pin three behaviours of that grace period:

  1. Happy path — pending registered before POST → 202 immediately.
  2. Race path — pending registered AFTER POST starts but inside
     the grace window → 202 after the registration lands.
  3. Stale path — no pending, none arrives → 410 after the grace
     window elapses, in roughly the budget the loop allows.
"""

import asyncio
import time
from uuid import uuid4

import pytest

from app import models, questions
from app.database import SessionLocal
from app.pending_questions import PendingQuestion


def _make_pending(future: asyncio.Future) -> PendingQuestion:
  """Builds a PendingQuestion whose future is owned by the caller.

  The runner-side coroutine that originally created the future is
  what awaits its result; here we keep the future visible so the test
  can assert delivery happened (set_result was called).
  """
  return PendingQuestion(
    question_id=str(uuid4()),
    questions=[{"id": "q1", "question": "Pick one", "options": ["a", "b"]}],
    future=future,
  )


def _seed_question_block(chat_id: str, question_id: str) -> None:
  """Persist an assistant message carrying the open question block.

  C2 routes the answer write through the actor's AnswerQuestion, which
  re-reads the chat and merges the answer into the question block matched
  by `question_id` — so a durable block must exist or the write raises.
  Pre-C2 the route wrote the answer inline regardless; the block is what
  the SDK runner's QuestionCommit would have persisted save-before-
  broadcast in production.
  """
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    chat.messages = [
      {"role": "user", "content": "go", "ts": 1},
      {
        "role": "assistant",
        "content": "",
        "ts": 2,
        "blocks": [
          {
            "type": "question",
            "question_id": question_id,
            "questions": [
              {"id": "q1", "question": "Pick one", "options": ["a", "b"]}
            ],
          }
        ],
      },
    ]
    db.commit()
  finally:
    db.close()


def test_answer_delivers_immediately_when_pending_registered(
  client, auth, chat,
):
  """Pending question already in the registry — POST resolves the
  future and returns 202 answer_delivered without any waiting."""
  async def go():
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    pending = _make_pending(fut)
    questions.register(chat.id, pending)
    # The actor's AnswerQuestion merges into a durable question block.
    _seed_question_block(chat.id, pending.question_id)

    started = time.monotonic()
    res = client.post(
      f"/api/chats/{chat.id}/messages",
      json={
        "content": "answer",
        "hidden": True,
        "answers": {"Pick one": "a"},
        "question_id": pending.question_id,
      },
      headers=auth,
    )
    elapsed = time.monotonic() - started

    assert res.status_code == 202, res.text
    assert res.json()["status"] == "answer_delivered"
    # Future resolved with the submitted answers.
    assert fut.done()
    assert fut.result() == {"Pick one": "a"}
    # Registry cleared atomically by claim().
    assert questions.get(chat.id) is None
    # No grace-period delay on the happy path. 500ms cap; 250ms
    # leaves comfortable headroom for slow CI without making the
    # test useless.
    assert elapsed < 0.25, (
      f"happy path waited unexpectedly long: {elapsed:.3f}s"
    )

  asyncio.run(go())


def test_answer_delivers_after_late_registration_within_grace(
  client, auth, chat,
):
  """Pending question registered ~200ms AFTER the POST starts —
  the grace loop's sleep yields long enough for the registration
  task to land and the second claim attempt succeeds."""

  loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
  future_holder: dict[str, asyncio.Future] = {}

  async def late_register():
    # Simulate the bridge handler racing the POST: arrives after a
    # ~200ms delay (well inside the 500ms grace window).
    await asyncio.sleep(0.2)
    fut = loop_holder["loop"].create_future()
    future_holder["future"] = fut
    pending = _make_pending(fut)
    # The actor's AnswerQuestion merges into a durable question block;
    # the runner's QuestionCommit would have persisted it before
    # publishing the card in production.
    _seed_question_block(chat.id, pending.question_id)
    future_holder["question_id"] = pending.question_id
    questions.register(chat.id, pending)

  async def go():
    loop_holder["loop"] = asyncio.get_event_loop()
    register_task = asyncio.create_task(late_register())

    started = time.monotonic()
    # TestClient.post is sync — run it on the executor so the
    # late_register task can run concurrently on this loop.
    res = await loop_holder["loop"].run_in_executor(
      None,
      lambda: client.post(
        f"/api/chats/{chat.id}/messages",
        json={
          "content": "answer",
          "hidden": True,
          "answers": {"Pick one": "b"},
        },
        headers=auth,
      ),
    )
    elapsed = time.monotonic() - started

    await register_task

    assert res.status_code == 202, res.text
    assert res.json()["status"] == "answer_delivered"
    # Future resolved with the submitted answers.
    fut = future_holder["future"]
    assert fut.done()
    assert fut.result() == {"Pick one": "b"}
    # Took at least the registration delay but well under the 500ms
    # grace cap.
    assert 0.15 <= elapsed < 0.55, (
      f"race path elapsed out of expected band: {elapsed:.3f}s"
    )
    assert questions.get(chat.id) is None

  asyncio.run(go())


def test_answer_returns_410_after_grace_when_nothing_registers(
  client, auth, chat,
):
  """No pending question and none arrives — POST returns 410 after
  exhausting the grace window. Bounded so a genuinely stale UI gets
  the error promptly rather than holding the request open."""
  async def go():
    assert questions.get(chat.id) is None

    started = time.monotonic()
    res = await asyncio.get_event_loop().run_in_executor(
      None,
      lambda: client.post(
        f"/api/chats/{chat.id}/messages",
        json={
          "content": "answer",
          "hidden": True,
          "answers": {"Pick one": "a"},
        },
        headers=auth,
      ),
    )
    elapsed = time.monotonic() - started

    assert res.status_code == 410, res.text
    assert "no longer accepting answers" in res.json()["detail"]
    # Grace loop is 10 × 50ms ≈ 500ms; require at least most of that
    # so we know the retry actually ran, but cap the upper bound so
    # a regression that hangs forever (or sleeps 5s) is caught.
    assert 0.4 <= elapsed < 1.5, (
      f"stale path elapsed out of expected band: {elapsed:.3f}s"
    )
    # Registry untouched.
    assert questions.get(chat.id) is None

  asyncio.run(go())
