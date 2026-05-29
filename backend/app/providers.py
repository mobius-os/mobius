"""AI provider adapters.

Post-SDK-migration the two providers run on different paths and share
only the identity + auth surface, not a polymorphic command shape:

  * `ClaudeProvider` — env-shaper for the SDK path. Chat turns run
    through `app.claude_sdk_runner`, which calls `check_auth` and
    `build_env` (for `CLAUDE_CONFIG_DIR` + `AGENT_BROWSER_SESSION`)
    and then drives the Anthropic Agent SDK directly. There is no
    argv to build and no stdout to parse on this path.
  * `CodexProvider` — everything-shaper for the subprocess (app-server)
    path. Codex still spawns `codex_appserver_runner.py` for per-token
    streaming, so it owns `check_auth`, `build_env`, plus `build`
    (argv + env for the runner) and `parse_line` (decode the runner's
    JSON event lines).

`BaseProvider` carries only the common surface (`check_auth`,
`build_env`, and the `name`/`cli_cmd`/`auth_dir` identifiers).
`build`/`parse_line` live on `CodexProvider` because the SDK path
has no use for them — putting them on the base class would be
fictional polymorphism that `NotImplementedError`s on Claude.

Adding a new provider means writing a new class here and registering
it in PROVIDERS. SDK-backed providers implement just `check_auth` +
`build_env`; subprocess-backed providers add `build` + `parse_line`.
"""

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from app.runtime_types import ChatEvent

if TYPE_CHECKING:
  from app.schemas import AgentSettingsOverride


log = logging.getLogger(__name__)


# Known models per provider, with the top entry treated as the
# default. Mirrors the order in the frontend's CLAUDE_MODELS /
# CODEX_MODELS lists — keep in sync when a model lands at the top
# of either list. Listing all known values lets the snapshot logic
# detect cross-provider model mismatches (e.g. the global file
# remembers a Codex model but a new chat starts on Claude) and
# fall back cleanly to the provider's own top entry.
KNOWN_MODELS = {
  "claude": [
    # Anthropic switched to dateless pinned IDs starting with 4.6;
    # the dated entries below stay listed because existing chats
    # persist them in agent_settings_json and the API still resolves
    # them as aliases.
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-sonnet-4-7-20251215",
    "claude-sonnet-4-5-20251001",
    "claude-haiku-4-5-20251001",
  ],
  "codex": [
    "gpt-5.5",
    "gpt-5.4",
  ],
}


# Human-readable label for each known model ID. Live registry calls
# return raw IDs without UI metadata (Anthropic's /v1/models returns
# only `id` + a generic `display_name`; Codex's models() returns
# slugs only), so the canonical label always comes from this map.
# Models the live API returns that are NOT in this map fall back to
# their raw ID as the label — the picker still renders, just with a
# less polished name. Add a row here when you add to KNOWN_MODELS.
MODEL_LABELS: dict[str, str] = {
  "claude-opus-4-8": "Opus 4.8",
  "claude-opus-4-7": "Opus 4.7",
  "claude-opus-4-6": "Opus 4.6",
  "claude-opus-4-5-20251001": "Opus 4.5",
  "claude-sonnet-4-6": "Sonnet 4.6",
  "claude-sonnet-4-7-20251215": "Sonnet 4.7",
  "claude-sonnet-4-5-20251001": "Sonnet 4.5",
  "claude-haiku-4-5-20251001": "Haiku 4.5",
  "gpt-5.5": "gpt-5.5",
  "gpt-5.4": "gpt-5.4",
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

  The Codex `build()` (which still spawns the app-server runner) and
  the SDK runners (`claude_sdk_runner.py`, `codex_sdk_runner.py`) all
  call this. The path is independent of `data_dir` — the skill is part
  of the deployment, not per-instance state, so resolution checks the
  baked container path first and falls back to the in-repo path for
  local development. Returns None if neither exists (callers handle
  skill-less startup gracefully).
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
  """Identity + auth surface shared by every provider.

  Both runtime paths (SDK and subprocess) need a display name, an auth
  preflight, and a base environment dict. They diverge after that:
  Codex builds argv and parses runner stdout; Claude hands `build_env`
  straight to the Agent SDK. Methods specific to the subprocess path
  (`build` / `parse_line`) live on `CodexProvider` rather than here so
  the interface reflects the real contract instead of an abstract one
  that would only ever raise `NotImplementedError` on the SDK side.
  """

  # Display name shown in the setup wizard.
  name: str = ""
  # CLI command name (used to check if the CLI is installed).
  cli_cmd: str = ""
  # Subdirectory under /data/cli-auth/ where credentials are stored.
  auth_dir: str = ""

  def check_auth(self, data_dir: str) -> str | None:
    """Returns an error message if not authenticated, None if ok."""
    return None

  def build_env(
    self,
    base_env: dict[str, str],
    data_dir: str,
    chat_id: str | None = None,
  ) -> dict[str, str]:
    """Returns the subprocess env (credentials path, per-chat
    agent-browser session) the runtime — SDK or subprocess — inherits.

    Each provider shapes a different set of variables (Claude needs
    `CLAUDE_CONFIG_DIR` + `AGENT_BROWSER_SESSION`; Codex needs
    `CODEX_HOME`), so subclasses always override. Raises on the base
    class to make a missing override loud instead of silently passing
    an unshaped env to the runtime.
    """
    raise NotImplementedError


class ClaudeProvider(BaseProvider):
  """Claude Code via the Anthropic Agent SDK.

  Chat turns run through `app.claude_sdk_runner` — there is no
  subprocess fallback. The CLI binary stays pinned in the Dockerfile
  because `routes/auth.py` extracts PKCE OAuth constants from it, but
  it is no longer spawned for chat traffic.
  """

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
    # spawned by the SDK runner picks up AGENT_BROWSER_SESSION via env,
    # so each chat gets its own isolated Chrome instance and they
    # don't fight over the "default" session when building in
    # parallel.  The session is torn down by chat.py in the finally
    # block.
    if chat_id:
      env["AGENT_BROWSER_SESSION"] = f"chat-{chat_id}"
    return env


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


# ─── Model registry (live fetch + per-provider fallback) ────────────
#
# The chat-settings picker (and the manage-models modal) queries
# `list_models()` to know which models exist for each provider. Two
# upstream sources back this:
#
#   - Anthropic /v1/models — REST API, called via httpx with the
#     OAuth access token from /data/cli-auth/claude/.credentials.json.
#     We don't import the `anthropic` SDK to keep dependency surface
#     flat; one httpx GET is simpler than a transitive dep pull.
#   - Codex `AsyncCodex.models()` — wraps the JSON-RPC `model/list`
#     call. Works under the same ChatGPT-account auth the rest of the
#     Codex bridge uses; no API key needed.
#
# Cache: 5 minutes per provider. The load-bearing scenario is "Claude
# just released a new model" — a 5-minute TTL means the user sees it
# within minutes of the upstream change, without hammering the API
# on every popover open. Per-provider cache so one provider's stale
# entry doesn't block the other's refresh.
#
# Fallback: if an upstream fetch raises (network, auth, rate limit),
# we return KNOWN_MODELS[provider] for THAT provider. The other
# provider's live data still flows. This is the "reversibility over
# prevention" axis applied to the picker — a transient outage
# shouldn't break the chat-creation surface.

_MODEL_CACHE_TTL_SECONDS = 5 * 60

# In-process cache. Single-process FastAPI means a dict is enough; if
# we ever scale out we'll need a shared cache, but that's not the
# constraint today.
_model_registry_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
# Per-provider lock so a concurrent caller waiting on a Claude refetch
# isn't blocked by an in-flight Codex refetch (and vice versa). The
# cache itself is read without a lock — Python dict reads are atomic
# under the GIL — and a stale-read race on the boundary just means
# both callers do a refetch and one overwrites the other's entry,
# which is the same result.
_model_registry_locks: dict[str, asyncio.Lock] = {
  pid: asyncio.Lock() for pid in PROVIDERS
}


def _label_for(model_id: str) -> str:
  """Returns the human-readable label for `model_id`, falling back
  to the raw ID when the model isn't in MODEL_LABELS."""
  return MODEL_LABELS.get(model_id, model_id)


def _fallback_models(provider_id: str) -> list[dict[str, Any]]:
  """Returns the KNOWN_MODELS list for `provider_id` as registry
  entries. Used when the upstream fetch fails AND as the seed for
  cross-referencing live IDs against the canonical order. `available`
  is set explicitly here so non-route callers (tests, internal
  helpers) get the same dict shape the route layer's Pydantic
  serialization would produce."""
  return [
    {
      "id": mid,
      "label": _label_for(mid),
      "provider": provider_id,
      "available": True,
    }
    for mid in KNOWN_MODELS.get(provider_id, [])
  ]


def _merge_live_with_known(
  provider_id: str, live_ids: list[str]
) -> list[dict[str, str]]:
  """Merges a live ID list with KNOWN_MODELS ordering + labels.

  Known IDs appear first in KNOWN_MODELS order (so the picker stays
  visually stable across an Anthropic-side reordering). Live-only
  IDs (released since the last KNOWN_MODELS bump) follow, in the
  order the upstream returned them. Stale-known IDs (in
  KNOWN_MODELS but not in live) are kept — they may still resolve
  as aliases server-side, and dropping them would silently break
  existing chats that persisted them.
  """
  known = KNOWN_MODELS.get(provider_id, [])
  live_set = set(live_ids)
  entries: list[dict[str, str]] = []
  for mid in known:
    entries.append({
      "id": mid,
      "label": _label_for(mid),
      "provider": provider_id,
      "available": mid in live_set,
    })
  for mid in live_ids:
    if mid in known:
      continue
    entries.append({
      "id": mid,
      "label": _label_for(mid),
      "provider": provider_id,
      "available": True,
    })
  return entries


async def _fetch_claude_models(data_dir: str) -> list[str]:
  """Calls Anthropic's /v1/models with the stored OAuth access token.

  Raises on any non-2xx or missing credentials so the caller can fall
  back to KNOWN_MODELS. The Claude Code OAuth flow grants the
  user:inference scope which the models endpoint accepts. We use
  httpx (already a requirement) instead of pulling the `anthropic`
  SDK to keep dependency surface flat.
  """
  import httpx  # local import — only the registry path needs it

  creds_path = Path(data_dir) / "cli-auth" / "claude" / ".credentials.json"
  if not creds_path.exists():
    raise RuntimeError("claude credentials missing")
  raw = json.loads(creds_path.read_text())
  oauth = raw.get("claudeAiOauth") or {}
  token = oauth.get("accessToken")
  if not token:
    raise RuntimeError("claude credentials malformed")
  headers = {
    "Authorization": f"Bearer {token}",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
  }
  async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.get(
      "https://api.anthropic.com/v1/models?limit=200",
      headers=headers,
    )
    resp.raise_for_status()
    payload = resp.json()
  ids: list[str] = []
  for entry in payload.get("data", []):
    mid = entry.get("id")
    if isinstance(mid, str):
      ids.append(mid)
  return ids


async def _fetch_codex_models(data_dir: str) -> list[str]:
  """Calls the Codex SDK's `AsyncCodex.models()`.

  Codex auth happens transparently inside the SDK — it reads the
  same `CODEX_HOME` directory the chat path uses, so once the user
  has connected Codex in Settings the call works.
  """
  from openai_codex import AsyncCodex
  from openai_codex.client import AppServerConfig

  codex_home = Path(data_dir) / "cli-auth" / "codex"
  if not (codex_home / "auth.json").exists():
    raise RuntimeError("codex credentials missing")
  # Match codex_sdk_runner.py's binary resolution: pass the resolved
  # path explicitly so AppServerConfig doesn't fall back to its own
  # discovery, which has diverged from PATH in past SDK versions.
  config = AppServerConfig(
    codex_home=str(codex_home),
    codex_bin=shutil.which("codex"),
  )
  ids: list[str] = []
  async with AsyncCodex(config=config) as codex:
    response = await codex.models()
  # The SDK returns a ModelListResponse with `.models` list. Each
  # entry exposes a `.slug` (the model ID). Defensive: tolerate
  # bare strings too in case the upstream shape drifts.
  raw_models = getattr(response, "models", None) or []
  for entry in raw_models:
    if isinstance(entry, str):
      ids.append(entry)
    else:
      slug = getattr(entry, "slug", None) or getattr(entry, "id", None)
      if isinstance(slug, str):
        ids.append(slug)
  return ids


async def _fetch_provider_models(
  provider_id: str, data_dir: str
) -> list[str]:
  """Dispatches to the right fetcher. Returns raw IDs; the caller
  wraps them with labels."""
  if provider_id == "claude":
    return await _fetch_claude_models(data_dir)
  if provider_id == "codex":
    return await _fetch_codex_models(data_dir)
  return []


async def list_models(
  data_dir: str,
  force_refresh: bool = False,
) -> dict[str, list[dict[str, str]]]:
  """Returns `{provider_id: [{id, label, provider, available}, ...]}`.

  Cache TTL is 5 minutes per provider. On upstream failure for a
  given provider we serve KNOWN_MODELS for THAT provider (the live
  data from the other provider still flows). `force_refresh=True`
  bypasses the cache — used by the manage-models modal's refresh
  button so the user can pull a just-released model on demand.

  Never raises — a failure on both providers still returns the full
  KNOWN_MODELS fallback for both.
  """

  def cache_fresh(provider_id: str) -> list[dict[str, str]] | None:
    """Returns cached entries if a non-forced read can use them."""
    if force_refresh:
      return None
    cached = _model_registry_cache.get(provider_id)
    if not cached:
      return None
    if time.monotonic() - cached[0] >= _MODEL_CACHE_TTL_SECONDS:
      return None
    return cached[1]

  async def fetch_one(provider_id: str) -> tuple[str, list[dict[str, str]]]:
    """Refetches under the provider's lock, with a double-checked
    cache read inside the lock so we don't redo a refetch another
    caller just completed for us."""
    async with _model_registry_locks[provider_id]:
      hit = cache_fresh(provider_id)
      if hit is not None:
        return provider_id, hit
      try:
        live_ids = await _fetch_provider_models(provider_id, data_dir)
        entries = _merge_live_with_known(provider_id, live_ids)
      except Exception as exc:  # noqa: BLE001 — fallback is the contract
        log.warning(
          "model registry fetch failed for %s: %s; using KNOWN_MODELS",
          provider_id, exc,
        )
        entries = _fallback_models(provider_id)
      _model_registry_cache[provider_id] = (time.monotonic(), entries)
      return provider_id, entries

  # Serve hot reads (cache hit + not forced) without ever taking a
  # lock — concurrent callers in the steady-state hit the cache
  # directly. Only cache misses go through fetch_one.
  result: dict[str, list[dict[str, str]]] = {}
  cold: list[str] = []
  for provider_id in PROVIDERS:
    hit = cache_fresh(provider_id)
    if hit is not None:
      result[provider_id] = hit
    else:
      cold.append(provider_id)

  if cold:
    # Refetch missing providers in parallel — Claude and Codex have
    # independent upstreams, so there's no reason to serialize them
    # under one lock when both are stale.
    fetched = await asyncio.gather(*(fetch_one(pid) for pid in cold))
    for pid, entries in fetched:
      result[pid] = entries

  return result


def invalidate_model_cache() -> None:
  """Clears the per-provider cache. Used by tests and by the
  manage-models modal's explicit refresh path."""
  _model_registry_cache.clear()
