"""Loop-owned quiet-write lifetime over the shared persistence actor.

The owner attaches this AFTER provider/helper attribution and BEFORE public
broadcast. It supplies existing tool dispatch and failure presentation; neither
success nor admission calls a provider. finish() is an owned barrier, not a
fire-and-forget task that can outlive its run. Normal-chat wiring is separate.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from app.agent_write_channel import FrameDecoder, OutputChannel, ProtocolError
from app.chat_writer import (AdmitAgentWrites, ClaimAgentWrite, SettleAgentWrite,
  SealAgentWrites, InterruptAgentWrites, RecordAgentWriteFailure,
  await_ack, get_writer)


@dataclass(frozen=True)
class WriteOutcome:
  status: str
  reason: str | None = None


class AgentWriteDelivery:
  def __init__(self, *, chat_id: str, run_token: str, nonce: str,
               eligible_tools: frozenset[str],
               dispatch: Callable[[dict], Awaitable[WriteOutcome]],
               on_failure: Callable[[dict], None]):
    self.owner = {"chat_id": chat_id, "run_token": run_token}
    self.channel = OutputChannel(nonce)
    self.eligible_tools = eligible_tools
    self.dispatch = dispatch
    self.on_failure = on_failure
    self.accepting = True
    self.closing = False
    self.worker = None
    self.admissions: set[asyncio.Task] = set()
    self.wake = asyncio.Event()
    self.finish_task = None
    self.persistence_failed = False
    self.interrupted = False
    self.reported_failures = set()
    # Once attribution is lost, provisional suffixes cannot be routed safely.
    # Complete snapshots still repair presentation, without guessed identities.
    self.unattributed_provisional_seen = False

  def _task(self, coroutine):
    task = asyncio.create_task(coroutine)
    self.admissions.add(task)
    def observed(done):
      self.admissions.discard(done)
      if done.cancelled() or done.exception() is not None:
        self.persistence_failed = True
    task.add_done_callback(observed)
    return task

  async def _command(self, command):
    return await await_ack(get_writer().submit(command))

  def _failure(self, reason: str, *, stage="protocol"):
    if (stage, reason) in self.reported_failures:
      return
    self.reported_failures.add((stage, reason))
    async def record():
      try:
        result = await self._command(RecordAgentWriteFailure(**self.owner,
          stage=stage, reason=reason))
        if result["status"] != "recorded":
          self.persistence_failed = True
      except Exception:
        self.persistence_failed = True
      self.on_failure({"stage": stage, "reason": reason})
    self._task(record())

  def filter(self, event: dict) -> dict | None:
    """Return only public bytes; complete frames schedule durable admission."""
    kind = event.get("type")
    if kind == "assistant_result":
      if not self.accepting:
        return None
      if "content" not in event:
        return event  # Reference to already-sanitized transcript text.
      # A result is a projection of provider output, not fresh command intent.
      # Strip duplicate private frames without executing or rejecting them.
      parser = FrameDecoder(self.channel.nonce, validate=False)
      try:
        content = parser.feed(event["content"]) + parser.finish()
        return {**event, "content": content}
      except ProtocolError:
        return None
    if kind not in {"text", "text_final", "text_boundary"}:
      return event
    if not self.accepting:
      return None
    item = event.get("text_item_id")
    if kind == "text" and not item:
      self.unattributed_provisional_seen = True
    if kind == "text" and self.unattributed_provisional_seen:
      return None
    if kind == "text_final" and not item:
      return self._presentation_only_final(event, "unattributed_write_frame")
    if kind != "text_boundary" and not self.channel.has_capacity(item):
      # Resource exhaustion closes this command path, not ordinary answers.
      # Do not track unbounded additional streams; their complete snapshots
      # can still be sanitized and displayed without side-effect authority.
      return self._presentation_only_final(event, "write_channel_item_capacity") if kind == "text_final" else None
    try:
      if kind == "text_boundary":
        replaced = event.get("replace_text_item_id")
        if replaced:
          self.channel.replace(replaced)
        return event
      if kind == "text":
        content = self.channel.delta(item, event.get("content", ""))
      else:
        content, writes = self.channel.final(item, event.get("content", ""))
        if writes:
          if any(write.tool not in self.eligible_tools for write in writes):
            raise ProtocolError("Tool requires the ordinary result-bearing path")
          fingerprint = self.channel.reserve_admission(item, content, writes)
          if fingerprint is not None:
            # Submit synchronously: message order becomes actor order before
            # asynchronous disk acknowledgments can complete out of order.
            ack = get_writer().submit(AdmitAgentWrites(**self.owner,
              item_id=item, fingerprint=fingerprint, writes=writes))
            self._task(self._accept(ack, item, content, writes))
      return {**event, "content": content} if content or kind == "text_final" else None
    except (ProtocolError, ValueError):
      # Hide an invalid snapshot in full; never show a raw private payload.
      self._failure("invalid_or_ineligible_write_frame")
      return None

  def _presentation_only_final(self, event, reason):
    # No incremental state: ambiguous/overflow deltas cannot accumulate or be
    # mistaken for a fresh public suffix. Only a whole snapshot is presentable.
    parser = FrameDecoder(self.channel.nonce, validate=False)
    try:
      content = parser.feed(event.get("content", "")) + parser.finish()
      if parser.frames_seen:
        self._failure(reason)
      return {**event, "content": content}
    except ProtocolError:
      self._failure(reason)
      return None

  async def _accept(self, ack, item, content, writes):
    try:
      result = await await_ack(ack)
      if result["status"] in {"accepted", "rejected"}:
        # A terminal rejection is a durable receipt too, not permission to
        # reinterpret or repeatedly submit the same authoritative snapshot.
        self.channel.acknowledge(item, content, writes)
      if result["status"] != "accepted":
        self.on_failure({"stage": "admission", "reason": result.get("reason", result["status"])})
        return
      if result.get("negative_replays"):
        self.on_failure({"stage": "admission", "reason": "replayed_unsuccessful_write"})
      if self.worker is None and not self.interrupted:
        self.worker = asyncio.create_task(self._drain())
      self.wake.set()
    except Exception:
      # A timeout does not prove rollback. Retain an explicit unknown admission
      # and never reissue the command merely because the ack was lost. The
      # channel keeps pending ownership until a terminal receipt acknowledges it.
      self.persistence_failed = True
      self.on_failure({"stage": "admission", "reason": "admission_outcome_unknown"})

  async def _drain(self):
    while True:
      await self.wake.wait()
      self.wake.clear()
      while True:
        if self.interrupted:
          return
        result = await self._command(ClaimAgentWrite(**self.owner))
        if self.interrupted:
          return  # Teardown fences the claim; no effect starts after Stop.
        if result["status"] in {"empty", "stale_run"}:
          break
        if result["status"] != "claimed":
          raise RuntimeError("Quiet-write execution ownership is inconsistent")
        write = result["write"]
        try:
          outcome = await self.dispatch(write)
          if outcome.status not in {"succeeded", "failed", "unknown"}:
            raise ValueError("Invalid dispatch outcome")
        except asyncio.CancelledError:
          raise  # Teardown fences every in-flight claim as unknown.
        except Exception:
          outcome = WriteOutcome("unknown", "dispatch_outcome_unknown")
        settled = await self._command(SettleAgentWrite(**self.owner,
          operation_id=write["id"], status=outcome.status, reason=outcome.reason))
        if settled["status"] != "settled":
          raise RuntimeError("Quiet-write outcome was not persisted")
        if outcome.status != "succeeded":
          self.on_failure({"id": write["id"], "stage": "completion",
                           "reason": outcome.reason, "outcome": outcome.status})
      if self.closing:
        return

  async def finish(self, *, interrupted=False):
    # Repeated teardown attaches to the same owner, never a second worker.
    self.accepting = False
    if interrupted:
      self.interrupt()
    if self.finish_task is None:
      self.finish_task = asyncio.create_task(self._finish())
    cancelled = None
    while True:
      try:
        result = await asyncio.shield(self.finish_task)
        break
      except asyncio.CancelledError as exc:
        if self.finish_task.done():
          raise
        # Cancellation does not detach a persistence operation or worker.
        # Own teardown to completion before returning control to the run.
        cancelled = exc
        self.interrupted = True
        if self.worker is not None and not self.worker.cancelling():
          self.worker.cancel()
    if cancelled is not None:
      raise cancelled
    return result

  def interrupt(self):
    """Close admissions synchronously at the owning Stop/generation fence.

    The run's finish barrier still joins cleanup; this never detaches a task.
    """
    self.accepting = False
    self.interrupted = True
    if self.worker is not None and not self.worker.cancelling():
      self.worker.cancel()

  async def _finish(self):
    failure = None
    try:
      results = await asyncio.gather(*self.admissions, return_exceptions=True)
      if any(isinstance(result, BaseException) for result in results):
        self.persistence_failed = True
      try:
        self.channel.finish()
      except ProtocolError:
        result = await self._command(RecordAgentWriteFailure(**self.owner,
          stage="protocol", reason="unaccepted_write_item"))
        if result["status"] != "recorded":
          self.persistence_failed = True
        self.on_failure({"stage": "protocol", "reason": "unaccepted_write_item"})
      sealed = await self._command(SealAgentWrites(**self.owner))
      if sealed["status"] != "sealed":
        self.persistence_failed = True
      self.closing = True
      self.wake.set()
      if self.worker is not None and not (self.interrupted or self.persistence_failed):
        try:
          await self.worker
        except asyncio.CancelledError:
          if not self.interrupted:
            raise
    except BaseException as exc:
      failure = exc
      self.persistence_failed = True
      self.on_failure({"stage": "completion", "reason": "write_worker_outcome_unknown"})
    finally:
      # Disk/actor failure must not skip process lifetime cleanup. No branch
      # returns from this owner with a dispatch task still alive.
      if self.worker is not None:
        if not self.worker.done() and not self.worker.cancelling():
          self.worker.cancel()
        results = await asyncio.gather(self.worker, return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
          self.persistence_failed = True
      if self.interrupted or self.persistence_failed:
        try:
          await self._command(InterruptAgentWrites(**self.owner))
        except Exception as exc:
          failure = failure or exc
          self.persistence_failed = True
    if failure is not None:
      raise failure
    if self.persistence_failed:
      raise RuntimeError("Quiet-write persistence outcome is uncertain; inspect before retry")
