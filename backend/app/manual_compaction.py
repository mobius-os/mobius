"""Explicit source-bound compaction batches; saved progress is never a queue."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
from datetime import UTC, datetime

from fastapi import HTTPException

from app import models, providers, transcript_rows
from app.chat_continuity import note_path, recovery_source
from app.chat_notes import extract_full_digest
from app.config import get_settings
from app.runner_registry import RunnerKind, registry


log = logging.getLogger("moebius.chat")

def _note(chat_id: str) -> str:
  try:
    return note_path(get_settings().data_dir, chat_id).read_text(encoding="utf-8")
  except FileNotFoundError:
    return ""


def source_identity(chat, messages: list, note: str) -> dict:
  from app.chat_writer import messages_fingerprint
  return {
    "messages_hash": messages_fingerprint(messages),
    "note_hash": hashlib.sha256(note.encode("utf-8")).hexdigest(),
    "provider": chat.provider or "claude",
    "session_id": chat.session_id,
    "overrides": copy.deepcopy(chat.agent_settings_json or {}),
    "settings": providers.effective_agent_settings(
      get_settings().data_dir, chat.agent_settings_json,
      provider=chat.provider or "claude",
    ),
  }


def evidence_reference(chat_id: str, count: int) -> str:
  """A same-chat retrieval path, not an assertion about evidence contents."""
  return (
    f"Original evidence remains in this chat's first {count} saved messages. "
    "Raw tool output and images are not interpreted by this briefing. Before "
    "repeating work or relying on an omitted result, inspect the originals with "
    f"mapi '/api/chats/{chat_id}?limit=100&before={count}'. "
    "Use the returned offset as before to page older messages. Full retained "
    f"tool output is at /api/chats/{chat_id}/tool-output/<tool_use_id> using "
    "the block's exact id; attachments retain their original authorized links. "
    "If an original is unavailable, report that limitation rather than inventing "
    "its content or replaying a side effect."
  )


def _matches_source(db, chat, state: dict) -> bool:
  from app.run_state import has_nonterminal_run
  if (chat is None or chat.deleted_at is not None or chat.pending_messages
      or chat.pending_question_id or has_nonterminal_run(db, chat.id)):
    return False
  return source_identity(chat, transcript_rows.read_all(db, chat), _note(chat.id)) == state["source"]


def checked_draft(db, chat, recovery_id, batch_id, generation):
  """Fence checkpoints and final replacement by source, owner and batch."""
  draft = db.get(models.ChatCompactionDraft, recovery_id)
  if (draft is None or draft.chat_id != chat.id
      or draft.state["status"] != "running"
      or draft.state["batch_id"] != batch_id
      or registry.current_generation(chat.id) != generation
      or not _matches_source(db, chat, draft.state)):
    return None
  return draft


def progress(draft, db=None, chat=None) -> dict | None:
  if draft is None or draft.state["status"] == "complete":
    return None
  state = draft.state
  status = state["status"]
  if status == "running" and registry.get_handle(draft.chat_id, RunnerKind.COMPACTION) is None:
    status = "interrupted"
  if db is not None and chat is not None and not _matches_source(db, chat, state):
    status = "stale"
  return {
    "recovery_id": draft.id, "state": status,
    "next_chunk": state["next_chunk"], "total_chunks": state["total_chunks"],
    "source_bytes": state["source_bytes"], "error": state.get("error"),
  }


def latest_progress(db, chat):
  draft = db.query(models.ChatCompactionDraft).filter_by(chat_id=chat.id).order_by(
    models.ChatCompactionDraft.created_at.desc(), models.ChatCompactionDraft.id.desc(),
  ).first()
  return progress(draft, db, chat)


def persist_manual_progress(db, cmd) -> dict:
  """Writer-only mutations; transcript and session replacement stay in its actor."""
  from app.chat_writer import BeginManualCompaction, AdvanceManualCompaction
  from app.compaction import _validated_briefing

  chat = db.get(models.Chat, cmd.chat_id)
  if chat is None or chat.deleted_at is not None:
    return {"status": "conflict"}
  draft = db.get(models.ChatCompactionDraft, cmd.recovery_id)
  if isinstance(cmd, BeginManualCompaction):
    if draft is not None and draft.chat_id != chat.id:
      return {"status": "conflict"}
    if draft is not None and cmd.batch_id in draft.state["batch_ids"]:
      return {"status": "duplicate", "complete": draft.state["status"] == "complete",
              "progress": progress(draft)}
    if cmd.continuing and draft is None:
      return {"status": "conflict"}
    if registry.current_generation(chat.id) != cmd.generation:
      return {"status": "conflict"}
    if draft is None:
      state = {**copy.deepcopy(cmd.source), "next_chunk": 0, "briefing": None,
               "batch_ids": [], "status": "paused", "error": None}
      if not _matches_source(db, chat, state):
        return {"status": "conflict"}
      draft = models.ChatCompactionDraft(id=cmd.recovery_id, chat_id=chat.id, state=state)
      db.add(draft)
    else:
      state = copy.deepcopy(draft.state)
      if state["status"] == "complete" or not _matches_source(db, chat, state):
        return {"status": "conflict"}
    draft.state = {**state, "status": "running", "batch_id": cmd.batch_id,
                   "batch_ids": state["batch_ids"] + [cmd.batch_id], "error": None}
    draft.updated_at = datetime.now(UTC)
    return {"status": "admitted", "draft": copy.deepcopy(draft.state)}
  if (draft is None or draft.chat_id != chat.id
      or draft.state.get("batch_id") != cmd.batch_id):
    return {"status": "conflict"}
  if isinstance(cmd, AdvanceManualCompaction):
    # Pause/restart revokes this batch's ownership, not its source snapshot.
    # An already queued result must not invalidate saved sections or reopen an
    # ended batch; only source/cursor conflicts make preparation stale.
    if (draft.state["status"] != "running"
        or registry.current_generation(chat.id) != cmd.generation):
      return {"status": "conflict"}
    if (not _matches_source(db, chat, draft.state)
        or draft.state["next_chunk"] != cmd.expected_chunk
        or cmd.next_chunk != cmd.expected_chunk + 1
        or cmd.next_chunk > draft.state["total_chunks"]):
      draft.state = {**draft.state, "status": "stale"}
      return {"status": "conflict"}
    draft.state = {**draft.state, "next_chunk": cmd.next_chunk,
                   "briefing": _validated_briefing(cmd.briefing)}
  elif draft.state["status"] != "complete":
    draft.state = {**draft.state,
                   "status": "stale" if draft.state["status"] == "stale" else "paused",
                   "error": cmd.error}
  draft.updated_at = datetime.now(UTC)
  return {"status": "saved", "progress": progress(draft)}


async def compact_batch(chat_id: str, body, db):
  """Run only this explicitly requested batch under the existing transition lock."""
  from app.chat_queue import get_transition_lock
  from app.chat_visibility import provider_switch_allowed
  from app.chat_compaction_state import compacting
  from app.chat_writer import (
    BeginManualCompaction, AdvanceManualCompaction, EndManualCompaction,
    PersistCompaction, alloc_run_token, await_ack, get_writer,
  )
  from app.compaction import (
    build_synthesis_source, summarize_batch, _utf8_chunks,
    _SYNTHESIS_CHUNK_BYTES, CompactionError, RecoverySynthesisHandle,
  )
  from app.run_state import has_nonterminal_run

  async with get_transition_lock(chat_id):
    db.expire_all()
    chat = db.get(models.Chat, chat_id)
    if chat is None or chat.deleted_at is not None:
      raise HTTPException(404, "Chat not found.")
    if not provider_switch_allowed(chat):
      raise HTTPException(409, "This background chat stays on its original provider.")
    recovery_id = body.recovery_id or body.batch_id
    existing = db.get(models.ChatCompactionDraft, recovery_id)
    if existing is not None and existing.chat_id == chat_id and body.batch_id in existing.state["batch_ids"]:
      return {"ok": existing.state["status"] == "complete", "progress": progress(existing, db, chat)}
    if (chat.pending_messages or chat.pending_question_id or has_nonterminal_run(db, chat_id)
        or not registry.mark_starting(chat_id)):
      raise HTTPException(409, "Chat is busy — finish or stop the current turn before compacting.")
    generation = registry.current_generation(chat_id)
    async def operation():
      messages = list(transcript_rows.history(chat))
      note = _note(chat_id)
      digest = extract_full_digest(note)
      selected = messages
      try:
        digest, selected = recovery_source(note, messages)
      except ValueError:
        pass  # Preserve full originals when the note cannot replace its prefix.
      evidence = {"message_count": len(messages),
                  "tool_messages": sum(any(b.get("type") == "tool" for b in m.get("blocks", []) if isinstance(b, dict)) for m in messages if isinstance(m, dict)),
                  "attachment_messages": sum(bool(m.get("attachments")) or any(bool(b.get("attachments")) for b in m.get("blocks", []) if isinstance(b, dict)) for m in messages if isinstance(m, dict))}
      try:
        material = build_synthesis_source(selected, source_digest=digest)
      except CompactionError:
        if not evidence["tool_messages"] and not evidence["attachment_messages"]:
          raise
        material = "--- ORIGINAL EVIDENCE REFERENCES ---\n" + evidence_reference(chat_id, len(messages))
      total = sum(1 for _ in _utf8_chunks(material, _SYNTHESIS_CHUNK_BYTES))
      source = {
        "source": source_identity(chat, messages, note),
        "material_hash": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "source_bytes": len(material.encode("utf-8")), "total_chunks": total,
        "instructions": body.instructions,
        "evidence": evidence,
      }
      admitted = await await_ack(get_writer().submit(BeginManualCompaction(
        chat_id=chat_id, recovery_id=recovery_id, batch_id=body.batch_id,
        source=source, generation=generation, continuing=bool(body.recovery_id),
      )))
      if admitted["status"] == "duplicate":
        return {"ok": admitted["complete"], "progress": admitted["progress"]}
      if admitted["status"] != "admitted":
        raise HTTPException(409, "The recovery source changed. Start compaction again.")
      state = admitted["draft"]
      if source["material_hash"] != state["material_hash"]:
        await await_ack(get_writer().submit(EndManualCompaction(
          chat_id=chat_id, recovery_id=recovery_id, batch_id=body.batch_id,
          error="Recovery source changed; start compaction again.",
        )))
        raise HTTPException(409, "The recovery source changed. Start compaction again.")
      # Do not hold a read transaction across provider I/O or writer acks.
      db.rollback()

      async def work():
        cursor = state["next_chunk"]

        async def checkpoint(next_chunk, briefing):
          nonlocal cursor
          saved = await await_ack(get_writer().submit(AdvanceManualCompaction(
            chat_id=chat_id, recovery_id=recovery_id, batch_id=body.batch_id,
            generation=generation, expected_chunk=cursor,
            next_chunk=next_chunk, briefing=briefing,
          )))
          if saved["status"] != "saved":
            raise CompactionError("The conversation changed; completed preparation was preserved.")
          cursor = next_chunk

        if cursor == state["total_chunks"]:
          # A crash after the final checkpoint must not re-run a paid call.
          result = {"complete": True, "briefing": state["briefing"]}
        else:
          result = await summarize_batch(
            material, start_chunk=cursor, briefing=state["briefing"],
            data_dir=get_settings().data_dir, provider_id=state["source"]["provider"],
            model=state["source"]["settings"].get("model"),
            effort=state["source"]["settings"].get("effort"),
            custom_instructions=state["instructions"], checkpoint=checkpoint,
          )
        if not result["complete"]:
          return {"ok": False}
        committed = await await_ack(get_writer().submit(PersistCompaction(
          chat_id=chat_id, run_token=alloc_run_token(), summary=result["briefing"],
          expected_provider=state["source"]["provider"],
          source_messages_hash=state["source"]["messages_hash"],
          recovery_id=recovery_id, batch_id=body.batch_id, generation=generation,
        )))
        if committed["status"] != "committed":
          raise CompactionError("The conversation changed; its previous session is unchanged.")
        return {"ok": True, "summary": result["briefing"], "stored": committed["stored"]}

      async def run_batch():
        # The registered lifetime includes the durable end checkpoint, so Stop
        # and restart cannot report completion while cleanup still uses writer.
        error = None
        with compacting(chat_id, "compact"):
          try:
            return await work()
          except asyncio.CancelledError:
            error = "Preparation stopped. Completed sections are saved; no next batch was started."
          except CompactionError:
            error = "This batch could not finish safely. Completed sections are saved; the previous session is unchanged."
          except Exception as exc:
            log.warning("Manual compaction batch failed (%s)", type(exc).__name__)
            error = "This batch failed. Completed preparation and the previous session are preserved."
          finally:
            await await_ack(get_writer().submit(EndManualCompaction(
              chat_id=chat_id, recovery_id=recovery_id, batch_id=body.batch_id, error=error,
            )))
        return {"ok": False}

      result = await run_batch()
      db.expire_all()
      draft = db.get(models.ChatCompactionDraft, recovery_id)
      current = db.get(models.Chat, chat_id)
      if draft is not None and draft.state["status"] == "complete" and not result["ok"]:
        # The final write can commit even if its acknowledgement is lost. Its
        # durable terminal state is authoritative, never another synthesis call.
        stored = next((m for m in reversed(transcript_rows.read_all(db, current))
                       if m.get("kind") == "compaction" and m.get("recovery_id") == recovery_id), None)
        if stored is not None:
          result = {"ok": True, "summary": stored["content"], "stored": stored}
      return {**result, "progress": progress(draft, db, current)}
    # Register before the first await (including writer admission). Pause and
    # planned restart own the whole batch, not just provider execution.
    async def registered_operation():
      try:
        return await operation()
      except asyncio.CancelledError:
        # Admission may already be queued in the writer. End is ordered after
        # it, and generation fencing denies any delayed work before provider I/O.
        await await_ack(get_writer().submit(EndManualCompaction(
          chat_id=chat_id, recovery_id=recovery_id, batch_id=body.batch_id,
          error="Preparation stopped before synthesis. No next batch was started.",
        )))
        db.expire_all()
        return {"ok": False, "progress": progress(
          db.get(models.ChatCompactionDraft, recovery_id), db, db.get(models.Chat, chat_id),
        )}

    task = asyncio.create_task(registered_operation())
    handle = RecoverySynthesisHandle(chat_id, task)
    registry.register(handle)
    try:
      return await task
    finally:
      if registry.get_handle(chat_id, RunnerKind.COMPACTION) is handle:
        registry.unregister(chat_id, RunnerKind.COMPACTION)
      registry.discard_starting(chat_id)
