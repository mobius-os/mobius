"""Saved handoffs shorten oversized history without losing its uncovered tail."""
from sqlalchemy.orm import object_session
from app import transcript_rows
import asyncio
import copy
from datetime import UTC, datetime

import pytest

from app import models, chat as chat_mod, compaction
from app.chat_continuity import (
  apply_checkpoint, checkpoint_coverage, note_path, recovery_source, write_note,
)
from app.chat_writer import (
  BeginNoteRecovery, ParkRun, StartTurn, FinishRun, get_writer,
)
from app.compaction import PreparedNoteRecovery, NoteRecoverySource
from app.config import get_settings
from app.chat_event_sink import ChatEventSink
from app.runner_registry import registry


HISTORY = [
  {"role": "user", "content": "Preserve the old files.", "ts": 1, "cid": "u1"},
  {"role": "assistant", "content": "Agreed; draft only.", "id": "old", "ts": 2},
]


def _bound(messages, run="run"):
  return apply_checkpoint(None, name="test", summary="Preserve old files; draft only.",
                          coverage=checkpoint_coverage(messages, run))


def _start(chat, db, *, run="run"):
  transcript_rows.replace_all(object_session(chat), chat, copy.deepcopy(HISTORY))
  chat.session_id = "old-session"
  db.commit()
  get_writer().submit(StartTurn(chat_id=chat.id, run_token=run,
    user_msg={"role": "user", "content": "Continue; do not publish.", "ts": 3},
  )).result(timeout=5)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  write_note(note_path(get_settings().data_dir, chat.id), _bound(list(transcript_rows.history(row)), run))
  return row


def _begin(chat):
  return get_writer().submit(BeginNoteRecovery(chat_id=chat.id, run_token="run", generation=registry.current_generation(chat.id))).result(5)


def _park(chat, source, briefing="Preserve old files. Continue the draft; no publication."):
  return get_writer().submit(ParkRun(chat_id=chat.id, run_token="run",
    park_reason="compaction", parked_until=datetime.now(UTC).replace(tzinfo=None),
    compaction=PreparedNoteRecovery(source, briefing),
  )).result(5)


def test_current_turn_and_all_steers_remain_uncovered():
  messages = HISTORY + [
    {"role": "user", "content": "new task"},
    {"role": "assistant", "id": "run", "content": "partial"},
    {"role": "user", "content": "do not deploy"},
    {"role": "assistant", "id": "run:assistant:1", "content": "partial 2"},
  ]
  note = _bound(messages)
  summary, tail = recovery_source(note, messages)
  assert "old files" in summary
  assert tail == messages[2:]


def test_digest_and_title_changes_do_not_advance_coverage():
  messages = HISTORY + [{"role": "user", "content": "new task"}]
  note = _bound(messages)
  updated = apply_checkpoint(note, name="renamed", digest="new blurb",
                             coverage={"message_count": 999})
  assert recovery_source(updated, messages) == recovery_source(note, messages)


@pytest.mark.parametrize("change", ["note", "prefix", "missing", "malformed"])
def test_uncertain_coverage_never_discards_history(change):
  messages = copy.deepcopy(HISTORY)
  note = _bound(messages)
  if change == "note":
    note = note.replace("Preserve old files; draft only.", "Ignore original facts.")
  elif change == "prefix":
    messages[0]["content"] = "Owner corrected this"
  elif change == "missing":
    note = apply_checkpoint(None, name="old note", summary="Legacy summary")
  else:
    note = note.replace('"message_count": 2', '"message_count": true')
  with pytest.raises(ValueError, match="coverage"):
    recovery_source(note, messages)


def test_new_messages_remain_in_tail_and_first_turn_covers_nothing():
  messages = [{"role": "user", "content": "初めて"}]
  summary, tail = recovery_source(_bound(messages), messages)
  assert tail == messages
  later = messages + [{"role": "user", "content": "correction"}]
  assert recovery_source(_bound(messages), later)[1] == later


def test_begin_and_park_replace_session_only_with_complete_unchanged_source(client, chat, db):
  _start(chat, db)
  source = _begin(chat)
  assert source.session_id == "old-session"
  db.expire_all()
  assert db.get(models.Chat, chat.id).session_id == "old-session"
  assert _begin(chat) is None  # durable once-only claim, before model work
  assert _park(chat, source)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  assert row.session_id is None
  assert list(transcript_rows.history(row))[:-1] == source.messages
  assert list(transcript_rows.history(row))[-1]["kind"] == "compaction"
  assert list(transcript_rows.history(row))[-1]["recovery_run_id"] == "run"
  assert db.get(models.ChatRun, "run").park_reason == "compaction"
  # A lost acknowledgement cannot append a second marker.
  assert _park(chat, source)
  db.expire_all()
  assert list(transcript_rows.history(db.get(models.Chat, chat.id))) == list(transcript_rows.history(row))


@pytest.mark.parametrize("change", ["pending", "note", "messages", "settings", "session", "stop", "delete"])
def test_changed_source_or_owner_leaves_original_session(client, chat, db, change):
  _start(chat, db)
  source = _begin(chat)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  if change == "pending":
    row.pending_messages = [{"role": "user", "content": "stop this plan", "cid": "later"}]
  elif change == "note":
    path = note_path(get_settings().data_dir, chat.id)
    write_note(path, path.read_text() + "\nchanged\n")
  elif change == "messages":
    transcript_rows.replace_all(object_session(row), row, [*list(transcript_rows.history(row)), {"role": "user", "content": "new correction"}])
  elif change == "settings":
    row.agent_settings_json = {"effort": "low"}
  elif change == "session":
    row.session_id = "new-session"
  elif change == "stop":
    db.get(models.ChatRun, "run").status = "stopped"
  else:
    row.deleted_at = datetime.now(UTC)
  db.commit()
  before = copy.deepcopy(list(transcript_rows.history(row)))
  old_session = row.session_id
  assert not _park(chat, source)
  db.expire_all()
  assert row.session_id == old_session
  assert list(transcript_rows.history(row)) == before


def test_failed_synthesis_budget_is_not_renewed_by_same_root_resume(client, chat, db):
  _start(chat, db)
  assert _begin(chat) is not None
  get_writer().submit(FinishRun(chat_id=chat.id, run_token="run", terminal_status="failed")).result(5)
  # Re-use the established ordinary StartTurn fixture then bind a physical
  # recovery to the same logical root, as the continuation writer does.
  get_writer().submit(StartTurn(chat_id=chat.id, run_token="next",
    user_msg={"role": "user", "content": "continue", "ts": 5},
  )).result(5)
  db.expire_all()
  db.get(models.ChatRun, "next").root_run_id = "run"
  db.commit()
  assert get_writer().submit(BeginNoteRecovery(chat_id=chat.id, run_token="next", generation=registry.current_generation(chat.id))).result(5) is None


@pytest.mark.asyncio
async def test_synthesizer_receives_full_note_and_only_uncovered_tail(monkeypatch):
  messages = HISTORY + [{"role": "user", "content": "uncovered correction"}]
  seen = []
  async def fake(messages, **kwargs):
    seen.append((messages, kwargs))
    return "safe briefing"
  monkeypatch.setattr(compaction, "summarize_chat", fake)
  source = NoteRecoverySource(messages, _bound(messages), "codex", "old", {})
  assert await source.summarize(data_dir="unused") == "safe briefing"
  assert seen[0][0] == messages[2:]
  assert "old files" in seen[0][1]["source_summary"]


class Broadcast:
  def __init__(self, chat_id):
    self.chat_id, self.events = chat_id, []
  def publish(self, value):
    self.events.append(value)
  def mark_completed(self):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("size_signal", [
  {"api_error_status": 413}, {"context_window_exceeded": True},
])
async def test_terminal_recovery_finalizes_before_snapshot_and_preserves_on_failure(
    client, chat, db, monkeypatch, failure, size_signal):
  _start(chat, db)
  registry.mark_starting(chat.id)
  gen = registry.current_generation(chat.id)
  bc = Broadcast(chat.id)
  sink = ChatEventSink(bc, chat.id, run_token="run")
  sink.publish({"type": "text", "content": "Completed work before failure."})
  kwargs = chat_mod._park_exit(sink, size_signal, "Provider refused input.")
  async def fake(messages, **kwargs):
    assert any("Completed work before failure." in row.get("content", "") for row in messages)
    if failure:
      raise compaction.CompactionError("fake rejection")
    return "Existing work completed; preserve files; draft only."
  monkeypatch.setattr(compaction, "summarize_chat", fake)
  result = await chat_mod._complete_turn(bc=bc, sink=sink, db=db,
    chat_id=chat.id, run_gen=gen, provider_id="codex", cost_usd=0,
    close_browser=False, **kwargs)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  assert list(transcript_rows.history(row))[:2] == HISTORY
  run = db.get(models.ChatRun, "run")
  if failure:
    assert row.session_id == "old-session" and run.status == "failed"
    assert not any(m.get("kind") == "compaction" for m in list(transcript_rows.history(row)))
  else:
    assert row.session_id is None and run.status == "parked"
    assert list(transcript_rows.history(row))[-1]["kind"] == "compaction"
  assert run.note_recovery_attempted is True
  assert run.continuation_json is None  # recovery accounting is not owner-input provenance


@pytest.mark.asyncio
async def test_stop_cancels_private_synthesis():
  cancelled = asyncio.Event()
  async def wait():
    try:
      await asyncio.Event().wait()
    finally:
      cancelled.set()
  task = asyncio.create_task(wait())
  await asyncio.sleep(0)
  handle = compaction.RecoverySynthesisHandle("chat", task)
  assert await handle.stop()
  assert cancelled.is_set() and task.cancelled()


def test_stop_generation_wins_before_writer_receives_recovery_park(client, chat, db):
  _start(chat, db)
  source = _begin(chat)
  registry.bump_generation(chat.id)
  assert not _park(chat, source)
  db.expire_all()
  assert db.get(models.Chat, chat.id).session_id == "old-session"


@pytest.mark.asyncio
async def test_stop_during_synthesis_keeps_old_session_and_never_parks(client, chat, db, monkeypatch):
  _start(chat, db)
  registry.mark_starting(chat.id)
  gen = registry.current_generation(chat.id)
  entered = asyncio.Event()
  async def fake(*args, **kwargs):
    entered.set()
    await asyncio.Event().wait()
  monkeypatch.setattr(compaction, "summarize_chat", fake)
  bc = Broadcast(chat.id)
  sink = ChatEventSink(bc, chat.id, run_token="run")
  sink.publish({"type": "error", "message": "request body too large"})
  task = asyncio.create_task(chat_mod._complete_turn(
    bc=bc, sink=sink, db=db, chat_id=chat.id, run_gen=gen,
    provider_id="codex", cost_usd=0, close_browser=False, oversized=True))
  await entered.wait()
  from app.runner_registry import RunnerKind
  handle = registry.get_handle(chat.id, RunnerKind.COMPACTION)
  assert handle is not None
  registry.bump_generation(chat.id)
  await handle.stop()
  assert await task == chat_mod.chat_queue.TerminalDisposition.STALE_NO_ACTION
  db.expire_all()
  assert db.get(models.Chat, chat.id).session_id == "old-session"
  assert db.get(models.ChatRun, "run").park_reason is None


@pytest.mark.asyncio
async def test_existing_sweep_starts_exact_recovered_context_once(client, chat, db, monkeypatch):
  _start(chat, db)
  source = _begin(chat)
  assert _park(chat, source)
  db.expire_all()
  # Mark due through the existing controller, without invoking any provider.
  from app.chat_writer import PrepareAutoResume
  get_writer().submit(PrepareAutoResume(chat_id=chat.id, run_token="run")).result(5)
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert await chat_mod._auto_resume_chat(chat.id, "run")
  assert len(scheduled) == 1
  assert scheduled[0]["session_id"] is None
  assert scheduled[0]["next_user"]["continuation_reason"] == "compaction"
  db.expire_all()
  successor = db.get(models.ChatRun, scheduled[0]["run_token"])
  assert successor.root_run_id == "run"
  assert list(transcript_rows.history(db.get(models.Chat, chat.id)))[:-1] == source.messages
  # Claim in the successor cannot renew the synthesis budget.
  assert get_writer().submit(BeginNoteRecovery(chat_id=chat.id, run_token=successor.id,
    generation=registry.current_generation(chat.id))).result(5) is None


@pytest.mark.asyncio
async def test_pending_owner_message_prevents_compaction_retry(client, chat, db, monkeypatch):
  _start(chat, db)
  assert _park(chat, _begin(chat))
  from app.chat_writer import PrepareAutoResume
  get_writer().submit(PrepareAutoResume(chat_id=chat.id, run_token="run")).result(5)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  pending = [{"role": "user", "content": "changed my mind", "cid": "new-owner"}]
  row.pending_messages = pending
  db.commit()
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert not await chat_mod._auto_resume_chat(chat.id, "run")
  assert scheduled == []
  db.expire_all()
  assert row.pending_messages == pending


def test_recovery_attempt_upgrade_is_additive_idempotent_and_preserves_history(tmp_path):
  from sqlalchemy import create_engine, text
  from app.schema_migrations import _add_note_recovery_attempted
  engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
  with engine.begin() as conn:
    conn.execute(text("CREATE TABLE chat_runs (id TEXT PRIMARY KEY, continuation_json JSON)"))
    conn.execute(text("INSERT INTO chat_runs VALUES ('old', NULL)"))
  _add_note_recovery_attempted(engine)
  with engine.begin() as conn:
    assert conn.execute(text("SELECT note_recovery_attempted FROM chat_runs")).scalar() == 0
    conn.execute(text("UPDATE chat_runs SET note_recovery_attempted=1"))
  _add_note_recovery_attempted(engine)
  with engine.connect() as conn:
    assert conn.execute(text("SELECT id, continuation_json, note_recovery_attempted FROM chat_runs")).one() == ("old", None, 1)


@pytest.mark.asyncio
async def test_large_covered_history_uses_small_existing_synthesis_without_raising_caps(monkeypatch):
  from types import SimpleNamespace
  from app import providers
  async def ensure_auth(*args):
    pass
  monkeypatch.setattr(providers, "get_provider", lambda _: SimpleNamespace(
    check_auth=lambda _: None, ensure_auth=ensure_auth))
  seen = []
  async def fake(prompt, **kwargs):
    seen.append(prompt)
    return "Preserve original files and do not publish."
  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", fake)
  history = copy.deepcopy(HISTORY)
  history[1]["content"] = "old detail " * 100_000
  messages = history + [{"role": "user", "content": "Do not publish."}]
  source = NoteRecoverySource(messages, _bound(messages), "codex", "old", {})
  await source.summarize(data_dir="unused")
  assert len(seen) == 1
  assert len(seen[0].encode()) < 5000
  assert "old detail " not in seen[0]
  assert "Do not publish." in seen[0]
  # Unsaved new work still has the original total-work guard, not truncation.
  messages.append({"role": "user", "content": "new " * 200_000})
  with pytest.raises(compaction.CompactionError, match="too large"):
    await source.summarize(data_dir="unused")
  assert len(seen) == 1


def test_missing_note_and_app_work_never_claim_automatic_recovery(client, chat, db):
  _start(chat, db)
  path = note_path(get_settings().data_dir, chat.id)
  path.unlink()
  assert _begin(chat) is None
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  write_note(path, _bound(list(transcript_rows.history(row))))
  # A hidden app-owned job must not gain reseed permission from this feature.
  run = db.get(models.ChatRun, "run")
  app = models.App(name="job", slug="job", source_dir="/unused/job")
  db.add(app)
  db.flush()
  run.initiated_by_app_id = app.id
  db.commit()
  assert _begin(chat) is None
  db.expire_all()
  assert run.note_recovery_attempted is False


def test_failed_park_commit_keeps_session_and_transcript(client, chat, db, monkeypatch):
  from app import chat_writer
  _start(chat, db)
  source = _begin(chat)
  def reject(db):
    db.rollback()
    return False
  monkeypatch.setattr(chat_writer, "_commit_or_rollback", reject)
  with pytest.raises(Exception, match="ParkRun did not persist"):
    _park(chat, source)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  assert row.session_id == "old-session"
  assert list(transcript_rows.history(row)) == source.messages
  assert db.get(models.ChatRun, "run").status == "running"


@pytest.mark.asyncio
async def test_committed_recovery_can_be_reconstructed_but_never_admitted_past_new_owner_input(
    client, chat, db, monkeypatch):
  from app.chat_writer import PrepareAutoResume, AdmitProviderExecution
  _start(chat, db)
  assert _park(chat, _begin(chat))
  get_writer().submit(PrepareAutoResume(chat_id=chat.id, run_token="run")).result(5)
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert await chat_mod._auto_resume_chat(chat.id, "run")
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  successor = db.get(models.ChatRun, scheduled[0]["run_token"])
  recovered = chat_mod._auto_resume_recovery(db, row, successor, park_token="run")
  assert recovered is not None
  assert recovered[1]["promoted"]["continuation_reason"] == "compaction"
  # Simulate owner input arriving after durable admission but before provider
  # entry, including the post-commit/pre-task crash reconstruction boundary.
  row.pending_messages = [{"role": "user", "content": "Wait", "cid": "late"}]
  db.commit()
  assert chat_mod._auto_resume_recovery(db, row, successor, park_token="run") is None
  with pytest.raises(Exception, match="owner input superseded"):
    get_writer().submit(AdmitProviderExecution(
      chat_id=chat.id, run_token=successor.id)).result(5)
  db.expire_all()
  assert successor.provider_execution_admitted is False
  assert row.pending_messages[0]["cid"] == "late"


def test_invalid_briefing_cannot_retire_the_old_session(client, chat, db):
  _start(chat, db)
  source = _begin(chat)
  with pytest.raises(compaction.CompactionError):
    _park(chat, source, briefing="   ")
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  assert row.session_id == "old-session"
  assert list(transcript_rows.history(row)) == source.messages


def test_checkpoint_route_binds_detailed_note_without_renaming_or_changing_save_semantics(
    client, chat, db):
  from app import auth
  row = _start(chat, db)
  token = auth.create_agent_token(chat.id, "test", 0, run_id="run")
  headers = {"Authorization": f"Bearer {token}"}
  response = client.post("/api/chat/continuity/checkpoints", headers=headers,
                         json={"summary": "Owner also forbids publication.", "digest": "Drafting."})
  assert response.status_code == 204
  path = note_path(get_settings().data_dir, chat.id)
  summary, tail = recovery_source(path.read_text(), list(transcript_rows.history(row)))
  assert "Preserve old files" in summary and "forbids publication" in summary
  assert tail == list(transcript_rows.history(row))[2:]
  response = client.post("/api/chat/continuity/checkpoints", headers=headers,
                         json={"digest": "Shorter."})
  assert response.status_code == 204
  assert recovery_source(path.read_text(), list(transcript_rows.history(row))) == (summary, tail)


@pytest.mark.parametrize("when", ["before_synthesis", "before_commit"])
def test_closed_goal_never_reseeds_the_old_session(client, chat, db, when):
  _start(chat, db)
  goal = models.ChatGoal(id="goal", chat_id=chat.id, objective="Preserve files", status="open")
  db.add(goal)
  db.flush()
  db.get(models.ChatRun, "run").goal_id = goal.id
  db.commit()
  source = _begin(chat) if when == "before_commit" else None
  db.expire_all()
  goal.status = "completed"
  db.commit()
  if source is None:
    assert _begin(chat) is None
  else:
    assert not _park(chat, source)
  db.expire_all()
  assert db.get(models.Chat, chat.id).session_id == "old-session"
  assert not any(m.get("kind") == "compaction" for m in list(transcript_rows.history(db.get(models.Chat, chat.id))))
