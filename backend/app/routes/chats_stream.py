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

from app import models, questions, schemas
from app.broadcast import create_broadcast, get_broadcast, get_system_broadcast
from app.chat import (
  _schedule_continuation,
  discard_starting,
  is_chat_running,
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
  get_writer,
)
from app import claude_sdk_runner, codex_sdk_runner
from app.providers import effective_agent_settings
from app.runner_registry import RunnerKind, registry
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, get_current_owner, get_principal, reject_cross_site,
)
from app.resource_access import (
  get_active_chat_for_principal, get_active_chat_or_404,
)

router = APIRouter(prefix="/api/chats", tags=["chats"])

log = logging.getLogger(__name__)

# Keepalive interval for the SSE stream to prevent proxy timeouts.
_KEEPALIVE_INTERVAL = 30  # seconds


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
    )
  )
  result = await await_ack(ack)
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
    content={"status": "answer_delivered", "chat_id": chat_id},
  )


def _queued_response(new_msg: dict, position: int) -> JSONResponse:
  """Standard 202 response for a queued message."""
  return JSONResponse(
    status_code=202,
    content={
      "status": "queued",
      "position": position,
      "ts": new_msg["ts"],
    },
  )


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
  """Whether mid-turn steering is opted in for this chat.

  Read through `effective_agent_settings`, the same merge other per-chat
  / per-owner flags use: hard-coded defaults, then the owner-wide
  `/data/shared/agent-settings.json`, then this chat's
  `agent_settings_json` overrides (later wins). The flag DEFAULTS OFF —
  it appears in none of those layers out of the box — so deploying this
  code changes nothing until the owner sets `steer_enabled: true`
  globally or on a chat. The legacy `codex_steer_enabled` key remains
  honored so existing Codex opt-ins keep working.
  """
  settings = get_settings()
  raw = chat.agent_settings_json
  if isinstance(raw, str):
    try:
      raw = json.loads(raw)
    except (ValueError, TypeError):
      raw = None
  overrides = raw if isinstance(raw, dict) else None
  merged = effective_agent_settings(
    settings.data_dir, chat_overrides=overrides, provider=chat.provider,
  )
  return bool(merged.get("steer_enabled", merged.get("codex_steer_enabled")))


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
  provider: str, chat_id: str, content: str,
) -> bool:
  """Routes a steer request to the active provider-specific handle."""
  if provider == "claude":
    return await claude_sdk_runner.steer_into_active_turn(chat_id, content)
  return await codex_sdk_runner.steer_into_active_turn(chat_id, content)


def _steered_response(chat_id: str) -> JSONResponse:
  """202 for a message steered into the live turn.

  The message is in the TRANSCRIPT (not the pending queue) and the live
  turn already saw it via `steer()`, so the client renders it inline as
  content growth rather than a queued-tray entry."""
  return JSONResponse(
    status_code=202,
    content={"status": "steered", "chat_id": chat_id},
  )


@router.post("/{chat_id}/messages", status_code=202)
async def send_message(
  body: schemas.SendMessage,
  chat_id: str,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Saves the user message, starts the agent as a background task,
  and returns 202 immediately.  The client streams via GET /stream.

  Owner tokens may send to any active chat. App tokens may send only to
  a chat they created (`created_by_app_id == app_id`) — the app-
  attributed contract (design §1). Foreign chats are 403; the runner /
  queue / SSE internals are reused unchanged for both actors.
  """
  chat = get_active_chat_for_principal(db, chat_id, principal)

  # SDK in-process answer delivery: if a live SDK turn is blocked waiting
  # for an AskUserQuestion answer (held in `questions._pending[chat_id]`),
  # persist the answer through the writer actor, then resolve the future
  # in-place and return — the SDK continues the active turn. The answer
  # merge is NO LONGER written inline on this request's session: the
  # actor's `AnswerQuestion` command re-reads the chat fresh, merges the
  # answer into the right question block (by id), and commits — the SOLE
  # runtime mutator of the JSON blob, so the answer can't lost-update
  # against a concurrent streaming snapshot.
  #
  # Registration race: the frontend renders the card the instant the
  # `question` SSE event lands, but the runner registers the pending
  # entry in a separate task (Codex via `run_coroutine_threadsafe` from
  # the SDK worker thread; Claude's `can_use_tool` callback). A user who
  # answers in the tens-of-ms window before the entry lands used to hit
  # 410. We PEEK (not pop) with a short grace period; the `await sleep`
  # yields so the runner can write the entry. 500ms covers the race in
  # practice; a genuinely stale UI still gets the 410 fast.
  if body.answers:
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
        return _answer_delivered_response(chat_id)
      # No pending question (Stop cancelled it, or stale UI). Falling
      # through would land the answer text as a new turn prompt (e.g.
      # "- Which color?: Red"), nonsense to the agent. 410 tells the
      # client the question is no longer accepting answers.
      raise HTTPException(
        status_code=410,
        detail="The question is no longer accepting answers.",
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

  # Queue path: agent is running OR stale pending exists from a
  # previous crash. Appending the new send at the END of pending
  # preserves chronological order. When pending was stale (server
  # crashed mid-turn), we additionally spawn a run that drains the
  # queue from the head, so the queued messages actually get answered
  # rather than sitting forever.
  if is_chat_running(chat_id) or chat.pending_messages:
    # Mid-turn steering (opt-in): Codex injects into the running SDK turn;
    # Claude has no wire-level inject, so it interrupts the live response
    # and re-prompts on the same client. On success the user message goes
    # into the transcript and a `steered_into_turn` event tells the client
    # to render it inline. On False (closed-turn race) or any exception,
    # fall through to the queue — steering must never break a send.
    provider = chat.provider or "claude"
    if (
      is_chat_running(chat_id)
      and _steer_enabled(chat)
      and _has_live_steerable_turn(chat_id, provider)
    ):
      user_msg = _user_message_from_body(chat, body)
      try:
        steered = await _steer_into_active_turn(
          provider, chat_id, user_msg["content"],
        )
      except Exception:
        # Any steer failure is non-fatal: log nothing louder than the
        # runner already does and queue the message instead.
        steered = False
      if steered:
        # Persist into the transcript via the actor (NOT pending), keeping
        # the single-writer invariant. The empty run_token broad-fences by
        # chat; the command inserts the row before the trailing assistant
        # partial so the streaming snapshot still targets the assistant.
        ack = get_writer().submit(
          AppendSteeredUserMessage(
            chat_id=chat_id, run_token="", user_msg=user_msg,
          )
        )
        try:
          await await_ack(ack)
        except Exception:
          # The transcript write didn't land, but the live turn already
          # absorbed the steer — we can't un-steer. Surface the failure so
          # the client refetches authoritative state rather than silently
          # dropping the message.
          log.warning(
            "AppendSteeredUserMessage did not persist chat_id=%s", chat_id,
          )
          raise HTTPException(
            status_code=503,
            detail="Could not save your message; please refresh.",
          )
        bc = get_broadcast(chat_id)
        if bc is not None:
          bc.publish({
            "type": "steered_into_turn",
            "ts": user_msg["ts"],
            "content": user_msg["content"],
          })
        return _steered_response(chat_id)

    new_msg = await _append_to_pending(
      chat, body, db, initiated_by_app_id=principal.app_id,
    )

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
            await chat_queue.promote_pending_messages(
              db, chat_id, drain_token,
            )
          )
          if next_user:
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
      return JSONResponse(status_code=202, content={"status": "started"})
    return _queued_response(new_msg, position)

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
    default_provider = (owner.provider if owner else "claude") or "claude"

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
    result = await await_ack(ack)
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

  return JSONResponse(status_code=202, content={"status": "started"})


@router.delete(
  "/{chat_id}/pending/{ts}",
  status_code=200,
  dependencies=[Depends(reject_cross_site)],
)
async def cancel_pending_message(
  chat_id: str,
  ts: int,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Removes a queued (not-yet-started) user message from the pending
  queue. Identifies the message by its client-assigned timestamp.

  Returns the updated pending queue so the client can reconcile any
  drift (e.g. the backend promoted a message into the active turn
  between the user clicking X and the DELETE landing).
  """
  # Existence check only — the actor's CancelPending does the RMW. The
  # 404 here keeps the route's contract (unknown / deleted chat → 404).
  get_active_chat_or_404(db, chat_id)

  # The actor's CancelPending removes the matching ts and commits — the
  # SOLE runtime mutator of pending_messages, so a DELETE racing a
  # concurrent POST/promote can't lost-update. Returns the remaining
  # queue so the client can reconcile drift (e.g. the backend promoted a
  # message into the active turn between the click and the DELETE).
  ack = get_writer().submit(
    CancelPending(chat_id=chat_id, run_token="", ts=ts)
  )
  result = await await_ack(ack)
  return {"pending_messages": result["pending"]}


@router.get("/{chat_id}/stream")
async def stream_chat(
  request: Request,
  chat_id: str,
  principal: Principal = Depends(get_principal),
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
  get_active_chat_for_principal(db, chat_id, principal)

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
    try:
      # Send all events buffered before this client connected.
      has_done = False
      for event in catch_up:
        yield _sse(event)
        if event.get("type") == "done":
          has_done = True

      # Signal the client that catch-up is complete and live events follow.
      # The client uses this to switch from instant rendering to typewriter.
      yield _sse({"type": "catch_up_done"})

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
