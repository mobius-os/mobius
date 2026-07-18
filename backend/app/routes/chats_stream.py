"""Routes for agent message sending and SSE streaming."""

import asyncio
import json
import logging
import time
from pathlib import Path as FilePath

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response
from sqlalchemy.orm import Session

from app import activity, models, questions, schemas
from app.broadcast import create_broadcast, get_broadcast, get_system_broadcast
from app.chat import (
  _schedule_continuation,
  discard_starting,
  get_active_sink,
  is_chat_running,
  is_draining,
  mark_starting,
  run_chat,
)
from app import chat_queue
from app.chat_writer import (
  AnswerQuestion,
  AppendPending,
  AppendSteeredUserMessage,
  CancelPending,
  StartTurn,
  alloc_run_token,
  await_ack,
  cid_of,
  ensure_user_cid,
  get_writer,
)
from app import claude_sdk_runner, codex_sdk_runner
from app.providers import _load_agent_settings, resolve_default_provider
from app.runner_registry import RunnerKind, registry
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, get_chat_view_principal, get_owner_or_chat_embed_principal,
  get_current_owner, reject_cross_site,
  chat_embed_session_is_active, require_chat_embed_operation,
)
from app.resource_access import (
  get_active_chat_for_principal, get_active_chat_or_404,
)

router = APIRouter(prefix="/api/chats", tags=["chats"])

log = logging.getLogger(__name__)

# Keepalive interval for the SSE stream to prevent proxy timeouts.
_KEEPALIVE_INTERVAL = 30  # seconds


def _message_persist_unavailable(exc: Exception, *, chat_id: str) -> HTTPException:
  """Normalize writer failures at the user-send boundary.

  The writer is the durability boundary for both fresh and queued messages. If
  it is dead or cannot acquire a DB connection, the request is retryable service
  unavailability—not an opaque route-specific 500.
  """
  log.warning("message persistence unavailable chat_id=%s: %r", chat_id, exc)
  return HTTPException(
    status_code=503,
    detail="Could not save your message; please try again.",
  )


def _safe_upload_path(path_str: str, data_dir: str) -> str | None:
  """Returns the resolved path if it lives within data_dir, None otherwise.

  Validates upload paths from the DB before passing them to the CLI so a
  tampered record cannot redirect the agent to read credentials or other
  sensitive files outside /data/.
  """
  try:
    p = FilePath(path_str).resolve()
    allowed = FilePath(data_dir).resolve()
    if not str(p).startswith(str(allowed) + '/'):
      logging.getLogger(__name__).warning(
        "Upload path outside data dir, skipping: %s", path_str
      )
      return None
    return str(p)
  except Exception:
    return None


def _sse(data: dict) -> str:
  """Formats a dict as a Server-Sent Events data line."""
  return f"data: {json.dumps(data)}\n\n"


def _content_with_uploads(chat: models.Chat, body: schemas.SendMessage) -> str:
  """Returns message content with the session upload notice appended."""
  settings = get_settings()
  content = body.content
  # Force-steer resends the exact canonical pending-message content. Pending
  # rows already include this hidden upload manifest; appending it again makes
  # steered multi-message turns look duplicated/newline-heavy in the client and
  # changes what the provider sees. If the body already carries the manifest,
  # leave it as-is.
  if "[Files in this session:" in content:
    return content
  if chat.uploads:
    safe_entries = []
    for f in chat.uploads:
      safe = _safe_upload_path(f['path'], settings.data_dir)
      if safe is not None:
        safe_entries.append(
          f"- {f['name']} → {safe}"
          f" ({f.get('mime_type', 'unknown')}, {round(f['size'] / 1024)} KB)"
        )
    if safe_entries:
      lines = "\n".join(safe_entries)
      content += f"\n\n[Files in this session:\n{lines}]"
  return content


async def _append_to_pending(
  chat: models.Chat, body: schemas.SendMessage, db: Session,
  *, initiated_by_app_id: int | None = None,
  front: bool = False,
  require_answer_match: bool = False,
) -> dict:
  """Queue a message via the actor's AppendPending; return the stored dict.

  The JSON-blob RMW (collision-free ts, append, optional answer merge,
  commit) is the actor's `AppendPending` command — the SOLE runtime
  mutator of `pending_messages`, so concurrent POST/DELETE/promote can't
  lost-update. The per-chat queue lock is still held by the caller around
  the compound queue decision; the actor never acquires it.

  AskUserQuestion answers ride along: when the body carries `answers`,
  AppendPending merges them into the matching question block in the SAME
  commit as the append, so the answer + the queued message land
  atomically (the race that used to leave answers missing on a mid-stream
  remount).

  The append is keyed on an empty run_token: a queued message isn't a
  streaming turn, so it has no snapshot key of its own to fence.
  """
  ack = get_writer().submit(
    AppendPending(
      chat_id=chat.id,
      run_token="",
      user_msg=_user_message_from_body(chat, body),
      answers=body.answers,
      question_id=body.question_id,
      initiated_by_app_id=initiated_by_app_id,
      front=front,
      require_answer_match=require_answer_match,
    )
  )
  try:
    result = await await_ack(ack)
  except Exception as exc:
    raise _message_persist_unavailable(exc, chat_id=chat.id) from exc
  # Reflect the committed state on the request's session so a later
  # `db.refresh(chat)` in this handler sees the actor's write.
  db.expire(chat)
  return result["stored"]


def _answer_delivered_response(chat_id: str) -> JSONResponse:
  """202 for an AskUserQuestion answer that was delivered in-process
  to a blocked SDK PreToolUse hook. The SDK resumes the active turn
  with the answer; no new turn is queued."""
  return JSONResponse(
    status_code=202,
    content={
      "status": "answer_delivered",
      "answer_turn": "same",
      "chat_id": chat_id,
    },
  )


def _has_unanswered_question(
  chat: models.Chat,
  question_id: str | None,
) -> bool:
  """Whether the durable tail prompt is the question being answered."""
  msgs = list(chat.messages or [])
  tail_questions: list[dict] = []
  for msg in reversed(msgs):
    if msg.get("hidden"):
      continue
    if msg.get("role") != "assistant":
      return False
    blocks = list(msg.get("blocks") or [])
    while blocks:
      block = blocks.pop()
      if block.get("type") != "question" or block.get("answers"):
        break
      tail_questions.append(block)
    break

  if not tail_questions:
    return False
  if question_id:
    return any(
      block.get("question_id") == question_id for block in tail_questions
    )
  return True


def _queued_response(
  new_msg: dict,
  position: int,
  *,
  started: bool = False,
  message: dict | None = None,
) -> JSONResponse:
  """Standard 202 response for a queued message."""
  payload = {
    "status": "queued",
    "position": position,
    "ts": new_msg["ts"],
    # Echo the canonical persisted row back to the client. The frontend's
    # optimistic queue row is built from the visible composer text, while
    # _user_message_from_body appends hidden session/upload context before
    # storing it. Force-steer compares against the server's canonical pending
    # content, so the client must swap to this exact row as soon as the queue
    # POST acks; waiting for a remount/hydrate leaves fast-forward rejected.
    "pending_message": new_msg,
  }
  if started:
    payload["started"] = True
  if message is not None:
    payload["message"] = message
  return JSONResponse(
    status_code=202,
    content=payload,
  )


def _duplicate_send_response(
  chat_id: str, chat: models.Chat, cid: str | None,
) -> JSONResponse | None:
  """Acknowledge a durable cid before any provider-side send effect.

  The transition lock makes this the ordinary retry gate. Actor-level cid
  checks remain as the persistence backstop for future callers and cross-
  process races, but steering must also be stopped here: de-duplicating only
  after injecting text into a live provider would be too late.
  """
  if not cid:
    return None
  pending = list(chat.pending_messages or [])
  for position, row in enumerate(pending, start=1):
    if cid_of(row) == cid:
      if is_chat_running(chat_id):
        return _queued_response(row, position)
      # Preserve the existing stale-queue self-heal: the normal queue branch
      # will idempotently find this cid via AppendPending, then promote the
      # idle queue into exactly one run. A preflight acknowledgement here
      # would leave durable work parked until some later user action.
      return None
  for row in list(chat.messages or []):
    if row.get("role") == "user" and cid_of(row) == cid:
      return JSONResponse(
        status_code=200,
        content={
          "status": "duplicate",
          "message": row,
          # A retry can race a later turn. The client must not tear down that
          # unrelated live stream while reconciling this durable message.
          "running": is_chat_running(chat_id),
        },
      )
  return None


def _user_message_from_body(
  chat: models.Chat,
  body: schemas.SendMessage,
) -> dict:
  """Builds the durable user message payload for a send request."""
  user_msg = {
    "role": "user",
    "content": _content_with_uploads(chat, body),
    "ts": int(time.time() * 1000),
  }
  # Carry the client-minted identity when present; API clients may omit it, so
  # stamp an opaque server identity before the row reaches any queue/transcript
  # command. The writer repeats this invariant for non-HTTP producers.
  if body.cid:
    user_msg["cid"] = body.cid
  ensure_user_cid(user_msg)
  if body.hidden:
    user_msg["hidden"] = True
  if body.attachments:
    user_msg["attachments"] = body.attachments
  if body.timezone:
    user_msg["timezone"] = body.timezone
  if body.viewport:
    user_msg["viewport"] = body.viewport
  return user_msg


# The answer-merge logic lives in `chat_writer.apply_answers_to_last_
# question` and is no longer called from this route directly: C2 routes
# every answer write through the writer actor's `AnswerQuestion` command
# (the sole runtime mutator of `chat.messages`), and the queue append
# carries answers via `AppendPending`. The merge runs on the actor thread
# so it can't lost-update against a concurrent streaming snapshot.


def _steer_enabled(chat: models.Chat) -> bool:
  """Whether AUTO-steering is opted in for this chat.

  This is the "steer every mid-turn send" flag, DISTINCT from the fast-forward
  button (which sends `force_steer` and steers regardless of this flag). It
  DEFAULTS OFF — queuing is the default; a mid-turn send only auto-steers when
  `steer_enabled: true` is set globally (`/data/shared/agent-settings.json`) or
  on the chat.

  Read DIRECTLY from the settings file merged with the per-chat override, like
  `skills_enabled` — NOT through `effective_agent_settings`, whose model/effort
  allowlist silently DROPPED this flag so a global `steer_enabled` was never
  respected.
  """
  settings = get_settings()
  merged = dict(_load_agent_settings(settings.data_dir))
  raw = chat.agent_settings_json
  if isinstance(raw, str):
    try:
      raw = json.loads(raw)
    except (ValueError, TypeError):
      raw = None
  if isinstance(raw, dict):
    merged.update(raw)  # per-chat override wins over the global file
  return bool(merged.get("steer_enabled"))


def _has_live_steerable_turn(chat_id: str, provider: str) -> bool:
  """True when a steerable provider handle is registered for this chat.

  Codex exposes a true turn/steer primitive. Claude has no wire-level
  mid-turn inject, so its registered client steers by interrupting the
  live response and re-prompting on the same SDK client.
  """
  if provider == "claude":
    return isinstance(
      registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK),
      claude_sdk_runner.ActiveClaudeClient,
    )
  handle = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
  return (
    isinstance(handle, codex_sdk_runner.ActiveCodexTurn)
    and handle.turn is not None
  )


async def _steer_into_active_turn(
  provider: str,
  chat_id: str,
  content: str,
  user_msgs: list[dict] | None = None,
  consume_pending_cids: list[str] | None = None,
) -> bool:
  """Routes a steer request to the active provider-specific handle.

  For Claude the `user_msgs` / `consume_pending_cids` payload is buffered on
  the handle so the RUNNER performs the transcript split (seal A1, append the
  steered rows, reset for A2) when the interrupted turn ends — the first point
  the true A1/A2 boundary is known. Codex still splits at the route below
  (its `turn.steer()` injects into the same running turn, so there is no
  interrupt boundary to defer to).
  """
  if provider == "claude":
    return await claude_sdk_runner.steer_into_active_turn(
      chat_id, content, user_msgs, consume_pending_cids,
    )
  return await codex_sdk_runner.steer_into_active_turn(chat_id, content)


async def _split_steer_at_route(
  chat_id: str, user_msgs: list[dict], consume_pending_cids: list[str],
) -> dict:
  """Route-driven transcript split for Codex (seal A1, append steered rows).

  Claude defers the split to the runner (there is a real interrupt boundary
  where A1 is complete); Codex injects into the same running turn with no such
  boundary, so the route drives the split here on this event loop, serialized
  with the sink's streaming snapshots. A turn with no registered sink (a
  closed-turn race or a non-SDK path) has no streamed text to seal, so it
  falls back to a plain end-of-transcript append of the steered rows.
  """
  sink = get_active_sink(chat_id)
  if sink is not None:
    return await sink.split_for_steer(user_msgs, consume_pending_cids)
  ack = get_writer().submit(
    AppendSteeredUserMessage(
      chat_id=chat_id,
      run_token="",
      user_msgs=user_msgs,
      consume_pending_cids=consume_pending_cids,
    )
  )
  return await await_ack(ack)


def _steered_response(
  chat_id: str, pending_messages: list[dict] | None = None,
) -> JSONResponse:
  """202 for a message steered into the live turn.

  The message is in the TRANSCRIPT (not the pending queue) and the live
  turn already saw it via `steer()`, so the client renders it inline as
  content growth rather than a queued-tray entry."""
  payload = {"status": "steered", "chat_id": chat_id}
  if pending_messages is not None:
    payload["pending_messages"] = pending_messages
  return JSONResponse(status_code=202, content=payload)


def _not_steered_response(chat_id: str) -> JSONResponse:
  return JSONResponse(
    status_code=202,
    content={"status": "not_steered", "chat_id": chat_id},
  )


def _selected_force_steer_pending(
  chat: models.Chat,
  body: schemas.SendMessage,
) -> list[dict] | None:
  """Return selected pending rows in queue order if force-steer is valid.

  Force-steer is only for converting already-queued UI messages. The browser
  may send a newer `steered_messages` hint so it can render a batch as
  separate rows, but durable rows are reconstructed from the server-owned
  pending queue here; the client cannot forge transcript entries.

  Selection is keyed on the stable `cid` (matched via cid_of, which retains a
  legacy `legacy-<ts>` fallback for rows missed by migration). The old content
  byte-match is GONE — cid binds the request to specific queued rows directly,
  with no fragile "\n\n"-join contract against the composer text.
  """
  requested_cids = set(body.consume_pending_cids or [])
  if not requested_cids:
    return None
  selected = [
    m for m in list(chat.pending_messages or [])
    if cid_of(m) in requested_cids
  ]
  if len(selected) != len(requested_cids):
    return None
  return selected


def _force_steer_matches_pending(chat: models.Chat, body: schemas.SendMessage) -> bool:
  """Force-steer is only for converting already-queued UI messages."""
  return _selected_force_steer_pending(chat, body) is not None


def _user_messages_from_pending(
  selected_pending: list[dict],
  fallback_user_msg: dict,
) -> list[dict]:
  """Build durable transcript rows for a force-steered pending batch."""
  user_msgs: list[dict] = []
  for pending in selected_pending:
    msg = dict(pending)
    msg["role"] = "user"
    msg.pop("queued", None)
    msg.pop("serverTs", None)
    msg.pop("position", None)
    # Preserve the stable identity across the queue→transcript hop. A legacy
    # pending row without a cid gets its `legacy-<ts>` fallback stamped so
    # the steered transcript row and its echo carry the same value the client
    # will compare against.
    if not msg.get("cid"):
      derived = cid_of(pending)
      if derived is not None:
        msg["cid"] = derived
    user_msgs.append(msg)
  return user_msgs or [fallback_user_msg]


@router.post(
  "/{chat_id}/messages",
  status_code=202,
  dependencies=[Depends(reject_cross_site)],
)
async def send_message(
  body: schemas.SendMessage,
  chat_id: str,
  principal: Principal = Depends(get_chat_view_principal),
  db: Session = Depends(get_db),
):
  """Saves the user message, starts the agent as a background task,
  and returns 202 immediately.  The client streams via GET /stream.

  Owner tokens may send to any active chat. App tokens may send only to
  a chat they created (`created_by_app_id == app_id`) — the app-
  attributed contract (design §1). Foreign chats are 403; the runner /
  queue / SSE internals are reused unchanged for both actors.
  """
  require_chat_embed_operation(principal, "chat:send")
  chat = get_active_chat_for_principal(db, chat_id, principal)

  # AskUserQuestion answer delivery. If a live SDK turn is blocked waiting for
  # the answer (held in `questions._pending[chat_id]`), persist through the
  # writer actor, then resolve the future in-place and return — the SDK
  # continues the active turn. If the process restarted and only the durable
  # question block remains, save the answer and start a hidden continuation
  # below. The answer merge is NO LONGER written inline on this request's
  # session: the actor owns the JSON blob so the answer can't lost-update
  # against a concurrent streaming snapshot.
  #
  # Registration race: the frontend renders the card the instant the
  # `question` SSE event lands, but the runner registers the pending
  # entry in a separate task (Codex via `run_coroutine_threadsafe` from
  # the SDK worker thread; Claude's `can_use_tool` callback). A user who
  # answers in the tens-of-ms window before the entry lands used to hit
  # 410. We PEEK (not pop) with a short grace period; the `await sleep`
  # yields so the runner can write the entry. 500ms covers the race in
  # practice; after that, the durable-transcript fallback below decides
  # whether this is recoverable or genuinely stale.
  if body.answers:
    # Snapshot the Stop tombstone BEFORE waiting on the queue lock. A Stop
    # that lands after this request began must still win the race (410); a
    # Stop that had already completed is different: pressing Submit afterward
    # is a fresh, explicit request to continue from the durable tail question.
    # The old unconditional tombstone check made that later Submit impossible
    # until the whole server restarted and forgot the in-memory tombstone.
    cancelled_when_submitted = questions.was_cancelled(
      chat_id, body.question_id,
    )
    async with chat_queue.get_lock(chat_id):
      _GRACE_ATTEMPTS = 10
      _GRACE_INTERVAL = 0.05  # seconds — total ~500ms
      pending = questions.get(chat_id)
      for _ in range(_GRACE_ATTEMPTS):
        if pending is not None:
          break
        await asyncio.sleep(_GRACE_INTERVAL)
        pending = questions.get(chat_id)
      if pending is not None:
        if (
          body.question_id is not None
          and body.question_id != pending.question_id
        ):
          raise HTTPException(
            status_code=410,
            detail="The question is no longer accepting answers.",
          )
        # Persist the answer through the actor FIRST (commit-before-
        # resolve). Keyed on the turn's run_token so the actor fences the
        # right (chat_id, run_token) snapshot; a tokenless pending (no
        # run_token) submits an empty token, which broad-fences by chat.
        # The body question_id was checked against the live pending entry
        # above, so a stale card cannot resolve a newer question.
        ack = get_writer().submit(
          AnswerQuestion(
            chat_id=chat_id,
            run_token=pending.run_token or "",
            question_id=(body.question_id or pending.question_id),
            answers=body.answers,
          )
        )
        try:
          await await_ack(ack)
        except Exception as exc:
          # The answer write did NOT land (no matching block, dropped
          # commit, or a wedged writer past the timeout). Do NOT resolve
          # the future — the pending question stays registered so the
          # user can retry. 503 tells the client "try again".
          log.warning(
            "AnswerQuestion did not persist chat_id=%s: %s", chat_id, exc,
          )
          raise HTTPException(
            status_code=503,
            detail="Could not save your answer; please try again.",
          )
        # Stop-races-answer guard: a concurrent Stop may have cancelled
        # this question (popping the entry + cancelling the future) WHILE
        # we awaited the ack. Re-claim by identity before resolving — if
        # the registry no longer holds exactly this pending entry, Stop
        # (or a superseding question) took it; do NOT resolve a cancelled
        # / foreign future. The answer is durable regardless (the actor
        # committed it); 410 tells the client the card is no longer live.
        if not questions.claim_if(chat_id, pending):
          raise HTTPException(
            status_code=410,
            detail="The question is no longer accepting answers.",
          )
        if not pending.future.done():
          pending.future.set_result(body.answers)
        # Tell every connected client (and the catch-up replay) the question
        # is answered. Without this, an already-open stream — or any client
        # that reconnects mid-turn — never learns the answer: the live
        # AskUserQuestion card stays pending/blank even though the DB block
        # carries the answers, because the persisted answered block is
        # suppressed while a same-id streaming card is still in flight. The
        # event rides the broadcast's event_log, so catch-up replay sees it
        # too (this closes the navigate-away-and-back blank-card bug).
        bc = get_broadcast(chat_id)
        if bc is not None:
          bc.publish({
            "type": "answers_applied",
            "question_id": body.question_id or pending.question_id,
            "answers": body.answers,
          })
        return _answer_delivered_response(chat_id)
      # No in-memory pending question. If the chat is still alive, this is a
      # stale/foreign card (or Stop cancelled the question) and must not answer
      # whichever turn is now running. If the chat is idle, however, the process
      # may have restarted while the durable transcript still carries the open
      # question. Treat that transcript question as the source of truth: save
      # the answer, queue the hidden continuation at the FRONT of pending work,
      # and immediately promote it into a new run. The human wait survives the
      # process; only the SDK future needed rehydrating.
      if is_chat_running(chat_id):
        raise HTTPException(
          status_code=410,
          detail="The question is no longer accepting answers.",
        )
      if (
        questions.was_cancelled(chat_id, body.question_id)
        and not cancelled_when_submitted
      ):
        raise HTTPException(
          status_code=410,
          detail="The question is no longer accepting answers.",
        )

      db.expire(chat)
      if not _has_unanswered_question(chat, body.question_id):
        raise HTTPException(
          status_code=410,
          detail="The question is no longer accepting answers.",
        )

      if not mark_starting(chat_id):
        raise HTTPException(
          status_code=409,
          detail="The chat is starting another turn; please try again.",
        )

      started_message = None
      try:
        await _append_to_pending(
          chat,
          body,
          db,
          initiated_by_app_id=principal.app_id,
          front=True,
          require_answer_match=True,
        )
        drain_token = alloc_run_token()
        next_messages, next_user, next_session_id = (
          await chat_queue.promote_pending_messages_locked(
            db, chat_id, drain_token,
          )
        )
        if not next_user:
          raise HTTPException(
            status_code=503,
            detail="Could not resume the question; please try again.",
          )
        started_message = next_user
        get_system_broadcast().publish({
          "type": "chat_run_started",
          "chatId": chat_id,
        })
        _schedule_continuation(
          chat_id=chat_id,
          messages=next_messages,
          session_id=next_session_id,
          provider_id=chat.provider,
          next_user=next_user,
          run_token=drain_token,
        )
        bc = get_broadcast(chat_id)
        if bc is not None:
          bc.publish({
            "type": "answers_applied",
            "question_id": body.question_id,
            "answers": body.answers,
          })
      except HTTPException:
        discard_starting(chat_id)
        raise
      except Exception as exc:
        discard_starting(chat_id)
        log.warning(
          "Could not resume durable question chat_id=%s: %s", chat_id, exc,
        )
        raise HTTPException(
          status_code=503,
          detail="Could not save your answer; please try again.",
        ) from exc

      return JSONResponse(
        status_code=202,
        content={
          "status": "started",
          "answer_turn": "new",
          "message": started_message,
        },
      )

  # Serialize an ordinary send with the provider-switch synthesis/commit.
  # Whichever request owns the lock first establishes the state the other
  # observes: a send first makes the switch see a busy chat; a switch first
  # makes the send reload and start on the newly committed provider. This
  # removes the actor-order race where a message could begin on the outgoing
  # provider while its handoff was still being synthesized.
  # Lock order is transition then queue; all send predicates refresh inside.
  async with chat_queue.get_transition_lock(chat_id):
    async with chat_queue.get_lock(chat_id):
      db.expire(chat)
      return await _send_message_locked(body, chat_id, principal, db, chat)


async def _send_message_locked(
  body: schemas.SendMessage,
  chat_id: str,
  principal: Principal,
  db: Session,
  chat: models.Chat,
):
  """Handle a normal send while holding the per-chat transition lock."""

  duplicate = _duplicate_send_response(chat_id, chat, body.cid)
  if duplicate is not None:
    return duplicate

  # One choke point for every genuine user send (initial / queued / steered all
  # pass here, after the answer-delivery returns above). Skip force_steer —
  # Stop's queue-collapse re-send of already-counted messages. Counts a turn
  # ARRIVAL, not a durable commit (a rare failed store can overcount by one) —
  # fine for a usage signal, not a billing counter. app_id = the originating
  # app for window.mobius.chat sends, null for the owner; metadata only.
  if not body.force_steer:
    activity.log_event(
      "chat_sent",
      chat_id=chat_id,
      provider=(chat.provider or "claude"),
      app_id=principal.app_id,
    )

  # Local helper used by both code paths below: coerces the chat's
  # JSON-column settings to a plain dict (defends against the
  # SQLite-driver string-mode quirk that _coerce_agent_settings in
  # routes/chats.py exists for).
  def _coerce_chat_settings(c):
    raw = c.agent_settings_json
    if raw is None:
      return {}
    if isinstance(raw, dict):
      return dict(raw)
    if isinstance(raw, str):
      import json as _json
      try:
        parsed = _json.loads(raw)
        return dict(parsed) if isinstance(parsed, dict) else {}
      except (ValueError, TypeError):
        return {}
    return {}

  # Drain gate (design §2.2): while the worker is draining for a restart, never
  # start a new turn or promote the queue — append to pending and return
  # "queued". The send is preserved and self-heals on the owner's next action
  # after the restart (the stale-pending drain), so nothing the owner sent is
  # lost. This must intercept BEFORE the queue-or-start branches below, which
  # would otherwise spawn a turn (fresh StartTurn, or a stale-pending drain).
  # force_steer is deliberately NOT exempt: a steer accepted while the drain is
  # interrupting a handle can buffer into the dying runner's continuation and
  # start fresh provider work mid-shutdown (or, with no running turn at all,
  # fall through to a fresh StartTurn). During the restart window every send —
  # steer included — queues.
  if is_draining():
    new_msg = await _append_to_pending(
      chat, body, db, initiated_by_app_id=principal.app_id,
    )
    db.expire(chat)
    return _queued_response(new_msg, len(chat.pending_messages or []))

  # Queue path: agent is running OR stale pending exists from a
  # previous crash. Appending the new send at the END of pending
  # preserves chronological order. When pending was stale (server
  # crashed mid-turn), we additionally spawn a run that drains the
  # queue from the head, so the queued messages actually get answered
  # rather than sitting forever.
  if is_chat_running(chat_id) or chat.pending_messages:
    selected_force_pending = (
      _selected_force_steer_pending(chat, body)
      if body.force_steer else None
    )
    if body.force_steer and selected_force_pending is None:
      return _not_steered_response(chat_id)

    # Mid-turn steering: ordinary sends require the opt-in flag, while
    # Stop's queue-collapse path may pass force_steer to turn already
    # queued messages into a live steer. Codex injects into the running
    # SDK turn; Claude interrupts and re-prompts on the same client. On
    # success the user message goes into the transcript and a
    # `steered_into_turn` event tells the client to render it inline.
    provider = chat.provider or "claude"
    if (
      is_chat_running(chat_id)
      and (body.force_steer or _steer_enabled(chat))
      and (body.force_steer or not chat.pending_messages)
      and _has_live_steerable_turn(chat_id, provider)
    ):
      # Every provider delivery names a row already durable in pending.
      user_msg = _user_message_from_body(chat, body)
      if body.force_steer:
        user_msgs = _user_messages_from_pending(
          selected_force_pending or [], user_msg,
        )
        consume_cids = list(body.consume_pending_cids or [])
        steer_content = user_msg["content"]
        reserved = None
      else:
        reserved = await _append_to_pending(
          chat, body, db, initiated_by_app_id=principal.app_id,
        )
        db.expire(chat)
        reserved_cid = cid_of(reserved)
        user_msgs = [reserved]
        consume_cids = [reserved_cid] if reserved_cid is not None else []
        steer_content = reserved.get("content", "")
      # Claude converts at its interrupt boundary; its buffer is only a cache.
      defer_to_runner = provider == "claude"
      try:
        steered = await _steer_into_active_turn(
          provider, chat_id, steer_content,
          user_msgs if defer_to_runner else None,
          consume_cids if defer_to_runner else None,
        )
      except Exception:
        # A failed delivery leaves the reserved row pending.
        steered = False
      if steered:
        # A question tool is a synchronous human pause. Once the owner
        # fast-forwards (or a steer-enabled chat auto-steers), the new message
        # supersedes that pause; leaving its future registered would strand
        # Codex waiting on a card the transcript has moved past. Retire it only
        # AFTER acceptance so a failed steer leaves the question answerable.
        questions.cancel(chat_id)
        if defer_to_runner:
          # The optimistic response mirrors the runner's cid conversion.
          consumed = set(consume_cids)
          stored_result = {
            "stored_messages": user_msgs,
            "pending": [
              m for m in (chat.pending_messages or [])
              if cid_of(m) not in consumed
            ],
          }
        else:
          # Codex append and pending removal commit atomically for one cid.
          try:
            stored_result = await _split_steer_at_route(
              chat_id, user_msgs, consume_cids,
            )
          except Exception:
            log.warning(
              "steered transcript write did not persist chat_id=%s", chat_id,
            )
            raise HTTPException(
              status_code=503,
              detail="Could not save your message; please refresh.",
            )
        bc = get_broadcast(chat_id)
        if bc is not None:
          stored_messages = stored_result.get("stored_messages")
          if not isinstance(stored_messages, list) or not stored_messages:
            stored = stored_result.get("stored") or user_msg
            stored_messages = [stored]
          bc.publish({
            "type": "steered_into_turn",
            "messages": [
              {
                "role": "user",
                "ts": msg.get("ts"),
                "cid": cid_of(msg),
                "content": msg.get("content", ""),
                **({"attachments": msg.get("attachments")} if msg.get("attachments") else {}),
              }
              for msg in stored_messages
            ],
            # Backward-compatible shape for any existing client still
            # expecting a single steered row.
            "ts": stored_messages[-1].get("ts"),
            "content": stored_messages[-1].get("content", ""),
          })
        return _steered_response(
          chat_id, stored_result.get("pending"),
        )
      if body.force_steer:
        return _not_steered_response(chat_id)
      # A failed ordinary steer reports the existing reservation as queued.
      db.expire(chat)
      remaining = list(chat.pending_messages or [])
      try:
        position = (
          [cid_of(m) for m in remaining].index(cid_of(reserved)) + 1
        )
      except ValueError:
        position = len(remaining)
      return _queued_response(reserved, position)
    if body.force_steer:
      return _not_steered_response(chat_id)

    new_msg = await _append_to_pending(
      chat, body, db, initiated_by_app_id=principal.app_id,
    )
    started_message = None

    if not is_chat_running(chat_id):
      # Stale pending — try to claim and drain. mark_starting prevents
      # a duplicate spawn if a concurrent request already started one
      # (e.g., two stale-pending POSTs racing).
      if mark_starting(chat_id):
        try:
          # The drained turn gets its own run_token: PromotePending sets
          # its run marker under it, and the spawned runner reuses it.
          drain_token = alloc_run_token()
          next_messages, next_user, next_session_id = (
            await chat_queue.promote_pending_messages_locked(
              db, chat_id, drain_token,
            )
          )
          if next_user:
            started_message = next_user
            get_system_broadcast().publish({
              "type": "chat_run_started",
              "chatId": chat_id,
            })
            _schedule_continuation(
              chat_id=chat_id,
              messages=next_messages,
              session_id=next_session_id,
              provider_id=chat.provider,
              next_user=next_user,
              run_token=drain_token,
            )
          else:
            # Nothing to promote (empty queue / queue race) — release. A
            # MALFORMED head no longer lands here: it raises in the actor and
            # is handled by the `except` below (→ FAILED_LEAVE_MARKER).
            discard_starting(chat_id)
        except Exception:
          discard_starting(chat_id)
          raise

    # Re-read pending after the potential promote so the reported
    # position reflects the user-visible queue (excludes the message
    # that just became the active turn). expire() drops the identity-map
    # copy so this read reflects the actor's committed write.
    db.expire(chat)
    remaining = list(chat.pending_messages or [])
    try:
      position = [m.get("ts") for m in remaining].index(new_msg["ts"]) + 1
    except ValueError:
      # Queue-collapse recovery consumed the message we just appended into
      # the newly-started combined turn. Tell the client to connect to the
      # stream instead of keeping a queued chip that no longer exists.
      position = 0
    if position == 0:
      payload = {"status": "started"}
      if started_message is not None:
        payload["message"] = started_message
      return JSONResponse(status_code=202, content=payload)
    return _queued_response(
      new_msg,
      position,
      started=started_message is not None,
      message=started_message,
    )

  if not mark_starting(chat_id):
    new_msg = await _append_to_pending(
      chat, body, db, initiated_by_app_id=principal.app_id,
    )
    return _queued_response(new_msg, len(chat.pending_messages))

  # From here until create_task, any exception must discard the
  # starting guard — otherwise the chat_id stays in the set forever and
  # the chat is stuck "starting" until process restart.  run_chat's
  # outer finally only fires after the task is scheduled.
  try:
    # The initial-send write — append the user message, set the title +
    # provider on the first message, AND set the run marker — is one
    # atomic actor command (StartTurn). Keyed on this turn's run_token,
    # which the spawned runner reuses, so the marker StartTurn sets
    # matches the runner's streaming/terminal writes. Building the user
    # message (uploads notice, attachments, ts) stays on the route; the
    # actor owns the JSON-blob mutation.
    from app.chat import current_run_generation
    # Capture the run generation BEFORE the StartTurn await. A Stop that
    # lands during the await bumps the generation, clears the marker, and
    # releases _starting (stop_chat_for, while no live handle is registered
    # yet). Reading the generation AFTER the ack (as this code once did)
    # would adopt Stop's bumped value, so the spawned run_chat's run_gen
    # would MATCH current and the already-stopped turn would run anyway —
    # with no durable marker (Stop cleared it), invisible to crash
    # recovery. Capturing here + revalidating after the ack closes that
    # window; a Stop that lands AFTER the spawn is caught by run_chat's own
    # generation guard.
    start_gen = current_run_generation(chat_id)
    run_token = alloc_run_token()
    user_msg = _user_message_from_body(chat, body)
    owner = db.query(models.Owner).first()
    default_provider = resolve_default_provider(
      get_settings().data_dir, owner.provider if owner else None,
    )

    ack = get_writer().submit(
      StartTurn(
        chat_id=chat_id,
        run_token=run_token,
        user_msg=user_msg,
        title_source=body.content,
        default_provider=default_provider,
        initiated_by_app_id=principal.app_id,
      )
    )
    # StartTurn returns the agent history (schemas.ChatMessage list built
    # exactly as the pre-C2 inline path), the session_id, and the
    # provider it set/kept. Awaited so the user message + run marker are
    # durable before the background task starts (the chat list reflects
    # the send immediately, and run_chat sees the marker).
    try:
      result = await await_ack(ack)
    except Exception as exc:
      raise _message_persist_unavailable(exc, chat_id=chat_id) from exc
    if result.get("duplicate"):
      # The first POST committed but its acknowledgement was lost. The actor
      # found this cid in durable state, so the retry is complete without a
      # new run. Release the speculative route claim before responding.
      discard_starting(chat_id)
      if result.get("duplicate_location") == "pending":
        pending = list(result.get("pending") or [])
        existing = result.get("message") or user_msg
        try:
          position = [cid_of(row) for row in pending].index(cid_of(existing)) + 1
        except ValueError:
          position = len(pending)
        return _queued_response(existing, position)
      return JSONResponse(
        status_code=200,
        content={
          "status": "duplicate",
          "message": result.get("message") or user_msg,
          "running": is_chat_running(chat_id),
        },
      )
    msgs = result["history"]
    session_id = result["session_id"]
    provider = result["provider"]

    if current_run_generation(chat_id) == start_gen:
      # No Stop raced during the StartTurn commit. Create the broadcast
      # before spawning so the stream endpoint can subscribe without a
      # race, then spawn the turn keyed on the generation we captured.
      bc = create_broadcast(chat_id)  # noqa: F841 — registered in global registry
      get_system_broadcast().publish({
        "type": "chat_run_started",
        "chatId": chat_id,
      })
      asyncio.create_task(
        run_chat(
          msgs, chat_id=chat_id, session_id=session_id,
          provider_id=provider, run_gen=start_gen,
          attachments=body.attachments, timezone=body.timezone,
          viewport=body.viewport, run_token=run_token,
        )
      )
    else:
      # A Stop raced during the StartTurn commit: it bumped the generation,
      # cleared the marker, and released _starting. The user message is
      # durable (StartTurn committed it) and the chat is now idle. Do NOT
      # spawn — running the already-stopped turn here would leave no durable
      # marker. Release _starting (idempotent — Stop already did) and let
      # the response fall through; the client's reconnect gets a 204 (no
      # active broadcast) and refreshes the now-idle, message-saved chat.
      discard_starting(chat_id)
  except Exception:
    discard_starting(chat_id)
    raise

  return JSONResponse(
    status_code=202,
    content={"status": "started", "message": user_msg},
  )


@router.delete(
  "/{chat_id}/pending/{cid}",
  status_code=200,
  dependencies=[Depends(reject_cross_site)],
)
async def cancel_pending_message(
  chat_id: str,
  cid: str,
  principal: Principal = Depends(get_owner_or_chat_embed_principal),
  db: Session = Depends(get_db),
):
  """Removes a queued (not-yet-started) user message from the pending
  queue. Identifies the message by its stable `cid` (client-minted, or a
  `legacy-<ts>` derivation for pre-cid rows).

  Returns the updated pending queue so the client can reconcile any
  drift (e.g. the backend promoted a message into the active turn
  between the user clicking X and the DELETE landing).
  """
  # Existence check only — the actor's CancelPending does the RMW. The
  # 404 here keeps the route's contract (unknown / deleted chat → 404).
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  require_chat_embed_operation(principal, "chat:send")
  get_active_chat_for_principal(db, chat_id, principal)

  # The actor's CancelPending removes the matching cid and commits — the
  # SOLE runtime mutator of pending_messages, so a DELETE racing a
  # concurrent POST/promote can't lost-update. Returns the remaining
  # queue so the client can reconcile drift (e.g. the backend promoted a
  # message into the active turn between the click and the DELETE).
  ack = get_writer().submit(
    CancelPending(chat_id=chat_id, run_token="", cid=cid)
  )
  result = await await_ack(ack)
  return {"pending_messages": result["pending"]}


@router.get("/{chat_id}/stream")
async def stream_chat(
  request: Request,
  chat_id: str,
  principal: Principal = Depends(get_chat_view_principal),
  db: Session = Depends(get_db),
):
  """SSE endpoint: subscribes to the chat's broadcast and streams events.

  Sends a catch-up burst of all prior events, then streams live events
  until the broadcast is completed or the client disconnects.  Keepalive
  comments are sent every 30 s to prevent proxy timeouts.

  Same actor gate as send_message: owner streams any chat; an app token
  streams only a chat it created (403 otherwise). The ownership check
  runs against the DB row up front so an app can't read another chat's
  event stream by guessing its id, even though the events themselves
  flow from the in-memory broadcast.
  """
  # Gate before touching the broadcast. Raises 404 (missing/deleted) or
  # 403 (app token, foreign chat) — matching send_message's surface.
  require_chat_embed_operation(principal, "chat:stream")
  get_active_chat_for_principal(db, chat_id, principal)
  embed_session_id = (
    principal.embed_session_id if principal.scope == "chat_embed" else None
  )

  # Release the DB connection before the stream loop. Like the shell SSE
  # in notify.py, this StreamingResponse would otherwise pin a pooled
  # connection for the whole life of the stream (FastAPI defers get_db's
  # teardown until the body finishes), and enough concurrent chat streams
  # would exhaust the Postgres QueuePool. The gate above is the only DB
  # use here; the generator never touches `db`.
  db.close()

  bc = get_broadcast(chat_id)
  if bc is None:
    # No broadcast either because none was ever created or because the
    # completed-broadcast TTL (30s) elapsed. The third case is a real
    # race: a continuation is being scheduled and the client reconnect
    # lands in the gap. Hard to fix without restructuring the
    # broadcast lifecycle; logging makes it visible if it gets noisy.
    log.debug(
      "stream subscribe: no broadcast for chat_id=%s "
      "(likely between turns or TTL)", chat_id,
    )
    return Response(status_code=204)

  async def generate():
    # Subscribe INSIDE the generator, before the try, so it pairs with
    # the `finally: bc.unsubscribe(queue)` below across the generator's
    # whole lifecycle. If we subscribed at the endpoint (before building
    # the StreamingResponse) and the client disconnected before the
    # generator's body ever ran, the finally would never fire and the
    # queue would leak in bc.subscribers until the broadcast completed.
    # subscribe() is synchronous (snapshots the event_log and registers
    # the queue with no await in between), so the catch-up burst still
    # captures exactly the events present when this subscriber attaches.
    catch_up, queue = bc.subscribe()
    last_embed_auth_check = 0.0

    def embed_session_active() -> bool:
      nonlocal last_embed_auth_check
      if embed_session_id is None:
        return True
      now_mono = time.monotonic()
      if now_mono - last_embed_auth_check < 1.0:
        return True
      last_embed_auth_check = now_mono
      return chat_embed_session_is_active(embed_session_id)
    try:
      if not embed_session_active():
        return
      # Send all events buffered before this client connected.
      has_done = False
      for event in catch_up:
        yield _sse(event)
        if event.get("type") == "done":
          has_done = True

      # Signal the client that catch-up is complete and live events follow.
      # The client uses this to switch from instant rendering to typewriter.
      # Include the server clock at replay completion. Thinking deltas retain
      # their original server timestamps in the broadcast log; the frontend
      # uses this marker to restore the quiet interval after the last delta
      # instead of restarting the visible timer when a chat remounts.
      yield _sse({"type": "catch_up_done", "ts": int(time.time() * 1000)})

      # If the broadcast already finished and the catch-up included the
      # done event, we're done — no need to wait on the live queue.
      if not bc.running and has_done:
        return

      # If the broadcast already finished but the catch-up had no done
      # event, synthesise one so the client unblocks.
      if not bc.running and not has_done:
        yield _sse({"type": "done"})
        return

      # Stream live events from the queue.
      while True:
        if not embed_session_active():
          return
        if await request.is_disconnected():
          break

        try:
          event = await asyncio.wait_for(
            queue.get(), timeout=_KEEPALIVE_INTERVAL
          )
        except asyncio.TimeoutError:
          # Send a keepalive comment — invisible to EventSource clients
          # but keeps the TCP connection alive through proxies.
          yield ": keepalive\n\n"
          continue

        # None is the sentinel pushed by mark_completed().
        if event is None:
          break

        yield _sse(event)

        if event.get("type") == "done":
          break

    finally:
      bc.unsubscribe(queue)

  return StreamingResponse(
    generate(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )
