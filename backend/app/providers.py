"""AI provider adapters.

Each provider knows how to:
  1. Build the CLI command for a chat message.
  2. Set up the subprocess environment (auth config, etc.).
  3. Parse a line of CLI stdout into an SSE event dict, or None to skip.

The chat module calls these to stay provider-agnostic.  Adding a new
provider means writing a new class here and registering it in PROVIDERS.
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _load_agent_settings(data_dir: str) -> dict:
  """Loads agent settings from /data/shared/agent-settings.json."""
  path = Path(data_dir) / "shared" / "agent-settings.json"
  if path.exists():
    try:
      return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
      pass
  return {}


def _skill_path() -> Path | None:
  """Returns the path to the agent skill file, or None if not found."""
  candidates = [
    Path("/app/skill/agent-skill.md"),
    Path(__file__).parent.parent.parent / "skill" / "agent-skill.md",
  ]
  return next((p for p in candidates if p.exists()), None)


@dataclass
class ProviderResult:
  """Everything the chat module needs to spawn a provider subprocess."""
  cmd: list[str]
  env: dict[str, str]


def _summarize_input(tool: str, inp: dict) -> str:
  """Returns a short human-readable summary of a tool's input."""
  if tool == "Bash":
    return inp.get("command", "")
  elif tool in ("Read", "Glob"):
    return inp.get("file_path", "") or inp.get("pattern", "")
  elif tool in ("Write", "Edit"):
    return inp.get("file_path", "")
  elif tool == "Grep":
    return inp.get("pattern", "")
  return str(inp)[:200] if inp else ""


class BaseProvider:
  """Interface that all providers implement."""

  # Display name shown in the setup wizard.
  name: str = ""
  # CLI command name (used to check if the CLI is installed).
  cli_cmd: str = ""
  # Subdirectory under /data/cli-auth/ where credentials are stored.
  auth_dir: str = ""

  def check_auth(self, data_dir: str) -> str | None:
    """Returns an error message if not authenticated, None if ok."""
    return None

  def build(
    self,
    user_message: str,
    session_id: str | None,
    base_env: dict[str, str],
    data_dir: str,
  ) -> ProviderResult:
    """Returns the command and env for the subprocess."""
    raise NotImplementedError

  def parse_line(self, line: str) -> Optional[dict]:
    """Parses one stdout line into an SSE event dict, or None."""
    raise NotImplementedError


class ClaudeProvider(BaseProvider):
  """Claude Code CLI (claude -p --output-format stream-json)."""

  name = "Claude Code"
  cli_cmd = "claude"
  auth_dir = "claude"

  def check_auth(self, data_dir):
    creds = Path(data_dir) / "cli-auth" / "claude" / ".credentials.json"
    if not creds.exists():
      return (
        "Not signed in. Open Settings and connect "
        "under AI provider."
      )
    return None

  def build(self, user_message, session_id, base_env, data_dir, chat_id=None):
    cmd = [
      "claude",
      "-p",
      "--output-format", "stream-json",
      "--verbose",
      # DO NOT REMOVE --include-partial-messages. Without it the CLI
      # closes stdout without emitting a result event — the stream
      # ends silently and no assistant response appears. stream-json
      # is used (instead of plain text) because it gives us structured
      # events: tool_start/tool_end for collapsible tool blocks,
      # session_id for multi-turn resume, and cost/usage in the result
      # event. Side effect: intermediate assistant events arrive with
      # incomplete tool input (e.g. empty questions array for
      # AskUserQuestion). Those are filtered in _parse_tool_event.
      "--include-partial-messages",
      "--dangerously-skip-permissions",
    ]
    if session_id:
      cmd += ["--resume", session_id]
    else:
      skill = _skill_path()
      if skill:
        cmd += ["--system-prompt-file", str(skill)]

    # The agent uses agent-browser (installed in the image) via Bash for
    # screenshots and interactive testing — no MCP browser tools needed.

    # Load user-configurable settings (model, effort).
    agent_settings = _load_agent_settings(data_dir)
    if agent_settings.get("model"):
      cmd += ["--model", agent_settings["model"]]
    if agent_settings.get("effort"):
      cmd += ["--effort", agent_settings["effort"]]

    # Message is a positional argument — always last.  The "--" terminates
    # option parsing so the agent doesn't confuse it with a flag value.
    cmd += ["--", user_message]

    env = dict(base_env)
    creds = Path(data_dir) / "cli-auth" / "claude" / ".credentials.json"
    if creds.exists():
      env["CLAUDE_CONFIG_DIR"] = str(creds.parent)

    # Per-chat agent-browser session.  Every agent-browser invocation
    # in this subprocess picks up AGENT_BROWSER_SESSION via env, so
    # each chat gets its own isolated Chrome instance and they don't
    # fight over the "default" session when building in parallel.
    # The session is torn down by chat.py in the finally block.
    if chat_id:
      env["AGENT_BROWSER_SESSION"] = f"chat-{chat_id}"

    return ProviderResult(cmd=cmd, env=env)

  def _parse_stream_event(self, event: dict):
    """Handles stream_event — text deltas and tool block starts."""
    inner = event.get("event", {})
    inner_type = inner.get("type")
    if inner_type == "content_block_delta":
      delta = inner.get("delta", {})
      if delta.get("type") == "text_delta" and delta.get("text"):
        return {"type": "text", "content": delta["text"]}
    # Emit tool_start as soon as the content block begins streaming,
    # not from the assistant event.  This handles max_tokens truncation
    # where the assistant event is never sent.
    elif inner_type == "content_block_start":
      block = inner.get("content_block", {})
      if block.get("type") == "tool_use":
        name = block.get("name", "")
        if name == "AskUserQuestion":
          return None
        return {
          "type": "tool_start",
          "tool": name,
          "input": "",
        }
    return None

  def _parse_tool_event(self, event: dict):
    """Handles assistant events — backfills tool input summaries."""
    # Tool starts are emitted from content_block_start (earlier, handles
    # max_tokens truncation).  The assistant event arrives later with the
    # full input, so we emit tool_input events to backfill the summaries.
    results = []
    for block in event.get("message", {}).get("content", []):
      if block.get("type") == "tool_use":
        name = block.get("name", "")
        inp = block.get("input", {})
        if name == "AskUserQuestion":
          # --include-partial-messages causes the CLI to emit
          # intermediate assistant events as tool input is assembled.
          # The first partial has empty/incomplete input (questions: []
          # or missing question text).  Skip those to avoid rendering
          # an empty QuestionCard before the real one arrives.
          questions = inp.get("questions", [])
          if not questions or not all(q.get("question") for q in questions):
            continue
          results.append({
            "type": "question",
            "questions": questions,
          })
          continue
        summary = _summarize_input(name, inp)
        if summary:
          results.append({
            "type": "tool_input",
            "tool": name,
            "input": summary,
          })
    return results if results else None

  def _parse_user_event(self, event: dict):
    """Handles user events — tool results and tool_end markers."""
    # Tool results come as user messages.  The shape varies:
    # sometimes a top-level tool_use_result dict, sometimes
    # content blocks inside message.content.
    results = []
    output = ""

    result_data = event.get("tool_use_result")
    if isinstance(result_data, dict):
      stdout = result_data.get("stdout", "")
      stderr = result_data.get("stderr", "")
      output = (stdout + ("\n" + stderr if stderr else "")).strip()
    elif isinstance(result_data, str):
      output = result_data.strip()
    else:
      # Try content blocks.
      for block in event.get("message", {}).get("content", []):
        if (isinstance(block, dict)
            and block.get("type") == "tool_result"):
          content = block.get("content", "")
          if isinstance(content, str):
            output = content.strip()

    if output:
      results.append({"type": "tool_output", "content": output})
    results.append({"type": "tool_end"})
    return results

  def _parse_result_event(self, event: dict):
    """Handles result events — final cost info or error."""
    if event.get("is_error"):
      msg = event.get("result", "Unknown error.")
      # Surface a friendly message for auth failures so the user
      # knows where to fix it instead of seeing a raw CLI error.
      lower = msg.lower() if isinstance(msg, str) else ""
      if any(k in lower for k in ("auth", "login", "credential",
                                   "not logged", "sign in")):
        msg += (
          "\n\nOpen Settings and reconnect under AI provider."
        )
      return {"type": "error", "message": msg}
    return {
      "type": "done",
      "cost_usd": event.get("total_cost_usd", 0),
    }

  def parse_line(self, line: str) -> list[dict]:
    """Parse one line of Claude CLI JSON output into agent events.

    The Claude CLI emits several event shapes on stdout:
      - {"type": "stream_event", ...} → text tokens during streaming
      - {"type": "assistant", ...} → tool_use blocks (end of turn)
      - {"type": "user", ...} → tool results
      - {"type": "result", ...} → session ID and final cost/usage

    Returns a list of normalized dicts, a single dict, or None.
    """
    try:
      event = json.loads(line)
    except json.JSONDecodeError:
      return None

    event_type = event.get("type")

    if event_type == "system":
      if event.get("subtype") == "init" and event.get("session_id"):
        return {"type": "session_init", "session_id": event["session_id"]}
      return None

    if event_type == "stream_event":
      return self._parse_stream_event(event)
    elif event_type == "assistant":
      return self._parse_tool_event(event)
    elif event_type == "user":
      return self._parse_user_event(event)
    elif event_type == "result":
      return self._parse_result_event(event)

    return None


class CodexProvider(BaseProvider):
  """OpenAI Codex CLI (codex exec --json)."""

  name = "Codex"
  cli_cmd = "codex"
  auth_dir = "codex"

  def check_auth(self, data_dir):
    creds = Path(data_dir) / "cli-auth" / "codex" / "auth.json"
    if not creds.exists():
      return (
        "Not signed in to Codex. Open Settings and connect "
        "under AI provider."
      )
    return None

  def build(self, user_message, session_id, base_env, data_dir, chat_id=None):
    agent_settings = _load_agent_settings(data_dir)
    model = agent_settings.get("codex_model")

    # Use the app-server runner. `codex exec --json` only emits one
    # final agent_message event (no per-token deltas), so it can't
    # produce the typewriter effect. The app-server JSON-RPC protocol
    # emits `item/agentMessage/delta` notifications for streaming.
    # The runner script handles the protocol handshake and translates
    # notifications into clean Möbius event lines (session_init, text,
    # tool_*, done) — so parse_line just JSON-decodes and returns.
    #
    # Prompt + base-instructions are passed via files (not argv) so
    # large prompts (experience block injection, ~20KB) don't risk
    # hitting argv limits on any OS.
    scripts_dir = Path(__file__).parent.parent / "scripts"
    runner = scripts_dir / "codex_appserver_runner.py"

    # Sanitize chat_id for filesystem use. The route layer accepts
    # chat ids over the wire and they end up in path components — a
    # malicious id like "../../" or one with NUL could escape the
    # data dir. Keep only alphanumerics, dash, underscore (matches
    # the format we generate; longer/legitimate ids unaffected).
    import re
    import uuid
    safe_chat_id = re.sub(r"[^A-Za-z0-9_-]", "_", chat_id or "default")
    chat_dir = Path(data_dir) / "chats" / safe_chat_id
    chat_dir.mkdir(parents=True, exist_ok=True)

    # Per-run UUID-suffixed prompt file. Same-chat continuations
    # (queued-turn drain) can launch multiple runs in quick succession;
    # a shared `codex-prompt.txt` would race. The runner unlinks both
    # the prompt and the per-run instructions file immediately after
    # reading them (see codex_appserver_runner.py) so they don't
    # accumulate on disk or retain transcript content outside the DB.
    run_id = uuid.uuid4().hex[:12]
    prompt_file = chat_dir / f"codex-prompt-{run_id}.txt"
    prompt_file.write_text(user_message, encoding="utf-8")

    cmd = [
      "python3", str(runner),
      "--prompt", str(prompt_file),
      "--cwd", data_dir,
    ]
    if session_id:
      cmd += ["--session-id", session_id]
    if model:
      cmd += ["--model", model]

    env = dict(base_env)

    # Point Codex to credential directory (device-auth tokens).
    codex_home = str(Path(data_dir) / "cli-auth" / "codex")
    env["CODEX_HOME"] = codex_home

    # System prompt on first message: write the skill to a per-run
    # file (same race rationale as the prompt) and pass it as
    # --base-instructions so codex uses it for the thread.
    if not session_id:
      skill = _skill_path()
      if skill:
        instructions_file = chat_dir / f"codex-instructions-{run_id}.txt"
        try:
          instructions_file.write_text(
            skill.read_text(encoding="utf-8"), encoding="utf-8",
          )
          cmd += ["--base-instructions", str(instructions_file)]
        except OSError:
          pass

    return ProviderResult(cmd=cmd, env=env)

  def _extract_command(self, raw_cmd: str) -> str:
    """Extracts the user command from Codex's bash -lc wrapper."""
    prefix = "/bin/bash -lc '"
    if raw_cmd.startswith(prefix) and raw_cmd.endswith("'"):
      return raw_cmd[len(prefix):-1]
    return raw_cmd

  def parse_line(self, line: str) -> list[dict] | dict | None:
    try:
      event = json.loads(line)
    except json.JSONDecodeError:
      return None

    # The runner script (scripts/codex_appserver_runner.py) translates
    # codex app-server JSON-RPC into clean Möbius events: session_init,
    # text, tool_start, tool_input, tool_output, tool_end, done, error.
    # When we see a top-level Möbius-shaped event, return it directly —
    # this is the fast path that handles everything from the runner.
    direct_type = event.get("type")
    if direct_type in (
      "session_init", "text", "tool_start", "tool_input",
      "tool_output", "tool_end", "done", "error",
    ):
      return event

    # Legacy fallback below: if for any reason raw `codex exec --json`
    # output (or raw JSON-RPC notifications) leaks through to us, the
    # branches below still translate them. Keeps the provider working
    # even if the runner script regresses or is bypassed.
    method = event.get("method")
    if method:
      etype = method.replace("/", ".")
      event = {**(event.get("params") or {}), "type": etype}
    else:
      etype = direct_type

    if etype in ("thread.started", "thread/started"):
      # Exec mode uses thread_id; app-server uses thread.id.
      tid = event.get("thread_id")
      if not tid:
        thread = event.get("thread") or {}
        tid = thread.get("id")
      if tid:
        return {"type": "session_init", "session_id": tid}
      return None

    # App-server streaming delta — the headline feature.
    if etype == "item.agentMessage.delta":
      delta = event.get("delta", "")
      if delta:
        return {"type": "text", "content": delta}
      return None

    if etype == "turn.started":
      return None

    if etype == "item.started":
      item = event.get("item", {})
      itype = item.get("type")
      if itype == "command_execution":
        return {
          "type": "tool_start",
          "tool": "Bash",
          "input": self._extract_command(item.get("command", "")),
        }
      if itype == "file_change":
        changes = item.get("changes", [])
        path = changes[0].get("path", "") if changes else ""
        return {"type": "tool_start", "tool": "Edit", "input": path}
      if itype == "mcp_tool_call":
        server = item.get("server", "")
        tool = item.get("tool", "")
        return {
          "type": "tool_start",
          "tool": f"{server}:{tool}" if server else tool,
          "input": "",
        }
      if itype == "web_search":
        return {
          "type": "tool_start",
          "tool": "WebSearch",
          "input": item.get("query", ""),
        }
      return None

    if etype == "item.completed":
      item = event.get("item", {})
      itype = item.get("type")
      # In app-server, deltas already streamed all the text — the
      # completion event's text is redundant, ignore it. In exec mode,
      # this is the ONLY source of agent text.
      if itype == "agent_message":  # exec mode
        text = item.get("text", "")
        if text:
          return {"type": "text", "content": text}
        return None
      if itype == "agentMessage":  # app-server: deltas already sent
        return None
      # App-server uses userMessage for the echo of the user's input —
      # we don't display that (chat.py already saved it).
      if itype == "userMessage":
        return None
      if itype == "command_execution":
        output = item.get("aggregated_output", "").strip()
        results = []
        if output:
          results.append({"type": "tool_output", "content": output})
        results.append({"type": "tool_end"})
        return results
      if itype == "file_change":
        # Upstream FileUpdateChange struct is {path, kind} — there is
        # no diff field. Render the per-change list as the tool output
        # so the user sees what was added/updated/deleted.
        changes = item.get("changes", [])
        lines = [
          f"{c.get('kind', '?')} {c.get('path', '')}".strip()
          for c in changes
        ]
        summary = "\n".join(l for l in lines if l)
        results = []
        if summary:
          results.append({"type": "tool_output", "content": summary})
        results.append({"type": "tool_end"})
        return results
      if itype == "web_search":
        # The actual query is only available at item.completed; the
        # item.started event has query="". Backfill via tool_input
        # (events.py applies it to the most recent input-less tool)
        # so the user sees what was searched.
        query = item.get("query", "")
        if not query:
          # Codex sometimes lists multiple queries under action.queries.
          action = item.get("action") or {}
          queries = action.get("queries") or []
          if queries:
            query = "\n".join(str(q) for q in queries)
        results = []
        if query:
          results.append({"type": "tool_input", "input": query})
        results.append({"type": "tool_end"})
        return results
      if itype == "mcp_tool_call":
        # MCP tool args / result may be carried in the completion item.
        # Best-effort surfacing — show args as input, result as output.
        args = item.get("arguments") or item.get("input") or ""
        if isinstance(args, (dict, list)):
          args = json.dumps(args, indent=2)
        result = item.get("result") or item.get("output") or ""
        if isinstance(result, (dict, list)):
          result = json.dumps(result, indent=2)
        results = []
        if args:
          results.append({"type": "tool_input", "input": str(args)})
        if result:
          results.append({"type": "tool_output", "content": str(result)})
        results.append({"type": "tool_end"})
        return results
      return None

    if etype == "turn.completed":
      return {"type": "done", "cost_usd": 0}

    if etype == "error":
      return {
        "type": "error",
        "message": event.get("message", "Codex error"),
      }

    return None


# Registry of available providers, keyed by ID.
PROVIDERS: dict[str, BaseProvider] = {
  "claude": ClaudeProvider(),
  "codex": CodexProvider(),
}

# The default provider when none is configured.
DEFAULT_PROVIDER = "claude"


def get_provider(provider_id: str | None = None) -> BaseProvider:
  """Returns a provider by ID, falling back to the default."""
  return PROVIDERS.get(provider_id or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])


def detect_available() -> list[str]:
  """Returns IDs of providers whose CLI tool is installed."""
  return [pid for pid, p in PROVIDERS.items() if shutil.which(p.cli_cmd)]
