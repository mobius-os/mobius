"""Hermetic end-to-end proof of Möbius's hooks under ``on_failure="block"``.

Möbius's identity/env PreToolUse hook and queued-prompt gate set
``on_failure="block"``. This drives the bundled Claude Code CLI through
claude_agent_sdk against the scripted fake Messages API and scrubbed
environment of ``test_claude_subagent_hook_stop_e2e``: the model asks for one
Bash call. A raising hook must block it, while Möbius's normal outputs (an
open prompt gate, an allow with a rewritten command, a deny with its reason)
must keep their meaning. No real model or credential is used.
"""

from __future__ import annotations

import asyncio
import json

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


async def _run_probe(scrubbed_env, event: str, hook) -> None:
  from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
  from claude_agent_sdk.types import PermissionResultAllow

  async def allow_all(name, tool_input, context):
    return PermissionResultAllow(updated_input=tool_input)

  options = ClaudeAgentOptions(
    system_prompt=PROBE_SYS, cwd=str(scrubbed_env), cli_path=str(CLI),
    permission_mode="default", can_use_tool=allow_all, model="claude-fake",
    hooks={event: [HookMatcher(matcher=None, hooks=[hook], on_failure="block")]},
  )

  async def run():
    async with ClaudeSDKClient(options) as client:
      await client.query("Run the probe.")
      async for _message in client.receive_response():
        pass

  await asyncio.wait_for(run(), timeout=90)


@pytest.mark.asyncio
async def test_raising_pre_tool_use_hook_blocks_the_call(scrubbed_env, fake_api):
  calls: list[str] = []

  async def raising_hook(data, tool_use_id, context):
    calls.append(data.get("tool_name"))
    raise RuntimeError("identity lookup failed")

  await _run_probe(scrubbed_env, "PreToolUse", raising_hook)
  assert calls == ["Bash"]
  assert not fake_api.marker.exists()
  assert [result.get("is_error") for result in fake_api.tool_results] == [True]


@pytest.mark.asyncio
async def test_open_prompt_gate_lets_the_turn_run(scrubbed_env, fake_api):
  # The main runner's queued_prompt_hook is a closure inside its run; this is
  # the exact output it returns while the turn still accepts prompts.
  calls: list[str] = []

  async def open_gate(data, tool_use_id, context):
    calls.append(data.get("hook_event_name"))
    return {"continue_": True}

  await _run_probe(scrubbed_env, "UserPromptSubmit", open_gate)
  assert calls == ["UserPromptSubmit"]
  assert fake_api.marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_helper_host_decisions_hold_under_block(scrubbed_env, fake_api, tmp_path, decision):
  from app.claude_helper_host import _allow, _deny

  rewritten = tmp_path / "rewritten"

  async def pre_tool_use(data, tool_use_id, context):
    if decision == "deny":
      return _deny("Möbius refused this call.")
    command = data["tool_input"]["command"]
    return _allow({**data["tool_input"], "command": f"touch {rewritten} && {command}"})

  await _run_probe(scrubbed_env, "PreToolUse", pre_tool_use)
  if decision == "allow":
    assert rewritten.exists() and fake_api.marker.exists()
  else:
    assert not fake_api.marker.exists()
    [result] = fake_api.tool_results
    assert result.get("is_error") is True
    assert "Möbius refused this call." in json.dumps(result.get("content"), ensure_ascii=False)
