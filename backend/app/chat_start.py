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
