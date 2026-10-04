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

log = logging.getLogger(__name__)

SESSION_PREFIX = "claude-host:"
DISPATCH_MODEL = "haiku"
DISPATCH_START_TIMEOUT = 90.0
DISPATCH_ATTEMPTS = 3
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BUILTIN_HELPER_TOOLS = ("Agent", "Task", "Workflow")
DISPATCHER_PROMPT = (
  "You are a dispatcher inside Möbius. You never do any work yourself and "
  "never write prose. Möbius sends you command lines:\n"
  '- "SPAWN <id>": call Agent with description "<id>", prompt "<id>", '
  'subagent_type "general-purpose", run_in_background true.\n'
  '- "MESSAGE <id>": call SendMessage with to "<id>", summary "<id>", '
  'message "<id>".\n'
  "Use the id exactly as given in every field; Möbius fills in the real "
  "values. Whenever your input contains command lines, make every matching "
  "call in ONE assistant message, even when the same input also holds task "
  "notifications or other text, then reply with exactly: OK\n"
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
  # Capturing report bytes is not proof that a native tool/attempt succeeded.
  result: str | None = None
  handback_seen: bool = False
  last_response: str | None = None
  usage: dict | None = None
  dispatch_error: str | None = None
  # The helper's last API error from its provider, as (kind, message text).
  api_error: tuple[str, str] | None = None
  # The host process died under this turn: its agent cannot be resumed.
  host_lost: bool = False
  started: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
  done: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
  session_state: dict = dataclasses.field(default_factory=dict)

  def finish(self, status: str, summary: str | None = None, usage=None) -> None:
    if self.done.is_set():
      return
    self.status, self.summary, self.usage = status, summary, usage
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
    self._query_lock = asyncio.Lock()
    # Resolved when the dispatcher finishes its next reply.
    self._dispatcher_replies: list[asyncio.Future] = []
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
    if self._closed or self._client is None:
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
    self._stack.close()

  # ------------------------------------------------------------------ hooks

  async def pre_tool_use(self, input_data, tool_use_id, context) -> dict:
    name = input_data.get("tool_name") or ""
    tool_input = input_data.get("tool_input") or {}
    agent = input_data.get("agent_id")
    if not agent:
      return self._dispatcher_call(name, tool_input, tool_use_id)
    turn = await self._turn_for_agent(agent)
    if name in BUILTIN_HELPER_TOOLS:
      return _deny(
        "Delegate with the Möbius spawn_agent tool instead; built-in helper "
        "tools are not available.",
      )
    if turn is None:
      return {}
    if name == "SubagentHandback" and turn.started.is_set() and not turn.done.is_set():
      # A handback may replace final assistant prose. Never fall back to its
      # closing text, even when the handback itself later fails. Möbius owns
      # delivery of this report independently; task status still owns success.
      turn.handback_seen = True
      report = tool_input.get("message")
      if isinstance(report, str):
        turn.result = report
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
    key = (
      tool_input.get("description") if name == "Agent"
      else tool_input.get("summary") if name == "SendMessage"
      else None
    )
    # Each dispatch is used once: a late call and a repeated command must never
    # launch or message a helper twice.
    spec = self._specs.pop(key, None) if isinstance(key, str) else None
    turn = self._turn_by_dispatch.get(key) if isinstance(key, str) else None
    if spec is None or turn is None:
      return _deny("Only dispatches registered by Möbius are allowed.")
    if tool_use_id:
      self._turn_by_tool_use[tool_use_id] = turn
    return _allow(spec)

  async def post_tool_use(self, input_data, tool_use_id, context) -> dict:
    """A SendMessage the host could not deliver fails its turn at once."""
    if input_data.get("agent_id") or input_data.get("tool_name") != "SendMessage":
      return {}
    turn = self._turn_by_tool_use.get(tool_use_id or "")
    response = input_data.get("tool_response")
    text = json.dumps(response) if not isinstance(response, str) else response
    if turn is not None and '"success": false' in text.replace('":false', '": false'):
      turn.dispatch_error = "unreachable"
      turn.started.set()
    return {}

  async def subagent_stop(self, input_data, tool_use_id, context) -> dict:
    """Capture the documented final response, never a task notification summary.

    https://code.claude.com/docs/en/hooks#subagentstop: with SubagentHandback,
    last_assistant_message is only closing text; PreToolUse owns that report.
    """
    turn = self._turn_by_agent.get(input_data.get("agent_id"))
    report = input_data.get("last_assistant_message")
    if (turn is not None and turn.started.is_set() and not turn.done.is_set()
        and not turn.handback_seen and isinstance(report, str)):
      turn.result = report
    return {}

  async def _turn_for_agent(self, agent_id: str) -> HelperTurn | None:
    # The helper's first tool call can race the host's task_started message.
    for _ in range(40):
      turn = self._turn_by_agent.get(agent_id)
      if turn is not None:
        return turn
      await asyncio.sleep(0.05)
    return None

  # ------------------------------------------------------------------ stream

  async def _read(self) -> None:
    from claude_agent_sdk.types import (
      AssistantMessage,
      ResultMessage,
      TaskNotificationMessage,
      TaskStartedMessage,
      TaskUpdatedMessage,
      ToolUseBlock,
    )
    from app.claude_events import dispatch_sdk_message
    try:
      async for message in self._client.receive_messages():
        if isinstance(message, TaskStartedMessage):
          self._on_task_started(message)
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
          self._dispatcher_replied()
          continue
        parent = getattr(message, "parent_tool_use_id", None)
        error = getattr(message, "error", None)
        if not parent:
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
            # The ordered child stream is available before its task-end event,
            # even when the SDK hook callback reaches us later. Never use the
            # dispatcher's prose or overwrite a handback with closing text.
            for block in message.content:
              if isinstance(block, ToolUseBlock) and block.name == "SubagentHandback":
                turn.handback_seen = True
                report = block.input.get("message")
                if isinstance(report, str):
                  turn.result = report
            text = _message_text(message)
            if text.strip():
              turn.last_response = text
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
      return
    # A resumed helper reports under its original launch id, so its current
    # (follow-up) turn wins over whatever turn first used that id.
    current = self._turn_by_agent.get(message.task_id)
    if current is not None and not current.done.is_set():
      turn = current
    else:
      turn = self._turn_by_tool_use.get(message.tool_use_id or "")
    if turn is None:
      return
    turn.agent_id = message.task_id
    self._turn_by_agent[message.task_id] = turn
    if message.tool_use_id:
      self._agent_of_tool_use[message.tool_use_id] = message.task_id
      if turn.launch_tool_use_id is None:
        turn.launch_tool_use_id = message.tool_use_id
    turn.started.set()

  def _on_task_end(self, task_id, status, summary, usage) -> None:
    turn = self._turn_by_agent.get(task_id)
    # A follow-up turn is mapped to its agent before the agent resumes, and
    # each run reports its end more than once; a late report of the previous
    # run must not end the follow-up before it starts.
    if turn is None or turn.done.is_set() or not turn.started.is_set():
      return
    normalized = {"completed": "completed", "failed": "failed"}.get(status, "stopped")
    report = turn.result
    if report is None and not turn.handback_seen:
      report = turn.last_response
    if report is not None:
      turn.sink.publish({"type": "assistant_result", "content": report})
    turn.finish(normalized, summary, dict(usage) if usage else None)

  # ------------------------------------------------------------------ work

  async def run_turn(self, turn: HelperTurn, on_started=None) -> HelperTurn:
    """Dispatch one helper turn and wait until it settles.

    ``on_started`` is awaited once the turn's agent exists, before the turn
    settles.
    """
    self._specs[turn.dispatch_id] = turn.spec
    self._turn_by_dispatch[turn.dispatch_id] = turn
    if turn.kind == "message" and turn.agent_id:
      # Route the resumed helper's output to this turn from its first event;
      # its messages carry its original launch id.
      self._turn_by_agent[turn.agent_id] = turn
      if turn.launch_tool_use_id:
        self._agent_of_tool_use[turn.launch_tool_use_id] = turn.agent_id
    try:
      verb = "SPAWN" if turn.kind == "spawn" else "MESSAGE"
      loop = asyncio.get_running_loop()
      deadline = loop.time() + DISPATCH_START_TIMEOUT
      # The dispatcher is a model and sometimes answers a command without
      # making its call (seen after helpers finish or a host resumes); it
      # usually makes it when asked again.
      for attempt in range(1, DISPATCH_ATTEMPTS + 1):
        replied = self._next_dispatcher_reply()
        async with self._query_lock:
          await self._client.query(f"{verb} {turn.dispatch_id}")
        if await self._await_dispatch(turn, replied, deadline):
          break
        log.info(
          "helper dispatch %s %s: dispatcher replied without the call "
          "(attempt %d) key=%s dispatcher_last=%r",
          turn.kind, turn.dispatch_id, attempt, self.key.digest,
          self.dispatcher_last,
        )
      if not turn.started.is_set():
        turn.dispatch_error = turn.dispatch_error or "timeout"
        log.warning(
          "helper dispatch %s %s never started its helper key=%s "
          "tool_call_seen=%s alive=%s dispatcher_last=%r stderr=%s",
          turn.kind, turn.dispatch_id, self.key.digest,
          any(seen is turn for seen in self._turn_by_tool_use.values()),
          self.alive,
          self.dispatcher_last, " | ".join(self.stderr_tail[-8:]),
        )
      if turn.dispatch_error and not turn.done.is_set():
        return turn
      if on_started is not None and turn.agent_id:
        await on_started(turn)
      await turn.done.wait()
      return turn
    finally:
      self._specs.pop(turn.dispatch_id, None)
      self._turn_by_dispatch.pop(turn.dispatch_id, None)

  def _next_dispatcher_reply(self) -> asyncio.Future:
    reply = asyncio.get_running_loop().create_future()
    self._dispatcher_replies.append(reply)
    return reply

  def _dispatcher_replied(self) -> None:
    replies, self._dispatcher_replies = self._dispatcher_replies, []
    for reply in replies:
      if not reply.done():
        reply.set_result(None)

  async def _await_dispatch(self, turn: HelperTurn, replied, deadline: float) -> bool:
    """Wait for the helper to start; False means ask the dispatcher again.

    That is when the dispatcher finished a reply and this dispatch's call is
    still unmade. Once the call is made, only the helper's start (or the
    deadline) ends the wait.
    """
    if turn.started.is_set():
      return True
    loop = asyncio.get_running_loop()
    started = asyncio.ensure_future(turn.started.wait())
    try:
      while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
          return True
        await asyncio.wait(
          {started, replied}, timeout=remaining,
          return_when=asyncio.FIRST_COMPLETED,
        )
        if turn.started.is_set() or not replied.done():
          return True
        if turn.dispatch_id in self._specs:
          return False
        replied = self._next_dispatcher_reply()
    finally:
      started.cancel()

  async def stop(self, turn: HelperTurn) -> None:
    if turn.agent_id and self._client is not None:
      with contextlib.suppress(Exception):
        await self._client.stop_task(turn.agent_id)


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
      # owns its visible answer and provider-neutral write intents.
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
        "PostToolUse": [HookMatcher(matcher="SendMessage", hooks=[host.post_tool_use])],
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
      sink=bc, env_file=env_file,
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
        return {
          "session_id": session_id, "cost_usd": None,
          "error": (
            f"{REVIEW_REQUIRED_MARKER}: This helper's session could not be "
            "reached. Its durable history is intact, but Möbius will not "
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
          "error": "The helper host could not start this helper.",
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
      if turn.usage:
        result["usage_metrics"] = {
          "total_tokens": turn.usage.get("total_tokens"),
          "duration_ms": turn.usage.get("duration_ms")
          or int((time.monotonic() - started_at) * 1000),
        }
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
