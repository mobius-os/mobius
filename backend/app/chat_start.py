"""Programmatic chat-turn startup behind one lifecycle boundary.

Interactive sends own request-specific validation and response shaping in the
chat route.  App conflict resolution, platform conflict resolution, and
Contribute Autopilot all start a prepared turn without that HTTP lifecycle.
Those callers still own their distinct eligibility rules; this module owns the
shared start protocol after eligibility is established.
"""

import asyncio
import time

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


async def start_programmatic_chat_turn(
  *, chat_id: str, title: str, content: str, provider: str,
  initiated_by_app_id: int | None = None,
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
  """
  from app import chat_queue, models, schemas
  from app.chat import (
    _schedule_continuation,
    discard_starting,
    is_chat_running,
    mark_starting,
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
      async with chat_queue.get_transition_lock(chat_id):
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
            if existing is not None:
              if (existing.root_run_id or existing.id) != root_run_id:
                return False
              if existing.initiated_by_app_id != initiated_by_app_id:
                return False
              if existing.status != "running" or is_chat_running(chat_id):
                return True
              chat = db.query(models.Chat).filter(
                models.Chat.id == chat_id,
                models.Chat.deleted_at.is_(None),
              ).first()
              messages = list(chat.messages or []) if chat is not None else []
              continuation = messages[-1] if messages else None
              safe_orphan = bool(
                isinstance(continuation, dict)
                and continuation.get("role") == "user"
                and continuation.get("cid") == continuation_id
                and continuation.get("content") == content
                and continuation.get("kind") == "continuation"
                and continuation.get("continuation_reason") == reason
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
