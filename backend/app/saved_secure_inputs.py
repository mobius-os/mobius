"""Saved sealed-input pauses and bounded backend-owned local execution.

Only command metadata and fixed outcomes persist. A durable consuming claim is
made before spawning, so duplicate submissions and crash recovery never repeat
a potentially irreversible operation. Values travel only through RAM and stdin.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
from typing import Any

from app import chat_queue, models
from app.broadcast import get_broadcast
from app.chat_writer import ClaimSecureInput, SettleSecureInput, await_ack, get_writer
from app.database import SessionLocal
from app.owner_input import publish_owner_input_changed

CONSUMER_TIMEOUT_SECONDS = 120
OWNER_CREDENTIAL_OUTCOMES = {
  0: (True, 0, "Credentials changed. Sign in again with the new details."),
  2: (False, 2, "Credential input was invalid."),
  3: (False, 1, "Owner account was not found."),
  4: (False, 1, "This instance uses managed sign-in."),
  5: (False, 1, "Current password is incorrect."),
  6: (False, 1, "Username must be 1–64 characters."),
  7: (False, 1, "Password cannot be blank or longer than 1024 characters."),
  8: (False, 1, "New passwords do not match."),
  9: (False, 1, "Credentials could not be changed."),
  10: (True, 0, "Credentials changed. Sign in again, then restart Möbius to refresh background access."),
}
SAFE_OUTCOMES = {
  "success": "Secure input was consumed without exposing its values.",
  "failed": "The sealed consumer failed; submitted values were discarded.",
  "timeout": "Sealed consumer timed out; submitted values were discarded. Its effects may be incomplete; do not repeat automatically.",
  "interrupted": "Secure input execution was interrupted; its outcome is unknown. Submitted values were discarded. Do not repeat the operation automatically.",
  "cancelled": "Secure input was cancelled.",
  **{f"owner-{code}": outcome[2] for code, outcome in OWNER_CREDENTIAL_OUTCOMES.items()},
}
log = logging.getLogger(__name__)
_tasks: dict[str, tuple[str, asyncio.Task]] = {}


def consumer_outcome(action: str, returncode: int) -> tuple[bool, int, str]:
  """Trusted result copy; consumer output is never inspected."""
  if returncode == 124:
    return False, 124, SAFE_OUTCOMES["timeout"]
  if action == "owner-credentials":
    return OWNER_CREDENTIAL_OUTCOMES.get(returncode, OWNER_CREDENTIAL_OUTCOMES[9])
  if returncode == 0:
    return True, 0, SAFE_OUTCOMES["success"]
  return False, returncode or 1, SAFE_OUTCOMES["failed"]


def validate_consumer_spec(payload: dict) -> dict:
  """A persisted command is pre-authored code, never a submitted value."""
  command, cwd, action = payload.get("command"), payload.get("cwd"), payload.get("action")
  if (not isinstance(command, list) or not 1 <= len(command) <= 64
      or any(not isinstance(arg, str) or not arg or "\0" in arg or len(arg) > 8192 for arg in command)
      or sum(len(arg) for arg in command) > 32768):
    raise ValueError("A bounded local consumer command is required.")
  if not isinstance(cwd, str) or len(cwd) > 4096 or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
    raise ValueError("A valid absolute consumer working directory is required.")
  if action not in {"run", "owner-credentials"}:
    raise ValueError("Unknown sealed consumer action.")
  if action == "owner-credentials":
    import sys
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts/update-owner-credentials.py")]
  return {"command": list(command), "cwd": cwd, "action": action}


def _consumer_env(chat_id: str) -> dict[str, str]:
  # Never preserve the publishing agent's environment or expired run token.
  allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "DATA_DIR", "API_BASE_URL")
  env = {key: os.environ[key] for key in allowed if key in os.environ}
  env["CHAT_ID"] = chat_id
  return env


async def _run_consumer(spec: dict, values: dict[str, str], chat_id: str) -> int:
  process = None
  try:
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
      *spec["command"], cwd=spec["cwd"], env=_consumer_env(chat_id),
      stdin=asyncio.subprocess.PIPE,
      stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
      start_new_session=True,
    ))
    try:
      process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
      # Cancellation must not lose ownership in the spawn/assignment window.
      process = await spawning
      raise
    payload = json.dumps(values, ensure_ascii=False).encode("utf-8")
    values.clear()
    try:
      await asyncio.wait_for(process.communicate(payload), CONSUMER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
      return 124
    finally:
      del payload
    return process.returncode
  finally:
    values.clear()
    if process is not None:
      # Kill the entire operation, including children left by an exited parent.
      try:
        os.killpg(process.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      await process.wait()


async def _publish_and_start(chat_id: str, request_id: str, result: dict, db) -> None:
  if result.get("queued"):
    from app.chat_event_sink import get_active_sink
    event = {"type": "answers_applied", "question_id": request_id, "answers": result["answers"]}
    sink = get_active_sink(chat_id)
    bc = get_broadcast(chat_id)
    if sink is not None:
      sink.publish(event)
    elif bc is not None:
      bc.publish(event)
    publish_owner_input_changed(chat_id, None, question_id=None)
  # Retry of a committed result may repair a lost wake, never a repeated
  # consumer. Ordinary pending-message admission is the sole scheduling owner.
  chat = db.get(models.Chat, chat_id, populate_existing=True)
  if (chat is not None and chat.deleted_at is None
      and any(message.get("cid") == f"secure-input:{request_id}"
              for message in chat.pending_messages or [])):
    from app.routes.chats_stream import start_queued_owner_continuation
    await start_queued_owner_continuation(chat_id, db)


async def finish(chat_id: str, request_id: str, status: str, outcome: str, *, generation: int | None = None, orphan_only: bool = False) -> dict:
  from app.chat import current_run_generation
  async with chat_queue.get_transition_lock(chat_id), chat_queue.get_lock(chat_id):
    if orphan_only and request_id in _tasks:
      # Claim and task registration share this gate. A reconciliation query
      # may have seen consuming while a fresh submission was still admitted.
      return {"status": "consuming"}
    if generation is not None and current_run_generation(chat_id) != generation:
      return {"status": "cancelled"}
    result = await await_ack(get_writer().submit(SettleSecureInput(
      chat_id=chat_id, request_id=request_id, status=status, outcome=outcome,
    )))
    with SessionLocal() as db:
      await _publish_and_start(chat_id, request_id, result, db)
    return result


async def _consume(chat_id: str, request_id: str, spec: dict, values: dict[str, str], generation: int) -> None:
  try:
    try:
      code = await _run_consumer(spec, values, chat_id)
      ok, _, message = consumer_outcome(spec["action"], code)
      outcome = next(key for key, value in SAFE_OUTCOMES.items() if value == message)
      status = "completed" if ok else "failed"
    except asyncio.CancelledError:
      # Stop owns durable cancellation; shutdown leaves the consuming claim
      # for conservative startup recovery. Neither path repeats execution.
      raise
    except Exception:
      status, outcome = "failed", "failed"
    await finish(chat_id, request_id, status, outcome, generation=generation)
  except asyncio.CancelledError:
    raise
  except Exception:
    # Do not log exception payloads from a secret-bearing operation.
    log.error("Could not save sealed consumer outcome; execution will not be repeated")
  finally:
    values.clear()


async def submit(chat_id: str, request_id: str, values: dict[str, str], generation: int) -> dict:
  """Return only after claim and backend task ownership; never wait for a human."""
  from app.chat import current_run_generation
  try:
    async with chat_queue.get_transition_lock(chat_id), chat_queue.get_lock(chat_id):
      if current_run_generation(chat_id) != generation:
        values.clear()
        return {"status": "cancelled"}
      claim = await await_ack(get_writer().submit(ClaimSecureInput(chat_id=chat_id, request_id=request_id)))
      if claim["status"] != "claimed":
        values.clear()
        return {"status": claim["status"]}
      task = asyncio.create_task(_consume(chat_id, request_id, claim, values, generation), name=f"sealed-input:{request_id}")
      _tasks[request_id] = (chat_id, task)
      def completed(done):
        values.clear()
        if _tasks.get(request_id, (None, None))[1] is done:
          _tasks.pop(request_id, None)
      task.add_done_callback(completed)
      bc = get_broadcast(chat_id)
      if bc is not None:
        bc.publish({"type": "secure_input_consuming", "request_id": request_id, "status": "consuming"})
      return {"status": "consuming"}
  except BaseException:
    values.clear()
    raise


def cancel_running(chat_id: str) -> None:
  for owner, task in list(_tasks.values()):
    if owner == chat_id:
      task.get_loop().call_soon_threadsafe(task.cancel)


async def shutdown() -> None:
  tasks = [task for _, task in list(_tasks.values())]
  for task in tasks:
    task.cancel()
  if tasks:
    await asyncio.gather(*tasks, return_exceptions=True)


async def recover_interrupted() -> None:
  """Reconcile orphan claims at boot and on the existing recovery cadence.

  A failed outcome commit can orphan a claim without a process restart. The
  same conservative recovery applies: fixed unknown result, never execution.
  """
  try:
    with SessionLocal() as db:
      rows = list(db.query(models.SavedSecureInput.request_id, models.SavedSecureInput.chat_id).filter_by(status="consuming"))
  except Exception:
    log.error("Could not read interrupted sealed operations; recovery remains pending")
    return
  for request_id, chat_id in rows:
    try:
      await finish(chat_id, request_id, "interrupted", "interrupted", orphan_only=True)
    except Exception:
      # One inaccessible chat must not take down the whole instance. A
      # consuming claim stays non-repeatable; committed queues have their
      # ordinary durable recovery owner even if a wake was interrupted.
      log.error("Could not reconcile an interrupted sealed operation")
