"""Grace-period retry for the AskUserQuestion answer-submit path.

The frontend renders the question card the instant the `question`
SSE event lands, but the bridge handler that inserts into
`questions._pending` runs in a separate coroutine (the SDK callback
for Claude, the `run_coroutine_threadsafe`-marshaled `park_question`
for Codex). A user who taps an answer chip in the tens-of-ms window
between "event published" and "registry populated" used to hit a
410. The route now polls with a short grace period before deciding
the question is stale.

These tests pin five behaviours of that grace period:

  1. Happy path — pending registered before POST → 202 immediately.
  2. Race path — pending registered AFTER POST starts but inside
     the grace window → 202 after the registration lands.
  3. Recovery path — no live pending, but a durable open question remains
     after restart → answer is recorded and a hidden continuation starts.
  4. Stale path — no pending, none arrives, and no durable open question
     exists → 410 after the grace window elapses.
  5. Stopped path — once Stop has completed, a later deliberate Submit
     restarts from the durable question while a Stop racing Submit still wins.
"""

import asyncio
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app import chat as chat_mod
from app import models, questions
from app.database import SessionLocal
from app.pending_questions import PendingQuestion
from app.routes import chats_stream


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


def _seed_question_blocks(chat_id: str, question_ids: list[str]) -> None:
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
            "question_id": qid,
            "questions": [
              {"id": qid, "question": f"Question {qid}", "options": ["a", "b"]}
            ],
          }
          for qid in question_ids
        ],
      },
    ]
    db.commit()
  finally:
    db.close()


def _question_blocks(chat_id: str) -> list[dict]:
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    return [
      block
      for block in (chat.messages[-1].get("blocks") or [])
      if block.get("type") == "question"
    ]
  finally:
    db.close()


def _set_pending_messages(chat_id: str, pending: list[dict]) -> None:
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    chat.pending_messages = pending
    db.commit()
  finally:
    db.close()


def _set_activity_at(chat_id: str, value: datetime) -> None:
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    chat.activity_at = value
    db.commit()
  finally:
    db.close()


def _activity_at(chat_id: str) -> datetime:
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    return chat.activity_at
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
    old_activity = datetime(2000, 1, 1, tzinfo=UTC)
    _set_activity_at(chat.id, old_activity)

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
    assert res.json()["answer_turn"] == "same"
    # Future resolved with the submitted answers.
    assert fut.done()
    assert fut.result() == {"Pick one": "a"}
    # Registry cleared atomically by claim().
    assert questions.get(chat.id) is None
    assert _activity_at(chat.id).replace(tzinfo=UTC) > old_activity
    # No grace-period delay on the happy path. 500ms cap; 250ms
    # leaves comfortable headroom for slow CI without making the
    # test useless.
    assert elapsed < 0.25, (
      f"happy path waited unexpectedly long: {elapsed:.3f}s"
    )

  asyncio.run(go())


def test_answer_recovers_durable_question_without_live_pending(
  client, auth, chat, monkeypatch,
):
  """A restart kills the in-memory future but keeps the question block.

  Submitting the answer should not gray out or 410 the card. It records the
  answer, places the hidden continuation before unrelated queued work, and
  starts that recovered continuation.
  """
  scheduled: list[dict] = []

  def fake_schedule_continuation(**kwargs):
    scheduled.append(kwargs)
    # The test does not spawn a runner, so release the starting reservation the
    # real scheduler would hand to run_chat.
    chat_mod.discard_starting(kwargs["chat_id"])

  monkeypatch.setattr(
    chats_stream, "_schedule_continuation", fake_schedule_continuation,
  )

  async def go():
    qid = "q-recovered"
    _seed_question_block(chat.id, qid)
    _set_pending_messages(
      chat.id,
      [{"role": "user", "content": "queued-visible", "ts": 3}],
    )
    assert questions.get(chat.id) is None

    res = await asyncio.get_event_loop().run_in_executor(
      None,
      lambda: client.post(
        f"/api/chats/{chat.id}/messages",
        json={
          "content": "- Pick one: b",
          "hidden": True,
          "answers": {"Pick one": "b"},
          "question_id": qid,
        },
        headers=auth,
      ),
    )

    assert res.status_code == 202, res.text
    assert res.json()["status"] == "started"
    assert res.json()["answer_turn"] == "new"
    assert scheduled, "recovered answer should start a hidden continuation"
    assert scheduled[0]["next_user"]["hidden"] is True
    assert scheduled[0]["next_user"]["content"] == "- Pick one: b"

    db = SessionLocal()
    try:
      row = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
      question = [
        b for b in row.messages[1]["blocks"]
        if b.get("type") == "question"
      ][0]
      assert question["answers"] == {"Pick one": "b"}
      assert row.messages[-1]["hidden"] is True
      assert row.messages[-1]["content"] == "- Pick one: b"
      assert [m["content"] for m in row.pending_messages] == [
        "queued-visible"
      ]
      assert row.run_status == "running"
    finally:
      db.close()

  asyncio.run(go())


def test_answer_after_completed_stop_recovers_durable_question(
  client, auth, chat, monkeypatch,
):
  """Submit after Stop is a fresh continuation request, not a stale race.

  Stop cancels the in-memory future and leaves its tombstone behind. If the
  user subsequently answers the still-durable tail card, that later Submit
  should record the answer and start a hidden continuation. A request that
  began before Stop remains 410 (covered by the real lock-contention test).
  """
  scheduled: list[dict] = []

  def fake_schedule_continuation(**kwargs):
    scheduled.append(kwargs)
    chat_mod.discard_starting(kwargs["chat_id"])

  monkeypatch.setattr(
    chats_stream, "_schedule_continuation", fake_schedule_continuation,
  )

  async def go():
    qid = "q-stopped-then-submitted"
    _seed_question_block(chat.id, qid)
    loop = asyncio.get_event_loop()
    pending = PendingQuestion(
      question_id=qid,
      questions=[{"id": "q1", "question": "Pick one"}],
      future=loop.create_future(),
    )
    questions.register(chat.id, pending)
    questions.cancel(chat.id)
    assert questions.was_cancelled(chat.id, qid)
    assert pending.future.cancelled()

    res = await loop.run_in_executor(
      None,
      lambda: client.post(
        f"/api/chats/{chat.id}/messages",
        json={
          "content": "- Pick one: a",
          "hidden": True,
          "answers": {"Pick one": "a"},
          "question_id": qid,
        },
        headers=auth,
      ),
    )

    assert res.status_code == 202, res.text
    assert res.json()["status"] == "started"
    assert res.json()["answer_turn"] == "new"
    assert scheduled
    assert scheduled[0]["next_user"]["hidden"] is True
    db = SessionLocal()
    try:
      row = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
      question = next(
        block
        for msg in row.messages
        for block in (msg.get("blocks") or [])
        if block.get("question_id") == qid
      )
      assert question["answers"] == {"Pick one": "a"}
    finally:
      db.close()

  asyncio.run(go())


def test_answer_with_stale_question_id_returns_410_without_resolving_live(
  client, auth, chat,
):
  """A stale card must not resolve whichever question is currently pending."""
  async def go():
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    pending = PendingQuestion(
      question_id="q2",
      questions=[
        {"id": "q2", "question": "Question q2", "options": ["a", "b"]}
      ],
      future=fut,
    )
    questions.register(chat.id, pending)
    _seed_question_blocks(chat.id, ["q1", "q2"])

    res = client.post(
      f"/api/chats/{chat.id}/messages",
      json={
        "content": "answer",
        "hidden": True,
        "answers": {"Question q1": "a"},
        "question_id": "q1",
      },
      headers=auth,
    )

    assert res.status_code == 410, res.text
    assert questions.get(chat.id) is pending
    assert not fut.done()
    blocks = _question_blocks(chat.id)
    assert [b["question_id"] for b in blocks] == ["q1", "q2"]
    assert all("answers" not in b for b in blocks)
    questions.cancel(chat.id)

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
    assert res.json()["answer_turn"] == "same"
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
