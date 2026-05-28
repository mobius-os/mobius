# backend/tests/test_augmentation.py
"""Tests that send_message appends uploaded-file info to the user message."""
import io
from dataclasses import dataclass
from unittest.mock import patch

from app.broadcast import get_broadcast
from app.runner_registry import RunnerKind, registry


def _mark_done(chat_id):
  """Mark the broadcast completed so the next test doesn't see it running."""
  bc = get_broadcast(chat_id)
  if bc and bc.running:
    bc.mark_completed()


@dataclass
class _FakeRunningHandle:
  """Minimal registry handle that pretends a chat is mid-turn.

  Only the registry protocol surface (`chat_id`, `kind`, `stop`) is
  needed — these tests register a fake handle so `is_chat_running`
  returns True, then exercise the queue/cancel paths.
  """

  chat_id: str
  proc: object  # MagicMock with .returncode; kept for parity with prior shape
  kind: RunnerKind = RunnerKind.SUBPROCESS

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    return True


def _register_proc(chat_id, proc):
  registry.register(_FakeRunningHandle(chat_id=chat_id, proc=proc))


def test_no_augmentation_without_uploads(client, db, auth, chat):
  """When no files are uploaded, the message content must be unchanged."""
  captured = []

  async def fake_run_chat(msgs, chat_id, session_id, **kwargs):
    captured.extend(msgs)
    _mark_done(chat_id)

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "hello"},
      headers=auth,
    )

  user_msg = next(m for m in captured if m.role == "user")
  assert user_msg.content == "hello"
  assert "[Files" not in user_msg.content


def test_attachments_saved_in_message(client, db, auth, chat):
  """When attachments are sent, they must be saved in the message dict."""
  attachments = [
    {"name": "photo.png", "size": 1024, "mime_type": "image/png"},
  ]

  captured = []

  async def fake_run_chat(msgs, chat_id, session_id, **kwargs):
    captured.append({"msgs": msgs, "attachments": kwargs.get("attachments")})
    _mark_done(chat_id)

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    resp = client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "check this", "attachments": attachments},
      headers=auth,
    )

  assert resp.status_code == 202
  assert len(captured) == 1
  assert captured[0]["attachments"] == attachments


def test_augmentation_with_uploads(client, db, auth, chat):
  """When files are uploaded, the file list must be appended."""
  client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("report.pdf", io.BytesIO(b"data"), "application/pdf"))],
    headers=auth,
  )

  captured = []

  async def fake_run_chat(msgs, chat_id, session_id, **kwargs):
    captured.extend(msgs)
    _mark_done(chat_id)

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "analyze this"},
      headers=auth,
    )

  user_msg = next(m for m in captured if m.role == "user")
  assert "report.pdf" in user_msg.content
  assert "[Files in this session:" in user_msg.content


def test_message_saved_before_run_chat(client, db, auth, chat):
  """The user message must be in the DB before the background task runs."""
  from app import models

  captured_messages = []

  async def fake_run_chat(msgs, chat_id, session_id, **kwargs):
    # Read messages from DB at the moment run_chat starts.
    from app.database import SessionLocal
    s = SessionLocal()
    c = s.query(models.Chat).filter(models.Chat.id == chat_id).first()
    captured_messages.extend(c.messages or [])
    s.close()
    _mark_done(chat_id)

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "hello world"},
      headers=auth,
    )

  assert len(captured_messages) == 1
  assert captured_messages[0]["role"] == "user"
  assert "hello world" in captured_messages[0]["content"]


def test_send_while_running_queues_message(client, db, auth, chat):
  """Sending a message while agent is running queues it (202 + queued)."""
  from unittest.mock import MagicMock

  mock_proc = MagicMock()
  mock_proc.returncode = None
  _register_proc(chat.id, mock_proc)

  try:
    resp = client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "second message"},
      headers=auth,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "queued"
    assert data["position"] == 1
    # The server must return the assigned ts so the frontend can
    # replace its optimistic ts and DELETE-by-ts works.
    assert "ts" in data and isinstance(data["ts"], int)

    # Verify message saved to pending_messages in DB with the same ts.
    db.refresh(chat)
    assert len(chat.pending_messages) == 1
    assert chat.pending_messages[0]["content"] == "second message"
    assert chat.pending_messages[0]["ts"] == data["ts"]

    # Cancel by the server-returned ts must actually remove the message.
    cancel = client.delete(
      f"/api/chats/{chat.id}/pending/{data['ts']}", headers=auth,
    )
    assert cancel.status_code == 200
    assert cancel.json()["pending_messages"] == []
  finally:
    registry.unregister(chat.id, RunnerKind.SUBPROCESS)


def test_multiple_queued_messages_ordered(client, db, auth, chat):
  """Multiple sends while running are queued in order."""
  from unittest.mock import MagicMock

  mock_proc = MagicMock()
  mock_proc.returncode = None
  _register_proc(chat.id, mock_proc)

  try:
    resp1 = client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "first queued"},
      headers=auth,
    )
    resp2 = client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "second queued"},
      headers=auth,
    )
    assert resp1.json()["position"] == 1
    assert resp2.json()["position"] == 2

    db.refresh(chat)
    assert len(chat.pending_messages) == 2
    assert chat.pending_messages[0]["content"] == "first queued"
    assert chat.pending_messages[1]["content"] == "second queued"
  finally:
    registry.unregister(chat.id, RunnerKind.SUBPROCESS)


def test_stale_pending_drains_on_fresh_send(client, db, auth, chat):
  """If pending exists but no run is active (e.g. server crashed
  mid-turn), a fresh send must queue at the end AND kick off a run
  that drains the queue from the head — not replace the queue with
  the new message."""
  from app.runner_registry import registry

  # Seed stale pending (simulating crash recovery).
  chat.pending_messages = [
    {"role": "user", "content": "stale 1", "ts": 100},
    {"role": "user", "content": "stale 2", "ts": 200},
  ]
  db.commit()

  async def fake_run_chat(*args, **kwargs):
    pass

  with patch("app.chat.run_chat", new=fake_run_chat):
    resp = client.post(
      f"/api/chats/{chat.id}/messages",
      json={"content": "new send"},
      headers=auth,
    )

  try:
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    # New send is at the end of the queue.
    assert resp.json()["position"] == 2  # 2 stale, one promoted, new + remaining
    db.refresh(chat)
    # First stale was promoted to messages.
    assert chat.messages[-1]["content"] == "stale 1"
    # Remaining stale + new are in pending.
    contents = [m["content"] for m in chat.pending_messages]
    assert contents == ["stale 2", "new send"]
    # Run was scheduled (gen bumped + starting marker set).
    assert chat.id in registry.all_alive_chat_ids()
  finally:
    registry.discard_starting(chat.id)
    registry.forget(chat.id)


def test_concurrent_queue_appends_dont_lose_messages(db, auth, chat):
  """Concurrent POSTs to /messages while running must serialize and
  preserve every queued message. The per-chat asyncio lock in
  _append_to_pending prevents the read-modify-write race that would
  otherwise drop one of the messages."""
  import asyncio
  from app import models
  from app.routes.chats_stream import _append_to_pending, _ensure_unique_ts
  from app.database import SessionLocal
  from app.schemas import SendMessage
  from unittest.mock import MagicMock

  mock_proc = MagicMock()
  mock_proc.returncode = None
  _register_proc(chat.id, mock_proc)

  async def append_one(content):
    # Each task uses its OWN db session, mirroring real request flow
    # where Depends(get_db) yields a fresh session per request.
    s = SessionLocal()
    try:
      c = s.query(models.Chat).filter(models.Chat.id == chat.id).first()
      body = SendMessage(content=content)
      # Force a scheduler yield BEFORE entering the lock so other
      # tasks have a chance to interleave. Without this, asyncio.gather
      # can run each task's lock acquisition serially and the test
      # never actually exercises the contended path.
      await asyncio.sleep(0)
      await _append_to_pending(c, body, s)
    finally:
      s.close()

  async def run_all():
    await asyncio.gather(*[append_one(f"msg-{i}") for i in range(10)])

  try:
    asyncio.run(run_all())
    db.refresh(chat)
    contents = {m["content"] for m in chat.pending_messages}
    assert len(chat.pending_messages) == 10, (
      f"Lost messages under concurrent append: {len(chat.pending_messages)}/10"
    )
    assert contents == {f"msg-{i}" for i in range(10)}
    # All ts must be unique.
    tss = [m["ts"] for m in chat.pending_messages]
    assert len(set(tss)) == 10, "ts collisions under concurrent append"
  finally:
    registry.unregister(chat.id, RunnerKind.SUBPROCESS)


def test_concurrent_cancel_and_append_dont_lose_messages(db, auth, chat):
  """A DELETE racing a POST must not undo the POST. Without the queue
  lock on DELETE, the DELETE could read a stale pending snapshot
  (missing the in-flight POST's append) and commit, dropping the new
  message."""
  import asyncio
  from app import models
  from app.routes.chats_stream import _append_to_pending, cancel_pending_message
  from app.database import SessionLocal
  from app.schemas import SendMessage
  from unittest.mock import MagicMock

  # Seed two queued messages.
  chat.pending_messages = [
    {"role": "user", "content": "keep", "ts": 100},
    {"role": "user", "content": "cancel me", "ts": 200},
  ]
  db.commit()

  mock_proc = MagicMock()
  mock_proc.returncode = None
  _register_proc(chat.id, mock_proc)

  async def do_append():
    s = SessionLocal()
    try:
      c = s.query(models.Chat).filter(models.Chat.id == chat.id).first()
      await asyncio.sleep(0)  # yield to let cancel start
      await _append_to_pending(c, SendMessage(content="appended"), s)
    finally:
      s.close()

  async def do_cancel():
    s = SessionLocal()
    try:
      from app.deps import get_current_owner
      owner = s.query(models.Owner).first()
      await cancel_pending_message(
        chat_id=chat.id, ts=200, _=owner, db=s,
      )
    finally:
      s.close()

  async def run():
    await asyncio.gather(do_cancel(), do_append())

  try:
    asyncio.run(run())
    db.refresh(chat)
    contents = [m["content"] for m in chat.pending_messages]
    # All non-canceled survive: original "keep" + new "appended".
    assert "keep" in contents
    assert "appended" in contents
    assert "cancel me" not in contents
  finally:
    registry.unregister(chat.id, RunnerKind.SUBPROCESS)


def test_pending_ts_strictly_unique_under_collision(client, db, auth, chat):
  """Two queued sends within the same ms must get distinct ts values
  so DELETE-by-ts targets exactly one entry and React keys stay unique."""
  from unittest.mock import MagicMock, patch

  mock_proc = MagicMock()
  mock_proc.returncode = None
  _register_proc(chat.id, mock_proc)

  try:
    # Force time.time() to return the same value for both POSTs to
    # simulate the same-millisecond collision case.
    with patch("app.routes.chats_stream.time.time", return_value=1.234):
      r1 = client.post(
        f"/api/chats/{chat.id}/messages",
        json={"content": "a"}, headers=auth,
      )
      r2 = client.post(
        f"/api/chats/{chat.id}/messages",
        json={"content": "b"}, headers=auth,
      )
    assert r1.status_code == 202 and r2.status_code == 202
    ts1 = r1.json()["ts"]
    ts2 = r2.json()["ts"]
    assert ts1 != ts2, f"ts collided: {ts1} == {ts2}"
    db.refresh(chat)
    assert {m["ts"] for m in chat.pending_messages} == {ts1, ts2}

    # Cancel ts1 removes only that one.
    client.delete(f"/api/chats/{chat.id}/pending/{ts1}", headers=auth)
    db.refresh(chat)
    assert [m["ts"] for m in chat.pending_messages] == [ts2]
  finally:
    registry.unregister(chat.id, RunnerKind.SUBPROCESS)


def test_promote_pending_messages(db):
  """_promote_pending_messages moves only the first pending into transcript."""
  import asyncio
  from app import models
  from app.chat_queue import promote_pending_messages as _promote_pending_messages

  chat = models.Chat(
    id="promote-test",
    title="Test",
    messages=[{"role": "user", "content": "first"}],
    pending_messages=[
      {"role": "user", "content": "queued msg 1", "ts": 123},
      {"role": "user", "content": "queued msg 2", "ts": 456},
    ],
    session_id="sess-1",
  )
  db.add(chat)
  db.commit()

  next_msgs, next_user, session_id = asyncio.run(
    _promote_pending_messages(db, "promote-test"),
  )

  db.refresh(chat)
  # Only first pending promoted, second remains.
  assert len(chat.pending_messages) == 1
  assert chat.pending_messages[0]["content"] == "queued msg 2"
  # First promoted to transcript.
  assert len(chat.messages) == 2
  assert chat.messages[1]["content"] == "queued msg 1"
  # Returned values for next run.
  assert next_user["content"] == "queued msg 1"
  assert session_id == "sess-1"
  assert len(next_msgs) == 2  # full history + new user msg
  # Promote does NOT claim _starting any more — caller manages it.
  # The round-7 design that claimed _starting here was broken: the
  # original send's _starting claim was never released by the time
  # finally called promote, so mark_starting always failed and queued
  # messages were silently never promoted in production.


def test_promote_drains_all_sequentially(db):
  """Calling _promote_pending_messages repeatedly drains the queue one by one."""
  import asyncio
  from app import models
  from app.chat_queue import promote_pending_messages as _promote_pending_messages

  chat = models.Chat(
    id="drain-test",
    title="Test",
    messages=[],
    pending_messages=[
      {"role": "user", "content": "a", "ts": 1},
      {"role": "user", "content": "b", "ts": 2},
      {"role": "user", "content": "c", "ts": 3},
    ],
    session_id="sess-2",
  )
  db.add(chat)
  db.commit()

  _, first, _ = asyncio.run(_promote_pending_messages(db, "drain-test"))
  assert first["content"] == "a"
  db.refresh(chat)
  assert len(chat.pending_messages) == 2

  _, second, _ = asyncio.run(_promote_pending_messages(db, "drain-test"))
  assert second["content"] == "b"
  db.refresh(chat)
  assert len(chat.pending_messages) == 1

  _, third, _ = asyncio.run(_promote_pending_messages(db, "drain-test"))
  assert third["content"] == "c"
  db.refresh(chat)
  assert len(chat.pending_messages) == 0

  _, none_user, _ = asyncio.run(_promote_pending_messages(db, "drain-test"))
  assert none_user is None


def test_promote_locked_atomic_with_append(db):
  """Regression for the late-drain race. The unlocked variant of
  promote can be called by the finally's late-drain critical section
  while holding the queue lock, so a concurrent _append_to_pending
  serializes against it. Without this atomic pair, a POST appending
  to an empty queue right between finally's promote check and the
  wrapper's _starting release would be stranded."""
  import asyncio
  from app import models
  from app.chat_queue import (
    promote_pending_messages_locked as _promote_pending_messages_locked,
    get_lock as get_queue_lock,
  )
  from app.routes.chats_stream import _append_to_pending
  from app.database import SessionLocal
  from app.schemas import SendMessage

  chat = models.Chat(
    id="late-drain-test",
    title="t",
    messages=[],
    pending_messages=[],
    session_id="sess-ld",
  )
  db.add(chat)
  db.commit()

  appended_first = False

  async def appender():
    """Simulates a POST arriving during the finally's critical section."""
    nonlocal appended_first
    s = SessionLocal()
    try:
      c = s.query(models.Chat).filter(
        models.Chat.id == "late-drain-test",
      ).first()
      # The append acquires the same queue lock and must wait for the
      # late-drain critical section to release. After our turn, we
      # should see the message we just appended.
      await _append_to_pending(c, SendMessage(content="late"), s)
      appended_first = True
    finally:
      s.close()

  async def finally_late_drain_critical_section():
    """Simulates the finally's atomic check + decide."""
    # Acquire the lock and yield so appender starts waiting.
    async with get_queue_lock("late-drain-test"):
      await asyncio.sleep(0)  # give appender a chance to await acquire
      # Inside the lock, promote unlocked. Pending should be empty
      # (appender is still waiting).
      _, user, _ = _promote_pending_messages_locked(db, "late-drain-test")
      return user

  async def run():
    # Start the critical section first; appender will block on the lock.
    crit = asyncio.create_task(finally_late_drain_critical_section())
    # Tiny yield to let crit acquire the lock first.
    await asyncio.sleep(0)
    app_task = asyncio.create_task(appender())
    user = await crit
    await app_task
    return user

  user_inside_lock = asyncio.run(run())
  # The promote inside the critical section saw an empty queue
  # (appender's commit hadn't happened yet — lock blocked it).
  assert user_inside_lock is None
  # After the lock released, the append committed. The DB now has
  # the message — proving the append wasn't lost.
  db.refresh(chat)
  assert len(chat.pending_messages) == 1
  assert chat.pending_messages[0]["content"] == "late"
  # In the real code, the finally would call _starting.discard inside
  # the critical section after seeing user==None. Then the next user
  # send (or this test's wrapper) would trigger the stale-pending
  # drain via the chats_stream.py route.


def test_promote_succeeds_when_starting_is_held_by_current_run(db):
  """Regression for the round-7 bug: _promote must succeed even when
  _starting already contains the chat_id (which is normal — the
  original send's claim hasn't been released yet when the finally
  fires). Without this, queued messages get silently stranded in
  production."""
  import asyncio
  from app import models
  from app.chat_queue import promote_pending_messages as _promote_pending_messages

  chat = models.Chat(
    id="starting-held-test",
    title="Test",
    messages=[],
    pending_messages=[{"role": "user", "content": "queued", "ts": 1}],
    session_id="sess-x",
  )
  db.add(chat)
  db.commit()

  # Simulate the in-progress run's _starting claim.
  registry.mark_starting("starting-held-test")
  try:
    msgs, user, sid = asyncio.run(
      _promote_pending_messages(db, "starting-held-test"),
    )
    # MUST succeed despite _starting being held — this is the bug fix.
    assert user is not None
    assert user["content"] == "queued"
    db.refresh(chat)
    assert chat.pending_messages == []
  finally:
    registry.discard_starting("starting-held-test")


def test_promote_and_append_dont_lose_messages(db):
  """The killer race: turn-end promote racing a concurrent POST append
  must not silently drop the append. With the lock on _promote, the
  append's commit is preserved."""
  import asyncio
  from app import models
  from app.chat_queue import promote_pending_messages as _promote_pending_messages
  from app.routes.chats_stream import _append_to_pending
  from app.database import SessionLocal
  from app.schemas import SendMessage

  chat = models.Chat(
    id="race-test",
    title="t",
    messages=[],
    pending_messages=[
      {"role": "user", "content": "head", "ts": 1},
    ],
    session_id="sess-r",
  )
  db.add(chat)
  db.commit()

  async def do_promote():
    s = SessionLocal()
    try:
      await asyncio.sleep(0)  # let append get into the lock queue
      await _promote_pending_messages(s, "race-test")
    finally:
      s.close()

  async def do_append():
    s = SessionLocal()
    try:
      c = s.query(models.Chat).filter(models.Chat.id == "race-test").first()
      await _append_to_pending(c, SendMessage(content="late"), s)
    finally:
      s.close()

  async def run():
    await asyncio.gather(do_promote(), do_append())

  try:
    asyncio.run(run())
    db.refresh(chat)
    transcript_contents = [m["content"] for m in chat.messages]
    pending_contents = [m["content"] for m in chat.pending_messages]
    # Either ordering is OK as long as nothing is lost:
    #   - promote first: messages=[head], pending=[late]
    #   - append first: messages=[head], pending=[late]
    # ("head" is always promoted because it was the first in pending,
    # regardless of when "late" was appended.)
    assert "head" in transcript_contents
    assert "late" in pending_contents or "late" in transcript_contents
    # Total entries must equal what we put in (1 seeded + 1 appended).
    assert len(transcript_contents) + len(pending_contents) == 2
  finally:
    registry.discard_starting("race-test")
    registry.forget("race-test")


def test_stop_clears_pending_queue(db):
  """stop_chat must clear chat.pending_messages and bump
  _run_generation. The dying run_chat's finally checks ownership
  via the gen counter and skips _promote_pending_messages /
  _schedule_continuation when bumped — that prevents the backend
  from double-firing the queue (the frontend's handleStop already
  collapses the queue into a combined doSend; if backend also
  drained, queued work would land twice). See CLAUDE.md
  `Stop-chat contract`.
  """
  import asyncio
  from app import models
  from app.chat import current_run_generation, stop_chat
  from unittest.mock import MagicMock

  chat = models.Chat(
    id="stop-clears",
    title="t",
    messages=[],
    pending_messages=[{"role": "user", "content": "queued", "ts": 1}],
  )
  db.add(chat)
  db.commit()

  mock_proc = MagicMock()
  mock_proc.returncode = None
  _register_proc("stop-clears", mock_proc)

  try:
    asyncio.run(stop_chat("stop-clears", db=db))
    db.refresh(chat)
    assert chat.pending_messages == []
    assert current_run_generation("stop-clears") == 1
  finally:
    registry.unregister("stop-clears", RunnerKind.SUBPROCESS)
    registry.forget("stop-clears")


def test_stop_chat_for_clears_pending_queue(db):
  """stop_chat_for must clear pending_messages too (mirror of the
  global path test). Backend Stop is purely interrupt; frontend
  owns the collapse-and-resend.
  """
  import asyncio
  from app import models
  from app.chat import stop_chat_for

  chat = models.Chat(
    id="stop-for-clears",
    title="t",
    messages=[],
    pending_messages=[{"role": "user", "content": "queued", "ts": 1}],
  )
  db.add(chat)
  db.commit()

  try:
    asyncio.run(stop_chat_for("stop-for-clears", db=db))
    db.refresh(chat)
    assert chat.pending_messages == []
  finally:
    registry.forget("stop-for-clears")


def test_cancel_pending_message_by_ts(client, db, auth):
  """DELETE /chats/{id}/pending/{ts} removes a queued message."""
  from app import models

  c = models.Chat(
    id="cancel-test",
    title="t",
    messages=[],
    pending_messages=[
      {"role": "user", "content": "keep", "ts": 100},
      {"role": "user", "content": "cancel me", "ts": 200},
      {"role": "user", "content": "keep too", "ts": 300},
    ],
  )
  db.add(c)
  db.commit()

  resp = client.delete("/api/chats/cancel-test/pending/200", headers=auth)
  assert resp.status_code == 200
  data = resp.json()
  assert len(data["pending_messages"]) == 2
  assert [m["ts"] for m in data["pending_messages"]] == [100, 300]

  db.refresh(c)
  assert [m["ts"] for m in c.pending_messages] == [100, 300]


def test_cancel_pending_missing_ts_noop(client, db, auth):
  """DELETE with a ts not in the queue returns the unchanged queue."""
  from app import models

  c = models.Chat(
    id="cancel-noop",
    title="t",
    messages=[],
    pending_messages=[{"role": "user", "content": "x", "ts": 100}],
  )
  db.add(c)
  db.commit()

  resp = client.delete("/api/chats/cancel-noop/pending/999", headers=auth)
  assert resp.status_code == 200
  assert len(resp.json()["pending_messages"]) == 1


def test_get_chat_returns_pending_messages(client, db, auth):
  """GET /chats/{id} must include pending_messages so client can hydrate."""
  from app import models

  c = models.Chat(
    id="hydrate-test",
    title="t",
    messages=[],
    pending_messages=[
      {"role": "user", "content": "wait for me", "ts": 1},
    ],
  )
  db.add(c)
  db.commit()

  resp = client.get("/api/chats/hydrate-test", headers=auth)
  assert resp.status_code == 200
  data = resp.json()
  assert "pending_messages" in data
  assert len(data["pending_messages"]) == 1
  assert data["pending_messages"][0]["content"] == "wait for me"


def test_generation_mismatch_does_not_clear_newer_starting(db):
  """Old run_chat with stale generation must not clear _starting."""
  from app.chat import current_run_generation
  chat_id = "gen-race-test"

  # Simulate: old run queued at gen 0, then stop bumps to gen 1.
  assert registry.mark_starting(chat_id) is True

  # Stop bumps generation (simulating stop_chat_for).
  assert registry.bump_generation(chat_id) == 1

  # Old run_chat checks generation — mismatch, should NOT clear
  # _starting because the newer run owns it.
  old_gen = 0
  if old_gen != current_run_generation(chat_id):
    pass  # would return early in real code
  else:
    registry.discard_starting(chat_id)

  # _starting should still contain chat_id (newer run owns it).
  assert chat_id in registry.starting_chat_ids()

  # Cleanup.
  registry.discard_starting(chat_id)
  registry.forget(chat_id)
