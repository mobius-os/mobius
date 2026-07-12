"""Routes for chat CRUD operations."""

import json
import logging
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import activity, auth, models, providers, questions
from app.config import get_settings
from app.events import (
  TOOL_OUTPUT_INLINE_THRESHOLD as _TOOL_OUTPUT_INLINE_THRESHOLD,
  excerpt_tool_output,
)
from app.chat import (
  _clear_run_status,
  bump_run_generation,
  forget_chat,
  is_chat_running,
  mark_chat_deleted,
  recover_chat_generation,
  stop_chat_for,
)
from app.database import get_db
from app.deps import (
  Principal, get_current_owner, get_principal, reject_cross_site,
)
from app.resource_access import get_active_chat_for_principal, get_active_chat_or_404
from app.schemas import ChatPatch
from app.timeutil import now_naive_utc, SOFT_DELETE_TTL

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chats", tags=["chats"])

# Separate router for the app-attributed chat contract (design §1). It
# lives under its own /api/app-chats prefix so the owner-only /api/chats
# surface stays unambiguously owner-only — the app path is additive and
# greppable, not a flag threaded through the owner routes.
app_chat_router = APIRouter(prefix="/api/app-chats", tags=["app-chats"])

# SOFT_DELETE_TTL is imported from app.timeutil — one shared window for chat +
# app soft-delete so the two recovery periods can't drift.

# How long an untouched empty chat (no session, no messages, no pending
# queue) survives before the list_chats sweeper hard-deletes it. Long
# enough that a user who opened a chat, started a draft in the browser,
# and walked away for the afternoon doesn't lose it; short enough that
# abandoned empties don't pile up across weeks. Hard-delete (not soft)
# because there's nothing to recover — content lived only in the
# browser's sessionStorage, which is the user's problem to preserve.
EMPTY_CHAT_GRACE = timedelta(hours=24)


def _purge_chat_dir(chat_id: str) -> None:
  """Removes per-chat dirs left on disk after a chat is gone.

  Three locations get cleaned: the chat's data dir
  (`/data/chats/{chat_id}/` — uploads, generated images, scratch),
  its agent-browser Chromium profile
  (`/data/agent-browser-profiles/chat-{chat_id}/` — IndexedDB,
  cache, cookies; typically 50-200 MB per profile that's seen any
  use), and its memory note dir
  (`/data/shared/memory/chats/{chat_id}/`). Without the second
  rmtree, profiles accumulated across every chat that ever invoked
  agent-browser and were never reclaimed by chat-delete or the
  7-day soft-delete purge — a slow disk leak proportional to chat
  count, not time. The note is DERIVED from the chat, so the
  owner's delete intent covers it; by hard-purge time the 7-day
  soft-delete window plus nightly reflection have had time to
  promote durable facts into topic notes, and an orphan note would
  otherwise linger as a memory entry pointing at a chat that no
  longer exists.

  All rmtrees use `ignore_errors=True` so chats that never wrote
  to a given location don't raise.
  """
  data_dir = Path(get_settings().data_dir)
  shutil.rmtree(data_dir / "chats" / chat_id, ignore_errors=True)
  shutil.rmtree(
    data_dir / "agent-browser-profiles" / f"chat-{chat_id}",
    ignore_errors=True,
  )
  shutil.rmtree(
    data_dir / "shared" / "memory" / "chats" / chat_id,
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


def _visible_in_owner_drawer(chat: models.Chat) -> bool:
  if chat.created_by_app_id is None:
    return True
  settings = _coerce_agent_settings(chat.agent_settings_json)
  return settings.get("owner_visible") is True


@router.post(
  "/{chat_id}/media-token",
  dependencies=[Depends(reject_cross_site)],
)
def issue_media_token(
  chat_id: str,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Issues a short-lived media token scoped to one chat's uploads/generated images.

  <img> tags and direct image fetches can't set Authorization headers, so they
  must use ?token= query params. Passing the full 30-day owner JWT as a query
  param leaks it into access logs, browser history, and Referer headers.

  This endpoint mints a 15-minute token with scope='media' and media_chat=chat_id.
  The serve routes (uploads, generated images) accept ONLY these tokens on ?token=;
  they explicitly reject owner JWTs arriving via query params.

  Cache the returned token client-side (~10 min) and refresh on 401. The token is
  revoked by "sign out everywhere" like all other tokens (carries token_epoch).
  """
  # Verify the chat exists and belongs to this owner before issuing a token.
  get_active_chat_or_404(db, chat_id)
  token = auth.create_media_token(
    chat_id=chat_id,
    owner_username=owner.username,
    token_epoch=owner.token_epoch,
  )
  return {"token": token, "expires_in": 900}


@router.get("")
def list_chats(
  include_app_chats: bool = False,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns all active chats ordered by most recently updated."""
  # Purge chats soft-deleted more than TTL ago.
  # Use naive datetime to match SQLite's naive UTC storage — comparing an
  # aware datetime against a naive DB value throws TypeError in Python 3.11+.
  cutoff = now_naive_utc() - SOFT_DELETE_TTL
  stale = db.query(models.Chat).filter(
    models.Chat.deleted_at.isnot(None),
    models.Chat.deleted_at < cutoff,
  ).all()
  for c in stale:
    questions.cancel(c.id)
    forget_chat(c.id)
    _purge_chat_dir(c.id)
    # Drop the chat's durable run records (077 Step 3) with it. chat_runs has
    # no ON DELETE CASCADE and SQLite leaves FK enforcement off, so a hard
    # chat delete would otherwise orphan its run rows and grow the table
    # unbounded over the instance's life.
    db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == c.id
    ).delete(synchronize_session=False)
    # Drop the chat's stashed large tool outputs (contract rule 6) with it —
    # same lifecycle, same no-FK-cascade reasoning as chat_runs above. The
    # rows rode the soft-delete window (a recovered chat re-showed its
    # outputs); at hard-purge time the chat is gone for good, so are they.
    db.query(models.ToolOutput).filter(
      models.ToolOutput.chat_id == c.id
    ).delete(synchronize_session=False)
    db.delete(c)
  # Hard-delete abandoned empties — chats that were created, never had
  # a message sent (no session_id, no messages, no pending queue), and
  # have been sitting that way for over EMPTY_GRACE. The soft-delete
  # TTL above is for chats the user EXPLICITLY deleted — those have
  # content worth a 7-day recovery window. An untouched empty has
  # nothing to recover; the soft-delete dance just defers reclaim.
  # The grace protects a chat opened minutes ago with a draft in the
  # browser's sessionStorage from being nuked out from under the user
  # between draft autosaves. JSON-column emptiness is checked in Python
  # rather than SQL because cross-dialect `JSON = '[]'` is fragile;
  # the SQL prefilter (NULL session, NULL deleted_at, older than the
  # grace) keeps the candidate set small.
  empty_cutoff = now_naive_utc() - EMPTY_CHAT_GRACE
  candidates = db.query(models.Chat).filter(
    models.Chat.deleted_at.is_(None),
    models.Chat.session_id.is_(None),
    models.Chat.created_at < empty_cutoff,
  ).all()
  for c in candidates:
    if c.messages or c.pending_messages:
      continue
    # An app-attributed chat is a mini-app's durable anchor: the app persists
    # its id (window.mobius.chat persist -> chat_id.json) and resumes it across
    # mounts, so an empty app-chat is not abandoned scratch the way an owner's
    # new-chat-then-leave is. Hard-deleting it left the app's persisted id
    # pointing at a dead row, so the next mount's resume PATCH 404'd. Leave
    # app-chats for the app + the soft-delete TTL to manage.
    if c.created_by_app_id is not None:
      continue
    questions.cancel(c.id)
    forget_chat(c.id)
    _purge_chat_dir(c.id)
    # Drop the chat's run records with it (see the stale-purge note above —
    # no FK cascade on SQLite, so these would orphan otherwise).
    db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == c.id
    ).delete(synchronize_session=False)
    db.delete(c)
  # Notification TTL: rows are written by agent-driven push calls
  # (POST /api/notifications/send), and nothing else deletes them. Keep
  # the table from growing unbounded by dropping anything older than
  # 90 days alongside the chat purge above — same cadence, same
  # transaction. Naive UTC matches `Notification.sent_at`'s storage
  # format (see the chat cutoff above for the same TypeError-avoidance
  # rationale).
  notification_cutoff = now_naive_utc() - timedelta(days=90)
  db.query(models.Notification).filter(
    models.Notification.sent_at < notification_cutoff,
  ).delete(synchronize_session=False)
  db.commit()

  # Pinned chats sort first (newest pin at top of the pinned group),
  # then unpinned by owner-send recency. `activity_at` is the drawer
  # ordering key; `updated_at` remains the generic row-modified time.
  # `pinned_at IS NOT NULL` is the primary key on SQLite's order_by —
  # a `desc()` on a nullable column would put NULL last under our
  # SQLite collation, but making the boolean explicit is clearer and
  # portable.
  q = db.query(models.Chat).filter(models.Chat.deleted_at.is_(None))
  chats = (
    q.order_by(
      models.Chat.pinned_at.is_(None),
      models.Chat.pinned_at.desc(),
      func.coalesce(models.Chat.activity_at, models.Chat.updated_at).desc(),
    )
    .all()
  )
  if not include_app_chats:
    # Drawer history is the owner's browse list. Most app-attributed chats are
    # embedded app panels and stay hidden; an app can opt a spawned, first-class
    # owner conversation into the drawer by setting owner_visible at creation.
    chats = [c for c in chats if _visible_in_owner_drawer(c)]
  return [
    {
      "id": c.id,
      "title": c.title,
      "updated_at": c.updated_at.isoformat(),
      "activity_at": c.activity_at.isoformat() if c.activity_at else None,
      "pinned_at": c.pinned_at.isoformat() if c.pinned_at else None,
      "has_messages": bool(c.messages and len(c.messages) > 0),
      "created_by_app_id": c.created_by_app_id,
      "run_status": c.run_status,
      "running": c.run_status == "running" or is_chat_running(c.id),
    }
    for c in chats
  ]


@router.post("", dependencies=[Depends(reject_cross_site)])
def create_chat(
  body: ChatUpdate,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Creates a new chat.

  Leaves `agent_settings_json` NULL so the chat reads the live global
  defaults from `/data/shared/agent-settings.json` until the user
  picks something specific. Snapshotting at creation time used to
  freeze whatever the defaults were when the empty chat was first
  created, and the frontend's empty-chat reuse path then surfaced
  that stale snapshot — silently ignoring whichever model/effort the
  user had since picked. The snapshot now happens lazily, at the
  first commit point: a PATCH from the picker (see `patch_chat`
  below) or the first message send (see `chat.py:_snapshot_initial_settings`).
  Either path freezes the chat's settings so subsequent global
  changes from OTHER chats don't bleed in. Provider is still
  inherited from owner.provider — the implicit "default = last
  picked" — because the provider lock kicks in after the first
  assistant turn and we want the new chat to start on the user's
  current provider.
  """
  import uuid

  owner = db.query(models.Owner).first()
  data_dir = get_settings().data_dir
  provider = providers.resolve_default_provider(
    data_dir, owner.provider if owner else None,
  )

  chat = models.Chat(
    id=str(uuid.uuid4()),
    title=body.title or "New chat",
    messages=body.messages or [],
    provider=provider,
    agent_settings_json=None,
  )
  db.add(chat)
  db.commit()
  db.refresh(chat)
  activity.log_event("chat_created", chat_id=chat.id)
  return {"id": chat.id, "title": chat.title, "messages": chat.messages}


@router.put("/{chat_id}", dependencies=[Depends(reject_cross_site)])
async def update_chat(
  body: ChatUpdate,
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Updates a chat's title and/or messages.

  A transcript replacement (`messages` present) MUST route through the
  writer actor's `ReplaceTranscript` — the actor is the sole runtime
  mutator of `messages`, and ReplaceTranscript broad-fences every
  in-flight streaming snapshot for the chat so a concurrent turn's save
  can't clobber the replacement (or vice versa). This holds regardless
  of caller (the agent editing its own transcript, a recovery flow, a
  direct API client).

  A title-only PUT (no `messages`) dirties only the title column — an
  allowed direct write (see the design's "can stay direct" list); it
  stays on this request's session so it does NOT broad-fence and wipe an
  in-flight streaming snapshot the way ReplaceTranscript intentionally
  does for a full replace.
  """
  from app.chat_writer import ReplaceTranscript, await_ack, get_writer

  get_active_chat_or_404(db, chat_id)
  if body.messages is not None:
    ack = get_writer().submit(
      ReplaceTranscript(
        chat_id=chat_id,
        run_token="",
        messages=body.messages,
        title=body.title,  # None leaves the title unchanged
      )
    )
    await await_ack(ack)
    return {"ok": True}

  # Title-only update — direct write (no transcript mutation).
  chat = get_active_chat_or_404(db, chat_id)
  if body.title is not None:
    chat.title = body.title
  # Touch updated_at (generic modified time). Ordering is driven by
  # activity_at (owner-send only), so a rename does not reorder the drawer.
  chat.updated_at = datetime.now(UTC)
  db.commit()
  return {"ok": True}


def _first_message_title(chat) -> str:
  """The 'first message' fallback name: the first user message's text trimmed
  to a sane length (mirrors the StartTurn initial-title behavior)."""
  for m in (chat.messages or []):
    if not isinstance(m, dict) or m.get("role") != "user":
      continue
    c = m.get("content")
    if isinstance(c, list):
      c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    c = (c or "").strip()
    if c:
      return c[:80]
  return ""


@router.patch("/{chat_id}", dependencies=[Depends(reject_cross_site)])
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

    # Naming precedence (user > agent > first-message). A clear resets the name;
    # a manual rename locks it; an agent by_agent sync only fills the name when
    # it isn't locked, so it can never clobber a name the owner chose.
    if body.clear_title:
      chat.title = _first_message_title(chat) or "New chat"
      chat.title_locked = False
    elif body.title is not None:
      new_title = body.title.strip()
      if new_title:
        if body.by_agent:
          if not chat.title_locked:
            chat.title = new_title
        else:
          chat.title = new_title
          chat.title_locked = True

    # Drawer pin toggle. We stamp the time on pin so the pinned group
    # sorts newest-pinned-first within itself.
    if body.pinned is not None:
      chat.pinned_at = now_naive_utc() if body.pinned else None

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

    # Capture the provider BEFORE any mutation so provider_switch logs the
    # real transition, and only when it actually changes (see after the commit).
    prev_provider = chat.provider
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
    # Record a real provider switch (Claude <-> Codex) once, after this first
    # commit — NOT after the owner-provider mirror commit below, which would
    # double-log. Model/effort tweaks within a provider are deliberately not
    # logged here (high-frequency picker noise the digest doesn't need).
    if target_provider is not None and chat.provider != prev_provider:
      activity.log_event(
        "provider_switch",
        chat_id=chat.id,
        provider=chat.provider,
        from_provider=prev_provider,
      )
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


def _truncate_large_tool_outputs(messages: list) -> list:
  """LEGACY-ROW reducer (contract rule 6). New turns are reduced at the event
  funnel (``chat.py`` ``_ChatEventSink._reduce_tool_output`` -> ``excerpt_tool_output``),
  so their persisted blocks already carry a bounded head+tail excerpt plus
  ``output_truncated`` / ``output_full_len`` / ``tool_use_id`` and the full text
  is stashed in ``tool_outputs``. This only trims the FEW pre-migration
  transcripts whose tool blocks still hold the full output inline, so loading
  such a chat does not ship a 2000-line Read for blocks the user never expands.

  A block already marked ``output_truncated`` (a new-funnel excerpt) is left
  UNTOUCHED. Legacy blocks (no ``tool_use_id``, full output still in
  ``Chat.messages``) are reduced to the same head+tail excerpt and the client
  fetches the full text via the legacy ``?ts=&i=`` endpoint; new blocks carry a
  ``tool_use_id`` and use the ``/tool-output/{tool_use_id}`` endpoint instead
  (dual-read). Returns a copy — never mutates the stored message dicts."""
  out = []
  for m in messages:
    blocks = m.get("blocks") if isinstance(m, dict) else None
    # A message with no ``ts`` (legacy pre-stamping rows) can't be located by
    # the ``/tool-output?ts=`` fetch route, so truncating it would strand the
    # block on a permanently-unfetchable "expand to load". Keep those inline.
    if (not isinstance(blocks, list) or m.get("role") != "assistant"
        or not m.get("ts")):
      out.append(m)
      continue
    new_blocks = None
    for i, blk in enumerate(blocks):
      if not isinstance(blk, dict) or blk.get("type") != "tool":
        continue
      # Already reduced at the funnel — leave the excerpt inline; the client
      # fetches the full text by tool_use_id.
      if blk.get("output_truncated"):
        continue
      output = blk.get("output")
      if (not isinstance(output, str)
          or len(output) <= _TOOL_OUTPUT_INLINE_THRESHOLD):
        continue
      excerpt, full_len, exit_code = excerpt_tool_output(output)
      if new_blocks is None:
        new_blocks = list(blocks)
      reduced = {
        **blk,
        "output": excerpt,
        "output_truncated": True,
        "output_full_len": full_len,
      }
      if exit_code is not None:
        reduced["output_exit_code"] = exit_code
      # This block's full text lives INLINE in Chat.messages, not the
      # `tool_outputs` side table (the sink already reduced + stashed every
      # tagged block, so anything reaching here is un-stashed). Drop any stray
      # tool_use_id so the frontend fetches the full via the legacy ?ts=&i=
      # endpoint (this reducer's contract) instead of the by-id endpoint, which
      # would 404 with no stash row.
      reduced.pop("tool_use_id", None)
      new_blocks[i] = reduced
    out.append({**m, "blocks": new_blocks} if new_blocks is not None else m)
  return out


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
  pending_question = questions.get(chat_id)
  return {
    "id": chat.id,
    "title": chat.title,
    "messages": _truncate_large_tool_outputs(page),
    "pending_messages": list(chat.pending_messages or []),
    "total": total,
    "offset": start,
    "running": is_chat_running(chat_id),
    "pending_question_id": (
      pending_question.question_id if pending_question is not None else None
    ),
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


@router.get("/{chat_id}/tool-output/{tool_use_id}", response_class=PlainTextResponse)
def get_tool_output_by_id(
  chat_id: str,
  tool_use_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> PlainTextResponse:
  """Returns the FULL text of a large tool block, fetched lazily on expand
  (contract rule 6). New blocks ship only a bounded excerpt in the chat-load
  payload and the live stream; the full output is stashed in ``tool_outputs``
  keyed by the tool's stable id. A 404 (a dropped/absent stash) tells the client
  to keep showing the inline excerpt.

  This is the NEW path for blocks that carry a ``tool_use_id``. Legacy blocks
  (no id, full output still inline in ``Chat.messages``) use the ``?ts=&i=``
  sibling endpoint below instead — the frontend branches on ``tool_use_id``
  (dual-read migration, no backfill)."""
  get_active_chat_or_404(db, chat_id)
  row = db.query(models.ToolOutput).filter(
    models.ToolOutput.chat_id == chat_id,
    models.ToolOutput.tool_use_id == tool_use_id,
  ).first()
  if row is None:
    raise HTTPException(status_code=404, detail="tool output not found")
  return PlainTextResponse(row.output or "")


@router.get("/{chat_id}/tool-output", response_class=PlainTextResponse)
def get_tool_output(
  chat_id: str,
  ts: int,
  i: int,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> PlainTextResponse:
  """LEGACY lazy fetch for a tool block whose full output still lives inline in
  ``Chat.messages`` (pre-contract-rule-6 rows, or any block with no
  ``tool_use_id``). The block is located by its message ``ts`` plus its index
  ``i`` within that message's blocks. Kept as the dual-read fallback; new blocks
  carry a ``tool_use_id`` and use ``/tool-output/{tool_use_id}`` instead."""
  chat = get_active_chat_or_404(db, chat_id)
  for m in (chat.messages or []):
    if m.get("role") != "assistant" or m.get("ts") != ts:
      continue
    blocks = m.get("blocks") or []
    if 0 <= i < len(blocks) and blocks[i].get("type") == "tool":
      return PlainTextResponse(blocks[i].get("output") or "")
    break
  raise HTTPException(status_code=404, detail="tool output not found")


@router.get("/{chat_id}/agent-context")
def get_chat_agent_context(
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns the assembled agent context for a chat — read-only observability.

  Exposes exactly what the agent is told, reconstructed the way run_chat
  assembles it but WITHOUT running a turn: the static system prompt (the
  core.md/skill constitution, or this chat's custom override) plus the
  first-turn injected blocks — the knowledge-graph memory block, the embedded
  <app_context>, the <app_report> brief, and any compaction summary. Lets the
  owner answer "what does the agent actually know here?", most useful for
  embedded-app chats (Latex/News/Reflection) where the app context + report
  data travel in the prompt rather than the visible message. Owner-only; the
  prompt holds instructions + memory, no secrets, but it is still the owner's
  instance. All the underlying builders are pure/read-only.
  """
  from app import memory
  from app.chat import (
    _build_app_context,
    _build_app_report_block,
    _chat_settings_dict,
    _custom_system_prompt,
    _latest_compaction_brief,
    _read_skill_text,
  )

  chat = get_active_chat_or_404(db, chat_id)
  data_dir = get_settings().data_dir
  overrides = _chat_settings_dict(chat)
  custom = _custom_system_prompt(overrides)
  system_prompt = custom or _read_skill_text()
  app_context_block, _env = _build_app_context(db, chat_id, data_dir)
  app_report_block = _build_app_report_block(db, chat_id, data_dir)
  compaction_brief = _latest_compaction_brief(chat)
  memory_block = memory.build_memory_block(data_dir).text or None
  return {
    "system_prompt": system_prompt,
    "system_prompt_source": "custom" if custom else "skill",
    "memory_block": memory_block,
    "app_context": app_context_block,
    "app_report": app_report_block,
    "compaction_brief": compaction_brief,
  }


@router.delete(
  "/{chat_id}", status_code=204, dependencies=[Depends(reject_cross_site)],
)
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
      stopped, _, _ = await stop_chat_for(chat_id, db=db)
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
    chat.deleted_at = now_naive_utc()
    db.commit()
  # Flag the chat soft-deleted in the registry (NOT forget_chat, which resets
  # the generation counter to a reusable 0). mark_chat_deleted preserves the
  # finite counter and makes `current_run_generation` return +inf, so a run
  # holding a pre-delete generation — including run_gen=0 on a brand-new chat,
  # the delete-ABA case — reads `we_own_gen=False` and skips finalizing onto
  # the soft-deleted row. recover_chat restores it with a strictly-newer gen.
  questions.cancel(chat_id)
  mark_chat_deleted(chat_id)
  # Close the chat's durable run state as part of the delete. A delete with a
  # LIVE handle stops the runner but does NOT clear the marker (that is handed
  # to run_chat's finally), and the dying run then bows out STALE_NO_ACTION on
  # the +inf generation — so without this the soft-deleted chat keeps a stale
  # run_status=="running" AND a "running" chat_runs record until the next boot
  # sweep. A tokenless ClearRunStatus closes every running run record + clears
  # run_status + drops the actor's _run_token_owner entry; it is best-effort
  # and safe on a soft-deleted row (clear commands don't resurrect), and
  # idempotent when the run state is already clean (the common idle delete).
  await _clear_run_status(chat_id)


@router.post("/{chat_id}/recover", dependencies=[Depends(reject_cross_site)])
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
  if (now_naive_utc() - chat.deleted_at) >= SOFT_DELETE_TTL:
    raise HTTPException(status_code=410, detail="Recovery window has expired.")
  chat.deleted_at = None
  db.commit()
  # Clear the registry's deleted flag and bump to a generation newer than every
  # pre-delete run, so a resurrected stale run can't reclaim the recovered chat.
  recover_chat_generation(chat_id)
  return {"ok": True}


@router.post(
  "/{chat_id}/compact", dependencies=[Depends(reject_cross_site)],
)
async def compact_chat(
  chat_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Compacts the chat into a portable plain-text briefing block.

  Feature 091's provider-switch groundwork: switching the chat's PROVIDER
  loses the session (sessions aren't cross-provider portable), so this runs
  a one-shot summarize turn over the current transcript and stores the
  result as a recognizable `kind="compaction"` assistant message via the
  writer actor. It does NOT switch provider here — the frontend owns the
  confirmation and follow-up provider/model PATCH. This endpoint only
  produces + stores + returns the summary so the client can display it.

  A failed summarize (empty chat, disconnected provider, no text produced)
  returns a non-2xx and stores NOTHING — a failed compaction must never
  silently drop the user's context. The route through the actor (rather
  than a direct `chat.messages` write) keeps the single-writer invariant:
  the compaction block can't clobber, or be clobbered by, a streaming
  snapshot for the same chat.
  """
  from app.chat_writer import (
    PersistCompaction, alloc_run_token, await_ack, get_writer,
  )
  from app.compaction import CompactionError, summarize_chat

  chat = get_active_chat_or_404(db, chat_id)
  if is_chat_running(chat_id):
    # A live turn's streaming snapshots target the trailing assistant row;
    # appending a compaction block mid-turn would race/clobber it. Compaction
    # is a between-turns operation (e.g. before a provider switch) — refuse
    # while a turn is active rather than risk the lost-update the docstring
    # promises against.
    raise HTTPException(
      status_code=409,
      detail="Chat is busy — finish or stop the current turn before compacting.",
    )
  messages = list(chat.messages or [])
  data_dir = get_settings().data_dir
  try:
    summary = await summarize_chat(messages, data_dir=data_dir)
  except CompactionError as exc:
    # The summarize step is the one allowed-to-fail step. Surface it as a
    # 422 so the client can show the reason and keep the chat unchanged
    # (no block stored, no provider switched).
    raise HTTPException(status_code=422, detail=str(exc))
  except Exception as exc:
    log.warning("compaction summarize failed for chat %s: %s", chat_id, exc)
    raise HTTPException(
      status_code=502, detail="The summarize turn failed; not compacting."
    )

  ack = get_writer().submit(
    PersistCompaction(
      chat_id=chat_id, run_token=alloc_run_token(), summary=summary
    )
  )
  try:
    result = await await_ack(ack)
  except Exception:
    raise HTTPException(
      status_code=503, detail="Could not store the compaction; try again."
    )
  command = f"POST /api/chats/{chat_id}/compact"
  return {
    "ok": True,
    "summary": summary,
    "command": command,
    "stored": result.get("stored"),
  }


# An app that opens a chat ABOUT one of its dated reports passes the report's
# date here; chat.py reads it back from agent_settings_json on the first turn
# and injects the stripped brief as context. Validated strictly as an ISO
# calendar date at the boundary because it becomes a path component
# downstream (data/apps/<id>/reports/<date>.html).
_REPORT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A project_id is used directly as a storage path component, so it must be a
# safe slug — alphanumerics, dash, underscore only; no separators or traversal.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CHAT_SCOPE_MAX = 160
_CHAT_SCOPE_LABEL_MAX = 120


def _clean_app_chat_text(value: str | None, max_len: int, field: str) -> str | None:
  if value is None:
    return None
  value = value.strip()
  if not value:
    return None
  if len(value) > max_len:
    raise ValueError(f"{field} must be <= {max_len} chars")
  if any(ord(ch) < 32 for ch in value):
    raise ValueError(f"{field} must not contain control characters")
  return value


class AppChatCreate(BaseModel):
  title: str | None = None
  system_prompt: str | None = Field(default=None, max_length=20000)
  model: str | None = Field(default=None, max_length=256)
  provider: str | None = None
  report_date: str | None = None
  report_kind: str | None = Field(default=None, max_length=64)
  project_id: str | None = Field(default=None, max_length=64)
  scope: str | None = Field(default=None, max_length=_CHAT_SCOPE_MAX)
  scope_label: str | None = Field(default=None, max_length=_CHAT_SCOPE_LABEL_MAX)
  owner_visible: bool = False

  @field_validator("project_id")
  @classmethod
  def _validate_project_id(cls, value: str | None) -> str | None:
    if value is None:
      return None
    value = value.strip()
    if not value:
      return None
    if not _PROJECT_ID_RE.match(value):
      raise ValueError("project_id must be a slug ([A-Za-z0-9_-], <=64 chars)")
    return value

  @field_validator("report_date")
  @classmethod
  def _validate_report_date(cls, value: str | None) -> str | None:
    if value is None:
      return None
    value = value.strip()
    if not value:
      return None
    if not _REPORT_DATE_RE.match(value):
      raise ValueError("report_date must be an ISO date (YYYY-MM-DD)")
    return value

  @field_validator("scope")
  @classmethod
  def _validate_scope(cls, value: str | None) -> str | None:
    return _clean_app_chat_text(value, _CHAT_SCOPE_MAX, "scope")

  @field_validator("scope_label")
  @classmethod
  def _validate_scope_label(cls, value: str | None) -> str | None:
    return _clean_app_chat_text(
      value, _CHAT_SCOPE_LABEL_MAX, "scope_label",
    )


class AppChatPatch(BaseModel):
  system_prompt: str | None = Field(default=None, max_length=20000)
  model: str | None = Field(default=None, max_length=256)
  provider: str | None = None
  scope: str | None = Field(default=None, max_length=_CHAT_SCOPE_MAX)
  scope_label: str | None = Field(default=None, max_length=_CHAT_SCOPE_LABEL_MAX)

  @field_validator("scope")
  @classmethod
  def _validate_scope(cls, value: str | None) -> str | None:
    return _clean_app_chat_text(value, _CHAT_SCOPE_MAX, "scope")

  @field_validator("scope_label")
  @classmethod
  def _validate_scope_label(cls, value: str | None) -> str | None:
    return _clean_app_chat_text(
      value, _CHAT_SCOPE_LABEL_MAX, "scope_label",
    )


def _merge_app_chat_settings(
  chat: models.Chat,
  *,
  system_prompt: str | None = None,
  model: str | None = None,
  report_date: str | None = None,
  report_kind: str | None = None,
  project_id: str | None = None,
  scope: str | None = None,
  scope_label: str | None = None,
  owner_visible: bool | None = None,
) -> None:
  """Merge app-supplied runtime metadata into Chat.agent_settings_json."""
  from sqlalchemy.orm.attributes import flag_modified

  settings = _coerce_agent_settings(chat.agent_settings_json)
  if system_prompt is not None:
    value = system_prompt.strip()
    if value:
      settings["system_prompt"] = value
    else:
      settings.pop("system_prompt", None)
  if model is not None:
    value = model.strip()
    if value:
      settings["model"] = value
    else:
      settings.pop("model", None)
  # report_date is already ISO-validated by AppChatCreate; chat.py reads it
  # on the first turn to inject the brief this chat is about. report_kind is
  # a free-form tag (e.g. "reflection") that travels alongside it.
  if report_date is not None:
    value = report_date.strip()
    if value:
      settings["report_date"] = value
    else:
      settings.pop("report_date", None)
  if report_kind is not None:
    value = report_kind.strip()
    if value:
      settings["report_kind"] = value
    else:
      settings.pop("report_kind", None)
  # project_id (already slug-validated by AppChatCreate) scopes an embedded
  # app chat to ONE of the app's projects: chat.py reads it to point the
  # injected <app_context> at projects/<project_id>/ instead of the app root.
  if project_id is not None:
    value = project_id.strip()
    if value:
      settings["project_id"] = value
    else:
      settings.pop("project_id", None)
  # Scoped embedded chats let one app host multiple durable conversations
  # grouped by its own domain object, such as one chat per workout session.
  if scope is not None:
    value = scope.strip()
    if value:
      settings["chat_scope"] = value
  if scope_label is not None:
    value = scope_label.strip()
    if value:
      settings["chat_scope_label"] = value
  if owner_visible is not None:
    if owner_visible:
      settings["owner_visible"] = True
    else:
      settings.pop("owner_visible", None)
  chat.agent_settings_json = settings or None
  if settings:
    flag_modified(chat, "agent_settings_json")


def _has_real_assistant_turn(chat: models.Chat) -> bool:
  return any(
    isinstance(m, dict)
    and m.get("role") == "assistant"
    and m.get("kind") != "compaction"
    for m in (chat.messages or [])
  )


def _app_chat_scope(chat: models.Chat) -> str | None:
  value = _coerce_agent_settings(chat.agent_settings_json).get("chat_scope")
  return value.strip() if isinstance(value, str) and value.strip() else None


def _app_chat_scope_label(chat: models.Chat) -> str | None:
  value = _coerce_agent_settings(chat.agent_settings_json).get("chat_scope_label")
  return value.strip() if isinstance(value, str) and value.strip() else None


def _app_chat_sort_ts(chat: models.Chat) -> float:
  ts = chat.activity_at or chat.updated_at or chat.created_at
  if ts is None:
    return 0
  if ts.tzinfo is None:
    ts = ts.replace(tzinfo=UTC)
  return ts.timestamp()


def _app_chat_summary(chat: models.Chat) -> dict:
  return {
    "id": chat.id,
    "title": chat.title,
    "created_by_app_id": chat.created_by_app_id,
    "created_at": chat.created_at.isoformat() if chat.created_at else None,
    "updated_at": chat.updated_at.isoformat() if chat.updated_at else None,
    "activity_at": chat.activity_at.isoformat() if chat.activity_at else None,
    "has_messages": bool(chat.messages and len(chat.messages) > 0),
    "provider": chat.provider or "claude",
    "scope": _app_chat_scope(chat),
    "scope_label": _app_chat_scope_label(chat),
  }


@app_chat_router.get("")
def list_app_chats(
  scope: str | None = None,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Lists active app-owned chats for the calling app token.

  `scope` is an app-defined grouping key for embedded UX (for example, a
  workout session id). Owner tokens stay on `/api/chats`; this endpoint is the
  app's private chat index and never returns another app's rows.
  """
  if principal.app_id is None:
    raise HTTPException(
      status_code=403,
      detail="App chats may only be listed by an app token.",
    )
  try:
    clean_scope = _clean_app_chat_text(scope, _CHAT_SCOPE_MAX, "scope")
  except ValueError as exc:
    raise HTTPException(status_code=422, detail=str(exc))
  chats = db.query(models.Chat).filter(
    models.Chat.deleted_at.is_(None),
    models.Chat.created_by_app_id == principal.app_id,
  ).all()
  if clean_scope is not None:
    chats = [c for c in chats if _app_chat_scope(c) == clean_scope]
  chats.sort(key=_app_chat_sort_ts, reverse=True)
  return [_app_chat_summary(c) for c in chats]


@app_chat_router.post(
  "", status_code=201, dependencies=[Depends(reject_cross_site)],
)
def create_app_chat(
  body: AppChatCreate,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Creates a chat owned by the calling app (app-attributed contract).

  App-token-only: the new chat is stamped with `created_by_app_id =
  principal.app_id`, so only that app's token (and the owner) may send
  to it or stream it (see `get_active_chat_for_principal`). The chat is
  hidden from the owner's drawer history (`GET /api/chats` excludes
  `created_by_app_id` rows unless `include_app_chats=1`), so an app's own
  conversations don't clutter the chat list; the owner can still open one
  directly by id, and the reflection agent reads them via the opt-in. This is
  the surface that unblocks an in-iframe app's chat panel, which the default
  `/api/chats` list intentionally omits.

  Owner tokens are rejected here on purpose: the owner's create path is
  `POST /api/chats`, which leaves `created_by_app_id` NULL. Allowing the
  owner through this endpoint would just produce an unattributed chat by
  a second route — needless ambiguity. One path per actor.
  """
  if principal.app_id is None:
    raise HTTPException(
      status_code=403,
      detail="Use POST /api/chats for owner-created chats.",
    )
  import uuid

  owner = db.query(models.Owner).first()
  data_dir = get_settings().data_dir
  provider = body.provider or providers.resolve_default_provider(
    data_dir, owner.provider if owner else None,
  )
  if provider not in ("claude", "codex"):
    raise HTTPException(status_code=422, detail=f"unknown provider: {provider}")

  chat = models.Chat(
    id=str(uuid.uuid4()),
    title=body.title or "New chat",
    messages=[],
    provider=provider,
    agent_settings_json=None,
    created_by_app_id=principal.app_id,
  )
  _merge_app_chat_settings(
    chat,
    system_prompt=body.system_prompt,
    model=body.model,
    report_date=body.report_date,
    report_kind=body.report_kind,
    project_id=body.project_id,
    scope=body.scope,
    scope_label=body.scope_label,
    owner_visible=body.owner_visible,
  )
  db.add(chat)
  db.commit()
  db.refresh(chat)
  return {
    "id": chat.id,
    "title": chat.title,
    "created_by_app_id": chat.created_by_app_id,
  }


@app_chat_router.patch(
  "/{chat_id}", dependencies=[Depends(reject_cross_site)],
)
def patch_app_chat(
  chat_id: str,
  body: AppChatPatch,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Updates runtime metadata for a chat owned by the calling app.

  This lets an embedded app re-assert its custom system prompt for an
  already-created saved chat, instead of relying on a one-time create body.
  """
  if principal.app_id is None:
    raise HTTPException(
      status_code=403,
      detail="App chat metadata may only be changed by an app token.",
    )
  chat = get_active_chat_for_principal(db, chat_id, principal)
  if body.provider is not None:
    if body.provider not in ("claude", "codex"):
      raise HTTPException(
        status_code=422, detail=f"unknown provider: {body.provider}"
      )
    if chat.provider != body.provider:
      if _has_real_assistant_turn(chat):
        raise HTTPException(
          status_code=409,
          detail=(
            "Cannot switch provider for an app chat after it has assistant "
            "turns. Create a new app chat instead."
          ),
        )
      chat.provider = body.provider
      chat.session_id = None
  _merge_app_chat_settings(
    chat,
    system_prompt=body.system_prompt,
    model=body.model,
    scope=body.scope,
    scope_label=body.scope_label,
  )
  chat.updated_at = datetime.now(UTC)
  db.commit()
  db.refresh(chat)
  return {
    "ok": True,
    "id": chat.id,
    "provider": chat.provider or "claude",
    "agent_settings_json": _coerce_agent_settings(chat.agent_settings_json) or None,
  }


class QuestionAnswers(BaseModel):
  answers: dict
  # Optional identity of the question being answered (the runner-
  # published PendingQuestion id). When supplied, the matching block is
  # located by this exact id rather than "the latest question block" —
  # fixing the wrong-block bug when two questions are open. Optional so
  # older clients keep working via the latest-question fallback.
  question_id: str | None = None


@router.post(
  "/{chat_id}/question-answers",
  dependencies=[Depends(reject_cross_site)],
)
async def save_question_answers(
  chat_id: str,
  body: QuestionAnswers,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Saves the user's answers into the question block being answered.

  Legacy path (no live SDK turn waiting): routes the merge through the
  writer actor's `AnswerQuestion` with NO run_token — a tokenless answer
  broad-fences EVERY pending streaming snapshot for the chat (the
  exact-key fence can't reach a snapshot under the live streaming token),
  so a stale snapshot can't clobber the answer after it commits. Prefers
  an exact `question_id` match when supplied (precise routing with two
  open questions); falls back to the LAST assistant message's last
  question block when absent (unchanged behaviour). The actor raises when
  no matching block exists, which maps to the route's 404 contract.
  """
  from app.chat_writer import AnswerQuestion, await_ack, get_writer

  get_active_chat_or_404(db, chat_id)
  ack = get_writer().submit(
    AnswerQuestion(
      chat_id=chat_id,
      run_token="",  # tokenless → broad-fence by chat
      question_id=body.question_id,
      answers=body.answers,
    )
  )
  try:
    await await_ack(ack)
  except Exception:
    # No matching question block (or the write dropped). Preserve the
    # route's 404 contract — the client treats it as "the question card
    # is no longer addressable".
    raise HTTPException(status_code=404, detail="No question block found.")
  return {"ok": True}
