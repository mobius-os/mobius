"""Programmatic chat-turn startup behind one lifecycle boundary.

Interactive sends own request-specific validation and response shaping in the
chat route.  App conflict resolution, platform conflict resolution, and
Contribute Autopilot all start a prepared turn without that HTTP lifecycle.
Those callers still own their distinct eligibility rules; this module owns the
shared start protocol after eligibility is established.
"""

import asyncio
from contextlib import nullcontext
import time

from app import models, providers
from app.broadcast import (
  create_broadcast,
  get_system_broadcast,
  remove_broadcast,
)
from app.chat import (
  current_run_generation,
  discard_starting,
  mark_starting,
  run_chat,
)
from app.chat_writer import (
  StartTurn,
  StartTurnBlockedByPendingQuestion,
  alloc_run_token,
  await_ack,
  get_writer,
)
from app.chat_visibility import coerce_agent_settings
from app.database import SessionLocal


class ProgrammaticChatModelRequired(RuntimeError):
  """A system caller attempted to start a chat with no persisted model."""


def require_programmatic_chat_model(chat_id: str, provider: str) -> str:
  """Return the chat's explicit model or fail before any turn is committed."""
  with SessionLocal() as db:
    chat = db.query(models.Chat).filter(
      models.Chat.id == chat_id,
      models.Chat.deleted_at.is_(None),
    ).first()
    if chat is None:
      raise ProgrammaticChatModelRequired(
        f"Programmatic chat {chat_id} does not exist."
      )
    model = coerce_agent_settings(chat.agent_settings_json).get("model")
    model = model.strip() if isinstance(model, str) and model.strip() else None
    if model is None:
      raise ProgrammaticChatModelRequired(
        f"Programmatic chat {chat_id} has no explicitly selected model."
      )
    if providers._model_belongs_to_other_provider(model, provider):
      raise ProgrammaticChatModelRequired(
        f"Programmatic chat {chat_id} model does not match provider {provider}."
      )
    return model


async def start_programmatic_chat_turn(
  *, chat_id: str, title: str, content: str, provider: str,
  initiated_by_app_id: int | None = None,
  hidden: bool = False,
  message_kind: str | None = None,
  source_work_id: str | None = None,
) -> bool:
  """Durably start one system-initiated turn if the chat can be claimed.

  The caller decides whether the chat is eligible (empty-only conflict chats,
  reusable Autopilot chats, or a newly created platform resolver).  This
  boundary owns the claim, writer command, Stop-generation fence, broadcast,
  task creation, and failure cleanup so those steps cannot drift by caller.

  Returns ``False`` when the claim fails, an owner question is pending, or a
  Stop wins while ``StartTurn`` is committing. Unexpected failures propagate
  after releasing the transient claim; the durable run remains available to
  normal reconciliation.
  """
  require_programmatic_chat_model(chat_id, provider)
  if not mark_starting(chat_id):
    return False

  try:
    start_gen = current_run_generation(chat_id)
    run_token = alloc_run_token()
    user_msg = {
      "role": "user",
      "content": content,
      "ts": int(time.time() * 1000),
    }
    if hidden:
      user_msg["hidden"] = True
    if message_kind is not None:
      user_msg["kind"] = message_kind
    if source_work_id is not None:
      user_msg["source_work_id"] = source_work_id
    result = await await_ack(get_writer().submit(StartTurn(
      chat_id=chat_id,
      run_token=run_token,
      user_msg=user_msg,
      title_source=title,
      default_provider=provider,
      initiated_by_app_id=initiated_by_app_id,
    )))

    if isinstance(result, StartTurnBlockedByPendingQuestion):
      discard_starting(chat_id)
      return False

    if current_run_generation(chat_id) != start_gen:
      discard_starting(chat_id)
      return False

    create_broadcast(chat_id)
    run_coro = None
    try:
      run_coro = run_chat(
        result["history"],
        chat_id=chat_id,
        session_id=result["session_id"],
        provider_id=result["provider"],
        run_gen=start_gen,
        run_token=run_token,
      )
      asyncio.create_task(run_coro)
    except BaseException:
      if run_coro is not None:
        run_coro.close()
      # No task or SSE subscriber owns this programmatic broadcast yet, so
      # remove it instead of publishing the continuation path's terminal pair.
      remove_broadcast(chat_id)
      raise
  except BaseException:
    discard_starting(chat_id)
    raise

  # Once scheduled, the task owns the claim and broadcast. A system
  # notification failure must not roll back a live run.
  get_system_broadcast().publish({
    "type": "chat_run_started",
    "chatId": chat_id,
  })
  return True


async def start_programmatic_chat_continuation(
  *, chat_id: str, root_run_id: str, run_token: str, content: str,
  continuation_id: str, reason: str, initiated_by_app_id: int | None = None,
  message_kind: str = "continuation", source_work_id: str | None = None,
  hidden: bool = False,
  activation_wait_id: str | None = None,
  _transition_lock_held: bool = False,
) -> bool:
  """Start one idempotent owner continuation on an already-settled root.

  Unlike ``start_programmatic_chat_turn``, this preserves the supplied logical
  root even when its prior physical run has reached a terminal.  It is the
  narrow seam durable coordinators need to resume an owner-authority controller
  without fabricating a second workflow engine or handing write authority to
  an app-scoped Delegation child.

  The continuation is started only while the chat is idle.  A live owner turn
  wins normally and the caller retries after that turn settles.  One
  ChatWriter command atomically appends the stable ``continuation_id`` and
  creates the caller-reserved ChatRun, eliminating a queue-only crash window.

  ``_transition_lock_held`` is reserved for coordinators that must retain an
  existing observation claim across this start. They still enter the queue
  lock here, preserving the canonical transition -> queue lock order.
  """
  from app import chat_queue, models, schemas
  from app.chat import (
    _schedule_continuation,
    discard_starting,
    is_chat_running,
    mark_starting,
    programmatic_start_blocked,
  )
  from app.chat_writer import (
    FinishRun,
    StartContinuation,
    StartContinuationAttached,
    StartContinuationBlocked,
  )
  from app.database import SessionLocal

  claimed = False
  try:
    async with asyncio.timeout(chat_queue.TERMINAL_LOCK_TIMEOUT_SECS):
      transition_guard = (
        nullcontext()
        if _transition_lock_held else chat_queue.get_transition_lock(chat_id)
      )
      async with transition_guard:
        async with chat_queue.get_lock(chat_id):
          # A retry after the durable command committed must attach even while
          # the in-process runner still owns the transient starting/running
          # marker. The command repeats this check inside its transaction for
          # the cross-process race after this read.
          orphaned = None
          with SessionLocal() as db:
            existing = db.query(models.ChatRun).filter(
              models.ChatRun.id == run_token,
              models.ChatRun.chat_id == chat_id,
            ).first()
            # Fresh machine work cannot release an owner-input, usage, or
            # manual restart hold. An already committed continuation keeps
            # its idempotent attachment/recovery path below.
            if existing is None and programmatic_start_blocked(
              db, chat_id, activation_wait_id=activation_wait_id,
            ):
              return False
            if existing is not None:
              if (existing.root_run_id or existing.id) != root_run_id:
                return False
              if existing.initiated_by_app_id != initiated_by_app_id:
                return False
              if existing.status == "completed":
                return True
              if existing.status != "running":
                return False
              if is_chat_running(chat_id):
                return True
              chat = db.query(models.Chat).filter(
                models.Chat.id == chat_id,
                models.Chat.deleted_at.is_(None),
              ).first()
              messages = list(chat.messages or []) if chat is not None else []
              continuation = messages[-1] if messages else None
              safe_orphan = bool(
                existing.provider_execution_admitted is False
                and isinstance(continuation, dict)
                and continuation.get("role") == "user"
                and continuation.get("cid") == continuation_id
                and continuation.get("content") == content
                and continuation.get("kind") == message_kind
                and continuation.get("continuation_reason") == (
                  reason if message_kind == "continuation" else None
                )
                and continuation.get("source_work_id") == source_work_id
                and bool(continuation.get("hidden")) == hidden
                and not ((chat.live_assistant or {}).get("blocks") or [])
              )
              if safe_orphan:
                history = [
                  schemas.ChatMessage(
                    role=message.get("role", "user"),
                    content=message.get("content", "") or "",
                  )
                  for message in messages
                ]
                orphaned = {
                  "history": history,
                  "promoted": continuation,
                  "session_id": chat.session_id,
                  "provider": chat.provider or "claude",
                }
              else:
                orphaned = None
          if existing is not None and orphaned is None:
            # An unowned running row with partial/ambiguous output is unsafe to
            # replay. Close that exact physical attempt so the coordinator
            # fails honestly instead of either duplicating tools or waiting
            # forever on a runner that does not exist.
            await await_ack(get_writer().submit(FinishRun(
              chat_id=chat_id,
              run_token=run_token,
              terminal_status="failed",
            )))
            return False
          if (
            activation_wait_id is not None
            and existing is None
            and is_chat_running(chat_id)
          ):
            # A planned-restart recovery can already own this same logical A.
            # Ask the writer to authenticate and attach the activation wait
            # without claiming a second transient runner. Different-root work
            # is rejected by StartContinuation and remains behind the barrier.
            from app.chat_event_sink import get_active_sink
            active_sink = get_active_sink(chat_id)
            if active_sink is None:
              return False
            attached = await await_ack(get_writer().submit(StartContinuation(
              chat_id=chat_id,
              run_token=run_token,
              root_run_id=root_run_id,
              content=content,
              cid=continuation_id,
              reason=reason,
              initiated_by_app_id=initiated_by_app_id,
              message_kind=message_kind,
              source_work_id=source_work_id,
              hidden=hidden,
              activation_wait_id=activation_wait_id,
              activation_attach_run_token=active_sink.run_token,
            )))
            return isinstance(attached, StartContinuationAttached)
          if not mark_starting(chat_id):
            return False
          claimed = True
          promoted = orphaned if existing is not None else await await_ack(
            get_writer().submit(StartContinuation(
              chat_id=chat_id,
              run_token=run_token,
              root_run_id=root_run_id,
              content=content,
              cid=continuation_id,
              reason=reason,
              initiated_by_app_id=initiated_by_app_id,
              message_kind=message_kind,
              source_work_id=source_work_id,
              hidden=hidden,
              activation_wait_id=activation_wait_id,
            ))
          )
          if isinstance(promoted, StartContinuationAttached):
            discard_starting(chat_id)
            claimed = False
            return True
          if isinstance(promoted, StartContinuationBlocked):
            discard_starting(chat_id)
            claimed = False
            return False
          next_user = promoted["promoted"]
          get_system_broadcast().publish({
            "type": "chat_run_started",
            "chatId": chat_id,
          })
          scheduled = _schedule_continuation(
            chat_id=chat_id,
            messages=promoted["history"],
            session_id=promoted["session_id"],
            provider_id=promoted["provider"],
            next_user=next_user,
            run_token=run_token,
          )
          if scheduled is False:
            # _schedule_continuation releases the transient claim and leaves
            # the durable run for boot reconciliation/review.
            claimed = False
            return False
          return True
  except BaseException:
    if claimed:
      discard_starting(chat_id)
    raise


async def start_programmatic_activity_continuation(
  *, chat_id: str, root_run_id: str, run_token: str,
  source_work_id: str, activity_id: str,
  _transition_lock_held: bool = False,
) -> bool:
  """Schedule one durable activity checkpoint without forging chat input.

  The writer creates only the ChatRun and empty live assistant slot. This seam
  then supplies ``run_chat`` a small ephemeral provider protocol prompt; it is
  never appended to ``messages`` or ``pending_messages``.
  """
  from app import chat_queue, schemas
  from app.chat import (
    _schedule_continuation,
    discard_starting,
    is_chat_running,
    mark_starting,
    programmatic_start_blocked,
  )
  from app.chat_writer import (
    FinishRun,
    StartActivityContinuation,
    StartContinuationAttached,
    StartContinuationBlocked,
  )
  from app.database import SessionLocal

  claimed = False
  try:
    async with asyncio.timeout(chat_queue.TERMINAL_LOCK_TIMEOUT_SECS):
      transition_guard = (
        nullcontext()
        if _transition_lock_held else chat_queue.get_transition_lock(chat_id)
      )
      async with transition_guard:
        async with chat_queue.get_lock(chat_id):
          orphaned = None
          with SessionLocal() as db:
            existing = db.query(models.ChatRun).filter(
              models.ChatRun.id == run_token,
              models.ChatRun.chat_id == chat_id,
            ).first()
            if existing is None and programmatic_start_blocked(db, chat_id):
              return False
            if existing is not None:
              if (
                (existing.root_run_id or existing.id) != root_run_id
                or existing.initiated_by_app_id is not None
              ):
                return False
              if existing.status == "completed":
                return False
              if existing.status != "running":
                return False
              if is_chat_running(chat_id):
                return True
              chat = db.query(models.Chat).filter(
                models.Chat.id == chat_id,
                models.Chat.deleted_at.is_(None),
              ).first()
              from app.delegations import (
                activity_continuation_source,
                safe_parent_activity_startup_writer_orphan,
              )
              if safe_parent_activity_startup_writer_orphan(
                db, chat, existing,
              ):
                source = activity_continuation_source(
                  run_token=run_token, source_work_id=source_work_id,
                )
                history = [
                  schemas.ChatMessage(
                    role=message.get("role", "user"),
                    content=message.get("content", "") or "",
                  )
                  for message in list(chat.messages or [])
                ]
                history.append(schemas.ChatMessage(
                  role="user", content=source["content"],
                ))
                orphaned = {
                  "history": history,
                  "promoted": source,
                  "session_id": chat.session_id,
                  "provider": chat.provider or "claude",
                }
          if existing is not None and orphaned is None:
            await await_ack(get_writer().submit(FinishRun(
              chat_id=chat_id,
              run_token=run_token,
              terminal_status="failed",
            )))
            return False
          if not mark_starting(chat_id):
            return False
          claimed = True
          promoted = orphaned if existing is not None else await await_ack(
            get_writer().submit(StartActivityContinuation(
              chat_id=chat_id,
              run_token=run_token,
              root_run_id=root_run_id,
              source_work_id=source_work_id,
              activity_id=activity_id,
            ))
          )
          if isinstance(promoted, StartContinuationAttached):
            discard_starting(chat_id)
            claimed = False
            return True
          if isinstance(promoted, StartContinuationBlocked):
            discard_starting(chat_id)
            claimed = False
            return False
          if "promoted" not in promoted:
            from app.delegations import activity_continuation_source
            source = activity_continuation_source(
              run_token=run_token, source_work_id=source_work_id,
            )
            promoted["history"].append(schemas.ChatMessage(
              role="user", content=source["content"],
            ))
            promoted["promoted"] = source
          get_system_broadcast().publish({
            "type": "chat_run_started",
            "chatId": chat_id,
          })
          scheduled = _schedule_continuation(
            chat_id=chat_id,
            messages=promoted["history"],
            session_id=promoted["session_id"],
            provider_id=promoted["provider"],
            next_user=promoted["promoted"],
            run_token=run_token,
          )
          if scheduled is False:
            claimed = False
            return False
          return True
  except BaseException:
    if claimed:
      discard_starting(chat_id)
    raise
