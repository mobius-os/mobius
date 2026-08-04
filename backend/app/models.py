"""SQLAlchemy ORM models.

Served from the editable platform checkout. main.py and many route modules
import these at module load; if a local edit breaks them, normal boot falls back
to the baked platform and external recovery can repair the preserved checkout.

To add a column to an existing table, edit and restart. For per-chat fields you
can usually skip a migration by
adding to `Chat.agent_settings_json` (a JSON column intentionally
included as the no-migration escape hatch). For non-secret app-scoped data
you'd otherwise add a column for, use per-app storage at
`/data/apps/<app_id>/...` via the storage API. Credentials belong in the
separate encrypted app-secrets API.
"""

import secrets
from datetime import UTC, datetime

from sqlalchemy import (
  Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Integer, JSON,
  LargeBinary, String, Text, UniqueConstraint, event, false, or_, true,
)

from sqlalchemy.orm import column_property, validates

from app.database import Base
from app.timeutil import now_naive_utc
from app.tool_output_storage import CompressedToolOutputText


CONTINUATION_RUN_STATUSES = ("parked", "resume_pending")
NONTERMINAL_RUN_STATUSES = ("running", *CONTINUATION_RUN_STATUSES)


class Owner(Base):
  """Single owner account for this installation."""

  __tablename__ = "owner"

  id = Column(Integer, primary_key=True)
  username = Column(String(64), nullable=False, unique=True)
  hashed_password = Column(String(255), nullable=False)
  # Managed Railway deployments bind the local single-owner row to the stable
  # launcher user id. Null is the ordinary self-hosted/password-only mode.
  # The email is informational and never used as an authorization key.
  sso_subject = Column(String(128), nullable=True)
  sso_email = Column(String(320), nullable=True)
  # Must stay in sync with providers.PROVIDER_NAMES.
  provider = Column(String(32), nullable=False, default="claude")
  # Default provider-limit recovery policy for newly-created chats. Each chat
  # stores its own copy; changing a chat's switch updates this seed for the
  # next chat without rewriting any existing conversation. Automatic provider
  # retries are initially off because they can consume paid usage.
  auto_resume_on_limit_default = Column(
    Boolean, nullable=False, default=False, server_default=false()
  )
  # Planned restarts are initiated by Möbius, so continuing interrupted work
  # is initially on. This remains independently configurable per chat.
  auto_resume_on_restart_default = Column(
    Boolean, nullable=False, default=True, server_default=true()
  )
  # Per-owner model-picker preferences. Shape:
  #   {"hidden_ids": ["claude-haiku-4-5-20251001", ...]}
  # The picker filters out any registry entry whose ID appears in
  # `hidden_ids`. Stored as JSON so future filter dimensions (sort
  # overrides, pinned models, per-provider hiding) can land without
  # a migration. Null means "show everything" — the picker treats
  # absence as the default state. Stale IDs (an entry referring to
  # a model the registry no longer returns) are tolerated silently:
  # the picker simply doesn't filter anything it can't find, and
  # cleanup happens lazily next time the owner edits prefs.
  model_prefs_json = Column(JSON, nullable=True, default=None)
  # Set the first time the user finishes (or explicitly skips) the
  # post-signup walkthrough. NULL means "show the walkthrough on next
  # sign-in." Once set, never re-shown. The timestamp is kept (rather
  # than a boolean flag) so we can correlate first-completion against
  # other onboarding signals later — same shape as a SCD type 1 row.
  walkthrough_completed_at = Column(DateTime, nullable=True, default=None)
  # Monotonic JWT-validity generation. Every owner-derived token (the
  # 30-day login token, the 8h app token, the 2h agent token, the
  # 90-day service token) is stamped with the owner's token_epoch at
  # mint time; the owner-resolving dependency in deps.py rejects any
  # token whose stamped epoch is behind this value. Incrementing it is
  # "sign out everywhere" — it invalidates every outstanding token at
  # once without rotating SECRET_KEY (which would also break encrypted app
  # secrets and the CLI credential derivation). A
  # token minted before this column existed carries no epoch claim and
  # reads as epoch 0, which equals a freshly-migrated owner's epoch, so
  # legacy tokens stay valid until the first bump.
  token_epoch = Column(Integer, nullable=False, default=0)
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class SystemPromptSnapshot(Base):
  """Deduplicated immutable prompt bytes captured at a chat's first turn."""

  __tablename__ = "system_prompt_snapshots"

  # sha256(content), so identical platform/app compositions across many chats
  # occupy one row and updates naturally create a new immutable identity.
  id = Column(String(64), primary_key=True)
  content = Column(Text, nullable=False)
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class Chat(Base):
  """A chat conversation with the agent."""

  __tablename__ = "chats"

  id = Column(String(64), primary_key=True)
  title = Column(String(256), nullable=False, default="New chat")
  # Naming precedence: user > agent > first-message. `title_locked` flips true
  # when the OWNER manually renames; the agent's title-sync (PATCH by_agent=true)
  # then never overwrites it. A clear-title PATCH resets it to false so the name
  # drops back to the agent summary / first message and gets re-derived.
  title_locked = Column(Boolean, nullable=False, default=False)
  messages = Column(JSON, nullable=False, default=list)
  # Drawer/list reads need only to know whether a transcript is empty. Keeping
  # that fact beside the blob prevents every chat-list request from scanning
  # every stored transcript. All runtime transcript writes flow through normal
  # ORM assignment or the two explicit bulk paths in chat_writer.
  has_messages = Column(
    Boolean, nullable=False, default=False, server_default=false()
  )
  # Current in-flight assistant state is separate from immutable history so a
  # streaming update never rewrites every prior message. Finalize and startup
  # recovery merge this bounded value into `messages`.
  live_assistant = Column(JSON, nullable=True, default=None)
  pending_messages = Column(JSON, nullable=False, default=list)
  uploads = Column(JSON, nullable=False, default=list)
  deleted_at = Column(DateTime, nullable=True, default=None)
  session_id = Column(String(128), nullable=True, default=None)
  # Must stay in sync with providers.PROVIDER_NAMES.
  provider = Column(String(32), nullable=False, default="claude")
  # Per-chat overrides for the agent runtime (model, effort, future
  # fields like thinking budget). When null, the chat uses the global
  # default from /data/shared/agent-settings.json. Stored as JSON
  # rather than dedicated columns so new fields can land without a
  # migration. Read in `chat.py:_run_chat_impl` and merged over the
  # file-loaded defaults; written by `PATCH /api/chats/{id}` from the
  # composer popover's model picker (see `ChatSettingsPanel`).
  agent_settings_json = Column(JSON, nullable=True, default=None)
  # Content-addressed system-prompt snapshot selected when this chat starts
  # its first turn. The provider receives the referenced bytes on every API
  # call (provider SDKs are stateless at that boundary), but Möbius never
  # recomposes installed-app fragments for an already-started chat. Installing,
  # updating, or uninstalling a system app therefore affects only chats that
  # start afterwards. Nullable is the migration/empty-chat state: the first
  # turn snapshots it atomically before invoking a provider.
  system_prompt_snapshot_id = Column(String(64), nullable=True, default=None)
  # Per-chat policy for automatic recovery after provider limits. Initially
  # off because another attempt can consume paid usage.
  auto_resume_on_limit = Column(
    Boolean, nullable=False, default=False, server_default=false()
  )
  # Per-chat policy for continuing after a supervisor-authenticated planned
  # restart. Initially on because Möbius interrupted the work itself.
  auto_resume_on_restart = Column(
    Boolean, nullable=False, default=True, server_default=true()
  )
  # Drawer pinning: NOT NULL = pinned, NULL = unpinned. Sort key for
  # the chats list — pinned rows render first, ordered by this
  # column DESC (newest pin at top of pinned group). PATCH
  # /api/chats/{id} accepts `pinned: bool` to toggle.
  pinned_at = Column(DateTime, nullable=True, default=None)
  # App that created this chat, when it was opened through the
  # app-attributed chat contract (design §1) rather than by the owner
  # in the shell. NULL = an ordinary owner chat. Set, this chat is
  # "owned" by that app: its token (and only its token, plus the owner)
  # may send to it, and app-driven turns are attributable + cappable
  # back to the app. The owner can always see + drive these chats; the
  # column is the actor tag, not an access fence against the owner.
  created_by_app_id = Column(
    Integer, ForeignKey("apps.id"), nullable=True, default=None
  )
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))
  updated_at = Column(
    DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
  )
  # Advances ONLY when the OWNER sends a message into this chat (initial
  # send, a queued send, or a fast-forward/steer send). This is the drawer
  # ordering key, deliberately decoupled from `updated_at` — which usually
  # bumps on row writes (run markers, session id, the agent's auto-retitle)
  # and would otherwise re-sort the chat to the top on activity the owner did
  # not initiate. No onupdate here.
  activity_at = Column(
    DateTime, nullable=True, default=lambda: datetime.now(UTC)
  )

  @validates("messages")
  def _sync_has_messages(self, _key, value):
    self.has_messages = bool(value)
    return value


class ChatRun(Base):
  """Durable per-turn run record and persisted run-state authority.

  One row per turn, keyed by the in-memory run_token (which IS this row's
  `id` — the same identity the actor commands and the sink already carry, not
  a second one). A row left ``status == "running"`` by a process that died is an
  interrupted turn that boot reconciliation resolves. It also carries the
  per-run attribution one shared column never could (provider, cost, the
  initiating app), which the app-attributed-chat contract (077 §1) and the
  redacted chat-log read API (Capability B) build on.

  The writer creates and transitions this row in the same transaction as the
  transcript mutation that starts, parks, or completes a turn. Startup and
  runtime recovery therefore consume the exact same identity the live sink
  carries instead of maintaining a second per-chat status marker.
  """

  __tablename__ = "chat_runs"

  # The run_token, verbatim — one durable identity for the turn.
  id = Column(String(64), primary_key=True)
  # Stable identity for one logical turn across physical restart/resume runs.
  # A fresh turn points at itself; durable continuation markers inherit the
  # first physical run's id. Provider processes may come and go while this
  # value remains the join key used by delegation idempotency + Workflows.
  root_run_id = Column(
    String(64), nullable=True, index=True,
    default=lambda context: context.get_current_parameters().get("id"),
  )
  chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=False, index=True
  )
  # "running" while in flight; terminal outcomes are "completed" for a clean
  # turn, "failed" for a provider/setup error, "stopped" for an explicit user
  # Stop, and "interrupted" for crash/supersession/watchdog recovery. Provider
  # limits additionally use the parked/resume_pending/parked_notified states.
  # A successfully drained planned restart reuses that retry path with
  # park_reason="restart"; an unplanned crash remains "interrupted".
  status = Column(String(16), nullable=False, default="running", index=True)
  provider = Column(String(32), nullable=True, default=None)
  # Objective shown by the shell while this exact run owns a native goal.
  # This belongs to the run rather than the transcript tail: mid-turn owner
  # questions are steered into the same run and must not make the goal vanish
  # after a reload. NULL is an ordinary non-goal run.
  goal_objective = Column(Text, nullable=True, default=None)
  # App that initiated this turn under the app-attributed-chat contract
  # (077 §1). NULL = an ordinary owner-driven turn. Reserved now so the
  # attribution lands on the run row, not retrofitted later.
  initiated_by_app_id = Column(
    Integer, ForeignKey("apps.id"), nullable=True, default=None
  )
  # Per-run provider cost. Rows created before usage telemetry remain NULL.
  cost_usd = Column(Float, nullable=True, default=None)
  # Provider session/thread id active for this run. This lets diagnostics
  # distinguish a fresh provider context from a resumed one without relying on
  # Chat.session_id, which is only the latest pointer.
  provider_session_id = Column(String(128), nullable=True, default=None)
  # Provider-neutral per-turn totals. `input_tokens` means total context
  # processed for the turn, including cached input; the cache columns split
  # that total where the provider exposes the distinction. Raw/provider-
  # specific cumulative detail stays in usage_json for forward compatibility.
  input_tokens = Column(Integer, nullable=True, default=None)
  output_tokens = Column(Integer, nullable=True, default=None)
  cache_read_input_tokens = Column(Integer, nullable=True, default=None)
  cache_creation_input_tokens = Column(Integer, nullable=True, default=None)
  reasoning_output_tokens = Column(Integer, nullable=True, default=None)
  total_tokens = Column(Integer, nullable=True, default=None)
  model_context_window = Column(Integer, nullable=True, default=None)
  usage_json = Column(JSON, nullable=True, default=None)
  started_at = Column(DateTime, default=lambda: datetime.now(UTC))
  ended_at = Column(DateTime, nullable=True, default=None)
  # Provider rate/usage-limit parking (design §2.4). When a turn dies on a
  # provider limit, the run is PARKED instead of just cleared: `status` moves
  # to "parked", `parked_until` holds the reset time (naive UTC, matching every
  # other DateTime here), and `park_reason` a short label ("rate_limit" /
  # "usage_limit" / …). Planned restarts also use this row with
  # park_reason="restart" and a due time of now. No separate state enum is
  # needed. The liveness checks read it via
  # `chat._parked_until_for_chat`; the periodic reset sweep notifies once at
  # `parked_until`; auto-resume may pass through the retryable
  # "resume_pending" state before the row becomes terminal. Null on every
  # non-parked run and on rows created before this column existed.
  parked_until = Column(DateTime, nullable=True, default=None)
  park_reason = Column(String(32), nullable=True, default=None)
  # One-shot platform-authored intent identity for a planned restart. It only
  # authorizes replay when the frozen supervisor's root-owned boot ledger binds
  # the same nonce + exact run id to the current boot.
  restart_nonce = Column(String(128), nullable=True, default=None)


class Delegation(Base):
  """Immutable control plane for one durable delegated task.

  The child conversation is an ordinary hidden app-owned ``Chat`` and its
  physical execution state remains authoritative in ``ChatRun``. This row
  stores only the immutable intent/policy needed to attach retries, constrain
  the SDK runner, and relate the child back to its parent logical run. Status is
  deliberately NOT duplicated here: every read derives it from the child run.
  """

  __tablename__ = "delegations"
  __table_args__ = (
    UniqueConstraint(
      "parent_root_run_id", "task_key",
      name="uq_delegations_parent_root_task",
    ),
  )

  id = Column(String(64), primary_key=True)
  app_id = Column(Integer, ForeignKey("apps.id"), nullable=False, index=True)
  parent_chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=False, index=True
  )
  # References the first physical run by value. Kept free of an FK so durable
  # audit history can outlive an unusual run-row repair without orphaning the
  # child chat or weakening the idempotency key.
  parent_root_run_id = Column(String(64), nullable=False, index=True)
  task_key = Column(String(128), nullable=False)
  child_chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=False, unique=True, index=True
  )
  provider = Column(String(32), nullable=False)
  model = Column(String(256), nullable=True)
  effort = Column(String(32), nullable=True)
  scope = Column(String(16), nullable=False)
  cwd = Column(String(1024), nullable=False)
  prompt_sha256 = Column(String(64), nullable=False)
  max_budget_usd = Column(Float, nullable=True)
  created_at = Column(DateTime, nullable=False, default=lambda: now_naive_utc())
  cancelled_at = Column(DateTime, nullable=True, default=None)


class ChatSessionLink(Base):
  """Append-only provider-session -> chat identity map (subagent observability).

  One row per (provider, session_id) the runner has ever persisted for a chat.
  The invariant is append-only: a first sighting inserts, a re-sighting only
  bumps ``last_seen_at``, and nothing on the normal path deletes a row (they
  ride the chat's hard-purge, same as ``chat_runs``).

  This is deliberately NOT ``Chat.session_id``. That column holds only the
  CURRENT session and is wiped whenever the owner switches providers (a Claude
  session id is not a valid Codex thread id, so the switch NULLs it in
  ``routes/chats.py``) or a session otherwise resets. Once that live pointer
  moves on, the old session id is unrecoverable from ``Chat``. This map never
  forgets, so an observer can resolve any session id a chat was ever seen under
  back to that chat — across provider switches and session resets.

  Composite PK ``(provider, session_id)``: a session id is unique within a
  provider, and one chat legitimately accumulates several rows over its life (a
  fresh Claude session, a Codex thread after a switch, a re-resumed id). All
  writes go through ``session_links.record_session_link`` — do not insert here
  directly.

  ``create_all`` builds this table on the next boot — a new table needs no ALTER
  migration (see ``run_migrations``, which only ALTERs existing tables); existing
  rows are untouched.
  """

  __tablename__ = "chat_session_links"

  provider = Column(String(32), primary_key=True)
  session_id = Column(String(128), primary_key=True)
  chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=False, index=True
  )
  # Naive UTC to match SQLite's DATETIME round-trip (see timeutil.now_naive_utc),
  # so `last_seen_at DESC` ordering compares like-for-like values. Both stamps
  # are set explicitly by record_session_link; these defaults are the safety net.
  first_seen_at = Column(DateTime, default=lambda: now_naive_utc())
  last_seen_at = Column(DateTime, default=lambda: now_naive_utc())


class AgentLifecycleEvent(Base):
  """Append-only normalized lifecycle milestones for spawned helpers.

  Provider-native identifiers and timestamps are retained for audit, while
  ``agent_id`` is the stable cross-provider identity exposed to Workflows.
  Prompt bodies never belong here; summaries are bounded and scrubbed by
  ``agent_lifecycle.normalize_chat_event`` before insertion.

  ``agent_id`` identifies a logical provider thread/task; ``activation_id``
  identifies one use inside a root ChatRun. ``event_key`` is the unique fact
  idempotency key. ``id`` is the AUTOINCREMENT ingestion cursor used only for
  incremental API reads and is never reused after tail deletion.
  """

  __tablename__ = "agent_lifecycle_events"
  __table_args__ = {"sqlite_autoincrement": True}

  id = Column(Integer, primary_key=True, autoincrement=True)
  event_key = Column(String(64), nullable=False, unique=True, index=True)
  chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=False, index=True
  )
  chat_run_id = Column(
    String(64), ForeignKey("chat_runs.id"), nullable=True, index=True
  )
  provider = Column(String(32), nullable=False)
  provider_session_id = Column(String(160), nullable=True)
  provider_agent_id = Column(String(160), nullable=False)
  agent_id = Column(String(70), nullable=False, index=True)
  # ``stable_activation_id`` is ``activation-`` (11 chars) plus a 64-char
  # SHA-256 digest. Keep the declared width exact: SQLite does not enforce a
  # VARCHAR length, but PostgreSQL does.
  activation_id = Column(String(75), nullable=False, index=True)
  parent_agent_id = Column(String(70), nullable=True, index=True)
  parent_activation_id = Column(String(75), nullable=True, index=True)
  parent_kind = Column(String(16), nullable=False, default="unknown")
  parent_source_id = Column(String(160), nullable=True)
  event_type = Column(String(32), nullable=False)
  state = Column(String(16), nullable=False)
  agent_type = Column(String(64), nullable=True)
  summary = Column(Text, nullable=True)
  occurred_at = Column(DateTime, nullable=True)
  observed_at = Column(DateTime, nullable=False, default=lambda: now_naive_utc())
  time_quality = Column(String(16), nullable=False, default="observed")
  source = Column(String(32), nullable=False, default="runner")
  source_event_id = Column(String(160), nullable=True)


class AgentLifecycleRunUpdate(Base):
  """Append-only cursor stream of root ChatRun snapshots for Workflows.

  A helper event cursor cannot reveal a later root-run status change, while
  returning every historical run on each poll is unbounded. This companion
  stream gives those changes their own never-reused incremental cursor. Its
  run id is deliberately not an FK: a final ``deleted`` tombstone must outlive
  rollback of a speculative ChatRun so consumers can remove the prior snapshot.
  """

  __tablename__ = "agent_lifecycle_run_updates"
  __table_args__ = {"sqlite_autoincrement": True}

  id = Column(Integer, primary_key=True, autoincrement=True)
  chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=False, index=True
  )
  chat_run_id = Column(
    String(64), nullable=False, index=True
  )
  provider = Column(String(32), nullable=True)
  status = Column(String(16), nullable=False)
  started_at = Column(DateTime, nullable=True)
  ended_at = Column(DateTime, nullable=True)
  observed_at = Column(DateTime, nullable=False, default=lambda: now_naive_utc())


def _append_agent_lifecycle_run_update(_mapper, connection, run) -> None:
  """Record every inserted/updated ChatRun snapshot in the same transaction."""
  connection.execute(AgentLifecycleRunUpdate.__table__.insert().values(
    chat_id=run.chat_id,
    chat_run_id=run.id,
    provider=run.provider,
    status=run.status,
    started_at=run.started_at,
    ended_at=run.ended_at,
    observed_at=now_naive_utc(),
  ))


def _append_agent_lifecycle_run_tombstone(_mapper, connection, run) -> None:
  """Keep cursor consumers honest when a speculative ChatRun is rolled back."""
  connection.execute(AgentLifecycleRunUpdate.__table__.insert().values(
    chat_id=run.chat_id,
    chat_run_id=run.id,
    provider=run.provider,
    status="deleted",
    started_at=run.started_at,
    ended_at=run.ended_at or now_naive_utc(),
    observed_at=now_naive_utc(),
  ))


event.listen(ChatRun, "after_insert", _append_agent_lifecycle_run_update)
event.listen(ChatRun, "after_update", _append_agent_lifecycle_run_update)
event.listen(ChatRun, "before_delete", _append_agent_lifecycle_run_tombstone)


class ChatEmbedGrant(Base):
  """One-time bootstrap grant and its revocable embedded-chat session.

  The browser receives the random grant secret once; only its SHA-256 digest is
  stored here. Exchange atomically stamps ``consumed_at`` and ``session_id``,
  closing bootstrap replay. The short-lived session JWT points back to this row
  so revocation/expiry and the live app/chat bindings are enforced on every
  request instead of trusting browser frame metadata.

  This is a new table, so ``create_all`` creates it on existing installations
  without an ALTER migration.
  """

  __tablename__ = "chat_embed_grants"

  # Monotonic creation order is security-relevant for refresh handoff: a slow
  # older exchange must never supersede a newer successfully exchanged grant.
  id = Column(Integer, primary_key=True, autoincrement=True)
  token_hash = Column(String(64), nullable=False, unique=True, index=True)
  app_id = Column(Integer, ForeignKey("apps.id"), nullable=False, index=True)
  app_nonce = Column(String(64), nullable=False)
  chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=False, index=True
  )
  instance_id = Column(String(160), nullable=False, index=True)
  owner_epoch = Column(Integer, nullable=False)
  role = Column(String(32), nullable=False, default="participant")
  operations_json = Column(JSON, nullable=False, default=list)
  created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
  expires_at = Column(DateTime, nullable=False, index=True)
  consumed_at = Column(DateTime, nullable=True, default=None)
  session_id = Column(String(64), nullable=True, unique=True, index=True)
  session_expires_at = Column(DateTime, nullable=True, default=None, index=True)
  revoked_at = Column(DateTime, nullable=True, default=None, index=True)


class InstallPassGrant(Base):
  """Opaque, one-use bridge into an iOS Home Screen app.

  Only a SHA-256 digest of the browser-visible random secret is stored. The
  row binds that secret to one app and owner epoch; redemption atomically
  stamps ``consumed_at`` before a fresh short session is minted. A restart
  therefore cannot make a spent pass usable again.

  This is a new table, so ``create_all`` adds it to existing installations.
  """

  __tablename__ = "install_pass_grants"

  id = Column(Integer, primary_key=True, autoincrement=True)
  token_hash = Column(String(64), nullable=False, unique=True, index=True)
  app_id = Column(Integer, ForeignKey("apps.id"), nullable=False, index=True)
  owner_epoch = Column(Integer, nullable=False)
  created_at = Column(DateTime, nullable=False, default=now_naive_utc)
  expires_at = Column(DateTime, nullable=False, index=True)
  consumed_at = Column(DateTime, nullable=True, default=None, index=True)


class App(Base):
  """A mini-app created and managed by the agent."""

  __tablename__ = "apps"
  __table_args__ = (
    CheckConstraint(
      "length(trim(slug)) > 0", name="ck_apps_slug_nonempty",
    ),
    CheckConstraint(
      "length(trim(source_dir)) > 0", name="ck_apps_source_dir_nonempty",
    ),
  )

  id = Column(Integer, primary_key=True, index=True)
  name = Column(String(128), nullable=False)
  description = Column(Text, nullable=False, default="")
  jsx_source = Column(Text, nullable=False, default="")
  compiled_path = Column(String(512), nullable=False, default="")
  # URL slug for the public standalone surface at /apps/<slug>/. Unique
  # across apps. Derived from `name` at creation time via the same
  # slugify rule as `source_dir`, with a numeric suffix on collision
  # (e.g. `snake-2`) so a user creating two apps with the same name
  # doesn't get a unique-constraint failure. Stable across renames —
  # the slug pins the install identity (manifest `id`), and changing
  # it after a user has installed the standalone PWA would orphan
  # their home-screen icon.
  slug = Column(String(128), nullable=False, unique=True, index=True)
  # Per-app secret stamped into every app-scoped token at mint and
  # verified on each request (deps._enforce_app_scope). It rotates with
  # the row: a freshly-created app gets a fresh random nonce, so a token
  # minted for a DELETED app can't authenticate against a DIFFERENT app
  # that later reused its SQLite integer id (which `INTEGER PRIMARY KEY`
  # does, lacking AUTOINCREMENT). Nullable so the additive migration can
  # backfill existing rows; tokens minted before the `app_nonce` claim
  # existed fall back to row-existence only.
  token_nonce = Column(
    String(32), nullable=True, default=lambda: secrets.token_hex(16)
  )
  # URL the app was installed from (manifest URL passed to
  # POST /api/apps/install). Null for user-built apps that didn't
  # come through the install endpoint. The install endpoint matches
  # by this for update-vs-install discrimination — slug collisions
  # between user-built apps and store-installed apps are tolerated
  # because allocate_unique_slug just picks the next free suffix.
  manifest_url = Column(String(1024), nullable=True, index=True)
  # Public manifest URL the owner explicitly attached for sharing this app.
  # Kept separate from `manifest_url`: the latter is install/update identity,
  # while a locally-built app may be published later without becoming a
  # Store-managed install or changing how its source updates are reconciled.
  share_manifest_url = Column(String(1024), nullable=True, default=None)
  # Soft-delete tombstone. Uninstall sets this instead of dropping the row, so
  # the source tree AND the id-keyed runtime storage tree survive — a reinstall
  # (matched by manifest_url) or POST /{id}/recover then revives the SAME id +
  # data instead of orphaning it under a freed integer id. Mirrors
  # Chat.deleted_at; hard-purged after APP_SOFT_DELETE_TTL. See feature 110.
  deleted_at = Column(DateTime, nullable=True, default=None)
  # The manifest's declared version that is currently installed (e.g.
  # "1.7.0"). Stamped on every clean install/update from the manifest;
  # left unchanged on a per-app-git conflict (the served code stays at
  # the old version). Null for user-built apps that never came through
  # the install endpoint, and for rows installed before this column
  # existed (they backfill on their next update). Exposed in AppOut so
  # the store reads the installed version authoritatively rather than
  # from a private side-map it can only populate for its own installs —
  # which is what made out-of-band installs read as "version unknown".
  version = Column(String(32), nullable=True, default=None)
  # Optional manifest-declared standalone PWA colors. Installed apps can
  # declare these in mobius.json so the OS splash/status bar and the
  # standalone loading shell match the app body instead of guessing from the
  # icon. Null falls back to the legacy icon-derived color.
  theme_color = Column(String(16), nullable=True, default=None)
  background_color = Column(String(16), nullable=True, default=None)
  # Optional manifest-declared PWA display mode (web-manifest `display`:
  # "standalone" | "fullscreen" | "minimal-ui" | "browser"). Drives the
  # served per-app manifest's `display`. Null falls back to "standalone".
  # A game declares "fullscreen" so the installed PWA launches with no OS
  # status bar and paints under the phone notch/cutout.
  display = Column(String(16), nullable=True, default=None)
  # Accepted package icon normalized from the manifest declaration.
  icon_png = Column(LargeBinary, nullable=True, default=None)
  # Owner-chosen home-screen artwork is an explicit override, not a second
  # writer racing the manifest-owned package icon.
  icon_override_png = Column(LargeBinary, nullable=True, default=None)
  # Lightweight response projection: advertise a canonical icon reference
  # without hydrating either blob into drawer/catalog queries.
  has_icon = column_property(or_(
    icon_override_png.isnot(None), icon_png.isnot(None),
  ))

  @property
  def effective_icon_png(self) -> bytes | None:
    """The owner override when present, otherwise the accepted package icon."""
    return (
      self.icon_override_png
      if self.icon_override_png is not None
      else self.icon_png
    )
  # Absolute directory holding this app's source files. Editable app source lives
  # under `/data/apps/<dirname>`. Stored explicitly so source apply can map a
  # directory back to its DB row without slugify-guessing the name.
  source_dir = Column(String(512), nullable=False, unique=True, index=True)
  # Chat that last created or modified this app.  Null for apps created
  # before this column was added.  Used to route app errors back to the
  # correct chat so the agent can fix them.
  chat_id = Column(String(64), nullable=True, default=None)
  # See `Chat.pinned_at` — same contract.
  pinned_at = Column(DateTime, nullable=True, default=None)
  # Subject-side: what THIS app's token can do against OTHER apps'
  # storage. The primary direction — designed for the threat model
  # "one mini-app is compromised, what stops it from reading every
  # other app's data". An app's outbound reach defaults to 'none';
  # the agent opts an app in to interop when the partner asks for it.
  #   'none'  (default) — cannot touch other apps
  #   'read'  — can GET from other apps; PUT/DELETE 403
  #   'write' — can GET/PUT/DELETE on other apps
  cross_app_access = Column(
    String(16), nullable=False, default="none"
  )
  # Object-side: what other apps can do against THIS app's storage.
  # Defense-in-depth on top of cross_app_access. The effective right
  # to (read|write) app B from app A's token is
  #     min(A.cross_app_access, B.share_with_apps)
  # — both sides must permit. If either is 'none', access is denied.
  # Owner tokens skip both checks; own-app tokens skip both.
  share_with_apps = Column(
    String(16), nullable=False, default="none"
  )
  # Install authority. When True, the app's token can call POST
  # /api/apps/install + DELETE /api/apps/{id} on the owner's behalf.
  # Distinct from `cross_app_access` so the install consent is a
  # separate user-visible permission rather than overloaded onto
  # storage-write. The App Store mini-app is the canonical caller.
  # Default False — only granted by manifest declaration on install.
  manage_apps = Column(Boolean, nullable=False, default=False)
  # Skills-management access. When True, the app's token can call the
  # /api/skills surface (install a skill from an online source, uninstall an
  # installed one) on the owner's behalf. Distinct from manage_apps so the
  # skills-install consent is its own user-visible permission. The Skills
  # mini-app is the canonical caller. Default False — only granted by manifest
  # declaration on install.
  manage_skills = Column(Boolean, nullable=False, default=False)
  # GitHub data access. This covers the read-only proxy and the narrow reviewed
  # contribution submit surface, never credential management or token export.
  github_access = Column(Boolean, nullable=False, default=False)
  # GitHub credential-management authority. Device flow, PAT install, status,
  # and disconnect are intentionally separate from github_access so read-only
  # consumers cannot mutate the owner's account connection.
  github_connect = Column(Boolean, nullable=False, default=False)
  # Owner filesystem capability. This is intentionally separate from storage
  # interop: it grants the app-scoped token access to the guarded /api/fs
  # surface (still path-confined and secret-denied there). The Editor is the
  # canonical holder. Default false and checked from the live row per request.
  filesystem_access = Column(Boolean, nullable=False, default=False)
  # Connection-registry management: the owner's /api/connectors surface
  # (list/add/re-check/toggle/remove). The Connections mini-app is the
  # canonical holder. Stored keys and broker capabilities never cross this
  # surface, so the grant manages rows without holding what they protect.
  connections_manage = Column(Boolean, nullable=False, default=False)
  # Offline capability. The agent opts an app in (default False) only
  # when it's built to run without the network — it uses
  # window.mobius.storage (which queues writes and syncs on reconnect)
  # and tolerates last-write-wins. This drives client + service-worker
  # caching only; the server does NOT block network use by non-capable
  # apps. The flag is a declaration, not a firewall (design philosophy
  # §4 "code empowers the agent; it does not police it").
  offline_capable = Column(Boolean, nullable=False, default=False)
  # Declared in the manifest as `embeds_agent`: the app mounts the agent
  # chat inside itself (e.g. LaTeX, Workout, the Editor). Purely informational
  # — the store + drawer surface a small "agent" badge so the owner knows
  # which apps drive a sub-agent. Not a permission.
  embeds_agent = Column(Boolean, nullable=False, default=False)
  # Chat-log read tier this app's token may request against
  # GET /api/chat-logs. Read at request time (not baked into the JWT)
  # so flipping it revokes access on the very next request — the
  # Settings "Data access" revoke is a column flip, not a token
  # rotation.
  #   'none'    (default) — GET /api/chat-logs returns 403 for this app
  #   'summary' — whitelisted {role, text} per chat, server-side
  #               structurally redacted (tool/thinking/question/error
  #               blocks, attachments, fs-path augmentation, titles all
  #               stripped; surviving text secret-scrubbed). "Reduced
  #               exposure," not "safe" — regex can't catch pasted
  #               documents or encoded secrets.
  #   'summary_with_deleted' — the same structural redaction widened only to
  #               chats still inside the seven-day recovery window.
  # App frames receive only their scoped JWT and run in opaque-origin
  # sandboxes, so this live-row permission is an enforceable boundary in
  # addition to recording owner consent.
  chat_log_access = Column(
    String(24), nullable=False, default="none"
  )
  # Per-app git model: `upstream_commit` is the sha of the last
  # pristine-manifest commit on the app's `upstream` branch — the merge
  # base an update diverges from. Null for an app with no tracked source
  # dir (it never enters the git path).
  upstream_commit = Column(String(64), nullable=True, default=None)
  # Exact local `main` commit selected by the durable App row. This closes the
  # Git/SQLite recovery boundary for explicit apply: a boot-time bundle rebuild
  # can compile the accepted tree without reading or rewriting a newer draft in
  # the editable worktree. Null for legacy rows until their next successful
  # install or explicit apply.
  source_commit = Column(String(64), nullable=True, default=None)
  # Owner-visible update-conflict resolver chats are keyed on upstream_commit.
  conflict_resolver_chat_id = Column(String(64), nullable=True, default=None)
  conflict_resolver_upstream_commit = Column(
    String(64), nullable=True, default=None
  )
  # Stopgap divergence marker (old finding #2): the sha256 of the
  # upstream entry JSX as last installed/updated. Lets the update path
  # cheaply tell "did the on-disk index.jsx diverge from what upstream
  # shipped" without a full repo, and survives even when the git model
  # is off. Null until the first flagged install/update sets it.
  upstream_jsx_sha = Column(String(64), nullable=True, default=None)
  # Offline contract declared in the manifest's `offline` block (P1-D).
  # Stored as JSON; None when no block was declared. Schema only — informational
  # for the agent and SW; no server-side enforcement. Example shape:
  #   {"reads": true, "writes": "queued", "execution": "full", "precache": []}
  offline_contract = Column(JSON, nullable=True, default=None)
  # Optional root-level markdown file contributed to new chat prompt snapshots.
  # Only live installed rows are composed at chat start. Soft-uninstall changes
  # future chats while existing snapshots and app data remain recoverable.
  system_prompt_file = Column(String(255), nullable=True, default=None)
  # Explicit manifest identity for apps that participate in the agent/system
  # lifecycle.  This flag grants nothing by itself; the individual manifest
  # declarations remain the capabilities and the install review is consent.
  system_app = Column(Boolean, nullable=False, default=False)
  # Server-derived, versioned capability contract reviewed at install time.
  # Null is a legitimate legacy state for apps installed before contracts.
  capability_contract = Column(JSON, nullable=True, default=None)
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))
  updated_at = Column(
    DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
  )


class AppActivityState(Base):
  """Durable unread-activity marker for one installed app.

  This deliberately lives outside ``apps``: acknowledging a report must not
  advance ``App.updated_at``, which is the shell's executable-bundle cache key.
  Notifications remain the detailed history; the drawer only needs one compact
  unread/read row per app.
  """

  __tablename__ = "app_activity_state"

  app_id = Column(Integer, ForeignKey("apps.id"), primary_key=True)
  activity_at = Column(DateTime, nullable=False, default=lambda: now_naive_utc())
  activity_version = Column(Integer, nullable=False, default=1, server_default="1")
  unseen = Column(Boolean, nullable=False, default=True, server_default=true())


class AppRecencyState(Base):
  """Durable last-opened timestamp for one installed app.

  This stays outside ``apps`` so opening an app does not advance
  ``App.updated_at``, which is the executable-bundle cache key.
  """

  __tablename__ = "app_recency_state"

  app_id = Column(Integer, ForeignKey("apps.id"), primary_key=True)
  last_opened_at = Column(
    DateTime, nullable=False, default=lambda: now_naive_utc()
  )


class AppPreviewState(Base):
  """Durable acknowledgement of the exact app build opened from its chat CTA.

  This is separate from ``apps`` so acknowledging a preview never advances
  ``App.updated_at`` — the executable-bundle version the acknowledgement is
  meant to record. ``seen_as_final`` distinguishes opening a live preview from
  opening the settled result: finishing the turn may surface the same build one
  last time even when no final source write was needed.
  """

  __tablename__ = "app_preview_state"

  app_id = Column(Integer, ForeignKey("apps.id"), primary_key=True)
  seen_updated_at = Column(DateTime, nullable=False)
  seen_as_final = Column(
    Boolean, nullable=False, default=False, server_default=false()
  )


class PushSubscription(Base):
  """Browser push subscription for Web Push delivery."""

  __tablename__ = "push_subscriptions"

  id = Column(String(64), primary_key=True)
  owner_id = Column(Integer, ForeignKey("owner.id"), nullable=False)
  endpoint = Column(Text, nullable=False, unique=True)
  p256dh = Column(Text, nullable=False)
  auth = Column(Text, nullable=False)
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class Notification(Base):
  """Record of a sent push notification."""

  __tablename__ = "notifications"

  id = Column(String(64), primary_key=True)
  owner_id = Column(Integer, ForeignKey("owner.id"), nullable=False)
  source_type = Column(String(16), nullable=False)
  source_id = Column(String(64), nullable=True)
  title = Column(String(256), nullable=False)
  body = Column(Text, nullable=True)
  icon = Column(Text, nullable=True)
  target = Column(Text, nullable=True)
  actions = Column(JSON, nullable=True)
  sent_at = Column(DateTime, default=lambda: datetime.now(UTC))
  clicked_at = Column(DateTime, nullable=True)
  # Seen via the in-app notification preview (bulk-stamped by read-all).
  # Distinct from clicked_at, which records a tap on the OS push itself —
  # bulk-marking THAT would fabricate click data.
  read_at = Column(DateTime, nullable=True)


class ToolOutput(Base):
  """Full text of a large tool result, stored compactly out-of-band.

  The chat transcript blob (`Chat.messages`) and the live / catch-up event
  stream carry only a bounded head+tail excerpt of a big tool output; the full
  text lives here, keyed by the tool's stable identity, and `ToolBlock` fetches
  it lazily on expand via GET /api/chats/{chat_id}/tool-output/{tool_use_id}.

  Why a table, not a file: `db/` (ultimate.db) is gitignored, so these blobs
  are correctly EXCLUDED from the nightly `/data` git safety-net (we do not want
  megabytes of tool output versioned every night), and the rows ride the chat
  lifecycle for free — soft-delete keeps them (a recovered chat re-shows its
  outputs), the hard-purge sweep drops them with their chat. Written via the
  single-writer actor's `StashToolOutput` command as an insert/upsert on the
  composite PK (race-immune; see chat_writer.py). The TEXT payload is a
  self-describing compressed frame; the read boundary also accepts legacy
  plain text while a bounded background fix-forward updates old rows. Keeping
  one column preserves SQLite/PostgreSQL portability without a schema migration.
  `create_all` builds this table on the next boot — a new table needs no ALTER
  migration (see run_migrations, which only ALTERs existing tables)."""

  __tablename__ = "tool_outputs"

  chat_id = Column(
    String(64), ForeignKey("chats.id"), primary_key=True, index=True
  )
  # The tool_use_id (Claude) / ThreadItem id (Codex) — stable emit→read and
  # unique within the chat, which is all the composite PK needs.
  tool_use_id = Column(String(128), primary_key=True)
  output = Column(CompressedToolOutputText(), nullable=False, default="")
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class Connector(Base):
  """Owner-managed remote MCP endpoint shared by both agent providers."""

  __tablename__ = "connectors"

  id = Column(Integer, primary_key=True, index=True)
  # Stable authorization identity. SQLite may reuse an INTEGER PRIMARY KEY
  # after deletion, so broker capabilities must never authorize by ``id``
  # alone or an old turn could reach a later connector that reused the row id.
  capability_id = Column(
    String(64), nullable=False, unique=True, index=True,
    default=lambda: secrets.token_hex(32),
  )
  slug = Column(String(64), nullable=False, unique=True, index=True)
  name = Column(String(128), nullable=False)
  url = Column(String(2048), nullable=False)
  auth_header = Column(String(64), nullable=True)
  auth_value_encrypted = Column(Text, nullable=True)
  enabled = Column(Boolean, nullable=False, default=True, server_default=true())
  tools_json = Column(JSON, nullable=False, default=list)
  est_tokens = Column(Integer, nullable=False, default=0, server_default="0")
  status = Column(String(16), nullable=False, default="ok", server_default="ok")
  status_detail = Column(Text, nullable=True)
  created_at = Column(DateTime, default=lambda: now_naive_utc())
  last_checked_at = Column(DateTime, nullable=True)


class ThinkingTrace(Base):
  """Full reasoning text stored outside the bounded chat transcript.

  Thinking blocks at or below the inline threshold remain self-contained.
  Larger runs keep only identity, revision, duration, and completion metadata
  in ``Chat.messages`` / ``live_assistant`` and are fetched when that exact
  nested thought is opened.  A revision is the server-side Python character
  count; it lets a live client ask for at least the version it has observed.
  """

  __tablename__ = "thinking_traces"

  chat_id = Column(
    String(64), ForeignKey("chats.id"), primary_key=True, index=True
  )
  thinking_id = Column(String(128), primary_key=True)
  content = Column(Text, nullable=False, default="")
  revision = Column(Integer, nullable=False, default=0)
  complete = Column(Boolean, nullable=False, default=False)
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class ContributionAutopilot(Base):
  """Platform-owned authorization + claim state for one contribution's autopilot.

  The Contribute mini-app's ledger (``contributions/<id>.json`` in app storage)
  is AGENT-WRITABLE, so nothing there can be trusted as consent or as claim
  truth. This table is the trust anchor: one row per (app_id, record_id), written
  ONLY by platform code — the submit endpoints stamp the grant when the owner
  clicks Send, and the autopilot endpoints move the claim/round state. A forged
  ``autopilot`` block in the ledger changes nothing; every enforcement path reads
  this row. The ledger block is a one-way MIRROR of these fields for the UI/cron.

  ``create_all`` builds this table on the next boot — a new table needs no ALTER
  migration (see ``database.run_migrations``, which only ALTERs existing tables).
  """

  __tablename__ = "contribution_autopilot"

  # (app_id, record_id) — the same identity the ledger file uses, but held here
  # where only the platform can write it. record_id is the ledger record's `id`.
  app_id = Column(
    Integer, ForeignKey("apps.id"), primary_key=True
  )
  record_id = Column(String(128), primary_key=True)
  # The grant: True once Send stamped consent, flipped False by an owner Pause.
  # Absent row = classic manual flow; this is the only authorization autopilot
  # ever consults.
  enabled = Column(Boolean, nullable=False, default=True)
  granted_at = Column(DateTime, nullable=True, default=None)
  # The reviewed head the grant was issued against — a re-send refreshes it.
  granted_head_sha = Column(String(64), nullable=True, default=None)
  # Immutable public/local target stamped from the successful owner-approved
  # submission. The contribution ledger is agent-writable, so follow-up routes
  # must never recover these authority-bearing values from it.
  target_repo = Column(String(256), nullable=True, default=None)
  target_pr_number = Column(Integer, nullable=True, default=None)
  target_head_repository = Column(String(256), nullable=True, default=None)
  target_branch = Column(String(256), nullable=True, default=None)
  target_repo_path = Column(String(1024), nullable=True, default=None)
  # "idle" between rounds; "responding" while a round holds the claim.
  state = Column(String(16), nullable=False, default="idle")
  # The live claim. run_id is a fresh uuid per round and is the round's whole
  # identity: /update, /reply, /complete, /escalate require the caller to
  # present it, so a zombie agent from a reclaimed round holds a dead id.
  run_id = Column(String(64), nullable=True, default=None)
  attention_key = Column(String(256), nullable=True, default=None)
  # Canonical timestamp copied from the claimed attention. Completion advances
  # the cursor to this value; an agent cannot choose its own future cursor.
  claimed_event_at = Column(String(40), nullable=True, default=None)
  # Successful server-mediated actions recorded during this claim. Completion
  # derives productivity from these fields instead of trusting agent prose.
  round_action = Column(String(16), nullable=True, default=None)
  round_head_sha = Column(String(64), nullable=True, default=None)
  # Exact GitHub URLs returned by recent server-mediated replies. Mirrored for
  # the scheduler so it ignores only Autopilot's own activity—not every comment
  # written by the owner's GitHub account.
  ignored_event_urls_json = Column(JSON, nullable=True, default=None)
  claimed_at = Column(DateTime, nullable=True, default=None)
  lease_expires_at = Column(DateTime, nullable=True, default=None)
  # The dedicated owner-visible chat where every round for this record runs.
  followup_chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=True, default=None
  )
  rounds_used = Column(Integer, nullable=False, default=0)
  max_rounds = Column(Integer, nullable=False, default=5)
  # Consecutive non-productive rounds (stale/failed); 2 in a row auto-escalates.
  consecutive_failures = Column(Integer, nullable=False, default=0)
  # Cursor: attention whose event timestamp is <= this is already settled, so a
  # re-posted event (incl. the agent's own reply seen by the next cron pass)
  # cannot re-trigger. Stored as the ISO string the ledger/GraphQL use.
  last_handled_event_at = Column(String(40), nullable=True, default=None)
  last_handled_attention_key = Column(String(256), nullable=True, default=None)
  # Capped audit log (newest last, trimmed to 30 entries): each entry is
  # {attention_key, run_id, started_at, finished_at, outcome, summary, head_sha}.
  rounds_json = Column(JSON, nullable=True, default=None)
  created_at = Column(DateTime, default=lambda: now_naive_utc())
  updated_at = Column(DateTime, default=lambda: now_naive_utc())
