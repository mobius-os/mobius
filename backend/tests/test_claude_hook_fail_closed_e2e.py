"""Hermetic end-to-end proof that a raising PreToolUse hook fails closed.

Möbius's identity/env PreToolUse hook sets ``on_failure="block"``. This drives
the bundled Claude Code CLI through claude_agent_sdk against the scripted fake
Messages API and scrubbed environment of ``test_claude_subagent_hook_stop_e2e``:
the model asks for one Bash call and the hook raises. The command must never
run, and the model must receive an error tool result. No real model or
credential is used.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.test_claude_subagent_hook_stop_e2e import (  # noqa: F401 - scrubbed_env is a fixture
  CLI, FakeAnthropic, _system_text, scrubbed_env,
)

PROBE_SYS = "PROBE-FAIL-CLOSED"


class BashProbeAPI(FakeAnthropic):
  """Asks for one Bash call, then ends the turn once it sees the result."""

  def __init__(self, command: str):
    super().__init__()
    self.command = command
    self.tool_results: list[dict] = []

  def respond(self, body):
    if PROBE_SYS not in _system_text(body):
      return "aux", [{"type": "text", "text": "OK"}]
    last = (body.get("messages") or [{}])[-1]
    content = last.get("content") if isinstance(last.get("content"), list) else []
    results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    if results:
      self.tool_results.extend(results)
      return "probe", [{"type": "text", "text": "DONE"}]
    return "probe", [self._tool("probe", "Bash", {"command": self.command, "description": "probe"})]


@pytest.fixture
def fake_api(tmp_path):
  marker = tmp_path / "ran"
  fake = BashProbeAPI(f"touch {marker}").start()
  fake.marker = marker
  yield fake
  fake.stop()


@pytest.mark.asyncio
async def test_raising_pre_tool_use_hook_blocks_the_call(scrubbed_env, fake_api):
  from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
  from claude_agent_sdk.types import PermissionResultAllow

  calls: list[str] = []

  async def raising_hook(data, tool_use_id, context):
    calls.append(data.get("tool_name"))
    raise RuntimeError("identity lookup failed")

  async def allow_all(name, tool_input, context):
    return PermissionResultAllow(updated_input=tool_input)

  options = ClaudeAgentOptions(
    system_prompt=PROBE_SYS, cwd=str(scrubbed_env), cli_path=str(CLI),
    permission_mode="default", can_use_tool=allow_all, model="claude-fake",
    hooks={"PreToolUse": [HookMatcher(
      matcher=None, hooks=[raising_hook], on_failure="block",
    )]},
  )

  async def run():
    async with ClaudeSDKClient(options) as client:
      await client.query("Run the probe.")
      async for _message in client.receive_response():
        pass

  await asyncio.wait_for(run(), timeout=90)
  assert calls == ["Bash"]
  assert not fake_api.marker.exists()
  assert [result.get("is_error") for result in fake_api.tool_results] == [True]
