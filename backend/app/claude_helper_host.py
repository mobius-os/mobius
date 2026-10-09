"""Claude helper host: many delegated Claude helpers in one Claude Code process.

See ``helper_hosts`` for the shared model. On Claude, a helper is one of Claude
Code's own background agents, so it runs inside the host process at a small
fraction of a separate process's memory. Möbius, not a model, decides every
helper's exact task and model:

- The host runs a minimal dispatcher on the cheapest model. Möbius sends it
  ``SPAWN <id>`` / ``MESSAGE <id>`` lines; the dispatcher calls the built-in
  ``Agent`` / ``SendMessage`` tools.
- A ``PreToolUse`` hook replaces those calls' input with the spec Möbius
  registered under ``<id>``. The dispatcher can do nothing else.
- The same hook gives each helper its own identity: its shell commands load
  the helper turn's private env file, and its Möbius control tool calls carry
  that file (the model never sees or supplies either). Helpers may not use the
  built-in helper tools; they delegate through Möbius like every agent.
- Each helper's messages stream out of the host tagged with the tool call that
  launched it; they are routed into that helper's own chat transcript.
- Stop is a control request (``stop_task``), no model involved.

The host remembers its Claude session id, so a follow-up can reach an earlier
helper by id even after the host restarted; a helper the new host cannot
reach is reseeded from its chat history instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import shlex
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from app import helper_hosts
from app.helper_hosts import Host, HostKey, TurnEnvFile
from app.usage_metrics import HELPER_TASK_COUNTERS, normalize_claude_helper_task_usage, reported_counter

log = logging.getLogger(__name__)

SESSION_PREFIX = "claude-host:"
DISPATCH_MODEL = "haiku"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BUILTIN_HELPER_TOOLS = ("Agent", "Task", "Workflow")
DISPATCHER_PROMPT = (
  "You are a dispatcher inside Möbius. You never do any work yourself and "
  "never write prose. Möbius sends you command lines:\n"
  '- "SPAWN <id>": call Agent with description "<id>", prompt "<id>", '
  'subagent_type "general-purpose", run_in_background true.\n'
  '- "MESSAGE <id>": call the built-in SendMessage tool with to "<id>" and '
  'message "<id>".\n'
  "Use only those two built-in tools, never an MCP tool such as "
  "send_agent_message. SendMessage is deferred: until it is loaded in this "
  'session, first call ToolSearch with query "select:SendMessage".\n'
  "Use the id exactly as given in every field; Möbius fills in the real "
  "values. Whenever your input contains command lines, make every matching "
  "call in ONE assistant message (after loading SendMessage, if needed), "
  "even when the same input also holds task notifications or other text, "
  "then reply with exactly: OK\n"
  "Input without command lines, such as a task notification alone, needs no "
  "call: reply with exactly: OK"
)


def agent_type_for(effort: str | None) -> str:
  return f"mobius-helper-{effort}" if effort in EFFORTS else "mobius-helper"


def resume_reference(host_session_id: str | None, turn: "HelperTurn") -> str:
  """The session reference a helper's next follow-up resumes from.

  A helper lost with its host keeps no agent: messaging that agent in a new
  host would start it fresh without its task, so the follow-up instead
  reseeds it from its own durable chat history.
  """
  if turn.host_lost:
    return f"{SESSION_PREFIX}{host_session_id or ''}::"
  return (
    f"{SESSION_PREFIX}{host_session_id or ''}:{turn.agent_id or ''}"
    f":{turn.launch_tool_use_id or ''}"
  )


async def record_reference(chat_id: str, reference: str) -> None:
  """Point the helper's chat at ``reference`` now, not only after success.

  Every later turn (a restart or limit continuation, a follow-up) resumes from
  the chat's session pointer, so it must name the helper's agent from the
  moment that agent exists, as the private runners do with their session id.
  Otherwise a turn that ends any other way leaves no pointer and the next turn
  starts a new helper that never saw its task. Best effort, like theirs.
  """
  try:
    from app.chat_writer import PersistSessionId, await_ack, get_writer
    await await_ack(get_writer().submit(
      PersistSessionId(chat_id=chat_id, session_id=reference),
    ))
  except Exception:
    log.warning(
      "helper session reference not saved chat_id=%s", chat_id, exc_info=True,
    )


def parse_session(
  session_id: str | None,
) -> tuple[str | None, str | None, str | None]:
  """``(host_session_id, agent_id, launch_tool_use_id)`` of a host helper.

  A resumed helper's messages keep the tool-call id that first launched it,
  so a host process that did not launch it needs that id to route them.
  """
  if not session_id or not session_id.startswith(SESSION_PREFIX):
    return None, None, None
  parts = session_id[len(SESSION_PREFIX):].split(":")
  parts += [""] * (3 - len(parts))
  return parts[0] or None, parts[1] or None, parts[2] or None


HANDBACK_TOOL = "SubagentHandback"


@dataclasses.dataclass
class HelperReport:
  """Choose one helper turn's report from Claude's child stream and hooks.

  Report content is never proof of success: task status stays with the
  task-end notification. New helper definitions exclude native handback;
  resumed agents keep their original launch definitions, so selection must
  still preserve reports from existing provider sessions.

  Contract (https://code.claude.com/docs/en/hooks#subagentstop): without a
  handback, SubagentStop's ``last_assistant_message`` is the final response;
  after a ``SubagentHandback`` it is only closing text and the report is the
  call's ``message``. A handback is classifier-reviewed and can be rejected, so
  it wins only until the helper visibly continues: its own tool result is an
  error, or it makes another tool call. A rejected handback with no later
  report still delivers its message.

  Ordering (claude_agent_sdk Query: one stdout reader buffers messages and
  spawns hook handlers; the CLI blocks on each PreToolUse answer): a hook may
  run before the stream drains messages emitted earlier, never before one
  emitted after it. The forwarded stream is therefore ordered truth, and
  PreToolUse order is exact among tool calls. A stream tool call whose hook ran
  before a handback's hook is earlier work; one whose hook has not run yet can
  only come after it. A handback seen only by its hook is placed at the first
  later work, or treated as the latest event when none follows.

  Why not hooks alone: hooks order tool calls, not text. Text is only in the
  stream, and both "a handback without a message keeps the text written before
  it" and "a rejected handback with no later answer still delivers its
  message" (over SubagentStop text, which can be empty or pre-handback
  narration, and which can arrive after the task end) need text ordered against
  the handback. Snapshotting stream text when the hook runs is racy, because the
  hook can run before earlier text drains.
  """
  # Latest report-bearing content, in stream order.
  candidate: str | None = None
  candidate_is_handback: bool = False
  # A live handback owns the report; text-only content after it is closing text.
  closing: bool = False
  handback_id: str | None = None
  stream_handback_ids: set = dataclasses.field(default_factory=set)
  # PreToolUse order of this turn's tool calls, by tool-use id.
  hook_order: dict = dataclasses.field(default_factory=dict)
  # Latest handback seen by PreToolUse: (tool_use_id, message, hook position).
  hook_handback: tuple | None = None
  hook_handback_continued: bool = False
  # SubagentStop's last_assistant_message.
  stop_message: str | None = None

  def observe_hook_tool(self, tool_use_id, name: str, tool_input) -> None:
    position = len(self.hook_order)
    if tool_use_id is not None:
      self.hook_order[tool_use_id] = position
    if name == HANDBACK_TOOL:
      self.hook_handback = (tool_use_id, (tool_input or {}).get("message"), position)
      self.hook_handback_continued = False
    elif self.hook_handback is not None:
      self.hook_handback_continued = True

  def observe_assistant(self, text: str, tool_uses: list) -> None:
    later_work = [block for block in tool_uses if block.name != HANDBACK_TOOL]
    if any(self._is_after_hook_handback(block.id) for block in later_work):
      self._place_hook_handback()
    if later_work:
      self.closing = False
    if text.strip() and not self.closing:
      self.candidate, self.candidate_is_handback = text, False
    for block in tool_uses:
      if block.name == HANDBACK_TOOL:
        self.stream_handback_ids.add(block.id)
        self._handback(block.id, (block.input or {}).get("message"))

  def observe_tool_result(self, tool_use_id: str | None, is_error: bool) -> None:
    if not is_error or tool_use_id is None:
      return
    if self._hook_only() and tool_use_id == self.hook_handback[0]:
      self._place_hook_handback()
    if tool_use_id == self.handback_id:
      self.closing = False  # Rejected: what the helper writes next is its answer.

  def _hook_only(self) -> bool:
    return (self.hook_handback is not None
            and self.hook_handback[0] not in self.stream_handback_ids)

  def _is_after_hook_handback(self, tool_use_id) -> bool:
    if not self._hook_only():
      return False
    position = self.hook_order.get(tool_use_id)
    return position is None or position > self.hook_handback[2]

  def _place_hook_handback(self) -> None:
    hook_id, message, _ = self.hook_handback
    self.stream_handback_ids.add(hook_id)
    self._handback(hook_id, message)

  def _handback(self, tool_use_id, message) -> None:
    self.handback_id = tool_use_id
    # A handback without a message delivered no report: the latest text the
    # helper wrote before it stands, never the closing text after it.
    if isinstance(message, str):
      self.candidate, self.candidate_is_handback = message, True
    self.closing = True

  def final(self) -> str | None:
    """The report to publish; ``""`` is a deliberate empty report, None is none."""
    if self._hook_only():
      if self.hook_handback_continued:
        # Later work happened that the stream never showed: the stop hook's
        # final response is the newest evidence, the handback the fallback.
        message = self.hook_handback[1]
        return (self.stop_message if self.stop_message
                else message if isinstance(message, str) else self.candidate)
      settled = dataclasses.replace(self, stream_handback_ids=set(self.stream_handback_ids))
      settled._place_hook_handback()
      return settled.final()
    if self.closing:
      return self.candidate or ""
    if self.candidate_is_handback:
      return self.candidate
    return self.stop_message if self.stop_message else self.candidate

@dataclasses.dataclass
class HelperTurn:
  """One helper turn dispatched into the host."""
  dispatch_id: str
  kind: str  # "spawn" | "message"
  spec: dict[str, Any]
  sink: Any
  env_file: TurnEnvFile
  agent_id: str | None = None
  launch_tool_use_id: str | None = None
  status: str | None = None
  summary: str | None = None
  # Provider report, distinct from task lifecycle summaries and closing text.
  report: HelperReport = dataclasses.field(default_factory=HelperReport)
  # Claude's task counters seen while this turn was live: the latest
  # cumulative progress snapshot and the terminal notification's, if any.
  progress_usage: dict | None = None
  final_usage: dict | None = None
  dispatch_error: str | None = None
  dispatch_consumed: bool = False
  dispatch_cancelled: bool = False
  # The helper's last API error from its provider, as (kind, message text).
  api_error: tuple[str, str] | None = None
  # The host process died under this turn: its agent cannot be resumed.
  host_lost: bool = False
  # goals.CompactionBriefRefresh for a helper with a Goal assignment.
  goal_brief_refresh: Any | None = None
  started: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
  done: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
  session_state: dict = dataclasses.field(default_factory=dict)

  def finish(self, status: str, summary: str | None = None) -> None:
    if self.done.is_set():
      return
    self.status, self.summary = status, summary
    self.started.set()
    self.done.set()


class ClaudeHelperHost(Host):
  """One Claude Code process hosting one parent's helpers."""

  def __init__(self, key: HostKey, *, options_factory, session_file: Path):
    super().__init__(key)
    self._options_factory = options_factory
    self._session_file = session_file
    self._client: Any = None
    self._reader: asyncio.Task | None = None
    self._stack = ExitStack()
    self._specs: dict[str, dict] = {}
    self._turn_by_dispatch: dict[str, HelperTurn] = {}
    self._turn_by_tool_use: dict[str, HelperTurn] = {}
    self._agent_of_tool_use: dict[str, str] = {}
    self._turn_by_agent: dict[str, HelperTurn] = {}
    # A first tool can precede its task-start event. Identity has no deadline:
    # only accepted/rejected task start or host shutdown settles it.
    self._agent_identity: dict[str, asyncio.Future] = {}
    self._identity_closed = False
    self._rejected_agents: set[str] = set()
    self._query_lock = asyncio.Lock()
    # One serialized dispatcher request/reply cycle at a time, independent of
    # helper lifetimes. The host owns draining a cancelled admission's reply,
    # so it cannot be mistaken for the next helper's dispatcher response.
    self._dispatcher_reply: asyncio.Future | None = None
    self._dispatcher_failed = False
    self._host_tasks: set[asyncio.Task] = set()
    # The CLI's last stderr lines, logged if the host process ends.
    self.stderr_tail: list[str] = []
    # The dispatcher's last reply (or provider error), logged when a dispatch
    # never starts its helper.
    self.dispatcher_last: str | None = None
    self.session_id: str | None = None
    self.process_group_id: int | None = None

  # ------------------------------------------------------------------ life

  @property
  def alive(self) -> bool:
    if (self._closed or self._client is None
        or (self._dispatcher_failed and self._leases == 0)):
      return False
    if self._reader is not None and self._reader.done():
      return False
    return True

  def _saved_session(self) -> str | None:
    try:
      data = json.loads(self._session_file.read_text())
      return data.get("session_id") or None
    except (OSError, ValueError):
      return None

  def _save_session(self, session_id: str) -> None:
    if not session_id or session_id == self.session_id:
      return
    self.session_id = session_id
    try:
      self._session_file.parent.mkdir(parents=True, exist_ok=True)
      self._session_file.write_text(json.dumps({"session_id": session_id}))
    except OSError:
      log.debug("helper host session not saved", exc_info=True)

  async def start(self) -> None:
    from claude_agent_sdk import ClaudeSDKClient
    from app.claude_sdk_runner import _claude_process_group_id
    from app.process_groups import lower_process_group_priority
    resume = self._saved_session()
    options = self._options_factory(self, resume, self._stack)
    client = ClaudeSDKClient(options)
    try:
      await client.connect()
    except Exception:
      if resume is None:
        raise
      # A saved session that can no longer resume starts a fresh host.
      log.info("helper host session %s not resumable; starting fresh", resume)
      options = self._options_factory(self, None, self._stack)
      client = ClaudeSDKClient(options)
      await client.connect()
      resume = None
    self._client = client
    self.session_id = resume
    self.process_group_id = _claude_process_group_id(client)
    lower_process_group_priority(
      self.process_group_id, logger=log, label="Claude helper host",
    )
    self._reader = asyncio.create_task(self._read())

  async def close(self) -> None:
    self._closed = True
    self._cancel_pending_identity()
    dispatches = list(self._host_tasks)
    for dispatch in dispatches:
      dispatch.cancel()
    for turn in list(self._turn_by_dispatch.values()):
      turn.finish("failed", "The helper host shut down.")
    if self._reader is not None:
      self._reader.cancel()
    client, self._client = self._client, None
    if client is not None:
      with contextlib.suppress(Exception):
        await asyncio.wait_for(client.disconnect(), timeout=10)
    if self.process_group_id is not None:
      from app.process_groups import terminate_process_group
      await asyncio.to_thread(
        terminate_process_group, self.process_group_id,
        logger=log, label="Claude helper host",
      )
      self.process_group_id = None
    # Disconnect/terminate first so provider I/O cannot keep a cancelled host
    # task shielded from its shutdown signal.
    await asyncio.gather(*dispatches, return_exceptions=True)
    self._stack.close()

  # ------------------------------------------------------------------ hooks

  async def pre_tool_use(self, input_data, tool_use_id, context) -> dict:
    name = input_data.get("tool_name") or ""
    tool_input = input_data.get("tool_input") or {}
    agent = input_data.get("agent_id")
    if not agent:
      return self._dispatcher_call(name, tool_input, tool_use_id)
    turn = await self._turn_for_agent(agent)
    if turn is None or not turn.started.is_set() or turn.done.is_set():
      return _deny("This helper has no active turn identity.")
    # Exact tool-call order for report selection, including denied calls.
    # Möbius owns delivery of a handback report independently of the native
    # handback; HelperReport decides whether later work supersedes it.
    turn.report.observe_hook_tool(tool_use_id, name, tool_input)
    if name in BUILTIN_HELPER_TOOLS:
      return _deny(
        "Delegate with the Möbius spawn_agent tool instead; built-in helper "
        "tools are not available.",
      )
    if name == "Bash" and isinstance(tool_input.get("command"), str):
      command = f". {shlex.quote(str(turn.env_file.path))} && {tool_input['command']}"
      return _allow({**tool_input, "command": command})
    if name.startswith("mcp__mobius_control__"):
      # Read by mobius_control_mcp's _CallerEnv inside a helper host.
      return _allow({
        **tool_input, "_mobius_caller_env_file": str(turn.env_file.path),
      })
    return {}

  def _dispatcher_call(self, name, tool_input, tool_use_id) -> dict:
    """The dispatcher may only launch or message helpers exactly as specified."""
    if name == "ToolSearch":
      # SendMessage is a deferred tool: the dispatcher must load it first.
      return {}
    if name not in ("Agent", "SendMessage"):
      # The host also serves helpers' MCP tools; a small dispatcher model
      # reaches for look-alikes (send_agent_message) instead of loading the
      # deferred SendMessage. Say what to call so it corrects in this turn.
      return _deny(
        f"{name} cannot dispatch helpers. For SPAWN call the built-in Agent "
        "tool; for MESSAGE call the built-in SendMessage tool, loading it "
        'first with ToolSearch query "select:SendMessage" if needed. Make '
        "those calls now.",
      )
    key = (
      tool_input.get("description") if name == "Agent"
      # SendMessage.summary is optional display text, not dispatch identity.
      # Both required fields carry the placeholder; only the registered spec
      # below supplies the real recipient and answer.
      else tool_input.get("to") if name == "SendMessage"
      else None
    )
    # Each dispatch is used once: a late call and a repeated command must never
    # launch or message a helper twice.
    turn = self._turn_by_dispatch.get(key) if isinstance(key, str) else None
    if turn is None or turn.done.is_set() or name != ("Agent" if turn.kind == "spawn" else "SendMessage"):
      return _deny("Only dispatches registered by Möbius are allowed.")
    spec = self._specs.pop(key, None)
    if spec is None:
      return _deny("Only dispatches registered by Möbius are allowed.")
    turn.dispatch_consumed = True
    if tool_use_id:
      self._turn_by_tool_use[tool_use_id] = turn
    return _allow(spec)

  async def pre_compact(self, input_data, tool_use_id, context) -> dict:
    """Mark the Goal brief stale for every helper this compaction may be.

    The pinned CLI builds PreCompact input without the compacting agent's
    tool context, so it never names the helper (or tells it from the
    dispatcher). Every live helper turn is marked; each then receives the
    brief once, at its own attributed PostToolUse. An uncompacted helper pays
    one redundant brief, never a missing one. An agent_id, if a later CLI
    sends one, narrows this to that helper.
    """
    agent = input_data.get("agent_id")
    turns = (
      [self._turn_by_agent.get(agent)] if agent
      else list(self._turn_by_dispatch.values())
    )
    for turn in turns:
      if (turn is not None and turn.goal_brief_refresh is not None
          and turn.started.is_set() and not turn.done.is_set()):
        turn.goal_brief_refresh.mark_compacted()
    log.info(
      "Claude helper host compacted key=%s trigger=%s agent=%s",
      self.key.digest, input_data.get("trigger"), agent or "unattributed",
    )
    return {}

  async def post_tool_use(self, input_data, tool_use_id, context) -> dict:
    """End a helper at its recorded question; refresh a compacted helper's
    Goal brief; fail undelivered SendMessage.

    PostToolUse reaches the agent whose tool ran, the same supported in-turn
    boundary the private Claude runner uses. ``continue_: False`` there ends
    only that helper's task, as completed and still resumable by a later
    SendMessage (unlike ``stop_task``, which makes it unresumable).
    """
    agent = input_data.get("agent_id")
    if agent:
      turn = self._turn_by_agent.get(agent)
      if turn is None or not turn.started.is_set() or turn.done.is_set():
        return {}
      if _records_own_question(input_data, turn.sink):
        return {
          "continue_": False,
          "stopReason": "The helper's question is recorded; its parent's answer resumes it.",
        }
      if turn.goal_brief_refresh is None:
        return {}
      brief = await turn.goal_brief_refresh.take()
      if not brief:
        return {}
      return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": brief,
      }}
    if input_data.get("tool_name") not in ("Agent", "SendMessage"):
      return {}
    response = input_data.get("tool_response")
    if isinstance(response, str):
      try:
        response = json.loads(response)
      except (ValueError, RecursionError):
        response = None
    if (input_data.get("hook_event_name") == "PostToolUseFailure"
        or isinstance(response, dict) and (
          response.get("success") is False or response.get("isError") is True
          or response.get("is_error") is True)):
      self._fail_dispatch(tool_use_id, input_data.get("tool_name"))
    return {}

  def _fail_dispatch(self, tool_use_id, tool_name: str) -> None:
    turn = self._turn_by_tool_use.get(tool_use_id or "")
    if turn is not None and not turn.started.is_set():
      turn.dispatch_error = "unreachable" if tool_name == "SendMessage" else "launch_failed"
      log.warning("helper dispatch failed kind=%s id=%s", turn.kind, turn.dispatch_id)
      turn.finish("failed", "The provider could not dispatch this helper turn.")

  async def subagent_stop(self, input_data, tool_use_id, context) -> dict:
    """Capture the documented final response, never a task notification summary.

    https://code.claude.com/docs/en/hooks#subagentstop: with SubagentHandback,
    last_assistant_message is only closing text; HelperReport ranks it last.
    """
    turn = self._turn_by_agent.get(input_data.get("agent_id"))
    report = input_data.get("last_assistant_message")
    if (turn is not None and turn.started.is_set() and not turn.done.is_set()
        and isinstance(report, str)):
      turn.report.stop_message = report
    return {}

  async def _turn_for_agent(self, agent_id: str) -> HelperTurn | None:
    if self._identity_closed or agent_id in self._rejected_agents:
      return None
    turn = self._turn_by_agent.get(agent_id)
    if turn is not None and turn.started.is_set():
      return turn
    identity = self._agent_identity.get(agent_id)
    if identity is None:
      identity = self._agent_identity[agent_id] = asyncio.get_running_loop().create_future()
    try:
      # Cancelling one hook must not cancel other hooks awaiting this identity.
      return await asyncio.shield(identity)
    except asyncio.CancelledError:
      if identity.cancelled():
        return None
      raise

  def _reject_identity(self, agent_id: str) -> None:
    """A rejected start settles this agent negatively, not its siblings."""
    self._rejected_agents.add(agent_id)
    identity = self._agent_identity.pop(agent_id, None)
    if identity is not None and not identity.done():
      identity.set_result(None)

  def _cancel_pending_identity(self) -> None:
    self._identity_closed = True
    for identity in self._agent_identity.values():
      identity.cancel()
    self._agent_identity.clear()

  # ------------------------------------------------------------------ stream

  async def _read(self) -> None:
    from claude_agent_sdk.types import (
      AssistantMessage,
      ResultMessage,
      TaskNotificationMessage,
      TaskProgressMessage,
      TaskStartedMessage,
      TaskUpdatedMessage,
      ToolResultBlock,
      ToolUseBlock,
      UserMessage,
    )
    from app.claude_events import dispatch_sdk_message
    try:
      async for message in self._client.receive_messages():
        if isinstance(message, TaskStartedMessage):
          self._on_task_started(message)
          continue
        if isinstance(message, TaskProgressMessage):
          self._on_task_progress(message.task_id, message.usage)
          continue
        if isinstance(message, TaskNotificationMessage):
          self._on_task_end(message.task_id, message.status, message.summary, message.usage)
          continue
        if isinstance(message, TaskUpdatedMessage):
          patch = (getattr(message, "data", {}) or {}).get("patch") or {}
          status = patch.get("status")
          if status in ("completed", "failed", "killed", "stopped"):
            self._on_task_end(message.task_id, status, None, None)
          continue
        if isinstance(message, ResultMessage):
          self._save_session(getattr(message, "session_id", None))
          self._dispatcher_replied(message)
          continue
        parent = getattr(message, "parent_tool_use_id", None)
        error = getattr(message, "error", None)
        if not parent:
          if isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
              if isinstance(block, ToolResultBlock) and block.is_error:
                turn = self._turn_by_tool_use.get(block.tool_use_id)
                if turn is not None:
                  self._fail_dispatch(block.tool_use_id, "Agent" if turn.kind == "spawn" else "SendMessage")
          session = getattr(message, "session_id", None)
          if session:
            self._save_session(session)
          if isinstance(message, AssistantMessage):
            text = _message_text(message)[:300]
            self.dispatcher_last = f"{error}: {text}" if error else text
          continue
        turn = self._turn_for_parent(parent)
        if turn is None or turn.done.is_set():
          continue
        if isinstance(message, AssistantMessage):
          if error:
            turn.api_error = (str(error), _message_text(message))
          else:
            # The ordered child stream is complete before its task-end event,
            # even when an SDK hook callback reaches us later. Never use the
            # dispatcher's prose.
            turn.report.observe_assistant(_message_text(message), [
              block for block in message.content if isinstance(block, ToolUseBlock)
            ])
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
          for block in message.content:
            if isinstance(block, ToolResultBlock):
              turn.report.observe_tool_result(block.tool_use_id, bool(block.is_error))
        try:
          rooted = dataclasses.replace(message, parent_tool_use_id=None)
          turn.session_state["sid"], _ = dispatch_sdk_message(
            rooted, turn.sink, turn.session_state.get("sid"),
          )
        except Exception:
          log.debug("helper event routing failed", exc_info=True)
    except asyncio.CancelledError:
      raise
    except Exception:
      log.warning("Claude helper host stream ended key=%s", self.key.digest, exc_info=True)
    finally:
      if not self._closed:
        log.warning(
          "Claude helper host exited key=%s stderr=%s",
          self.key.digest, " | ".join(self.stderr_tail[-8:]),
        )
      self._fail_open_turns()

  def _fail_open_turns(self) -> None:
    """The host process is gone: fail its open turns, whose agents died with it."""
    self._cancel_pending_identity()
    for dispatch in self._host_tasks:
      dispatch.cancel()
    for turn in list(self._turn_by_dispatch.values()):
      if not turn.done.is_set():
        turn.host_lost = True
      turn.finish("failed", "The helper host stopped unexpectedly.")

  def _turn_for_parent(self, tool_use_id: str) -> HelperTurn | None:
    agent = self._agent_of_tool_use.get(tool_use_id)
    if agent is not None:
      return self._turn_by_agent.get(agent)
    return self._turn_by_tool_use.get(tool_use_id)

  def _on_task_started(self, message) -> None:
    if getattr(message, "task_type", None) not in (None, "local_agent"):
      self._reject_identity(message.task_id)
      return
    # A resumed helper reports under its original launch id, so its current
    # (follow-up) turn wins over whatever turn first used that id.
    current = self._turn_by_agent.get(message.task_id)
    if current is not None:
      turn = current
    else:
      turn = self._turn_by_tool_use.get(message.tool_use_id or "")
    if turn is None or turn.done.is_set() or self._identity_closed:
      self._reject_identity(message.task_id)
      if (turn is not None and turn.dispatch_consumed and not self._identity_closed
          and (turn.dispatch_cancelled or turn.dispatch_error)):
        self._own_task(self._stop_agent(message.task_id))
      return
    self._rejected_agents.discard(message.task_id)
    turn.agent_id = message.task_id
    self._turn_by_agent[message.task_id] = turn
    if message.tool_use_id:
      self._agent_of_tool_use[message.tool_use_id] = message.task_id
      if turn.launch_tool_use_id is None:
        turn.launch_tool_use_id = message.tool_use_id
    turn.started.set()
    identity = self._agent_identity.pop(message.task_id, None)
    if identity is not None and not identity.done():
      identity.set_result(turn)

  def _on_task_progress(self, task_id, usage) -> None:
    turn = self._turn_by_agent.get(task_id)
    # The same liveness rule as a task end: progress of a previous run, or
    # after this turn settled, never counts toward it. Keep each latest valid
    # counter: sparse/malformed snapshots cannot erase earlier evidence.
    # Counters are cumulative, so replace rather than sum (even on a reset).
    if (turn is None or turn.done.is_set() or not turn.started.is_set()
        or not isinstance(usage, dict)):
      return
    reported = {key: reported_counter(usage.get(key)) for key in HELPER_TASK_COUNTERS}
    valid = {key: value for key, value in reported.items() if value is not None}
    if valid:
      turn.progress_usage = {**(turn.progress_usage or {}), **valid}

  def _on_task_end(self, task_id, status, summary, usage) -> None:
    turn = self._turn_by_agent.get(task_id)
    # A follow-up turn is mapped to its agent before the agent resumes, and
    # each run reports its end more than once; a late report of the previous
    # run must not end the follow-up before it starts.
    if turn is None or turn.done.is_set() or not turn.started.is_set():
      return
    normalized = {"completed": "completed", "failed": "failed"}.get(status, "stopped")
    report = turn.report.final()
    if report is not None:
      turn.sink.publish({"type": "assistant_result", "content": report})
    # The first terminal event settles the turn. Counters on a later one are
    # dropped: the turn's result may already be persisted, and waiting for an
    # event Claude may never send would hold the turn open.
    if isinstance(usage, dict):
      turn.final_usage = dict(usage)
    turn.finish(normalized, summary)

  # ------------------------------------------------------------------ work

  async def run_turn(self, turn: HelperTurn, on_started=None) -> HelperTurn:
    """Dispatch one helper turn and wait until it settles.

    ``on_started`` is awaited once the turn's agent exists, before the turn
    settles.
    """
    if self._dispatcher_failed:
      self._fail_unsubmitted(turn)
      return turn
    if self._identity_closed:
      turn.host_lost = True
      turn.finish("failed", "The helper host is no longer available.")
      return turn
    self._specs[turn.dispatch_id] = turn.spec
    self._turn_by_dispatch[turn.dispatch_id] = turn
    if turn.kind == "message" and turn.agent_id:
      # Route the resumed helper's output to this turn from its first event;
      # its messages carry its original launch id.
      self._turn_by_agent[turn.agent_id] = turn
      if turn.launch_tool_use_id:
        self._agent_of_tool_use[turn.launch_tool_use_id] = turn.agent_id
    self._own_task(self._dispatch(turn))
    try:
      # Start/failure, explicit turn cancellation and host closure settle this
      # admission even while query() or the dispatcher's response is pending.
      await turn.started.wait()
      if turn.dispatch_error:
        return turn
      if on_started is not None and turn.agent_id:
        await on_started(turn)
      await turn.done.wait()
      return turn
    except BaseException:
      self._specs.pop(turn.dispatch_id, None)
      await self.stop(turn)
      turn.finish("failed", "The helper dispatch was interrupted.")
      raise
    finally:
      self._specs.pop(turn.dispatch_id, None)
      self._turn_by_dispatch.pop(turn.dispatch_id, None)

  def _fail_unsubmitted(self, turn: HelperTurn) -> None:
    self._specs.pop(turn.dispatch_id, None)
    turn.dispatch_error = "launch_failed"
    turn.finish("failed", "Claude could not start this helper request. "
                "Its dispatcher is unavailable; no work was launched or message sent.")

  def _retire_dispatcher(self) -> None:
    """An ambiguous stream may drain, but never admit another request.

    Existing helpers keep running. Queued requests fail visibly and release
    their leases, so the normal host lifecycle can replace this host once its
    active helpers settle, even when no reply to the failed query will arrive.
    """
    self._dispatcher_failed = True
    for queued in list(self._turn_by_dispatch.values()):
      if not queued.done.is_set() and not queued.dispatch_consumed:
        self._fail_unsubmitted(queued)

  def _own_task(self, work) -> None:
    task = asyncio.create_task(work)
    self._host_tasks.add(task)
    task.add_done_callback(self._host_tasks.discard)

  async def _stop_agent(self, agent_id: str) -> None:
    if self._client is not None:
      try:
        await self._client.stop_task(agent_id)
      except Exception:
        log.warning("helper task stop failed agent=%s", agent_id, exc_info=True)

  async def _dispatch(self, turn: HelperTurn) -> None:
    """One request and its exact reply; no model retries or guessed deadlines.

    A stopped turn returns locally. The host still drains a submitted query's
    reply before accepting the next request; close() cancels that drain.
    """
    async with self._query_lock:
      if turn.done.is_set() or self._identity_closed or self._dispatcher_failed:
        return
      replied = asyncio.get_running_loop().create_future()
      self._dispatcher_reply = replied
      try:
        verb = "SPAWN" if turn.kind == "spawn" else "MESSAGE"
        await self._client.query(f"{verb} {turn.dispatch_id}")
        response = await replied
        if turn.done.is_set():
          return
        if getattr(response, "is_error", False) and not turn.started.is_set():
          self._specs.pop(turn.dispatch_id, None)
          turn.dispatch_error = "launch_failed"
          turn.finish("failed", "Claude failed to dispatch this helper request. "
                      "Möbius will not replay it automatically.")
        elif turn.dispatch_id in self._specs:
          self._specs.pop(turn.dispatch_id, None)
          turn.dispatch_error = "not_called"
          log.warning(
            "helper dispatch %s %s completed without its tool call key=%s "
            "tool_call_seen=False dispatcher_last=%r",
            turn.kind, turn.dispatch_id, self.key.digest, self.dispatcher_last,
          )
          turn.finish("failed", "Claude finished without starting this helper or "
                      "sending its message. No work was launched or message sent.")
      except asyncio.CancelledError:
        raise
      except Exception:
        log.warning("helper dispatch query failed id=%s", turn.dispatch_id, exc_info=True)
        if not turn.done.is_set():
          turn.dispatch_error = turn.dispatch_error or "launch_failed"
          turn.finish("failed", "Claude could not confirm this helper request. "
                      "Some work may have started; Möbius will not replay it automatically.")
          if turn.agent_id and turn.dispatch_consumed:
            self._own_task(self._stop_agent(turn.agent_id))
        # Submission may have reached Claude, or failed before producing any
        # reply at all. Retire admissions rather than wait on an undrainable
        # response or attribute a late result to a future request.
        self._retire_dispatcher()
      finally:
        if not replied.done():
          replied.cancel()
        self._dispatcher_reply = None

  def _dispatcher_replied(self, response=None) -> None:
    reply = self._dispatcher_reply
    if reply is not None and not reply.done():
      reply.set_result(response)

  async def stop(self, turn: HelperTurn) -> None:
    turn.dispatch_cancelled = True
    if not turn.started.is_set():
      self._specs.pop(turn.dispatch_id, None)
      turn.finish("stopped", "Stopped.")
      # Consumption is irreversible even before task-start supplies identity.
      # A known resumed agent can be stopped now; a newly spawned task is
      # stopped when its late task-start supplies the provider id.
      if turn.agent_id and turn.dispatch_consumed:
        await self._stop_agent(turn.agent_id)
      return
    if turn.agent_id:
      await self._stop_agent(turn.agent_id)


def _records_own_question(input_data: dict, sink) -> bool:
  """Whether this tool result is the helper's own recorded ask_parent question."""
  from app.owner_card_receipts import turn_end_receipt_id
  from app.platform_tools import ASK_PARENT_TOOL_NAME, CONTROL_SERVER_NAME

  # Bash is the supported script fallback and emits the same receipt.
  # The sink, not the command text, owns this exact turn-ending question.
  if (input_data.get("hook_event_name") == "PostToolUseFailure"
      or input_data.get("tool_name") not in {
        f"mcp__{CONTROL_SERVER_NAME}__{ASK_PARENT_TOOL_NAME}", "Bash",
      }):
    return False
  receipt_id = turn_end_receipt_id(input_data.get("tool_response"))
  ends_turn = getattr(sink, "ends_turn", None)
  return receipt_id is not None and callable(ends_turn) and bool(ends_turn(receipt_id))


def _message_text(message) -> str:
  return "".join(
    block.text for block in getattr(message, "content", None) or []
    if isinstance(getattr(block, "text", None), str)
  ).strip()


def _allow(updated: dict) -> dict:
  return {"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": updated,
  }}


def _deny(reason: str) -> dict:
  return {"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": reason,
  }}


class ActiveClaudeHelperTurn:
  """Registry handle so Stop reaches a helper that runs inside a host."""

  def __init__(self, chat_id: str, host: ClaudeHelperHost, turn: HelperTurn, marker: str):
    from app.runner_registry import RunnerKind
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self._host = host
    self._turn = turn
    self._marker = marker
    self.stop_requested = False
    self._finished = asyncio.Event()

  @property
  def is_steerable(self) -> bool:
    return False

  async def stop(self, timeout: float = 2.0) -> bool:
    self.stop_requested = True
    await self._host.stop(self._turn)
    try:
      await asyncio.wait_for(self._finished.wait(), timeout=timeout)
      return True
    except asyncio.TimeoutError:
      return False

  async def suspend(self, timeout: float = 2.0) -> bool:
    """End this turn for a planned restart and leave its agent resumable.

    Claude Code refuses to resume an agent that was stopped (``stop_task``)
    but resumes one whose host process ended, with its whole transcript. So a
    restart ends the host itself; the same drain parks every turn in it.
    """
    self.stop_requested = True
    await helper_hosts.MANAGER.discard(self._host)
    try:
      await asyncio.wait_for(self._finished.wait(), timeout=timeout)
      return True
    except asyncio.TimeoutError:
      return False

  async def force_stop(self, timeout: float = 5.0) -> bool:
    self.stop_requested = True
    await self._host.stop(self._turn)
    from app.process_groups import terminate_run_processes
    await asyncio.to_thread(
      terminate_run_processes, self._marker, logger=log, label="Claude helper",
    )
    self._turn.finish("stopped", "Stopped.")
    try:
      await asyncio.wait_for(self._finished.wait(), timeout=timeout)
      return True
    except asyncio.TimeoutError:
      return False

  async def interrupt(self) -> None:
    await self.stop()

  def mark_finished(self) -> None:
    self._finished.set()


def _host_options(
  *, key: HostKey, host_env: dict, skill_text: str, connector_plan,
  skills_enabled: bool, model: str | None, supports_effort: bool,
):
  """Build the host's Claude Code options once per host start."""
  from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, HookMatcher
  from app.claude_sdk_runner import (
    _CLAUDE_NATIVE_OWNER_INPUT_TOOLS,
    _CLAUDE_NATIVE_SCHEDULING_TOOLS,
    _CLAUDE_UNUSED_BUILTINS,
    _claude_cli_path,
    _system_prompt_with_register,
  )
  from app.connectors import claude_mcp_config_handle
  from app.platform_tools import claude_control_servers

  blocked = [
    # Supported AgentDefinition restriction: reports belong to Möbius, not
    # Claude's classifier-reviewed native handback. Resumed agents keep their
    # original definitions, so HelperReport still owns legacy handback ordering.
    HANDBACK_TOOL,
    *BUILTIN_HELPER_TOOLS,
    *_CLAUDE_NATIVE_SCHEDULING_TOOLS,
    *_CLAUDE_NATIVE_OWNER_INPUT_TOOLS,
    *_CLAUDE_UNUSED_BUILTINS,
  ]

  # The Agent tool accepts only model aliases, so the exact model lives on
  # the helper definitions; the host key includes it (one host per model).
  def definition(effort: str | None) -> AgentDefinition:
    return AgentDefinition(
      description="A Möbius helper working on one delegated task.",
      prompt=_system_prompt_with_register(skill_text),
      disallowedTools=blocked,
      effort=effort if supports_effort else None,
      model=model,
    )

  agents = {agent_type_for(None): definition(None)}
  agents.update({agent_type_for(e): definition(e) for e in EFFORTS})

  def factory(host: ClaudeHelperHost, resume: str | None, stack: ExitStack):
    def capture_stderr(line: str) -> None:
      if line:
        host.stderr_tail.append(line.rstrip("\n")[:300])
        del host.stderr_tail[:-50]

    async def allow_all(_tool, _input, _context):
      from claude_agent_sdk.types import PermissionResultAllow
      return PermissionResultAllow()

    options: dict[str, Any] = dict(
      system_prompt=DISPATCHER_PROMPT,
      model=DISPATCH_MODEL,
      cwd=key.cwd,
      env=host_env,
      resume=resume,
      strict_mcp_config=True,
      setting_sources=["user", "project"] if skills_enabled else None,
      include_partial_messages=True,
      # Without this the SDK forwards helper tools, but not the text that
      # owns its visible answer.
      forward_subagent_text=True,
      can_use_tool=allow_all,
      disallowed_tools=[
        *_CLAUDE_NATIVE_SCHEDULING_TOOLS,
        *_CLAUDE_NATIVE_OWNER_INPUT_TOOLS,
        *_CLAUDE_UNUSED_BUILTINS,
        "Workflow",
      ],
      agents=agents,
      cli_path=_claude_cli_path(),
      stderr=capture_stderr,
      hooks={
        "PreToolUse": [HookMatcher(matcher=None, hooks=[host.pre_tool_use])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[host.post_tool_use])],
        "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[host.post_tool_use])],
        "PreCompact": [HookMatcher(matcher=None, hooks=[host.pre_compact])],
        "SubagentStop": [HookMatcher(hooks=[host.subagent_stop])],
      },
      extra_args={"settings": json.dumps({"disableWorkflows": True})},
    )
    if skills_enabled:
      options["skills"] = "all"
    handle = stack.enter_context(claude_mcp_config_handle(
      connector_plan,
      extra_servers=claude_control_servers(enabled=True),
    ))
    if handle:
      options["mcp_servers"] = handle.path
    return ClaudeAgentOptions(**options)

  return factory


async def run_claude_host_turn(
  *,
  user_message: str,
  session_id: str | None,
  base_env: dict[str, str],
  chat_id: str,
  skill_text: str,
  bc,
  agent_settings: dict | None,
  skills_enabled: bool,
  run_policy,
  connector_plan,
  helper_host_key: HostKey,
  data_dir: str,
  goal_brief_refresh=None,
) -> dict:
  """Run one delegated Claude helper turn inside its parent's shared host."""
  from app.process_groups import RUN_MARKER_ENV, terminate_run_processes
  from app.runner_registry import registry, RunnerKind

  host_env, turn_env = helper_hosts.split_env(base_env)
  host_env[helper_hosts.HOST_MARKER_ENV] = helper_hosts.host_marker(helper_host_key.digest)
  marker = turn_env.get(RUN_MARKER_ENV, "")
  env_file = TurnEnvFile(Path(turn_env.get("TMPDIR") or data_dir), marker, turn_env)
  settings = agent_settings or {}
  model = settings.get("model") or (run_policy.model if run_policy else None)
  effort = settings.get("effort") or (run_policy.effort if run_policy else None)
  from app.providers import model_supports_effort
  supports_effort = await model_supports_effort(data_dir, model)
  if not supports_effort:
    effort = None
  _host_session, agent_id, launch_tool_use_id = parse_session(session_id)
  dispatch_id = f"d{uuid.uuid4().hex[:16]}"

  def spawn_spec(prompt: str) -> dict:
    return {
      "description": dispatch_id,
      "prompt": prompt,
      "subagent_type": agent_type_for(effort),
      "run_in_background": True,
    }

  if agent_id:
    # The SDK keeps an existing agent's launch options; do not recreate it
    # on capability changes, since that could replay already-completed work.
    turn = HelperTurn(
      dispatch_id=dispatch_id, kind="message",
      spec={"to": agent_id, "summary": dispatch_id, "message": user_message},
      sink=bc, env_file=env_file, agent_id=agent_id,
      launch_tool_use_id=launch_tool_use_id,
      goal_brief_refresh=goal_brief_refresh,
    )
  else:
    # A first turn may spawn; a lost provider agent must not replay work.
    if session_id:
      # A trusted task may have changed state; never replay lost provider work.
      from app.delegations import REVIEW_REQUIRED_MARKER
      return {
        "session_id": session_id, "cost_usd": None,
        "error": (
          f"{REVIEW_REQUIRED_MARKER}: This helper's session could not "
          "be resumed. Its durable history is intact, but Möbius will not "
          "replay work automatically; start a new helper if another "
          "pass is needed."
        ),
      }
    turn = HelperTurn(
      dispatch_id=dispatch_id, kind="spawn", spec=spawn_spec(user_message),
      sink=bc, env_file=env_file, goal_brief_refresh=goal_brief_refresh,
    )

  factory = _host_options(
    key=helper_host_key, host_env=host_env, skill_text=skill_text,
    connector_plan=connector_plan, skills_enabled=skills_enabled, model=model,
    supports_effort=supports_effort,
  )
  session_file = Path(data_dir) / "run" / "helper-hosts" / f"{helper_host_key.digest}.json"
  handle: ActiveClaudeHelperTurn | None = None
  try:
    async with helper_hosts.MANAGER.lease(
      helper_host_key,
      lambda: ClaudeHelperHost(
        helper_host_key, options_factory=factory, session_file=session_file,
      ),
    ) as host:
      handle = ActiveClaudeHelperTurn(chat_id, host, turn, marker)
      registry.register(handle)
      started_at = time.monotonic()

      async def on_started(started: HelperTurn) -> None:
        await record_reference(chat_id, resume_reference(host.session_id, started))

      await host.run_turn(turn, on_started)
      if turn.kind == "message" and turn.dispatch_error:
        from app.delegations import REVIEW_REQUIRED_MARKER
        failure = (
          "Claude could not deliver the message to this helper's session."
          if turn.dispatch_error == "unreachable" else
          "Claude's dispatcher did not start this helper's follow-up. "
          "This does not establish that its session was lost."
        )
        return {
          "session_id": session_id, "cost_usd": None,
          "error": (
            f"{REVIEW_REQUIRED_MARKER}: {failure} "
            "Its durable history is intact, but Möbius will not "
            "replay work automatically; start a new helper if another pass "
            "is needed."
          ),
        }
      if turn.host_lost:
        # Clear the dead agent pointer; a later turn needs parent review rather
        # than an automatic replay. A failed turn's result never reaches it.
        await record_reference(chat_id, resume_reference(host.session_id, turn))
      if turn.dispatch_error:
        return {
          "session_id": session_id, "cost_usd": None,
          "error": turn.summary or "The helper host could not start this helper.",
        }
      result: dict[str, Any] = {
        "session_id": resume_reference(host.session_id, turn),
        "cost_usd": None,
        "error": None,
      }
      if turn.status == "failed" or (
        turn.status == "stopped" and not handle.stop_requested
      ):
        # A provider error reaches the host only as the helper's own last
        # message; report it as the private runner reports its result, so a
        # usage limit parks the turn until the limit resets.
        kind, text = turn.api_error or (None, None)
        result["error"] = text or turn.summary or (
          "The helper failed." if turn.status == "failed"
          else "The helper stopped unexpectedly."
        )
        if kind == "rate_limit":
          result["api_error_status"] = 429
      # A follow-up resumes the same Claude task, whose counters may include
      # earlier turns; only a freshly spawned task's counters are this turn's.
      usage = normalize_claude_helper_task_usage(
        final=turn.final_usage, progress=turn.progress_usage,
        attributable=turn.kind == "spawn",
        turn_duration_ms=int((time.monotonic() - started_at) * 1000),
      )
      if usage is not None:
        result["usage_metrics"] = usage
      return result
  finally:
    if handle is not None:
      if registry.get_handle(chat_id, RunnerKind.CLAUDE_SDK) is handle:
        registry.unregister(chat_id, RunnerKind.CLAUDE_SDK)
      handle.mark_finished()
    env_file.remove()
    await asyncio.to_thread(
      terminate_run_processes, marker, logger=log, label="Claude helper",
    )
