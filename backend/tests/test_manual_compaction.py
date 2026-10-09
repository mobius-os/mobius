"""Explicit recovery batches preserve source, completed work and owner control."""

import asyncio
import copy
import httpx
import threading

import pytest
from sqlalchemy import create_engine, inspect, text

from app import compaction, models, transcript_rows
from app.chat_context import _latest_compaction_brief
from app.chat_continuity import note_path
from app.chat_writer import get_writer, ReplaceTranscript
from app.config import get_settings
from app.manual_compaction import evidence_reference
from app.runner_registry import registry, RunnerKind


class FakeProvider:
  def check_auth(self, _data_dir):
    return None

  async def ensure_auth(self, _data_dir):
    return None


@pytest.fixture
def fake_synthesis(monkeypatch):
  calls = []
  monkeypatch.setattr("app.providers.get_provider", lambda _provider: FakeProvider())

  async def turn(prompt, **kwargs):
    calls.append((prompt, kwargs))
    return f"Verified fixture briefing {len(calls)}"

  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", turn)
  return calls


def make_chat(client, auth, db, *, large=False, messages=None):
  chat_id = client.post("/api/chats", headers=auth, json={"title": "Batch fixture"}).json()["id"]
  source = messages if messages is not None else [
    {"role": "user", "content": "Preserve originals." + ("x" * 810000 if large else "")},
    {"role": "assistant", "content": "Original answer."},
  ]
  assert client.put(f"/api/chats/{chat_id}", headers=auth, json={"messages": source}).status_code == 200
  chat = db.get(models.Chat, chat_id)
  chat.session_id = "original-session"
  chat.agent_settings_json = {"model": "claude-sonnet-4-6"}
  db.commit()
  return chat_id, source


def batch(client, auth, chat_id, batch_id="first", recovery_id=None):
  body = {"batch_id": batch_id}
  if recovery_id:
    body["recovery_id"] = recovery_id
  return client.post(f"/api/chats/{chat_id}/compact", headers=auth, json=body)


def test_large_manual_recovery_requires_explicit_next_batch(client, auth, db, fake_synthesis):
  chat_id, original = make_chat(client, auth, db, large=True)
  first = batch(client, auth, chat_id).json()
  assert first["ok"] is False
  assert first["progress"]["next_chunk"] == 8
  assert first["progress"]["state"] == "paused"
  assert len(fake_synthesis) == 8
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  assert chat.session_id == "original-session"
  assert list(transcript_rows.history(chat)) == original
  # Reads, reopening and duplicate request identities cannot start work.
  for _ in range(2):
    assert client.get(f"/api/chats/{chat_id}/compact-progress", headers=auth).json()["progress"]["next_chunk"] == 8
    assert batch(client, auth, chat_id).json()["ok"] is False
  assert len(fake_synthesis) == 8
  final = batch(client, auth, chat_id, "second", first["progress"]["recovery_id"]).json()
  assert final["ok"] and final["progress"] is None
  assert len(fake_synthesis) == 11
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  assert chat.session_id is None
  saved = list(transcript_rows.history(chat))
  assert saved[:-1] == original
  assert saved[-1]["source_evidence"]["message_count"] == len(original)
  assert batch(client, auth, chat_id, "second", "first").json()["ok"]
  assert len(fake_synthesis) == 11


@pytest.mark.parametrize("change", ["note", "messages", "settings", "session", "pending", "question", "global"])
def test_changed_source_cannot_continue_or_replace_session(client, auth, db, fake_synthesis, change):
  chat_id, original = make_chat(client, auth, db, large=True)
  first = batch(client, auth, chat_id).json()
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  if change == "note":
    path = note_path(get_settings().data_dir, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Digest\nOwner correction", encoding="utf-8")
  elif change == "messages":
    get_writer().submit(ReplaceTranscript(chat_id=chat_id, messages=original + [{"role": "user", "content": "New choice"}])).result(5)
  elif change == "global":
    from pathlib import Path
    path = Path(get_settings().data_dir) / "shared" / "agent-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"effort":"low"}', encoding="utf-8")
  else:
    if change == "settings":
      chat.agent_settings_json = {"model": "claude-sonnet-4-6", "effort": "low"}
    elif change == "session":
      chat.session_id = "newer-session"
    elif change == "pending":
      chat.pending_messages = [{"role": "user", "content": "Queued follow-up"}]
    else:
      chat.pending_question_id = "unanswered"
    db.commit()
  response = batch(client, auth, chat_id, "second", first["progress"]["recovery_id"])
  assert response.status_code == 409
  assert len(fake_synthesis) == 8
  assert client.get(f"/api/chats/{chat_id}/compact-progress", headers=auth).json()["progress"]["state"] == "stale"
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  assert chat.session_id == ("newer-session" if change == "session" else "original-session")
  if change == "global":
    path.unlink()


def test_missing_or_unbound_notes_do_not_replace_original_history(client, auth, db, fake_synthesis):
  chat_id, original = make_chat(client, auth, db)
  path = note_path(get_settings().data_dir, chat_id)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text("## Digest\nUnbound old note", encoding="utf-8")
  assert batch(client, auth, chat_id).json()["ok"]
  assert "Preserve originals" in fake_synthesis[0][0]
  assert "Original answer" in fake_synthesis[0][0]
  assert "Unbound old note" in fake_synthesis[0][0]


def test_verified_full_digest_replaces_only_its_covered_prefix(client, auth, db, fake_synthesis):
  from app.chat_continuity import apply_checkpoint
  from app.chat_writer import messages_fingerprint

  source = [
    {"role": "user", "content": "COVERED ORIGINAL" * 60000},
    {"role": "assistant", "content": "Covered response"},
    {"role": "user", "content": "Fresh uncovered decision"},
  ]
  chat_id, _ = make_chat(client, auth, db, messages=source)
  path = note_path(get_settings().data_dir, chat_id)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(apply_checkpoint(
    None, name="Fixture", summary="SHORT SUMMARY IS NOT SOURCE",
    digest="Verified detailed handoff", coverage={
      "message_count": 2, "messages_sha256": messages_fingerprint(source[:2]),
    },
  ), encoding="utf-8")

  assert batch(client, auth, chat_id).json()["ok"]
  assert len(fake_synthesis) == 1
  prompt = fake_synthesis[0][0]
  assert "Verified detailed handoff" in prompt
  assert "Fresh uncovered decision" in prompt
  assert "COVERED ORIGINAL" not in prompt
  assert "SHORT SUMMARY IS NOT SOURCE" not in prompt
  db.expire_all()
  assert list(transcript_rows.history(db.get(models.Chat, chat_id)))[:-1] == source


def test_provider_failure_keeps_checkpoint_and_does_not_repeat_completed_call(client, auth, db, monkeypatch, fake_synthesis):
  chat_id, _ = make_chat(client, auth, db, large=True)
  calls = []

  async def turn(prompt, **_kwargs):
    calls.append(prompt)
    if len(calls) == 2:
      raise RuntimeError("fake provider failure")
    return "Saved first section"

  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", turn)
  first = batch(client, auth, chat_id).json()
  assert first["progress"]["next_chunk"] == 1
  assert first["progress"]["error"]
  assert batch(client, auth, chat_id).json()["progress"]["next_chunk"] == 1
  assert len(calls) == 2
  second = batch(client, auth, chat_id, "second", "first").json()
  assert second["progress"]["next_chunk"] == 9
  assert len(calls) == 10
  assert "CURRENT PORTABLE BRIEFING" in calls[2]


def test_final_checkpoint_can_be_committed_without_repeating_synthesis(client, auth, db, monkeypatch, fake_synthesis):
  chat_id, _ = make_chat(client, auth, db)
  writer = get_writer()
  original = writer._persist_compaction
  failed = False

  def fail_once(db_, cmd):
    nonlocal failed
    if cmd.recovery_id and not failed:
      failed = True
      return {"status": "conflict"}
    return original(db_, cmd)

  monkeypatch.setattr(writer, "_persist_compaction", fail_once)
  first = batch(client, auth, chat_id).json()
  assert first["ok"] is False and first["progress"]["next_chunk"] == 1
  assert batch(client, auth, chat_id, "second", "first").json()["ok"]
  assert len(fake_synthesis) == 1


@pytest.mark.asyncio
async def test_stop_cancels_only_preparation_and_preserves_progress(client, auth, db, monkeypatch, fake_synthesis):
  from app.main import app
  chat_id, original = make_chat(client, auth, db, large=True)
  entered = asyncio.Event()
  calls = []

  async def turn(prompt, **_kwargs):
    calls.append(prompt)
    if len(calls) == 2:
      entered.set()
      await asyncio.Event().wait()
    return "First completed section"

  monkeypatch.setattr(compaction, "_run_provider_summarize_turn", turn)
  # Concurrent ASGI requests share the server event loop, as in production.
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as cli:
    worker = asyncio.create_task(cli.post(f"/api/chats/{chat_id}/compact", headers=auth, json={"batch_id": "first"}))
    try:
      await asyncio.wait_for(entered.wait(), 5)
      stopped = await cli.post(f"/api/chats/{chat_id}/compact-stop", headers=auth)
      assert stopped.status_code == 200, stopped.text
      result = (await asyncio.wait_for(worker, 5)).json()
    finally:
      if not worker.done():
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
  assert result["progress"]["next_chunk"] == 1
  assert "stopped" in result["progress"]["error"]
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  assert chat.session_id == "original-session"
  assert list(transcript_rows.history(chat)) == original
  assert not registry.is_alive(chat_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["pause", "restart"])
async def test_queued_checkpoint_after_pause_or_restart_keeps_saved_sections_resumable(
    client, auth, db, monkeypatch, fake_synthesis, interrupt):
  from app import chat as chat_mod, manual_compaction
  from app.chat_writer import AdvanceManualCompaction
  from app.main import app

  chat_id, original = make_chat(client, auth, db, large=True)
  loop = asyncio.get_running_loop()
  queued = asyncio.Event()
  release = threading.Event()
  persist = manual_compaction.persist_manual_progress
  bump = registry.bump_generation

  def held_checkpoint(session, cmd):
    if isinstance(cmd, AdvanceManualCompaction) and cmd.next_chunk == 2:
      loop.call_soon_threadsafe(queued.set)
      assert release.wait(5), "checkpoint was not released"
    return persist(session, cmd)

  def fenced(target):
    generation = bump(target)
    if target == chat_id:
      release.set()
    return generation

  monkeypatch.setattr(manual_compaction, "persist_manual_progress", held_checkpoint)
  monkeypatch.setattr(registry, "bump_generation", fenced)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as cli:
    worker = asyncio.create_task(cli.post(
      f"/api/chats/{chat_id}/compact", headers=auth, json={"batch_id": "first"}))
    try:
      await asyncio.wait_for(queued.wait(), 5)
      if interrupt == "pause":
        stopped = await cli.post(f"/api/chats/{chat_id}/compact-stop", headers=auth)
        assert stopped.status_code == 200, stopped.text
      else:
        # Exercise the real drain, but never restart a process.
        await chat_mod.drain_all_for_restart(timeout=2, prepared_runs=[])
      response = await asyncio.wait_for(worker, 5)
      assert response.status_code == 200, response.text
      result = response.json()
    finally:
      release.set()
      if not worker.done():
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

  assert result["ok"] is False
  assert result["progress"]["state"] == "paused"
  assert result["progress"]["next_chunk"] == 1
  assert len(fake_synthesis) == 2
  db.expire_all()
  row = db.get(models.Chat, chat_id)
  assert row.session_id == "original-session"
  assert list(transcript_rows.history(row)) == original
  assert client.get(f"/api/chats/{chat_id}/compact-progress", headers=auth).json()["progress"]["state"] == "paused"
  assert len(fake_synthesis) == 2  # Observation never starts another call.
  if interrupt == "restart":
    chat_mod.draining = False
    registry.reset_for_tests()  # Model a fresh runtime; the draft is durable.
  continued = batch(client, auth, chat_id, "continued", "first").json()
  assert continued["progress"]["next_chunk"] == 9
  assert len(fake_synthesis) == 10  # Section 1 was not summarized again.


@pytest.mark.parametrize("status", ["paused", "complete"])
def test_late_checkpoint_cannot_invalidate_an_ended_batch(
    client, auth, db, fake_synthesis, status):
  from app.chat_writer import AdvanceManualCompaction

  chat_id, _ = make_chat(client, auth, db, large=True)
  batch(client, auth, chat_id)
  db.expire_all()
  draft = db.get(models.ChatCompactionDraft, "first")
  draft.state = {**draft.state, "status": status}
  db.commit()
  saved = copy.deepcopy(draft.state)
  receipt = get_writer().submit(AdvanceManualCompaction(
    chat_id=chat_id, recovery_id="first", batch_id="first",
    generation=registry.current_generation(chat_id),
    expected_chunk=8, next_chunk=9, briefing="Late result",
  )).result(5)
  assert receipt["status"] == "conflict"
  db.expire_all()
  assert db.get(models.ChatCompactionDraft, "first").state == saved


def test_changed_note_still_invalidates_a_current_checkpoint(client, auth, db, fake_synthesis):
  from app.chat_writer import BeginManualCompaction, AdvanceManualCompaction

  chat_id, _ = make_chat(client, auth, db, large=True)
  batch(client, auth, chat_id)
  generation = registry.current_generation(chat_id)
  admitted = get_writer().submit(BeginManualCompaction(
    chat_id=chat_id, recovery_id="first", batch_id="continued",
    generation=generation, continuing=True,
  )).result(5)
  assert admitted["status"] == "admitted"
  path = note_path(get_settings().data_dir, chat_id)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text("## Digest\nA new owner correction", encoding="utf-8")
  receipt = get_writer().submit(AdvanceManualCompaction(
    chat_id=chat_id, recovery_id="first", batch_id="continued",
    generation=generation, expected_chunk=8, next_chunk=9, briefing="Old-source result",
  )).result(5)
  assert receipt["status"] == "conflict"
  db.expire_all()
  saved = db.get(models.ChatCompactionDraft, "first").state
  assert saved["status"] == "stale"
  assert saved["next_chunk"] == 8
  assert len(fake_synthesis) == 8


def test_evidence_pointers_preserve_tool_and_image_sources_without_interpreting_them(client, auth, db, fake_synthesis):
  messages = [{"role": "user", "content": "Use the saved evidence", "attachments": [{"url": "/api/media/fixture.png"}]},
              {"role": "assistant", "content": "", "blocks": [{"type": "tool", "tool_use_id": "fixture-tool", "output": "EXACT ORIGINAL EVIDENCE"}]}]
  chat_id, _ = make_chat(client, auth, db, messages=messages)
  assert batch(client, auth, chat_id).json()["ok"]
  assert "EXACT ORIGINAL EVIDENCE" not in fake_synthesis[0][0]
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  brief = _latest_compaction_brief(chat)
  assert f"/api/chats/{chat_id}?limit=100&before=2" in brief
  assert "not interpreted" in brief
  assert list(transcript_rows.history(chat))[:-1] == messages


def test_cross_chat_recovery_identity_cannot_read_or_replace_other_work(client, auth, db, fake_synthesis):
  first, _ = make_chat(client, auth, db, large=True)
  second, _ = make_chat(client, auth, db)
  assert batch(client, auth, first).status_code == 200
  assert batch(client, auth, second, "other", "first").status_code == 409
  assert len(fake_synthesis) == 8


def test_observing_interrupted_progress_never_starts_provider_work(client, auth, db, fake_synthesis):
  chat_id, _ = make_chat(client, auth, db, large=True)
  batch(client, auth, chat_id)
  db.expire_all()
  draft = db.get(models.ChatCompactionDraft, "first")
  draft.state = {**draft.state, "status": "running"}
  db.commit()
  assert client.get(f"/api/chats/{chat_id}/compact-progress", headers=auth).json()["progress"]["state"] == "interrupted"
  assert len(fake_synthesis) == 8


def test_compaction_draft_migration_is_additive_idempotent_and_indexed(tmp_path):
  from app.schema_migrations import _add_chat_compaction_drafts
  eng = create_engine(f"sqlite:///{tmp_path / 'drafts.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE chats(id VARCHAR(64) PRIMARY KEY, session_id TEXT)"))
    conn.execute(text("INSERT INTO chats VALUES ('chat', 'old-session')"))
  _add_chat_compaction_drafts(eng)
  _add_chat_compaction_drafts(eng)
  assert inspect(eng).get_foreign_keys("chat_compaction_drafts")[0]["referred_table"] == "chats"
  assert inspect(eng).get_indexes("chat_compaction_drafts")[0]["name"] == "ix_chat_compaction_drafts_chat_id"
  with eng.connect() as conn:
    assert conn.execute(text("SELECT session_id FROM chats")).scalar() == "old-session"


def test_saved_batch_survives_runtime_loss_but_never_runs_on_observation(client, auth, db, fake_synthesis):
  chat_id, original = make_chat(client, auth, db, large=True)
  first = batch(client, auth, chat_id).json()
  registry.reset_for_tests()
  assert client.get(f"/api/chats/{chat_id}/compact-progress", headers=auth).json()["progress"]["next_chunk"] == 8
  assert len(fake_synthesis) == 8
  assert batch(client, auth, chat_id, "after-restart", first["progress"]["recovery_id"]).json()["ok"]
  assert len(fake_synthesis) == 11
  db.expire_all()
  assert list(transcript_rows.history(db.get(models.Chat, chat_id)))[:-1] == original


@pytest.mark.parametrize("change", ["settings", "pending", "generation"])
def test_freshness_is_checked_again_at_atomic_finalization(client, auth, db, monkeypatch, fake_synthesis, change):
  chat_id, original = make_chat(client, auth, db)
  writer = get_writer()
  persist = writer._persist_compaction

  def raced(session, cmd):
    if cmd.recovery_id:
      row = session.get(models.Chat, chat_id)
      if change == "settings":
        row.agent_settings_json = {"model": "claude-sonnet-4-6", "effort": "low"}
        session.commit()
      elif change == "pending":
        row.pending_messages = [{"role": "user", "content": "Preserve queued B", "attachments": [{"url": "/api/media/queued.png"}]}]
        session.commit()
      else:
        registry.bump_generation(chat_id)
    return persist(session, cmd)

  monkeypatch.setattr(writer, "_persist_compaction", raced)
  result = batch(client, auth, chat_id).json()
  assert not result["ok"]
  assert len(fake_synthesis) == 1
  db.expire_all()
  row = db.get(models.Chat, chat_id)
  assert row.session_id == "original-session"
  assert list(transcript_rows.history(row)) == original
  if change == "pending":
    assert row.pending_messages[0]["content"] == "Preserve queued B"
    assert row.pending_messages[0]["attachments"] == [{"url": "/api/media/queued.png"}]


def test_empty_source_rejected_without_provider_work_or_draft(client, auth, db, fake_synthesis):
  chat_id, _ = make_chat(client, auth, db, messages=[])
  assert batch(client, auth, chat_id).status_code == 422
  assert fake_synthesis == []
  assert db.query(models.ChatCompactionDraft).filter_by(chat_id=chat_id).count() == 0
  assert not registry.is_alive(chat_id)


def test_tool_only_source_uses_retrievable_reference_not_invented_contents(client, auth, db, fake_synthesis):
  source = [{"role": "assistant", "content": "", "blocks": [{"type": "tool", "tool_use_id": "only-tool", "output": "UNKNOWN TOOL FACT"}]}]
  chat_id, _ = make_chat(client, auth, db, messages=source)
  assert batch(client, auth, chat_id).json()["ok"]
  assert "UNKNOWN TOOL FACT" not in fake_synthesis[0][0]
  assert "ORIGINAL EVIDENCE REFERENCES" in fake_synthesis[0][0]
  db.expire_all()
  row = db.get(models.Chat, chat_id)
  assert list(transcript_rows.history(row))[:-1] == source
  assert "tool-output/<tool_use_id>" in _latest_compaction_brief(row)


def test_expired_chat_purge_removes_its_compaction_drafts(client, auth, db, fake_synthesis, monkeypatch):
  from datetime import UTC, datetime, timedelta
  from app import chat_retention
  chat_id, _ = make_chat(client, auth, db, large=True)
  batch(client, auth, chat_id)
  row = db.get(models.Chat, chat_id)
  row.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()
  monkeypatch.setattr(chat_retention, "_purge_chat_storage", lambda _chat_id: None)
  assert chat_id in chat_retention.purge_expired_chat_tombstones(db)
  assert db.get(models.ChatCompactionDraft, "first") is None


@pytest.mark.asyncio
async def test_stop_owns_admission_before_any_provider_call(client, auth, db, fake_synthesis, monkeypatch):
  from concurrent.futures import Future
  from app.main import app
  from app.chat_writer import BeginManualCompaction
  chat_id, original = make_chat(client, auth, db)
  entered = asyncio.Event()
  writer = get_writer()
  submit = writer.submit
  held = []

  def delayed(cmd):
    if isinstance(cmd, BeginManualCompaction):
      ack = Future()
      held.append((cmd, ack))
      entered.set()
      return ack
    return submit(cmd)

  monkeypatch.setattr(writer, "submit", delayed)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as cli:
    worker = asyncio.create_task(cli.post(f"/api/chats/{chat_id}/compact", headers=auth, json={"batch_id": "first"}))
    await asyncio.wait_for(entered.wait(), 5)
    assert registry.get_handle(chat_id, RunnerKind.COMPACTION) is not None
    assert (await cli.post(f"/api/chats/{chat_id}/compact-stop", headers=auth)).status_code == 200
    reply = await asyncio.wait_for(worker, 5)
    assert reply.status_code == 200 and reply.json()["ok"] is False
  # A writer receipt completing after cancellation must still be fenced.
  cmd, _ack = held[0]
  assert submit(cmd).result(5)["status"] == "conflict"
  assert not fake_synthesis
  db.expire_all()
  row = db.get(models.Chat, chat_id)
  assert row.session_id == "original-session"
  assert list(transcript_rows.history(row)) == original


def test_paused_recovery_keeps_original_guidance_for_later_batches(client, auth, db, fake_synthesis):
  chat_id, _ = make_chat(client, auth, db, large=True)
  first = client.post(f"/api/chats/{chat_id}/compact", headers=auth,
                      json={"batch_id": "first", "instructions": "Preserve key dates."}).json()
  assert not first["ok"]
  assert batch(client, auth, chat_id, "second", "first").json()["ok"]
  assert len(fake_synthesis) == 11
  assert all("Preserve key dates." in prompt for prompt, _kwargs in fake_synthesis)


def test_lost_final_receipt_reconciles_durable_success_without_new_provider_call(client, auth, db, monkeypatch, fake_synthesis):
  chat_id, original = make_chat(client, auth, db)
  writer = get_writer()
  persist = writer._persist_compaction

  def committed_then_lost(session, cmd):
    result = persist(session, cmd)
    if cmd.recovery_id and result["status"] == "committed":
      raise RuntimeError("Lost acknowledgement after durable commit")
    return result

  monkeypatch.setattr(writer, "_persist_compaction", committed_then_lost)
  result = batch(client, auth, chat_id).json()
  assert result["ok"] and result["progress"] is None
  assert result["stored"]["recovery_id"] == "first"
  assert len(fake_synthesis) == 1
  assert batch(client, auth, chat_id).json()["ok"]
  assert len(fake_synthesis) == 1
  db.expire_all()
  row = db.get(models.Chat, chat_id)
  assert row.session_id is None
  assert list(transcript_rows.history(row))[:-1] == original
