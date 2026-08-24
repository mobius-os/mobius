"""Startup reconciliation of chats stranded "running" by a crash.

The runner registry that tracks "is this chat running" lives only in
memory, so an OOM / SIGKILL mid-turn leaves a durable ChatRun reading
"running" with no live registry entry.
``chat.reconcile_interrupted_chats`` runs once at lifespan startup and
resolves those rows so the user doesn't see a forever-spinning turn or
strand queued messages. These tests pin that contract; they exercise
the pure reconciliation function directly (the lifespan wiring is a
thin wrapped call around it).
"""

from datetime import UTC, datetime

from app import chat as chat_mod
from app import models
from app.runner_registry import RunnerKind, registry


def _make_chat(db, chat_id, **kwargs):
  running = kwargs.pop("running", False)
  started_at = kwargs.pop("started_at", datetime.now(UTC))
  c = models.Chat(id=chat_id, title="t", messages=kwargs.pop("messages", []))
  for k, v in kwargs.items():
    setattr(c, k, v)
  db.add(c)
  db.flush()
  if running:
    db.add(models.ChatRun(
      id=f"rt-{chat_id}",
      chat_id=chat_id,
      status="running",
      provider=c.provider,
      started_at=started_at,
    ))
  db.commit()
  db.refresh(c)
  return c


def test_fresh_chat_has_no_durable_run(db, chat):
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == chat.id,
  ).first() is None


def test_startup_reconciles_stale_running_chats(db):
  """A chat marked running with an empty registry is stale (its process
  died mid-turn) and must be reconciled: marker cleared, transcript
  resolved, and the queue PRESERVED so a restart doesn't discard the
  user's queued messages (owner-reported bug)."""
  _make_chat(
    db, "stale",
    running=True,
    started_at=datetime.now(UTC),
    messages=[{"role": "user", "content": "build me a thing"}],
    pending_messages=[{"role": "user", "content": "and another", "ts": 1}],
  )

  reconciled = chat_mod.reconcile_interrupted_chats(db)

  assert reconciled == ["stale"]
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "stale").first()
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == "stale",
    models.ChatRun.status == "running",
  ).first() is None
  # The queue is PRESERVED across the restart — clearing only the run
  # marker drops the chat into the markerless-queue state that self-heals
  # on the next user POST's stale-pending drain. It is NOT re-run as part
  # of the interrupted turn (whose own message is already in `messages`).
  assert row.pending_messages == [
    {"role": "user", "content": "and another", "ts": 1}
  ], "queued messages must survive a restart (not be dropped)"
  # The interrupted turn is surfaced as an assistant message so the
  # user's send isn't left unanswered.
  assert row.messages[-1]["role"] == "assistant"
  err_blocks = [b for b in row.messages[-1]["blocks"] if b["type"] == "error"]
  assert err_blocks, "an interrupted-turn error block must be appended"
  # `message` is the field MsgContent.jsx + events.process_event read.
  assert "paused" in err_blocks[0]["message"].lower()
  # The still-queued count is surfaced to the user (not "cleared").
  assert "1 queued message" in err_blocks[0]["message"]
  assert "still queued" in err_blocks[0]["message"]
  assert "cleared" not in err_blocks[0]["message"]


def test_reconcile_finalizes_running_tool_block(db):
  """A tool block left 'running' by the crash is forced to a terminal
  status server-side (not just masked client-side) and an error block
  is appended to the same assistant message."""
  _make_chat(
    db, "midtool",
    running=True,
    messages=[
      {"role": "user", "content": "do it"},
      {
        "role": "assistant",
        "content": "working",
        "blocks": [
          {"type": "text", "content": "working"},
          {"type": "tool", "tool": "Bash", "input": "ls",
           "output": "", "status": "running"},
        ],
      },
    ],
  )

  chat_mod.reconcile_interrupted_chats(db)

  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "midtool").first()
  blocks = row.messages[-1]["blocks"]
  tool_blocks = [b for b in blocks if b["type"] == "tool"]
  assert all(b["status"] != "running" for b in tool_blocks), (
    "no tool block may remain 'running' after reconciliation"
  )
  assert any(b["type"] == "error" for b in blocks)


def test_reconcile_merges_bounded_live_snapshot_before_restart_note(db):
  _make_chat(
    db,
    "live-snapshot",
    running=True,
    messages=[{"role": "user", "content": "keep this", "ts": 1}],
    live_assistant={
      "role": "assistant",
      "blocks": [{"type": "text", "content": "partial survives"}],
      "ts": 2,
    },
  )

  assert chat_mod.reconcile_interrupted_chats(db) == ["live-snapshot"]

  db.expire_all()
  row = db.get(models.Chat, "live-snapshot")
  assert row.live_assistant is None
  assert row.messages[-1]["ts"] == 2
  assert row.messages[-1]["blocks"][0]["content"] == "partial survives"
  assert row.messages[-1]["blocks"][-1]["type"] == "error"


def test_reconcile_rebuilds_open_question_barrier_from_repaired_tail(db):
  """A pre-crash Finalize clear cannot orphan an unanswered card.

  The transcript is the recovery source: the restart note belongs before the
  trailing question and the durable admission marker must name that question,
  even if the pre-restart marker was already null.
  """
  _make_chat(
    db,
    "question-recovery",
    running=True,
    messages=[
      {"role": "user", "content": "Review PR #787", "ts": 1},
      {
        "role": "assistant",
        "ts": 2,
        "blocks": [{
          "type": "question",
          "question_id": "owner-decision",
          "questions": [{
            "id": "push_requeue_787",
            "question": "Push and requeue PR #787?",
          }],
        }],
      },
    ],
    pending_messages=[{
      "role": "user",
      "content": "<wait_result>PR checks completed</wait_result>",
      "hidden": True,
      "ts": 3,
      "cid": "wait-result-787",
    }],
    pending_question_id=None,
  )

  assert chat_mod.reconcile_interrupted_chats(db) == ["question-recovery"]

  db.expire_all()
  row = db.get(models.Chat, "question-recovery")
  blocks = row.messages[-1]["blocks"]
  assert [block["type"] for block in blocks[-2:]] == ["error", "question"]
  assert blocks[-1]["question_id"] == "owner-decision"
  assert blocks[-1].get("answers") is None
  assert row.pending_question_id == "owner-decision"
  assert [item["cid"] for item in row.pending_messages] == ["wait-result-787"]


def test_reconcile_appends_turn_when_no_assistant_message(db):
  """If the process died before any assistant content persisted, the
  interruption becomes a standalone assistant turn rather than mutating
  the user's own message."""
  _make_chat(
    db, "early",
    running=True,
    messages=[{"role": "user", "content": "hi"}],
  )

  chat_mod.reconcile_interrupted_chats(db)

  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "early").first()
  assert len(row.messages) == 2
  assert row.messages[0]["role"] == "user"
  assert row.messages[1]["role"] == "assistant"
  assert any(b["type"] == "error" for b in row.messages[1]["blocks"])


def test_reconcile_leaves_idle_chats_untouched(db):
  """Chats not marked running must not be reconciled — no transcript
  mutation, no return entry."""
  _make_chat(
    db, "idle",
    messages=[{"role": "user", "content": "done long ago"}],
    pending_messages=[],
  )

  reconciled = chat_mod.reconcile_interrupted_chats(db)

  assert "idle" not in reconciled
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "idle").first()
  assert len(row.messages) == 1, "idle chat transcript must be untouched"


def test_reconcile_skips_soft_deleted_chats(db):
  """A soft-deleted chat that happened to crash mid-turn is on its way
  out — don't resurrect it into the user's view."""
  _make_chat(
    db, "deleted",
    running=True,
    deleted_at=datetime.now(UTC),
    messages=[{"role": "user", "content": "x"}],
  )

  reconciled = chat_mod.reconcile_interrupted_chats(db)

  assert "deleted" not in reconciled


def test_reconcile_skips_chat_with_live_registry_entry(db):
  """Belt-and-suspenders: a chat that IS in the registry has a turn
  genuinely in flight; reconciliation must not yank its transcript.
  (Cannot happen at a cold boot — the registry is empty — but guards a
  future warm-restart caller.)"""
  class _Handle:
    chat_id = "live"
    kind = RunnerKind.CLAUDE_SDK

    async def stop(self, timeout=2.0):
      return True

  _make_chat(
    db, "live",
    running=True,
    messages=[{"role": "user", "content": "still going"}],
  )
  registry.register(_Handle())

  reconciled = chat_mod.reconcile_interrupted_chats(db)

  assert "live" not in reconciled
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "live").first()
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == "live",
    models.ChatRun.status == "running",
  ).one()
  assert len(row.messages) == 1


def test_finish_run_closes_the_durable_run(db, chat):
  """C2: the SET is folded into the turn's StartTurn / PromotePending
  writer-actor command (covered in the writer-contention suite); the
  CLEAR routes through the actor's FinishRun. Seed a running marker
  directly, then assert the actor-routed clear empties it."""
  import asyncio
  from datetime import UTC, datetime

  db.add(models.ChatRun(
    id="rt-finish",
    chat_id=chat.id,
    status="running",
    provider=chat.provider,
    started_at=datetime.now(UTC),
  ))
  db.commit()

  asyncio.run(chat_mod._finish_run(chat.id, "rt-finish"))

  db.expire_all()
  run = db.get(models.ChatRun, "rt-finish")
  assert run.status == "completed"


def test_reconcile_assigns_ts_to_interrupted_messages(db):
  """Reconciled assistant messages must carry a stable ts: build_assistant_message
  omits ts and the frontend bridge drops ts-less messages, so reconciliation has
  to preserve an existing ts or assign a fresh one."""
  _make_chat(
    db, "had-assistant",
    running=True,
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      {"role": "assistant",
       "blocks": [{"type": "text", "content": "partial"}], "ts": 2},
    ],
  )
  _make_chat(
    db, "no-assistant",
    running=True,
    messages=[{"role": "user", "content": "hi", "ts": 5}],
  )

  chat_mod.reconcile_interrupted_chats(db)
  db.expire_all()

  a = db.query(models.Chat).filter(models.Chat.id == "had-assistant").first()
  assert a.messages[-1]["role"] == "assistant"
  assert a.messages[-1].get("ts") == 2, "existing assistant ts must be preserved"

  b = db.query(models.Chat).filter(models.Chat.id == "no-assistant").first()
  assert b.messages[-1]["role"] == "assistant"
  assert b.messages[-1].get("ts") is not None, "standalone reconciled msg needs a ts"
  assert b.messages[-1]["ts"] > 5, "fresh ts must follow existing messages"


def test_reconcile_warns_on_markerless_pending_queue_but_leaves_it(db, caplog):
  """A Stop's ClearPending committing just before a racing AppendPending leaves
  an idle chat with a non-empty pending queue. Reconciliation must
  NOT consume it — auto-promoting at startup would spawn a post-crash turn, and
  the next POST's stale-pending drain is the repair path — but it WARNS so a
  never-drained accumulating queue is visible rather than silent.
  """
  _make_chat(
    db, "markerless",
    pending_messages=[{"role": "user", "content": "queued", "ts": 1}],
  )

  with caplog.at_level("WARNING"):
    reconciled = chat_mod.reconcile_interrupted_chats(db)

  assert "markerless" not in reconciled, "an idle queue must not be consumed"
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "markerless").first()
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == "markerless",
    models.ChatRun.status == "running",
  ).first() is None
  assert len(row.pending_messages) == 1, (
    "the queue is left intact for the next-POST stale-pending drain"
  )
  assert any(
    "idle pending queue" in r.getMessage() for r in caplog.records
  ), "an accumulating idle queue must be surfaced as a warning"


def test_reconcile_preserved_queue_drains_on_next_post(db):
  """End-to-end of the owner-reported fix: a restart preserves the queue
  (reconcile leaves pending_messages set with no running row), and the
  preserved queue then drains via the same promote path the next user
  POST's stale-pending self-heal runs. This proves the queue isn't just
  retained but is actually recoverable — the user's queued work survives
  a restart and gets answered on the next interaction."""
  import asyncio

  from app import chat_queue

  _make_chat(
    db, "restart-then-drain",
    running=True,
    started_at=datetime.now(UTC),
    session_id="sess-restart",
    messages=[{"role": "user", "content": "the interrupted turn", "ts": 1}],
    pending_messages=[
      {"role": "user", "content": "queued one", "ts": 2},
      {"role": "user", "content": "queued two", "ts": 3},
    ],
  )

  # 1. Restart reconciliation: marker cleared, queue PRESERVED.
  chat_mod.reconcile_interrupted_chats(db)
  db.expire_all()
  row = db.query(models.Chat).filter(
    models.Chat.id == "restart-then-drain"
  ).first()
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == "restart-then-drain",
    models.ChatRun.status == "running",
  ).first() is None
  assert [m["content"] for m in row.pending_messages] == [
    "queued one", "queued two"
  ], "the queue must survive the restart"

  # 2. The next POST's stale-pending self-heal drains the head (here the
  #    whole queue, collapsed) — the same promote path send_message runs
  #    when `not is_chat_running and chat.pending_messages`.
  async def go():
    return await chat_queue.promote_pending_messages_locked(
      db, "restart-then-drain", "rt-restart",
    )

  next_msgs, promoted, sid = asyncio.run(go())
  assert promoted is not None
  assert promoted["content"] == "queued one\nqueued two", (
    "the preserved queue collapses + promotes on the next interaction"
  )
  assert sid == "sess-restart", "the session resumes (no context loss)"
  db.refresh(row)
  assert row.pending_messages == [], "the queue drained, nothing lost"
