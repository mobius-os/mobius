"""Codex SDK turn runner for Möbius.

Codex's `TurnHandle.steer()` is the product win that motivated this
module. Unlike Claude's serial SDK flow, Codex exposes a live turn
handle that can accept in-band user steering while the current turn is
still running. Möbius can keep its existing queued-message behavior for
Claude while upgrading Codex chats to true mid-turn injection.

This module runs one Codex SDK turn, translates streamed SDK
notifications into Möbius broadcast events, relies on the SDK's
default auto-approval behavior, and stores the live `ActiveCodexTurn`
in the shared runner registry, keyed by `(chat_id, RunnerKind.CODEX_SDK)`,
so Stop and queued-message steering can reach it.

**AskUserQuestion parity is shipped via the `request_user_input`
tool.** The underlying wire surface is `item/tool/requestUserInput`
JSON-RPC requests emitted by the app-server when the model calls
the tool. `CodexClient.approval_handler` is the documented
constructor argument that receives them (public as of
openai-codex 0.134.0; was a private attribute on a less-stable
path before). Only the sync `CodexClient` accepts
`approval_handler` in its constructor. The async wrappers
(`AsyncCodex`, `AsyncCodexClient`) don't, so we set the
attribute on `codex._client._sync` after construction, targeting
the same callable slot the public constructor argument populates.
See `_install_request_user_input_handler` below.

Why we don't drop `AsyncCodex` and construct `CodexClient`
directly to pass `approval_handler` as a kwarg: doing so would
mean rebuilding everything `AsyncCodex` / `AsyncThread` /
`AsyncTurnHandle` give us for free. That list includes lazy
`start()` + `initialize()` + metadata validation, the
`ApprovalMode` enum translation to `(approval_policy,
approvals_reviewer)` via private `_approval_mode_settings`
helpers, `ThreadStartParams` / `TurnStartParams` Pydantic
construction, `_normalize_run_input` + `_to_wire_input`
translation, `register_turn_notifications` +
`next_turn_notification` polling that terminates on
`turn/completed`, and the `AsyncThread` / `AsyncTurnHandle`
context. That's ~100 lines of plumbing built on four private SDK
helpers, replacing one public-attribute set on a wrapper-internal
chain. The current pattern has the smaller fragility surface.
Revisit if `AsyncCodex` ever grows `approval_handler` in its
constructor (forwarded down to `_sync`), at which point
`_install_request_user_input_handler` collapses to a kwarg.

The tool is gated by the `default_mode_request_user_input`
feature flag (stage `UnderDevelopment`, default off), enabled via
`features.default_mode_request_user_input=true` in the
`CodexConfig.config_overrides` list. Once enabled, the model
sees `request_user_input` in its tool list and uses it the same
way Claude uses its `AskUserQuestion` tool — both producers
publish a `question` event on the Möbius wire and both wait on
the shared `_pending_questions` future for the user's answer.
The handler then translates Möbius's text-keyed answer back into
Codex's id-keyed `{answers: {qid: {answers: [label]}}}` schema.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import functools
import logging
import os
import signal
import shutil
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.codex_sdk_contract import (
  app_server_pid,
  control_client,
  goal_notification_stream_type,
  install_approval_handler,
  wait_for_goal_snapshot,
)

from app.codex_events import (
  _thinking_event,
  _codex_thinking_segment_id,
  _extract_rate_limit_reset,
  _model_dump,
  _reasoning_summary_setting,
  _stamp_tool_use_id,
  _stamp_notification_item_id,
  _subagent_lifecycle_event,
  _thread_started_lifecycle_event,
  _record_private_lifecycle,
  _public_task_event,
  _collab_reactivation_events,
  _collab_completion_events,
  _thread_status_lifecycle_event,
  _record_collab_child_links,
  _tool_start_event,
  _tool_completed_events,
  _enum_wire_value,
  _codex_user_error,
  _agent_message_phase,
  _codex_terminal_error,
  # Compatibility import for existing internal callers; implementation lives
  # beside the Codex event/observability code it supports.
  _skill_names_in_command,
  _observe_skill_reads,
  _file_change_patch_summary,
  _file_change_edit_preview,
)
from app.process_groups import lower_process_group_priority
from app.providers import get_skill_path
from app.question_bridge import (
  QuestionOverlapError,
  QuestionPersistenceError,
  park_question,
)
from app.runtime_types import RunnerResult
from app.usage_metrics import codex_cost_usd, normalize_codex_usage
from app.runner_registry import RunnerKind, registry
from app.memory_observability import record_memory_checkpoint_once

log = logging.getLogger("moebius.chat")

# The SDK implements every async protocol wait with ``asyncio.to_thread``.
# A turn parked on request_user_input can hold that worker for hours; enough
# parked chats therefore exhaust Python's small process-wide default executor
# and prevent a new Codex client from even starting. Give each SDK client a
# small owned pool: one worker may block on notifications while control/close
# retain independent progress, and unrelated application work never queues
# behind parked turns.
_CODEX_CALL_EXECUTOR_WORKERS = 3
_PROCESS_GROUP_CAPTURE_SPIN_SECONDS = 0.1
_PROCESS_GROUP_CAPTURE_POLL_SECONDS = 0.01


def _process_group_capture_delay(elapsed: float) -> float:
  """Yield eagerly during normal startup, then back off a wedged poller."""
  if elapsed < _PROCESS_GROUP_CAPTURE_SPIN_SECONDS:
    return 0
  return _PROCESS_GROUP_CAPTURE_POLL_SECONDS

# Möbius supplies the complete behavioral constitution through the thread's
# base_instructions. These overrides prevent user/project Codex configuration
# from silently adding a second instruction stack. Permission, tool, app, and
# environment blocks remain Codex-owned because they describe the runtime
# surface rather than agent behavior.
_CODEX_PROMPT_CONTROL_OVERRIDES = [
  'instructions=""',
  'developer_instructions=""',
  "project_doc_max_bytes=0",
]


def _env_flag_on(name: str, *, default: bool) -> bool:
  """Read a boolean env var: ``off``/``0``/``false``/``no``/empty disable it;
  anything else enables; unset falls back to ``default``."""
  raw = os.environ.get(name)
  if raw is None:
    return default
  return raw.strip().lower() not in ("off", "0", "false", "no", "")


def _codex_config_overrides(
  *,
  allow_questions: bool = True,
  allow_multi_agent: bool = True,
  allow_goals: bool = True,
  delegated_read_sandbox: bool = False,
) -> list[str]:
  """Assemble the Codex ``CodexConfig.config_overrides`` for a turn.

  Prompt-control overrides are unconditional: per-chat ``base_instructions``
  owns behavior, while config files and project instruction documents must not
  grow a provider-specific second constitution.

  ``request_user_input`` (AskUserQuestion parity) is on for ordinary chats and
  deliberately absent for delegated children. Multi-agent (collab /
  spawn_agent — the Codex analog of Claude's Task fleet, whose
  ``collabAgentToolCall`` items the dispatch surfaces as ordinary background
  activity) is on by DEFAULT but behind a RUNTIME kill switch: set the env var
  ``MOEBIUS_CODEX_MULTI_AGENT`` to off/0/false/no to disable it and restart
  uvicorn — a runtime rollback that needs no image rebuild, since the overrides
  are read fresh per turn.

  When enabled, the tool namespace is PINNED to ``agents``. Codex #31864: the
  pinned SDK source still DEFAULTS multi_agent_v2's spawn_agent tool to the
  ``collaboration`` namespace, which gpt-5.6 reserves, so the Responses API can
  reject the tool schema on EVERY turn (not only spawn turns). A live probe on
  0.144.5 spawned a sub-agent cleanly under the observed default, but the model
  rollout is server-side and mutable — so we do not depend on that observation:
  pinning ``agents`` (the reporter-confirmed bypass in #31864) keeps enablement
  robust to a rollout change, not just to the binary we probed. Re-run the
  delegate probe after any @openai/codex bump.
  """
  overrides = list(_CODEX_PROMPT_CONTROL_OVERRIDES)
  if allow_questions:
    overrides.append("features.default_mode_request_user_input=true")
  if allow_goals:
    # Codex owns goal durability in its thread store. Enabling the native goal
    # extension lets a new app-server resume the logical operation after
    # Möbius deliberately tears the previous process down for a restart.
    overrides.append("features.goals=true")
  if (
    allow_multi_agent
    and _env_flag_on("MOEBIUS_CODEX_MULTI_AGENT", default=True)
  ):
    overrides += [
      "features.multi_agent_v2.enabled=true",
      "features.multi_agent_v2.tool_namespace=agents",
      "suppress_unstable_features_warning=true",
    ]
  if delegated_read_sandbox:
    # The production container blocks the user/mount namespaces required by
    # Codex's default bubblewrap backend. Read Delegations still need a real
    # filesystem boundary, so use Codex's own Landlock backend rather than
    # retrying a failed command outside the sandbox. Do not use this legacy
    # backend for workspace-write policies: the pinned CLI rejects that
    # combination instead of enforcing it.
    overrides.append("features.use_legacy_landlock=true")
  return overrides


def _codex_app_server_launch_args(
  codex_bin: str | None,
  config_overrides: list[str],
) -> list[str] | None:
  """Build an app-server command isolated in its own Unix session.

  The Python SDK starts Codex with a plain ``subprocess.Popen`` and, on
  ``close()``, terminates only that one PID.  Tool commands are descendants of
  the app-server; if a shell exits while one of its children is still running,
  that child is re-parented to PID 1 and survives both the tool result and the
  SDK close.  A memory-hungry survivor can therefore outlive its chat and push
  the whole platform cgroup into OOM.

  ``setsid`` makes the app-server the leader of a private process group/session
  without requiring a change in the upstream SDK.  The terminal cleanup below
  can then signal exactly that group, never uvicorn or another concurrent chat.
  Return ``None`` outside the Linux/container runtime so local SDK resolution
  keeps its existing fallback behavior.
  """
  setsid_bin = shutil.which("setsid")
  if not codex_bin or not setsid_bin:
    return None
  args = [setsid_bin, codex_bin]
  for override in config_overrides:
    args.extend(["--config", override])
  args.extend(["app-server", "--listen", "stdio://"])
  return args


def _codex_process_group_id(
  codex: Any,
  *,
  log_unisolated: bool = True,
) -> int | None:
  """Return the isolated app-server PGID, or ``None`` if not provable.

  The SDK does not expose its child PID publicly.  We already target its sync
  client for the documented approval-handler slot, so keep this second private
  access in one defensive helper.  Refuse a group that is not led by the SDK
  process: on an old/non-``setsid`` launch that group can be uvicorn's own.
  """
  pid = app_server_pid(codex)
  if pid is None:
    return None
  try:
    pgid = os.getpgid(pid)
  except (OSError, ProcessLookupError):
    return None
  if pgid != pid or pgid == os.getpgrp():
    if not log_unisolated:
      return None
    log.error(
      "Codex app-server process group is not isolated pid=%s pgid=%s; "
      "descendant cleanup disabled",
      pid,
      pgid,
    )
    return None
  return pgid


class _CodexCallExecutor:
  """Per-client worker pool for the SDK's blocking sync protocol surface."""

  def __init__(self, chat_id: str) -> None:
    self._executor = _cf.ThreadPoolExecutor(
      max_workers=_CODEX_CALL_EXECUTOR_WORKERS,
      thread_name_prefix=f"mobius-codex-{chat_id[:8]}",
    )

  async def call(self, fn, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
      self._executor,
      functools.partial(fn, *args, **kwargs),
    )

  def close(self) -> None:
    # AsyncCodex.__aexit__ has already closed the transport before this owner
    # is released. ``wait=False`` keeps an unexpected SDK waiter from ever
    # blocking the FastAPI event loop during terminal cleanup.
    self._executor.shutdown(wait=False, cancel_futures=True)


def _install_codex_call_executor(
  codex_context: Any,
  chat_id: str,
) -> _CodexCallExecutor | None:
  """Route one AsyncCodex client's sync bridge off the default executor.

  Unit-test fakes intentionally omit the private ``_client._call_sync`` seam;
  they have no blocking SDK protocol and need no executor. The production SDK
  is pinned and this is the same wrapper-internal chain already guarded by the
  approval-handler and process-identity contract helpers.
  """
  client = getattr(codex_context, "_client", None)
  if client is None or not callable(getattr(client, "_call_sync", None)):
    return None
  owner = _CodexCallExecutor(chat_id)
  try:
    client._call_sync = owner.call
  except (AttributeError, TypeError):
    owner.close()
    log.warning(
      "Codex SDK async client does not allow an owned call executor; "
      "falling back to its default executor chat_id=%s",
      chat_id,
      exc_info=True,
    )
    return None
  return owner


async def _enter_codex_context_owned(
  codex_context: Any,
) -> tuple[Any, asyncio.CancelledError | None]:
  """Enter AsyncCodex without abandoning its threaded startup on cancel.

  The pinned SDK implements ``AsyncCodexClient.start()`` as a blocking sync
  call offloaded to a worker. Cancelling ``__aenter__`` therefore cancels only
  the awaiter; the owned worker can continue through ``Popen`` and
  publish ``_proc`` after the cancelled runner has already returned. Keep the
  complete enter operation in a runner-owned task, shield it through every
  caller cancellation, and hand the cancellation back to the caller only once
  startup has either completed or failed. That keeps PGID capture alive for
  the entire interval in which the worker can create a process.
  """
  enter_task = asyncio.create_task(codex_context.__aenter__())
  deferred_cancel: asyncio.CancelledError | None = None
  while not enter_task.done():
    try:
      await asyncio.shield(enter_task)
    except asyncio.CancelledError as exc:
      if enter_task.cancelled():
        break
      deferred_cancel = deferred_cancel or exc
    except BaseException:
      # The owned task has reached a terminal failure. Reconcile it below so
      # a caller cancellation already deferred by this loop takes precedence
      # over the later startup error instead of leaking that error as a normal
      # RunnerResult.
      break
  try:
    entered = enter_task.result()
  except asyncio.CancelledError:
    if deferred_cancel is not None:
      raise deferred_cancel
    raise
  except BaseException as exc:
    if deferred_cancel is not None:
      raise deferred_cancel from exc
    raise
  return entered, deferred_cancel


class _EnteredCodexContext:
  """Own exit for an AsyncCodex context whose enter is already owned.

  The pinned SDK delegates close to a blocking worker call (routed through the
  per-client executor above). An ordinary await lets a second caller
  cancellation cancel that awaiter while its worker is still queued or
  running, allowing process-group reap and runner return to overtake the SDK's
  direct-child ``wait()``. Keep the complete exit in a runner-owned task and
  defer every repeated cancellation until it finishes.
  """

  def __init__(self, context: Any, entered: Any) -> None:
    self._context = context
    self._entered = entered

  async def __aenter__(self) -> Any:
    return self._entered

  async def __aexit__(self, exc_type, exc, traceback) -> Any:
    exit_task = asyncio.create_task(
      self._context.__aexit__(exc_type, exc, traceback)
    )
    deferred_cancel: asyncio.CancelledError | None = None
    while not exit_task.done():
      try:
        await asyncio.shield(exit_task)
      except asyncio.CancelledError as cancel_exc:
        if exit_task.cancelled():
          break
        deferred_cancel = deferred_cancel or cancel_exc
      except BaseException:
        # Reconcile the terminal exit failure below. In particular, a caller
        # cancellation that was already unwinding the context must not be
        # replaced by a later SDK-close error.
        break
    try:
      result = exit_task.result()
    except asyncio.CancelledError as exit_cancel:
      caller_cancel = (
        exc if isinstance(exc, asyncio.CancelledError) else deferred_cancel
      )
      if caller_cancel is not None:
        raise caller_cancel from exit_cancel
      raise
    except BaseException as exit_exc:
      caller_cancel = (
        exc if isinstance(exc, asyncio.CancelledError) else deferred_cancel
      )
      if caller_cancel is not None:
        raise caller_cancel from exit_exc
      raise
    if deferred_cancel is not None:
      raise deferred_cancel
    return result


async def _capture_codex_process_group_during_start(
  codex: Any,
  stop: asyncio.Event,
) -> int | None:
  """Capture the isolated PGID while ``AsyncCodex.__aenter__`` initializes.

  ``__aenter__`` starts the subprocess and then performs a separate async SDK
  initialize request. If that request fails, the SDK closes the direct Popen
  and clears ``_proc`` before propagating the error. Polling concurrently from
  before entry captures the group in that window, so the outer runner finally
  can still reap a descendant the SDK direct-PID close left behind.

  During the tiny pre-exec window the child still has uvicorn's group. The
  identity helper therefore runs silently until ``setsid`` makes PID == PGID;
  it never returns or signals the shared group.
  """
  started_at = time.monotonic()
  while not stop.is_set():
    pgid = _codex_process_group_id(codex, log_unisolated=False)
    if pgid is not None:
      return pgid
    # Preserve zero-delay polling during the narrow Popen→initialize window:
    # an initialization failure can clear the only child PID after one loop
    # turn. If startup remains unresolved beyond that normal window, back off
    # so a wedged client cannot spin Uvicorn's event loop at 100% CPU.
    delay = _process_group_capture_delay(
      time.monotonic() - started_at,
    )
    await asyncio.sleep(delay)
  return _codex_process_group_id(codex, log_unisolated=False)


def _terminate_codex_process_group(
  pgid: int | None,
  *,
  grace_seconds: float = 0.25,
) -> bool:
  """Terminate every process left in one completed Codex turn's group.

  Called only after the SDK context has closed its app-server PID.  SIGTERM
  gives ordinary shell/browser helpers a brief exit window; SIGKILL is the
  bounded backstop for the exact failure mode this guards (a CPU-heavy child
  that ignores or never receives its parent's termination).  Returns whether
  a live group was found.  All failures are best-effort because terminal chat
  persistence must not fail merely because the OS already reaped the group.
  """
  if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
    return False
  try:
    os.killpg(pgid, signal.SIGTERM)
  except ProcessLookupError:
    return False
  except OSError as exc:
    log.warning("Codex descendant SIGTERM failed pgid=%s: %s", pgid, exc)
    return False

  deadline = time.monotonic() + max(0.0, grace_seconds)
  while time.monotonic() < deadline:
    try:
      os.killpg(pgid, 0)
    except ProcessLookupError:
      return True
    except OSError:
      break
    time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))

  try:
    os.killpg(pgid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  except OSError as exc:
    log.warning("Codex descendant SIGKILL failed pgid=%s: %s", pgid, exc)
  return True


class _BridgeError(Exception):
  """Signals the sync AskUserQuestion handler to return an error response
  to Codex rather than continuing with empty or fabricated answers.

  Module-level so test code and any future bridge paths can catch it by
  name without importing from inside a closure.
  """


class _SteerOverlapError(_BridgeError):
  """A user steer won admission before request_user_input registered.

  The Codex SDK has one stdout reader thread. It handles server requests
  synchronously, while ``AsyncTurnHandle.steer`` waits in another worker for
  that same reader to route its response. Parking the reader on a new question
  during steer would therefore deadlock the steer acknowledgement. Rejecting
  the not-yet-published tool call lets the reader route the already-admitted
  user steer; no durable question or owner answer is discarded.
  """


async def _persist_session_id(db, chat_id: str, session_id: str | None) -> None:
  """Best-effort early persistence for provider resume continuity.

  Advances two records from the same sighting: the CURRENT-session pointer on
  the chat row (via the single-writer actor, since it lives on the hot Chat
  row), and the append-only ``chat_session_links`` map (via
  ``record_session_link_async``, which commits on its own short-lived session in
  a worker thread — never the runner's ``db``, which chat.py closes before the
  long run). This funnel runs on BOTH a fresh ``thread_start`` and a
  ``thread_resume``: the caller sets ``thread.id`` from either path before
  invoking it, so the codex thread id is recorded (and re-sighted idempotently
  on resume) with no second call site. The link record is what survives the
  provider switch / session reset that later NULLs ``Chat.session_id``. Mirrors
  ``claude_sdk_runner._persist_session_id``. Out-of-band callers such as the
  nightly Reflection runner pass ``db=None`` because their synthetic chat id
  has no durable Chat row; they must not enter these chat-only write paths.
  """
  if db is None or not chat_id or not session_id:
    return
  try:
    from app.chat_writer import PersistSessionId, await_ack, get_writer
    from app.session_links import record_session_link_async
    ack = get_writer().submit(
      PersistSessionId(chat_id=chat_id, session_id=session_id)
    )
    await await_ack(ack)
    await record_session_link_async("codex", session_id, chat_id)
  except Exception:
    log.warning(
      "Codex session id persistence failed chat_id=%s session_id=%s",
      chat_id,
      session_id,
      exc_info=True,
    )


@dataclass
class _CodexSteerAttempt:
  """One admitted, durably-reserved Codex steering delivery."""

  message: str
  user_msgs: list[dict]
  consume_pending_cids: list[str]
  task: asyncio.Task[None] | None = None
  commit_started: bool = False
  settled: bool = False
  failure_published: bool = False


class ActiveCodexTurn:
  """Stop + steer handle registered for SDK-backed Codex turns.

  Wraps the live `(thread, turn_handle)` pair so callers can either steer
  (via `.turn`) or interrupt (via `.interrupt()`). The interrupt method
  signals `turn.interrupt()` upstream — which per the SDK is signal-only
  (`v2_all.py:4260-4264` returns an empty `TurnInterruptResponse`) — then
  awaits `_finished`, which the runner resolves once its notification
  loop drains the resulting `TurnCompletedNotification(status=interrupted)`.
  Same shape as Claude's `ActiveClaudeClient`.
  """

  def __init__(
    self,
    thread: Any,
    turn: Any,
    chat_id: str,
    process_group_id: int | None = None,
    sink: Any | None = None,
  ):
    self.chat_id = chat_id
    self.kind = RunnerKind.CODEX_SDK
    self.thread = thread
    self.turn = turn
    self._sink = sink
    self._process_group_id = process_group_id
    # A retained PGID must never be signalled twice: after the first kill the
    # kernel may eventually reuse that number for an unrelated process group.
    self._force_stop_started = False
    # Admission flag shared with request_user_input on the runner loop. Set
    # synchronously before turn.steer's first await so a not-yet-registered
    # question cannot park the SDK reader ahead of the steer acknowledgement.
    self._steer_in_flight = False
    self._steer_attempt: _CodexSteerAttempt | None = None
    # Set synchronously before interrupt()'s first await. The SDK reports both
    # user-requested stops and unexpected provider interruption with the same
    # TurnStatus.interrupted value; this local fact is what lets terminal
    # validation distinguish them without treating a deliberate Stop as an
    # error.
    self._interrupt_requested = False
    self._finished: asyncio.Future[None] = (
      asyncio.get_running_loop().create_future()
    )

  @property
  def steer_in_flight(self) -> bool:
    return self._steer_in_flight

  @property
  def is_steerable(self) -> bool:
    """Whether this registered handle exposes a live Codex turn."""
    return (
      self.turn is not None
      and not self._finished.done()
      and not self._interrupt_requested
    )

  @property
  def interrupt_requested(self) -> bool:
    return self._interrupt_requested

  async def interrupt(self) -> None:
    """Signals the live turn and waits for runner-side drain."""
    self._interrupt_requested = True
    self._reject_pending_steer()
    try:
      await self.turn.interrupt()
    except Exception as exc:
      log.warning("codex interrupt() raised: %s", exc)
    try:
      await asyncio.wait_for(asyncio.shield(self._finished), timeout=5.0)
    except asyncio.TimeoutError:
      log.warning(
        "codex active_turn._finished never resolved within 5s; runner is wedged"
      )
      return

  async def stop(self, timeout: float = 2.0) -> bool:
    """Interrupts the active turn and waits up to `timeout` seconds."""
    try:
      await asyncio.wait_for(self.interrupt(), timeout=timeout)
      return True
    except asyncio.CancelledError:
      raise
    except asyncio.TimeoutError:
      log.warning(
        "Codex SDK stop timed out chat_id=%s", self.chat_id,
      )
      return False
    except Exception:
      log.exception(
        "Codex SDK stop failed chat_id=%s", self.chat_id,
      )
      return False

  async def force_stop(self, timeout: float = 5.0) -> bool:
    """One-shot hard stop for this turn's verified private process group."""
    self._reject_pending_steer()
    if not self._force_stop_started:
      if self._process_group_id is None:
        return False
      self._force_stop_started = True
      await asyncio.to_thread(
        _terminate_codex_process_group, self._process_group_id,
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
        "Codex SDK hard stop did not finish chat_id=%s", self.chat_id,
      )
      return False

  def mark_finished(self) -> None:
    """Resolves the stop waiter once the runner is fully drained."""
    self._reject_pending_steer()
    if not self._finished.done():
      self._finished.set_result(None)

  def _steer_broadcast(self):
    raw_bc = getattr(self._sink, "bc", None)
    if raw_bc is not None and callable(getattr(raw_bc, "publish", None)):
      return raw_bc
    from app.broadcast import get_broadcast
    return get_broadcast(self.chat_id)

  def _reject_pending_steer(
    self,
    attempt: _CodexSteerAttempt | None = None,
    *,
    after_commit_failure: bool = False,
  ) -> None:
    """Release one uncommitted steer back to the durable pending queue."""
    attempt = attempt or self._steer_attempt
    if attempt is None or attempt.settled:
      return
    # Once the transcript cut has begun under the queue lock, it owns the row:
    # Stop waits behind that lock and must observe the consumed queue rather
    # than simultaneously restoring/resending the same cid.
    if attempt.commit_started and not after_commit_failure:
      return
    attempt.settled = True
    if attempt.failure_published:
      return
    attempt.failure_published = True
    raw_bc = self._steer_broadcast()
    if raw_bc is None:
      return
    from app.chat_event_sink import steer_delivery_failed_event
    raw_bc.publish(
      steer_delivery_failed_event(attempt.consume_pending_cids)
    )

  async def _commit_steer_cut(
    self, attempt: _CodexSteerAttempt,
  ) -> None:
    """Persist + publish the accepted cut through the turn's owning sink."""
    from app.chat_event_sink import commit_steer_cut
    await commit_steer_cut(
      self.chat_id,
      attempt.user_msgs,
      attempt.consume_pending_cids,
      sink=self._sink,
    )

  async def _deliver_steer(
    self, attempt: _CodexSteerAttempt,
  ) -> None:
    """Settle provider delivery away from the HTTP request and queue lock."""
    try:
      await self.turn.steer(attempt.message)
      if (
        attempt.settled
        or self._finished.done()
        or self._interrupt_requested
        or registry.get_handle(self.chat_id, RunnerKind.CODEX_SDK) is not self
      ):
        self._reject_pending_steer(attempt)
        return

      # Only the short durable cut takes the queue lock. Provider I/O above can
      # wait forever without blocking Send or Stop. Recheck after acquisition:
      # Stop bumps/clears first when it wins, while a cut that wins consumes the
      # cid before Stop decides which queued rows to resend.
      from app import chat_queue
      async with chat_queue.get_lock(self.chat_id):
        if (
          attempt.settled
          or self._finished.done()
          or self._interrupt_requested
          or registry.get_handle(
            self.chat_id, RunnerKind.CODEX_SDK,
          ) is not self
        ):
          self._reject_pending_steer(attempt)
          return
        attempt.commit_started = True
        try:
          await self._commit_steer_cut(attempt)
        except Exception:
          log.exception(
            "Codex steer cut failed chat_id=%s; row remains queued",
            self.chat_id,
          )
          self._reject_pending_steer(
            attempt, after_commit_failure=True,
          )
          return
        attempt.settled = True
    except asyncio.CancelledError:
      self._reject_pending_steer(attempt)
      raise
    except (AttributeError, TypeError):
      self._reject_pending_steer(attempt)
    except Exception as exc:
      if _is_closed_turn_error(exc):
        log.info(
          "Codex steer reached a closed turn chat_id=%s", self.chat_id,
        )
      else:
        log.warning(
          "Codex steer delivery failed chat_id=%s: %s",
          self.chat_id, exc,
        )
      self._reject_pending_steer(attempt)
    finally:
      if self._steer_attempt is attempt:
        self._steer_attempt = None
        self._steer_in_flight = False

  async def finish_steer_before_turn_end(self) -> None:
    """Settle a committing cut or reject a still-provider-pending attempt."""
    attempt = self._steer_attempt
    if attempt is None or attempt.settled:
      return
    if not attempt.commit_started:
      self._reject_pending_steer(attempt)
      # The provider control RPC may be the very thing that wedged. Once the
      # owning turn is ending there is no future acknowledgement worth
      # retaining, so release its task too; otherwise each recovered turn could
      # leave one permanently suspended task behind.
      if attempt.task is not None and not attempt.task.done():
        attempt.task.cancel()
      return
    if attempt.task is not None:
      await asyncio.shield(attempt.task)

  async def steer(
    self,
    message: str,
    user_msgs: list[dict] | None = None,
    consume_pending_cids: list[str] | None = None,
  ) -> bool:
    """Admit a durable steer and settle its provider RPC in the background."""
    if not self.is_steerable:
      return False
    rows = [dict(row) for row in list(user_msgs or [])]
    consume = list(consume_pending_cids or [])
    if not rows or not consume:
      # A provider delivery without its durable queue identity cannot be
      # settled exactly once after this request returns.
      return False

    current = self._steer_attempt
    if current is not None and not current.settled:
      # Ambiguous HTTP retries may re-admit the same cids. The first attempt
      # already owns them; report accepted without delivering twice.
      if set(consume).issubset(set(current.consume_pending_cids)):
        return True
      return False

    attempt = _CodexSteerAttempt(
      message=message,
      user_msgs=rows,
      consume_pending_cids=consume,
    )
    self._steer_attempt = attempt
    self._steer_in_flight = True
    attempt.task = asyncio.create_task(self._deliver_steer(attempt))
    # Let the owned task enter the provider call before acknowledging
    # admission. This is one event-loop turn, not a provider wait: a wedged RPC
    # remains detached while immediate failures can publish their queue
    # restoration promptly.
    await asyncio.sleep(0)
    return True


class _CodexGoalTurn:
  """TurnHandle-shaped adapter for one SDK-native logical goal operation.

  Codex may execute a goal as several physical turns.  The SDK's goal stream
  coalesces those into one logical stream while the persisted thread goal is
  active.  This adapter keeps the rest of Möbius on the ordinary TurnHandle
  interface: Stop pauses then interrupts through the SDK, and steering targets
  whichever physical turn the goal runtime currently owns.
  """

  def __init__(
    self,
    client: Any,
    state: Any,
    stream_type: Any,
    invalid_request_type: type[Exception],
  ):
    self._client = client
    self._state = state
    self._invalid_request_type = invalid_request_type
    self.id = state.logical_turn_id
    if not self.id:
      raise RuntimeError("Codex goal operation has no logical turn id")
    self._stream = stream_type(
      state,
      lambda: client.next_goal_notification(state),
      lambda: client.unregister_goal_operation(state),
      lambda: client.cancel_goal_operation(state),
    )

  def stream(self):
    return self._stream

  async def interrupt(self) -> None:
    # The SDK operation deliberately pauses the durable goal before signalling
    # its current physical turn.  A later thread/resume can therefore restore
    # the same objective and counters rather than starting a replacement chat.
    await self._client.cancel_goal_operation(self._state)

  async def steer(self, message: str) -> None:
    physical_turn_id = await asyncio.to_thread(self._state.active_turn)
    while physical_turn_id is not None:
      try:
        await self._client.turn_steer(
          self._state.thread_id,
          physical_turn_id,
          message,
        )
        return
      except self._invalid_request_type as exc:
        error_message = str(getattr(exc, "message", exc))
        if not (
          error_message.startswith("expected active turn id")
          or error_message.startswith("no active turn to steer")
        ):
          raise
        # A logical goal can roll from one physical turn to the next between
        # reading current_turn and the steer RPC. Rejection is side-effect-free;
        # wait for the SDK route to observe the successor, then deliver the same
        # owner message there exactly once. Stop/goal completion wakes this wait
        # and returns None rather than spinning on a stale id.
        physical_turn_id = await asyncio.to_thread(
          self._state.active_turn,
          after=physical_turn_id,
        )
    raise RuntimeError("Codex goal ended before the message could be delivered")


async def _codex_thread_goal(client: Any, sdk: dict[str, Any], thread_id: str):
  """Read a persisted goal before thread/resume can auto-start it."""
  response = await client.request(
    "thread/goal/get",
    {"threadId": thread_id},
    response_model=sdk["ThreadGoalGetResponse"],
  )
  return response.goal


def _release_goal_route(client: Any, state: Any | None) -> None:
  """Release a pre-resume goal route when no logical stream will own it."""
  if state is None:
    return
  try:
    state.finish()
    state.wake_notification_reader()
    client.unregister_goal_operation(state)
  except Exception:
    log.warning("Codex goal route cleanup failed", exc_info=True)


def _sdk_imports() -> dict[str, Any]:
  """Imports the SDK lazily so this module stays importable without it.

  The current upstream git refs are packaging-broken, so Möbius keeps
  the import inside the runtime path for now. Docker import verification
  can still succeed while dispatch wiring catches up to a real install.

  We intentionally import notification and item types from
  `openai_codex.generated.v2_all`, which is a private/generated path.
  The upstream stable surface does not yet expose these typed classes
  publicly. This is brittle: an SDK bump can rename or move them
  freely, so the contract suite imports these symbols at test time and
  catches breakage before an SDK update reaches the runner.
  """
  from openai_codex import ApprovalMode, AsyncCodex, Sandbox
  from openai_codex.client import CodexConfig
  from openai_codex.errors import (
    CodexRpcError,
    InvalidRequestError,
    TransportClosedError,
  )
  from openai_codex.types import Personality, ReasoningEffort, ReasoningSummary
  from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    CommandExecutionOutputDeltaNotification,
    CommandExecutionThreadItem,
    ContextCompactedNotification,
    ContextCompactionThreadItem,
    DynamicToolCallThreadItem,
    ErrorNotification,
    FileChangePatchUpdatedNotification,
    FileChangeThreadItem,
    ImageViewThreadItem,
    ItemCompletedNotification,
    ItemGuardianApprovalReviewCompletedNotification,
    ItemGuardianApprovalReviewStartedNotification,
    ItemStartedNotification,
    MessagePhase,
    McpToolCallThreadItem,
    ReasoningSummaryTextDeltaNotification,
    ReasoningTextDeltaNotification,
    ThreadTokenUsageUpdatedNotification,
    ThreadGoalGetResponse,
    ThreadGoalStatus,
    ThreadItem,
    ThreadTokenUsage,
    TokenUsageBreakdown,
    TurnCompletedNotification,
    TurnStatus,
    WebSearchThreadItem,
  )

  _enable_web_search_results_passthrough(
    web_search_type=WebSearchThreadItem,
    thread_item_type=ThreadItem,
    started_notification_type=ItemStartedNotification,
    completed_notification_type=ItemCompletedNotification,
  )

  _enable_cache_write_usage_passthrough(
    breakdown_type=TokenUsageBreakdown,
    thread_usage_type=ThreadTokenUsage,
    notification_type=ThreadTokenUsageUpdatedNotification,
  )
  # Multi-agent (collab) types exist only on multi-agent-capable SDKs (the
  # openai-codex multi_agent_v2 line). Import them defensively in their own
  # block so an SDK that predates them still boots — a missing type here must
  # not break the whole runner import. A None entry means "this SDK cannot emit
  # collab items / spawned-child thread notifications", and every dispatch
  # branch guards on non-None before its isinstance check. ThreadStartedNotification
  # rides the same block because the only stream occurrence we act on is a
  # spawned child announcing itself, which only happens once collab exists.
  # SubAgentActivityThreadItem (the sub-agent lifecycle marker Codex persists in
  # the parent thread's item history) landed natively in openai-codex
  # rust-v0.145.0-alpha.13; importing it here replaces the earlier resume-time
  # validation-error fallback that reconstructed the thread handle when the SDK
  # could not parse this variant. Its dispatch is a documented no-op (see
  # _tool_start_event / _tool_completed_events). test_codex_sdk_contract asserts
  # this symbol stays importable so a future SDK that renames/drops it fails
  # loudly instead of silently reintroducing the resume gap.
  try:
    from openai_codex.generated.v2_all import (
      CollabAgentToolCallThreadItem,
      SubAgentActivityThreadItem,
      ThreadStartedNotification,
    )
  except ImportError:
    CollabAgentToolCallThreadItem = None
    SubAgentActivityThreadItem = None
    ThreadStartedNotification = None
  try:
    from openai_codex.generated.v2_all import ThreadStatusChangedNotification
  except ImportError:
    ThreadStatusChangedNotification = None
  try:
    from openai_codex.generated.v2_all import (
      AccountRateLimitsUpdatedNotification,
    )
  except ImportError:
    # Older Codex SDKs predate structured rate-limit push; the dispatch loop
    # guards on non-None before its isinstance check, so absence just falls
    # back to error-text limit detection.
    AccountRateLimitsUpdatedNotification = None

  return {
    "CollabAgentToolCallThreadItem": CollabAgentToolCallThreadItem,
    "SubAgentActivityThreadItem": SubAgentActivityThreadItem,
    "ThreadStartedNotification": ThreadStartedNotification,
    "ThreadStatusChangedNotification": ThreadStatusChangedNotification,
    "AccountRateLimitsUpdatedNotification": (
      AccountRateLimitsUpdatedNotification
    ),
    "AgentMessageDeltaNotification": AgentMessageDeltaNotification,
    "AgentMessageThreadItem": AgentMessageThreadItem,
    "ApprovalMode": ApprovalMode,
    "AsyncCodex": AsyncCodex,
    "AsyncGoalNotificationStream": goal_notification_stream_type(),
    "CodexConfig": CodexConfig,
    "CodexRpcError": CodexRpcError,
    "InvalidRequestError": InvalidRequestError,
    "CommandExecutionOutputDeltaNotification": (
      CommandExecutionOutputDeltaNotification
    ),
    "CommandExecutionThreadItem": CommandExecutionThreadItem,
    "ContextCompactedNotification": ContextCompactedNotification,
    "ContextCompactionThreadItem": ContextCompactionThreadItem,
    "DynamicToolCallThreadItem": DynamicToolCallThreadItem,
    "ErrorNotification": ErrorNotification,
    "FileChangePatchUpdatedNotification": FileChangePatchUpdatedNotification,
    "FileChangeThreadItem": FileChangeThreadItem,
    "ImageViewThreadItem": ImageViewThreadItem,
    "Personality": Personality,
    "ReasoningEffort": ReasoningEffort,
    "ReasoningSummary": ReasoningSummary,
    "Sandbox": Sandbox,
    "ItemCompletedNotification": ItemCompletedNotification,
    "ItemGuardianApprovalReviewCompletedNotification": (
      ItemGuardianApprovalReviewCompletedNotification
    ),
    "ItemGuardianApprovalReviewStartedNotification": (
      ItemGuardianApprovalReviewStartedNotification
    ),
    "ItemStartedNotification": ItemStartedNotification,
    "MessagePhase": MessagePhase,
    "McpToolCallThreadItem": McpToolCallThreadItem,
    "ReasoningSummaryTextDeltaNotification": (
      ReasoningSummaryTextDeltaNotification
    ),
    "ReasoningTextDeltaNotification": ReasoningTextDeltaNotification,
    "ThreadTokenUsageUpdatedNotification": (
      ThreadTokenUsageUpdatedNotification
    ),
    "ThreadGoalGetResponse": ThreadGoalGetResponse,
    "ThreadGoalStatus": ThreadGoalStatus,
    "TransportClosedError": TransportClosedError,
    "TurnCompletedNotification": TurnCompletedNotification,
    "TurnStatus": TurnStatus,
    "WebSearchThreadItem": WebSearchThreadItem,
  }


def _enable_web_search_results_passthrough(
  *,
  web_search_type: Any,
  thread_item_type: Any,
  started_notification_type: Any,
  completed_notification_type: Any,
) -> None:
  """Preserve structured search results during temporary SDK schema drift.

  Codex app-server 0.145 emits ``webSearch.results`` and its Rust protocol plus
  public app-server documentation declare the field. The generated Python SDK
  at the same release still omits it and Pydantic silently drops the unknown
  value before Möbius can turn URLs into source pills.

  Keep extra fields only on this one leaf item, then rebuild the discriminated
  union and its two notification envelopes. Once the generated type gains a
  real ``results`` field this is a no-op and normal typed parsing takes over.
  """
  if "results" in getattr(web_search_type, "model_fields", {}):
    return
  if getattr(web_search_type, "model_config", {}).get("extra") == "allow":
    return
  from pydantic import ConfigDict

  config = dict(getattr(web_search_type, "model_config", {}))
  config["extra"] = "allow"
  web_search_type.model_config = ConfigDict(**config)
  web_search_type.model_rebuild(force=True)
  thread_item_type.model_rebuild(force=True)
  started_notification_type.model_rebuild(force=True)
  completed_notification_type.model_rebuild(force=True)


def _enable_cache_write_usage_passthrough(
  *,
  breakdown_type: Any,
  thread_usage_type: Any,
  notification_type: Any,
) -> None:
  """Preserve Codex's cache-write counter across generated SDK schema lag."""
  field = "cache_write_input_tokens"
  if field in getattr(breakdown_type, "model_fields", {}):
    return
  if getattr(breakdown_type, "model_config", {}).get("extra") == "allow":
    return
  from pydantic import ConfigDict

  config = dict(getattr(breakdown_type, "model_config", {}))
  config["extra"] = "allow"
  breakdown_type.model_config = ConfigDict(**config)
  breakdown_type.model_rebuild(force=True)
  thread_usage_type.model_rebuild(force=True)
  notification_type.model_rebuild(force=True)


def _is_transport_death(exc: BaseException) -> bool:
  """Returns True when the app-server connection itself died.

  Strictly the transport: the SDK's own TransportClosedError, or an RPC
  error the app-server raised about a closed/dead channel. Deliberately
  excludes plain RuntimeErrors, because the runner reraises every
  non-retryable provider ErrorNotification as `RuntimeError(message)` —
  an MCP server "is not running" is a provider fault to report, not a
  dead pipe.

  The transport class comes from `_sdk_imports()` and is matched with
  isinstance, not by class name: this predicate decides whether an error
  reaches the owner at all, so it must be bound to the real symbol (and
  must match its subclasses) rather than to anything that happens to
  share a name.
  """
  sdk: dict[str, Any] | None = None
  try:
    sdk = _sdk_imports()
  except ImportError:
    # ImportError, not ModuleNotFoundError: an SDK that renames or drops
    # TransportClosedError fails the `from ... import` the same way a missing
    # package does, and this predicate runs INSIDE the turn's except handler —
    # raising here would mask the very exception it was asked to classify.
    sdk = None
  if sdk is None:
    # Without the SDK no turn can have started, so no transport of ours can
    # have died. Matching on a class name alone would be worse than useless
    # here: it would let any look-alike be mistaken for the real thing.
    return False
  if isinstance(exc, sdk["TransportClosedError"]):
    return True
  # InvalidParamsError is a subclass of CodexRpcError, so matching the base
  # class alone already covers it.
  if isinstance(exc, sdk["CodexRpcError"]):
    text = str(exc).lower()
    return "closed" in text or "not running" in text or "broken pipe" in text
  return False


def _is_closed_turn_error(exc: BaseException) -> bool:
  """Returns True when the live turn handle is already closed/dead.

  Wider than `_is_transport_death`: a steer against a finished turn also
  surfaces as a plain RuntimeError, and there the cost of a false positive
  is only a refused steer.
  """
  if _is_transport_death(exc):
    return True
  if isinstance(exc, RuntimeError):
    text = str(exc).lower()
    return "closed" in text or "not running" in text or "broken pipe" in text
  return False


# JSON-RPC method the Codex app-server sends when the model invokes the
# `request_user_input` tool. Confirmed via probe3.py (the abbreviated
# `tool/requestUserInput` string in the upstream README is incomplete —
# the wire prepends `item/`). If a future SDK bump renames this, the
# AskUserQuestion bridge will silently regress (model's question will
# fall through to the default approval handler, which returns `{}` and
# the turn will likely fail). Lock this in with a contract test against
# the installed SDK's surface.
_REQUEST_USER_INPUT_METHOD = "item/tool/requestUserInput"


def _install_request_user_input_handler(
  codex: Any,
  *,
  loop: asyncio.AbstractEventLoop,
  chat_id: str,
  bc: Any,
  pending_questions: dict,
  db: Any,
) -> None:
  """Wires Möbius's question-bridge into `codex._client._sync._approval_handler`.

  As of openai-codex 0.142.5, `CodexClient.approval_handler` is a
  documented constructor argument on the sync client. Neither
  `AsyncCodex` nor `AsyncCodexClient` accept it in their
  constructors, so we still set the attribute on the underlying sync
  client after construction, targeting the same callable slot the
  public constructor argument populates. See the module docstring for
  the full reasoning on why we don't drop `AsyncCodex` and construct
  `CodexClient` directly.

  The handler runs on the SDK's sync worker thread, so anything that
  touches asyncio state (the future, the broadcast, the DB session)
  must be marshaled onto the runner's loop via
  `asyncio.run_coroutine_threadsafe`.

  For non-`request_user_input` approval methods, defers to the SDK's
  default handler (auto-accept commandExecution / fileChange), which
  preserves the trust-the-agent posture documented in CLAUDE.md.
  """
  async def park_codex_question(questions_payload: list[dict]) -> dict:
    """Run provider-specific admission around the shared question park."""
    # Admission is atomic on the runner loop: steer_into_active_turn marks the
    # ActiveCodexTurn before its first await, and this coroutine also runs on
    # that loop. If the user steer won, fail this not-yet-persisted tool call
    # instead of parking the SDK's sole reader thread before it can route the
    # steer response. A question that registered first is protected by the
    # route's questions.is_waiting gate and remains answerable.
    active = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
    if isinstance(active, ActiveCodexTurn) and active.steer_in_flight:
      raise _SteerOverlapError(
        "Question superseded by steering input; continue with the new input."
      )

    try:
      return await park_question(
        chat_id=chat_id,
        questions=questions_payload,
        bc=bc,
        pending_questions=pending_questions,
      )
    except QuestionPersistenceError as exc:
      log.error(
        "AskUserQuestion save-before-broadcast failed chat_id=%s: %s",
        chat_id, exc,
      )
      raise _BridgeError("could not save the question") from exc
    except (asyncio.CancelledError, _cf.CancelledError) as exc:
      raise _BridgeError("cancelled") from exc

  def handler(method: str, params: dict | None) -> dict:
    if method != _REQUEST_USER_INPUT_METHOD:
      # Replicate the SDK's default auto-accept for the two known
      # approval methods (mirrors CodexClient._default_approval_handler
      # in openai_codex.client). We inline rather than delegate so the
      # bridge stays decoupled from the SDK's internal layout — a
      # future SDK rename won't break this fallback.
      if method == "item/commandExecution/requestApproval":
        return {"decision": "accept"}
      if method == "item/fileChange/requestApproval":
        return {"decision": "accept"}
      return {}

    # Strict payload validation: a malformed requestUserInput payload
    # (missing `questions` key, wrong type, items lacking `id`/`text`)
    # used to silently return empty answers. Silent acceptance hides a
    # real SDK shape change behind a model that keeps fabricating
    # answers. Surface it as a tool-call error so Codex aborts instead.
    if not isinstance(params, dict):
      err = (
        "invalid requestUserInput payload: expected object params, "
        f"got {type(params).__name__}"
      )
      log.error("Codex bridge: %s chat_id=%s", err, chat_id)
      return {"error": {"message": err}}
    if "questions" not in params:
      err = "invalid requestUserInput payload: missing 'questions' key"
      log.error("Codex bridge: %s chat_id=%s", err, chat_id)
      return {"error": {"message": err}}
    questions = params.get("questions")
    if not isinstance(questions, list):
      err = (
        "invalid requestUserInput payload: 'questions' must be a list, "
        f"got {type(questions).__name__}"
      )
      log.error("Codex bridge: %s chat_id=%s", err, chat_id)
      return {"error": {"message": err}}
    if not questions:
      # Empty list (vs missing key): accept and proceed.
      return {"answers": {}}
    for idx, q in enumerate(questions):
      if not isinstance(q, dict):
        err = (
          f"invalid requestUserInput payload: questions[{idx}] not an "
          f"object, got {type(q).__name__}"
        )
        log.error("Codex bridge: %s chat_id=%s", err, chat_id)
        return {"error": {"message": err}}
      if not q.get("id"):
        err = (
          f"invalid requestUserInput payload: questions[{idx}] missing "
          f"'id'"
        )
        log.error("Codex bridge: %s chat_id=%s", err, chat_id)
        return {"error": {"message": err}}
      # The wire field for the question text varies (`question` on the
      # public schema, `text` in some upstream samples); accept either.
      if not (q.get("question") or q.get("text") or q.get("header")):
        err = (
          f"invalid requestUserInput payload: questions[{idx}] missing "
          f"'question'/'text'/'header'"
        )
        log.error("Codex bridge: %s chat_id=%s", err, chat_id)
        return {"error": {"message": err}}

    # Bridge from sync (this thread) to async (runner loop). It deliberately
    # has no user-answer timeout: an AskUserQuestion is a human pause point, and
    # only an answer, Stop/cancel, or a real bridge failure should resolve it.
    # On failures, translate to a JSON-RPC-shaped error so Codex actually fails
    # the tool call instead of continuing with empty answers (B2, B5).
    try:
      fut = asyncio.run_coroutine_threadsafe(
        park_codex_question(questions), loop,
      )
    except RuntimeError as exc:
      # Loop closed between handler install and invocation.
      log.error(
        "Codex bridge: asyncio loop unavailable for chat_id=%s: %s",
        chat_id, exc,
      )
      return {"error": {"message": "Möbius bridge unavailable."}}

    try:
      text_keyed = fut.result()
    except _SteerOverlapError as exc:
      log.info(
        "Codex bridge: question lost steer admission race chat_id=%s", chat_id,
      )
      return {"error": {"message": str(exc)}}
    except QuestionOverlapError as exc:
      log.warning(
        "Codex bridge: overlap rejected chat_id=%s: %s", chat_id, exc,
      )
      return {"error": {"message": str(exc)}}
    except (asyncio.CancelledError, _cf.CancelledError):
      log.info("Codex bridge: cancelled chat_id=%s", chat_id)
      return {"error": {"message": "Interrupted by Stop."}}
    except Exception as exc:
      log.exception(
        "Codex request_user_input bridge failed chat_id=%s: %s",
        chat_id, exc,
      )
      return {"error": {"message": str(exc)}}

    # B3: walk questions by INDEX and map by id, falling back to text
    # match. The previous text-only map silently collided when two
    # questions shared text or when q.question was empty. Möbius's UI
    # currently POSTs `{question_text: label}`; we look up by both
    # the canonical text (`question` field) and the header so the
    # bridge survives UI changes that prefer one or the other.
    answers_by_qid: dict[str, dict] = {}
    for q in questions:
      qid = q.get("id")
      if not qid:
        continue  # malformed question, skip
      label = None
      for key in (q.get("question"), q.get("header"), q.get("id")):
        if key and key in text_keyed:
          label = text_keyed[key]
          break
      if label is None:
        continue
      # Schema expects `answers: list[str]` — Möbius UI is
      # single-choice today so we wrap the single label in a list.
      answers_by_qid[qid] = {
        "answers": [label] if isinstance(label, str) else list(label)
      }
    return {"answers": answers_by_qid}

  if not install_approval_handler(codex, handler):
    log.warning(
      "Codex SDK has no _client._sync chain — request_user_input "
      "bridge NOT installed for chat_id=%s (likely a unit-test fake).",
      chat_id,
    )
    return
  log.debug(
    "Codex request_user_input bridge installed chat_id=%s", chat_id,
  )


def _install_delegated_approval_handler(codex: Any, *, chat_id: str) -> None:
  """Decline any sandbox-bypass request from a delegated child.

  Delegations use ``ApprovalMode.deny_all``, so the app-server should resolve
  escalation internally without calling its client. Keep this handler as the
  fail-closed side of that contract: the Python SDK's default callback accepts
  both request types, which would turn a future wire regression into an
  unsandboxed child.
  """
  def handler(method: str, _params: dict | None) -> dict:
    if method in {
      "item/commandExecution/requestApproval",
      "item/fileChange/requestApproval",
    }:
      return {"decision": "decline"}
    return {}

  if not install_approval_handler(codex, handler):
    log.warning(
      "Codex SDK has no _client._sync chain — delegated approval guard "
      "NOT installed for chat_id=%s (likely a unit-test fake).",
      chat_id,
    )


def _publish_codex_context_compaction(bc: Any, chat_id: str) -> None:
  """Make provider-native compaction visible without affecting the turn."""
  log.info("Codex context compacted for chat %s", chat_id)
  try:
    bc.publish({
      "type": "context_compacted",
      "provider": "codex",
    })
  except Exception:
    # Visibility must never interfere with the provider's own compaction or
    # the rest of its turn.
    log.warning(
      "Codex context-compaction marker failed for chat %s",
      chat_id,
      exc_info=True,
    )


async def run_codex_sdk_turn(
  user_message: str,
  session_id: str | None,
  base_env: dict[str, str],
  cwd: str,
  chat_id: str,
  bc,
  pending_questions: dict,
  db,
  agent_settings: dict | None = None,
  system_prompt: str | None = None,
  resumed_context: str | None = None,
  should_abort: Callable[[], bool] | None = None,
  goal_objective: str | None = None,
  goal_clear: bool = False,
  goal_mode: bool = False,
  goal_continue: bool = False,
  fallback_goal_objective: str | None = None,
  run_policy=None,
  connector_plan=None,
  gauntlet_writer: bool = False,
) -> RunnerResult:
  """Runs one Codex SDK turn and publishes Möbius-shaped events.

  Args:
    user_message: Fully prepared user prompt for this turn.
    session_id: Existing Codex thread id, or None for a new thread.
    base_env: Environment for the SDK app-server process.
    cwd: Working directory for the Codex thread.
    chat_id: Möbius chat identifier for registries.
    bc: Chat broadcast used for `bc.publish(event)`.
    pending_questions: Shared AskUserQuestion registry owned by
      chat.py — keyed by chat_id. Used by the request_user_input
      bridge to park on a future while the user answers.
    db: SQLAlchemy session for durable-chat persistence paths, or None for an
      out-of-band turn with no Chat row (for example nightly Reflection).
    connector_plan: Detached owner-managed MCP configuration built before the
      request session was released. It is plain data and never queries SQLite.

  Returns:
    Dict with `session_id`, `cost_usd`, and `error`.
  """
  record_memory_checkpoint_once(
    "codex_first_runner_enter",
    chat_id=chat_id,
    resuming=session_id is not None,
  )
  sdk = _sdk_imports()
  record_memory_checkpoint_once("codex_first_sdk_loaded", chat_id=chat_id)
  # chat.py always pre-merges the per-chat overrides on top of the
  # global file defaults; treat a missing dict as empty rather than
  # re-reading the file here. Standalone callers (tests) pass `{}`.
  if agent_settings is None:
    agent_settings = {}
  # The per-chat picker writes the `model` key.
  model = agent_settings.get("model")
  # Cross-provider mismatch defense. Chats persisted before the
  # snapshot logic learned to provider-validate (see chat.py
  # snapshot-on-first-send and effective_agent_settings) can end up
  # with a Claude model on a Codex chat (the global default file
  # remembered the last Claude pick when a fresh Codex chat was
  # created). Sending that to Codex 400s every turn with "model
  # not supported". Quietly normalize to the Codex default so
  # existing chats keep working; the user can re-pick in the
  # picker if they want a specific Codex model.
  from app.providers import _model_belongs_to_other_provider, DEFAULT_MODELS
  if model and _model_belongs_to_other_provider(model, "codex"):
    log.warning(
      "codex turn started with non-codex model %r — normalizing to %r",
      model, DEFAULT_MODELS["codex"],
    )
    model = DEFAULT_MODELS["codex"]

  # Reasoning effort comes from Codex's live per-model catalog. The generated
  # enum implements `_missing_`, so newer wire values such as max/ultra survive
  # even before codegen grows named members. Pass through the string and let
  # the SDK convert; a genuinely invalid value degrades to the model default.
  effort_str = agent_settings.get("effort")
  effort = None
  if effort_str:
    try:
      effort = sdk["ReasoningEffort"](effort_str)
    except (ValueError, KeyError):
      log.warning(
        "Codex: unknown effort %r — passing turn without effort override",
        effort_str,
      )

  reasoning_summary = _reasoning_summary_setting(sdk)

  # Compute the constitution snapshot for BOTH thread_start and thread_resume.
  # chat.py passes the SAME immutable per-chat system_prompt snapshot on every
  # turn (never live core.md), so re-supplying it on resume cannot drift a chat
  # off its frozen prompt. The Claude runner re-sends its system prompt every
  # turn because the transport otherwise wipes it; the Codex SDK now accepts
  # base_instructions on thread_resume too (rust-v0.145.0-alpha.13+), so doing
  # the same keeps an established Codex thread anchored to its constitution even
  # after a server-side compaction, instead of trusting the thread's original
  # instructions to survive.
  base_instructions: str | None = None
  if system_prompt is not None:
    base_instructions = system_prompt
  elif session_id is None:
    # Fresh-thread fallback ONLY: read the live skill file when no snapshot was
    # supplied. On resume we never read it — production always passes the
    # per-chat snapshot, so a legacy resume caller without one should neither
    # trigger a file read nor override the thread's existing instructions.
    skill = get_skill_path()
    if skill is not None:
      try:
        base_instructions = skill.read_text(encoding="utf-8")
      except OSError:
        base_instructions = None

  env = dict(base_env)
  env.setdefault("CODEX_HOME", "/data/cli-auth/codex")

  # Remote MCP connections are materialized in chat.py while its DB session is
  # still live. Secrets use Codex's env indirection rather than thread config or
  # argv; the thread receives only env-variable names. Snapshot construction is
  # the optional-capability failure boundary; this runner trusts the typed,
  # detached plan rather than silently masking internal contract violations.
  connector_thread_config = None
  if connector_plan is not None:
    connector_thread_config = connector_plan.codex_config
    env.update(connector_plan.codex_env)

  # config_overrides always isolates the prompt stack, then carries the
  # request_user_input (AskUserQuestion parity), goal, and multi-agent flags.
  # Delegated children disable those optional tools at this provider-owned seam.
  codex_bin = shutil.which("codex")
  delegated = run_policy is not None
  restricted = delegated or gauntlet_writer
  config_overrides = _codex_config_overrides(
    allow_questions=not restricted,
    allow_multi_agent=not restricted,
    allow_goals=not restricted,
    delegated_read_sandbox=(
      delegated and run_policy.scope == "read"
    ),
  )
  launch_args = _codex_app_server_launch_args(codex_bin, config_overrides)
  config_kwargs: dict[str, Any] = dict(
    codex_bin=codex_bin,
    cwd=cwd,
    env=env,
    config_overrides=config_overrides,
  )
  if launch_args is not None:
    config_kwargs["launch_args_override"] = launch_args
  else:
    log.warning(
      "Codex app-server process-group isolation unavailable; "
      "descendant cleanup is best-effort only"
    )
  config = sdk["CodexConfig"](**config_kwargs)

  thread = None
  turn = None
  goal_state = None
  goal_cleared = False
  goal_steer_message: str | None = None
  active_turn: ActiveCodexTurn | None = None
  current_session_id = session_id
  completed_turn: Any | None = None
  completed_message_phases: list[str | None] = []
  first_token_usage: Any | None = None
  final_token_usage: Any | None = None
  call_token_usages: list[Any] = []
  process_group_id: int | None = None
  task_host_open = False
  task_host_tool_use_id: str | None = None
  public_task_ids: set[str] = set()
  codex_context = sdk["AsyncCodex"](config=config)
  codex_call_executor = _install_codex_call_executor(codex_context, chat_id)
  process_group_capture_stop: asyncio.Event | None = None
  process_group_capture_task: asyncio.Task[int | None] | None = None
  if launch_args is not None:
    process_group_capture_stop = asyncio.Event()
    process_group_capture_task = asyncio.create_task(
      _capture_codex_process_group_during_start(
        codex_context, process_group_capture_stop,
      )
    )

  def abort_requested() -> bool:
    return bool(should_abort and should_abort())

  def stop_requested() -> bool:
    """True when Möbius, not the provider, ended this turn.

    The single definition of "we did this to ourselves", shared by terminal
    validation (which sees a clean TurnStatus.interrupted) and the except
    path (which sees the transport die mid-stream because force_stop killed
    the turn's process group). Both need the same fact.

    Includes the superseded-generation abort: a newer turn taking over this
    chat is still Möbius ending this one. That leg can be true before
    `active_turn` exists, which is deliberate — a teardown during startup is
    no more the provider's fault than one mid-stream.
    """
    return bool(
      (active_turn is not None and active_turn.interrupt_requested)
      or abort_requested()
    )

  def with_usage(result: RunnerResult) -> RunnerResult:
    """Attaches whatever the turn spent before it ended, however it ended."""
    if final_token_usage is not None:
      result["usage"] = _model_dump(final_token_usage)
      metrics = normalize_codex_usage(
        first_token_usage,
        final_token_usage,
        call_token_usages,
      )
      result["usage_metrics"] = metrics
      # Codex reports tokens but no dollar cost; derive it from the rate card so
      # a Codex chat records real spend like a Claude chat instead of always
      # $0. Only overrides the caller's None when a priced model + usage exist.
      cost = codex_cost_usd(model, metrics)
      if cost is not None:
        result["cost_usd"] = cost
    return result

  def aborted_result() -> RunnerResult:
    return {
      "session_id": current_session_id,
      "cost_usd": None,
      "error": None,
    }

  try:
    codex, entry_cancel = await _enter_codex_context_owned(codex_context)
    async with _EnteredCodexContext(codex_context, codex) as codex:
      needs_goal_control = bool(
        goal_mode or goal_objective is not None or goal_clear
        or goal_continue or fallback_goal_objective is not None
      )
      goal_client = control_client(codex) if needs_goal_control else None
      record_memory_checkpoint_once(
        "codex_first_client_connected",
        chat_id=chat_id,
      )
      process_group_id = _codex_process_group_id(codex)
      lower_process_group_priority(
        process_group_id,
        logger=log,
        label="Codex app-server",
      )
      if process_group_capture_stop is not None:
        process_group_capture_stop.set()
      # Do NOT await the capture task here. Cancellation immediately after
      # __aenter__ would propagate through an ordinary await and cancel the
      # task that owns our initialization-failure PGID. The outer finally is
      # its sole joiner and shields that join before reaping the group.
      if entry_cancel is not None:
        raise entry_cancel
      # Install AskUserQuestion bridge on the sync CodexClient's
      # approval_handler attribute. `approval_handler` is a public
      # sync-client constructor argument as of openai-codex 0.142.5;
      # neither AsyncCodex nor AsyncCodexClient accept it, so we
      # set it on `goal_client._sync` after construction. Staying
      # on AsyncCodex (instead of dropping to CodexClient to pass
      # the kwarg natively) keeps ~100 lines of SDK glue out of this
      # module. See the module docstring for the full reasoning.
      # When the model calls the `request_user_input` tool (enabled by
      # the features.default_mode_request_user_input config_override
      # above), the app-server sends an `item/tool/requestUserInput`
      # JSON-RPC request to our handler; we park on the shared
      # `_pending_questions` future (same registry Claude uses), publish
      # a `question` event to the SSE wire (same UI), and translate the
      # user's answer back into the Codex response shape. For other
      # approval methods (commandExecution / fileChange), defer to the
      # SDK's default auto-accept behavior so we keep our trust-the-agent
      # posture.
      #
      # Threading note: approval_handler runs on the SDK's sync JSON-RPC
      # worker thread, NOT this asyncio loop. We use
      # asyncio.run_coroutine_threadsafe to bridge into the loop where
      # the pending-question future lives, and block the worker on the
      # resulting concurrent.futures.Future. That keeps the JSON-RPC
      # round-trip blocked (correct — the app-server is waiting for our
      # response) while letting asyncio handle the user's answer POST.
      if delegated:
        _install_delegated_approval_handler(codex, chat_id=chat_id)
      elif not restricted:
        _install_request_user_input_handler(
          codex,
          loop=asyncio.get_running_loop(),
          chat_id=chat_id,
          bc=bc,
          pending_questions=pending_questions,
          db=db,
        )

      # Ordinary owner turns use the SDK's `ApprovalMode.auto_review`, which
      # maps to `approvalPolicy=on_request` with an automatic reviewer.
      # Delegations instead deny every escalation: accepting an unsandboxed
      # retry would erase the read/workspace boundary their durable policy
      # promises. Read-only app-servers use the Landlock override above, so
      # sandboxed inspection remains available in this namespace-restricted
      # container. A write Delegation whose workspace sandbox cannot start
      # fails closed rather than escaping its scope.
      approval_mode = (
        sdk["ApprovalMode"].deny_all
        if delegated
        else sdk["ApprovalMode"].auto_review
      )

      # Sandbox.full_access maps to wire SandboxMode.danger_full_access
      # and disables bwrap. Möbius runs
      # inside a Docker container where the default bwrap-based
      # workspace_write sandbox fails with `bwrap: No permissions to
      # create a new namespace, likely because the kernel does not
      # allow non-privileged user namespaces` (the docker default
      # seccomp profile blocks CLONE_NEWUSER even when the host
      # allows it). That blocked every tool that spawned a
      # sub-process — including the Read tool reading PNGs, which
      # silently broke the agent's ability to verify its own
      # screenshots. Full access here follows the same reasoning, and
      # Möbius's design philosophy
      # ("trust the agent; container is the sandbox") is consistent.
      _sandbox = (
        sdk["Sandbox"].read_only
        if delegated and run_policy.scope == "read"
        else sdk["Sandbox"].workspace_write
        if delegated
        else sdk["Sandbox"].full_access
      )
      persisted_goal = None
      goal_store_available = True
      if session_id is not None and goal_mode:
        try:
          persisted_goal = await _codex_thread_goal(
            goal_client, sdk, session_id,
          )
        except sdk["InvalidRequestError"] as exc:
          error_message = str(getattr(exc, "message", exc))
          if not error_message.startswith("thread not found:"):
            raise
          # Preserve the existing lost-thread recovery path. Goal lookup must
          # run before resume to register an active goal route in time, but a
          # stale session id should still be allowed to resume as a new thread.
          goal_store_available = False
        if goal_clear:
          if persisted_goal is not None:
            cleared = await goal_client.thread_goal_clear(session_id)
            goal_cleared = bool(getattr(cleared, "cleared", True))
          persisted_goal = None
        elif goal_objective is not None and persisted_goal is not None:
          # An explicit new /goal replaces the stored operation.  Clear it
          # before resume so app-server cannot auto-start the old objective in
          # the small window between thread/resume and start_goal_operation.
          await goal_client.thread_goal_clear(session_id)
          persisted_goal = None
        elif (
          persisted_goal is not None
          and persisted_goal.status != sdk["ThreadGoalStatus"].complete
        ):
          # Register before thread/resume: app-server emits the goal snapshot
          # and may start the next physical turn immediately after its resume
          # response.  Routing afterward loses those early notifications.
          goal_state = goal_client.register_goal_operation(session_id)

      if session_id is None:
        thread = await codex.thread_start(
          approval_mode=approval_mode,
          sandbox=_sandbox,
          base_instructions=base_instructions,
          developer_instructions="",
          config=connector_thread_config,
          cwd=cwd,
          model=model,
          personality=sdk["Personality"].none,
        )
      else:
        # Resume parses the thread's persisted history, which can include
        # subAgentActivity items. The SDK's generated ThreadItem union models
        # that variant natively (openai-codex rust-v0.145.0-alpha.13+), so
        # thread_resume no longer raises the validation error the old
        # _resume_codex_thread wrapper caught and worked around. Möbius uses only
        # the returned handle's id + turn() (it never re-renders resumed
        # history), so a native parse is a straight pass-through here.
        thread = await codex.thread_resume(
          session_id,
          approval_mode=approval_mode,
          sandbox=_sandbox,
          base_instructions=base_instructions,
          developer_instructions="",
          config=connector_thread_config,
          cwd=cwd,
          model=model,
          personality=sdk["Personality"].none,
        )
      record_memory_checkpoint_once(
        "codex_first_thread_ready",
        chat_id=chat_id,
        resumed=session_id is not None,
      )

      current_session_id = thread.id
      if abort_requested():
        if goal_state is not None:
          await goal_client.cancel_goal_operation(goal_state)
          _release_goal_route(goal_client, goal_state)
          goal_state = None
        log.info("Codex turn aborted before turn setup chat_id=%s", chat_id)
        return aborted_result()
      if session_id is not None and current_session_id != session_id:
        if goal_state is not None:
          _release_goal_route(goal_client, goal_state)
        goal_state = None
        persisted_goal = None
        # The requested Codex session is gone (rollout cleaned up, or a phantom
        # id) — Codex returned a fresh thread instead of resuming. Rather than
        # dead-end the chat, reseed like the Claude runner: continue on the fresh
        # thread with the chat's own prior history prepended, so recovery is
        # invisible to the user. Crucially the loss is NOT masked — a warning log
        # plus a durable `codex_session_reseed` activity event (Reflection reads
        # /data/logs/activity.jsonl) record that it happened, so the recovery is
        # silent to the user, not to operators. A genuine resume ERROR still
        # raises upstream and surfaces; only this "different thread returned"
        # case (a lost session) reseeds.
        if delegated and not run_policy.allow_session_reseed:
          from app.delegations import REVIEW_REQUIRED_MARKER
          return {
            "session_id": current_session_id,
            "cost_usd": None,
            "error": (
              f"{REVIEW_REQUIRED_MARKER}: The delegated write session could "
              "not be resumed after restart. Its durable history is intact, "
              "but Möbius will not replay write work automatically. Review "
              "the child history and start a new task if another pass is needed."
            ),
          }
        log.warning(
          "Codex session lost for chat %s (requested=%s actual=%s); reseeding "
          "from DB transcript",
          chat_id,
          session_id,
          current_session_id,
        )
        try:
          from app import activity
          activity.log_event(
            "codex_session_reseed",
            chat_id=chat_id,
            requested_session=session_id,
            replacement_session=current_session_id,
            reseeded=bool(resumed_context),
          )
        except Exception:
          log.debug(
            "codex session reseed activity log failed chat_id=%s",
            chat_id, exc_info=True,
          )
        if resumed_context:
          user_message = f"{resumed_context}\n\n{user_message}"
      bc.publish({
        "type": "session_init",
        "session_id": current_session_id,
      })

      if goal_clear:
        await _persist_session_id(db, chat_id, current_session_id)
        bc.publish({
          "type": "text",
          "content": (
            "Goal cleared." if goal_cleared else "No active goal to clear."
          ),
        })
        return {
          "session_id": current_session_id,
          "cost_usd": None,
          "error": None,
        }

      native_goal_objective = (
        (goal_objective or fallback_goal_objective)
        if goal_store_available
        else None
      )
      if (
        session_id is not None
        and current_session_id != session_id
      ):
        # Lost-thread reseeding is an ordinary turn: the replacement needs the
        # transcript block before a new durable goal can safely be created.
        # The next explicit /goal can then start natively on that thread.
        native_goal_objective = None

      if native_goal_objective is not None and persisted_goal is None:
        goal_state, _goal_turn_id = (
          await goal_client.start_goal_operation(
            thread.id, native_goal_objective,
          )
        )
        turn = _CodexGoalTurn(
          goal_client,
          goal_state,
          sdk["AsyncGoalNotificationStream"],
          sdk["InvalidRequestError"],
        )
      elif goal_state is not None and persisted_goal is not None:
        resumed_goal_status = await asyncio.to_thread(
          wait_for_goal_snapshot, goal_state, 30.0,
        )
        if resumed_goal_status is None:
          raise RuntimeError(
            "Timed out waiting for the persisted Codex goal snapshot"
          )
        if resumed_goal_status != sdk["ThreadGoalStatus"].active:
          # Paused/blocked/limited goals stay idle across thread/resume.  A new
          # Möbius turn is the owner's request to continue, so reactivate the
          # SAME stored goal and wait for the runtime-created physical turn.
          await goal_client.thread_goal_set(
            thread.id,
            status=sdk["ThreadGoalStatus"].active,
          )
        logical_turn_id = await asyncio.to_thread(
          goal_state.wait_for_start, 30.0,
        )
        if logical_turn_id is None:
          raise RuntimeError(
            "Timed out waiting for the persisted Codex goal to resume"
          )
        turn = _CodexGoalTurn(
          goal_client,
          goal_state,
          sdk["AsyncGoalNotificationStream"],
          sdk["InvalidRequestError"],
        )
        if not goal_continue:
          # thread/resume already started the goal's next physical turn.  A
          # real owner message belongs inside it; the synthetic restart marker
          # "continue" carries no extra content and is intentionally omitted.
          goal_steer_message = user_message
      else:
        turn = await thread.turn(
          user_message,
          cwd=cwd,
          model=model,
          effort=effort,
          summary=reasoning_summary,
        )
      if abort_requested():
        try:
          await turn.interrupt()
        except Exception:
          log.warning(
            "Codex stale turn interrupt failed chat_id=%s",
            chat_id,
            exc_info=True,
          )
        log.info("Codex turn aborted before stream registration chat_id=%s", chat_id)
        return aborted_result()
      active_turn = ActiveCodexTurn(
        thread,
        turn,
        chat_id=chat_id,
        process_group_id=process_group_id,
        sink=bc,
      )
      registry.register(active_turn)
      record_memory_checkpoint_once(
        "codex_first_turn_ready",
        chat_id=chat_id,
      )

      # Persist the session id AFTER registering the live turn: this is a
      # best-effort write (the actor persist + the append-only session-link
      # record), and Stop/steer reachability must never wait on it — mirrors the
      # Claude runner, which also registers before persisting. It runs after the
      # stale-resume check above so a rejected (mismatched) session is never
      # recorded.
      await _persist_session_id(db, chat_id, current_session_id)

      if goal_steer_message is not None:
        try:
          await turn.steer(goal_steer_message)
        except Exception:
          if active_turn.interrupt_requested:
            log.info(
              "Codex goal steer ended during requested Stop chat_id=%s",
              chat_id,
            )
          else:
            raise

      known_child_ids: set[str] = set()
      active_activation_by_child: dict[str, str] = {}
      last_activation_by_child: dict[str, str] = {}
      activation_by_call_child: dict[tuple[str, str], str] = {}
      activation_counts: dict[str, int] = {}
      task_host_tool_use_id = (
        f"codex-agents:{getattr(turn, 'id', None) or id(turn)}"
      )

      def ensure_task_host() -> None:
        nonlocal task_host_open
        if task_host_open:
          return
        bc.publish({
          "type": "tool_start",
          "tool": "Task",
          "input": "Working in the background",
          "tool_use_id": task_host_tool_use_id,
        })
        task_host_open = True

      def record_task_lifecycle(
        lifecycle: dict[str, Any] | None,
      ) -> None:
        _record_private_lifecycle(bc, lifecycle)
        event = _public_task_event(
          lifecycle,
          tool_use_id=task_host_tool_use_id,
        )
        if event is None:
          return
        ensure_task_host()
        task_id = str(event["task_id"])
        if event["type"] == "task_start":
          public_task_ids.add(task_id)
        else:
          public_task_ids.discard(task_id)
        bc.publish(event)

      # Structured rate-limit state, mirroring the Claude runner. Captured from
      # AccountRateLimitsUpdatedNotification during the turn so a Codex quota
      # kill parks with the provider's REAL reset time (read by
      # chat._limit_park_fields) and is detected structurally via
      # api_error_status=429 — instead of the 30-minute error-text fallback.
      rate_limit_resets_at: int | None = None
      rate_limit_reached = False

      async for notification in turn.stream():
        payload = notification.payload

        if isinstance(payload, sdk["AgentMessageDeltaNotification"]):
          if payload.delta:
            event = {"type": "text", "content": payload.delta}
            item_id = getattr(payload, "item_id", None)
            if item_id:
              event["text_item_id"] = item_id
            bc.publish(event)
          continue

        # Reasoning deltas are Codex's analog of Claude's thinking_delta:
        # both publish the same `thinking` event so the provider-agnostic
        # frontend renders the collapsed "Thinking…" trace either way.
        # Codex emits one of two visible reasoning delta streams depending on
        # SDK/app-server version and summary config: item/reasoning/textDelta
        # or item/reasoning/summaryTextDelta. We request `auto` summaries for
        # the richest public API surface, but handle both SDK event names so a
        # version bump does not silently drop the trace.
        if isinstance(
          payload,
          (
            sdk["ReasoningTextDeltaNotification"],
            sdk["ReasoningSummaryTextDeltaNotification"],
          ),
        ):
          if payload.delta:
            bc.publish(_thinking_event(
              payload.delta,
              _codex_thinking_segment_id(payload),
            ))
          continue

        if isinstance(
          payload,
          sdk["CommandExecutionOutputDeltaNotification"],
        ):
          if payload.delta:
            event = {"type": "tool_output", "content": payload.delta}
            _stamp_notification_item_id(event, payload)
            bc.publish(event)
          continue

        if isinstance(payload, sdk["ItemStartedNotification"]):
          item = payload.item.root if hasattr(payload.item, "root") else payload.item
          if isinstance(item, sdk["AgentMessageThreadItem"]):
            bc.publish({"type": "text_boundary"})
            continue
          collab_cls = sdk.get("CollabAgentToolCallThreadItem")
          if collab_cls is not None and isinstance(item, collab_cls):
            # One provider-neutral Task block hosts every helper activation in
            # this turn. Codex may announce ThreadStarted before or after its
            # collab item, so opening it here is a fallback while the lifecycle
            # path below can also open it on demand.
            ensure_task_host()
          else:
            event = _tool_start_event(item, sdk)
            if event is not None:
              _stamp_tool_use_id(event, item)
              bc.publish(event)
          _observe_skill_reads(item, sdk, bc=bc, chat_id=chat_id)
          # A spawn's child thread ids first appear on its collab item; record
          # the session->chat link now so the child rollout stays attributed
          # even if we never resume it directly.
          await _record_collab_child_links(item, sdk, chat_id=chat_id)
          for lifecycle in _collab_reactivation_events(
            item, sdk, root_thread_id=current_session_id,
            occurred_at=getattr(payload, "started_at_ms", None),
            active=active_activation_by_child, known=known_child_ids,
            activation_by_call_child=activation_by_call_child,
            last_activation_by_child=last_activation_by_child,
          ):
            record_task_lifecycle(lifecycle)
          child_id = str(getattr(item, "agent_thread_id", None) or "")
          kind = getattr(getattr(item, "kind", None), "value", None)
          kind = kind or str(getattr(item, "kind", ""))
          activation = (active_activation_by_child.get(child_id)
                        or last_activation_by_child.get(child_id))
          if child_id and kind == "started":
            known_child_ids.add(child_id)
            activation = activation or str(getattr(item, "id", None) or child_id)
            active_activation_by_child[child_id] = activation
            last_activation_by_child[child_id] = activation
          lifecycle = _subagent_lifecycle_event(
            item, sdk, provider_session_id=current_session_id,
            occurred_at=getattr(payload, "started_at_ms", None),
            provider_activation_id=activation,
          )
          record_task_lifecycle(lifecycle)
          if lifecycle is not None and lifecycle.get("event_type") == "agent_terminal":
            activation = str(lifecycle.get("provider_activation_id") or "")
            if activation:
              last_activation_by_child[child_id] = activation
            if active_activation_by_child.get(child_id) == activation:
              active_activation_by_child.pop(child_id, None)
          continue

        if isinstance(payload, sdk["FileChangePatchUpdatedNotification"]):
          edit_preview = _file_change_edit_preview(payload.changes)
          if edit_preview:
            first = _model_dump(payload.changes[0]) if payload.changes else {}
            event = {
              "type": "tool_input",
              "input": first.get("path", "") if isinstance(first, dict) else "",
              "edit_preview": edit_preview,
            }
            _stamp_notification_item_id(event, payload)
            bc.publish(event)
          summary = _file_change_patch_summary(payload.changes)
          if summary:
            event = {"type": "tool_output", "content": summary}
            _stamp_notification_item_id(event, payload)
            bc.publish(event)
          continue

        if isinstance(payload, sdk["ItemCompletedNotification"]):
          item = payload.item.root if hasattr(payload.item, "root") else payload.item
          if isinstance(item, sdk["ContextCompactionThreadItem"]):
            _publish_codex_context_compaction(bc, chat_id)
            continue
          if isinstance(item, sdk["AgentMessageThreadItem"]):
            completed_message_phases.append(_agent_message_phase(item, sdk))
          collab_cls = sdk.get("CollabAgentToolCallThreadItem")
          if collab_cls is None or not isinstance(item, collab_cls):
            for event in _tool_completed_events(item, sdk):
              _stamp_tool_use_id(event, item)
              bc.publish(event)
          # Also record child links here (idempotent) in case receiver_thread_ids
          # only populates on completion — a missed link silently loses the
          # attribution this recording exists to provide.
          await _record_collab_child_links(item, sdk, chat_id=chat_id)
          for lifecycle in _collab_completion_events(
            item, sdk, root_thread_id=current_session_id,
            occurred_at=getattr(payload, "completed_at_ms", None),
            active=active_activation_by_child, known=known_child_ids,
            activation_by_call_child=activation_by_call_child,
            last_activation_by_child=last_activation_by_child,
          ):
            record_task_lifecycle(lifecycle)
          continue

        if isinstance(payload, sdk["ThreadTokenUsageUpdatedNotification"]):
          if first_token_usage is None:
            first_token_usage = payload.token_usage
          final_token_usage = payload.token_usage
          # ``last`` is the exact upstream Responses completion. Attribute only
          # notifications owned by this turn so a resume-time replay from an
          # earlier turn cannot be charged again.
          if (
            str(getattr(payload, "turn_id", ""))
            == str(getattr(turn, "id", ""))
          ):
            call_token_usages.append(
              getattr(payload.token_usage, "last", None)
            )
          continue

        if isinstance(
          payload,
          sdk["ItemGuardianApprovalReviewStartedNotification"],
        ):
          continue

        if isinstance(
          payload,
          sdk["ItemGuardianApprovalReviewCompletedNotification"],
        ):
          continue

        if isinstance(payload, sdk["ContextCompactedNotification"]):
          # Compatibility for older app-server releases. Current v2 servers
          # expose compaction as a ContextCompactionThreadItem instead.
          _publish_codex_context_compaction(bc, chat_id)
          continue

        ratelimit_cls = sdk.get("AccountRateLimitsUpdatedNotification")
        if ratelimit_cls is not None and isinstance(payload, ratelimit_cls):
          _reset, _reached = _extract_rate_limit_reset(
            getattr(payload, "rate_limits", None)
          )
          if _reset is not None:
            rate_limit_resets_at = _reset
          if _reached:
            rate_limit_reached = True
          continue

        if sdk.get("ThreadStartedNotification") is not None and isinstance(
          payload, sdk["ThreadStartedNotification"]
        ):
          # Never emit session_init (that would repoint the chat to the child),
          # but preserve the child thread's exact spawn time + immediate parent
          # as a normalized lifecycle fact for Workflows.
          lifecycle = _thread_started_lifecycle_event(
            payload, root_thread_id=current_session_id,
            parent_provider_activation_id=(
              active_activation_by_child.get(str(
                getattr(getattr(payload, "thread", None), "parent_thread_id", None)
              )) or last_activation_by_child.get(str(
                getattr(getattr(payload, "thread", None), "parent_thread_id", None)
              ))
            ),
          )
          if lifecycle is not None:
            child_id = str(lifecycle["provider_agent_id"])
            known_child_ids.add(child_id)
            activation = active_activation_by_child.setdefault(
              child_id, str(lifecycle["provider_activation_id"]),
            )
            last_activation_by_child[child_id] = activation
            lifecycle["provider_activation_id"] = activation
            record_task_lifecycle(lifecycle)
          continue

        status_cls = sdk.get("ThreadStatusChangedNotification")
        if status_cls is not None and isinstance(payload, status_cls):
          record_task_lifecycle(_thread_status_lifecycle_event(
            payload, root_thread_id=current_session_id,
            active=active_activation_by_child, known=known_child_ids,
            activation_counts=activation_counts,
            last_activation_by_child=last_activation_by_child,
          ))
          continue

        if isinstance(payload, sdk["TurnCompletedNotification"]):
          completed_turn = payload.turn
          break

        if (
          notification.method == "error"
          and isinstance(payload, sdk["ErrorNotification"])
        ):
          message = getattr(payload.error, "message", None)
          if getattr(payload, "will_retry", False):
            log.warning(
              "Codex turn error will retry for chat %s: %s",
              chat_id,
              message or "Codex error",
            )
            continue
          # When a preceding AccountRateLimitsUpdatedNotification told us a quota
          # window actually reached its cap, surface a STRUCTURED limit terminal
          # rather than raising: api_error_status=429 lets chat._is_limit_terminal
          # detect the kill without string-matching, and the captured reset epoch
          # gives an exact park/resume time. This is the Codex analog of Claude's
          # api_error_status/resets_at terminal. Absent that structured signal we
          # keep raising, so chat.py's existing error-text detection is unchanged.
          if rate_limit_reached:
            limit_result: RunnerResult = with_usage({
              "session_id": current_session_id,
              "cost_usd": None,
              "error": str(message or "Codex usage limit reached."),
              "api_error_status": 429,
            })
            if rate_limit_resets_at is not None:
              limit_result["rate_limit_resets_at"] = rate_limit_resets_at
            return limit_result
          raise RuntimeError(str(message or "Codex error"))

      error_text, terminal_status, final_message_phase = _codex_terminal_error(
        completed_turn,
        sdk,
        interrupt_requested=stop_requested(),
        completed_message_phases=completed_message_phases,
      )
      result: RunnerResult = with_usage({
        "session_id": current_session_id,
        "cost_usd": None,
        "error": _codex_user_error(error_text),
      })
      if terminal_status is not None:
        result["terminal_status"] = terminal_status
      if final_message_phase is not None:
        result["final_message_phase"] = final_message_phase
      # Carry any reset the SDK reported this turn, so a limit surfaced in the
      # terminal (rather than as an ErrorNotification) still parks on real time.
      if rate_limit_resets_at is not None:
        result.setdefault("rate_limit_resets_at", rate_limit_resets_at)
      return result
  except Exception as exc:
    if _is_transport_death(exc) and stop_requested():
      # Our own teardown, seen from the inside. A stop interrupts the turn;
      # when that times out the escalation SIGTERMs the turn's private
      # process group, so the transport dies mid-stream instead of
      # delivering turn/completed. That is our requested interruption, not a
      # provider failure, so it must stay out of the owner-facing transcript.
      #
      # Usually expected after escalation, but WARNING is deliberate: a real
      # app-server crash can coincide with a requested stop and has the same
      # transport shape. The dying words are the only forensic evidence left
      # once the owner-facing error is suppressed.
      log.warning(
        "Codex transport closed by our own stop chat_id=%s: %s", chat_id, exc,
      )
      return with_usage({
        "session_id": current_session_id,
        "cost_usd": None,
        "error": None,
        "terminal_status": _enum_wire_value(sdk["TurnStatus"].interrupted),
      })
    return with_usage({
      "session_id": current_session_id,
      "cost_usd": None,
      "error": _codex_user_error(str(exc)),
    })
  finally:
    if task_host_open and task_host_tool_use_id is not None:
      # A provider error/interrupt may skip terminal child notifications.
      # Close every still-live chip honestly before closing its Task host.
      for task_id in sorted(public_task_ids):
        bc.publish({
          "type": "task_done",
          "task_id": task_id,
          "status": "stopped",
          "summary": None,
          "tool_use_id": task_host_tool_use_id,
        })
      bc.publish({
        "type": "tool_end",
        "tool_use_id": task_host_tool_use_id,
      })
    deferred_cancel: asyncio.CancelledError | None = None
    if process_group_capture_stop is not None:
      process_group_capture_stop.set()
    if process_group_capture_task is not None:
      while not process_group_capture_task.done():
        try:
          await asyncio.shield(process_group_capture_task)
        except asyncio.CancelledError as exc:
          if process_group_capture_task.cancelled():
            break
          # A caller cancellation landed during the shielded join. Keep
          # waiting for the runner-owned task so initialization-failure cleanup
          # cannot lose the only observed PGID.
          deferred_cancel = deferred_cancel or exc
      try:
        captured_during_start = process_group_capture_task.result()
        if process_group_id is None:
          process_group_id = captured_during_start
          lower_process_group_priority(
            process_group_id,
            logger=log,
            label="Codex app-server",
          )
      except asyncio.CancelledError:
        # A task owned only by this runner should not be cancelled, but a
        # proven PGID captured synchronously after __aenter__ still permits
        # safe cleanup. Never let its CancelledError skip that cleanup.
        log.warning("Codex process-group capture task was cancelled")
      except Exception as exc:
        log.warning("Codex process-group capture failed: %s", exc)
    current = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
    group_already_terminated = False
    if isinstance(current, ActiveCodexTurn) and current.turn is turn:
      group_already_terminated = current._force_stop_started
      try:
        await current.finish_steer_before_turn_end()
      except asyncio.CancelledError as exc:
        # A committing cut owns its durable queue row. Finish that bounded
        # writer settlement before honoring cancellation so the wrapper's
        # terminal Finalize cannot race it and reorder Q1/A1/Q2.
        deferred_cancel = deferred_cancel or exc
        await asyncio.shield(current.finish_steer_before_turn_end())
      except Exception:
        log.exception(
          "Codex steer settlement failed during turn teardown chat_id=%s",
          chat_id,
        )
      registry.unregister(chat_id, RunnerKind.CODEX_SDK)
      current.mark_finished()
    # AsyncCodex.close() terminates only its direct Popen PID.  Reap the
    # isolated group after that context exit so a tool child cannot be
    # re-parented to container init and consume CPU/RAM after the turn.  A
    # worker keeps the short grace period off the FastAPI event loop; shield
    # ensures task cancellation cannot prevent the SIGKILL backstop from
    # running in that worker once cleanup has started.
    if process_group_id is not None and not group_already_terminated:
      reap_task = asyncio.create_task(asyncio.to_thread(
        _terminate_codex_process_group, process_group_id,
      ))
      while not reap_task.done():
        try:
          await asyncio.shield(reap_task)
        except asyncio.CancelledError as exc:
          # shield keeps the worker alive. Defer every caller cancellation
          # until its bounded TERM/KILL sequence has completed.
          deferred_cancel = deferred_cancel or exc
      reap_task.result()
    if codex_call_executor is not None:
      codex_call_executor.close()
    if deferred_cancel is not None:
      raise deferred_cancel


async def steer_into_active_turn(
  chat_id: str,
  message: str,
  user_msgs: list[dict] | None = None,
  consume_pending_cids: list[str] | None = None,
) -> bool:
  """Admit a durably-reserved message into the active Codex turn.

  Args:
    chat_id: Möbius chat identifier to look up in the registry.
    message: Text to inject into the in-flight turn.
    user_msgs: Durable queued rows to move into the transcript after provider
      acknowledgement.
    consume_pending_cids: Stable ids of those queued rows.

  Returns:
    True when the live handle accepted ownership of provider settlement.
  """
  current = registry.get_handle(chat_id, RunnerKind.CODEX_SDK)
  if not isinstance(current, ActiveCodexTurn):
    return False
  return await current.steer(
    message, user_msgs, consume_pending_cids,
  )
