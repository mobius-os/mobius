"""CLI subprocess backend — parses `claude --print --output-format stream-json`
into the SDK-shaped typed messages defined in types.py.

LEVEL-1 behavior for AskUserQuestion: when the agent emits an
`AskUserQuestion` tool_use, we kill the subprocess BEFORE the CLI's
internal auto-resolution (synthetic `is_error="Answer questions?"`) can
leak a `tool_result` through, and emit a synthetic ResultMessage so the
caller's lifecycle ends cleanly. The user's answer arrives as a new
turn via `--resume`, exactly like today's flow — the difference is the
agent's post-question tool_starts no longer pollute the UI.

LEVEL-2 will replace this with a `can_use_tool` callback that suspends
inside the wrapper and feeds the answer as a synthetic tool_result via
stream-json input, matching SDK semantics 1:1. Not in this file yet.

Why parse here instead of reusing providers.py: the existing
`provider.parse_line` produces *mobius-wire* events (text/tool_start/
question/done). The wrapper's job is to produce typed SDK-shaped
messages so a future SDK swap is mechanical. The translation from typed
messages back to mobius-wire happens in chat.py, one layer up.

Subprocess launching uses `asyncio.create_subprocess_exec` (the
non-shell variant — argv list, no shell interpolation). This is the
Python equivalent of execve/execFile and does not risk shell injection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator

from app.agent_runtime.options import AgentOptions
from app.agent_runtime.types import (
  AssistantMessage,
  Message,
  PartialAssistantMessage,
  ResultMessage,
  SystemMessage,
  TextBlock,
  ToolResultBlock,
  ToolUseBlock,
  UserMessage,
)


_log = logging.getLogger("mobius.agent_runtime.cli")


def _resolve_skill_path() -> Path | None:
  """Locate the agent skill file packaged with the image."""
  candidates = [
    Path("/app/skill/agent-skill.md"),
    Path(__file__).resolve().parents[3] / "skill" / "agent-skill.md",
  ]
  for p in candidates:
    if p.exists():
      return p
  return None


def _build_claude_cmd(
  user_message: str,
  options: AgentOptions,
) -> list[str]:
  """Build the claude CLI argv (no shell interpolation)."""
  cmd = [
    "claude",
    "-p",
    "--output-format", "stream-json",
    "--verbose",
    # Without this, the CLI closes stdout without emitting a result —
    # the stream ends silently with no assistant response. Required
    # for stream-json output to actually stream.
    "--include-partial-messages",
    "--dangerously-skip-permissions",
  ]

  if options.resume:
    cmd += ["--resume", options.resume]
  else:
    if options.session_id:
      cmd += ["--session-id", options.session_id]
    if options.system_prompt_file:
      cmd += ["--system-prompt-file", options.system_prompt_file]
    elif options.system_prompt:
      cmd += ["--append-system-prompt", options.system_prompt]
    else:
      skill = _resolve_skill_path()
      if skill:
        cmd += ["--system-prompt-file", str(skill)]

  if options.model:
    cmd += ["--model", options.model]
  if options.effort:
    cmd += ["--effort", options.effort]

  cmd += ["--", user_message]
  return cmd


def _content_blocks_from_api(content: list) -> list:
  """Translate API-shaped content blocks (dicts) into typed dataclasses."""
  blocks = []
  for c in content or []:
    t = c.get("type")
    if t == "text":
      blocks.append(TextBlock(text=c.get("text", "")))
    elif t == "tool_use":
      blocks.append(ToolUseBlock(
        id=c.get("id", ""),
        name=c.get("name", ""),
        input=c.get("input", {}) or {},
      ))
    elif t == "tool_result":
      blocks.append(ToolResultBlock(
        tool_use_id=c.get("tool_use_id", ""),
        content=c.get("content", ""),
        is_error=bool(c.get("is_error", False)),
      ))
  return blocks


def _parse_line(line: str, session_id: str) -> tuple[Message | None, str]:
  """Parse one CLI stream-json line into a typed Message.

  Returns (message_or_None, possibly_updated_session_id). session_id
  is updated when we see a system.init carrying the CLI-assigned id.
  """
  try:
    event = json.loads(line)
  except json.JSONDecodeError:
    return None, session_id

  etype = event.get("type")

  if etype == "system":
    sub = event.get("subtype", "")
    sid = event.get("session_id") or session_id
    if sub == "init" and event.get("session_id"):
      sid = event["session_id"]
    return SystemMessage(subtype=sub, session_id=sid, data=event), sid

  if etype == "stream_event":
    inner = event.get("event") or {}
    sid = event.get("session_id") or session_id
    return PartialAssistantMessage(
      event=inner,
      session_id=sid,
      parent_tool_use_id=event.get("parent_tool_use_id"),
    ), sid

  if etype == "assistant":
    msg = event.get("message") or {}
    sid = event.get("session_id") or session_id
    return AssistantMessage(
      content=_content_blocks_from_api(msg.get("content") or []),
      session_id=sid,
      model=msg.get("model"),
      parent_tool_use_id=event.get("parent_tool_use_id"),
    ), sid

  if etype == "user":
    msg = event.get("message") or {}
    sid = event.get("session_id") or session_id
    content = msg.get("content") or []
    if isinstance(content, str):
      content = [{"type": "text", "text": content}]
    return UserMessage(
      content=_content_blocks_from_api(content),
      session_id=sid,
      parent_tool_use_id=event.get("parent_tool_use_id"),
    ), sid

  if etype == "result":
    sid = event.get("session_id") or session_id
    return ResultMessage(
      subtype=event.get("subtype", "success"),
      session_id=sid,
      is_error=bool(event.get("is_error", False)),
      duration_ms=int(event.get("duration_ms", 0)),
      num_turns=int(event.get("num_turns", 0)),
      result=event.get("result"),
      total_cost_usd=float(event.get("total_cost_usd", 0) or 0),
      usage=event.get("usage") or {},
      stop_reason=event.get("stop_reason"),
    ), sid

  return None, session_id


def _assistant_has_ask_user_question(msg: AssistantMessage) -> bool:
  """True iff this assistant message contains an AskUserQuestion
  tool_use with a populated questions array.

  --include-partial-messages delivers progressive updates as the tool
  input is assembled. Initial partials have empty/incomplete questions.
  Wait for fully-formed questions before treating it as a real event
  (otherwise we'd kill the subprocess on the first empty partial).
  """
  for block in msg.content:
    if isinstance(block, ToolUseBlock) and block.name == "AskUserQuestion":
      questions = (block.input or {}).get("questions") or []
      if questions and all(q.get("question") for q in questions):
        return True
  return False


async def _drain_stderr(stream: asyncio.StreamReader) -> None:
  """Discard stderr so the subprocess doesn't block on a full pipe."""
  try:
    async for _ in stream:
      pass
  except Exception:
    pass


async def query_cli(
  user_message: str,
  options: AgentOptions,
  *,
  proc_handle: dict | None = None,
) -> AsyncIterator[Message]:
  """Spawn the Claude CLI and yield typed messages until the turn ends.

  AskUserQuestion handling: as soon as we see an AssistantMessage
  carrying a populated AskUserQuestion tool_use, we kill the
  subprocess and emit a synthetic ResultMessage(stop_reason=
  'ask_user_question'). The caller maps this back to mobius's normal
  turn-end semantics.

  `proc_handle`, if provided, is a dict the caller can use to grab
  the live subprocess for an external stop. Mobius uses this to
  register the process in `_active_procs` for the Stop button.
  """
  cmd = _build_claude_cmd(user_message, options)
  env = dict(os.environ)
  env.update(options.env or {})
  cwd = options.cwd or os.getcwd()

  _log.info(
    "spawning claude: resume=%s session=%s msg_len=%d",
    options.resume, options.session_id, len(user_message),
  )

  proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=cwd,
    env=env,
    limit=1024 * 1024,
  )
  if proc_handle is not None:
    proc_handle["proc"] = proc

  stderr_task = asyncio.ensure_future(_drain_stderr(proc.stderr))
  session_id = options.resume or options.session_id or ""
  killed_for_question = False

  try:
    async for raw in proc.stdout:
      line = raw.decode("utf-8", errors="replace").strip()
      if not line:
        continue

      message, session_id = _parse_line(line, session_id)
      if message is None:
        continue

      yield message

      if isinstance(message, AssistantMessage) and \
         _assistant_has_ask_user_question(message):
        killed_for_question = True
        try:
          proc.kill()
          await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
          _log.warning("claude proc didn't exit after kill within 2s")
        yield ResultMessage(
          subtype="success",
          session_id=session_id,
          is_error=False,
          duration_ms=0,
          num_turns=0,
          stop_reason="ask_user_question",
        )
        return

  finally:
    if not killed_for_question and proc.returncode is None:
      try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
      except asyncio.TimeoutError:
        proc.kill()
        try:
          await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
          pass
    if not stderr_task.done():
      stderr_task.cancel()
      try:
        await stderr_task
      except (asyncio.CancelledError, Exception):
        pass
