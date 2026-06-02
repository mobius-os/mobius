"""Startup reconciliation of chats stranded "running" by a crash.

The runner registry that tracks "is this chat running" lives only in
memory, so an OOM / SIGKILL mid-turn leaves the chat's durable
``run_status`` column reading "running" with no live registry entry.
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
  c = models.Chat(id=chat_id, title="t", messages=kwargs.pop("messages", []))
  for k, v in kwargs.items():
    setattr(c, k, v)
  db.add(c)
  db.commit()
  db.refresh(c)
  return c


def test_run_status_column_defaults_none(db, chat):
  """A freshly created chat is not marked running."""
  assert chat.run_status is None
  assert chat.run_started_at is None


def test_startup_reconciles_stale_running_chats(db):
  """A chat marked running with an empty registry is stale (its process
  died mid-turn) and must be reconciled: marker cleared, pending
  dropped, transcript resolved."""
  _make_chat(
    db, "stale",
    run_status="running",
    run_started_at=datetime.now(UTC),
    messages=[{"role": "user", "content": "build me a thing"}],
    pending_messages=[{"role": "user", "content": "and another", "ts": 1}],
  )

  reconciled = chat_mod.reconcile_interrupted_chats(db)

  assert reconciled == ["stale"]
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "stale").first()
  assert row.run_status is None, "stale running marker must be cleared"
  assert row.run_started_at is None
  assert row.pending_messages == [], "stranded queued messages must be dropped"
  # The interrupted turn is surfaced as an assistant message so the
  # user's send isn't left unanswered.
  assert row.messages[-1]["role"] == "assistant"
  err_blocks = [b for b in row.messages[-1]["blocks"] if b["type"] == "error"]
  assert err_blocks, "an interrupted-turn error block must be appended"
  # `message` is the field MsgContent.jsx + events.process_event read.
  assert "interrupted" in err_blocks[0]["message"].lower()
  # The dropped-queue count is surfaced to the user.
  assert "1 queued message" in err_blocks[0]["message"]


def test_reconcile_finalizes_running_tool_block(db):
  """A tool block left 'running' by the crash is forced to a terminal
  status server-side (not just masked client-side) and an error block
  is appended to the same assistant message."""
  _make_chat(
    db, "midtool",
    run_status="running",
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


def test_reconcile_appends_turn_when_no_assistant_message(db):
  """If the process died before any assistant content persisted, the
  interruption becomes a standalone assistant turn rather than mutating
  the user's own message."""
  _make_chat(
    db, "early",
    run_status="running",
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
    run_status=None,
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
    run_status="running",
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
    run_status="running",
    messages=[{"role": "user", "content": "still going"}],
  )
  registry.register(_Handle())

  reconciled = chat_mod.reconcile_interrupted_chats(db)

  assert "live" not in reconciled
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "live").first()
  assert row.run_status == "running", "a live turn's marker must survive"
  assert len(row.messages) == 1


def test_clear_run_status_clears_the_durable_marker(db, chat):
  """C2: the SET is folded into the turn's StartTurn / PromotePending
  writer-actor command (covered in the writer-contention suite); the
  CLEAR routes through the actor's ClearRunStatus. Seed a running marker
  directly, then assert the actor-routed clear empties it."""
  import asyncio
  from datetime import UTC, datetime

  chat.run_status = "running"
  chat.run_started_at = datetime.now(UTC)
  db.commit()

  asyncio.run(chat_mod._clear_run_status(chat.id))

  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
  assert row.run_status is None
  assert row.run_started_at is None


def test_reconcile_assigns_ts_to_interrupted_messages(db):
  """Reconciled assistant messages must carry a stable ts: build_assistant_message
  omits ts and the frontend bridge drops ts-less messages, so reconciliation has
  to preserve an existing ts or assign a fresh one."""
  _make_chat(
    db, "had-assistant",
    run_status="running",
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      {"role": "assistant",
       "blocks": [{"type": "text", "content": "partial"}], "ts": 2},
    ],
  )
  _make_chat(
    db, "no-assistant",
    run_status="running",
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
  a chat run_status=None with a non-empty pending queue. Reconciliation must
  NOT consume it — auto-promoting at startup would spawn a post-crash turn, and
  the next POST's stale-pending drain is the repair path — but it WARNS so a
  never-drained accumulating queue is visible rather than silent.
  """
  _make_chat(
    db, "markerless",
    run_status=None,
    pending_messages=[{"role": "user", "content": "queued", "ts": 1}],
  )

  with caplog.at_level("WARNING"):
    reconciled = chat_mod.reconcile_interrupted_chats(db)

  assert "markerless" not in reconciled, "a markerless queue must not be consumed"
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == "markerless").first()
  assert row.run_status is None
  assert len(row.pending_messages) == 1, (
    "the queue is left intact for the next-POST stale-pending drain"
  )
  assert any(
    "markerless pending queue" in r.getMessage() for r in caplog.records
  ), "an accumulating markerless queue must be surfaced as a warning"
