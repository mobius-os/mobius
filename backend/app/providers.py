"""AI provider adapters.

Both providers run through the Agent SDK and share only the identity +
auth + env surface — there is no polymorphic command/parse shape:

  * `ClaudeProvider` — env-shaper for the SDK path. Chat turns run
    through `app.claude_sdk_runner`, which calls `check_auth` and
    `build_env` (for `CLAUDE_CONFIG_DIR` + `AGENT_BROWSER_SESSION`)
    and then drives the Anthropic Agent SDK directly.
  * `CodexProvider` — identity/auth/env shaper. Live Codex chat turns
    run through the Agent SDK: `chat.py` dispatches to
    `codex_sdk_runner.run_codex_sdk_turn`. The SDK runner reuses one
    helper from `codex_appserver.py` (`_extract_bash_command`); the
    provider itself shapes credentials + env.

`BaseProvider` carries the whole surface (`check_auth`, `build_env`,
and the `name`/`cli_cmd`/`auth_dir` identifiers); every provider
implements just `check_auth` + `build_env`.

Adding a new provider means writing a new class here and registering
it in PROVIDERS.
"""

import asyncio
import json
import logging
import os
import shutil
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
  from app.schemas import AgentSettingsOverride


log = logging.getLogger(__name__)


# Fallback models per provider, ordered the same way the pickers display
# them. Listing known values lets the snapshot logic detect cross-provider
# model mismatches (e.g. the global file remembers a Codex model but a new
# chat starts on Claude) while live registry fetches remain the broader
# source of truth for newly released IDs.
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
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
  ],
}


# Human-readable label for model IDs we know how to polish. Live
# registry calls return raw IDs without consistent UI metadata
# (Anthropic's /v1/models returns `id` + a generic `display_name`;
# Codex's models() returns slugs only), so labels come from this map
# when present and fall back to the raw ID for newly released models.
MODEL_LABELS: dict[str, str] = {
  "claude-opus-4-8": "Opus 4.8",
  "claude-opus-4-7": "Opus 4.7",
  "claude-opus-4-6": "Opus 4.6",
  "claude-opus-4-5-20251001": "Opus 4.5",
  "claude-sonnet-4-6": "Sonnet 4.6",
  "claude-sonnet-4-7-20251215": "Sonnet 4.7",
  "claude-sonnet-4-5-20251001": "Sonnet 4.5",
  "claude-haiku-4-5-20251001": "Haiku 4.5",
  "gpt-5.6-sol": "GPT-5.6 Sol",
  "gpt-5.6-terra": "GPT-5.6 Terra",
  "gpt-5.6-luna": "GPT-5.6 Luna",
  "gpt-5.5": "gpt-5.5",
  "gpt-5.4": "gpt-5.4",
  "gpt-5.4-mini": "gpt-5.4 mini",
  "gpt-5.3-codex-spark": "GPT-5.3 Codex Spark",
}

# Optional model-specific effort capability overrides. Provider defaults remain
# the fallback, so adding a new model normally needs no entry. Add a row only
# when a model supports a narrower, reordered, or extended effort scale; the
# registry carries it to every shell/app picker as data.
MODEL_EFFORT_LEVELS: dict[str, list[str]] = {}

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


def skills_enabled(data_dir: str) -> bool:
  """Whether SDK skills are offered to the Claude agent (default OFF).

  Reads the `skills_enabled` flag from /data/shared/agent-settings.json.
  This is the gate for the BEHAVIOR-SHIFTING half of skill
  observability: when off (the default), `claude_sdk_runner` keeps
  `setting_sources=None` and passes no `skills=`, so the Skill tool is
  never offered and nothing loads — deploying skill-observability
  changes nothing about the agent's behavior. When the owner opts in,
  the runner enables user+project setting sources and `skills="all"`,
  at which point the agent can load skills and the observability path
  (chip + activity log) starts seeing real loads.

  Absent / malformed flag reads as off — opt-in is explicit.
  """
  return bool(_load_agent_settings(data_dir).get("skills_enabled") is True)


def auto_resume_on_limit(data_dir: str) -> bool:
  """Whether to auto-resume a limit-parked turn at reset (default OFF).

  Reads the `auto_resume_on_limit` flag from /data/shared/agent-settings.json
  (design §2.4). Off by default: the reset sweep only push-notifies and leaves
  a one-tap Resume. When the owner opts in, the sweep additionally starts a
  serial "continue" turn once the limit resets, re-parking if it re-hits.
  Absent / malformed flag reads as off — opt-in is explicit, mirroring
  `skills_enabled`.
  """
  return bool(
    _load_agent_settings(data_dir).get("auto_resume_on_limit") is True
  )


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
  """Merges per-chat overrides on top of the global defaults.

  Layer order (later wins per key):
    1. Hard-coded effort default (medium). There is intentionally
       NO hard-coded model default: until the owner manually picks a
       model, the provider SDK runs with its own default.
    2. Global file at /data/shared/agent-settings.json.
    3. Per-chat overrides from Chat.agent_settings_json.

  Provider-aware filtering still rejects a saved model that belongs
  to the other provider, but it now resolves that case to ``None``
  rather than silently inventing the provider's top model. A model
  becomes the default only after a manual picker choice mirrors into
  /data/shared/agent-settings.json.

  Known keys today: `model`, `effort`. Future picker fields (thinking
  budget, sandbox mode) follow the same path — add the key here
  without a migration.
  """
  prov = provider or "claude"
  if chat_overrides is None:
    overrides = None
  elif hasattr(chat_overrides, "model_dump"):
    overrides = chat_overrides.model_dump()
  else:
    overrides = dict(chat_overrides)
  merged = {
    "model": None,
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
  if "model" in file_layer:
    fm = file_layer.get("model")
    merged["model"] = (
      fm
      if isinstance(fm, str) and fm and not _model_belongs_to_other_provider(fm, prov)
      else None
    )
  # Per-chat overrides are authoritative for the active chat, while
  # still applying the same cross-provider safety rail: a stale Codex
  # model on a Claude chat resolves to provider default rather than
  # sending the wrong ID into the SDK.
  if overrides:
    for k, v in overrides.items():
      if k == "model":
        model = v if isinstance(v, str) and v else None
        merged["model"] = (
          model
          if model and not _model_belongs_to_other_provider(model, prov)
          else None
        )
        continue
      if v is None:
        continue
      merged[k] = v
  return merged


def _background_default_choice(
  provider: str,
  *,
  enabled: bool = False,
  model: str | None = None,
) -> dict:
  return {
    "provider": provider,
    "model": model,
    "effort": DEFAULT_EFFORT,
    "enabled": enabled,
  }


def _clean_background_choice(
  raw: Any,
  fallback_provider: str | None = None,
  *,
  include_enabled: bool = False,
) -> dict | None:
  if not isinstance(raw, dict):
    return None
  provider = raw.get("provider")
  if provider not in PROVIDERS:
    provider = fallback_provider if fallback_provider in PROVIDERS else None
  if provider not in PROVIDERS:
    return None
  out: dict[str, Any] = {"provider": provider}
  raw_model = raw.get("model")
  if isinstance(raw_model, str) and raw_model.strip():
    model = raw_model.strip()
    if _model_belongs_to_other_provider(model, provider):
      model = DEFAULT_MODELS.get(provider)
  elif "model" in raw:
    # Explicit null/empty means "let this provider use its native default".
    model = None
  else:
    # Legacy provider-only choices predate nullable model defaults; keep them
    # concrete so background runners do not inherit the chat model by accident.
    model = DEFAULT_MODELS.get(provider)
  out["model"] = model
  effort = raw.get("effort")
  out["effort"] = effort.strip() if isinstance(effort, str) and effort.strip() else None
  if include_enabled:
    out["enabled"] = raw.get("enabled") is not False
  return out


def background_agent_settings(data_dir: str, default_provider: str | None = None) -> dict:
  """Return the system-level background provider choices.

  Stored in /data/shared/agent-settings.json under:
    {
      "background_agents": {
        "providers": [
          {"provider": "claude", "model": "...", "effort": "...", "enabled": true},
          {"provider": "codex", "model": "...", "effort": "...", "enabled": true}
        ],
        "primary": {"provider": "claude", "model": "...", "effort": "..."},
        "fallback": {"provider": "codex", "model": "...", "effort": "..."}
      }
    }

  `primary`/`fallback` are kept for older runners and app settings. The richer
  `providers` list is the owner-facing source of truth: one default per
  provider, plus enabled/order for quota fallback. Absence stays backwards-
  compatible: only the resolved provider is enabled until the owner opts
  additional providers in.
  """
  provider = default_provider if default_provider in PROVIDERS else DEFAULT_PROVIDER
  file_layer = _load_agent_settings(data_dir)
  raw = file_layer.get("background_agents")
  bg = raw if isinstance(raw, dict) else {}
  # When the owner has already picked a chat model, synthesize a concrete
  # provider-native background model rather than inheriting that chat default.
  # With no manual model choice at all, keep the background model nullable so
  # the provider SDK can use its own default until the owner saves a row.
  synthetic_default_model = (
    DEFAULT_MODELS.get(provider) if "model" in file_layer else None
  )

  rows: list[dict[str, Any]] = []
  seen: set[str] = set()

  def add_row(choice: dict | None, *, enabled_default: bool) -> None:
    if choice is None:
      return
    provider_id = choice["provider"]
    if provider_id in seen:
      return
    row = dict(choice)
    row["model"] = row.get("model")
    row["effort"] = row.get("effort") or DEFAULT_EFFORT
    row["enabled"] = (
      bool(row.get("enabled"))
      if "enabled" in row
      else enabled_default
    )
    rows.append(row)
    seen.add(provider_id)

  raw_rows = bg.get("providers")
  if isinstance(raw_rows, list):
    for raw_choice in raw_rows:
      add_row(
        _clean_background_choice(raw_choice, include_enabled=True),
        enabled_default=True,
      )
  else:
    primary = _clean_background_choice(bg.get("primary"), provider)
    if primary is None:
      primary = _background_default_choice(
        provider,
        enabled=True,
        model=synthetic_default_model,
      )
    add_row(primary, enabled_default=True)
    add_row(_clean_background_choice(bg.get("fallback")), enabled_default=True)

  if not rows:
    add_row(
      _background_default_choice(
        provider,
        enabled=True,
        model=synthetic_default_model,
      ),
      enabled_default=True,
    )

  for provider_id in PROVIDERS:
    if provider_id not in seen:
      rows.append(
        _background_default_choice(
          provider_id,
          enabled=False,
          model=DEFAULT_MODELS.get(provider_id),
        )
      )

  enabled_rows = [dict(row) for row in rows if row.get("enabled") is not False]
  if not enabled_rows:
    rows = [
      {**row, "enabled": row["provider"] == provider}
      for row in rows
    ]
    enabled_rows = [dict(row) for row in rows if row.get("enabled") is not False]

  primary = {k: v for k, v in enabled_rows[0].items() if k != "enabled"}
  fallback = (
    {k: v for k, v in enabled_rows[1].items() if k != "enabled"}
    if len(enabled_rows) > 1 else None
  )
  return {"providers": rows, "primary": primary, "fallback": fallback}


def get_skill_path() -> Path | None:
  """Resolves the agent skill file location. Single source of truth.

  `chat.py` and the SDK runners (`claude_sdk_runner.py`,
  `codex_sdk_runner.py`) all call this. The path is independent of
  `data_dir` — the skill is part
  of the deployment, not per-instance state, so resolution checks the
  baked container path first and falls back to the in-repo path for
  local development. Returns None if neither exists (callers handle
  skill-less startup gracefully).
  """
  # The CONSTITUTION (core.md) is the system prompt. The detailed how-to
  # skills it points to live agent-editable under /data/shared/skills/
  # (seeded by init_skills.py). The baked container path wins over the
  # in-repo path so a deploy always serves the deployed constitution.
  repo = Path(__file__).parent.parent.parent / "skill"
  candidates = [
    Path("/app/skill/core.md"),
    repo / "core.md",
  ]
  return next((p for p in candidates if p.exists()), None)


class BaseProvider:
  """Identity + auth surface shared by every provider.

  Every provider needs a display name, an auth preflight, and a base
  environment dict; both live providers (Claude and Codex) run through
  the Agent SDK and hand `build_env` straight to their SDK runner.
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

  async def ensure_auth(self, data_dir: str) -> None:
    """Pre-turn auth preparation hook. No-op by default.

    Providers whose runtime reads a credential file that can go stale
    mid-turn override this to refresh it before the turn starts. Best
    effort by contract — a failure here must never block the turn.
    """
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
    `CLAUDE_CONFIG_DIR`; Codex needs `CODEX_HOME`; both inherit the
    per-chat `AGENT_BROWSER_SESSION` when a chat id is available), so
    subclasses always override. Raises on the base
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

  async def ensure_auth(self, data_dir: str) -> None:
    """Refresh the Claude OAuth token before a chat turn, if expired.

    The intermittent "401 Invalid authentication credentials" on a fresh
    chat send had two compounding causes: (1) the chat preflight only
    checked the credentials file *exists*, never its `expiresAt`, so an
    expired token reached the CLI; (2) the CLI then refreshes mid-turn,
    and because Anthropic rotates the single-use refresh token, a
    concurrent model-registry refresh could consume the token out from
    under the CLI → 401. Refreshing here (sharing `_claude_refresh_lock`
    with the registry path) serializes the two in-process refreshers and
    removes the at-spawn-expired case by handing the CLI a fresh token. It
    does NOT cover a turn that outlives the token: the token is only
    refreshed within the 60s margin, so a multi-minute build can still cross
    an expiry mid-turn, and the CLI's own refresh then runs outside this
    lock by design (raise `_CLAUDE_TOKEN_REFRESH_MARGIN_MS` above the longest
    expected turn to close that remainder too).

    Best effort by the BaseProvider contract: if the refresh endpoint is
    briefly unreachable we log and proceed — the CLI can still attempt its
    own refresh, and a genuinely dead refresh token surfaces through the
    normal turn-error path (which prompts the user to reconnect) rather
    than blocking every turn here.
    """
    try:
      await _claude_access_token(data_dir)
    except Exception as exc:  # noqa: BLE001 - best-effort; never block a turn
      log.warning("claude pre-turn token refresh failed: %s", exc)

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
    # The in-product agent reaches Codex for ensemble / "use codex" work via
    # the Agent tool's `codex:codex-rescue` subagent — the codex plugin's
    # companion broker shells out to `codex exec`, and that codex process
    # inherits THIS environment. The codex CLI reads its credentials from
    # CODEX_HOME, which otherwise only CodexProvider.build_env sets; a Claude
    # turn left CODEX_HOME unset, so the spawned codex fell back to the empty
    # default config and died "401 Invalid authentication credentials" —
    # which is why "leverage codex subagents" failed in-product. Point it at
    # the shared codex auth dir (only when codex is actually connected) so
    # cross-provider codex calls authenticate.
    codex_auth = Path(data_dir) / "cli-auth" / "codex" / "auth.json"
    if codex_auth.exists():
      env["CODEX_HOME"] = str(codex_auth.parent)
    return env


class CodexProvider(BaseProvider):
  """OpenAI Codex provider.

  Live chat runs through the Codex Agent SDK (`codex_sdk_runner`); this
  class shapes identity, auth, and the subprocess env (`CODEX_HOME` plus
  the per-chat agent-browser session).
  """

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

  def build_env(
    self,
    base_env: dict[str, str],
    data_dir: str,
    chat_id: str | None = None,
  ) -> dict[str, str]:
    env = dict(base_env)
    env["CODEX_HOME"] = str(Path(data_dir) / "cli-auth" / "codex")
    # Match Claude's per-chat agent-browser isolation. Without this, Codex
    # turns that invoke `agent-browser` all attach to the CLI's global
    # "default" session; a browser launched by one Codex chat can then leak
    # viewport, tabs, and profile choice into another, and chat.py's
    # terminal `agent-browser --session chat-<id> close` misses the live
    # default session.
    if chat_id:
      env["AGENT_BROWSER_SESSION"] = f"chat-{chat_id}"
    return env


# Registry of available providers, keyed by ID.
PROVIDERS: dict[str, BaseProvider] = {
  "claude": ClaudeProvider(),
  "codex": CodexProvider(),
}

ProviderName = Literal["claude", "codex"]
PROVIDER_NAMES: frozenset[str] = frozenset(PROVIDERS)

# The default provider when none is configured.
DEFAULT_PROVIDER = "claude"

# When the stored provider is still the historical default but is not
# authenticated, prefer a connected provider over showing a dead default.
# Codex is first because the setup wizard leads with it for new installs.
CONNECTED_DEFAULT_ORDER = ("codex", "claude")


def authenticated_provider_ids(data_dir: str) -> list[str]:
  """Return provider ids whose credential preflight currently passes."""
  ids: list[str] = []
  for provider_id in CONNECTED_DEFAULT_ORDER:
    provider = PROVIDERS.get(provider_id)
    if provider and provider.check_auth(data_dir) is None:
      ids.append(provider_id)
  return ids


def resolve_default_provider(
  data_dir: str,
  configured_provider: str | None = None,
) -> str:
  """Resolve the provider new chats/settings should present.

  Existing installs have `Owner.provider="claude"` from the original DB
  default even when the user only connected Codex during setup. In that
  case, surfacing Claude as the default creates a dead-end picker. Treat the
  configured provider as authoritative when it is connected, but if it is the
  historical default and is not connected, fall forward to the first connected
  provider.
  """
  provider_id = (
    configured_provider if configured_provider in PROVIDERS else DEFAULT_PROVIDER
  )
  if (
    provider_id == DEFAULT_PROVIDER
    and PROVIDERS[provider_id].check_auth(data_dir) is not None
  ):
    connected = authenticated_provider_ids(data_dir)
    if connected:
      return connected[0]
  return provider_id


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
_model_registry_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
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
  entries. Used when the upstream fetch fails. `available` is set
  explicitly here so non-route callers (tests, internal helpers) get
  the same dict shape the route layer's Pydantic serialization would
  produce."""
  return [
    {
      "id": mid,
      "label": _label_for(mid),
      "provider": provider_id,
      "available": True,
      **({"effort_levels": MODEL_EFFORT_LEVELS[mid]}
         if mid in MODEL_EFFORT_LEVELS else {}),
    }
    for mid in KNOWN_MODELS.get(provider_id, [])
  ]


def _live_model_entries(
  provider_id: str, live_ids: list[str]
) -> list[dict[str, Any]]:
  """Wraps live SDK/CLI model IDs as registry entries.

  Static KNOWN_MODELS is only the failure fallback. When a live fetch
  succeeds, the provider SDK/CLI is the source of truth; labels are a
  cosmetic map with raw-ID fallback.
  """
  return [
    {
      "id": mid,
      "label": _label_for(mid),
      "provider": provider_id,
      "available": True,
      **({"effort_levels": MODEL_EFFORT_LEVELS[mid]}
         if mid in MODEL_EFFORT_LEVELS else {}),
    }
    for mid in live_ids
  ]


# Claude CLI OAuth constants — the registry path refreshes an expired
# access token itself rather than 401ing. Duplicated from routes/auth.py
# (and recover_oauth.py) on purpose, the same way recover_oauth duplicates
# them: providers.py is a low-level adapter module that must not import a
# route module, and these are public Anthropic values that change rarely.
# Keep in sync with routes/auth._CLAUDE_CLIENT_ID / _TOKEN_URL if Anthropic
# ever rotates them.
_CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Refresh when fewer than this many ms remain on the access token, so a
# token that expires mid-flight doesn't 401. 60s comfortably covers the
# 10s request timeout plus clock skew.
_CLAUDE_TOKEN_REFRESH_MARGIN_MS = 60_000

# Serializes Claude OAuth refreshes within this process. Anthropic rotates
# the single-use refresh token on every grant, so two concurrent refreshes
# of the same .credentials.json (the chat pre-turn refresh + the model-
# registry fetch) would consume each other's token and 401. A single
# uvicorn worker runs the app, so an in-process lock is the right scope; the
# chat CLI is kept off the refresh path entirely by ensure_auth handing it
# an already-fresh token before the turn.
_claude_refresh_lock = asyncio.Lock()


def _read_claude_oauth(data_dir: str) -> tuple[Path, dict]:
  """Returns (credentials path, claudeAiOauth dict). Raises when the file
  is missing or has no claudeAiOauth block."""
  creds_path = Path(data_dir) / "cli-auth" / "claude" / ".credentials.json"
  if not creds_path.exists():
    raise RuntimeError("claude credentials missing")
  raw = json.loads(creds_path.read_text())
  oauth = raw.get("claudeAiOauth")
  if not isinstance(oauth, dict):
    raise RuntimeError("claude credentials malformed")
  return creds_path, oauth


def _write_claude_oauth(creds_path: Path, oauth: dict) -> None:
  """Persists a refreshed claudeAiOauth block back to the credentials file
  in the CLI's expected shape (atomic tmp+rename so a crash mid-write can't
  leave a truncated file the chat path then reads).

  Writing it back is the point of refreshing here: the chat runner and the
  CLI read the same file, so a refresh on the registry path also un-expires
  the token for everyone else, instead of each caller re-refreshing.

  Read-modify-write the WHOLE file and replace only the claudeAiOauth block:
  the host CLI's .credentials.json (shipped into the container via the
  documented `docker cp ~/.claude/.credentials.json` dev workflow) also
  carries mcpOAuth and organizationUuid. Rewriting just {"claudeAiOauth": …}
  would drop those sibling keys on the first registry-path refresh, which is
  what 'the file stays the single source of truth' is meant to prevent. The
  file mode is preserved (the CLI writes 0600); we mirror it on the tmp file
  and default to 0600 when the source is gone."""
  try:
    raw = json.loads(creds_path.read_text())
    if not isinstance(raw, dict):
      raw = {}
  except (OSError, ValueError):
    raw = {}
  raw["claudeAiOauth"] = oauth
  try:
    mode = stat.S_IMODE(creds_path.stat().st_mode)
  except OSError:
    mode = 0o600
  tmp = creds_path.with_suffix(creds_path.suffix + ".tmp")
  fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
  try:
    with os.fdopen(fd, "w") as f:
      json.dump(raw, f)
    os.replace(tmp, creds_path)
  except Exception:
    tmp.unlink(missing_ok=True)
    raise


async def _refresh_claude_access_token(oauth: dict) -> dict:
  """Exchanges the stored refresh token for a fresh access token.

  Returns the new claudeAiOauth dict (accessToken/refreshToken/expiresAt/
  scopes preserved). Raises when there's no refresh token or the token
  endpoint rejects the exchange — the caller then falls back to
  KNOWN_MODELS, same as any other registry-fetch failure.

  Anthropic's OAuth refresh-token grant rotates the refresh token, so we
  persist whatever the endpoint returns (a stale refresh token would fail
  the NEXT refresh)."""
  import httpx  # local import — only the registry path needs it

  refresh_token = oauth.get("refreshToken")
  if not refresh_token:
    raise RuntimeError("claude credentials have no refresh token")
  async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.post(
      _CLAUDE_OAUTH_TOKEN_URL,
      json={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLAUDE_OAUTH_CLIENT_ID,
      },
      headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
  for field in ("access_token", "refresh_token", "expires_in"):
    if field not in data:
      raise RuntimeError(f"refresh response missing '{field}'")
  refreshed = dict(oauth)
  refreshed["accessToken"] = data["access_token"]
  refreshed["refreshToken"] = data["refresh_token"]
  refreshed["expiresAt"] = int(time.time() * 1000) + data["expires_in"] * 1000
  if data.get("scope"):
    refreshed["scopes"] = data["scope"].split()
  return refreshed


async def _claude_access_token(data_dir: str) -> str:
  """Returns a non-expired Claude OAuth access token, refreshing it when the
  stored one has expired (or is within the refresh margin).

  The 401 root cause this fixes: the registry call read `accessToken`
  verbatim and never checked `expiresAt`, so once the CLI's access token
  expired EVERY /v1/models call 401'd and the picker silently fell back to
  the static KNOWN_MODELS list forever (the CLI refreshes its own token for
  chat turns, but that refresh never ran for this out-of-band httpx call).
  We now refresh the same way the CLI does — refresh-token grant against the
  OAuth token endpoint — and write the result back so the file stays the
  single source of truth."""
  def _token_if_fresh(oauth: dict) -> str | None:
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt")
    if (
      token
      and isinstance(expires_at, (int, float))
      and expires_at - time.time() * 1000 >= _CLAUDE_TOKEN_REFRESH_MARGIN_MS
    ):
      return token
    return None

  creds_path, oauth = _read_claude_oauth(data_dir)
  fresh = _token_if_fresh(oauth)
  if fresh:
    return fresh
  async with _claude_refresh_lock:
    # Re-read under the lock: a coroutine that refreshed while we waited
    # already wrote a fresh token, so reusing it avoids burning a second
    # single-use rotation (the race this lock exists to close).
    creds_path, oauth = _read_claude_oauth(data_dir)
    fresh = _token_if_fresh(oauth)
    if fresh:
      return fresh
    refreshed = await _refresh_claude_access_token(oauth)
    try:
      _write_claude_oauth(creds_path, refreshed)
    except OSError as exc:
      # The refresh succeeded but persistence failed (read-only fs, perms).
      # The token in hand is still valid for this call; log and use it
      # rather than fall back to the stale KNOWN_MODELS list. Next call
      # re-refreshes.
      log.warning("could not persist refreshed claude token: %s", exc)
    return refreshed["accessToken"]


async def _fetch_claude_models(data_dir: str) -> list[str]:
  """Calls Anthropic's /v1/models with the stored OAuth access token.

  Raises on any non-2xx or missing credentials so the caller can fall
  back to KNOWN_MODELS. The Claude Code OAuth flow grants the
  user:inference scope which the models endpoint accepts. We use
  httpx (already a requirement) instead of pulling the `anthropic`
  SDK to keep dependency surface flat.

  The access token is refreshed in `_claude_access_token` when expired —
  without that, an expired CLI token 401s here and the picker is stuck on
  the static KNOWN_MODELS fallback until the container restarts.
  """
  import httpx  # local import — only the registry path needs it

  token = await _claude_access_token(data_dir)
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


def _codex_model_slug(entry: Any) -> str | None:
  """Extract a model id from SDK objects, raw CLI JSON dicts, or strings."""
  if isinstance(entry, str):
    return entry
  if isinstance(entry, dict):
    if entry.get("visibility") == "hide":
      return None
    slug = entry.get("slug") or entry.get("id")
    return slug if isinstance(slug, str) else None
  if getattr(entry, "visibility", None) == "hide":
    return None
  slug = getattr(entry, "slug", None) or getattr(entry, "id", None)
  return slug if isinstance(slug, str) else None


def _codex_model_slugs_from_payload(payload: Any) -> list[str]:
  """Extract ordered Codex model slugs from `codex debug models` JSON."""
  raw_models = payload.get("models") if isinstance(payload, dict) else payload
  if not isinstance(raw_models, list):
    return []
  ids: list[str] = []
  for entry in raw_models:
    slug = _codex_model_slug(entry)
    if slug:
      ids.append(slug)
  return ids


async def _fetch_codex_models_from_cli(data_dir: str) -> list[str]:
  """Fetch raw Codex model catalog JSON from `codex debug models`.

  This is the compatibility fallback for catalog schema drift in the Python
  SDK. New model metadata can add enum values before the SDK types are updated;
  the CLI debug command prints the raw catalog, so model discovery can keep
  working while chat execution still uses the SDK path.
  """
  codex_home = Path(data_dir) / "cli-auth" / "codex"
  codex_bin = shutil.which("codex")
  if not codex_bin:
    raise RuntimeError("codex CLI not found")
  env = dict(os.environ)
  env["CODEX_HOME"] = str(codex_home)
  proc = await asyncio.create_subprocess_exec(
    codex_bin,
    "debug",
    "models",
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=env,
  )
  try:
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
  except asyncio.TimeoutError:
    proc.kill()
    await proc.wait()
    raise RuntimeError("codex debug models timed out")
  if proc.returncode != 0:
    msg = stderr.decode("utf-8", "replace").strip()
    raise RuntimeError(f"codex debug models failed: {msg[:500]}")
  try:
    payload = json.loads(stdout.decode("utf-8"))
  except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise RuntimeError("codex debug models returned invalid JSON") from exc
  ids = _codex_model_slugs_from_payload(payload)
  if not ids:
    raise RuntimeError("codex debug models returned no model slugs")
  return ids


async def _fetch_codex_models(data_dir: str) -> list[str]:
  """Calls the Codex SDK's `AsyncCodex.models()`.

  Codex auth happens transparently inside the SDK — it reads the
  same `CODEX_HOME` directory the chat path uses, so once the user
  has connected Codex in Settings the call works. If the SDK cannot
  parse a newer model-catalog shape, fall back to `codex debug models`
  and extract only the slugs.
  """
  from openai_codex import AsyncCodex
  from openai_codex.client import CodexConfig

  codex_home = Path(data_dir) / "cli-auth" / "codex"
  if not (codex_home / "auth.json").exists():
    raise RuntimeError("codex credentials missing")
  # Match codex_sdk_runner.py's binary/env resolution: pass the resolved
  # path explicitly and set CODEX_HOME in the app-server environment.
  # Recent SDKs no longer accept `codex_home=` on CodexConfig.
  config = CodexConfig(
    codex_bin=shutil.which("codex"),
    env={"CODEX_HOME": str(codex_home)},
  )
  try:
    async with AsyncCodex(config=config) as codex:
      response = await codex.models()
  except Exception as exc:  # noqa: BLE001 - CLI raw catalog is the fallback
    log.warning(
      "codex SDK model registry fetch failed: %s; trying codex debug models",
      exc,
    )
    return await _fetch_codex_models_from_cli(data_dir)
  # The SDK returns a ModelListResponse with `.models` list. Each
  # entry exposes a `.slug` (the model ID). Defensive: tolerate
  # bare strings too in case the upstream shape drifts.
  raw_models = getattr(response, "models", None) or []
  ids: list[str] = []
  for entry in raw_models:
    slug = _codex_model_slug(entry)
    if slug:
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
) -> dict[str, list[dict[str, Any]]]:
  """Returns `{provider_id: [{id, label, provider, available}, ...]}`.

  Cache TTL is 5 minutes per provider. On upstream failure for a
  given provider we serve KNOWN_MODELS for THAT provider (the live
  data from the other provider still flows). `force_refresh=True`
  bypasses the cache — used by the manage-models modal's refresh
  button so the user can pull a just-released model on demand.

  Never raises — a failure on both providers still returns the full
  KNOWN_MODELS fallback for both.
  """

  def cache_fresh(provider_id: str) -> list[dict[str, Any]] | None:
    """Returns cached entries if a non-forced read can use them."""
    if force_refresh:
      return None
    cached = _model_registry_cache.get(provider_id)
    if not cached:
      return None
    if time.monotonic() - cached[0] >= _MODEL_CACHE_TTL_SECONDS:
      return None
    return cached[1]

  async def fetch_one(provider_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Refetches under the provider's lock, with a double-checked
    cache read inside the lock so we don't redo a refetch another
    caller just completed for us."""
    async with _model_registry_locks[provider_id]:
      hit = cache_fresh(provider_id)
      if hit is not None:
        return provider_id, hit
      try:
        live_ids = await _fetch_provider_models(provider_id, data_dir)
        entries = _live_model_entries(provider_id, live_ids)
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
  result: dict[str, list[dict[str, Any]]] = {}
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
