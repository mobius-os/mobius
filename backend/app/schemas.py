"""Pydantic request and response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import (
  BaseModel, ConfigDict, Field, field_validator, model_validator,
)

from app.providers import PROVIDER_NAMES, _model_belongs_to_other_provider


class SetupRequest(BaseModel):
  username: str = Field(min_length=1, max_length=64)
  # Generous enough for long passphrases while bounding accidental or hostile
  # setup payloads before password hashing work. bcrypt receives a fixed-width
  # digest, so every character remains significant.
  password: str = Field(min_length=1, max_length=1024)

  @field_validator("username", mode="before")
  @classmethod
  def normalize_username(cls, value):
    """Store the visible username, not accidental surrounding whitespace."""
    return value.strip() if isinstance(value, str) else value

  @field_validator("password")
  @classmethod
  def reject_blank_password(cls, value: str) -> str:
    if not value.strip():
      raise ValueError("Password cannot be blank.")
    return value

class SetupStatus(BaseModel):
  configured: bool
  auth_mode: Literal["local", "mobius_sso"] = "local"


class TokenResponse(BaseModel):
  access_token: str
  token_type: str = "bearer"


# Declared storage-access level. Used on two sides of the same coin:
#   cross_app_access  — what THIS app's token can do against others
#   share_with_apps   — what others can do against THIS app
# Cross-app traffic passes only when BOTH sides permit. Defaults to
# 'none' on both; the agent opts an app in when the partner asks.
ShareLevel = Literal["none", "read", "write"]


class AppApply(BaseModel):
  model_config = ConfigDict(extra="forbid")

  source_dir: str = Field(min_length=1, max_length=512)
  chat_id: str | None = Field(default=None, max_length=64)


class AppResolveUpdate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  source_dir: str = Field(min_length=1, max_length=512)


class AppUpdate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  # Drawer rename uses `name`; 500-char cap matches ChatPatch.title
  # so a runaway agent can't bloat the apps list response.
  name: str | None = Field(default=None, max_length=500)
  description: str | None = None
  # None means "omit from update" (the field is not changed).
  # To explicitly clear chat_id, pass an empty string ("").
  chat_id: str | None = None
  # Drawer pin toggle. True sets pinned_at = now, False clears it.
  pinned: bool | None = None
  cross_app_access: ShareLevel | None = None
  share_with_apps: ShareLevel | None = None
  # Owner-only DOWNGRADE of skills authority: False revokes immediately (the
  # request gate reads the live row, so already-minted app JWTs lose access on
  # their next call). Granting (True) is rejected — that path stays with the
  # reviewed manifest install.
  manage_skills: bool | None = None


class AppOut(BaseModel):
  id: int
  name: str
  description: str
  compiled_path: str
  chat_id: str | None = None
  source_dir: str | None = None
  pinned_at: datetime | None = None
  # A durable app-attributed notification landed since this app was last
  # opened. The shell renders the same quiet activity dot used for chats.
  has_unseen_activity: bool = False
  unseen_activity_version: int | None = None
  # Exact executable build last opened from the owning chat's CTA. Opening a
  # live preview and opening the settled result are separate acknowledgements:
  # the same build may surface once more when its agent turn finishes.
  preview_seen_updated_at: datetime | None = None
  preview_seen_final: bool = False
  cross_app_access: ShareLevel = "none"
  share_with_apps: ShareLevel = "none"
  offline_capable: bool = False
  # The app embeds an agent chat — surfaced as a badge. See models.App.
  embeds_agent: bool = False
  # Install authority — see models.App.manage_apps for the contract.
  manage_apps: bool = False
  # GitHub data/reviewed-submit access — see models.App.github_access.
  github_access: bool = False
  # Skills lifecycle authority (install/uninstall/catalog refresh) — see
  # models.App.manage_skills.
  manage_skills: bool = False
  # GitHub credential management — see models.App.github_connect.
  github_connect: bool = False
  # Guarded owner-filesystem access — see models.App.filesystem_access.
  filesystem_access: bool = False
  # URL slug for the standalone PWA install at /apps/<slug>/. Null
  # only for legacy rows from before the slug column existed; lazy-
  # backfilled on first access via standalone routes (see
  # routes/apps.py:ensure_slug).
  slug: str | None = None
  # URL the app was installed from (manifest URL passed to
  # POST /api/apps/install). Null for user-built apps. The install
  # endpoint matches by this for update-vs-install discrimination.
  manifest_url: str | None = None
  # The manifest version currently installed (e.g. "1.7.0"). Null for
  # user-built apps and for rows installed before the column existed
  # (they backfill on their next update). The store reads this to show
  # "Installed · vX.Y.Z" and to detect when the catalog ships a newer
  # version — see models.App.version.
  version: str | None = None
  # Optional standalone PWA colors persisted from installed app manifests.
  theme_color: str | None = None
  background_color: str | None = None
  # Optional PWA display mode (web-manifest `display`). Null → "standalone".
  display: str | None = None
  # Offline contract from the manifest `offline` block (P1-D). None when no
  # block was declared; otherwise the raw validated JSON object. Informational
  # for the agent + future store badge — no server-side enforcement.
  offline_contract: dict | None = None
  # Root-level manifest file composed into the agent prompt while this app is
  # live. Informational so install UIs can surface the privileged declaration.
  system_prompt_file: str | None = None
  system_app: bool = False
  chat_log_access: Literal["none", "summary", "full"] = "none"
  capability_contract: dict | None = None
  created_at: datetime
  updated_at: datetime

  model_config = {"from_attributes": True}


class AppApplyOut(BaseModel):
  mode: Literal["created", "updated", "unchanged"]
  app: AppOut


class AppResolveUpdateOut(BaseModel):
  mode: Literal["updated", "conflict"]
  app: AppOut
  warnings: list[str] = Field(default_factory=list)
  conflict_paths: list[str] = Field(default_factory=list)


class AppInstall(BaseModel):
  """Body for POST /api/apps/install — atomic install from a manifest.

  Exactly one of `manifest_url` or `manifest` must be set:
    - `manifest_url`: the installer GETs the manifest, derives raw_base
      from the URL (everything before the trailing filename), and
      fetches the entry JSX + icon + storage_seed files relative to it.
    - `manifest`: an inline manifest object. The caller must also pass
      `raw_base` so the installer knows where to fetch referenced files
      from. Useful for tests + future "install from local tarball".
  """
  manifest_url: str | None = None
  manifest: dict | None = None
  raw_base: str | None = None
  # Optional review binding supplied by an install UI. Direct owner/agent
  # installs may omit it; when present, install must apply the exact capability
  # contract the owner just reviewed or fail with 409 before mutating state.
  reviewed_capability_digest: str | None = Field(
    default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$",
  )
  # Optional binding for a pre-update source review. If the manifest or any
  # executable source byte changes before Apply, install rejects before writes.
  reviewed_source_digest: str | None = Field(
    default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$",
  )


class AppPreviewOut(BaseModel):
  manifest: dict
  capability_contract: dict
  capability_digest: str
  installed_contract: dict | None = None
  capability_diff: dict


class AppInstallOut(AppOut):
  """Install endpoint response — AppOut plus install-only fields."""
  # 'install' for a fresh row, 'update' if a same-manifest app already
  # existed and got its jsx_source / seeds / cron refreshed in place,
  # 'conflict' if the per-app git model is enabled and merging the new
  # upstream into local edits conflicted (feature 084). On 'conflict'
  # the served app is UNCHANGED — local edits are preserved and the
  # conflicting files are named in `conflict_paths` for an agent to
  # resolve. 'conflict' never occurs while the flag is explicitly off.
  mode: Literal["install", "update", "conflict"]
  # The version currently installed after the call. On `conflict`, the served
  # app is unchanged, so this remains the pre-conflict version.
  version: str
  # The manifest version that was fetched but could not be applied yet. Only
  # present on `mode == "conflict"`.
  upstream_version: str | None = None
  # Steps that were skipped because mini-app permissions could only
  # take them so far — e.g. "icon: 404 in source repo", "schedule:
  # no shell access (manual agent step)". Empty list = full success.
  warnings: list[str] = Field(default_factory=list)
  # Files that conflicted when merging the new upstream into local
  # edits. Non-empty only when `mode == "conflict"`. The store surfaces
  # these so the owner can ask the agent to resolve them.
  conflict_paths: list[str] = Field(default_factory=list)
  # How the local working branch related to the upstream this update
  # carried. Meaningful only when per-app git is enabled; conflicts are
  # carried by `mode == "conflict"`, not this field.
  divergence: Literal["none", "fast_forward", "clean_merge"] = "none"


class AppScheduleUpdate(BaseModel):
  """Body for updating one installed app's cron schedule."""

  cron: str
  job: str | None = None


class AppScheduleOut(BaseModel):
  """Read-only metadata for an installed app's recurring cron job."""

  id: int
  name: str
  slug: str | None = None
  cron: str
  job: str
  next_run: datetime | None = None


class ConflictFile(BaseModel):
  path: str
  merged_with_markers: str


class UpdatePreviewOut(BaseModel):
  app_id: int
  status: Literal["clean", "conflict"]
  upstream_version: str | None = None
  upstream_commit: str | None = None
  conflict_paths: list[str] = Field(default_factory=list)
  conflicts: list[ConflictFile] = Field(default_factory=list)
  upstream_diff: str | None = None


class UpdateCandidatePreviewOut(BaseModel):
  """Incoming published source compared with the last installed upstream."""

  app_id: int
  upstream_version: str | None = None
  # The installed upstream base used for the comparison. This is intentionally
  # not the candidate's remote SHA: synthetic manifest installs have no remote
  # commit, but both package shapes share the same source-diff contract.
  upstream_commit: str | None = None
  upstream_diff: str | None = None
  source_digest: str = Field(
    min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$",
  )


class UpdateCheckOut(BaseModel):
  """Git-native update detection: fetched upstream source vs recorded upstream.

  `update_available` is null (unknown) when a content compare can't run — no
  manifest_url, no git repo, no recorded upstream branch, or the upstream fetch
  failed — so the caller falls back to version comparison. It is a real
  true/false only when a byte-level compare actually ran, so a push that changed
  code WITHOUT bumping the version still reads as an update.

  A durable conflict receipt has two materially different states. In
  `needs_resolution`, upstream is not yet incorporated into local source (or a
  materialized merge still has conflicts). In `replay_pending`, source was
  resolved and committed but the canonical installer still has to promote the
  bundle/metadata transaction. Only the former should open a resolver chat.
  `needs_resolution` remains as a derived rolling-deploy compatibility field;
  new consumers should use `pending_update_state`. `unknown` means a receipt
  proves an update is pending but Git could not safely classify its resolution
  phase. The version strings are display-only, never the detection signal."""
  update_available: bool | None = None
  pending_update_state: Literal[
    "none", "needs_resolution", "replay_pending", "unknown",
  ] = "none"
  needs_resolution: bool = False
  upstream_version: str | None = None
  local_version: str | None = None
  checked_at: datetime


class AppConflictResolverChatOut(BaseModel):
  chat_id: str
  created: bool
  started: bool


class ProviderCodeRequest(BaseModel):
  code: str


class AppTokenRequest(BaseModel):
  app_id: int


class ChatMessage(BaseModel):
  # Request-side only. This is the shape a client POSTs in ChatRequest.messages,
  # not the persisted or returned message shape. Chat transcripts are stored as
  # dicts inside the Chat.messages JSON column; see the message-shape and
  # single-writer rules in ARCHITECTURE.md (Chat persistence — single-writer actor).
  role: str
  content: str


class ChatRequest(BaseModel):
  messages: list[ChatMessage]
  chat_id: str = ""


class ChatStopRequest(BaseModel):
  chat_id: str = ""


class AgentSettingsOverride(BaseModel):
  """Per-chat agent settings override. Unknown fields are rejected
  (422) so a typo'd or experimental key can't silently land in the
  persisted chat row + every GET response. To add a new field,
  declare it explicitly below."""

  model_config = ConfigDict(extra="forbid")

  model: str | None = None
  # Union of both SDKs' effort enums:
  #   Codex `ReasoningEffort` (openai-codex): none, minimal, low,
  #     medium, high, xhigh — and, since rust-v0.145.0-alpha.13, a
  #     forgiving `str` enum that also accepts the efforts newer models
  #     advertise (gpt-5.6-sol: max, ultra) instead of rejecting them.
  #     The picker scopes which values it actually offers per model.
  #   Claude `EffortLevel` (claude-agent-sdk): low, medium, high,
  #     xhigh, max (xhigh + max are Opus-tier only).
  # Plus one Möbius-only Claude tier: `ultracode` — the Claude Code
  # CLI's ultracode mode (xhigh effort + dynamic multi-agent Workflow
  # orchestration). It is NOT an SDK EffortLevel; `claude_sdk_runner`
  # translates it to `--effort xhigh` + the CLI's ultracode keyword
  # trigger. Accepted here so the PATCH round-trips; the runner owns
  # the translation.
  # The picker enforces per-provider scoping. Runners forward the
  # value as-is to the SDK; a model/effort mismatch (e.g. `max` on a
  # non-Opus Claude) surfaces as a 400 at turn time, not at PATCH.
  # Acceptable per the platform's "reversibility over prevention"
  # philosophy.
  effort: Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultracode"
  ] | None = None
  # Per-provider memory of the last-picked effort. The enums are
  # NOT comparable across providers — Codex `medium` is roughly
  # Claude `low`, not Claude `medium` — so the picker has to
  # remember each provider's last value separately and swap
  # `effort` to that value when the user switches providers.
  # Frontend writes the full dict each PATCH; backend stores it
  # verbatim under this key.
  effort_by_provider: dict[str, str] | None = None


class ChatPatch(BaseModel):
  """Partial-update payload for chat runtime settings."""

  agent_settings_json: AgentSettingsOverride | None = None
  clear_agent_settings: bool = False
  provider: str | None = None
  # Drawer rename uses this. Empty string is rejected so a misfired
  # blur on an empty input can't blank the title in the sidebar.
  # 500-char cap defends against runaway-agent megabyte payloads
  # bloating the chats list response on every refresh.
  title: str | None = Field(default=None, max_length=500)
  # Drawer pin toggle. True sets pinned_at = now, False clears it.
  pinned: bool | None = None
  # Per-chat automatic continuation after a paid provider limit.
  auto_resume_on_limit: bool | None = None
  # Per-chat automatic continuation after a supervisor-authenticated restart.
  auto_resume_on_restart: bool | None = None
  # Naming precedence. by_agent marks an AGENT title-sync — it fills the name
  # only when the owner hasn't locked it via a manual rename. clear_title resets
  # the name (unlock + drop to the first-message default; re-derived next turn).
  by_agent: bool = False
  clear_title: bool = False

  @field_validator("provider")
  @classmethod
  def validate_provider(cls, value: str | None) -> str | None:
    """Reject unknown provider IDs at request-deserialize time."""
    if value is not None and value not in PROVIDER_NAMES:
      raise ValueError(f"unknown provider: {value}")
    return value


class ChatProviderSwitch(BaseModel):
  """Atomic cross-provider switch prepared by the incoming provider."""

  provider: Literal["claude", "codex"]
  agent_settings_json: AgentSettingsOverride
  # Stable across a network retry so the writer can return the already-stored
  # switch instead of appending a duplicate compaction marker.
  switch_id: str = Field(min_length=1, max_length=128)

  @model_validator(mode="after")
  def validate_target_settings(self):
    """Require a coherent target runtime instead of preserving old settings."""
    model = (self.agent_settings_json.model or "").strip()
    effort = self.agent_settings_json.effort
    if not model or len(model) > 200:
      raise ValueError("provider switches require a valid target model")
    if _model_belongs_to_other_provider(model, self.provider):
      raise ValueError("target model does not belong to target provider")
    self.agent_settings_json.model = model
    allowed_efforts = {
      "codex": {"none", "minimal", "low", "medium", "high", "xhigh"},
      "claude": {"low", "medium", "high", "xhigh", "max", "ultracode"},
    }
    if effort not in allowed_efforts[self.provider]:
      raise ValueError("target effort does not belong to target provider")
    return self


class SendMessage(BaseModel):
  content: str
  attachments: list[dict] | None = None
  timezone: str | None = None
  viewport: dict | None = None
  hidden: bool = False
  # Client-minted stable identity for the user message, minted once at
  # compose time and carried across the wire. It is the canonical row
  # identity (React key, DOM pin target, queue cancel key, steer dedup
  # key); `ts` is display/ordering metadata only. Untrusted client input:
  # never an auth boundary — a duplicate cid is treated as a duplicate
  # POST retry, not a security event.
  cid: str | None = None
  # Internal UI hint: Stop can collapse already-queued messages and ask
  # the backend to steer them into the live turn even when the chat's
  # normal send-while-running behavior is queueing.
  force_steer: bool = False
  # Which already-queued rows a force-steer should pull into the live
  # turn, selected by their stable `cid` (the server still reconstructs
  # the durable rows from Chat.pending_messages so the browser cannot
  # forge transcript entries).
  consume_pending_cids: list[str] | None = None
  # Optional UI hint for force-steering multiple already-queued messages.
  # The server still reconstructs the durable rows from Chat.pending_messages
  # so the browser cannot forge transcript entries; this only lets newer
  # clients declare that they expect separate ordered rows rather than one
  # joined row.
  steered_messages: list[dict] | None = None
  # When `hidden=True` and the user is answering an AskUserQuestion,
  # frontend includes the resolved answers here. Backend either resolves the
  # live parked future or persists the answer with a recovered hidden
  # continuation — eliminating the POST /question-answers + POST /messages
  # race that left answers missing on mid-stream remounts.
  answers: dict | None = None
  # Optional identity of the question being answered (the runner-
  # published PendingQuestion id). When supplied, the backend writes
  # the answers into the question block with this exact `question_id`
  # instead of the latest assistant message's question block — fixing
  # the wrong-block bug when two questions are open at once. Optional
  # so older clients that omit it keep working via the latest-question
  # fallback (no behaviour change when absent).
  question_id: str | None = None


class PushKeys(BaseModel):
  p256dh: str
  auth: str


class PushSubscribeRequest(BaseModel):
  endpoint: str
  keys: PushKeys


class PushUnsubscribeRequest(BaseModel):
  endpoint: str


class NotificationAction(BaseModel):
  action: str
  title: str
  target: str | None = None


class NotificationSendRequest(BaseModel):
  title: str
  body: str | None = None
  icon: str | None = None
  target: str | None = None
  actions: list[NotificationAction] | None = None
  # Defaults to 'agent' so the common agent-authored curl works
  # with just {title, body}. Apps should pass 'app' + their id.
  source_type: str = "agent"
  source_id: str | None = None


class BackgroundAgentChoice(BaseModel):
  """One provider/model choice for unattended background work."""

  provider: str | None = None
  model: str | None = Field(default=None, max_length=256)
  effort: str | None = Field(default=None, max_length=32)
  enabled: bool | None = True

  @field_validator("provider")
  @classmethod
  def validate_provider(cls, value: str | None) -> str | None:
    if value is not None and value not in PROVIDER_NAMES:
      raise ValueError(f"unknown provider: {value}")
    return value


class BackgroundAgentsUpdate(BaseModel):
  """System-level provider choices for scheduled app agents."""

  providers: list[BackgroundAgentChoice] | None = None
  primary: BackgroundAgentChoice | None = None
  fallback: BackgroundAgentChoice | None = None


class SettingsUpdate(BaseModel):
  """Owner-level settings updates."""

  # Settings used to include a global auto-resume flag. Reject stale cached
  # clients (and typos) instead of returning {ok: true} for an ignored field.
  model_config = ConfigDict(extra="forbid")

  provider: str | None = None
  # Legacy owner-level agent settings. Live chat surfaces should write
  # per-chat choices through ChatPatch.agent_settings_json.
  agent_settings: AgentSettingsOverride | None = None
  background_agents: BackgroundAgentsUpdate | None = None
  # Opt-in to offering SDK skills to the Claude agent. Behavior-
  # shifting and default-off (see providers.skills_enabled); persisted
  # to the shared agent-settings.json rather than the frozen Owner
  # model. None means "leave unchanged".
  skills_enabled: bool | None = None

  @field_validator("provider")
  @classmethod
  def validate_provider(cls, value: str | None) -> str | None:
    """Reject unknown provider IDs at request-deserialize time."""
    if value is not None and value not in PROVIDER_NAMES:
      raise ValueError(f"unknown provider: {value}")
    return value


class ModelEntry(BaseModel):
  """One row in the model registry — what the picker renders."""

  id: str
  label: str
  provider: str
  # True for rows returned by a successful live fetch or by the
  # fallback registry. Kept in the response shape for compatibility
  # with older clients that already read this field.
  available: bool = True
  # Optional per-model capability metadata. Absent means "use the provider's
  # default scale" so older registry producers and newly-discovered models keep
  # working. A future model with a narrower/different scale can declare it here
  # without teaching every picker about that model id.
  effort_levels: list[str] | None = None


class ModelRegistryResponse(BaseModel):
  """`GET /api/models` response shape."""

  # Map provider id → ordered list of models.
  providers: dict[str, list[ModelEntry]]


class ModelPrefsUpdate(BaseModel):
  """`PATCH /api/owner/model-prefs` body.

  `hidden_ids` replaces the persisted list verbatim — the modal
  always sends the full set so partial-update merge logic isn't
  needed. Passing an empty list clears all hidden entries (shows
  everything). Unknown IDs are tolerated; they no-op until/unless
  the registry surfaces them again.
  """

  model_config = ConfigDict(extra="forbid")

  hidden_ids: list[str] = Field(default_factory=list)


class NotificationOut(BaseModel):
  id: str
  source_type: str
  source_id: str | None
  title: str
  body: str | None
  icon: str | None
  target: str | None
  actions: list | None
  sent_at: datetime
  clicked_at: datetime | None

  model_config = {"from_attributes": True}
