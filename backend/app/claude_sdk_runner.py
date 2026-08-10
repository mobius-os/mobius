"""Claude SDK turn runner for Möbius.

This module isolates the Claude Agent SDK integration behind one
function that executes exactly one Möbius chat turn and publishes the
same event shapes the rest of the backend already understands.

Design choices:

- The runner stays in the SDK's default permission mode and registers a
  dummy PreToolUse keepalive hook so `can_use_tool` still fires.
  `can_use_tool` auto-approves every tool except `AskUserQuestion`, which
  becomes an explicit partner choice in the Möbius UI. Using
  `permission_mode="bypassPermissions"` would skip `can_use_tool` and
  break that interception.
- `ClaudeSDKClient` is used instead of one-shot `query()` because
  Möbius needs the bidirectional control surface: explicit `connect()`,
  `query()`, streaming `receive_response()`, and external
  `interrupt()` support for Stop.
- `AskUserQuestion` is intercepted via the SDK's `can_use_tool`
  callback (NOT PreToolUse/PostToolUse hooks). The callback parks a
  future in the shared `pending_questions` registry, broadcasts the
  question event, and awaits the user's answer. When the answer
  arrives (POST /messages with body.answers, resolved by
  routes/chats_stream.py), the callback returns
  `PermissionResultAllow(updated_input={"questions": ..., "answers": ...})`
  — the SDK then runs `AskUserQuestion` with the answers as input and
  the tool's headless implementation echoes them back as the result
  the model sees. PreToolUse/PostToolUse was explored and rejected:
  the SDK does NOT fire PostToolUse for AskUserQuestion in headless
  mode, so the two-hook flow never worked.
- Stop and steer support is wired through the shared runner registry. The
  caller looks up the registered `ActiveClaudeClient` handle and
  interrupts the live SDK client while this runner keeps draining
  `receive_response()` until the terminal result arrives.
- `system_prompt` is passed on EVERY turn, not just the first. The
  installed SDK transport
  (`claude_agent_sdk/_internal/transport/subprocess_cli.py:227-228`)
  serializes `system_prompt is None → --system-prompt ""`, which on
  resume silently wipes the original session's system prompt. Since
  ClaudeAgentOptions defaults `system_prompt` to `None`, omitting the
  kwarg has the same effect. Always passing `skill_text` keeps the
  skill load-bearing across resumes and matches our "skill is always-
  on" contract — passing the same text on resume is a no-op; passing
  updated text after a deploy correctly updates the resumed session.
- We deliberately pass `skill_text` as a custom string (not
  `SystemPromptPreset{append=skill_text, exclude_dynamic_sections=True}`).
  The preset+append form would layer Claude Code's default
  engineer-facing preset on top of our Möbius skill — adding
  generic tool-use / communication guidance that our skill already
  defines in Möbius-specific terms (and sometimes contradicts).
  `exclude_dynamic_sections` only applies with the default preset
  (the CLI ignores it with `--system-prompt`, per the CLI's own
  `--help`), so for our custom-string path it would be a no-op
  even if we set it. Möbius owns its system prompt end-to-end;
  the skill is the contract, not a layer on top of someone else's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shutil
from collections import deque
from contextlib import ExitStack
from typing import Any

from claude_agent_sdk import (
  ClaudeAgentOptions,
  ClaudeSDKClient,
  HookMatcher,
  ProcessError,
)
from claude_agent_sdk.types import (
  AssistantMessage,
  PermissionResultAllow,
  PermissionResultDeny,
  RateLimitEvent,
  ResultMessage,
  StreamEvent,
  UserMessage,
)

from app import activity
from app.claude_events import _clip_task_text, dispatch_sdk_message
from app.claude_sdk_contract import transport_exit_error, transport_process_pid
from app.process_groups import (
  isolated_process_group_id,
  lower_process_group_priority,
  terminate_process_group,
)
from app.question_bridge import (
  QuestionOverlapError,
  QuestionPersistenceError,
  park_question,
)
from app.runner_registry import RunnerKind, registry
from app.runtime_types import RunnerResult

log = logging.getLogger(__name__)

_CLAUDE_CLI = "/usr/local/bin/claude"
_ISOLATED_CLAUDE_CLI = "/app/scripts/claude-isolated"


def _claude_cli_path() -> str:
  """Use the baked process-group wrapper when its runtime is available."""
  if (
    os.path.isfile(_ISOLATED_CLAUDE_CLI)
    and os.access(_ISOLATED_CLAUDE_CLI, os.X_OK)
    and shutil.which("setsid")
  ):
    return _ISOLATED_CLAUDE_CLI
  return _CLAUDE_CLI


async def _drain_background_tasks(client, bc, inflight, session_id, chat_id):
  """Let still-running background subagents/tasks finish before the turn reaps.

  The main agent's terminal ResultMessage ends the TURN, not the background
  tasks it spawned — the SDK keeps the channel open past the result frame
  ("result frame ends one turn, not necessarily the run"). The turn-end
  process-group reap would kill any task still running and lose its work. So at a
  genuine terminal we keep the connected client and keep reading its stream,
  dispatching each event and clearing tasks as they settle, until every tracked
  task finishes; then the caller returns into the normal reap.

  Wait for the real work, not an arbitrary clock. Only drainable delegated-agent
  tasks are tracked (``dispatch_sdk_message`` maintains ``inflight``), and those
  reliably reach a terminal status, so this returns as soon as the subagents
  actually settle — there is deliberately no time cap. Fail-safe: on ANY stream
  error it returns so the reap proceeds; a real Stop raises CancelledError
  (BaseException), which propagates untouched and aborts the wait at once.
  """
  try:
    async for sdk_msg in client.receive_messages():
      dispatch_sdk_message(sdk_msg, bc, session_id, inflight)
      if not inflight:
        return
  except Exception as exc:
    log.warning(
      "background-task drain ended early chat_id=%s inflight=%d: %s",
      chat_id, len(inflight), exc,
    )


def _claude_process_group_id(client: ClaudeSDKClient) -> int | None:
  """Return the isolated Claude CLI PGID through the SDK transport."""
  pid = transport_process_pid(client)
  pgid = isolated_process_group_id(pid)
  if pgid is None and isinstance(pid, int):
    log.error(
      "Claude CLI process group is not isolated pid=%s; "
      "descendant cleanup disabled",
      pid,
    )
  return pgid


def _claude_process_was_force_stopped(client: ClaudeSDKClient) -> bool:
  """Whether the SDK transport recorded one of Möbius's stop signals.

  The SDK's background reader converts ``ProcessError`` into a plain
  ``Exception`` before it reaches ``receive_response()``. Keep this predicate
  narrow by reading the still-typed transport outcome and accepting only the
  TERM/KILL return codes ``terminate_process_group`` can cause. A different
  typed process failure that merely races a Stop must remain visible to the
  owner. The transport is already an intentional private SDK seam here
  (``_claude_process_group_id`` reads its child pid); if the SDK changes shape,
  this fails closed and the error remains visible.
  """
  exit_error = transport_exit_error(client)
  return (
    isinstance(exit_error, ProcessError)
    and exit_error.exit_code in (-signal.SIGTERM, -signal.SIGKILL)
  )


def _terminate_claude_process_group(pgid: int | None) -> bool:
  return terminate_process_group(
    pgid,
    logger=log,
    label="Claude descendant",
  )

# The SDK's 1 MiB default is smaller than a single base64-encoded screenshot
# tool result, so the subprocess transport can reject an otherwise healthy
# turn before Möbius sees the message. This is a per-record ceiling, not a
# preallocation: keep it bounded while leaving enough room for image tools.
_CLAUDE_SDK_MAX_BUFFER_SIZE = 10 * 1024 * 1024


def _claude_thinking_config(model: str | None) -> dict[str, str] | None:
  """Request displayable thinking summaries on adaptive-thinking models."""
  mid = (model or "").lower()
  if not mid:
    return {"type": "adaptive", "display": "summarized"}
  adaptive_prefixes = (
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-sonnet-4-7",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-fable-5",
    "claude-mythos",
  )
  if mid.startswith(adaptive_prefixes):
    return {"type": "adaptive", "display": "summarized"}
  return None


def _should_retry_without_model(error_text: str | None) -> bool:
  """True when Claude rejected the explicit model selection."""
  if not error_text:
    return False
  text = error_text.lower()
  return (
    "selected model" in text
    and "may not exist or you may not have access" in text
  )


async def _persist_session_id(db, chat_id: str, session_id: str | None) -> None:
  """Best-effort early persistence for provider resume continuity.

  Advances two records from the same sighting: the CURRENT-session pointer on
  the chat row (via the single-writer actor, since it lives on the hot Chat
  row), and the append-only ``chat_session_links`` map. The link write goes
  through ``record_session_link_async``, which commits on its OWN short-lived
  session in a worker thread — NOT the runner's ``db`` (which chat.py closes
  before the long run, and which the later ``Chat.session_id`` save reuses), so
  a link-write stall or failure can neither block the loop nor poison that
  shared session. The ``db`` argument is unused here now, kept for the call
  signature. The link record is what survives the provider switch / session
  reset that later NULLs ``Chat.session_id``.
  """
  if not chat_id or not session_id:
    return
  try:
    from app.chat_writer import PersistSessionId, await_ack, get_writer
    from app.session_links import record_session_link_async
    ack = get_writer().submit(
      PersistSessionId(chat_id=chat_id, session_id=session_id)
    )
    await await_ack(ack)
    await record_session_link_async("claude", session_id, chat_id)
  except Exception:
    log.warning(
      "Claude session id persistence failed chat_id=%s session_id=%s",
      chat_id,
      session_id,
      exc_info=True,
    )


def _resumable(
  session_id: str | None, cwd: str, config_dir: str | None = None
) -> bool:
  """True iff a transcript .jsonl for session_id exists for this cwd.

  `claude --resume <id>` reads the transcript the CLI stored under
  `<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>/<id>.jsonl`, where the
  project dir encodes the cwd by stripping the leading slash and
  replacing every `/` with `-` (cwd `/data` -> `-data`, cwd
  `/data/apps/news-2` -> `-data-apps-news-2`). A stored id can fail to
  resolve two ways, both of which make `--resume` die "No conversation
  found" (exit 1): a pre-fix PHANTOM id (the codex plugin's SessionStart
  hook minted an id that got a `session-env/<id>` dir but never a
  transcript), or a real id whose transcript the CLI's ~30-day cleanup
  has since deleted. Callers use this to fall back to a DB-transcript
  reseed instead of letting the turn hard-fail.

  The `-data` derivation is verified against prod: every stored
  session id that resolves on disk lives under `projects/-data/`, and
  `fork-chat.sh` resumes chat sessions from the same `/data` cwd.
  """
  if not session_id:
    return False
  base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
  if not base:
    return False
  proj = "-" + cwd.strip("/").replace("/", "-")
  return os.path.isfile(
    os.path.join(base, "projects", proj, f"{session_id}.jsonl")
  )


class ActiveClaudeClient:
  """Stop/steer handle registered for SDK-backed Claude turns.

  Ordering contract: `interrupt()` signals the SDK and then awaits
  `_finished`, which the runner resolves ONLY after `client.disconnect()`
  returns. Callers (stop_chat / stop_chat_for) therefore block until
  the SDK subprocess is fully torn down, so a late `bc.publish(done)`
  from the runner cannot land after `bc.mark_completed()` has already
  closed the broadcast for live SSE subscribers.
  """

  def __init__(self, client: ClaudeSDKClient, chat_id: str):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self._client = client
    self._process_group_id: int | None = None
    # Never signal a retained PGID twice; the kernel can eventually reuse it
    # after the first hard stop.
    self._force_stop_started = False
    # Set synchronously before interrupt()'s first await. Claude reports both a
    # Möbius-requested Stop and an unexpected provider interruption as an
    # error-shaped ResultMessage, so the runner needs this local ownership fact
    # to keep a deliberate Stop from overwriting its resumable pause note.
    self._interrupt_requested = False
    # FIFO of mid-turn steer texts: two rapid sends must both reach Claude
    # (both are already persisted to the transcript), so a single slot would
    # silently drop the first. The runner drains the whole list on interrupt.
    self.pending_steer: list[str] = []
    # Transcript-side payload for the buffered steers: the steered user rows +
    # any queued rows they consume. The RUNNER drives the transcript split
    # (seal the pre-interrupt A1, append these user rows, reset the sink for
    # A2) when the interrupted turn ends — the first point the true A1/A2 cut
    # is known. The old route-driven split ran at HTTP arrival, before A1 had
    # streamed, so it sealed an empty A1 and the real A1 then merged with A2
    # after the steered row on reload (Q1, Q2, A1A2 instead of Q1, A1, Q2, A2).
    self._steer_user_msgs: list[dict] = []
    self._steer_consume_cids: list[str] = []
    # One interrupt is in flight: `steer()` has signalled `interrupt()` but the
    # terminal ResultMessage that ends the interrupted turn has not arrived
    # yet. Guards the steer cut so a second steer arriving in the drain window
    # can't fire a duplicate interrupt before the SDK has closed the first; the
    # runner clears it on the terminal result and drains every buffered steer
    # text together. Stop's hard `interrupt()` does not consult this — Stop
    # always cuts immediately.
    self._interrupt_in_flight = False
    self._finished: asyncio.Future[None] = (
      asyncio.get_running_loop().create_future()
    )

  async def steer(
    self,
    text: str,
    user_msgs: list[dict] | None = None,
    consume_pending_cids: list[str] | None = None,
  ) -> bool:
    """Fires an immediate soft interrupt so a steer lands right away.

    Claude's SDK cannot append to an in-flight tool loop, and its only
    mid-turn lever is `interrupt()`. We record the redirect text, then
    interrupt the live turn NOW — on the same connected client — instead of
    waiting for the next completed content block. The runner's
    `receive_response()` loop can be parked inside a long-running tool call,
    where no `AssistantMessage` arrives for seconds to minutes; deferring the
    cut to that boundary is what made steering land unpredictably. Interrupting
    immediately aborts the in-flight step (its work is redone once the model
    reads the steer) and may seal a partial block, matching Codex's immediate
    steer. The interrupt's terminal ResultMessage flows to the existing
    drain-then-requery path in the runner, which seals the pre-steer A1,
    appends the steered rows, and re-queries the buffered text on the same
    session (preserving context). Two rapid steers collapse into one interrupt
    via `_interrupt_in_flight` and drain together. (Stop is the separate
    teardown path — see `interrupt()`.)

    `user_msgs` / `consume_pending_cids` are the transcript-side payload the
    runner replays into `sink.split_for_steer` when the interrupted turn
    ends (seal A1, append these rows, reset for A2). They are buffered here
    rather than split at the route so A1 is the real pre-interrupt text.
    """
    if self._finished.done():
      return False
    # Dedup before buffering. A repeated force-steer of the SAME still-live
    # pending row (common when the client retries a send right after an
    # interrupt) re-delivers the same user_msg / consume cid here. The queued
    # row is not consumed until the interrupt-boundary drain, so without this
    # guard the buffer grows to [msgA, msgA] and the writer persists the row
    # twice. A queued row carries a stable `cid` (see schemas.SendMessage.cid),
    # so keying on cid drops only true re-deliveries and never a genuinely
    # distinct send — even two sends with identical text carry distinct cids.
    #
    # The provider-facing `text` follows the SAME boundary: when every
    # delivered row is a cid-duplicate, the whole call is a re-delivery and
    # the redirect text must not queue a second time — otherwise the durable
    # transcript holds one user message while Claude receives it twice.
    appended_any = False
    if user_msgs:
      from app.chat_writer import cid_of
      buffered_cids = {cid_of(m) for m in self._steer_user_msgs}
      for m in user_msgs:
        mcid = cid_of(m)
        if mcid is not None and mcid in buffered_cids:
          continue
        self._steer_user_msgs.append(m)
        buffered_cids.add(mcid)
        appended_any = True
    if user_msgs and not appended_any:
      return True
    self.pending_steer.append(text)
    if consume_pending_cids:
      buffered_consume = set(self._steer_consume_cids)
      for cid in consume_pending_cids:
        if cid in buffered_consume:
          continue
        self._steer_consume_cids.append(cid)
        buffered_consume.add(cid)
    # Fire the interrupt immediately (soft interrupt on the same connected
    # client) so the steer lands now instead of at the next content-block
    # boundary. The `_interrupt_in_flight` guard collapses two rapid steers
    # into a single interrupt — both texts drain together when the terminal
    # ResultMessage arrives. A racing hard Stop resolves `_finished`, so the
    # guard no-ops rather than interrupting a torn-down client.
    if not self._interrupt_in_flight and not self._finished.done():
      self._interrupt_in_flight = True
      await self._client.interrupt()
    return True

  async def interrupt(self) -> None:
    """Interrupts the live run and waits for runner-side drain.

    Bounds the `_finished` wait at 5s as a defense-in-depth so a
    wedged runner (one that never reaches its `finally` block) can't
    hang Stop indefinitely. `chat.py:stop_chat_for` adds its own 2s
    bound at the call site; this inner timeout protects any other
    direct caller.

    Stop is the hard, immediate-cut path: it drops the buffered steer
    ENTIRELY — the provider-facing text (`pending_steer`, so no requery fires
    for work the user just abandoned) AND the transcript-side rows
    (`_steer_user_msgs` + `_steer_consume_cids`, so the
    turn-end seal appends nothing).

    Both halves have to go, because Stop OWNS those rows from here on: a
    deferred steer's row is still a durable entry in `chat.pending_messages`
    (the split that would consume it never ran), `/chat/stop` clears that queue
    and reports the cleared cids, and the client re-sends exactly them as one
    fresh turn. Leaving the rows buffered meant the dying turn's seal appended
    the same row into the transcript while the client re-sent it — the row
    appeared twice, once interrupted and once answered. Nothing is lost by
    dropping them here: they were never in the transcript, and Stop's own
    clear-and-resend path is what preserves them.
    """
    self._interrupt_requested = True
    self.pending_steer = []
    self._steer_user_msgs = []
    self._steer_consume_cids = []
    await self._client.interrupt()
    try:
      await asyncio.wait_for(asyncio.shield(self._finished), timeout=5.0)
    except asyncio.TimeoutError:
      import logging
      logging.getLogger("moebius.chat").warning(
        "ActiveClaudeClient._finished never resolved within 5s; "
        "runner is wedged",
      )

  @property
  def interrupt_requested(self) -> bool:
    return self._interrupt_requested

  async def stop(self, timeout: float = 2.0) -> bool:
    """Interrupts the SDK run and waits up to `timeout` seconds."""
    try:
      await asyncio.wait_for(self.interrupt(), timeout=timeout)
      return True
    except asyncio.CancelledError:
      raise
    except asyncio.TimeoutError:
      log.warning(
        "Claude SDK stop timed out chat_id=%s", self.chat_id,
      )
      return False
    except Exception:
      log.exception(
        "Claude SDK stop failed chat_id=%s", self.chat_id,
      )
      return False

  def set_process_group_id(self, pgid: int | None) -> None:
    self._process_group_id = pgid

  async def force_stop(self, timeout: float = 5.0) -> bool:
    """One-shot hard stop for this turn's verified private process group."""
    if not self._force_stop_started:
      if self._process_group_id is None:
        return False
      self._force_stop_started = True
      await asyncio.to_thread(
        _terminate_claude_process_group, self._process_group_id,
      )
    try:
      await asyncio.wait_for(
        asyncio.shield(self._finished), timeout=max(0.0, timeout),
      )
      return True
    except asyncio.CancelledError:
      raise
    except asyncio.TimeoutError:
      log.warning(
        "Claude SDK hard stop did not finish chat_id=%s", self.chat_id,
      )
      return False

  def mark_finished(self) -> None:
    """Resolves the stop waiter once the runner is fully drained."""
    if not self._finished.done():
      self._finished.set_result(None)


def _steer_redirect_message(text: str) -> str:
  """Frames a Claude steer as a redirect on the still-connected client."""
  return (
    "The user added this while you were working. Incorporate it and "
    "continue the same task:\n\n"
    f"{text}"
  )


async def steer_into_active_turn(
  chat_id: str,
  text: str,
  user_msgs: list[dict] | None = None,
  consume_pending_cids: list[str] | None = None,
) -> bool:
  """Interrupts a live Claude SDK turn so it can resume with `text`.

  `user_msgs` / `consume_pending_cids` are buffered on the handle so the
  runner can seal A1 and append the steered rows at the interrupt boundary;
  see `ActiveClaudeClient.steer`.
  """
  handle = registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK)
  if not isinstance(handle, ActiveClaudeClient):
    return False
  return await handle.steer(text, user_msgs, consume_pending_cids)


async def _seal_steer_split(bc, active_client, chat_id: str) -> None:
  """Seal the pre-interrupt A1 and append the buffered steered user row(s).

  Called at each requery boundary (so A1 is sealed before the answer A2
  streams) AND unconditionally in the turn-end `finally` (so a steer that was
  buffered but never sealed — an exception/early-return before the requery — is
  still persisted rather than discarded with the handle). A hard Stop is NOT one
  of those cases: `interrupt()` drops the buffered rows outright because Stop's
  clear-and-resend path owns them from that point (see `interrupt`), so the
  finally finds an empty buffer and appends nothing to the turn it just killed.

  A1 is the sink's accumulated pre-interrupt content — complete once the turn
  closes — so `split_for_steer` seals it as its own message, appends the steered
  row(s) after it, and resets the sink so A2 lands fresh: reload order Q1, A1,
  Q2, A2.

  This is the fix for the steer-merge: the route cannot know where A1 ends (at
  HTTP arrival A1 has not streamed yet, so a route-side split sealed an empty
  A1 and the real A1 then merged with A2 after the steered row), but the runner
  does. `bc` is the live `_ChatEventSink`; a non-sink `bc` (legacy path / a
  test double) cannot persist here and drops the buffered rows.

  This is ALSO where the client's cut lands. `steered_into_turn` is the client's
  only "seal the live stream here and re-base it" signal, so it must be
  published from the same instant as the durable split — deferring the split to
  here while the route published the cut at HTTP arrival meant every block
  streamed in between was BOTH folded into the sealed A1 and left at the head of
  the client's re-based stream, painting twice for the rest of the turn. No
  split (no live sink, or a failed write) publishes no cut: the client must
  never re-base earlier than the server's actual seal. A split with no
  publisher is the one asymmetric case — it commits and logs, see below.

  Durability contract (adversarial-review hardening):
  - The rows are snapshotted BEFORE the await and only the snapshotted count is
    removed on success, so a second steer landing during `split_for_steer`'s
    actor round-trips is not wiped (it survives for the next call / the
    finally).
  - On a persistence FAILURE the buffer is left intact so the turn-end
    `finally` retries the write; the rows are not silently dropped after the
    client was already told the steer landed. A persistent failure means the
    writer is down (the whole turn is failing to persist), not a steer-specific
    loss.
  """
  rows = list(active_client._steer_user_msgs)
  if not rows:
    return
  consume = list(active_client._steer_consume_cids)
  split = getattr(bc, "split_for_steer", None)
  # Resolve the client-facing publisher BEFORE committing anything. Take the
  # broadcast off the SINK rather than re-resolving it by chat_id: the cut
  # belongs in the same event log that carries A1's blocks (so a reconnect
  # replays the boundary at its true position), and a lookup could hand back a
  # successor turn's broadcast when this runs from the turn-end `finally`.
  #
  # A missing publisher does NOT abort the split: the rows are already durable
  # in the pending queue and the client was told the steer landed, so
  # persistence wins over notification. It does mean this seal produces no cut,
  # leaving the client's live stream un-rebased until its next authoritative
  # fetch — a real divergence, so it is logged loudly here rather than returned
  # away silently after the write has already committed.
  raw_bc = getattr(bc, "bc", None)
  if raw_bc is not None and not callable(getattr(raw_bc, "publish", None)):
    raw_bc = None
  if split is not None and raw_bc is None:
    log.error(
      "steer split has no broadcast to publish the cut on chat_id=%s; "
      "the transcript will be split but the client stream cannot re-base "
      "until it refetches", chat_id,
    )
  if split is None:
    # No live sink (legacy/test caller): there is no streamed A1 to seal
    # against and no way to persist here — drop the buffer.
    active_client._steer_user_msgs = active_client._steer_user_msgs[len(rows):]
    active_client._steer_consume_cids = (
      active_client._steer_consume_cids[len(consume):]
    )
    return
  try:
    stored_result = await split(rows, consume)
  except Exception:
    # Leave the buffer intact so the turn-end finally retries the write.
    log.exception(
      "steer split failed chat_id=%s; will retry at turn end", chat_id,
    )
    return
  # Success: remove ONLY the rows just sealed; a steer that landed during the
  # await was appended after them and must survive.
  active_client._steer_user_msgs = active_client._steer_user_msgs[len(rows):]
  active_client._steer_consume_cids = (
    active_client._steer_consume_cids[len(consume):]
  )
  # Publish the cut now that A1 + Q2 are committed, on the broadcast resolved
  # above. No await separates the split from this publish, so no continuation
  # block can slip in front of it.
  if raw_bc is None:
    return
  from app.chat_event_sink import steered_into_turn_event

  stored_messages = (
    stored_result.get("stored_messages") if isinstance(stored_result, dict)
    else None
  )
  if not isinstance(stored_messages, list) or not stored_messages:
    # The writer echoes the rows it stored; fall back to the rows we handed it
    # so an older/leaner ack shape still produces a well-formed cut.
    stored_messages = rows
  try:
    raw_bc.publish(steered_into_turn_event(stored_messages))
  except Exception:
    # The split already COMMITTED, so failing to announce it is a notification
    # loss, not a durability one — same asymmetry as the missing-publisher case
    # above. Swallow and log: this function is awaited from the turn-end
    # `finally`, where a raise would skip unregistering the handle and
    # disconnecting the client, leaving the chat looking permanently live.
    log.exception(
      "publishing the steer cut failed chat_id=%s; the split committed but the "
      "client stream cannot re-base until it refetches", chat_id,
    )


def _skill_file_read_name(
  tool_name: str, input_data: Any, cwd: str,
) -> str:
  """Returns the skill name when a Read targets a Möbius skill file.

  The in-product agent loads its skills by Reading
  `<data_dir>/shared/skills/<name>.md` (flat) or
  `<data_dir>/shared/skills/<name>/SKILL.md` (the external
  directory convention installed skills use) — on the default posture
  (skills_enabled off) the SDK Skill tool is never offered, so the
  Read input is the only place skill loads are actually observable.
  The match is purely lexical (normpath, no filesystem access) and
  returns "" for anything that isn't a direct skill-file read. A
  relative path is resolved against the turn's cwd: the agent runs
  with cwd=/data, so `shared/skills/example.md` is the same load.
  Deeper resource reads inside a skill directory deliberately do NOT
  count as loads — only the SKILL.md entry document does — and the
  generated `skills-index.md` is the index, not a skill.
  """
  if tool_name != "Read" or not isinstance(input_data, dict):
    return ""
  raw = input_data.get("file_path")
  if not isinstance(raw, str) or not raw.strip():
    return ""
  path = raw.strip()
  if not os.path.isabs(path):
    path = os.path.join(cwd or "/", path)
  path = os.path.normpath(path)
  from app.config import get_settings
  skills_dir = os.path.normpath(
    os.path.join(get_settings().data_dir, "shared", "skills")
  )
  parent, filename = os.path.split(path)
  if parent == skills_dir and filename.endswith(".md"):
    from app.skills import GENERATED_INDEX_STEMS

    name = filename[: -len(".md")]
    return "" if name in GENERATED_INDEX_STEMS else name
  grandparent, dirname = os.path.split(parent)
  if grandparent == skills_dir and filename.upper() == "SKILL.MD" and dirname:
    return dirname
  return ""


def observe_skill_file_read(
  tool_name: str,
  input_data: Any,
  *,
  bc,
  chat_id: str,
  cwd: str,
  tool_use_id: str | None = None,
) -> None:
  """Fire-and-forget skill observability for skill-file Reads.

  Publishes the same targeted `skill_loaded` event + activity record the Skill
  tool path emits (see the dispatch below), so the activity log's
  most-used-skills cross-check sees Read-based loads too — before
  this, the cross-check endpoint returned empty every night because
  the agent never goes through the Skill tool. Never raises: a broken
  broadcast or a full disk must not block or fail the tool call being
  intercepted.
  """
  try:
    skill = _skill_file_read_name(tool_name, input_data, cwd)
    if not skill:
      return
    bc.publish({
      "type": "skill_loaded",
      "skill": skill,
      **({"tool_use_id": tool_use_id} if tool_use_id else {}),
    })
    activity.log_skill_load(chat_id, skill)
  except Exception:
    log.debug("skill_loaded read observability failed", exc_info=True)


# Injected when the owner picks the "ultracode" effort tier. Ultracode (xhigh
# effort + standing dynamic-workflow orchestration) is armed the DOCUMENTED way
# — the CLI's `ultracode` settings flag, passed below — NOT by putting the word
# "ultracode" in the prompt. That keyword trigger (`workflowKeywordTriggerEnabled`,
# default-on) is an interactive-CLI convenience and is brittle here: a stray
# "ultracode" token in injected memory/context arms the whole Workflow fleet on a
# turn the owner never opted into (the observed "$32 for a restaurant question").
# We disable the keyword trigger and drive ultracode purely by the flag, so this
# reminder carries only behavioural guidance and deliberately contains NO arming
# keyword. Möbius's turn is one-shot (no post-turn re-invoke), so the agent must
# await its Workflow within the turn or the fleet's work is lost when the turn ends.
_ULTRACODE_REMINDER = (
  "\n\n<system-reminder>You have the Workflow tool for dynamic multi-agent "
  "orchestration this turn. Use it for substantial multi-step work; answer "
  "trivial turns directly.\n\n"
  "This runtime gives you exactly ONE turn per message and CANNOT wake you "
  "after it ends — there is no background notification and no follow-up turn. "
  "So any Workflow (or background task) you launch must be fully awaited AND its "
  "result delivered WITHIN this same turn, or the work is lost and the partner "
  "is left with a dead turn. Concretely: right after launching a Workflow, block "
  "on it here — call TaskOutput(task_id=..., block=True, timeout=600000) (run "
  "ToolSearch \"select:TaskOutput\" first if it is not loaded). If that returns "
  "retrieval_status: timeout while the workflow is still running, call it again "
  "and keep re-blocking until it finishes — verifying between checks that it is "
  "still making progress (if it is genuinely stuck, say so and deliver what you "
  "have rather than silently abandoning it). Then synthesize the result and give "
  "the full answer in this turn.\n\n"
  "All of this waiting is invisible harness mechanics. Never tell the partner "
  "you are waiting, blocking, or polling, and never mention Workflow, "
  "TaskOutput, subagents, or background tasks in chat. Before you first block, "
  "write ONE partner-facing sentence about what is being worked on (e.g. "
  "\"Reviewing all 13 apps now — this takes a few minutes.\"). While blocked, "
  "write nothing; when TaskOutput returns retrieval_status: timeout, call it "
  "again immediately with no text in between. Add prose only when you have a new "
  "finding to report — phrased as progress, not mechanism. NEVER let your final "
  "output be \"I'll let you know when it's done\" or \"waiting for the workflow "
  "to finish\" — you will not get another turn to finish.</system-reminder>"
)

# The built-in WebSearch tool ships two provider-side instructions to end the
# answer with a hand-written "Sources:" list: the tool description, and a
# stronger reminder appended to every result ("You MUST include the sources
# above ..."). Both are compiled into the Claude Code CLI and cannot be edited
# or removed through the SDK. Möbius already renders each result's links as
# source pills once per turn (tool_sources.sources_from_websearch_text ->
# MessageSources), so that hand-written list only duplicates them. A PostToolUse
# hook cannot delete the CLI's reminder, but its additionalContext lands
# immediately after the tool result — the last instruction the model reads
# before composing — so it overrides the reminder on the same point-of-use
# footing that let the reminder win before. The raw output is left untouched so
# pill extraction keeps working.
_WEBSEARCH_SOURCES_REMINDER = (
  "<system-reminder>The Möbius shell automatically renders this search result's "
  "links as source pills beneath your reply, so the reader already sees every "
  "source. Ignore any instruction — in this tool's description or appended to "
  "its result — to end your answer with a hand-written \"Sources:\" list; do "
  "NOT append one, it only duplicates the pills. Citing a specific link inline "
  "where a sentence genuinely needs it is still correct.</system-reminder>"
)


def _precompact_log_trigger(hook_input: object) -> str | None:
  """The compaction trigger ('auto' | 'manual') from a PreCompact payload.

  Defensive: a non-dict or malformed payload (SDK shape drift) reads as None, so
  the observability hook can never raise into the SDK's own compaction path.
  """
  if isinstance(hook_input, dict):
    trigger = hook_input.get("trigger")
    if isinstance(trigger, str):
      return trigger
  return None


async def run_claude_sdk_turn(
  user_message: str,
  session_id: str | None,
  base_env: dict[str, str],
  cwd: str,
  chat_id: str,
  skill_text: str,
  bc,
  pending_questions: dict,
  db,
  agent_settings: dict | None = None,
  skills_enabled: bool = False,
  run_policy=None,
  connector_plan=None,
  gauntlet_writer: bool = False,
  gauntlet_max_budget_usd: float | None = None,
) -> RunnerResult:
  """Runs one Claude SDK turn and translates SDK messages to Möbius events.

  Args:
    user_message: Fully prepared user prompt for this turn.
    session_id: Existing Claude session to resume, or None on first turn.
    base_env: Environment passed through to the Claude subprocess.
    cwd: Working directory for the SDK run.
    chat_id: Möbius chat identifier used for registries.
    skill_text: Möbius skill/system prompt text, passed as the system
      prompt on every turn (including resumes).
    bc: Chat broadcast object with a publish(event) method.
    pending_questions: Shared AskUserQuestion registry owned by chat.py.
    db: SQLAlchemy session used by runner-side persistence.
    skills_enabled: When True, offer SDK skills to the agent
      (`setting_sources` including user+project + `skills="all"`). This
      is behavior-shifting and defaults OFF so the skill-observability
      path can ship without changing what the agent does — skill loads
      are still observed (chip + activity log) whenever a skill does
      load, regardless of this flag.
    connector_plan: Detached owner-managed MCP configuration built before the
      request session was released. It is plain data and never queries SQLite.

  Returns:
    A dict containing the resulting session ID, final cost, and error.
  """
  current_session_id = session_id
  cost_usd: float | None = None

  # Canonical AskUserQuestion handling via can_use_tool, per
  # https://code.claude.com/docs/en/agent-sdk/user-input
  # The SDK does NOT fire PostToolUse for AskUserQuestion (empirically
  # confirmed). The correct injection point is `can_use_tool`: return
  # PermissionResultAllow with updated_input containing the original
  # questions array plus an `answers` dict {question_text: label}.
  # The SDK then runs the tool with that input and the model sees the
  # answers as the tool's result.
  #
  # `bypassPermissions` would skip the can_use_tool callback entirely —
  # use the documented dummy PreToolUse keepalive + default permission
  # mode so the callback fires only on AskUserQuestion (other tools are
  # auto-approved by the keepalive hook returning continue_=True).
  async def can_use_tool(
    tool_name: str,
    input_data: dict[str, Any],
    context,
  ) -> PermissionResultAllow | PermissionResultDeny:
    del context
    if run_policy is not None or gauntlet_writer:
      nested_tools = {
        "Task", "TaskOutput", "TaskStop", "Workflow", "Workflows", "Agent",
        "create_goal", "update_goal", "get_goal", "request_user_input",
      }
      if tool_name in nested_tools:
        return PermissionResultDeny(
          message="Delegated child tasks cannot launch or manage other agents."
        )
      if tool_name == "AskUserQuestion":
        return PermissionResultDeny(
          message=(
            "Delegated child tasks cannot park on an owner question; return the "
            "blocker to the parent instead."
          )
        )
      if run_policy is not None and run_policy.scope == "read" and tool_name in {
        "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit",
      }:
        return PermissionResultDeny(
          message="This delegated task is read-only."
        )
      return PermissionResultAllow(updated_input=input_data)
    # Auto-approve every tool except AskUserQuestion — this preserves
    # the "trust the agent" posture (no tool gating) while still
    # intercepting AskUserQuestion for the partner UX. The callback is
    # also the canonical observation point for skill-file Reads (the
    # agent loads /data/shared/skills/*.md via Read, not the Skill
    # tool); the observe call is fire-and-forget and never blocks or
    # fails the tool.
    if tool_name != "AskUserQuestion":
      observe_skill_file_read(
        tool_name, input_data, bc=bc, chat_id=chat_id, cwd=cwd,
        tool_use_id=getattr(context, "tool_use_id", None),
      )
      return PermissionResultAllow(updated_input=input_data)

    questions = input_data.get("questions", [])
    if not isinstance(questions, list):
      questions = []

    try:
      answers = await park_question(
        chat_id=chat_id,
        questions=questions,
        bc=bc,
        pending_questions=pending_questions,
      )
    except QuestionOverlapError as exc:
      return PermissionResultDeny(
        message=str(exc)
      )
    except QuestionPersistenceError as exc:
      log.error(
        "AskUserQuestion save-before-broadcast failed chat_id=%s: %s",
        chat_id, exc,
      )
      return PermissionResultDeny(
        message=(
          "Could not save the question (persistence unavailable); not "
          "asking. Please try again."
        )
      )

    except asyncio.CancelledError:
      return PermissionResultDeny(
        message="AskUserQuestion cancelled."
      )

    # Per docs: return updated_input with BOTH the original questions
    # array AND an answers dict {question_text: selected_label}.
    # The SDK passes this through as the tool input; AskUserQuestion's
    # implementation in headless mode echoes the answers back as the
    # tool result the model sees.
    return PermissionResultAllow(
      updated_input={
        "questions": questions,
        "answers": answers,
      }
    )

  # Required workaround per the SDK docs: a dummy PreToolUse hook
  # returning continue_=True keeps the stream open so can_use_tool can
  # be invoked. Without this, the stream closes before the callback
  # fires. See https://code.claude.com/docs/en/agent-sdk/user-input
  async def keepalive_hook(
    hook_input: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
  ) -> dict[str, Any]:
    del hook_input, tool_use_id, context
    return {"continue_": True}

  # The Claude SDK fires PreCompact before it auto- or manually compacts the
  # running session. Möbius does not influence that memory-management action;
  # it publishes a small product event so the moment is visible in the same
  # timeline position as Codex's ContextCompactedNotification, then logs it for
  # operators too.
  # Returns continue_=True — the established "observe and proceed" shape in this
  # file — so compaction is never blocked.
  async def precompact_hook(
    hook_input: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
  ) -> dict[str, Any]:
    del tool_use_id, context
    trigger = _precompact_log_trigger(hook_input)
    log.info(
      "Claude context compacted for chat %s (trigger=%s)",
      chat_id, trigger,
    )
    try:
      event = {"type": "context_compacted", "provider": "claude"}
      if trigger is not None:
        event["trigger"] = trigger
      bc.publish(event)
    except Exception:
      # Visibility must never interfere with the provider's own compaction.
      log.warning(
        "Claude context-compaction marker failed for chat %s",
        chat_id,
        exc_info=True,
      )
    return {"continue_": True}

  # Fires after every WebSearch result. Injects Möbius's own point-of-use
  # instruction (see _WEBSEARCH_SOURCES_REMINDER) so the model does not append a
  # duplicate hand-written "Sources:" list on top of the shell's pills. Leaves
  # the raw output alone (no updatedToolOutput) so pill extraction is unaffected.
  async def websearch_sources_hook(
    hook_input: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
  ) -> dict[str, Any]:
    del hook_input, tool_use_id, context
    return {
      "continue_": True,
      "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": _WEBSEARCH_SOURCES_REMINDER,
      },
    }

  # Per-chat model/effort overrides flow in via `agent_settings`
  # (merged in chat.py from global defaults + Chat.agent_settings_json).
  # Both are session-wide on the SDK but Möbius spawns one `query()`
  # per turn, so passing them here applies to *this* turn — which is
  # exactly the "apply on next turn" semantics the slash picker promises.
  _model = (agent_settings or {}).get("model") or None
  _effort = (agent_settings or {}).get("effort") or None
  # The "ultracode" tier maps to xhigh effort for the SDK flag (which only
  # accepts low/medium/high/xhigh/max) and arms the Workflow-tool
  # orchestration via the keyword trigger appended to this turn's prompt.
  _ultracode = (
    _effort == "ultracode" and run_policy is None and not gauntlet_writer
  )
  if _effort == "ultracode":
    _effort = "xhigh"
  turn_message = user_message + _ULTRACODE_REMINDER if _ultracode else user_message
  # Cross-provider mismatch defense (mirrors codex_sdk_runner).
  # Chats persisted before the snapshot logic learned to
  # provider-validate (see chat.py snapshot-on-first-send and
  # effective_agent_settings) can carry a Codex model on a Claude
  # chat. Sending that through here would surface as an obscure SDK
  # error. Quietly normalize so existing chats keep working.
  from app.providers import _model_belongs_to_other_provider, DEFAULT_MODELS
  if _model and _model_belongs_to_other_provider(_model, "claude"):
    log.warning(
      "claude turn started with non-claude model %r — normalizing to %r",
      _model, DEFAULT_MODELS["claude"],
    )
    _model = DEFAULT_MODELS["claude"]
  async def _run_once(model_override: str | None) -> RunnerResult:
    nonlocal current_session_id, cost_usd
    # Most recent provider rate-limit reset time seen this attempt (from any
    # RateLimitEvent). Threaded into the terminal result so a 429/limit kill
    # can park until the STRUCTURED reset time rather than parsing the error
    # string (design §2.4). Lives HERE, in the attempt scope where it is
    # assigned — an outer-scope init would be shadowed by that assignment and
    # read unbound on turns with no rate-limit event.
    rate_limit_resets_at = None
    # Skills are gated behind the per-owner `skills_enabled` flag. OFF
    # (the default) keeps the historical posture: `setting_sources=None`
    # means the SDK loads NO user/project settings, so the Skill tool is
    # never offered and no skill can load. ON enables user+project
    # setting sources and `skills="all"` so the agent may load any
    # installed skill — a behavior-shifting change the owner opts into.
    # Observability (the skill_loaded event + activity log) lives in the
    # tool-use dispatch and works whenever a skill loads, independent of
    # this flag.
    # Capture the CLI subprocess's stderr. The SDK transport only pipes
    # stderr when a callback is registered; without one, a CLI that dies
    # before emitting a structured result surfaces the SDK's generic
    # placeholder ("Command failed ... Check stderr output for details")
    # with zero diagnostic content. Bounded so a chatty CLI can't balloon
    # memory; each line truncated. Used only to enrich an opaque failure
    # (see the except below).
    stderr_tail: deque[str] = deque(maxlen=50)

    def _capture_stderr(line: str) -> None:
      if line:
        stderr_tail.append(line.rstrip("\n")[:500])

    options_kwargs = {
      "system_prompt": skill_text,
      "resume": session_id if session_id is not None else None,
      "cwd": cwd,
      "env": base_env,
      "setting_sources": (
        ["user", "project"] if skills_enabled else None
      ),
      "include_partial_messages": True,
      "max_buffer_size": _CLAUDE_SDK_MAX_BUFFER_SIZE,
      "can_use_tool": can_use_tool,
      "cli_path": _claude_cli_path(),
      "stderr": _capture_stderr,
      "hooks": {
        "PreToolUse": [
          HookMatcher(matcher=None, hooks=[keepalive_hook]),
        ],
        "PostToolUse": [
          HookMatcher(matcher="WebSearch", hooks=[websearch_sources_hook]),
        ],
        "PreCompact": [
          HookMatcher(matcher=None, hooks=[precompact_hook]),
        ],
      },
    }
    if run_policy is not None or gauntlet_writer:
      restricted_options = {
        "disallowed_tools": [
          "AskUserQuestion", "Task", "TaskOutput", "TaskStop",
          "Workflow", "Workflows", "Agent", "create_goal",
          "update_goal", "get_goal", "request_user_input",
        ],
        "agents": {},
      }
      if run_policy is not None:
        restricted_options.update({
        "permission_mode": (
          "plan" if run_policy.scope == "read" else "acceptEdits"
        ),
        "max_budget_usd": run_policy.max_budget_usd,
        })
      elif gauntlet_max_budget_usd is not None:
        restricted_options["max_budget_usd"] = gauntlet_max_budget_usd
      options_kwargs.update(restricted_options)
    if skills_enabled:
      options_kwargs["skills"] = "all"
    if model_override:
      options_kwargs["model"] = model_override
    thinking_config = _claude_thinking_config(model_override)
    if thinking_config is not None:
      options_kwargs["thinking"] = thinking_config
    if _effort:
      options_kwargs["effort"] = _effort
    # Arm ultracode via its documented `ultracode` settings flag; on every other
    # turn set the documented `disableWorkflows` flag so a stray "ultracode" token
    # in injected memory/context can't arm the Workflow fleet on a turn the owner
    # did not opt into (the observed "$32 for a restaurant question"). Both keys
    # are documented + stable in the Claude Code settings reference — unlike the
    # binary-only `workflowKeywordTriggerEnabled`, which we deliberately avoid.
    # Passed via --settings as inline JSON.
    _cli_settings = {"ultracode": True} if _ultracode else {"disableWorkflows": True}
    options_kwargs["extra_args"] = {"settings": json.dumps(_cli_settings)}

    # A dict-valued SDK mcp_servers option is serialized directly into the CLI
    # argv. Keep credentials out of /proc/cmdline by handing Claude an anonymous
    # 0600 config file path instead. After connect(), replace that fd with
    # /dev/null but keep its number reserved until teardown: simply closing it
    # would let the argv-visible path alias an unrelated descriptor later.
    connector_config_stack = ExitStack()
    connector_config_handle = None
    if connector_plan is not None:
      try:
        from app.connectors import claude_mcp_config_handle
        connector_config_handle = connector_config_stack.enter_context(
          claude_mcp_config_handle(connector_plan)
        )
        if connector_config_handle:
          options_kwargs["mcp_servers"] = connector_config_handle.path
      except Exception:
        connector_config_stack.close()
        connector_config_stack = ExitStack()
        log.warning(
          "Claude MCP connection injection skipped chat_id=%s",
          chat_id,
          exc_info=True,
        )
    try:
      options = ClaudeAgentOptions(**options_kwargs)
      client = ClaudeSDKClient(options)
    except Exception:
      connector_config_stack.close()
      raise

    active_client = ActiveClaudeClient(client, chat_id=chat_id)
    registry.register(active_client)

    try:
      try:
        try:
          await asyncio.wait_for(client.connect(), timeout=30.0)
        finally:
          # Claude has consumed the config by the time the control channel is
          # connected. Destroy its contents before query() gives the model a
          # shell or process-inspection tool, while reserving the argv-visible
          # fd number so it cannot alias another live descriptor.
          if connector_config_handle is not None:
            connector_config_handle.retire()
      except asyncio.TimeoutError:
        bc.publish({
          "type": "error",
          "message": "Claude SDK failed to start (connect timeout)",
        })
        return {
          "session_id": current_session_id,
          "cost_usd": None,
          "error": "connect timeout",
        }
      process_group_id = _claude_process_group_id(client)
      lower_process_group_priority(
        process_group_id,
        logger=log,
        label="Claude CLI",
      )
      active_client.set_process_group_id(process_group_id)
      await client.query(turn_message)

      # At most one automatic re-query per turn (see the synthetic-resume
      # recovery in the terminal branch below), so a genuinely-empty resume
      # can never loop.
      did_auto_requery = False
      # task_ids of background subagents/tasks still running, tracked across the
      # requery loop so a terminal result knows whether to drain before reaping.
      inflight_tasks: set = set()
      while True:
        async for sdk_msg in client.receive_response():
          # Persist the session id ONLY from real conversation messages.
          # SystemMessage and its subclasses — notably HookEventMessage,
          # which the codex plugin's SessionStart hook emits on every
          # resumed turn — carry a PHANTOM session id that gets a
          # `session-env/<id>` dir but never a transcript `.jsonl`.
          # Persisting that phantom overwrites Chat.session_id with an id
          # the CLI cannot resume, so the next turn dies "No conversation
          # found". Only StreamEvent/Assistant/User/Result carry the
          # resumable id (the same types dispatch advances the session from).
          if isinstance(
            sdk_msg,
            (StreamEvent, AssistantMessage, UserMessage, ResultMessage),
          ):
            incoming_session_id = getattr(sdk_msg, "session_id", None)
            if incoming_session_id and incoming_session_id != current_session_id:
              await _persist_session_id(db, chat_id, incoming_session_id)
          if isinstance(sdk_msg, RateLimitEvent):
            _resets = getattr(sdk_msg.rate_limit_info, "resets_at", None)
            if _resets is not None:
              rate_limit_resets_at = _resets
          current_session_id, terminal = dispatch_sdk_message(
            sdk_msg, bc, current_session_id, inflight_tasks,
          )
          if terminal is None:
            # A steer fires its interrupt synchronously in
            # ActiveClaudeClient.steer() (a soft interrupt on the same
            # connected client), so there is no boundary cut to make here — a
            # steer during a long-running tool call would otherwise sit
            # buffered until the tool returned. The interrupt's terminal
            # ResultMessage is handled below, where the drain-then-requery
            # path seals A1 and re-asks with the buffered steer text.
            continue
          if (
            active_client.interrupt_requested
            and isinstance(sdk_msg, ResultMessage)
            and sdk_msg.stop_reason == "interrupt"
          ):
            # The SDK describes a deliberate Stop with the same
            # error_during_execution envelope it uses for an unexpected
            # interruption. Preserve its usage/cost, but do not let the
            # provider-shaped error overwrite chat.py's resumable stop note.
            terminal["error"] = None
            terminal["terminal_status"] = "interrupted"
          # Terminal result: the interrupt cycle (if any) is closed, so a
          # fresh boundary cut may fire on a later turn.
          active_client._interrupt_in_flight = False
          steer_texts = active_client.pending_steer
          if steer_texts:
            # Seal A1 + append the steered row(s) BEFORE the requery so the
            # answer (A2) lands as a fresh message. The turn-end finally is the
            # durability catch-all for a steer that never reaches a requery.
            await _seal_steer_split(bc, active_client, chat_id)
            active_client.pending_steer = []
            await client.query(
              _steer_redirect_message("\n\n".join(steer_texts))
            )
            break
          # Recover a synthetic no-op RESUME. When a resumed session's prior
          # turn was interrupted (e.g. a server restart with a dangling
          # background task), the Claude CLI can spend the resumed turn
          # RECONCILING state — it writes a synthetic "No response requested."
          # close-out and returns a CLEAN terminal (is_error False) WITHOUT ever
          # running the model on the real prompt, so the sink accrues zero
          # blocks and the reply silently vanishes (proven from CLI transcripts:
          # the "continue" case, chat 04ef66df). Re-ask the same prompt ONCE to
          # force a real answer — exactly what a manual re-send recovers, and
          # what a second reconciled turn produced in the wild. Bounded by
          # did_auto_requery so a legitimately-empty resume cannot loop; if the
          # retry is also empty the finalize backstop records a retry marker.
          if (
            session_id is not None            # a resume (non-first turn)
            and not active_client.interrupt_requested  # Stop is terminal
            and not terminal.get("error")     # clean terminal (is_error False)
            and terminal.get("api_error_status") != 429  # not a bare 429/park
            and not active_client.pending_steer
            and not did_auto_requery
            and len(bc.assistant_blocks) == 0  # zero blocks: the synthetic no-op
          ):
            did_auto_requery = True
            log.info(
              "claude resume produced no reply (synthetic no-op); "
              "auto-requerying once chat_id=%s", chat_id,
            )
            await client.query(turn_message)
            break
          cost_usd = terminal.get("cost_usd")
          if rate_limit_resets_at is not None:
            terminal.setdefault("rate_limit_resets_at", rate_limit_resets_at)
          # Background subagents/tasks the agent launched but did not block on
          # are still live at this terminal result. The turn-end reap would kill
          # them mid-work; keep the connected client and drain until they finish,
          # THEN return into the reap. Skipped on a Stop — the owner asked to
          # stop, so honor it immediately. Fail-safe: on any stream error the
          # drain returns and the reap proceeds exactly as before.
          if inflight_tasks and not active_client.interrupt_requested:
            await _drain_background_tasks(
              client, bc, inflight_tasks, current_session_id, chat_id,
            )
          return terminal
        else:
          # The stream ended without a terminal ResultMessage. Any buffered
          # steer still gets delivered here (the boundary cut may not have
          # fired — e.g. a tool-only turn with no AssistantMessage text
          # block — so this is the catch-all that preserves the original
          # pending_steer→requery contract).
          active_client._interrupt_in_flight = False
          steer_texts = active_client.pending_steer
          if steer_texts:
            # Seal A1 before the requery (see the terminal-result branch); the
            # turn-end finally covers the no-requery case.
            await _seal_steer_split(bc, active_client, chat_id)
            active_client.pending_steer = []
            await client.query(
              _steer_redirect_message("\n\n".join(steer_texts))
            )
            continue
          break

      # Reaching here means the outer while broke out of the resultless-end
      # path above (line ~1237): the SDK stream ended WITHOUT a terminal
      # ResultMessage and with no pending steer to requery. A successful turn
      # returns its terminal at `return terminal` above and never falls
      # through here. So this is an error exit, not a clean turn — the CLI
      # died mid-stream (early resume failure, auth, OOM/SIGTERM kill) before
      # emitting a result. Return it error-shaped so chat.py publishes the
      # error and finalize() persists a durable error block, instead of the
      # old silent `error=None` that logged a clean $0 "done" and let the
      # just-consumed user message go unanswered with nothing to reconcile.
      if active_client.interrupt_requested:
        # A graceful interrupt may close the response stream without its usual
        # ResultMessage. The local ownership flag is enough here: there is no
        # provider error to suppress, only the resultless end caused by Stop.
        log.warning(
          "Claude response stream ended after our own stop chat_id=%s",
          chat_id,
        )
        return {
          "session_id": current_session_id,
          "cost_usd": cost_usd,
          "usage": None,
          "error": None,
          "terminal_status": "interrupted",
        }
      return {
        "session_id": current_session_id,
        "cost_usd": cost_usd,
        "usage": None,
        "error": (
          "The response ended unexpectedly before it finished "
          "(the agent stopped without returning a result). Please try again."
        ),
      }
    except Exception as exc:
      msg = str(exc)
      if (
        active_client.interrupt_requested
        and _claude_process_was_force_stopped(client)
      ):
        # force_stop() SIGTERMs the verified private CLI process group when a
        # graceful interrupt times out. The SDK's reader converts the typed
        # ProcessError into a plain Exception before it reaches us, so consult
        # the still-typed transport outcome rather than matching message text.
        # WARNING is deliberate: the owner sees a clean interrupted turn, but
        # operators retain the only evidence a coincident CLI crash leaves.
        log.warning(
          "Claude process exited during our own stop chat_id=%s: %s",
          chat_id,
          exc,
        )
        return {
          "session_id": current_session_id,
          "cost_usd": None,
          "usage": None,
          "error": None,
          "terminal_status": "interrupted",
        }
      # The SDK raises this generic placeholder when the CLI dies before a
      # structured result (early resume failure, auth, crash, OOM/SIGTERM
      # kill). Splice in the captured stderr tail ONLY then — gating on the
      # placeholder keeps _should_retry_without_model's text matching intact
      # for real structured errors. Empty tail means the process was killed
      # before writing stderr (the OOM/timeout case).
      if "Check stderr output for details" in msg:
        tail = "\n".join(stderr_tail).strip()
        if tail:
          msg = f"{msg}\nstderr (tail):\n{tail}"
        else:
          msg = (
            f"{msg}\n(no stderr captured — the CLI was likely killed "
            "before writing output, e.g. OOM or timeout)"
          )
      return {
        "session_id": current_session_id,
        "cost_usd": None,
        "usage": None,
        "error": msg,
      }
    finally:
      # Durability catch-all: persist any steer that was buffered but never
      # sealed at a requery boundary — an exception/early return above, or a
      # hard Stop that cleared pending_steer. Runs before disconnect so the
      # sink is still live. No-op when nothing is buffered (the normal path
      # sealed + cleared it already). Never raises (swallowed inside).
      await _seal_steer_split(bc, active_client, chat_id)
      current_handle = registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK)
      if current_handle is active_client:
        registry.unregister(chat_id, RunnerKind.CLAUDE_SDK)
      pending = pending_questions.get(chat_id)
      if pending is not None and not pending.future.done():
        pending.future.cancel()
      if pending is not None:
        pending_questions.pop(chat_id, None)
      try:
        await client.disconnect()
      finally:
        connector_config_stack.close()
        # The SDK closes only its direct CLI PID. Reap the verified private
        # group as a bounded backstop for tool children, and do not let a
        # repeated task cancellation skip the SIGKILL worker once it starts.
        deferred_cancel: asyncio.CancelledError | None = None
        if (
          active_client._process_group_id is not None
          and not active_client._force_stop_started
        ):
          reap_task = asyncio.create_task(asyncio.to_thread(
            _terminate_claude_process_group,
            active_client._process_group_id,
          ))
          while not reap_task.done():
            try:
              await asyncio.shield(reap_task)
            except asyncio.CancelledError as exc:
              deferred_cancel = deferred_cancel or exc
          try:
            reap_task.result()
          except Exception:
            log.warning(
              "Claude process-group cleanup failed chat_id=%s",
              chat_id,
              exc_info=True,
            )
        active_client.mark_finished()
        if deferred_cancel is not None:
          raise deferred_cancel

  result = await _run_once(_model)
  if _model and _should_retry_without_model(result.get("error")):
    log.warning(
      "Claude model %r unavailable for chat %s; retrying without explicit model",
      _model,
      chat_id,
    )
    return await _run_once(None)
  return result
