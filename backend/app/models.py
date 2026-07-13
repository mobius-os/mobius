"""SQLAlchemy ORM models.

FROZEN at runtime (chmod 444 root-owned per protected-files.txt).
main.py and many route modules import these at module load; if I'm
broken the server can't boot and /recover/chat is unreachable.

To add a column to an existing table: edit me on the host repo and
rebuild. For per-chat fields you can usually skip a migration by
adding to `Chat.agent_settings_json` (a JSON column intentionally
included as the no-migration escape hatch). For app-scoped data
you'd otherwise add a column for, use per-app storage at
`/data/apps/<app_id>/...` via the storage API.
"""

import secrets
from datetime import UTC, datetime

from sqlalchemy import (
  Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, LargeBinary,
  String, Text,
)

from app.database import Base


class Owner(Base):
  """Single owner account for this installation."""

  __tablename__ = "owner"

  id = Column(Integer, primary_key=True)
  username = Column(String(64), nullable=False, unique=True)
  hashed_password = Column(String(255), nullable=False)
  gemini_api_key_enc = Column(Text, nullable=True, default=None)
  # Must stay in sync with providers.PROVIDER_NAMES.
  provider = Column(String(32), nullable=False, default="claude")
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
  # once without rotating SECRET_KEY (which would also break the
  # Fernet-encrypted API keys and the CLI credential derivation). A
  # token minted before this column existed carries no epoch claim and
  # reads as epoch 0, which equals a freshly-migrated owner's epoch, so
  # legacy tokens stay valid until the first bump.
  token_epoch = Column(Integer, nullable=False, default=0)
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
  pending_messages = Column(JSON, nullable=False, default=list)
  uploads = Column(JSON, nullable=False, default=list)
  generated_images = Column(JSON, nullable=False, default=list)
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
  # Vestigial: the named-agent feature was removed; column retained
  # nullable to avoid a prod migration. Nothing reads or writes it.
  agent_id = Column(String(64), nullable=True, default=None)
  # Drawer pinning: NOT NULL = pinned, NULL = unpinned. Sort key for
  # the chats list — pinned rows render first, ordered by this
  # column DESC (newest pin at top of pinned group). PATCH
  # /api/chats/{id} accepts `pinned: bool` to toggle.
  pinned_at = Column(DateTime, nullable=True, default=None)
  # Crash-recovery run marker. "running" while a turn is in flight,
  # NULL otherwise. The runner registry holds the same truth in
  # memory; this column is the DURABLE copy that survives an OOM /
  # SIGKILL. On the next process start, lifespan reconciliation
  # (chat.reconcile_interrupted_chats) finds any row still marked
  # "running" — the in-memory registry is always empty at boot, so
  # such a row is by definition a turn the dead process never
  # finished — and resolves it (finalize the transcript, clear the
  # marker, drop stranded pending_messages) instead of stranding the
  # chat "running" forever in the user's view. Set at the top of
  # chat._run_chat_impl, cleared in chat.run_chat's finally under the
  # same generation-ownership guard that releases the _starting claim.
  run_status = Column(String(16), nullable=True, default=None)
  # When the in-flight turn started (UTC). Set alongside run_status;
  # cleared to NULL when the turn ends. Not consulted by the startup
  # reconciliation decision (run_status alone is sufficient — a boot
  # with an empty registry makes every "running" row stale regardless
  # of age) but kept for observability: the reconciliation log line
  # reports how long the interrupted turn had been running, and a
  # future liveness probe can read it without another migration.
  run_started_at = Column(DateTime, nullable=True, default=None)
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
  # ordering key, deliberately decoupled from `updated_at` — which bumps on
  # EVERY row write via onupdate (run markers, session id, streamed
  # transcript, the agent's auto-retitle) and would otherwise re-sort the
  # chat to the top on activity the owner did not initiate. No onupdate here.
  activity_at = Column(
    DateTime, nullable=True, default=lambda: datetime.now(UTC)
  )


class ChatRun(Base):
  """Durable per-turn run record (persistence redesign 077 Step 3).

  One row per turn, keyed by the in-memory run_token (which IS this row's
  `id` — the same identity the actor commands and the sink already carry, not
  a second one). This is the per-run successor to the single `Chat.run_status`
  column: a row left ``status == "running"`` by a process that died is an
  interrupted turn that boot reconciliation resolves. It also carries the
  per-run attribution one shared column never could (provider, cost, the
  initiating app), which the app-attributed-chat contract (077 §1) and the
  redacted chat-log read API (Capability B) build on.

  Transitional dual-write: `Chat.run_status` is still set/cleared in lockstep
  with this row (in the same actor commit) for one deploy cycle, so a rollback
  to pre-Step-3 code keeps recovering, and so reconciliation still catches a
  turn that was in flight ACROSS the deploy (started under old code, with no
  `chat_runs` row). Reconciliation reads the UNION of both signals during the
  transition; retiring the `run_status` column is the Step-3b follow-up (along
  with collapsing the in-memory generation onto this same run identity, which
  is what finally closes 080 item 4's split source of truth).

  `create_all` builds this table on the next boot (a new table, so no ALTER
  migration is needed — see `run_migrations`, which only ALTERs existing
  tables); existing rows are untouched.
  """

  __tablename__ = "chat_runs"

  # The run_token, verbatim — one durable identity for the turn.
  id = Column(String(64), primary_key=True)
  chat_id = Column(
    String(64), ForeignKey("chats.id"), nullable=False, index=True
  )
  # "running" while in flight, "completed" on a clean turn end, "interrupted"
  # when boot reconciliation resolves a turn whose process died mid-flight.
  status = Column(String(16), nullable=False, default="running", index=True)
  provider = Column(String(32), nullable=True, default=None)
  # App that initiated this turn under the app-attributed-chat contract
  # (077 §1). NULL = an ordinary owner-driven turn. Reserved now so the
  # attribution lands on the run row, not retrofitted later.
  initiated_by_app_id = Column(
    Integer, ForeignKey("apps.id"), nullable=True, default=None
  )
  # Reserved for per-run cost attribution (Capability B). No code path writes
  # this yet, so reads are NULL until the Step-3b follow-up wires a producer.
  cost_usd = Column(Float, nullable=True, default=None)
  started_at = Column(DateTime, default=lambda: datetime.now(UTC))
  ended_at = Column(DateTime, nullable=True, default=None)
  # Provider rate/usage-limit parking (design §2.4). When a turn dies on a
  # provider limit, the run is PARKED instead of just cleared: `status` moves
  # to "parked", `parked_until` holds the reset time (naive UTC, matching every
  # other DateTime here), and `park_reason` a short label ("rate_limit" /
  # "usage_limit" / …). This run row IS the provider-parked signal — no
  # separate state enum. The liveness checks read it via
  # `chat._parked_until_for_chat`; the periodic reset sweep notifies once at
  # `parked_until` and moves the row to "parked_notified". Null on every
  # non-parked run and on rows created before this column existed.
  parked_until = Column(DateTime, nullable=True, default=None)
  park_reason = Column(String(32), nullable=True, default=None)


class App(Base):
  """A mini-app created and managed by the agent."""

  __tablename__ = "apps"

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
  slug = Column(String(128), nullable=True, unique=True, index=True)
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
  # User-uploaded icon for the standalone PWA install (PNG bytes).
  # Null means fall back to the auto-generated default (first letter
  # of `name` on a deterministic color). Stored inline because icons
  # are small (~10-50KB at 512x512) and per-app — avoids needing a
  # separate file store + cleanup path.
  icon_png = Column(LargeBinary, nullable=True, default=None)
  # Absolute directory holding this app's source files. Editable app source lives
  # under `/data/apps/<dirname>`. Stored explicitly so the file watcher can map a
  # modified `index.jsx` back to its DB row without slugify-guessing the name.
  # Null for apps created before this column existed.
  source_dir = Column(String(512), nullable=True, default=None)
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
  # GitHub connection access. When True, the app's token can call the
  # whole /api/github surface: manage the connection (connect / poll /
  # disconnect / status) and use the read-only data proxy (GET
  # /api/github/api/* and POST /api/github/graphql, both read-only by
  # construction, INV2). The connected token is never returned to the
  # app. The Contribute mini-app is the canonical caller. A boolean gate
  # like manage_apps, not a ladder. Default False — only granted by
  # manifest declaration on install.
  github_access = Column(Boolean, nullable=False, default=False)
  # Owner filesystem capability. This is intentionally separate from storage
  # interop: it grants the app-scoped token access to the guarded /api/fs
  # surface (still path-confined and secret-denied there). The Editor is the
  # canonical holder. Default false and checked from the live row per request.
  filesystem_access = Column(Boolean, nullable=False, default=False)
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
  #   'full'    — DEFERRED. Reserved so the column's value space is
  #               stable; the read API rejects it until a concrete
  #               consumer + louder consent lands (design §2).
  # App frames receive only their scoped JWT and run in opaque-origin
  # sandboxes, so this live-row permission is an enforceable boundary in
  # addition to recording owner consent.
  chat_log_access = Column(
    String(16), nullable=False, default="none"
  )
  # Per-app git model: `upstream_commit` is the sha of the last
  # pristine-manifest commit on the app's `upstream` branch — the merge
  # base an update diverges from. Null for an app with no tracked source
  # dir (it never enters the git path).
  upstream_commit = Column(String(64), nullable=True, default=None)
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
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))
  updated_at = Column(
    DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
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


class ToolOutput(Base):
  """Full text of a large tool result, stored out-of-band (contract rule 6).

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
  composite PK (race-immune; see chat_writer.py). `create_all` builds this table
  on the next boot — a new table needs no ALTER migration (see run_migrations,
  which only ALTERs existing tables)."""

  __tablename__ = "tool_outputs"

  chat_id = Column(
    String(64), ForeignKey("chats.id"), primary_key=True, index=True
  )
  # The tool_use_id (Claude) / ThreadItem id (Codex) — stable emit→read and
  # unique within the chat, which is all the composite PK needs.
  tool_use_id = Column(String(128), primary_key=True)
  output = Column(Text, nullable=False, default="")
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))
