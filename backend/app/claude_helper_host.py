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
EFFORTS = ("low", "medium", "high", "xhigh", "max")
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
BUILTIN_HELPER_TOOLS = ("Agent", "Task", "Workflow")
DISPATCHER_PROMPT = (
  "You are a dispatcher inside Möbius. You never do any work yourself and "
  "never write prose. Each user message contains command lines. For EVERY "
  "line make the matching tool call, all in ONE assistant message:\n"
  '- "SPAWN <id>": call Agent with description "<id>", prompt "<id>", '
  'subagent_type "general-purpose", run_in_background true.\n'
  '- "MESSAGE <id>": call SendMessage with to "<id>", summary "<id>", '
  'message "<id>".\n'
  "After the calls reply with exactly: OK\n"
  "For anything else, including task notifications, reply with exactly: OK"
)


def agent_type_for(effort: str | None) -> str:
  return f"mobius-helper-{effort}" if effort in EFFORTS else "mobius-helper"


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
  read_only: bool
  agent_id: str | None = None
  launch_tool_use_id: str | None = None
  status: str | None = None
  summary: str | None = None
  usage: dict | None = None
  dispatch_error: str | None = None
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
    # The CLI's last stderr lines, logged if the host process ends.
    self.stderr_tail: list[str] = []
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
    if turn.read_only and name in WRITE_TOOLS:
      return _deny("This helper is read-only; it may not change files.")
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
    spec = self._specs.get(key) if isinstance(key, str) else None
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
      ResultMessage,
      TaskNotificationMessage,
      TaskStartedMessage,
      TaskUpdatedMessage,
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
          continue
        parent = getattr(message, "parent_tool_use_id", None)
        if not parent:
          session = getattr(message, "session_id", None)
          if session:
            self._save_session(session)
          continue
        turn = self._turn_for_parent(parent)
        if turn is None or turn.done.is_set():
          continue
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
      for turn in list(self._turn_by_dispatch.values()):
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
    if turn is None or turn.done.is_set():
      return
    normalized = {"completed": "completed", "failed": "failed"}.get(status, "stopped")
    turn.finish(normalized, summary, dict(usage) if usage else None)

  # ------------------------------------------------------------------ work

  async def run_turn(self, turn: HelperTurn) -> HelperTurn:
    """Dispatch one helper turn and wait until it settles."""
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
      async with self._query_lock:
        await self._client.query(f"{verb} {turn.dispatch_id}")
      try:
        await asyncio.wait_for(turn.started.wait(), DISPATCH_START_TIMEOUT)
      except asyncio.TimeoutError:
        turn.dispatch_error = turn.dispatch_error or "timeout"
      if turn.dispatch_error and not turn.done.is_set():
        return turn
      await turn.done.wait()
      return turn
    finally:
      self._specs.pop(turn.dispatch_id, None)
      self._turn_by_dispatch.pop(turn.dispatch_id, None)

  async def stop(self, turn: HelperTurn) -> None:
    if turn.agent_id and self._client is not None:
      with contextlib.suppress(Exception):
        await self._client.stop_task(turn.agent_id)


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
  skills_enabled: bool, model: str | None,
):
  """Build the host's Claude Code options once per host start."""
  from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, HookMatcher
  from app.claude_sdk_runner import (
    _CLAUDE_NATIVE_OWNER_INPUT_TOOLS,
    _CLAUDE_NATIVE_SCHEDULING_TOOLS,
    _CLAUDE_UNUSED_BUILTINS,
    _claude_cli_path,
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
      prompt=skill_text,
      disallowedTools=blocked,
      effort=effort,
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
  resumed_context: str | None,
  helper_host_key: HostKey,
  data_dir: str,
) -> dict:
  """Run one delegated Claude helper turn inside its parent's shared host."""
  from app.process_groups import RUN_MARKER_ENV, terminate_run_processes
  from app.runner_registry import registry, RunnerKind

  host_env, turn_env = helper_hosts.split_env(base_env)
  host_env[helper_hosts.HOST_MARKER_ENV] = helper_host_key.digest
  marker = turn_env.get(RUN_MARKER_ENV, "")
  env_file = TurnEnvFile(Path(turn_env.get("TMPDIR") or data_dir), marker, turn_env)
  settings = agent_settings or {}
  model = settings.get("model") or (run_policy.model if run_policy else None)
  effort = settings.get("effort") or (run_policy.effort if run_policy else None)
  _host_session, agent_id, launch_tool_use_id = parse_session(session_id)
  dispatch_id = f"d{uuid.uuid4().hex[:16]}"
  read_only = bool(run_policy and run_policy.scope == "read")

  def spawn_spec(prompt: str) -> dict:
    return {
      "description": dispatch_id,
      "prompt": prompt,
      "subagent_type": agent_type_for(effort),
      "run_in_background": True,
    }

  if agent_id:
    turn = HelperTurn(
      dispatch_id=dispatch_id, kind="message",
      spec={"to": agent_id, "summary": dispatch_id, "message": user_message},
      sink=bc, env_file=env_file, read_only=read_only, agent_id=agent_id,
      launch_tool_use_id=launch_tool_use_id,
    )
  else:
    # A first turn, or a helper whose earlier turns ran outside a host:
    # start it here with its own history as context.
    prompt = user_message
    if session_id and resumed_context:
      prompt = f"{resumed_context}\n\n{user_message}"
    turn = HelperTurn(
      dispatch_id=dispatch_id, kind="spawn", spec=spawn_spec(prompt),
      sink=bc, env_file=env_file, read_only=read_only,
    )

  factory = _host_options(
    key=helper_host_key, host_env=host_env, skill_text=skill_text,
    connector_plan=connector_plan, skills_enabled=skills_enabled, model=model,
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
      await host.run_turn(turn)
      if turn.kind == "message" and turn.dispatch_error:
        if run_policy is not None and not run_policy.allow_session_reseed:
          # Never replay write work automatically (same rule as a lost
          # private session): the parent reviews and restarts it if needed.
          from app.delegations import REVIEW_REQUIRED_MARKER
          return {
            "session_id": session_id, "cost_usd": None,
            "error": (
              f"{REVIEW_REQUIRED_MARKER}: This write helper's session could not "
              "be reached. Its durable history is intact, but Möbius will not "
              "replay write work automatically; start a new helper if another "
              "pass is needed."
            ),
          }
        # The host cannot reach this read-only helper (its host session is
        # gone): reseed it as a new helper from its own chat history.
        log.info("helper %s unreachable in host; reseeding", agent_id)
        prompt = f"{resumed_context}\n\n{user_message}" if resumed_context else user_message
        turn = HelperTurn(
          dispatch_id=f"d{uuid.uuid4().hex[:16]}", kind="spawn",
          spec={}, sink=bc, env_file=env_file, read_only=read_only,
        )
        turn.spec = {**spawn_spec(prompt), "description": turn.dispatch_id}
        handle._turn = turn
        await host.run_turn(turn)
      if turn.dispatch_error:
        return {
          "session_id": session_id, "cost_usd": None,
          "error": "The helper host could not start this helper.",
        }
      result: dict[str, Any] = {
        "session_id": (
          f"{SESSION_PREFIX}{host.session_id or ''}:{turn.agent_id}"
          f":{turn.launch_tool_use_id or ''}"
        ),
        "cost_usd": None,
        "error": None,
      }
      if turn.status == "failed":
        result["error"] = turn.summary or "The helper failed."
      elif turn.status == "stopped" and not handle.stop_requested:
        result["error"] = turn.summary or "The helper stopped unexpectedly."
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
