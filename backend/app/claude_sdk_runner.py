"""Claude SDK turn runner for Möbius.

This module isolates the Claude Agent SDK integration behind one
function that executes exactly one Möbius chat turn and publishes the
same event shapes the rest of the backend already understands.

Design choices:

- Owner questions use the provider-neutral Möbius `request_question` control:
  it saves a durable terminal card and starts exactly one continuation when
  answered. Claude's native `AskUserQuestion` / `request_user_input` tools are
  excluded on new and resumed turns so a visually identical, process-bound
  wait cannot bypass that lifecycle.
- `ClaudeSDKClient` is used instead of one-shot `query()` because
  Möbius needs the bidirectional control surface: explicit `connect()`,
  `query()`, streaming `receive_response()`, and external
  `interrupt()` support for Stop.
- Normal steering uses the CLI's native next-priority input queue. Root user
  replays acknowledge consumption; a ResultMessage may precede queued input,
  so the runner drains every admitted message before ending the Möbius turn.
  Only Stop and owner-card termination interrupt the connected client.
- `system_prompt` is passed on EVERY turn, not just the first. The
  installed SDK transport
  (`claude_agent_sdk/_internal/transport/subprocess_cli.py`)
  serializes `system_prompt is None → --system-prompt ""`, which on
  resume silently wipes the original session's system prompt. Since
  ClaudeAgentOptions defaults `system_prompt` to `None`, omitting the
  kwarg has the same effect. Always passing `skill_text` keeps the
  skill load-bearing across resumes and matches our "skill is always-
  on" contract. The CLI records the prompt on a conversation's first
  request and reuses that record on resume until compaction
  (`--system-prompt-snapshot` defaults on), so updated text reaches an
  existing session only after it compacts.
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
- That custom prompt reaches the CLI through `--system-prompt-file`, never as
  a `--system-prompt` argument: Linux refuses to start a process with any
  single argument of 128 KiB or more, so a long prompt would stop every turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import shutil
import tempfile
from collections import deque
from collections.abc import Awaitable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from uuid import uuid4
from typing import Any, Literal

from claude_agent_sdk import (
  ClaudeAgentOptions,
  ClaudeSDKClient,
  HookMatcher,
  ProcessError,
  ResultError,
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

from app import activity, generated_files, tracing
from app.memory_observability import cgroup_oom_kill_count, process_was_oom_killed
from app.claude_events import (
  NativeContinuationTracker,
  _clip_task_text,
  dispatch_sdk_message,
  is_root_conversation_message,
)
from app.claude_sdk_contract import transport_process_pid
from app.owner_card_receipts import owner_card_receipt_id
from app.platform_tools import (
  APPROVAL_TOOL_NAME,
  CONTROL_SERVER_NAME,
  QUESTION_TOOL_NAME,
  RESTART_TOOL_NAME,
  SECRET_TOOL_NAME,
)
from app.process_groups import (
  isolated_process_group_id,
  lower_process_group_priority,
  RUN_MARKER_ENV,
  terminate_agent_processes,
)
from app.runner_registry import RunnerKind, registry
from app.runtime_types import RunnerResult

log = logging.getLogger(__name__)

_CONTROL_MCP_READY_TIMEOUT_SECONDS = 10.0


async def _await_control_mcp_ready(
  client,
  *,
  enabled: bool,
  expected_tool_names: tuple[str, ...] | None = None,
) -> str | None:
  """Wait for Claude's local control tool to be discoverable before query.

  ``ClaudeSDKClient.connect()`` establishes the SDK control channel, but stdio
  MCP servers may still be pending. Querying before the tool appears creates a
  cold-start race where ToolSearch reports no match and the same session sees
  it only later. The SDK's MCP status is the owning readiness signal.

  Return ``None`` when ready, otherwise a bounded diagnostic. Agent turns keep
  the documented shell fallback rather than failing wholesale when this
  optional control cannot start.
  """
  if not enabled:
    return None
  get_status = getattr(client, "get_mcp_status", None)
  if not callable(get_status):
    # Lightweight test doubles and older compatible SDK clients do not expose
    # the status call. Production's pinned SDK does.
    return None

  from app.platform_tools import CONTROL_SERVER_NAME, CONTROL_TOOL_NAMES

  required_tools = expected_tool_names or CONTROL_TOOL_NAMES

  loop = asyncio.get_running_loop()
  deadline = loop.time() + _CONTROL_MCP_READY_TIMEOUT_SECONDS
  last_state = "not listed"
  while True:
    remaining = deadline - loop.time()
    if remaining <= 0:
      return f"timed out ({last_state})"
    try:
      payload = await asyncio.wait_for(get_status(), timeout=remaining)
    except asyncio.TimeoutError:
      return f"timed out ({last_state})"
    except Exception as exc:
      return f"status unavailable: {exc}"

    servers = payload.get("mcpServers", []) if isinstance(payload, dict) else []
    server = next((
      item for item in servers
      if isinstance(item, dict) and item.get("name") == CONTROL_SERVER_NAME
    ), None)
    if server is not None:
      status = str(server.get("status") or "unknown")
      tools = server.get("tools") if isinstance(server.get("tools"), list) else []
      available_tools = {
        tool.get("name") for tool in tools if isinstance(tool, dict)
      }
      if (
        status == "connected"
        and set(required_tools).issubset(available_tools)
      ):
        return None
      if status in {"failed", "needs-auth", "disabled"}:
        detail = server.get("error")
        return f"{status}: {detail}" if detail else status
      last_state = status
    await asyncio.sleep(min(0.05, max(0, deadline - loop.time())))

# --- Provider register (documented amendment to system_prompts.py's contract) ---
# The per-chat snapshot in `system_prompts.py` is the identical behavioral
# constitution handed to every provider. A runner MAY append its own small,
# provider-authored behavioral register on top of that shared base — the narrow,
# deliberate exception that module's contract now allows. The Codex runner
# declares none, so it is unaffected. This is the Claude runner's register:
# appended AFTER the constitution, never substituted for it.
# Waiting guidance names shell patterns, not Claude tool names: the bundled CLI
# retires tools between releases (TaskOutput went in 2.1.x), and a register that
# names a missing tool sends every agent into a blocked `sleep` instead.
_CONCISE_REGISTER = r"""# Concise register

Keep replies proportionate: lead with the result and skip preamble. Match length to what the partner needs: brief for simple answers, complete for findings, decisions, and anything they must act on. Brevity never drops substance: a required citation, the escaped `\$` for currency, a screenshot embedded before you describe it, the detail the chat's saved summary or a future continuation needs, or the deliberate speech acts the constitution requires (the one-sentence intent opener, making non-obvious findings explicit, clarifying-question cards, destructive-op and restart confirmations, and the turn closeout).

# Execution lifetimes in Möbius

Bash background tasks are local to this running turn. Their native receipt's
"You will be notified" promise does not survive ending this turn or a restart,
and a running background command never keeps the turn open. Run work that
finishes within the Bash timeout in the foreground. To join longer work in
this turn, start it so it records its own exit
(`cmd > "$TMPDIR/job.log" 2>&1; echo $? > "$TMPDIR/job.exit"`), then wait in
the foreground with `until [ -e "$TMPDIR/job.exit" ]; do sleep 5; done` and
read the log; an output file alone is not completion, and a bare leading
`sleep N` is refused. For an external condition that must outlive this turn,
declare a durable Möbius Wait and confirm its saved receipt. Never end with
"I'm waiting" on a Bash task, an output file, or ListAgents. Workflow work
is also turn-local; join and synthesize it. Helpers started with the Möbius
`spawn_agent` tool are durable: their results reach this chat by themselves,
so never wait on them.

# Interruptions in Möbius

Messages arriving mid-turn join at the next model-response/tool boundary;
they do not interrupt running work. Stop and restart can interrupt work.
Follow the stated reason for an interruption rather than inferring that
the owner rejected a particular tool call.
"""
# Cross-turn scheduling has one owner in Möbius: the durable Waiting lifecycle.
# Provider-native schedulers cannot render its card, survive the same restart
# boundary, or reliably wake a top-level parent from a delegated child.
_CLAUDE_NATIVE_SCHEDULING_TOOLS = (
  "Monitor",
  "ScheduleWakeup",
  "CronCreate",
)
# Owner input also has one platform-owned lifecycle. These native tools wait
# inside a provider process, while Möbius's request_question card is durable
# across turn settlement and restart.
_CLAUDE_NATIVE_OWNER_INPUT_TOOLS = (
  "AskUserQuestion",
  "request_user_input",
)
# Möbius-owned surfaces replace these built-ins entirely: Möbius owns
# scheduling, notifications, and outbound reporting, and zero recorded native
# uses exist. Disabling them trims the fixed per-turn tool-schema tax.
_CLAUDE_UNUSED_BUILTINS = (
  "CronDelete",
  "CronList",
  "EnterWorktree",
  "ExitWorktree",
  "DesignSync",
  "ReportFindings",
  "PushNotification",
)

# Helpers are Möbius's: agents delegate with the Möbius `spawn_agent` tool,
# whose helpers run on any provider, outlive the turn, and share a helper host
# (see helper_hosts). Claude's own helper tool (Agent, formerly Task) is off.
# Workflows keep their own lifecycle for the owner's top effort tier.
_CLAUDE_BUILTIN_HELPER_TOOLS = (
  "Agent",
  "Task",
)
# The tools through which a turn can save an owner-input card: the platform's
# card tools, plus Bash for the `mobius_control_mcp.py call` / `secure-input`
# command-line fallbacks, which print the same receipt. Naming them keeps the card-end
# hook from cutting on an unrelated tool that merely echoes receipt-shaped JSON
# — notably a Task result quoting a child agent's card.
_CLAUDE_OWNER_CARD_TOOLS = (
  f"mcp__{CONTROL_SERVER_NAME}__{APPROVAL_TOOL_NAME}",
  f"mcp__{CONTROL_SERVER_NAME}__{QUESTION_TOOL_NAME}",
  f"mcp__{CONTROL_SERVER_NAME}__{RESTART_TOOL_NAME}",
  f"mcp__{CONTROL_SERVER_NAME}__{SECRET_TOOL_NAME}",
  "Bash",
)


def _system_prompt_with_register(skill_text: str) -> str:
  """Append this runner's provider-authored register to the shared constitution.

  The SDK `system_prompt` is the shared per-chat snapshot (`skill_text`) plus the
  Claude runner's own concise register. The register is appended, never
  substituted, so the constitution the other provider receives is unchanged. An
  empty register returns `skill_text` unchanged, keeping the seam behavior-neutral.
  """
  register = _CONCISE_REGISTER.strip()
  if not register:
    return skill_text
  return skill_text.rstrip() + "\n\n" + register + "\n"


@contextmanager
def _system_prompt_file(text: str) -> Iterator[str]:
  """Yield the path the Claude CLI reads its system prompt from.

  The file is anonymous, so nothing is left behind after a crash, and stays
  open for the client's whole lifetime. Like the MCP config, the CLI opens it
  through this process's ``/proc`` fd.
  """
  with tempfile.TemporaryFile(prefix="mobius-prompt-", suffix=".md") as handle:
    handle.write(text.encode("utf-8"))
    handle.flush()
    yield f"/proc/{os.getpid()}/fd/{handle.fileno()}"


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


_HELPER_REPORT_LOST = (
  "Background work ended before Claude reported its results back. "
  "The reply above is saved; ask again to redo the background work."
)


def _helper_phase_spend(helper_result: dict[str, Any] | None) -> dict[str, Any]:
  """Cost and usage already spent before a turn's helper phase ended early."""
  if helper_result is None:
    return {"cost_usd": None, "usage": None}
  return {
    "cost_usd": helper_result.get("cost_usd"),
    "usage": helper_result.get("usage"),
  }


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


def _claude_process_was_force_stopped(error: ProcessError) -> bool:
  """Whether a public SDK process error is from Möbius's force-stop signals.

  claude-agent-sdk 0.2.152 preserves ``ProcessError`` (and its structured
  ``ResultError`` subclass) through ``receive_response()``. Classifying the
  public exit code removes the old dependency on transport ``_exit_error``;
  only the TERM/KILL signals sent by ``terminate_agent_processes`` are hidden.
  """
  return (
    error.exit_code in (-signal.SIGTERM, -signal.SIGKILL)
  )


_UNSTRUCTURED_PROCESS_STDERR = "Check stderr output for details"


def _process_error_with_stderr_tail(
  error: ProcessError,
  stderr_tail: deque[str],
) -> str:
  """Enrich only a public, pre-result process failure with bounded stderr.

  ``ResultError`` carries the CLI's structured terminal result and is therefore
  already actionable. A bare ``ProcessError`` with the SDK's documented stderr
  placeholder is the early-start/crash path where the captured tail is the
  only additional diagnostic available to the owner.
  """
  message = str(error)
  if (
    isinstance(error, ResultError)
    or error.stderr != _UNSTRUCTURED_PROCESS_STDERR
  ):
    return message
  tail = "\n".join(stderr_tail).strip()
  if tail:
    return f"{message}\nstderr (tail):\n{tail}"
  return (
    f"{message}\n(no stderr captured — the CLI was likely killed "
    "before writing output, e.g. OOM or timeout)"
  )


def _terminate_claude_processes(pgid: int | None, run_marker: str | None) -> bool:
  return terminate_agent_processes(
    pgid,
    run_marker=run_marker,
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


async def _persist_session_id(chat_id: str, session_id: str | None) -> None:
  """Best-effort early persistence for provider resume continuity.

  Advances two records from the same sighting: the CURRENT-session pointer on
  the chat row (via the single-writer actor, since it lives on the hot Chat
  row), and the append-only ``chat_session_links`` map. The link write goes
  through ``record_session_link_async``, which commits on its OWN short-lived
  session in a worker thread, so a link-write stall or failure can neither
  block the loop nor poison the chat's session. The link record survives a
  provider switch / session reset that later NULLs ``Chat.session_id``.
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


@dataclass
class _ClaudeSteer:
  """One native input and its durable rows, retained until a result follows it."""

  uuid: str
  text: str
  user_msgs: list[dict]
  consume_pending_cids: list[str]
  consumed: bool = False
  committed: bool = False
  write_failed: bool = False


class ActiveClaudeClient:
  """Stop/steer handle registered for SDK-backed Claude turns.

  Ordering contract: `interrupt()` signals the SDK and then awaits
  `_finished`, which the runner resolves ONLY after `client.disconnect()`
  returns. Callers (stop_chat / stop_chat_for) therefore block until
  the SDK subprocess is fully torn down, so a late `bc.publish(done)`
  from the runner cannot land after `bc.mark_completed()` has already
  closed the broadcast for live SSE subscribers.
  """

  def __init__(
    self, client: ClaudeSDKClient, chat_id: str, run_marker: str | None = None,
    *, sink=None,
  ):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self._client = client
    self._sink = sink
    self._process_group_id: int | None = None
    # Names this turn's commands after the CLI (and its group) are gone.
    self._run_marker = run_marker
    # Never signal a retained PGID twice; the kernel can eventually reuse it
    # after the first hard stop.
    self._force_stop_started = False
    self._interrupt_owner: Literal["stop", "card"] | None = None
    self._run_generation = registry.current_generation(chat_id)
    self._ready = False
    self._admission_closed = False
    self._native_queue_needs_cancel = False
    self._steers: dict[str, _ClaudeSteer] = {}
    self._steer_cids: set[str] = set()
    self._send_lock = asyncio.Lock()
    self._send_tasks: set[asyncio.Task] = set()
    self._finished: asyncio.Future[None] = (
      asyncio.get_running_loop().create_future()
    )

  @property
  def accepts_native_prompt(self) -> bool:
    """Whether this run still owns permission to start another model request."""
    return bool(
      not self._admission_closed
      and not self._finished.done()
      and self._interrupt_owner is None
      and registry.current_generation(self.chat_id) == self._run_generation
    )

  @property
  def is_steerable(self) -> bool:
    return self._ready and self.accepts_native_prompt

  def mark_ready(self) -> None:
    """The initial query is on the wire; steers can no longer overtake it."""
    self._ready = True

  async def steer(
    self,
    text: str,
    user_msgs: list[dict] | None = None,
    consume_pending_cids: list[str] | None = None,
  ) -> bool:
    """Admit native next-priority input without blocking Send or Stop on I/O.

    Writing stdin is not a consumption receipt. The durable rows remain in
    the pending queue until Claude replays this UUID into its root context.
    No interrupt, duplicate requery, or inferred delivery at a terminal.
    """
    if not self.is_steerable:
      return False
    from app.chat_writer import cid_of

    rows = list(user_msgs or [])
    cids = {cid_of(row) for row in rows} - {None}
    if cids and cids <= self._steer_cids:
      return True
    if cids & self._steer_cids:
      # A composite provider prompt cannot be partially deduplicated without
      # replaying accepted text. Leave the whole new admission queued.
      return False
    self._steer_cids.update(cids)
    attempt = _ClaudeSteer(
      uuid=str(uuid4()),
      text=_steer_redirect_message(
        [text], from_person=any(not row.get("hidden") for row in rows),
      ),
      user_msgs=rows,
      consume_pending_cids=list(consume_pending_cids or []),
    )
    self._steers[attempt.uuid] = attempt
    task = asyncio.create_task(self._send_steer(attempt))
    self._send_tasks.add(task)
    return True

  def _reject_steer(self, attempt: _ClaudeSteer) -> None:
    if attempt.consumed or self._steers.pop(attempt.uuid, None) is None:
      return
    from app.chat_writer import cid_of
    from app.chat_event_sink import steer_delivery_failed_event
    from app.broadcast import get_broadcast

    self._steer_cids.difference_update(cid_of(row) for row in attempt.user_msgs)
    bc = (
      (getattr(self._sink, "bc", None) or self._sink)
      if self._sink is not None else get_broadcast(self.chat_id)
    )
    if bc is not None:
      try:
        bc.publish(steer_delivery_failed_event(attempt.consume_pending_cids))
      except Exception:
        log.exception(
          "Claude steer failure notification lost chat_id=%s", self.chat_id,
        )

  async def _send_steer(self, attempt: _ClaudeSteer) -> None:
    write_started = False
    async def prompt():
      yield {
        "type": "user",
        "uuid": attempt.uuid,
        "priority": "next",
        "message": {"role": "user", "content": attempt.text},
        "parent_tool_use_id": None,
      }

    try:
      async with self._send_lock:
        if not self.is_steerable:
          self._reject_steer(attempt)
          return
        write_started = True
        await self._client.query(prompt())
    except asyncio.CancelledError:
      self._native_queue_needs_cancel |= write_started
      self._reject_steer(attempt)
      raise
    except Exception:
      # A complete line may have reached the CLI before the write raised.
      # No receipt means no durable delivery, but shutdown must withdraw it.
      self._native_queue_needs_cancel |= write_started
      log.warning(
        "Claude native steer write failed chat_id=%s", self.chat_id, exc_info=True,
      )
      # Keep the UUID matchable until the response settles: a replay can prove
      # consumption even when the write reported an ambiguous failure.
      attempt.write_failed = True

  async def consume_steer(self, message: UserMessage, sink) -> None:
    """Commit only a root replay, atomically against Stop's pending clear."""
    if not is_root_conversation_message(message):
      return
    attempt = self._steers.get(message.uuid)
    if attempt is None or attempt.consumed:
      return
    from app.chat_queue import get_lock

    async with get_lock(self.chat_id):
      # Stop bumps generation BEFORE clearing pending rows and calling stop().
      # Checking only interrupt_requested would append an already-cleared cid
      # in that gap, duplicating the frontend's deliberate resend.
      if not self.is_steerable:
        self._reject_steer(attempt)
        return
      attempt.consumed = True
      await _seal_steer_split(sink, self, self.chat_id)

  async def settle_response(self) -> bool:
    """Keep reading when a successful Result precedes native queued input.

    A result completes inputs whose consumption replay we already observed.
    Finish outstanding stdin writes before deciding: a failed write must not
    strand the reader waiting for a response that can never arrive. These
    tasks never hold the queue lock; Stop can cancel them immediately.
    """
    for uuid, attempt in list(self._steers.items()):
      if attempt.consumed and attempt.committed:
        del self._steers[uuid]
    while self._send_tasks:
      tasks = tuple(self._send_tasks)
      await asyncio.gather(*tasks, return_exceptions=True)
      self._send_tasks.difference_update(tasks)
    for attempt in list(self._steers.values()):
      if attempt.write_failed:
        self._reject_steer(attempt)
    return bool(self._steers) and self.is_steerable

  def close_admission(self) -> None:
    """Fence new and unconsumed inputs before yielding to teardown."""
    self._admission_closed = True
    for task in self._send_tasks:
      task.cancel()
    for attempt in list(self._steers.values()):
      if not attempt.consumed:
        self._native_queue_needs_cancel = True
      self._reject_steer(attempt)

  async def cancel_native_queue(self) -> bool:
    """Withdraw rejected CLI inputs before its transport can drain on EOF.

    Python SDK interrupt() omits the CLI's cancel_queued option. Its disconnect
    also retires hook callbacks before closing stdin, so a prompt hook alone
    cannot prevent queued work during shutdown. Use the same public dict-stream
    transport as native input; no SDK internals or premature process kill.
    The runner's final drain/disconnect, not this write, acknowledges Stop.
    """
    # Admission is already closed. Join cancellation before withdrawing the
    # queue so a partially written input cannot race behind the withdrawal.
    tasks = tuple(self._send_tasks)
    if tasks:
      await asyncio.gather(*tasks, return_exceptions=True)
      self._send_tasks.difference_update(tasks)
    if not self._native_queue_needs_cancel:
      return False

    async def request():
      yield {
        "type": "control_request",
        "request_id": str(uuid4()),
        "request": {"subtype": "interrupt", "cancel_queued": True},
      }

    await self._client.query(request())
    self._native_queue_needs_cancel = False
    return True

  def claim_owner_card_end(self) -> bool:
    """Own this turn's end at the saved owner card, cutting no generation yet.

    A saved question / approval / secure-input card is the terminal action of
    the turn: the owner's answer resumes the chat in a LATER turn, so nothing
    said after the card could be delivered. The PostToolUse card-end hook calls
    this while the card's tool result is still inside the CLI, then refuses to
    continue the agent loop — so the next model request is never made and there
    is no post-card generation to interrupt.

    Tagged `card` so the terminal branch classifies the result as a clean
    completion, not a resumable "Paused" note; the chat's durable pending-question marker already owns
    resumption. Returns False when Stop or another card already owns the cut.
    """
    if self._finished.done():
      return False
    if self._interrupt_owner is not None:
      return False
    self._interrupt_owner = "card"
    self.close_admission()
    return True

  def begin_finish_after_owner_card(self) -> Awaitable[None] | None:
    """Fallback cut for a card receipt the card-end hook could not observe.

    `claim_owner_card_end` is the mechanism: PostToolUse ends the ROOT agent's
    loop before the receipt reaches the model, so the normal path reaches here
    already owned and returns None without interrupting. A native child agent's
    card is the case the hook deliberately leaves open — cutting inside a
    subagent's hook would end the child, not the turn, so the child's receipt
    only reaches Möbius later, echoed inside its parent Task tool result. This
    soft interrupt still ends such a turn at its
    source, accepting the historical race with generation already in flight.
    Events emitted during that window still drain through the sink and remain
    visible and durable.

    Claim ownership synchronously, before returning the interrupt awaitable:
    the SDK terminal may already be queued behind the tool result and must see
    `card` ownership even if the event loop has not yet run the interrupt task.
    Defers to a Stop or card that already owns this turn's cut.
    """
    if not self.claim_owner_card_end():
      return None
    return self._client.interrupt()

  async def interrupt(self) -> None:
    """Interrupts the live run and waits for runner-side drain.

    Bounds the `_finished` wait at 5s as a defense-in-depth so a
    wedged runner (one that never reaches its `finally` block) can't
    hang Stop indefinitely. `chat.py:stop_chat_for` adds its own 2s
    bound at the call site; this inner timeout protects any other
    direct caller.

    Unconsumed input remains owned by Stop's clear-and-resend flow. Consumed
    rows have already committed under the queue lock and cannot be resent.
    """
    self._interrupt_owner = "stop"
    self.close_admission()
    if not await self.cancel_native_queue():
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
    """Whether an owner Stop owns this turn's interrupt."""
    return self._interrupt_owner == "stop"

  @property
  def turn_cut_owned(self) -> bool:
    """Whether Möbius — not the provider — ended this turn deliberately.

    True for a Stop and a saved owner card, so the terminal branch
    can tell our own ending apart from a provider abort. A card end normally
    cuts through the PostToolUse hook rather than `client.interrupt()`.
    """
    return self._interrupt_owner is not None

  @property
  def cut_tool_label(self) -> str | None:
    """How the chat shows a tool call cut by Stop."""
    if self._interrupt_owner == "stop":
      return "Stopped"
    return None

  @property
  def owner_card_end(self) -> bool:
    """Whether a continuation owner-input card ended this turn."""
    return self._interrupt_owner == "card"

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
    if self._process_group_id is None and not self._run_marker:
      return False
    await self.terminate_owned_processes()
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

  async def terminate_owned_processes(self) -> None:
    """Reap this run once, completing signal escalation despite cancellation."""
    if self._force_stop_started or (
      self._process_group_id is None and not self._run_marker
    ):
      return
    self._force_stop_started = True
    reap_task = asyncio.create_task(asyncio.to_thread(
      _terminate_claude_processes, self._process_group_id, self._run_marker,
    ))
    deferred_cancel: asyncio.CancelledError | None = None
    while not reap_task.done():
      try:
        await asyncio.shield(reap_task)
      except asyncio.CancelledError as exc:
        deferred_cancel = deferred_cancel or exc
    try:
      reap_task.result()
    except Exception:
      log.warning("Claude process-group cleanup failed chat_id=%s", self.chat_id,
                  exc_info=True)
    if deferred_cancel is not None:
      raise deferred_cancel

  def mark_finished(self) -> None:
    """Resolves the stop waiter once the runner is fully drained."""
    self.close_admission()
    if not self._finished.done():
      self._finished.set_result(None)


def _steer_redirect_message(texts: list[str], *, from_person: bool) -> str:
  """Frame native queued input without changing its authority.

  A person's message is owed a visible acknowledgement (a question folded
  silently into the work went unanswered), but it usually adds to the current
  task rather than replacing it, so the agent keeps going unless it is told to
  stop or change course. Agent-originated carriers (helper results, peer
  notes) remain context for the ongoing work.
  """
  text = "\n\n".join(texts)
  if from_person:
    return (
      "The partner sent this message while you were working. "
      "It usually "
      "adds to your current task rather than replacing it: unless it asks "
      "you to stop or change course, keep going with what you were doing and "
      "fold it in where it fits, or handle it once the current step is done. "
      "Acknowledge it in your visible response and answer any question it "
      "asks:\n\n"
      f"{text}"
    )
  return (
    "New context arrived while you were working. Incorporate the update according "
    "to its stated authority and continue the same task:\n\n"
    f"{text}"
  )


async def steer_into_active_turn(
  chat_id: str,
  text: str,
  user_msgs: list[dict] | None = None,
  consume_pending_cids: list[str] | None = None,
) -> bool:
  """Admit a native next-priority message into a live Claude turn."""
  handle = registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK)
  if not isinstance(handle, ActiveClaudeClient):
    return False
  return await handle.steer(text, user_msgs, consume_pending_cids)


async def _seal_steer_split(bc, active_client, chat_id: str) -> None:
  """Persist consumed native inputs only; called with the chat queue lock held.

  An SDK write or terminal cannot prove consumption. Failed writes and missing
  replays keep their durable pending rows, rather than manufacturing delivery
  in a finally block. Consumed rows survive a failed seal for teardown retry.
  """
  from app.chat_event_sink import commit_steer_cut

  for attempt in list(active_client._steers.values()):
    if not attempt.consumed or attempt.committed:
      continue
    if attempt.user_msgs:
      await commit_steer_cut(
        chat_id, attempt.user_msgs, attempt.consume_pending_cids, sink=bc,
      )
    attempt.committed = True


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
# keyword. Claude's own background completion wakes the parent inside this same
# provider session; the runner preserves that follow-up result before teardown.
_ULTRACODE_REMINDER = (
  "\n\n<system-reminder>You have the Workflow tool for dynamic multi-agent "
  "orchestration this turn. Use it for substantial multi-step work; answer "
  "trivial turns directly. Claude's native completion notification returns you "
  "to this conversation after background work settles; synthesize that result "
  "before finishing. Native Workflow and Agent work is turn-local: do not "
  "finish until it has settled and you have synthesized it.</system-reminder>"
)

# The built-in WebSearch tool appends a standalone "REMINDER: You MUST include
# the sources above ..." line to every result, telling the model to end its
# answer with a hand-written "Sources:" list. Möbius already renders each
# result's links as source pills once per turn
# (tool_sources.sources_from_websearch_text -> MessageSources), so that list only
# duplicates them. Both this reminder and the tool's description are compiled
# into the Claude Code CLI and cannot be edited through the SDK — but the
# reminder rides in the tool OUTPUT, so a PostToolUse hook can drop it at the
# source with updatedToolOutput rather than layering a counter-instruction on top
# of it. (The description's softer nudge cannot be reached this way; if it ever
# leaks a Sources list on its own we can revisit.)
_WEBSEARCH_SOURCES_NAG_MARKER = "REMINDER:"


def _strip_websearch_sources_nag(tool_response: Any) -> tuple[Any, bool]:
  """Return ``(new_response, changed)`` with the trailing sources REMINDER gone.

  Only the reminder line is removed; the "Web search results for query" prefix
  and the ``Links:[...]`` array pill extraction parses are left intact, so the
  results and their pills are unaffected. The response SHAPE is mirrored (str in
  -> str out, list in -> list out) so the CLI accepts the replacement instead of
  rejecting a schema mismatch and silently keeping the original.
  """

  def _strip_text(text: str) -> tuple[str, bool]:
    if not isinstance(text, str):
      return text, False
    idx = text.rfind("\n" + _WEBSEARCH_SOURCES_NAG_MARKER)
    if idx == -1 and text.startswith(_WEBSEARCH_SOURCES_NAG_MARKER):
      idx = 0
    if idx == -1 or "source" not in text[idx:].lower():
      return text, False
    return text[:idx].rstrip(), True

  if isinstance(tool_response, str):
    return _strip_text(tool_response)
  if isinstance(tool_response, list):
    new_items: list[Any] = []
    changed = False
    for item in tool_response:
      if isinstance(item, str):
        new_text, item_changed = _strip_text(item)
        new_items.append(new_text)
        changed = changed or item_changed
      elif isinstance(item, dict) and isinstance(item.get("text"), str):
        new_text, item_changed = _strip_text(item["text"])
        if item_changed:
          item = {**item, "text": new_text}
          changed = True
        new_items.append(item)
      else:
        new_items.append(item)
    return new_items, changed
  return tool_response, False


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
  *,
  user_message: str,
  session_id: str | None,
  base_env: dict[str, str],
  cwd: str,
  chat_id: str,
  skill_text: str,
  bc,
  agent_settings: dict | None = None,
  skills_enabled: bool = False,
  run_policy=None,
  connector_plan=None,
  coordination_enabled: bool = True,
  provider_id: str = "claude",
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
  # The handle for the client currently streaming this turn. Declared in the
  # turn scope because the hooks below are built before `_run_once` constructs
  # it, and the card-end hook must reach the live handle to own the turn's end.
  active_client: ActiveClaudeClient | None = None
  # Generated deliverables use a chat-private directory, so provenance does
  # not depend on serializing unrelated chats that share cwd=/data.
  from app.config import get_settings
  generated_data_dir = get_settings().data_dir
  generated_dir = generated_files.output_dir(
    generated_data_dir, chat_id, create=True,
  )
  base_env = dict(base_env)
  base_env["MOBIUS_COORDINATION_ENABLED"] = (
    "1" if coordination_enabled else "0"
  )
  base_env["MOBIUS_GENERATED_DIR"] = str(generated_dir)

  # Keep the SDK callback for tool policy and skill-read observability.
  # The pinned SDK owns input stream lifetime for permission callbacks.
  # Native owner-question tools are disabled on every launch, including resumes.
  async def can_use_tool(
    tool_name: str,
    input_data: dict[str, Any],
    context,
  ) -> PermissionResultAllow | PermissionResultDeny:
    if tool_name in _CLAUDE_NATIVE_SCHEDULING_TOOLS:
      return PermissionResultDeny(
        message=(
          "Provider-native scheduling is unavailable. Await finite work in "
          "this turn, or let the top-level chat use Möbius Waiting before it "
          "ends. A delegated child must return the condition to its parent."
        )
      )
    if tool_name in _CLAUDE_NATIVE_OWNER_INPUT_TOOLS:
      return PermissionResultDeny(
        message="Use a Möbius saved owner-input card instead."
      )
    if run_policy is not None:
      top_level_controls = {
        "create_goal", "update_goal", "get_goal",
      }
      if tool_name in top_level_controls:
        return PermissionResultDeny(
          message="Delegated child tasks cannot manage the parent Goal."
        )
      return PermissionResultAllow(updated_input=input_data)
    # Auto-approve the other tools, preserving the trust-the-agent posture.
    # This is also the observation point for skill-file Reads (the
    # agent loads /data/shared/skills/*.md via Read, not the Skill
    # tool); the observe call is fire-and-forget and never blocks or
    # fails the tool.
    observe_skill_file_read(
      tool_name, input_data, bc=bc, chat_id=chat_id, cwd=cwd,
      tool_use_id=getattr(context, "tool_use_id", None),
    )
    return PermissionResultAllow(updated_input=input_data)

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

  # Fires after every WebSearch result. Strips the CLI's appended
  # "REMINDER: ... sources ..." line at the source (see
  # _strip_websearch_sources_nag) so the model is not told to append a duplicate
  # hand-written "Sources:" list on top of the shell's pills. No-ops — leaving
  # the original output untouched — when there is nothing to strip.
  async def websearch_sources_hook(
    hook_input: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
  ) -> dict[str, Any]:
    del tool_use_id, context
    new_response, changed = _strip_websearch_sources_nag(
      hook_input.get("tool_response")
    )
    if not changed:
      return {"continue_": True}
    return {
      "continue_": True,
      "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "updatedToolOutput": new_response,
      },
    }

  # Fires after every root-agent tool result and ENDS THE TURN when that result
  # is this turn's saved owner card. `continue_: False` refuses the next model
  # request while the receipt is still inside the CLI, so the response is cut at
  # the card and no post-card text can be generated — instead of racing an
  # interrupt against generation the receipt already started. Möbius never
  # filters what a provider does emit; this removes the cause, not the evidence.
  #
  # Observed terminal for the cut (SDK 0.2.153): subtype "success",
  # is_error False, stop_reason "tool_use", terminal_reason "hook_stopped",
  # result "". The `stopReason` string is not rendered anywhere and the session
  # stays resumable, which is how the owner's answer continues the chat.
  async def owner_card_end_hook(
    hook_input: dict[str, Any],
    tool_use_id: str | None,
    context: dict[str, Any],
  ) -> dict[str, Any]:
    del tool_use_id, context
    if hook_input.get("tool_name") not in _CLAUDE_OWNER_CARD_TOOLS:
      return {"continue_": True}
    # `agent_id` is present only inside a Task-spawned child. Refusing to
    # continue there would end the CHILD, not the owner's turn, so a child's
    # card stays with the sink's `begin_finish_after_owner_card` fallback.
    if hook_input.get("agent_id"):
      return {"continue_": True}
    question_id = owner_card_receipt_id(hook_input.get("tool_response"))
    if question_id is None:
      return {"continue_": True}
    # Only the card this turn actually saved ends this turn: a tool that merely
    # printed an old receipt has no matching continuation block here.
    has_card = getattr(bc, "has_continuation_card", None)
    if not callable(has_card) or not has_card(question_id):
      return {"continue_": True}
    if active_client is None or not active_client.claim_owner_card_end():
      # Stop or another card already owns the cut.
      return {"continue_": True}
    log.info(
      "Claude turn ended at saved owner card chat_id=%s question_id=%s",
      chat_id, question_id,
    )
    return {
      "continue_": False,
      "stopReason": "Saved owner card ends the turn.",
    }

  # Per-chat model/effort overrides flow in via `agent_settings`
  # (merged in chat.py from global defaults + Chat.agent_settings_json).
  # Both are session-wide on the SDK but Möbius spawns one `query()`
  # per turn, so passing them here applies to *this* turn — which is
  # exactly the "apply on next turn" semantics the slash picker promises.
  from app.providers import _model_belongs_to_other_provider, model_supports_effort
  _model = (agent_settings or {}).get("model") or None
  _effort = (agent_settings or {}).get("effort") or None
  # A saved or global default effort must not reach a model that rejects the
  # parameter (the picker hides the control, but defaults still carry one).
  from app.config import get_settings
  if not await model_supports_effort(
    get_settings().data_dir, _model, provider_id=provider_id,
  ):
    _effort = None
  # The "ultracode" tier maps to xhigh effort for the SDK flag (which only
  # accepts low/medium/high/xhigh/max) and arms the Workflow-tool
  # orchestration via the keyword trigger appended to this turn's prompt.
  _ultracode = _effort == "ultracode" and run_policy is None
  if _effort == "ultracode":
    _effort = "xhigh"
  turn_message = user_message + _ULTRACODE_REMINDER if _ultracode else user_message
  # Cross-provider mismatch defense (mirrors codex_sdk_runner). Admission and
  # effective settings normally reject this before the SDK boundary. Keep the
  # boundary strict too: a legacy/corrupt value must never become an implicit
  # provider-chosen model. This runner also serves app Messages providers, so
  # validate against the active provider rather than a hardcoded 'claude'.
  if _model and _model_belongs_to_other_provider(_model, provider_id):
    raise ValueError(
      f"Selected model {_model!r} does not belong to provider {provider_id!r}."
    )
  async def queued_prompt_hook(hook_input, tool_use_id, context):
    """A queued prompt cannot start new work after Stop or a saved card."""
    del hook_input, tool_use_id, context
    if active_client is not None and not active_client.accepts_native_prompt:
      return {"continue_": False, "stopReason": "This Möbius turn has ended."}
    return {"continue_": True}

  async def _run_once() -> RunnerResult:
    nonlocal current_session_id, cost_usd, active_client
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
      "system_prompt": (
        _system_prompt_with_register(skill_text).rstrip()
        + "\n\n" + generated_files.delivery_instruction(generated_dir) + "\n"
      ),
      "resume": session_id if session_id is not None else None,
      "cwd": cwd,
      "env": base_env,
      # Skills may intentionally load user/project settings, but MCP servers
      # remain platform-owned. Keep the two concerns independent so enabling
      # native skills cannot silently add an unreviewed server beside the
      # explicit Möbius control and connector set below.
      "strict_mcp_config": True,
      "setting_sources": (
        ["user", "project"] if skills_enabled else None
      ),
      "include_partial_messages": True,
      "max_buffer_size": _CLAUDE_SDK_MAX_BUFFER_SIZE,
      # Chat text is data, not a Claude Code command line: the SDK marks each
      # outgoing user message client-composed, including resumed turns and
      # internally queued steering. Native @file and /command shortcuts are
      # deliberately unavailable in Möbius chats.
      "verbatim_prompts": True,
      "can_use_tool": can_use_tool,
      "disallowed_tools": [
        *_CLAUDE_BUILTIN_HELPER_TOOLS,
        *_CLAUDE_NATIVE_SCHEDULING_TOOLS,
        *_CLAUDE_NATIVE_OWNER_INPUT_TOOLS,
        *_CLAUDE_UNUSED_BUILTINS,
      ],
      "cli_path": _claude_cli_path(),
      "stderr": _capture_stderr,
      "hooks": {
        "UserPromptSubmit": [HookMatcher(matcher=None, hooks=[queued_prompt_hook])],
        "PostToolUse": [
          HookMatcher(matcher="WebSearch", hooks=[websearch_sources_hook]),
          HookMatcher(matcher=None, hooks=[owner_card_end_hook]),
        ],
        "PreCompact": [
          HookMatcher(matcher=None, hooks=[precompact_hook]),
        ],
      },
    }
    if run_policy is not None:
      options_kwargs["disallowed_tools"].extend([
        "create_goal", "update_goal", "get_goal",
      ])
      restricted_options = {}
      if run_policy is not None:
        restricted_options.update({
        "permission_mode": "acceptEdits",
        })
      options_kwargs.update(restricted_options)
    if skills_enabled:
      options_kwargs["skills"] = "all"
    if _model:
      options_kwargs["model"] = _model
    thinking_config = _claude_thinking_config(_model)
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
    options_kwargs["extra_args"] = {
      "settings": json.dumps(_cli_settings), "replay-user-messages": None,
    }

    # A dict-valued SDK mcp_servers option is serialized directly into the CLI
    # argv. Keep credentials out of /proc/cmdline by handing Claude an anonymous
    # 0600 config file path instead. After connect(), replace that fd with
    # /dev/null but keep its number reserved until teardown: simply closing it
    # would let the argv-visible path alias an unrelated descriptor later.
    # The system-prompt file shares this stack; it carries no credential, so
    # it is not retired.
    startup_file_stack = ExitStack()
    connector_config_handle = None
    # Durable delegated children need the same provider-neutral network tools as
    # their parent.
    control_server_enabled = True
    try:
      from app.connectors import claude_mcp_config_handle
      from app.platform_tools import (
        claude_control_servers,
        expected_control_tool_names,
      )
      connector_config_handle = startup_file_stack.enter_context(
        claude_mcp_config_handle(
          connector_plan,
          extra_servers=claude_control_servers(
            enabled=control_server_enabled,
          ),
        )
      )
      if connector_config_handle:
        options_kwargs["mcp_servers"] = connector_config_handle.path
    except Exception:
      startup_file_stack.close()
      startup_file_stack = ExitStack()
      log.warning(
        "Claude MCP connection injection skipped chat_id=%s",
        chat_id,
        exc_info=True,
      )
    try:
      options_kwargs["system_prompt"] = {
        "type": "file",
        "path": startup_file_stack.enter_context(
          _system_prompt_file(options_kwargs["system_prompt"])
        ),
      }
      options = ClaudeAgentOptions(**options_kwargs)
      client = ClaudeSDKClient(options)
    except Exception:
      startup_file_stack.close()
      raise

    active_client = ActiveClaudeClient(
      client, chat_id=chat_id, run_marker=base_env.get(RUN_MARKER_ENV), sink=bc,
    )
    registry.register(active_client)
    # The root result reached while native helpers still owe a follow-up. Its
    # cost and usage are already spent, so any exit before the follow-up keeps
    # them.
    helper_result: dict[str, Any] | None = None

    oom_kills_before = cgroup_oom_kill_count()
    try:
      try:
        try:
          with tracing.span("claude.connect"):
            await asyncio.wait_for(client.connect(), timeout=30.0)
          with tracing.span("claude.control_mcp_ready"):
            control_ready_error = await _await_control_mcp_ready(
              client,
              enabled=(
                control_server_enabled and connector_config_handle is not None
              ),
              expected_tool_names=(
                expected_control_tool_names(
                  top_level=run_policy is None,
                  coordination_enabled=coordination_enabled,
                )
                if control_server_enabled and connector_config_handle is not None
                else None
              ),
            )
          if control_ready_error:
            log.warning(
              "Claude control MCP unavailable before query chat_id=%s: %s",
              chat_id,
              control_ready_error,
            )
        finally:
          # Keep the anonymous config readable until the local control MCP has
          # completed its initialize + tools/list handshake. Then destroy its
          # contents before query() gives the model a shell or process-
          # inspection tool, while reserving the argv-visible fd number so it
          # cannot alias another live descriptor.
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
      active_client.mark_ready()
      # Startup timing: from the query to Claude's first message of any kind,
      # and to its first model output (stream or assistant message).
      first_message_span = tracing.start_span("claude.wait_first_message")
      first_output_span = tracing.start_span("claude.wait_first_output")

      # Provider-native finite work can finish after its spawning turn, or even
      # immediately before that turn's ResultMessage. Keep its exact
      # continuation boundary across provider responses so neither ordering is
      # reaped before Claude's parent reacts.
      native_work = NativeContinuationTracker()
      # Root AssistantMessage usage is per model call, unlike the terminal
      # ResultMessage aggregate. Keep the latest call across retries, steers,
      # and native background follow-ups so context occupancy stays exact.
      usage_state: dict[str, Any] = {}
      while True:
        async for sdk_msg in client.receive_response():
          if first_message_span is not None:
            tracing.annotate(first_message_span, {"mobius.message_type": type(sdk_msg).__name__})
            tracing.end_span(first_message_span)
            first_message_span = None
          if first_output_span is not None and isinstance(sdk_msg, (StreamEvent, AssistantMessage)):
            tracing.end_span(first_output_span)
            first_output_span = None
          # Persist the session id ONLY from ROOT conversation messages.
          # SystemMessage and its subclasses — notably HookEventMessage,
          # which the codex plugin's SessionStart hook emits on every
          # resumed turn — carry a PHANTOM session id that gets a
          # `session-env/<id>` dir but never a transcript `.jsonl`.
          # Persisting that phantom overwrites Chat.session_id with an id
          # the CLI cannot resume, so the next turn dies "No conversation
          # found". Only StreamEvent/Assistant/User/Result carry the
          # resumable id (the same types dispatch advances the session from).
          # Native child-agent sidechains use those same classes but carry
          # parent_tool_use_id; they must not repoint the chat at a child
          # session or contribute content to the root row.
          if isinstance(
            sdk_msg,
            (StreamEvent, AssistantMessage, UserMessage, ResultMessage),
          ) and is_root_conversation_message(sdk_msg):
            incoming_session_id = getattr(sdk_msg, "session_id", None)
            if incoming_session_id and incoming_session_id != current_session_id:
              await _persist_session_id(chat_id, incoming_session_id)
          if isinstance(sdk_msg, UserMessage):
            await active_client.consume_steer(sdk_msg, bc)
          if isinstance(sdk_msg, RateLimitEvent):
            _resets = getattr(sdk_msg.rate_limit_info, "resets_at", None)
            if _resets is not None:
              rate_limit_resets_at = _resets
          current_session_id, terminal = dispatch_sdk_message(
            sdk_msg,
            bc,
            current_session_id,
            native_work=native_work,
            usage_state=usage_state,
            cut_label=active_client.cut_tool_label,
          )
          if terminal is None:
            continue
          if (
            isinstance(sdk_msg, ResultMessage)
            and native_work.is_inherited_notification_result(
              sdk_msg.num_turns, sdk_msg.is_error,
            )
            and not active_client.interrupt_requested
          ):
            # A resumed session answered a task its previous process left
            # unsettled before reading this turn's query. That empty result is
            # not this turn's answer; keep reading the same stream for it,
            # unless a Stop arrived meanwhile (the CLI dropped that interrupt
            # because nothing was generating yet), so this result ends it.
            break
          if (
            isinstance(sdk_msg, ResultMessage)
            and active_client.turn_cut_owned
            and (
              sdk_msg.stop_reason == "interrupt"
              # Newer SDKs name an interrupt by its terminal reason even when
              # the last model stop_reason was not `interrupt`.
              or sdk_msg.terminal_reason in (
                "aborted_streaming", "aborted_tools",
              )
              # A card end lands while the card's tool is the last action, so
              # the CLI's terminal carries stop_reason `tool_use`/null (observed
              # `terminal_reason: "hook_stopped"` for the PostToolUse cut, and
              # its own `[ede_diagnostic]` for the fallback interrupt), never
              # `interrupt`. We ended this turn, so classify it by our own
              # ownership flag rather than the provider's stop_reason, or a raw
              # "Execution interrupted." error leaks as a red block after the
              # card.
              or active_client.owner_card_end
            )
          ):
            # Our own cut is not a failure (see `_interrupt_owner`). A
            # Stop writes its own pause note through the stop flow.
            terminal["error"] = None
            if active_client.owner_card_end:
              # A continuation owner-input card is the turn's NATURAL terminal:
              # the owner's saved answer resumes the chat, so this is a clean
              # completion — never a resumable "Paused" (which would auto-offer
              # Resume and race the pending-question wait).
              terminal["terminal_status"] = "completed"
            else:
              terminal["terminal_status"] = "interrupted"
          cost_usd = terminal.get("cost_usd")
          if rate_limit_resets_at is not None:
            terminal.setdefault("rate_limit_resets_at", rate_limit_resets_at)
          # Observe every provider result even when steered input also keeps
          # the turn open: native task delivery has its own result bookkeeping.
          native_pending = native_work.observe_result()
          if not terminal.get("error") and active_client.accepts_native_prompt:
            steer_pending = await active_client.settle_response()
            if (steer_pending or native_pending) and active_client.accepts_native_prompt:
              # Native queues own these follow-ups, never duplicate query().
              helper_result = terminal
              break
          active_client.close_admission()
          return terminal
        else:
          # EOF is not delivery. Leave unconsumed rows queued for the next
          # explicit/normal continuation instead of replaying uncertain work.
          break

      # Reached only when the stream ended with no result for the current
      # phase (before the first result, or while native work is outstanding). That is an error exit, not a clean turn: return it
      # error-shaped so chat.py publishes it and finalize() persists a durable
      # error block instead of a silent clean $0 "done".
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
          "usage": _helper_phase_spend(helper_result)["usage"],
          "error": None,
          "terminal_status": "interrupted",
        }
      if helper_result is not None:
        # The reply is already saved; only the helper's report back was lost.
        return {
          **_helper_phase_spend(helper_result),
          "session_id": current_session_id,
          "error": _HELPER_REPORT_LOST,
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
    except ProcessError as exc:
      if (
        active_client.interrupt_requested
        and _claude_process_was_force_stopped(exc)
      ):
        # force_stop() SIGTERMs the verified private CLI process group when a
        # graceful interrupt times out. Keep the classification constrained to
        # the public TERM/KILL exit codes; an unrelated failure that races Stop
        # remains visible to the owner.
        log.warning(
          "Claude process exited during our own stop chat_id=%s: %s",
          chat_id,
          exc,
        )
        return {
          **_helper_phase_spend(helper_result),
          "session_id": current_session_id,
          "error": None,
          "terminal_status": "interrupted",
        }
      return {
        **_helper_phase_spend(helper_result),
        "session_id": current_session_id,
        "error": _process_error_with_stderr_tail(exc, stderr_tail),
        "oom_killed": (
          not isinstance(exc, ResultError)
          and process_was_oom_killed(
            exc.exit_code, oom_kills_before=oom_kills_before,
          )
        ),
      }
    except Exception as exc:
      return {
        **_helper_phase_spend(helper_result),
        "session_id": current_session_id,
        "error": str(exc),
      }
    finally:
      active_client.close_admission()
      queue_cancel_failed = False
      try:
        await active_client.cancel_native_queue()
      except Exception:
        queue_cancel_failed = True
        log.exception("Claude native queue cancellation failed chat_id=%s", chat_id)
      # A failed receipt seal ends the turn with an explicit error. Preserve
      # proven consumption before final file events so the pending row cannot
      # be blindly replayed, and later output stays on its correct side. Never
      # manufacture delivery for unacknowledged stdin writes.
      from app.chat_queue import get_lock
      try:
        async with get_lock(chat_id):
          if registry.current_generation(chat_id) == active_client._run_generation:
            await _seal_steer_split(bc, active_client, chat_id)
      except Exception:
        log.exception("Claude consumed-input seal failed chat_id=%s", chat_id)
      try:
        await generated_files.publish_inbox_files(
          bc,
          data_dir=generated_data_dir,
          chat_id=chat_id,
        )
      except Exception:
        log.debug("generated-file turn capture failed", exc_info=True)
      current_handle = registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK)
      if current_handle is active_client:
        registry.unregister(chat_id, RunnerKind.CLAUDE_SDK)
      try:
        # Only a failed queue withdrawal forfeits graceful EOF: otherwise the
        # SDK retires its hooks and can execute rejected input during its
        # session-flush grace. Normal completion keeps that flush unchanged.
        if queue_cancel_failed:
          await active_client.terminate_owned_processes()
      finally:
        try:
          await client.disconnect()
        finally:
          startup_file_stack.close()
          try:
            # The SDK closes its direct PID; also reap this run's descendants.
            await active_client.terminate_owned_processes()
          finally:
            active_client.mark_finished()
            from app.file_cache import reclaim_provider_cache
            await reclaim_provider_cache("claude")

  return await _run_once()
