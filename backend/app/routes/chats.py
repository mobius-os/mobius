"""Routes for chat CRUD operations."""

import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models, questions
from app.config import get_settings
from app.chat import (
  bump_run_generation,
  forget_chat,
  is_chat_running,
  stop_chat_for,
)
from app.database import get_db
from app.deps import get_current_owner
from app.resource_access import get_active_chat_or_404
from app.schemas import ChatPatch

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chats", tags=["chats"])

SOFT_DELETE_TTL = timedelta(days=7)


def _purge_chat_dir(chat_id: str) -> None:
  """Removes per-chat scratch dirs left on disk after a chat is gone.

  Two locations get cleaned: the chat's data dir
  (`/data/chats/{chat_id}/` — uploads, generated images, scratch)
  and its agent-browser Chromium profile
  (`/data/agent-browser-profiles/chat-{chat_id}/` — IndexedDB,
  cache, cookies; typically 50-200 MB per profile that's seen any
  use). Without the second rmtree, profiles accumulated across
  every chat that ever invoked agent-browser and were never
  reclaimed by chat-delete or the 7-day soft-delete purge — a slow
  disk leak proportional to chat count, not time.

  Both rmtrees use `ignore_errors=True` so chats that never wrote
  to a given location don't raise.
  """
  data_dir = Path(get_settings().data_dir)
  shutil.rmtree(data_dir / "chats" / chat_id, ignore_errors=True)
  shutil.rmtree(
    data_dir / "agent-browser-profiles" / f"chat-{chat_id}",
    ignore_errors=True,
  )



class ChatUpdate(BaseModel):
  title: str | None = None
  messages: list[dict] | None = None


def _coerce_agent_settings(raw) -> dict:
  """Returns a fresh dict from a possibly-string JSON value.

  SQLAlchemy's JSON column type usually returns dict on read, but
  on some SQLite + driver combos (especially with text-backed JSON
  columns) the value comes back as a raw string. Calling
  `dict(some_str)` raises TypeError. Normalize once at every
  read site to defend against that — and against legacy rows
  written before the column was typed as JSON.

  Returns `{}` for None, invalid JSON, or non-dict values.
  """
  if raw is None:
    return {}
  if isinstance(raw, dict):
    return dict(raw)
  if isinstance(raw, str):
    try:
      parsed = json.loads(raw)
      return dict(parsed) if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
      return {}
  return {}


@router.get("")
def list_chats(
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns all active chats ordered by most recently updated."""
  # Purge chats soft-deleted more than TTL ago.
  # Use naive datetime to match SQLite's naive UTC storage — comparing an
  # aware datetime against a naive DB value throws TypeError in Python 3.11+.
  cutoff = datetime.now(UTC).replace(tzinfo=None) - SOFT_DELETE_TTL
  stale = db.query(models.Chat).filter(
    models.Chat.deleted_at.isnot(None),
    models.Chat.deleted_at < cutoff,
  ).all()
  for c in stale:
    questions.cancel(c.id)
    forget_chat(c.id)
    _purge_chat_dir(c.id)
    db.delete(c)
  # Notification TTL: rows are written by every AskUserQuestion ack
  # and every agent-driven push, and nothing else deletes them. Keep
  # the table from growing unbounded by dropping anything older than
  # 90 days alongside the chat purge above — same cadence, same
  # transaction. Naive UTC matches `Notification.sent_at`'s storage
  # format (see the chat cutoff above for the same TypeError-avoidance
  # rationale).
  notification_cutoff = (
    datetime.now(UTC).replace(tzinfo=None) - timedelta(days=90)
  )
  db.query(models.Notification).filter(
    models.Notification.sent_at < notification_cutoff,
  ).delete(synchronize_session=False)
  db.commit()

  # Pinned chats sort first (newest pin at top of the pinned group),
  # then unpinned by recency. `pinned_at IS NOT NULL` is the primary
  # key on SQLite's order_by — a `desc()` on a nullable column would
  # put NULL last under our SQLite collation, but making the boolean
  # explicit is clearer and portable.
  chats = (
    db.query(models.Chat)
    .filter(models.Chat.deleted_at.is_(None))
    .order_by(
      models.Chat.pinned_at.is_(None),
      models.Chat.pinned_at.desc(),
      models.Chat.updated_at.desc(),
    )
    .all()
  )
  return [
    {
      "id": c.id,
      "title": c.title,
      "updated_at": c.updated_at.isoformat(),
      "pinned_at": c.pinned_at.isoformat() if c.pinned_at else None,
      "has_messages": bool(c.messages and len(c.messages) > 0),
    }
    for c in chats
  ]


@router.post("")
def create_chat(
  body: ChatUpdate,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Creates a new chat.

  Snapshots the current global agent-settings defaults (model +
  effort) into chat.agent_settings_json so the picker always renders
  with something selected AND so subsequent changes to the global
  default don't bleed into this chat. The chat's provider is
  inherited from owner.provider (the implicit "default = last
  picked").
  """
  import uuid
  from app.providers import initial_chat_defaults

  owner = db.query(models.Owner).first()
  provider = (owner.provider if owner else None) or "claude"
  defaults = initial_chat_defaults(get_settings().data_dir, provider)

  chat = models.Chat(
    id=str(uuid.uuid4()),
    title=body.title or "New chat",
    messages=body.messages or [],
    provider=provider,
    agent_settings_json=defaults,
  )
  db.add(chat)
  db.commit()
  db.refresh(chat)
  return {"id": chat.id, "title": chat.title, "messages": chat.messages}


@router.put("/{chat_id}")
def update_chat(
  body: ChatUpdate,
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Updates a chat's title and/or messages."""
  chat = get_active_chat_or_404(db, chat_id)
  if body.title is not None:
    chat.title = body.title
  if body.messages is not None:
    chat.messages = body.messages
  # Always touch updated_at so the chat moves to the top of history.
  chat.updated_at = datetime.now(UTC)
  db.commit()
  return {"ok": True}


@router.patch("/{chat_id}")
async def patch_chat(
  body: ChatPatch,
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Partial-update endpoint used by the `/` slash picker.

  The picker writes per-chat overrides for the agent runtime (model,
  effort, ...) here. The new dict is MERGED into the existing
  `agent_settings_json` (last-write-wins per key) so changing just
  `effort` doesn't blow away a previously-picked `model`.

  Pass `clear_agent_settings=true` to revert this chat to the global
  default. The `effective` field in the response is what the next
  turn will actually use (override merged onto global default).

  Serialized per-chat via the same lock that guards pending_messages
  RMW — two PATCHes racing on the same chat would otherwise both
  read the same snapshot and the later commit would clobber keys
  from the earlier one.
  """
  from sqlalchemy.orm.attributes import flag_modified
  from app.config import get_settings as get_app_settings
  from app.providers import effective_agent_settings
  from app.chat_queue import get_lock as get_queue_lock

  async with get_queue_lock(chat_id):
    chat = get_active_chat_or_404(db, chat_id)

    # Drawer rename. Trim + reject empty so a stray blur on an empty
    # input can't silently blank a chat's title.
    if body.title is not None:
      new_title = body.title.strip()
      if new_title:
        chat.title = new_title

    # Drawer pin toggle. We stamp the time on pin so the pinned group
    # sorts newest-pinned-first within itself.
    if body.pinned is not None:
      chat.pinned_at = (
        datetime.now(UTC).replace(tzinfo=None) if body.pinned else None
      )

    if body.clear_agent_settings:
      chat.agent_settings_json = None
    elif body.agent_settings_json is not None:
      existing = _coerce_agent_settings(chat.agent_settings_json)
      for k, v in body.agent_settings_json.model_dump(exclude_unset=True).items():
        if v is None:
          existing.pop(k, None)
        else:
          existing[k] = v
      chat.agent_settings_json = existing or None
      # SQLAlchemy doesn't always notice in-place JSON mutations even
      # after a fresh dict assignment in older versions; flag_modified
      # is the belt-and-suspenders fix.
      flag_modified(chat, "agent_settings_json")

    # Determine the effective target provider. The body may set it
    # explicitly, OR it may be implied by a model-only PATCH whose
    # `model` belongs to a different provider than the chat is
    # currently on. The latter case used to leak through silently,
    # leaving `chat.provider=codex` + `chat.agent_settings_json.model
    # = claude-sonnet-X`; the runner's own cross-provider fallback
    # (claude_sdk_runner / codex_sdk_runner) then re-normalized at
    # turn time, masking the picker bug and running the wrong model.
    # Infer the provider from the model whenever the user didn't
    # state one explicitly so the chat row stays self-consistent.
    target_provider = body.provider
    if (
      target_provider is None
      and body.agent_settings_json is not None
    ):
      new_model = body.agent_settings_json.model_dump(exclude_unset=True).get(
        "model"
      )
      if new_model:
        from app.providers import _model_belongs_to_other_provider
        current_provider = chat.provider or "claude"
        if _model_belongs_to_other_provider(new_model, current_provider):
          target_provider = (
            "codex" if current_provider == "claude" else "claude"
          )

    if target_provider is not None and target_provider in ("claude", "codex"):
      # Reject a switch to a disconnected provider — the picker may
      # have raced ahead of /auth/providers/status, or the user may
      # be on stale state. Without this check the PATCH would succeed
      # silently and then every subsequent message turn would fail
      # auth, leaving the user confused. 409 surfaces the real
      # problem at pick-time.
      from app.providers import get_provider
      candidate = get_provider(target_provider)
      auth_error = candidate.check_auth(get_app_settings().data_dir)
      if auth_error is not None:
        raise HTTPException(
          status_code=409,
          detail=(
            f"{candidate.name} is not connected. "
            "Open Settings to connect, then try again."
          ),
        )
      if chat.provider != target_provider:
        # Sessions aren't cross-provider portable: a Claude session id
        # is not a valid Codex thread id and vice versa. Wipe the
        # session id when the provider actually changes so the next
        # turn starts a fresh session for the new provider. The
        # frontend lock (has_assistant_turns → only same-provider
        # picks visible) prevents this from happening mid-thread in
        # the UI, but a direct API caller or a recovery scenario can
        # still hit it.
        chat.session_id = None
      chat.provider = target_provider

    db.commit()
    db.refresh(chat)
    data_dir = get_app_settings().data_dir

    # Mirror the new pick to the global default immediately. New
    # chats read /data/shared/agent-settings.json on creation, so
    # the user's latest model/effort/provider becomes the seed for
    # the next new chat. Mirror is best-effort + ADDITIVE: only
    # keys actually set on the chat are written, preserving any
    # other keys already in the global file.
    settings_obj = _coerce_agent_settings(chat.agent_settings_json) or {}
    if settings_obj:
      from app.providers import _load_agent_settings, write_agent_settings
      mirror = _load_agent_settings(data_dir) or {}
      for key in ("model", "effort", "effort_by_provider"):
        value = settings_obj.get(key)
        if value is not None:
          mirror[key] = value
      if mirror:
        write_agent_settings(data_dir, mirror)
    if chat.provider:
      owner = db.query(models.Owner).first()
      if owner is not None:
        owner.provider = chat.provider
        db.commit()

    return {
      "ok": True,
      "agent_settings_json": _coerce_agent_settings(chat.agent_settings_json) or None,
      "provider": chat.provider or "claude",
      "effective": effective_agent_settings(
        data_dir,
        _coerce_agent_settings(chat.agent_settings_json) or None,
        provider=chat.provider or "claude",
      ),
    }


@router.get("/{chat_id}")
def get_chat(
  chat_id: str,
  limit: int = 20,
  before: int | None = None,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns a chat with paginated messages and running status.

  Pagination uses a message-index cursor. `before` is the index (in the full
  message list) of the first message the client does NOT have. Omit it (or
  pass None) to fetch the most recent `limit` messages. Pass the index of
  the oldest message from the previous page to load older messages.

  Messages are returned in the order they appear in the list, so newer
  messages have higher indices. The response includes `offset` (the index
  of the first message in this page) and `total` (total message count).
  """
  chat = get_active_chat_or_404(db, chat_id)
  all_msgs = chat.messages or []
  total = len(all_msgs)
  if before is not None:
    start = max(0, before - limit)
    page = all_msgs[start:before]
  else:
    start = max(0, total - limit)
    page = all_msgs[start:]
  # Compute the effective per-turn agent settings — provider-aware
  # so the picker always has a real model + effort to show, even for
  # legacy chats that never got a create_chat snapshot.
  from app.config import get_settings as get_app_settings
  from app.providers import effective_agent_settings
  data_dir = get_app_settings().data_dir
  has_assistant_turns = any(
    m.get("role") == "assistant" for m in all_msgs
  )
  provider = chat.provider or "claude"
  return {
    "id": chat.id,
    "title": chat.title,
    "messages": page,
    "pending_messages": list(chat.pending_messages or []),
    "total": total,
    "offset": start,
    "running": is_chat_running(chat_id),
    "session_id": chat.session_id,
    "provider": provider,
    "agent_settings_json": _coerce_agent_settings(chat.agent_settings_json) or None,
    "effective_agent_settings": effective_agent_settings(
      data_dir,
      _coerce_agent_settings(chat.agent_settings_json) or None,
      provider=provider,
    ),
    "has_assistant_turns": has_assistant_turns,
  }


@router.delete("/{chat_id}", status_code=204)
async def delete_chat(
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Soft-deletes a chat and stops any running agent for it."""
  # Only attempt to stop if the chat is actually running. An idle chat
  # has no proc/SDK client/session to interrupt, so calling
  # stop_chat_for would be a no-op — but a transient error during the
  # no-op (DB hiccup, lookup glitch) would falsely 409 and make the
  # chat un-deleteable. The 409 only fires when the chat WAS running
  # and we couldn't stop it cleanly — that's the case we actually need
  # to protect against (orphan runner writing to a soft-deleted row).
  if is_chat_running(chat_id):
    try:
      stopped = await stop_chat_for(chat_id, db=db)
    except Exception:
      log.warning("Failed to stop agent for chat %s during delete", chat_id)
      stopped = False
    if not stopped:
      raise HTTPException(
        status_code=409,
        detail="Could not stop active agent; retry",
      )
  # Bump generation BEFORE the soft-delete commit so that any run
  # that started in the TOCTOU window between the is_chat_running
  # check above and now sees `we_own_gen == False` on its next gen
  # check and skips auto-promote / continuation. Otherwise a runner
  # racing the delete could write to the just-deleted row.
  bump_run_generation(chat_id)
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  if chat:
    chat.deleted_at = datetime.now(UTC)
    db.commit()
  # Drop in-memory per-chat state so a deleted chat doesn't leave a
  # stale `_run_generation` entry on long-running containers.
  questions.cancel(chat_id)
  forget_chat(chat_id)


@router.post("/{chat_id}/recover")
def recover_chat(
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Restores a soft-deleted chat if the TTL window has not expired."""
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.isnot(None),
  ).first()
  if not chat:
    raise HTTPException(status_code=404, detail="Chat not found or not deleted.")
  if (datetime.now(UTC).replace(tzinfo=None) - chat.deleted_at) >= SOFT_DELETE_TTL:
    raise HTTPException(status_code=410, detail="Recovery window has expired.")
  chat.deleted_at = None
  db.commit()
  return {"ok": True}


class QuestionAnswers(BaseModel):
  answers: dict


@router.post("/{chat_id}/question-answers")
async def save_question_answers(
  chat_id: str,
  body: QuestionAnswers,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Saves the user's answers into the last question block."""
  from sqlalchemy.orm.attributes import flag_modified

  chat = get_active_chat_or_404(db, chat_id)
  msgs = list(chat.messages or [])
  for msg in reversed(msgs):
    if msg.get("role") != "assistant":
      continue
    for block in reversed(msg.get("blocks", [])):
      if block.get("type") == "question":
        block["answers"] = body.answers
        chat.messages = msgs
        flag_modified(chat, "messages")
        db.commit()
        return {"ok": True}
  raise HTTPException(status_code=404, detail="No question block found.")
