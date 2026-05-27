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
from typing import TYPE_CHECKING, Any, Literal

from app.runtime_types import ChatEvent
from app.tool_summaries import summarize_tool_input

if TYPE_CHECKING:
  from app.schemas import AgentSettingsOverride


# Known models per provider, with the top entry treated as the
# default. Mirrors the order in the frontend's CLAUDE_MODELS /
# CODEX_MODELS lists — keep in sync when a model lands at the top
# of either list. Listing all known values lets the snapshot logic
# detect cross-provider model mismatches (e.g. the global file
# remembers a Codex model but a new chat starts on Claude) and
# fall back cleanly to the provider's own top entry.
KNOWN_MODELS = {
  "claude": [
    "claude-sonnet-4-5-20251001",
    "claude-sonnet-4-7-20251215",
    "claude-opus-4-5-20251001",
    "claude-opus-4-6-20251015",
    "claude-opus-4-7-20251215",
    "claude-haiku-4-5-20251001",
  ],
  "codex": [
    "gpt-5.4",
  ],
}

DEFAULT_MODELS = {
  provider: models[0] for provider, models in KNOWN_MODELS.items()
}

# Initial effort when no global default exists. Aligns with the
# picker's middle option so new chats always render the picker with
# something selected — no error handling needed for "user sent without
# picking anything".
DEFAULT_EFFORT = "medium"


def _model_belongs_to_other_provider(model: str, provider: str) -> bool:
  """True when `model` is a KNOWN model for some OTHER provider.
  Use this to reject cross-provider mismatches without blocking
  unknown / future model names — the SDK is the authority on what
  it accepts; we only intercept the specific failure mode of
  sending a Codex model to Claude or vice versa."""
  for p, models in KNOWN_MODELS.items():
    if p != provider and model in models:
      return True
  return False


def _load_agent_settings(data_dir: str) -> dict:
  """Loads agent settings from /data/shared/agent-settings.json."""
  path = Path(data_dir) / "shared" / "agent-settings.json"
  if path.exists():
    try:
      return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
      pass
  return {}


def write_agent_settings(data_dir: str, settings: dict) -> bool:
  """Persists `settings` to /data/shared/agent-settings.json.

  Returns True on success, False on disk/permission failure. The
  caller is responsible for retry / re-marking the source as dirty
  so the mirror isn't silently lost.
  """
  path = Path(data_dir) / "shared" / "agent-settings.json"
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2))
    return True
  except OSError:
    return False


def initial_chat_defaults(data_dir: str, provider: str) -> dict:
  """Returns the {model, effort} snapshot a brand-new chat should
  start with — current global defaults merged with hard-coded
  per-provider fallbacks so the picker always renders something
  selected AND the model actually belongs to the chat's provider.

  The global file holds ONE model (the user's last pick across
  providers); when a new chat starts on a DIFFERENT provider, that
  remembered model is the wrong one to seed — e.g. file has
  `gpt-5.4` but owner.provider is still `claude`. In that case
  ignore the file's model and use the provider's own top model.
  Effort is provider-agnostic so the file's value carries cleanly.

  This snapshot is written into chat.agent_settings_json so each
  chat is fully self-contained: subsequent global-default changes
  don't bleed into existing chats.
  """
  defaults = _load_agent_settings(data_dir)
  file_model = defaults.get("model")
  if file_model and not _model_belongs_to_other_provider(file_model, provider):
    model = file_model
  else:
    model = DEFAULT_MODELS.get(provider, DEFAULT_MODELS["claude"])
  return {
    "model": model,
    "effort": defaults.get("effort") or DEFAULT_EFFORT,
  }


def effective_agent_settings(
  data_dir: str,
  chat_overrides: "AgentSettingsOverride | dict[str, Any] | None" = None,
  provider: str | None = None,
) -> dict:
  """Merges per-chat overrides on top of the global defaults, with
  provider-aware fallback so model+effort are ALWAYS populated.

  Layer order (later wins per key):
    1. Hard-coded provider defaults (top model + medium effort).
    2. Global file at /data/shared/agent-settings.json.
    3. Per-chat overrides from Chat.agent_settings_json.

  Provider-aware fallback fires when neither the file nor the
  override supplies a key — that guarantees the picker always shows
  a real selection and the runner always uses a real model. Existing
  chats created before the snapshot-on-create change have no
  override; the fallback bridges them without a migration.

  Known keys today: `model`, `effort`, `codex_model`. Future picker
  fields (thinking budget, sandbox mode) follow the same path — add
  the key here without a migration.
  """
  prov = provider or "claude"
  if chat_overrides is None:
    overrides = None
  elif hasattr(chat_overrides, "model_dump"):
    overrides = chat_overrides.model_dump()
  else:
    overrides = dict(chat_overrides)
  merged = {
    "model": DEFAULT_MODELS.get(prov, DEFAULT_MODELS["claude"]),
    "effort": DEFAULT_EFFORT,
  }
  # File layer: only carry the model if it belongs to this provider.
  # Effort is provider-agnostic so it always carries. Also carry the
  # per-provider effort memory so the picker can restore it on
  # provider switch — without this, a brand-new empty chat would lose
  # the user's previously-picked Claude effort the moment they
  # switched to Codex in the panel (and vice versa).
  file_layer = _load_agent_settings(data_dir)
  if file_layer.get("effort") is not None:
    merged["effort"] = file_layer["effort"]
  if file_layer.get("effort_by_provider") is not None:
    merged["effort_by_provider"] = file_layer["effort_by_provider"]
  fm = file_layer.get("model")
  if fm and not _model_belongs_to_other_provider(fm, prov):
    merged["model"] = fm
  # Per-chat overrides are authoritative — the user explicitly
  # picked them for THIS chat, so they trump the cross-provider
  # check.
  if overrides:
    for k, v in overrides.items():
      if v is None:
        continue
      merged[k] = v
  return merged


def get_skill_path() -> Path | None:
  """Resolves the agent skill file location. Single source of truth.

  Both the subprocess fallback path in this module and the Codex SDK
  runner (`codex_sdk_runner.py`) call this. The path is independent
  of `data_dir` — the skill is part of the deployment, not per-instance
  state, so resolution checks the baked container path first and falls
  back to the in-repo path for local development. Returns None if
  neither exists (callers handle skill-less startup gracefully).
  """
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

  def build_env(
    self,
    base_env: dict[str, str],
    data_dir: str,
    chat_id: str | None = None,
  ) -> dict[str, str]:
    """Returns just the env dict that build() would produce.

    The SDK path uses only the env (credentials path, per-chat
    agent-browser session) and does not need the cmd list. Splitting
    the env construction here keeps the SDK path from building and
    discarding a full CLI argv.
    """
    raise NotImplementedError

  def parse_line(self, line: str) -> list[ChatEvent]:
    """Parses one stdout line into zero or more SSE events."""
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

  def build(
    self, user_message, session_id, base_env, data_dir,
    chat_id=None, agent_settings=None,
  ):
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
      skill = get_skill_path()
      if skill:
        cmd += ["--system-prompt-file", str(skill)]

    # The agent uses agent-browser (installed in the image) via Bash for
    # screenshots and interactive testing — no MCP browser tools needed.

    # Load user-configurable settings (model, effort). When the caller
    # passes pre-merged effective settings (chat.py merges per-chat
    # overrides on top of the file defaults), prefer those — keeps
    # the merge logic in a single place.
    merged = (
      dict(agent_settings)
      if agent_settings is not None
      else _load_agent_settings(data_dir)
    )
    if merged.get("model"):
      cmd += ["--model", merged["model"]]
    if merged.get("effort"):
      cmd += ["--effort", merged["effort"]]

    # Message is a positional argument — always last.  The "--" terminates
    # option parsing so the agent doesn't confuse it with a flag value.
    cmd += ["--", user_message]

    env = self.build_env(base_env, data_dir, chat_id=chat_id)
    return ProviderResult(cmd=cmd, env=env)

  def build_env(
    self,
    base_env: dict[str, str],
    data_dir: str,
    chat_id: str | None = None,
  ) -> dict[str, str]:
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
    return env

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
        summary = summarize_tool_input(name, inp)
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

  def parse_line(self, line: str) -> list[ChatEvent]:
    """Parse one line of Claude CLI JSON output into agent events.

    The Claude CLI emits several event shapes on stdout:
      - {"type": "stream_event", ...} → text tokens during streaming
      - {"type": "assistant", ...} → tool_use blocks (end of turn)
      - {"type": "user", ...} → tool results
      - {"type": "result", ...} → session ID and final cost/usage

    Returns a list of normalized events. Unknown lines return `[]`.
    """
    try:
      event = json.loads(line)
    except json.JSONDecodeError:
      return []

    event_type = event.get("type")

    if event_type == "system":
      if event.get("subtype") == "init" and event.get("session_id"):
        return [{
          "type": "session_init",
          "session_id": event["session_id"],
        }]
      return []

    if event_type == "stream_event":
      parsed = self._parse_stream_event(event)
    elif event_type == "assistant":
      parsed = self._parse_tool_event(event)
    elif event_type == "user":
      parsed = self._parse_user_event(event)
    elif event_type == "result":
      parsed = self._parse_result_event(event)
    else:
      parsed = None

    if parsed is None:
      return []
    if isinstance(parsed, list):
      return parsed
    return [parsed]


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

  def build(
    self, user_message, session_id, base_env, data_dir,
    chat_id=None, agent_settings=None,
  ):
    merged = (
      dict(agent_settings)
      if agent_settings is not None
      else _load_agent_settings(data_dir)
    )
    # Codex accepts the picker's `model` key OR a Codex-specific
    # `codex_model` for backwards compatibility. The per-chat picker
    # writes `model`; the legacy file uses `codex_model`.
    model = merged.get("model") or merged.get("codex_model")

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

    env = self.build_env(base_env, data_dir, chat_id=chat_id)

    # System prompt on first message: write the skill to a per-run
    # file (same race rationale as the prompt) and pass it as
    # --base-instructions so codex uses it for the thread.
    if not session_id:
      skill = get_skill_path()
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

  def build_env(
    self,
    base_env: dict[str, str],
    data_dir: str,
    chat_id: str | None = None,
  ) -> dict[str, str]:
    del chat_id  # codex doesn't use AGENT_BROWSER_SESSION
    env = dict(base_env)
    env["CODEX_HOME"] = str(Path(data_dir) / "cli-auth" / "codex")
    return env

  def parse_line(self, line: str) -> list[ChatEvent]:
    """Returns the runner-shaped event when present, else `[]`.

    `scripts/codex_appserver_runner.py` already translates app-server
    JSON-RPC notifications into Möbius event dicts (session_init / text /
    tool_* / done / error). Lines this runner doesn't recognize are
    dropped at the runner; parse_line just decodes the JSON envelope.
    The translator in `app.codex_appserver` is the source of truth for
    notification shapes — exercised directly by the runner and by tests.
    """
    try:
      event = json.loads(line)
    except json.JSONDecodeError:
      return []
    if event.get("type") in (
      "session_init", "text", "tool_start", "tool_input",
      "tool_output", "tool_end", "done", "error",
    ):
      return [event]
    return []


# Registry of available providers, keyed by ID.
PROVIDERS: dict[str, BaseProvider] = {
  "claude": ClaudeProvider(),
  "codex": CodexProvider(),
}

ProviderName = Literal["claude", "codex"]
PROVIDER_NAMES: frozenset[str] = frozenset(PROVIDERS)

# The default provider when none is configured.
DEFAULT_PROVIDER = "claude"


def get_provider(provider_id: str | None = None) -> BaseProvider:
  """Returns a provider by ID, falling back to the default."""
  return PROVIDERS.get(provider_id or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])


def detect_available() -> list[str]:
  """Returns IDs of providers whose CLI tool is installed."""
  return [pid for pid, p in PROVIDERS.items() if shutil.which(p.cli_cmd)]
