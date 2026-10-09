"""Hermetic end-to-end probe: PostToolUse ``continue: false`` inside a subagent.

Question this answers about the bundled Claude Code CLI (driven through
claude_agent_sdk exactly the way ``app.claude_helper_host`` drives it: one
long-lived ``ClaudeSDKClient``, a dispatcher main thread that launches
background ``Agent`` subagents and resumes them with ``SendMessage``, hooks for
PreToolUse / PostToolUse / SubagentStop):

1. When the host's PostToolUse hook returns ``{"continue_": False, ...}`` for a
   tool call made *inside* a subagent (hook input carries ``agent_id``), does
   only that subagent end, with no further model request for it, while the
   main session keeps working?
2. What does the host observe (task messages, Agent tool result, SubagentStop
   and its ``last_assistant_message``)?
3. Can the same subagent be resumed later via ``SendMessage`` to its agent id,
   with its transcript (tool call + result) intact? Compare with
   ``stop_task(agent_id)``.

No real model is used. A local fake Anthropic Messages API (127.0.0.1, SSE)
scripts every response and records every request. The environment handed to
the CLI is rebuilt from an allowlist (no inherited credentials), with HOME and
CLAUDE_CONFIG_DIR in a temp dir and nonessential traffic disabled.

Run:  python -m pytest -q -s test_claude_subagent_hook_stop_e2e.py
Set PROBE_VERBOSE=1 to print the request/event timelines.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sdk = pytest.importorskip("claude_agent_sdk")

CLI = Path(sdk.__file__).parent / "_bundled" / "claude"
if not CLI.exists():  # pragma: no cover - environment dependent
  pytest.skip("bundled Claude Code CLI is absent", allow_module_level=True)

DISPATCHER_SYS = "PROBE-DISPATCHER: you launch helpers."
HELPER_SYS = "PROBE-HELPER-SYS: you are a probe helper."
AGENT_TYPE = "probe-helper"
MCP_TOOL = "mcp__probe__ask_parent"  # stands in for mobius_control ask_parent
STOP_MARKER = "marker-1"
VERBOSE = bool(os.environ.get("PROBE_VERBOSE"))


# --------------------------------------------------------------------------
# Fake Anthropic Messages API


def _text_of(content) -> str:
  if isinstance(content, str):
    return content
  out = []
  for block in content or []:
    if not isinstance(block, dict):
      continue
    if block.get("type") == "text":
      out.append(block.get("text", ""))
    elif block.get("type") == "tool_result":
      out.append(_text_of(block.get("content")))
  return "\n".join(out)


def _system_text(body) -> str:
  return _text_of(body.get("system") or "")


def _tool_results(message) -> dict[str, str]:
  content = message.get("content")
  if not isinstance(content, list):
    return {}
  return {
    b.get("tool_use_id"): _text_of(b.get("content"))
    for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
  }


def _plain_text(message) -> str:
  content = message.get("content")
  if isinstance(content, str):
    return content
  return "\n".join(
    b.get("text", "") for b in content or []
    if isinstance(b, dict) and b.get("type") == "text"
  )


class FakeAnthropic:
  """Scripted model. ``requests`` records a compact summary of each call."""

  def __init__(self):
    self.requests: list[dict] = []
    self.lock = threading.Lock()
    self.counter = 0
    self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

  @property
  def url(self) -> str:
    return f"http://127.0.0.1:{self.server.server_address[1]}"

  def start(self):
    self.thread.start()
    return self

  def stop(self):
    self.server.shutdown()
    self.server.server_close()

  def _next_id(self, stem: str) -> str:
    with self.lock:
      self.counter += 1
      return f"toolu_{stem}_{self.counter:03d}"

  # ---------------------------------------------------------------- scripts

  def respond(self, body) -> tuple[str, list[dict]]:
    """Return (kind, content blocks)."""
    system = _system_text(body)
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    if HELPER_SYS in system:
      return "helper", self._helper(messages, last)
    if DISPATCHER_SYS in system:
      return "parent", self._parent(body, messages, last)
    return "aux", [{"type": "text", "text": "OK"}]

  def _helper(self, messages, last):
    results = _tool_results(last)
    text = _plain_text(last)
    for tid in results:
      if "_m2_" in tid:
        return [{"type": "text", "text": "SUB-DONE-2"}]
    follow = re.search(r"FOLLOWUP-\d+", _text_of(last.get("content")))
    if follow and not any("_m2_" in t for t in results):
      return [self._tool("sub_m2", "Bash", {
        "command": f"echo marker-2 {follow.group(0)}", "description": "m2",
      })]
    for tid in results:
      if "_m1_" in tid:
        return [{"type": "text", "text": "SUB-AFTER-M1"}]
      if "_slow_" in tid:
        return [{"type": "text", "text": "SUB-AFTER-SLOW"}]
    if len(messages) == 1:
      if "TASK-MCP" in text:
        return [
          {"type": "text", "text": "SUB-PRE-M1 calling the stop tool"},
          self._tool("sub_m1", MCP_TOOL, {"question": STOP_MARKER}),
        ]
      if "TASK-SLOW" in text:
        return [self._tool("sub_slow", "Bash", {
          "command": "sleep 20; echo slow", "description": "slow",
        })]
      return [
        {"type": "text", "text": "SUB-PRE-M1 calling the stop tool"},
        self._tool("sub_m1", "Bash", {
          "command": f"echo {STOP_MARKER}", "description": "m1",
        }),
      ]
    return [{"type": "text", "text": "SUB-DEFAULT"}]

  def _parent(self, body, messages, last):
    tools = {t.get("name") for t in body.get("tools") or []}
    results = _tool_results(last)
    command = None
    if any("_toolsearch_" in tid for tid in results):
      # Resume the MESSAGE command that needed SendMessage loaded.
      for msg in reversed(messages):
        if msg.get("role") == "user":
          m = re.search(r"MESSAGE (\S+) (FOLLOWUP-\d+)", _plain_text(msg))
          if m:
            command = m
            break
      if command:
        return [self._tool("sendmessage", "SendMessage", {
          "to": command.group(1), "message": command.group(2),
          "summary": command.group(2),
        })]
    if results:
      return [{"type": "text", "text": "OK"}]
    text = _plain_text(last)
    spawn = re.search(r"SPAWN (TASK-\w+)", text)
    if spawn:
      return [self._tool("agent", "Agent", {
        "description": spawn.group(1), "prompt": spawn.group(1),
        "subagent_type": AGENT_TYPE, "run_in_background": True,
      })]
    command = re.search(r"MESSAGE (\S+) (FOLLOWUP-\d+)", text)
    if command:
      if "SendMessage" not in tools:
        return [self._tool("toolsearch", "ToolSearch", {
          "query": "select:SendMessage", "max_results": 1,
        })]
      return [self._tool("sendmessage", "SendMessage", {
        "to": command.group(1), "message": command.group(2),
        "summary": command.group(2),
      })]
    return [{"type": "text", "text": "OK"}]

  def _tool(self, stem, name, tool_input):
    return {"type": "tool_use", "id": self._next_id(stem), "name": name,
            "input": tool_input}

  # ---------------------------------------------------------------- HTTP

  def _record(self, kind, body, blocks):
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    entry = {
      "i": len(self.requests), "t": time.monotonic(), "kind": kind,
      "n_messages": len(messages),
      "last_text": _plain_text(last)[-300:],
      "last_tool_results": {k: v[:200] for k, v in _tool_results(last).items()},
      "reply": [b.get("name") or b.get("text") for b in blocks],
    }
    if kind == "helper":
      # Full (small) transcript for transcript-integrity checks.
      entry["transcript"] = [
        {"role": m.get("role"), "content": m.get("content")} for m in messages
      ]
    with self.lock:
      self.requests.append(entry)

  def _handler(self):
    fake = self

    class Handler(BaseHTTPRequestHandler):
      protocol_version = "HTTP/1.1"

      def log_message(self, *args):
        pass

      def _json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

      def do_GET(self):
        if self.path.startswith("/v1/models"):
          return self._json({"data": [], "has_more": False})
        return self._json({})

      def do_HEAD(self):
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

      def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
          body = json.loads(raw or b"{}")
        except ValueError:
          body = {}
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
          return self._json({"input_tokens": 10})
        if not path.endswith("/v1/messages"):
          return self._json({})
        kind, blocks = fake.respond(body)
        fake._record(kind, body, blocks)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        model = body.get("model") or "claude-fake"
        msg_id = f"msg_fake_{len(fake.requests):04d}"
        usage = {"input_tokens": 10, "output_tokens": 5,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if not body.get("stream"):
          return self._json({
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": blocks, "stop_reason": stop, "stop_sequence": None,
            "usage": usage,
          })
        events = [("message_start", {"type": "message_start", "message": {
          "id": msg_id, "type": "message", "role": "assistant", "model": model,
          "content": [], "stop_reason": None, "stop_sequence": None,
          "usage": {**usage, "output_tokens": 1}}})]
        for index, block in enumerate(blocks):
          if block["type"] == "text":
            events.append(("content_block_start", {"type": "content_block_start",
              "index": index, "content_block": {"type": "text", "text": ""}}))
            events.append(("content_block_delta", {"type": "content_block_delta",
              "index": index, "delta": {"type": "text_delta", "text": block["text"]}}))
          else:
            events.append(("content_block_start", {"type": "content_block_start",
              "index": index, "content_block": {"type": "tool_use",
                "id": block["id"], "name": block["name"], "input": {}}}))
            events.append(("content_block_delta", {"type": "content_block_delta",
              "index": index, "delta": {"type": "input_json_delta",
                "partial_json": json.dumps(block["input"])}}))
          events.append(("content_block_stop",
                         {"type": "content_block_stop", "index": index}))
        events.append(("message_delta", {"type": "message_delta",
          "delta": {"stop_reason": stop, "stop_sequence": None},
          "usage": {"output_tokens": 5}}))
        events.append(("message_stop", {"type": "message_stop"}))
        data = "".join(
          f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    return Handler


# --------------------------------------------------------------------------
# Host-like driver


ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "TERM", "TZ")


@pytest.fixture
def scrubbed_env(monkeypatch, tmp_path, fake_api):
  """Rebuild os.environ from an allowlist; the SDK passes os.environ through."""
  for key in list(os.environ):
    if key not in ENV_ALLOW:
      monkeypatch.delenv(key, raising=False)
  home = tmp_path / "home"
  (home / ".claude").mkdir(parents=True)
  work = tmp_path / "work"
  work.mkdir()
  values = {
    "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
    "TMPDIR": str(tmp_path), "ANTHROPIC_API_KEY": "sk-ant-fake-probe",
    "ANTHROPIC_BASE_URL": fake_api.url,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
  }
  for key, value in values.items():
    monkeypatch.setenv(key, value)
  return work


@pytest.fixture
def fake_api():
  fake = FakeAnthropic().start()
  yield fake
  fake.stop()


class Probe:
  """Minimal ClaudeHelperHost stand-in that records everything it sees."""

  def __init__(self, fake: FakeAnthropic, cwd: Path, *, hook_stop: bool):
    self.fake = fake
    self.cwd = cwd
    self.hook_stop = hook_stop
    self.events: list[dict] = []
    self.task_ids: list[str] = []
    self.terminal: dict[str, list[str]] = {}
    self.on_pre_tool = None
    self.client = None
    self._reader = None
    self.results = 0

  def log(self, **entry):
    entry["t"] = time.monotonic()
    self.events.append(entry)

  async def pre_tool_use(self, data, tool_use_id, context):
    self.log(ev="PreToolUse", agent=data.get("agent_id"), tool=data.get("tool_name"),
             input=json.dumps(data.get("tool_input"))[:160])
    if self.on_pre_tool is not None:
      await self.on_pre_tool(data)
    return {}

  async def post_tool_use(self, data, tool_use_id, context):
    response = data.get("tool_response")
    self.log(ev="PostToolUse", agent=data.get("agent_id"), tool=data.get("tool_name"),
             response=(response if isinstance(response, str)
                       else json.dumps(response))[:300])
    if (self.hook_stop and data.get("agent_id")
        and STOP_MARKER in json.dumps(data.get("tool_input"))):
      self.log(ev="hook-stop-returned", agent=data.get("agent_id"))
      return {"continue_": False, "stopReason": "PROBE-STOP: handed back to parent"}
    return {}

  async def subagent_stop(self, data, tool_use_id, context):
    self.log(ev="SubagentStop", agent=data.get("agent_id"),
             last=data.get("last_assistant_message"),
             keys=sorted(data))
    return {}

  def options(self):
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, HookMatcher
    from claude_agent_sdk.types import PermissionResultAllow

    async def allow_all(_tool, _input, _context):
      return PermissionResultAllow()

    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("ask_parent", "Ask the parent a question and hand back.", {"question": str})
    async def ask_parent(args):
      return {"content": [{"type": "text", "text": f"queued: {args['question']}"}]}

    probe_server = create_sdk_mcp_server("probe", tools=[ask_parent])

    return ClaudeAgentOptions(
      system_prompt=DISPATCHER_SYS, model="haiku", cwd=str(self.cwd),
      env={k: os.environ[k] for k in os.environ},
      strict_mcp_config=True, setting_sources=None,
      mcp_servers={"probe": probe_server},
      include_partial_messages=True, forward_subagent_text=True,
      can_use_tool=allow_all,
      disallowed_tools=["Workflow"],
      agents={AGENT_TYPE: AgentDefinition(
        description="A probe helper.", prompt=HELPER_SYS,
        disallowedTools=["Agent", "Task", "Workflow"],
      )},
      cli_path=str(CLI),
      stderr=lambda line: self.log(ev="stderr", line=line[:200]),
      hooks={
        "PreToolUse": [HookMatcher(matcher=None, hooks=[self.pre_tool_use])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[self.post_tool_use])],
        "SubagentStop": [HookMatcher(hooks=[self.subagent_stop])],
      },
      extra_args={"settings": json.dumps({"disableWorkflows": True})},
    )

  async def __aenter__(self):
    from claude_agent_sdk import ClaudeSDKClient
    self.client = ClaudeSDKClient(self.options())
    await self.client.connect()
    self._reader = asyncio.create_task(self._read())
    return self

  async def __aexit__(self, *exc):
    self._reader.cancel()
    try:
      await asyncio.wait_for(self.client.disconnect(), timeout=10)
    except Exception:
      pass

  async def _read(self):
    from claude_agent_sdk.types import (
      AssistantMessage, ResultMessage, StreamEvent, TaskNotificationMessage,
      TaskStartedMessage, TaskUpdatedMessage, ToolResultBlock, ToolUseBlock,
      UserMessage,
    )
    async for m in self.client.receive_messages():
      if isinstance(m, StreamEvent):
        continue
      if isinstance(m, TaskStartedMessage):
        self.task_ids.append(m.task_id)
        self.log(ev="task_started", task=m.task_id, tool_use=m.tool_use_id,
                 type=getattr(m, "task_type", None))
      elif isinstance(m, TaskNotificationMessage):
        self.terminal.setdefault(m.task_id, []).append(m.status)
        self.log(ev="task_notification", task=m.task_id, status=m.status,
                 summary=(m.summary or "")[:200])
      elif isinstance(m, TaskUpdatedMessage):
        patch = (getattr(m, "data", {}) or {}).get("patch") or {}
        if patch.get("status"):
          self.terminal.setdefault(m.task_id, []).append(patch["status"])
        self.log(ev="task_updated", task=m.task_id, patch=json.dumps(patch)[:200])
      elif isinstance(m, ResultMessage):
        self.results += 1
        self.log(ev="result", subtype=m.subtype, is_error=m.is_error,
                 text=(m.result or "")[:120])
      elif isinstance(m, AssistantMessage):
        self.log(ev="assistant", parent=m.parent_tool_use_id,
                 text="".join(getattr(b, "text", "") or "" for b in m.content)[:120],
                 tools=[b.name for b in m.content if isinstance(b, ToolUseBlock)])
      elif isinstance(m, UserMessage):
        blocks = m.content if isinstance(m.content, list) else []
        for b in blocks:
          if isinstance(b, ToolResultBlock):
            content = b.content if isinstance(b.content, str) else json.dumps(b.content)
            self.log(ev="tool_result", parent=m.parent_tool_use_id,
                     tool_use=b.tool_use_id, is_error=b.is_error,
                     content=(content or "")[:400])
      else:
        self.log(ev=type(m).__name__, sub=getattr(m, "subtype", None),
                 data=json.dumps(getattr(m, "data", None), default=str)[:200]
                 if getattr(m, "subtype", None) == "api_retry" else None)

  async def send(self, line: str):
    await self.client.query(line)

  async def wait_for(self, predicate, timeout=60.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      if predicate():
        return
      await asyncio.sleep(0.1)
    self.dump()
    raise AssertionError(f"timed out waiting for {what}")

  def ended(self, task_id, after=0):
    terminal = {"completed", "failed", "killed", "stopped"}
    return sum(s in terminal for s in self.terminal.get(task_id, [])) > after

  def helper_requests(self):
    return [r for r in self.fake.requests if r["kind"] == "helper"]

  def dump(self):
    if not VERBOSE:
      return
    t0 = min([e["t"] for e in self.events] + [r["t"] for r in self.fake.requests])
    rows = [(e["t"], "EV ", {k: v for k, v in e.items() if k != "t"})
            for e in self.events if e.get("ev") != "stderr"]
    rows += [(r["t"], "REQ", {k: v for k, v in r.items() if k not in ("t", "transcript")})
             for r in self.fake.requests]
    for t, tag, row in sorted(rows, key=lambda x: x[0]):
      print(f"{t - t0:7.2f} {tag} {json.dumps(row)[:600]}")


def _settle(seconds=3.0):
  return asyncio.sleep(seconds)


def _helper_after(probe, t):
  return [r for r in probe.helper_requests() if r["t"] > t]


# --------------------------------------------------------------------------
# Scenarios


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["TASK-FAST", "TASK-MCP"], ids=["bash", "mcp"])
async def test_hook_stop_ends_only_the_subagent_and_it_resumes(scrubbed_env, fake_api, task):
  async with Probe(fake_api, scrubbed_env, hook_stop=True) as probe:
    await probe.send(f"SPAWN {task}")
    await probe.wait_for(lambda: probe.task_ids, what="task_started")
    agent = probe.task_ids[0]
    await probe.wait_for(lambda: probe.ended(agent), what="hook-stopped task end")
    stop_t = next(e["t"] for e in probe.events if e["ev"] == "hook-stop-returned")
    await _settle()

    # Q2: what the host saw for the hook-stopped run.
    pre_resume = list(probe.events)
    assert probe.terminal[agent] == ["completed", "completed"]  # updated + notification
    note = next(e for e in pre_resume if e["ev"] == "task_notification")
    assert note["summary"] == "SUB-PRE-M1 calling the stop tool"  # last text before the tool
    # SubagentStop does NOT fire for a hook-stopped subagent.
    assert not [e for e in pre_resume if e["ev"] == "SubagentStop"]

    # Q1: no model request for the subagent after the hook stop.
    after_stop = _helper_after(probe, stop_t)
    assert after_stop == [], after_stop
    assert not any("SUB-AFTER-M1" in str(r["reply"]) for r in fake_api.requests)
    # ... and the main session still answers.
    results_before = probe.results
    await probe.send("PING")
    await probe.wait_for(lambda: probe.results > results_before, what="parent alive")

    # Q3: resume through the host's normal follow-up path.
    resume_from = time.monotonic()
    await probe.send(f"MESSAGE {agent} FOLLOWUP-1")
    await probe.wait_for(
      lambda: any("SUB-DONE-2" in str(r["reply"]) for r in probe.helper_requests()),
      timeout=60, what="resumed subagent finished")
    await probe.wait_for(lambda: probe.ended(agent, after=1), what="resumed task end")
    await _settle(1.5)
    probe.dump()

    resumed = _helper_after(probe, resume_from)
    assert resumed, "subagent was not resumed"
    first = resumed[0]["transcript"]
    flat = json.dumps(first)
    assert task in flat and STOP_MARKER in flat
    # Same prior transcript: [prompt, assistant(text + stop tool_use),
    # user(stop tool_result + the follow-up text)].
    assert [m["role"] for m in first] == ["user", "assistant", "user"]
    last_user = json.dumps(first[-1])
    assert "FOLLOWUP-1" in last_user and "hook stopped continuation" in last_user
    # The resumed run ends normally and fires SubagentStop with its own text.
    stops = [e for e in probe.events if e["ev"] == "SubagentStop"]
    assert [e["last"] for e in stops] == ["SUB-DONE-2"]
    # The stop tool's own result is in the resumed transcript.
    m1_results = [b for m in first if isinstance(m["content"], list)
                  for b in m["content"] if b.get("type") == "tool_result"
                  and "_m1_" in b.get("tool_use_id", "")]
    assert m1_results and STOP_MARKER in json.dumps(m1_results)
    print("\nRESUMED FIRST REQUEST TRANSCRIPT:\n" + json.dumps(first, indent=1)[:4000])
    print("\nSUBAGENT_STOP EVENTS:", [e for e in probe.events if e["ev"] == "SubagentStop"])
    print("TASK EVENTS:", [e for e in probe.events if e["ev"].startswith("task_")])
    print("AGENT TOOL RESULTS:", [e for e in probe.events if e["ev"] == "tool_result"
                                  and e.get("parent") is None])


@pytest.mark.asyncio
async def test_control_without_hook_stop_makes_the_next_request(scrubbed_env, fake_api):
  async with Probe(fake_api, scrubbed_env, hook_stop=False) as probe:
    await probe.send("SPAWN TASK-FAST")
    await probe.wait_for(lambda: probe.task_ids, what="task_started")
    agent = probe.task_ids[0]
    await probe.wait_for(lambda: probe.ended(agent), what="task end")
    await _settle(1.5)
    probe.dump()
    assert any("SUB-AFTER-M1" in str(r["reply"]) for r in probe.helper_requests())


@pytest.mark.asyncio
async def test_stop_task_comparison(scrubbed_env, fake_api):
  async with Probe(fake_api, scrubbed_env, hook_stop=False) as probe:
    stopped = asyncio.Event()

    async def on_pre(data):
      if data.get("agent_id") and "sleep" in json.dumps(data.get("tool_input")):
        stopped.set()

    probe.on_pre_tool = on_pre
    await probe.send("SPAWN TASK-SLOW")
    await probe.wait_for(lambda: probe.task_ids, what="task_started")
    agent = probe.task_ids[0]
    await asyncio.wait_for(stopped.wait(), 30)
    await asyncio.sleep(0.5)
    await probe.client.stop_task(agent)
    await probe.wait_for(lambda: probe.ended(agent), what="stopped task end")
    await _settle(1.5)
    resume_from = time.monotonic()
    await probe.send(f"MESSAGE {agent} FOLLOWUP-2")
    send_results = lambda: [e for e in probe.events if e["ev"] == "tool_result"
                            and e.get("parent") is None
                            and "sendmessage" in e.get("tool_use", "")]
    await probe.wait_for(send_results, timeout=30, what="SendMessage result")
    await _settle(4)
    probe.dump()
    resumed = _helper_after(probe, resume_from)
    assert resumed == []
    assert '\\"success\\":false' in send_results()[0]["content"]
    assert probe.terminal.get(agent)[0] == "killed"
    print("\nSTOP_TASK terminal statuses:", probe.terminal.get(agent))
    print("SendMessage result after stop_task:", send_results())
    print("helper requests after follow-up:", len(resumed))
    print("SUBAGENT_STOP EVENTS:", [e for e in probe.events if e["ev"] == "SubagentStop"])
    assert not any("SUB-AFTER-SLOW" in str(r["reply"]) for r in probe.helper_requests())
