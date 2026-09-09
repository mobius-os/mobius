"""Append-only database upgrades and ORM/live-schema verification.

This module owns every operation that changes or validates schema history.
``database.py`` deliberately owns only engine/session plumbing and ``Base``;
keeping the ledger and its historical functions here makes the append-only end
obvious and prevents current schema work from disappearing into boot plumbing.
"""

import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.database import Base


def _agent_lifecycle_width_migrations(
  dialect_name: str, columns: list[dict],
) -> list[str]:
  """Return lossless ALTERs for the original undersized activation ids."""
  if dialect_name != "postgresql":
    # SQLite does not enforce VARCHAR lengths and cannot ALTER a column type in
    # place. Its existing 75-character values are already stored losslessly.
    return []
  by_name = {column["name"]: column for column in columns}
  statements = []
  for column_name in ("activation_id", "parent_activation_id"):
    column = by_name.get(column_name)
    length = getattr(column.get("type"), "length", None) if column else None
    if length is not None and length < 75:
      statements.append(
        "ALTER TABLE agent_lifecycle_events "
        f"ALTER COLUMN {column_name} TYPE VARCHAR(75)"
      )
  return statements


def _upgrade_app_capability_contract(value):
  """Advance known contracts and drop retired job-authority fields."""
  if isinstance(value, str):
    try:
      value = json.loads(value)
    except (TypeError, ValueError):
      return None
  if not isinstance(value, dict):
    return None
  schema = value.get("schema")
  if type(schema) is not int or schema not in (1, 2, 3, 4, 5):
    return None

  upgraded = dict(value)
  changed = schema != 5
  background = value.get("background")
  if isinstance(background, dict):
    next_background = {
      key: item for key, item in background.items()
      if key not in ("agent", "authority")
    }
    if next_background != background:
      upgraded["background"] = next_background
      changed = True
  if not isinstance(value.get("public"), dict):
    upgraded["public"] = {"network": []}
    changed = True
  if not changed:
    return None
  upgraded["schema"] = 5
  return upgraded


def _converge_legacy_schema(eng) -> None:
  """Converge every schema that predates the versioned migration ledger.

  Uses SQLAlchemy's database-agnostic inspector so this works for both
  SQLite and PostgreSQL.  Safe to call on every boot — no-ops if already
  up to date. Production creates missing tables before calling this function,
  which lets migrations move legacy column data into newly introduced tables.
  Direct callers with no application tables still no-op.
  """
  from sqlalchemy import JSON as SAJSON, bindparam, inspect as sa_inspect, text
  inspector = sa_inspect(eng)
  tables = inspector.get_table_names()
  if "apps" not in tables:
    return  # fresh install — create_all handles it
  # Removed built-in image generation left two provider-specific columns on
  # existing installs. Drop them instead of carrying dead schema forever;
  # fresh installs never create them because the ORM no longer declares them.
  if "owner" in tables:
    owner_cols = {c["name"] for c in inspector.get_columns("owner")}
    if "gemini_api_key_enc" in owner_cols:
      with eng.connect() as conn:
        conn.execute(text(
          "ALTER TABLE owner DROP COLUMN gemini_api_key_enc"
        ))
        conn.commit()
  if "chats" in tables:
    chats_cols = {c["name"] for c in inspector.get_columns("chats")}
    if "generated_images" in chats_cols:
      with eng.connect() as conn:
        conn.execute(text(
          "ALTER TABLE chats DROP COLUMN generated_images"
        ))
        conn.commit()
  # Retire the pre-ChatRun per-chat marker without losing an interrupted turn
  # across the upgrade. ``main._init_db`` creates ``chat_runs`` first, then this
  # migration copies every still-running legacy marker that lacks a durable run
  # identity. Only after the recovery handle exists do we drop both old
  # columns. Each insert is independently idempotent by the NOT EXISTS query,
  # and each DROP is independently schema-gated for crash-safe retries.
  if "chats" in tables and "chat_runs" in tables:
    chats_cols = {c["name"] for c in inspector.get_columns("chats")}
    if "run_status" in chats_cols:
      provider_expr = "c.provider" if "provider" in chats_cols else "NULL"
      started_expr = (
        "c.run_started_at" if "run_started_at" in chats_cols else "NULL"
      )
      with eng.connect() as conn:
        legacy = conn.execute(text(
          f"SELECT c.id, {provider_expr}, {started_expr} "
          "FROM chats c "
          "WHERE c.run_status = 'running' "
          "AND NOT EXISTS ("
          "SELECT 1 FROM chat_runs r "
          "WHERE r.chat_id = c.id AND r.status = 'running'"
          ")"
        )).all()
        insert_run = text(
          "INSERT INTO chat_runs "
          "(id, chat_id, status, provider, started_at) "
          "VALUES (:id, :chat_id, 'running', :provider, :started_at)"
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        for chat_id, provider, started_at in legacy:
          conn.execute(insert_run, {
            "id": str(uuid.uuid4()),
            "chat_id": chat_id,
            "provider": provider,
            "started_at": started_at or now,
          })
        conn.commit()
      with eng.connect() as conn:
        conn.execute(text("ALTER TABLE chats DROP COLUMN run_status"))
        conn.commit()
      chats_cols.remove("run_status")
    if "run_started_at" in chats_cols:
      with eng.connect() as conn:
        conn.execute(text("ALTER TABLE chats DROP COLUMN run_started_at"))
        conn.commit()
  apps_cols = {c["name"] for c in inspector.get_columns("apps")}
  if "chats" in tables:
    chats_cols = {c["name"] for c in inspector.get_columns("chats")}
    if "title_locked" not in chats_cols:
      with eng.connect() as conn:
        conn.execute(text(
          "ALTER TABLE chats ADD COLUMN title_locked BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.commit()
    if "live_assistant" not in chats_cols:
      with eng.connect() as conn:
        conn.execute(text(
          "ALTER TABLE chats ADD COLUMN live_assistant JSON NULL"
        ))
        conn.commit()
  if "chat_id" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN chat_id VARCHAR(64) NULL"))
      conn.commit()
  if "source_dir" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN source_dir VARCHAR(512) NULL"))
      conn.commit()
  if "pinned_at" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN pinned_at DATETIME NULL"))
      conn.commit()
  if "share_with_apps" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN share_with_apps VARCHAR(16) "
        "NOT NULL DEFAULT 'none'"
      ))
      conn.commit()
  if "cross_app_access" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN cross_app_access VARCHAR(16) "
        "NOT NULL DEFAULT 'none'"
      ))
      conn.commit()
  if "offline_capable" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN offline_capable BOOLEAN "
        "NOT NULL DEFAULT FALSE"
      ))
      conn.commit()
  if "manage_apps" not in apps_cols:
    # Install authority — distinct from cross_app_access (storage).
    # Defaults to 0; apps gain authority by declaring
    # permissions.manage_apps=true in their manifest and reinstalling.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN manage_apps BOOLEAN "
        "NOT NULL DEFAULT FALSE"
      ))
      conn.commit()
  if "manage_skills" not in apps_cols:
    # Skills-management authority — gates the /api/skills install/uninstall
    # surface. Defaults to 0; apps gain it by declaring
    # permissions.manage_skills=true in their manifest and reinstalling.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN manage_skills BOOLEAN "
        "NOT NULL DEFAULT FALSE"
      ))
      conn.commit()
  if "github_access" not in apps_cols:
    # GitHub data access — gates the read-only proxy and reviewed contribution
    # submit surface. Connection management has its own stronger grant below.
    # apps gain it by declaring permissions.github_access=true in their
    # manifest and reinstalling.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN github_access BOOLEAN "
        "NOT NULL DEFAULT FALSE"
      ))
      conn.commit()
  if "github_connect" not in apps_cols:
    # GitHub credential-management authority: device flow, PAT install, status,
    # and disconnect. Kept separate so a future read-only GitHub consumer never
    # inherits account mutation merely to inspect public repository state.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN github_connect BOOLEAN "
        "NOT NULL DEFAULT FALSE"
      ))
      conn.commit()
  if "filesystem_access" not in apps_cols:
    # Privileged owner-filesystem capability for the Editor. Existing apps stay
    # denied until reinstalled from a manifest that explicitly requests it.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN filesystem_access BOOLEAN "
        "NOT NULL DEFAULT FALSE"
      ))
      conn.commit()
  if "manifest_url" not in apps_cols:
    # Install identity — see models.App.manifest_url. Nullable for
    # user-built apps; installed apps stamp it on install/update.
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN manifest_url VARCHAR(1024) NULL"))
      conn.commit()
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_apps_manifest_url ON apps (manifest_url)"
    ))
    conn.commit()
  if "share_manifest_url" not in apps_cols:
    # Optional distribution metadata for locally-built apps. This is not
    # install identity: attaching a public manifest must never make the local
    # source tree Store-managed or change update matching.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN share_manifest_url VARCHAR(1024) NULL"
      ))
      conn.commit()
  if "version" not in apps_cols:
    # Installed manifest version — see models.App.version. Nullable;
    # existing rows backfill on their next install/update.
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN version VARCHAR(32) NULL"))
      conn.commit()
  if "embeds_agent" not in apps_cols:
    # The app mounts an embedded agent chat — see models.App.embeds_agent.
    # Existing rows default false; backfill on their next install/update.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN embeds_agent BOOLEAN NOT NULL DEFAULT FALSE"
      ))
      conn.commit()
  if "deleted_at" not in apps_cols:
    # Reversible-uninstall tombstone — see models.App.deleted_at (feature 110).
    # Additive + nullable: every existing row reads deleted_at IS NULL = live,
    # so behavior is byte-identical until an app is actually soft-deleted.
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN deleted_at DATETIME NULL"))
      conn.commit()
  if "theme_color" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN theme_color VARCHAR(16) NULL"))
      conn.commit()
  if "background_color" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN background_color VARCHAR(16) NULL"))
      conn.commit()
  if "display" not in apps_cols:
    # Per-app PWA display mode (web-manifest `display`); see models.App.display.
    # Additive + nullable: every existing row reads display IS NULL, which the
    # manifest serves as "standalone" — byte-identical to prior behavior.
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN display VARCHAR(16) NULL"))
      conn.commit()
  # Slug column: split into three independent idempotent gates so a
  # crash anywhere in the sequence leaves a recoverable state. The
  # previous shape gated the backfill on "column missing", which
  # meant a mid-loop crash would commit the ALTER but skip the
  # backfill+index on every subsequent boot — leaving NULL slugs
  # forever and silently degrading the three-dots menu on every
  # legacy app. Each gate below re-checks its own precondition.
  if "slug" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN slug VARCHAR(128) NULL"
      ))
      conn.commit()
  # Backfill: runs whenever any row has a NULL slug. Idempotent —
  # already-populated rows are filtered out by the WHERE clause and
  # their slugs are read into `taken` so we don't collide with them.
  #
  # The app_identity slug algorithm is intentionally inlined here. Importing
  # application lifecycle code into the frozen baseline migration would let a
  # later app edit prevent the database from booting. The implementation is
  # frozen to this copy; if the live algorithm changes, decide explicitly
  # whether old rows should retain their historical identity.
  def _slugify_for_source_dir(name: str) -> str:
    slug = "".join(
      ch if ch.isalnum() else "-" for ch in (name or "").lower()
    ).strip("-")
    while "--" in slug:
      slug = slug.replace("--", "-")
    slug = slug or "app"
    if slug.isdigit():
      slug = f"app-{slug}"
    return slug

  with eng.connect() as conn:
    null_rows = conn.execute(
      text("SELECT id, name FROM apps WHERE slug IS NULL ORDER BY id")
    ).fetchall()
    if null_rows:
      existing = conn.execute(
        text("SELECT slug FROM apps WHERE slug IS NOT NULL")
      ).fetchall()
      taken: set[str] = {r[0] for r in existing if r[0]}
      for row in null_rows:
        base = _slugify_for_source_dir(row[1])
        candidate = base
        suffix = 2
        while candidate in taken:
          candidate = f"{base}-{suffix}"
          suffix += 1
        taken.add(candidate)
        conn.execute(
          text("UPDATE apps SET slug = :s WHERE id = :i"),
          {"s": candidate, "i": row[0]},
        )
      conn.commit()
  # Unique index: separate gate so a crashed backfill on a prior boot
  # doesn't leave us indexless forever. `IF NOT EXISTS` handles the
  # happy-path re-run case at zero cost.
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE UNIQUE INDEX IF NOT EXISTS ix_apps_slug ON apps (slug)"
    ))
    conn.commit()
  if "icon_png" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN icon_png BLOB NULL"))
      conn.commit()
  if "icon_override_png" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN icon_override_png BLOB NULL"
      ))
      conn.commit()
  if "icon_ownership_split" not in apps_cols:
    # Existing icon_png values predate package/override separation and must be
    # classified from accepted source before either writer may replace them.
    # New ORM-created rows explicitly write TRUE; a raw or interrupted insert
    # remains safely eligible for startup reconciliation.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN icon_ownership_split "
        "BOOLEAN NOT NULL DEFAULT FALSE"
      ))
      conn.commit()
  # Per-app token nonce. Add the column, then backfill
  # any NULL row with a fresh random nonce so existing apps get the same
  # id-reuse protection as new ones. Two independent idempotent gates so a
  # crash between them still converges on the next boot.
  if "token_nonce" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN token_nonce VARCHAR(32) NULL"
      ))
      conn.commit()
  import secrets
  with eng.connect() as conn:
    null_nonce = conn.execute(
      text("SELECT id FROM apps WHERE token_nonce IS NULL")
    ).fetchall()
    for row in null_nonce:
      conn.execute(
        text("UPDATE apps SET token_nonce = :n WHERE id = :i"),
        {"n": secrets.token_hex(16), "i": row[0]},
      )
    if null_nonce:
      conn.commit()
  if "chat_log_access" not in apps_cols:
    # Chat-log read tier gating GET /api/chat-logs.
    # Defaults to 'none'; an app gains read access by declaring
    # permissions.chat_log_access in its manifest (validated in
    # install.py) and the owner consenting at install. See models.App.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN chat_log_access VARCHAR(16) "
        "NOT NULL DEFAULT 'none'"
      ))
      conn.commit()
  else:
    # `full` was a legacy spelling for the same structurally redacted active
    # chat view as `summary`; it never exposed raw transcripts. Move existing
    # grants forward deliberately before the stricter schema/ladders load.
    with eng.connect() as conn:
      conn.execute(text(
        "UPDATE apps SET chat_log_access = 'summary' "
        "WHERE chat_log_access = 'full'"
      ))
      conn.commit()
  # Per-app git model (feature 084). Both columns are nullable with no
  # backfill: NULL means "no upstream recorded," which is correct for
  # every app installed before the flag was turned on. See models.App.
  if "upstream_commit" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN upstream_commit VARCHAR(64) NULL"
      ))
      conn.commit()
  if "source_commit" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN source_commit VARCHAR(64) NULL"
      ))
      conn.commit()
  if "conflict_resolver_chat_id" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN conflict_resolver_chat_id "
        "VARCHAR(64) NULL"
      ))
      conn.commit()
  if "conflict_resolver_upstream_commit" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN conflict_resolver_upstream_commit "
        "VARCHAR(64) NULL"
      ))
      conn.commit()
  if "upstream_jsx_sha" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN upstream_jsx_sha VARCHAR(64) NULL"
      ))
      conn.commit()
  if "offline_contract" not in apps_cols:
    # Offline contract from the manifest `offline` block (P1-D). Nullable JSON;
    # NULL for apps with no block or apps installed before this migration. The
    # column is informational — no existing query filters on it (that is an
    # explicit design decision: the offline_capable bool flag is the runtime
    # gate; this stores the rich declaration for the agent + future UI).
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN offline_contract JSON NULL"
      ))
      conn.commit()
  if "system_prompt_file" not in apps_cols:
    # Installed system-app prompt contribution. Existing apps remain inert
    # until updated from a manifest that explicitly declares the file.
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN system_prompt_file VARCHAR(255) NULL"
      ))
      conn.commit()
  if "system_app" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN system_app BOOLEAN "
        "NOT NULL DEFAULT 0"
      ))
      # The system-prompt mechanism predates the explicit identity by one
      # release. Preserve those already-reviewed live capabilities.
      conn.execute(text(
        "UPDATE apps SET system_app = 1 "
        "WHERE system_prompt_file IS NOT NULL"
      ))
      conn.commit()
  if "capability_contract" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN capability_contract JSON NULL"
      ))
      conn.commit()
  # Authority used to be an execution-policy switch in capability receipts.
  # Server-side jobs now have one owner-trusted process path, so rewrite known
  # historical receipts once rather than leaving inert policy vocabulary in
  # owner-visible app reviews forever. Unknown future schemas remain untouched.
  with eng.connect() as conn:
    rows = conn.execute(text(
      "SELECT id, capability_contract FROM apps "
      "WHERE capability_contract IS NOT NULL"
    )).fetchall()
    update_contract = text(
      "UPDATE apps SET capability_contract = :contract WHERE id = :app_id"
    ).bindparams(bindparam("contract", type_=SAJSON))
    changed_contracts = 0
    for app_id, contract in rows:
      upgraded = _upgrade_app_capability_contract(contract)
      if upgraded is None:
        continue
      conn.execute(update_contract, {"contract": upgraded, "app_id": app_id})
      changed_contracts += 1
    if changed_contracts:
      conn.commit()
  if "chats" in tables:
    chats_cols = {c["name"] for c in inspector.get_columns("chats")}
    _add = []
    if "uploads" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN uploads JSON NOT NULL DEFAULT '[]'")
    if "pending_messages" not in chats_cols:
      _add.append(
        "ALTER TABLE chats ADD COLUMN pending_messages JSON NOT NULL DEFAULT '[]'"
      )
    if "deleted_at" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN deleted_at DATETIME")
    if "session_id" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN session_id VARCHAR(128)")
    if "provider" not in chats_cols:
      _add.append(
        "ALTER TABLE chats ADD COLUMN provider VARCHAR(32) "
        "NOT NULL DEFAULT 'claude'"
      )
    if "agent_settings_json" not in chats_cols:
      # Nullable JSON blob holding per-chat overrides for the agent
      # runtime (model, effort, ...). Null means "fall back to the
      # global default in /data/shared/agent-settings.json".
      _add.append(
        "ALTER TABLE chats ADD COLUMN agent_settings_json JSON"
      )
    if "system_prompt_snapshot_id" not in chats_cols:
      # Existing and empty chats start NULL. The first turn after this
      # migration captures one immutable, content-addressed prompt snapshot;
      # later app installs/updates/uninstalls cannot change that chat's prompt.
      _add.append(
        "ALTER TABLE chats ADD COLUMN system_prompt_snapshot_id VARCHAR(64) NULL"
      )
    if "auto_resume_on_limit" not in chats_cols:
      # Paid provider-limit retries start off until the owner enables them.
      _add.append(
        "ALTER TABLE chats ADD COLUMN auto_resume_on_limit BOOLEAN "
        "NOT NULL DEFAULT FALSE"
      )
    if "auto_resume_on_restart" not in chats_cols:
      # Möbius-initiated planned restarts continue by default.
      _add.append(
        "ALTER TABLE chats ADD COLUMN auto_resume_on_restart BOOLEAN "
        "NOT NULL DEFAULT TRUE"
      )
    if "pinned_at" not in chats_cols:
      # NOT NULL = pinned. Drawer sort key (see routes/chats.py).
      _add.append("ALTER TABLE chats ADD COLUMN pinned_at DATETIME NULL")
    if "created_by_app_id" not in chats_cols:
      # App that opened this chat via the app-attributed chat contract
      # (design §1). NULL = an ordinary owner chat. No FK constraint in
      # the ALTER — SQLite can't add one post-hoc, and the column is an
      # attribution tag, not a referential-integrity guarantee (a
      # deleted app leaving a stale id behind just reads as "no live
      # owner app," which the route tolerates). See models.Chat.
      _add.append("ALTER TABLE chats ADD COLUMN created_by_app_id INTEGER NULL")
    if "activity_at" not in chats_cols:
      # Drawer ordering key that advances only on owner-send. Backfill
      # existing rows to updated_at so their current order is preserved
      # the first time this column appears. See models.Chat.activity_at.
      _add.append("ALTER TABLE chats ADD COLUMN activity_at DATETIME NULL")
      _add.append(
        "UPDATE chats SET activity_at = updated_at WHERE activity_at IS NULL"
      )
    if _add:
      with eng.connect() as conn:
        for stmt in _add:
          conn.execute(text(stmt))
        conn.commit()

  if "owner" in tables:
    owner_cols = {c["name"] for c in inspector.get_columns("owner")}
    _add_owner = []
    if "provider" not in owner_cols:
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN provider VARCHAR(32) "
        "NOT NULL DEFAULT 'claude'"
      )
    if "auto_resume_on_limit_default" not in owner_cols:
      # Paid provider-limit retries start off. Later chat selections update this
      # owner seed so new chats inherit the most recently chosen value.
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN auto_resume_on_limit_default BOOLEAN "
        "NOT NULL DEFAULT FALSE"
      )
    if "auto_resume_on_restart_default" not in owner_cols:
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN auto_resume_on_restart_default BOOLEAN "
        "NOT NULL DEFAULT TRUE"
      )
    if "model_prefs_json" not in owner_cols:
      # Nullable JSON blob holding the owner's model-picker
      # preferences (e.g. hidden model IDs). Null = "show
      # everything" — no backfill needed; the picker treats
      # absence as the default state. See models.Owner for the
      # schema.
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN model_prefs_json JSON"
      )
    if "walkthrough_completed_at" not in owner_cols:
      # NULL = "show the walkthrough." No backfill: existing owners
      # of this single-owner-per-install platform will see the
      # walkthrough exactly once on their next sign-in, which is
      # the explicitly chosen rollout for the new onboarding.
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN walkthrough_completed_at DATETIME"
      )
    if "token_epoch" not in owner_cols:
      # JWT-revocation generation counter. DEFAULT 0 means existing
      # owners migrate to epoch 0 and their already-issued tokens
      # (which carry no epoch claim) keep validating as epoch 0 — no
      # forced sign-out on upgrade. The owner bumps it to 1+ via "sign
      # out everywhere", which strands every pre-bump token. See
      # models.Owner.token_epoch.
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN token_epoch INTEGER NOT NULL DEFAULT 0"
      )
    if "sso_subject" not in owner_cols:
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN sso_subject VARCHAR(128)"
      )
    if "sso_email" not in owner_cols:
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN sso_email VARCHAR(320)"
      )
    if _add_owner:
      with eng.connect() as conn:
        for stmt in _add_owner:
          conn.execute(text(stmt))
        conn.commit()

  # `chat_runs` is a newer table (persistence redesign Step 3): create_all
  # builds it fresh with the current schema, but on an already-deployed DB the
  # table exists WITHOUT the provider-park columns, so add them here. Guarded on
  # the table existing — a fresh install returned above (create_all handles it).
  if "chat_runs" in tables:
    chat_runs_cols = {c["name"] for c in inspector.get_columns("chat_runs")}
    _add_runs = []
    if "parked_until" not in chat_runs_cols:
      _add_runs.append(
        "ALTER TABLE chat_runs ADD COLUMN parked_until DATETIME NULL"
      )
    if "park_reason" not in chat_runs_cols:
      _add_runs.append(
        "ALTER TABLE chat_runs ADD COLUMN park_reason VARCHAR(32) NULL"
      )
    if "restart_nonce" not in chat_runs_cols:
      _add_runs.append(
        "ALTER TABLE chat_runs ADD COLUMN restart_nonce VARCHAR(128) NULL"
      )
    if "provider_session_id" not in chat_runs_cols:
      _add_runs.append(
        "ALTER TABLE chat_runs ADD COLUMN provider_session_id "
        "VARCHAR(128) NULL"
      )
    for column in (
      "input_tokens",
      "output_tokens",
      "cache_read_input_tokens",
      "cache_creation_input_tokens",
      "reasoning_output_tokens",
      "total_tokens",
      "model_context_window",
    ):
      if column not in chat_runs_cols:
        _add_runs.append(
          f"ALTER TABLE chat_runs ADD COLUMN {column} INTEGER NULL"
        )
    if "usage_json" not in chat_runs_cols:
      _add_runs.append(
        "ALTER TABLE chat_runs ADD COLUMN usage_json JSON NULL"
      )
    if _add_runs:
      with eng.connect() as conn:
        for stmt in _add_runs:
          conn.execute(text(stmt))
        conn.commit()

  # d6fae591 briefly copied Codex delegated prompts/thread previews into
  # non-terminal lifecycle ``summary``. Remove all such already-persisted
  # values on upgrade. The corrected emitter keeps identity/role on agent_type
  # and reserves Codex summary for terminal provider-authored results, so this
  # structural cleanup needs no brittle inference from clipped source ids.
  if "agent_lifecycle_events" in tables:
    with eng.connect() as conn:
      conn.execute(text(
        "UPDATE agent_lifecycle_events SET summary = NULL "
        "WHERE provider = 'codex' AND summary IS NOT NULL "
        "AND event_type IN ('agent_spawned', 'agent_started')"
      ))
      conn.commit()

  # The same commit introduced activation ids as ``activation-`` + SHA-256 (75
  # characters) but declared both columns VARCHAR(70). SQLite ignores the
  # declared VARCHAR length, so its already-deployed rows are intact and need no
  # table rebuild. PostgreSQL enforces it and therefore needs an explicit widen:
  # create_all never alters an existing table. Widening is lossless and each
  # column is independently gated so a restart after one ALTER converges.
  if (
    eng.dialect.name == "postgresql"
    and "agent_lifecycle_events" in tables
  ):
    _widen_lifecycle = _agent_lifecycle_width_migrations(
      eng.dialect.name,
      inspector.get_columns("agent_lifecycle_events"),
    )
    if _widen_lifecycle:
      with eng.connect() as conn:
        for stmt in _widen_lifecycle:
          conn.execute(text(stmt))
        conn.commit()

  # Unread tracking for the in-app notification preview. Backfill pre-feature
  # history as read in the SAME transaction as the ALTER — an upgrade must not
  # greet the owner with a badge counting every notification ever sent, and a
  # crash between the two statements must not leave that state half-applied.
  if "notifications" in tables:
    notif_cols = {c["name"] for c in inspector.get_columns("notifications")}
    if "read_at" not in notif_cols:
      with eng.connect() as conn:
        conn.execute(text(
          "ALTER TABLE notifications ADD COLUMN read_at DATETIME NULL"
        ))
        conn.execute(text(
          "UPDATE notifications SET read_at = sent_at WHERE read_at IS NULL"
        ))
        conn.commit()


def _add_chat_run_goal_objective(eng) -> None:
  """Persist active goal identity on its owning durable run.

  The bounded backfill covers a goal already running during this upgrade. User
  timestamps are server-authored, and the run starts after its initiating row
  is committed; later steered questions therefore fall strictly after the run
  boundary and cannot replace the initiating ``/goal`` candidate.
  """
  import re
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  tables = set(inspector.get_table_names())
  if "chat_runs" not in tables:
    return
  columns = {column["name"] for column in inspector.get_columns("chat_runs")}
  if "goal_objective" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE chat_runs ADD COLUMN goal_objective TEXT NULL"
    ))
    rows = (
      conn.execute(text(
        "SELECT r.id, r.started_at, c.messages "
        "FROM chat_runs r JOIN chats c ON c.id = r.chat_id "
        "WHERE r.status IN ('running', 'parked', 'resume_pending')"
      )).all()
      if "chats" in tables
      else []
    )
    for run_id, raw_started_at, raw_messages in rows:
      try:
        started_at = raw_started_at
        if isinstance(started_at, str):
          started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started_at.tzinfo is None:
          started_at = started_at.replace(tzinfo=UTC)
        started_ms = started_at.timestamp() * 1000
        messages = (
          json.loads(raw_messages)
          if isinstance(raw_messages, str)
          else list(raw_messages or [])
        )
      except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        continue
      initiating = None
      for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
          continue
        timestamp = message.get("ts")
        if isinstance(timestamp, (int, float)) and timestamp <= started_ms:
          initiating = message
      content = initiating.get("content", "") if initiating else ""
      if not isinstance(content, str):
        continue
      match = re.match(r"^\s*/goal(?:\s+([\s\S]+))?\s*$", content)
      objective = (match.group(1) or "").strip() if match else ""
      if not objective or objective.lower() == "clear":
        continue
      conn.execute(text(
        "UPDATE chat_runs SET goal_objective = :objective WHERE id = :run_id"
      ), {"objective": objective, "run_id": run_id})


def _add_chat_run_goal_plan(eng) -> None:
  """Add the bounded plan snapshot and optimistic revision to goal roots."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chat_runs" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chat_runs")}
  with eng.begin() as conn:
    if "goal_plan_json" not in columns:
      conn.execute(text(
        "ALTER TABLE chat_runs ADD COLUMN goal_plan_json JSON NULL"
      ))
    if "goal_plan_revision" not in columns:
      conn.execute(text(
        "ALTER TABLE chat_runs ADD COLUMN goal_plan_revision INTEGER "
        "NOT NULL DEFAULT 0"
      ))


def _add_chat_run_goal_identity(eng) -> None:
  """Give a native Goal identity that survives logical-run recovery."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chat_runs" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chat_runs")}
  with eng.begin() as conn:
    if "goal_id" not in columns:
      conn.execute(text("ALTER TABLE chat_runs ADD COLUMN goal_id VARCHAR(64) NULL"))
    required = {"id", "root_run_id", "goal_objective"}
    if not required.issubset(columns):
      return
    rows = conn.execute(text(
      "SELECT id, root_run_id FROM chat_runs "
      "WHERE goal_objective IS NOT NULL"
    )).mappings().all()
    for row in rows:
      # Historical logical roots are the only identity the old schema proves.
      # Never merge two Goals merely because their objective text matches: an
      # owner may deliberately start the same Goal again. Future recovery rows
      # inherit the stable id through goal_identity_for_run_start.
      identity = str(row["root_run_id"] or row["id"])
      conn.execute(text(
        "UPDATE chat_runs SET goal_id = :goal_id WHERE id = :run_id "
        "AND goal_id IS NULL"
      ), {"goal_id": identity, "run_id": row["id"]})
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_chat_runs_goal_id ON chat_runs (goal_id)"
    ))


def _add_chat_run_root_identity(eng) -> None:
  """Give every physical run a stable logical identity across continuations."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chat_runs" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chat_runs")}
  with eng.begin() as conn:
    if "root_run_id" not in columns:
      conn.execute(text(
        "ALTER TABLE chat_runs ADD COLUMN root_run_id VARCHAR(64) NULL"
      ))
    # Idempotent backfill: pre-feature physical runs are each their own logical
    # root. New continuation writes inherit explicitly in chat_writer.
    conn.execute(text(
      "UPDATE chat_runs SET root_run_id = id WHERE root_run_id IS NULL"
    ))
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_chat_runs_root_run_id "
      "ON chat_runs (root_run_id)"
    ))
    if eng.dialect.name == "postgresql":
      conn.execute(text(
        "ALTER TABLE chat_runs ALTER COLUMN root_run_id SET NOT NULL"
      ))


def _require_app_identity(eng) -> None:
  """Make every app row retain its canonical URL and source identities.

  Fresh databases receive ordinary NOT NULL + CHECK constraints from the ORM
  model. SQLite cannot add those constraints to an existing table without a
  high-risk table rebuild, so upgraded databases enforce the identical write
  boundary with small BEFORE triggers after proving every stored row is ready.
  PostgreSQL can promote the columns directly.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "apps" not in inspector.get_table_names():
    return
  app_columns = {column["name"] for column in inspector.get_columns("apps")}
  with eng.begin() as conn:
    # Frozen migration copy. Historical identities must not change when the
    # lifecycle helper evolves after this migration has shipped.
    def slugify_for_source_dir(name: str) -> str:
      slug = "".join(
        ch if ch.isalnum() else "-" for ch in (name or "").lower()
      ).strip("-")
      while "--" in slug:
        slug = slug.replace("--", "-")
      slug = slug or "app"
      if slug.isdigit():
        slug = f"app-{slug}"
      return slug

    apps_root = Path(get_settings().data_dir) / "apps"
    used_slugs = {
      str(slug)
      for (slug,) in conn.execute(text(
        "SELECT slug FROM apps "
        "WHERE slug IS NOT NULL AND length(trim(slug)) > 0"
      ))
    }
    missing_slugs = conn.execute(text(
      "SELECT id, name FROM apps "
      "WHERE slug IS NULL OR length(trim(slug)) = 0 ORDER BY id"
    )).all()
    for app_id, name in missing_slugs:
      base = slugify_for_source_dir(str(name or ""))
      slug = base
      suffix = 2
      while slug in used_slugs:
        slug = f"{base}-{suffix}"
        suffix += 1
      conn.execute(text(
        "UPDATE apps SET slug = :slug WHERE id = :app_id"
      ), {"slug": slug, "app_id": app_id})
      used_slugs.add(slug)
    source_projection = "jsx_source" if "jsx_source" in app_columns else "NULL"
    apps_root_resolved = apps_root.resolve()
    existing_sources = conn.execute(text(
      "SELECT id, source_dir FROM apps "
      "WHERE source_dir IS NOT NULL AND length(trim(source_dir)) > 0 "
      "ORDER BY id"
    )).all()
    canonical_sources: dict[str, int] = {}
    canonical_updates: list[tuple[int, str]] = []
    for app_id, stored_source in existing_sources:
      try:
        resolved_path = Path(stored_source).resolve()
      except (OSError, RuntimeError) as exc:
        raise RuntimeError(
          f"cannot require app identity: app {app_id} has an invalid source_dir"
        ) from exc
      if (
        resolved_path.parent != apps_root_resolved
        or resolved_path.name.isdigit()
      ):
        raise RuntimeError(
          "cannot require app identity: app "
          f"{app_id} source_dir is outside the canonical apps root"
        )
      resolved = str(resolved_path)
      prior_owner = canonical_sources.get(resolved)
      if prior_owner is not None:
        raise RuntimeError(
          "cannot require app identity: apps "
          f"{prior_owner} and {app_id} resolve to the same source_dir"
        )
      canonical_sources[resolved] = app_id
      if str(stored_source) != resolved:
        canonical_updates.append((app_id, resolved))
    for app_id, resolved in canonical_updates:
      conn.execute(text(
        "UPDATE apps SET source_dir = :source_dir WHERE id = :app_id"
      ), {"source_dir": resolved, "app_id": app_id})
    reserved_sources = set(canonical_sources)
    missing_sources = conn.execute(text(
      f"SELECT id, slug, {source_projection} AS jsx_source FROM apps "
      "WHERE source_dir IS NULL OR length(trim(source_dir)) = 0"
    )).all()
    for app_id, slug, jsx_source in missing_sources:
      if not slug or not str(slug).strip():
        raise RuntimeError(
          f"cannot require app identity: app {app_id} has no slug"
        )
      # URL slugs on very old/corrupt rows were never a filesystem trust
      # boundary. Preserve the URL identity in SQLite, but derive the source
      # basename through the same sanitizer used for newly allocated apps.
      source_basename = slugify_for_source_dir(str(slug))
      source_dir = apps_root / source_basename
      app_git = None
      if isinstance(jsx_source, str):
        from app import app_git

        def reusable_legacy_tree(path: Path) -> bool:
          marker = path / ".mobius-identity-migration"
          try:
            migration_owned = marker.read_text(encoding="utf-8") == (
              f"0004_app_identity_required:{app_id}\n"
            )
          except (FileNotFoundError, OSError):
            migration_owned = False
          try:
            names = {child.name for child in path.iterdir()}
          except (FileNotFoundError, OSError):
            names = set()
          # A crash before the atomic marker publish can leave only the new
          # directory (or its marker temp); a crash in the older implementation
          # could leave only ensure_repo's clean seed. Neither contains owner
          # source, so this app may safely resume the same deterministic path.
          if names <= {
            ".git",
            ".gitignore",
            ".mobius-identity-migration",
            ".mobius-identity-migration.tmp",
          }:
            return True
          try:
            same_entry = (path / "index.jsx").read_text(
              encoding="utf-8"
            ) == jsx_source
          except (FileNotFoundError, OSError):
            return False
          if not same_entry:
            return False
          # A valid marker plus the stored source is exactly the partial state
          # this migration itself can leave between write and commit. Without
          # the marker, accept only an already-clean equivalent repository.
          return migration_owned or (
            app_git.is_repo(path) and not app_git.worktree_dirty(path)
          )

      # Allocate every missing identity, even when an extremely old schema has
      # no stored JSX. Reservations cover existing rows and earlier assignments
      # in this transaction. Resolved containment rejects symlinks that escape
      # apps_root before any mkdir, marker, or Git operation can touch them.
      candidate_number = 0
      while True:
        if candidate_number == 0:
          candidate = source_dir
        elif candidate_number == 1:
          candidate = apps_root / f"{source_basename}-legacy-{app_id}"
        else:
          candidate = apps_root / (
            f"{source_basename}-legacy-{app_id}-{candidate_number}"
          )
        candidate_number += 1
        try:
          resolved_path = candidate.resolve()
        except (OSError, RuntimeError):
          # A pathological occupied basename (for example a symlink loop) does
          # not get to brick boot; allocate the next deterministic sibling.
          continue
        resolved = str(resolved_path)
        if (
          resolved_path.parent != apps_root_resolved
          or resolved_path.name.isdigit()
          or resolved in reserved_sources
        ):
          continue
        if candidate.exists():
          if app_git is None or not reusable_legacy_tree(candidate):
            continue
        source_dir = candidate
        reserved_sources.add(resolved)
        break

      if isinstance(jsx_source, str):
        source_dir.mkdir(parents=True, exist_ok=True)
        marker = source_dir / ".mobius-identity-migration"
        marker_temp = source_dir / ".mobius-identity-migration.tmp"
        marker_temp.write_text(
          f"0004_app_identity_required:{app_id}\n", encoding="utf-8"
        )
        os.replace(marker_temp, marker)
        try:
          app_git.ensure_repo(source_dir)
          # Keep the durable ownership marker through the source commit without
          # accepting it as app source. A crash at any earlier boundary can now
          # retry the same directory deterministically.
          exclude = source_dir / ".git" / "info" / "exclude"
          exclude.parent.mkdir(parents=True, exist_ok=True)
          existing_exclude = (
            exclude.read_text(encoding="utf-8")
            if exclude.exists()
            else ""
          )
          if ".mobius-identity-migration" not in existing_exclude.splitlines():
            exclude.write_text(
              existing_exclude.rstrip("\n")
              + ("\n" if existing_exclude else "")
              + ".mobius-identity-migration\n",
              encoding="utf-8",
            )
          entry = source_dir / "index.jsx"
          # The marker proves this directory belongs to this migration, so a
          # partial prior write is safe to replace with the stored revision.
          entry.write_text(jsx_source, encoding="utf-8")
          if entry.read_text(encoding="utf-8") != jsx_source:
            raise RuntimeError(
              f"cannot require app identity: legacy source for app {app_id} "
              "does not match its stored revision"
            )
          app_git.commit_local(
            source_dir, "Materialize legacy app source identity"
          )
          if app_git.worktree_dirty(source_dir):
            raise RuntimeError(
              "cannot require app identity: materialized source for app "
              f"{app_id} "
              "is not clean"
            )
          marker.unlink(missing_ok=True)
        except Exception:
          # Deliberately retain the marker: it is the crash/retry ownership
          # proof and is excluded from app history once Git exists.
          raise
        if app_git.worktree_dirty(source_dir):
          raise RuntimeError(
            "cannot require app identity: materialized source for app "
            f"{app_id} "
            "is not clean"
          )
      conn.execute(text(
        "UPDATE apps SET source_dir = :source_dir WHERE id = :app_id"
      ), {
        "source_dir": str(source_dir),
        "app_id": app_id,
      })
    invalid = conn.execute(text(
      "SELECT COUNT(*) FROM apps "
      "WHERE slug IS NULL OR length(trim(slug)) = 0 "
      "OR source_dir IS NULL OR length(trim(source_dir)) = 0"
    )).scalar_one()
    if invalid:
      raise RuntimeError(
        f"cannot require app identity: {invalid} app row(s) are incomplete"
      )
    duplicate_sources = conn.execute(text(
      "SELECT source_dir FROM apps GROUP BY source_dir HAVING COUNT(*) > 1"
    )).all()
    if duplicate_sources:
      raise RuntimeError(
        "cannot require app identity: duplicate source_dir values exist"
      )
    conn.execute(text(
      "CREATE UNIQUE INDEX IF NOT EXISTS ix_apps_source_dir "
      "ON apps (source_dir)"
    ))
    if eng.dialect.name == "sqlite":
      predicate = (
        "NEW.slug IS NULL OR length(trim(NEW.slug)) = 0 "
        "OR NEW.source_dir IS NULL OR length(trim(NEW.source_dir)) = 0"
      )
      conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS apps_require_identity_insert "
        f"BEFORE INSERT ON apps WHEN {predicate} BEGIN "
        "SELECT RAISE(ABORT, 'apps require slug and source_dir'); END"
      ))
      conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS apps_require_identity_update "
        f"BEFORE UPDATE OF slug, source_dir ON apps WHEN {predicate} BEGIN "
        "SELECT RAISE(ABORT, 'apps require slug and source_dir'); END"
      ))
    elif eng.dialect.name == "postgresql":
      conn.execute(text(
        "ALTER TABLE apps ALTER COLUMN slug SET NOT NULL"
      ))
      conn.execute(text(
        "ALTER TABLE apps ALTER COLUMN source_dir SET NOT NULL"
      ))
      checks = {
        item.get("name")
        for item in sa_inspect(conn).get_check_constraints("apps")
      }
      if "ck_apps_slug_nonempty" not in checks:
        conn.execute(text(
          "ALTER TABLE apps ADD CONSTRAINT ck_apps_slug_nonempty "
          "CHECK (length(trim(slug)) > 0)"
        ))
      if "ck_apps_source_dir_nonempty" not in checks:
        conn.execute(text(
          "ALTER TABLE apps ADD CONSTRAINT ck_apps_source_dir_nonempty "
          "CHECK (length(trim(source_dir)) > 0)"
        ))


def _add_chat_has_messages(eng) -> None:
  """Materialize transcript emptiness for the drawer's hot list query."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chats" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chats")}
  if "has_messages" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE chats ADD COLUMN has_messages BOOLEAN "
      "NOT NULL DEFAULT FALSE"
    ))
    # One deliberate upgrade-time scan replaces the same scan on every drawer
    # refresh. Inspect the JSON value rather than relying on its serialization.
    if "messages" in columns:
      conn.execute(text(
        "UPDATE chats SET has_messages = CASE "
        "WHEN json_array_length(messages) > 0 "
        "THEN TRUE ELSE FALSE END"
      ))


def _create_chat_search_tables(eng) -> None:
  """Install the disposable normalized search schema for each database."""
  from sqlalchemy import text

  dialect = eng.dialect.name
  if dialect not in {"sqlite", "postgresql"}:
    raise RuntimeError(f"unsupported chat-search database: {dialect}")

  # Search rows are derived from chats. Replace the runtime-created generation
  # once rather than preserving a permanent schema detector in the
  # request path; the first search repopulates these empty canonical tables.
  with eng.begin() as conn:
    if dialect == "sqlite":
      conn.execute(text("DROP TABLE IF EXISTS chat_search_fts"))
    for table_name in (
      "chat_search_docs",
      "chat_search_state",
      "chat_search_meta",
    ):
      conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    id_type = "INTEGER" if dialect == "sqlite" else "BIGSERIAL"
    conn.execute(text(
      "CREATE TABLE chat_search_docs ("
      f"id {id_type} PRIMARY KEY, "
      "chat_id VARCHAR(64) NOT NULL, "
      "msg_idx INTEGER NOT NULL, "
      "ts BIGINT, "
      "role VARCHAR(16), "
      "text TEXT NOT NULL"
      ")"
    ))
    # One composite index owns both row identity and chat-local scans; a
    # separate chat_id index would duplicate its leftmost prefix.
    conn.execute(text(
      "CREATE UNIQUE INDEX ix_chat_search_docs_chat_message "
      "ON chat_search_docs (chat_id, msg_idx)"
    ))
    conn.execute(text(
      "CREATE TABLE chat_search_state ("
      "chat_id VARCHAR(64) PRIMARY KEY, "
      "indexed_updated_at TEXT NOT NULL"
      ")"
    ))

    if dialect == "sqlite":
      conn.execute(text(
        "CREATE VIRTUAL TABLE chat_search_fts USING fts5("
        "text, content='chat_search_docs', content_rowid='id', "
        "tokenize='unicode61 remove_diacritics 2'"
        ")"
      ))
      conn.execute(text(
        "CREATE TRIGGER chat_search_docs_ai "
        "AFTER INSERT ON chat_search_docs BEGIN "
        "INSERT INTO chat_search_fts(rowid, text) VALUES (new.id, new.text); "
        "END"
      ))
      conn.execute(text(
        "CREATE TRIGGER chat_search_docs_ad "
        "AFTER DELETE ON chat_search_docs BEGIN "
        "INSERT INTO chat_search_fts(chat_search_fts, rowid, text) "
        "VALUES ('delete', old.id, old.text); "
        "END"
      ))


def _add_connectors_table(eng) -> None:
  """Create the provider-neutral MCP registry without replacing preview rows."""
  from app.models import Connector

  Connector.__table__.create(bind=eng, checkfirst=True)


def _add_connector_capability_identity(eng) -> None:
  """Give every connector an immutable identity for broker authorization."""
  import secrets
  from sqlalchemy import inspect as sa_inspect, text

  columns = {
    column["name"] for column in sa_inspect(eng).get_columns("connectors")
  }
  with eng.begin() as conn:
    if "capability_id" not in columns:
      conn.execute(text(
        "ALTER TABLE connectors ADD COLUMN capability_id VARCHAR(64) NULL"
      ))
    rows = conn.execute(text(
      "SELECT id FROM connectors "
      "WHERE capability_id IS NULL OR length(trim(capability_id)) = 0"
    )).all()
    for (connector_id,) in rows:
      conn.execute(text(
        "UPDATE connectors SET capability_id = :capability_id WHERE id = :id"
      ), {
        "capability_id": secrets.token_hex(32),
        "id": connector_id,
      })
    conn.execute(text(
      "CREATE UNIQUE INDEX IF NOT EXISTS ix_connectors_capability_id "
      "ON connectors (capability_id)"
    ))
    if eng.dialect.name == "postgresql":
      conn.execute(text(
        "ALTER TABLE connectors ALTER COLUMN capability_id SET NOT NULL"
      ))
    elif eng.dialect.name == "sqlite":
      predicate = (
        "NEW.capability_id IS NULL "
        "OR length(trim(NEW.capability_id)) = 0"
      )
      conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS connectors_require_capability_insert "
        f"BEFORE INSERT ON connectors WHEN {predicate} BEGIN "
        "SELECT RAISE(ABORT, 'connectors require capability_id'); END"
      ))
      conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS connectors_require_capability_update "
        f"BEFORE UPDATE OF capability_id ON connectors WHEN {predicate} BEGIN "
        "SELECT RAISE(ABORT, 'connectors require capability_id'); END"
      ))


def mapped_schema_gaps(eng) -> list[str]:
  """Mapped columns/tables the live database lacks (``table.column``).

  Runs after ``create_all`` + migrations, so any gap is a written-code bug
  (a declared column with no migration), not a pending upgrade. Such a gap
  is invisible at boot and fatal at first query — the 2026-08-04 outage
  hung every chat turn on one missing ``apps`` column while the container
  reported healthy.
  """
  from sqlalchemy import inspect as sa_inspect

  inspector = sa_inspect(eng)
  live_tables = set(inspector.get_table_names())
  gaps: list[str] = []
  for table in Base.metadata.sorted_tables:
    if table.name not in live_tables:
      gaps.append(f"{table.name} (missing table)")
      continue
    live = {column["name"] for column in inspector.get_columns(table.name)}
    gaps.extend(
      f"{table.name}.{column.name}"
      for column in table.columns
      if column.name not in live
    )
  return gaps


def _add_connector_oauth_gcloud_fields(eng) -> None:
  """Add the Google-account (gcloud) sign-in fields to ``connector_oauth``.

  Additive and idempotent: each column is inspector-gated so a re-run no-ops,
  and existing browser-flow grants keep working unchanged (auth_mode defaults
  to ``browser``). ``connector_oauth`` may not exist yet on an install that has
  never added an OAuth connection; ``create_all`` builds it with these columns
  already present, so skip the ALTERs entirely in that case.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "connector_oauth" not in inspector.get_table_names():
    return
  columns = {c["name"] for c in inspector.get_columns("connector_oauth")}
  additions = (
    ("auth_mode",
     "ALTER TABLE connector_oauth ADD COLUMN auth_mode VARCHAR(16) "
     "NOT NULL DEFAULT 'browser'"),
    ("client_id",
     "ALTER TABLE connector_oauth ADD COLUMN client_id VARCHAR(512) NULL"),
    ("client_secret_encrypted",
     "ALTER TABLE connector_oauth ADD COLUMN client_secret_encrypted TEXT NULL"),
    ("user_project",
     "ALTER TABLE connector_oauth ADD COLUMN user_project VARCHAR(256) NULL"),
  )
  with eng.begin() as conn:
    for name, ddl in additions:
      if name not in columns:
        conn.execute(text(ddl))


def _add_app_connections_manage(eng) -> None:
  """Grant column for the Connections mini-app's registry access.

  Numbered migration, NOT a ``_converge_legacy_schema`` ALTER: 0001 is a
  recorded one-shot, so a column added there never reaches a database that
  already ran it — the exact gap behind the 2026-08-04 silent-turn outage.
  Schema-gated for the hand-patched production database and for fresh
  installs whose tables are created from ORM metadata.
  """
  from sqlalchemy import inspect as sa_inspect, text

  columns = {
    column["name"] for column in sa_inspect(eng).get_columns("apps")
  }
  if "connections_manage" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE apps ADD COLUMN connections_manage BOOLEAN "
      "NOT NULL DEFAULT FALSE"
    ))


def _add_app_connect_manage(eng) -> None:
  """Grant column for the Connect mini-app's external-machine access."""
  from sqlalchemy import inspect as sa_inspect, text

  columns = {
    column["name"] for column in sa_inspect(eng).get_columns("apps")
  }
  if "connect_manage" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE apps ADD COLUMN connect_manage BOOLEAN "
      "NOT NULL DEFAULT FALSE"
    ))


def _add_owner_auth_mode(eng) -> None:
  """Add the durable owner login mode (models.Owner.auth_mode).

  Column-existence gated so it is a no-op on a fresh install whose owner table
  was built from ORM metadata, and safe to retry after a crash. Every existing
  row defaults to 'local', so behaviour is byte-identical until an operator
  flips an owner to 'mobius' host-side.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "owner" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("owner")}
  if "auth_mode" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE owner ADD COLUMN auth_mode VARCHAR(16) "
      "NOT NULL DEFAULT 'local'"
    ))


def _add_chat_active_assistant_identity(eng) -> None:
  """Add the scalar owner of regenerable assistant browser state.

  A pending question is the stronger protocol boundary, so locate the exact
  unanswered card it names first. Otherwise backfill from the bounded live
  snapshot. Rows whose exact parked-question owner predates assistant message
  ids stay null here: startup repairs that transcript through the chat-writer
  actor, then stores both identities in one serialized transaction. Schema
  migrations never rewrite ``Chat.messages``.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chats" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chats")}
  with eng.begin() as conn:
    if "active_assistant_message_id" not in columns:
      conn.execute(text(
        "ALTER TABLE chats ADD COLUMN "
        "active_assistant_message_id VARCHAR(128) NULL"
      ))
      columns.add("active_assistant_message_id")
    if not {"id", "messages"}.issubset(columns):
      return
    # Keep the common upgrade path scalar-only. Historical transcripts can be
    # large; hydrate one only for the small set of chats actually parked on a
    # question instead of pulling every chat blob through the migration.
    selected = ["id"]
    if "live_assistant" in columns:
      selected.append("live_assistant")
    if "pending_question_id" in columns:
      selected.append("pending_question_id")
    filters = ["active_assistant_message_id IS NULL"]
    if "deleted_at" in columns:
      filters.append("deleted_at IS NULL")
    rows = conn.execute(text(
      "SELECT " + ", ".join(selected) + " FROM chats WHERE "
      + " AND ".join(filters)
    )).mappings().all()

    def decoded(value):
      if not isinstance(value, str):
        return value
      try:
        return json.loads(value)
      except (TypeError, ValueError, json.JSONDecodeError):
        return None

    def bounded_id(value):
      return value if isinstance(value, str) and 0 < len(value) <= 128 else None

    for row in rows:
      owner_id = None
      pending_question_id = row.get("pending_question_id")
      if isinstance(pending_question_id, str):
        raw_messages = conn.execute(text(
          "SELECT messages FROM chats WHERE id = :chat_id"
        ), {"chat_id": row["id"]}).scalar_one_or_none()
        messages = decoded(raw_messages)
        if isinstance(messages, list):
          for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, dict) or message.get("hidden"):
              continue
            if message.get("role") != "assistant":
              continue
            matching_question = any(
              isinstance(block, dict)
              and block.get("type") == "question"
              and block.get("question_id") == pending_question_id
              and not block.get("answers")
              for block in (message.get("blocks") or [])
            )
            if matching_question:
              owner_id = bounded_id(message.get("id"))
              break
      if owner_id is None:
        live = decoded(row.get("live_assistant"))
        if isinstance(live, dict):
          owner_id = bounded_id(live.get("id"))
      if owner_id is not None:
        conn.execute(text(
          "UPDATE chats SET active_assistant_message_id = :owner_id "
          "WHERE id = :chat_id AND active_assistant_message_id IS NULL"
        ), {"chat_id": row["id"], "owner_id": owner_id})


def _add_chat_pending_question_id(eng) -> None:
  """Add the durable open-AskUserQuestion marker (models.Chat).

  Backfill only chats with a nonterminal durable run and an unanswered question
  in their latest visible assistant message. That preserves a question parked
  at upgrade without reviving historical cards on completed chats.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chats" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chats")}
  if "pending_question_id" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE chats ADD COLUMN pending_question_id VARCHAR(64) NULL"
    ))
    tables = set(inspector.get_table_names())
    if "chat_runs" not in tables or not {"id", "messages"}.issubset(columns):
      return
    run_columns = {
      column["name"] for column in inspector.get_columns("chat_runs")
    }
    if not {"chat_id", "status"}.issubset(run_columns):
      return
    active_rows = conn.execute(text(
      "SELECT c.id, c.messages FROM chats c "
      "WHERE c.pending_question_id IS NULL "
      + ("AND c.deleted_at IS NULL " if "deleted_at" in columns else "")
      + "AND EXISTS ("
      "SELECT 1 FROM chat_runs r WHERE r.chat_id = c.id "
      "AND r.status IN ('running', 'parked', 'resume_pending'))"
    )).all()
    for chat_id, raw_messages in active_rows:
      try:
        messages = (
          json.loads(raw_messages)
          if isinstance(raw_messages, str)
          else list(raw_messages or [])
        )
      except (TypeError, ValueError, json.JSONDecodeError):
        continue
      question_id = None
      for message in reversed(messages):
        if not isinstance(message, dict) or message.get("hidden"):
          continue
        if message.get("role") != "assistant":
          break
        for block in reversed(message.get("blocks") or []):
          if not isinstance(block, dict):
            continue
          candidate = block.get("question_id")
          if (
            block.get("type") == "question"
            and not block.get("answers")
            and isinstance(candidate, str)
            and 0 < len(candidate) <= 64
          ):
            question_id = candidate
            break
        break
      if question_id is not None:
        conn.execute(text(
          "UPDATE chats SET pending_question_id = :question_id "
          "WHERE id = :chat_id AND pending_question_id IS NULL"
        ), {"chat_id": chat_id, "question_id": question_id})


def _add_delegation_parent_wake(eng) -> None:
  """Add the delegation parent auto-wake columns (models.Delegation).

  ``notify_parent_on_complete`` (opt-in, default FALSE) and ``parent_woken_at``
  (nullable retry latch). Existing rows keep the safe defaults: no wake
  fires for delegations created before the upgrade.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "delegations" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("delegations")}
  with eng.begin() as conn:
    if "notify_parent_on_complete" not in columns:
      conn.execute(text(
        "ALTER TABLE delegations ADD COLUMN notify_parent_on_complete "
        "BOOLEAN NOT NULL DEFAULT FALSE"
      ))
    if "parent_woken_at" not in columns:
      conn.execute(text(
        "ALTER TABLE delegations ADD COLUMN parent_woken_at DATETIME NULL"
      ))


def _add_app_hosted_publication(eng) -> None:
  """Replace the live public flag with an immutable hosted snapshot."""
  from sqlalchemy import JSON as SAJSON, bindparam, inspect as sa_inspect, text
  from app.app_capabilities import (
    capability_digest,
    public_access_declaration_from_contract,
  )
  from app.compiler import publish_public_bundle

  inspector = sa_inspect(eng)
  if "apps" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("apps")}
  with eng.begin() as conn:
    additions = {
      "published_manifest_url": "VARCHAR(1024) NULL",
      "public_name": "VARCHAR(255) NULL",
      "public_bundle_path": "VARCHAR(512) NULL",
      "public_bundle_digest": "VARCHAR(64) NULL",
      "public_source_commit": "VARCHAR(64) NULL",
      "public_access_contract": "JSON NULL",
      "public_access_digest": "VARCHAR(64) NULL",
      "public_token_nonce": "VARCHAR(32) NULL",
      "public_published_at": "DATETIME NULL",
    }
    for name, declaration in additions.items():
      if name not in columns:
        conn.execute(text(f"ALTER TABLE apps ADD COLUMN {name} {declaration}"))
        columns.add(name)

    # The outbound distribution field was renamed before the hosted feature
    # merged. Preserve local developer-instance data, then retire the old name;
    # no runtime reads both shapes.
    if "share_manifest_url" in columns:
      conn.execute(text(
        "UPDATE apps SET published_manifest_url = share_manifest_url "
        "WHERE published_manifest_url IS NULL"
      ))

    if "capability_contract" in columns:
      rows = conn.execute(text(
        "SELECT id, capability_contract FROM apps "
        "WHERE capability_contract IS NOT NULL"
      )).fetchall()
      update_contract = text(
        "UPDATE apps SET capability_contract = :contract WHERE id = :app_id"
      ).bindparams(bindparam("contract", type_=SAJSON))
      for app_id, contract in rows:
        upgraded = _upgrade_app_capability_contract(contract)
        if upgraded is not None:
          conn.execute(update_contract, {"contract": upgraded, "app_id": app_id})

    # Owners who tried the unmerged boolean version keep one exact snapshot of
    # what was live at migration time. Missing/legacy bundles fail private: a
    # publication without executable bytes is not durable state.
    required = {
      "public_enabled", "compiled_path", "source_commit", "capability_contract",
    }
    if required.issubset(columns):
      active = conn.execute(text(
        "SELECT id, name, compiled_path, source_commit, capability_contract "
        "FROM apps WHERE public_enabled = TRUE"
      )).fetchall()
      publish_row = text(
        "UPDATE apps SET public_name = :public_name, "
        "public_bundle_path = :bundle_path, "
        "public_bundle_digest = :bundle_digest, "
        "public_source_commit = :source_commit, "
        "public_access_contract = :contract, "
        "public_access_digest = :contract_digest, "
        "public_token_nonce = :token_nonce, "
        "public_published_at = :published_at WHERE id = :app_id"
      ).bindparams(bindparam("contract", type_=SAJSON))
      for app_id, name, compiled_path, source_commit, contract in active:
        if isinstance(contract, str):
          try:
            contract = json.loads(contract)
          except json.JSONDecodeError:
            contract = {}
        contract = contract if isinstance(contract, dict) else {}
        try:
          bundle_path, bundle_digest = publish_public_bundle(app_id, compiled_path)
        except (OSError, ValueError):
          continue
        public_access = public_access_declaration_from_contract(contract)
        conn.execute(publish_row, {
          "public_name": name,
          "bundle_path": str(bundle_path),
          "bundle_digest": bundle_digest,
          "source_commit": source_commit,
          "contract": public_access,
          "contract_digest": capability_digest(public_access),
          "token_nonce": secrets.token_hex(16),
          "published_at": datetime.now(UTC).replace(tzinfo=None),
          "app_id": app_id,
        })

    for retired in ("share_manifest_url", "public_enabled"):
      if retired in columns:
        conn.execute(text(f"ALTER TABLE apps DROP COLUMN {retired}"))


def _repair_chat_retention_orphans(eng) -> None:
  """Finish chat purges performed before workflow-aware retention existed.

  Older releases could hard-delete a controller or child chat without removing
  its Gauntlet/Delegation graph. They could also leave optional Autopilot links
  and lifecycle rows pointing at already-purged chat/run rows. Current
  ``chat_retention`` removes the whole graph in one transaction; this one-shot
  repair gives databases upgraded from the older behavior that same terminal
  state instead of carrying corrupt compatibility data forever.

  The graph expansion is deliberately data-driven. A broken workflow may have
  nested delegated children, or a task may be the only surviving edge between
  an otherwise-valid run and an orphaned delegation. Once any control edge is
  invalid, reclaim its entire workflow-owned branch just as the current hard
  purge does. Ordinary chats are never inferred abandoned by age or content.
  """
  from sqlalchemy import bindparam, inspect as sa_inspect, text

  tables = set(sa_inspect(eng).get_table_names())
  if "chats" not in tables:
    return

  def _rows(conn, table: str, columns: str):
    if table not in tables:
      return []
    return conn.execute(text(f"SELECT {columns} FROM {table}")).all()

  def _delete_ids(conn, table: str, column: str, values: set[str]) -> None:
    if table not in tables or not values:
      return
    ordered = sorted(values)
    statement = text(
      f"DELETE FROM {table} WHERE {column} IN :values"
    ).bindparams(bindparam("values", expanding=True))
    # Stay below conservative SQLite/PostgreSQL parameter limits on a database
    # carrying many years of legacy workflow artifacts.
    for offset in range(0, len(ordered), 500):
      conn.execute(statement, {"values": ordered[offset:offset + 500]})

  reclaimed_chat_ids: set[str] = set()
  with eng.begin() as conn:
    # SQLite's driver does not begin on SELECT. Hold one write transaction
    # across the baseline and cleanup so concurrent writes cannot change debt.
    before_violations = set()
    if eng.dialect.name == "sqlite":
      conn.exec_driver_sql("BEGIN IMMEDIATE")
      before_violations = set(conn.exec_driver_sql("PRAGMA foreign_key_check"))
    chat_ids = {str(row[0]) for row in _rows(conn, "chats", "id")}
    delegations = {
      str(row[0]): (str(row[1]), str(row[2]))
      for row in _rows(
        conn, "delegations", "id, parent_chat_id, child_chat_id",
      )
    }
    gauntlets = {
      str(row[0]): str(row[1])
      for row in _rows(conn, "gauntlet_runs", "id, parent_chat_id")
    }
    tasks = [
      (str(row[0]), str(row[1]), str(row[2]) if row[2] is not None else None)
      for row in _rows(
        conn, "gauntlet_tasks", "id, gauntlet_run_id, delegation_id",
      )
    ]

    delete_delegations = {
      delegation_id
      for delegation_id, (parent_id, child_id) in delegations.items()
      if parent_id not in chat_ids or child_id not in chat_ids
    }
    delete_gauntlets = {
      gauntlet_id
      for gauntlet_id, parent_id in gauntlets.items()
      if parent_id not in chat_ids
    }
    delete_tasks = {
      task_id
      for task_id, gauntlet_id, delegation_id in tasks
      if gauntlet_id not in gauntlets
      or (delegation_id is not None and delegation_id not in delegations)
    }
    # A task whose other control parent already disappeared identifies the
    # surviving side as part of that same incomplete workflow, not as a new
    # standalone authority.
    for task_id, gauntlet_id, delegation_id in tasks:
      if task_id not in delete_tasks:
        continue
      if gauntlet_id in gauntlets:
        delete_gauntlets.add(gauntlet_id)
      if delegation_id in delegations:
        delete_delegations.add(delegation_id)

    while True:
      before = (
        len(reclaimed_chat_ids), len(delete_delegations),
        len(delete_gauntlets), len(delete_tasks),
      )
      reclaimed_chat_ids.update(
        child_id
        for delegation_id, (_parent_id, child_id) in delegations.items()
        if delegation_id in delete_delegations and child_id in chat_ids
      )
      delete_gauntlets.update(
        gauntlet_id
        for gauntlet_id, parent_id in gauntlets.items()
        if parent_id in reclaimed_chat_ids
      )
      delete_delegations.update(
        delegation_id
        for delegation_id, (parent_id, child_id) in delegations.items()
        if parent_id in reclaimed_chat_ids or child_id in reclaimed_chat_ids
      )
      for task_id, gauntlet_id, delegation_id in tasks:
        if (
          gauntlet_id in delete_gauntlets
          or delegation_id in delete_delegations
        ):
          delete_tasks.add(task_id)
          if gauntlet_id in gauntlets:
            delete_gauntlets.add(gauntlet_id)
          if delegation_id in delegations:
            delete_delegations.add(delegation_id)
      after = (
        len(reclaimed_chat_ids), len(delete_delegations),
        len(delete_gauntlets), len(delete_tasks),
      )
      if after == before:
        break

    if "contribution_autopilot" in tables:
      # The follow-up chat is a convenience pointer, not the Autopilot record's
      # identity. Preserve the ledger while clearing both old and newly-reclaimed
      # targets.
      conn.execute(text(
        "UPDATE contribution_autopilot SET followup_chat_id = NULL "
        "WHERE followup_chat_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM chats c WHERE c.id = followup_chat_id)"
      ))
      if reclaimed_chat_ids:
        statement = text(
          "UPDATE contribution_autopilot SET followup_chat_id = NULL "
          "WHERE followup_chat_id IN :values"
        ).bindparams(bindparam("values", expanding=True))
        ordered = sorted(reclaimed_chat_ids)
        for offset in range(0, len(ordered), 500):
          conn.execute(statement, {"values": ordered[offset:offset + 500]})

    _delete_ids(conn, "gauntlet_tasks", "id", delete_tasks)
    _delete_ids(conn, "gauntlet_runs", "id", delete_gauntlets)
    _delete_ids(conn, "delegations", "id", delete_delegations)

    if "agent_lifecycle_events" in tables:
      conn.execute(text(
        "DELETE FROM agent_lifecycle_events "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM chats c WHERE c.id = agent_lifecycle_events.chat_id"
        ") OR (chat_run_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM chat_runs r "
        "WHERE r.id = agent_lifecycle_events.chat_run_id))"
      ))

    if reclaimed_chat_ids:
      # Match the current hard-purge dependency order. Lifecycle events also
      # bind ChatRun, so remove them before the run rows even if corrupt legacy
      # data gave the event a mismatched chat_id.
      if "agent_lifecycle_events" in tables and "chat_runs" in tables:
        statement = text(
          "DELETE FROM agent_lifecycle_events WHERE chat_id IN :values "
          "OR chat_run_id IN (SELECT id FROM chat_runs "
          "WHERE chat_id IN :values)"
        ).bindparams(bindparam("values", expanding=True))
        ordered = sorted(reclaimed_chat_ids)
        for offset in range(0, len(ordered), 500):
          conn.execute(statement, {"values": ordered[offset:offset + 500]})
      for table in (
        "chat_embed_grants",
        "agent_lifecycle_run_updates",
        "tool_outputs",
        "thinking_traces",
        "chat_session_links",
        "chat_runs",
        "chat_search_docs",
        "chat_search_state",
      ):
        _delete_ids(conn, table, "chat_id", reclaimed_chat_ids)
      _delete_ids(conn, "chats", "id", reclaimed_chat_ids)

    if eng.dialect.name == "sqlite":
      after_violations = set(conn.exec_driver_sql("PRAGMA foreign_key_check"))
      # Validate the edges this repair owns, not every FK on these tables
      # (for example their independent app references). Also reject new debt
      # anywhere: equal counts must not conceal a different orphan. This
      # migration only deletes/updates rows, so row identities stay stable.
      owned_edges = {
        ("delegations", "chats"),
        ("gauntlet_runs", "chats"),
        ("gauntlet_tasks", "gauntlet_runs"),
        ("gauntlet_tasks", "delegations"),
        ("contribution_autopilot", "chats"),
        ("agent_lifecycle_events", "chats"),
        ("agent_lifecycle_events", "chat_runs"),
      }
      violations = (after_violations - before_violations) | {
        row for row in after_violations if (row[0], row[2]) in owned_edges
      }
      if violations:
        kinds = sorted({f"{row[0]}->{row[2]}" for row in violations})
        raise RuntimeError(
          "chat-retention repair left owned or introduced foreign-key violations: "
          + ", ".join(kinds)
        )

  # The database commit is authoritative. Derived filesystem state follows the
  # same best-effort rule as normal retention and never risks erasing a chat
  # whose database transaction could still roll back.
  if reclaimed_chat_ids:
    import shutil

    # Keep this historical migration self-contained. Calling the ordinary
    # retention helper would make a future cleanup refactor silently rewrite
    # what an already-published database migration does.
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    for chat_id in sorted(reclaimed_chat_ids):
      shutil.rmtree(data_dir / "chats" / chat_id, ignore_errors=True)
      shutil.rmtree(
        data_dir / "agent-browser-profiles" / f"chat-{chat_id}",
        ignore_errors=True,
      )
      shutil.rmtree(
        data_dir / "shared" / "memory" / "chats" / chat_id,
        ignore_errors=True,
      )


def _add_app_project_templates(eng) -> None:
  """Persist validated manifest project-template declarations on App rows."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "apps" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("apps")}
  if "project_templates_json" not in columns:
    with eng.begin() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN project_templates_json JSON NULL"
      ))


def _add_project_artifacts(eng) -> None:
  """Persist the per-project artifact registry and build status.

  Additive and idempotent: the column is inspector-gated so a re-run no-ops.
  ``create_all`` builds a fresh projects table with the column already present,
  so this ALTER only covers an already-deployed projects table. Nullable with no
  backfill — every existing row reads NULL as "no artifacts yet." Project files
  (including the ``artifacts/`` output trees) live outside the database and are
  untouched.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "projects" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("projects")}
  if "artifacts_json" not in columns:
    with eng.begin() as conn:
      conn.execute(text(
        "ALTER TABLE projects ADD COLUMN artifacts_json JSON NULL"
      ))


def _add_project_color(eng) -> None:
  """Add an optional owner-chosen color to project identity controls.

  Existing projects remain NULL and continue following the instance accent.
  The inspector gate makes a retry and a fresh ORM-created database no-ops.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "projects" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("projects")}
  if "color" not in columns:
    with eng.begin() as conn:
      conn.execute(text(
        "ALTER TABLE projects ADD COLUMN color VARCHAR(7) NULL"
      ))


def _add_project_chat_collection(eng) -> None:
  """Move Projects from one required primary chat to zero-or-more chats.

  Existing primary chats are preserved and associated through
  ``chats.project_id``. SQLite cannot drop a NOT NULL constraint in place, so
  its small metadata-only Projects table is rebuilt transactionally. Project
  files remain outside the database and are untouched.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  tables = set(inspector.get_table_names())
  if not {"projects", "chats"}.issubset(tables):
    return
  project_columns = {
    column["name"]: column for column in inspector.get_columns("projects")
  }
  if not project_columns["chat_id"].get("nullable", True):
    if eng.dialect.name == "sqlite":
      # No table points at Projects before this migration. Rebuild it before
      # adding chats.project_id so SQLite cannot retarget an incoming FK to the
      # temporary table name during ALTER TABLE RENAME.
      raw = eng.raw_connection()
      try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("ALTER TABLE projects RENAME TO projects__pre_0021")
        cursor.execute(
          "CREATE TABLE projects ("
          "id VARCHAR(64) NOT NULL PRIMARY KEY, "
          "name VARCHAR(256) NOT NULL, "
          "project_type VARCHAR(128) NOT NULL, "
          "root_path VARCHAR(1024) NOT NULL UNIQUE, "
          "chat_id VARCHAR(64) NULL UNIQUE REFERENCES chats(id), "
          "source_app_id INTEGER NULL REFERENCES apps(id) ON DELETE SET NULL, "
          "template_snapshot_json JSON NOT NULL, "
          "legacy_source_json JSON NULL, "
          "deleted_at DATETIME NULL, "
          "created_at DATETIME NULL, "
          "updated_at DATETIME NULL"
          ")"
        )
        cursor.execute(
          "INSERT INTO projects "
          "(id, name, project_type, root_path, chat_id, source_app_id, "
          "template_snapshot_json, legacy_source_json, deleted_at, created_at, updated_at) "
          "SELECT id, name, project_type, root_path, chat_id, source_app_id, "
          "template_snapshot_json, legacy_source_json, deleted_at, created_at, updated_at "
          "FROM projects__pre_0021"
        )
        cursor.execute("DROP TABLE projects__pre_0021")
        cursor.execute("CREATE INDEX ix_projects_chat_id ON projects (chat_id)")
        cursor.execute("CREATE INDEX ix_projects_source_app_id ON projects (source_app_id)")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
      except Exception:
        raw.rollback()
        raise
      finally:
        raw.close()
    else:
      with eng.begin() as conn:
        conn.execute(text(
          "ALTER TABLE projects ALTER COLUMN chat_id DROP NOT NULL"
        ))

  inspector = sa_inspect(eng)
  chat_columns = {column["name"] for column in inspector.get_columns("chats")}
  with eng.begin() as conn:
    if "project_id" not in chat_columns:
      if eng.dialect.name == "sqlite":
        conn.execute(text(
          "ALTER TABLE chats ADD COLUMN project_id VARCHAR(64) NULL"
        ))
      else:
        conn.execute(text(
          "ALTER TABLE chats ADD COLUMN project_id VARCHAR(64) NULL"
        ))
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_chats_project_id ON chats (project_id)"
    ))
    conn.execute(text(
      "UPDATE chats SET project_id = ("
      "SELECT projects.id FROM projects WHERE projects.chat_id = chats.id"
      ") WHERE project_id IS NULL AND EXISTS ("
      "SELECT 1 FROM projects WHERE projects.chat_id = chats.id"
      ")"
    ))
    conn.execute(text("UPDATE projects SET chat_id = NULL WHERE chat_id IS NOT NULL"))


def _add_chat_goal_dismissal(eng) -> None:
  """Give Goal presentation a first-class chat-owned dismissal pointer."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chats" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chats")}
  if "dismissed_goal_id" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE chats ADD COLUMN dismissed_goal_id VARCHAR(64) NULL"
    ))


def _retire_restart_resume_toggle(eng) -> None:
  """Retire the owner restart-resume seed and lift chats a toggle latched off.

  Restart continuation is now always on with no owner toggle. Earlier installs
  carried an ``auto_resume_on_restart_default`` owner seed and let a per-chat
  toggle latch continuation off. Lift every chat a prior toggle latched off —
  EXCEPT a cancelled delegation child, whose ``False`` is an internal
  do-not-resurrect latch owned by ``delegations.mark_cancelled`` — then drop the
  dead seed column. Guarded on the seed column's presence, so it no-ops on any
  database that never carried it.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  tables = set(inspector.get_table_names())
  if "owner" not in tables:
    return
  owner_cols = {c["name"] for c in inspector.get_columns("owner")}
  if "auto_resume_on_restart_default" not in owner_cols:
    return
  # The data lift and schema retirement are one migration outcome. If the
  # column drop fails (for example because the database is locked), roll the
  # lift back and let the migration ledger retry the complete operation later.
  with eng.begin() as conn:
    if "chats" in tables:
      if "delegations" in tables:
        conn.execute(text(
          "UPDATE chats SET auto_resume_on_restart = 1 "
          "WHERE auto_resume_on_restart = 0 AND id NOT IN ("
          "SELECT child_chat_id FROM delegations "
          "WHERE cancelled_at IS NOT NULL AND child_chat_id IS NOT NULL)"
        ))
      else:
        conn.execute(text(
          "UPDATE chats SET auto_resume_on_restart = 1 "
          "WHERE auto_resume_on_restart = 0"
        ))
    conn.execute(text(
      "ALTER TABLE owner DROP COLUMN auto_resume_on_restart_default"
    ))


def _pin_established_legacy_chat_models(eng) -> None:
  """Replace the retired interactive SDK default with explicit chat choices.

  Chats that already completed an assistant turn before model selection became
  mandatory were allowed to persist no model at all. Pin only those established
  conversations to an explicit model the owner has already chosen for the same
  provider. Empty chats keep the intentional first-send selection prompt, while
  deleted and already-explicit chats remain byte-for-byte unchanged.

  The current shared picker file is the strongest source for its provider. For
  the other provider, the most recently used explicit owner chat is the best
  durable evidence available: historical SDK defaults were never recorded.
  """
  from sqlalchemy import JSON as SAJSON, bindparam, inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  tables = set(inspector.get_table_names())
  if "chats" not in tables:
    return
  columns = {column["name"] for column in inspector.get_columns("chats")}
  required = {"id", "provider", "messages", "agent_settings_json"}
  if not required.issubset(columns):
    return

  invalid_json = object()

  def decoded_json(value):
    if isinstance(value, str):
      try:
        return json.loads(value)
      except (TypeError, ValueError):
        return invalid_json
    return value

  def model_provider(model):
    if not isinstance(model, str) or not model.strip():
      return None
    normalized = model.strip()
    if normalized.startswith("claude-"):
      return "claude"
    if normalized.startswith("gpt-"):
      return "codex"
    if normalized == "inkling":
      return "mobius"
    return None

  provider_ids = {"claude", "codex", "mobius"}
  selected_models = {}

  # The shared file contains the latest picker choice. Associate a future or
  # custom model id with owner.provider unless its known prefix contradicts it.
  owner_provider = None
  if "owner" in tables:
    owner_columns = {
      column["name"] for column in inspector.get_columns("owner")
    }
    if "provider" in owner_columns:
      with eng.connect() as conn:
        owner_provider = conn.execute(text(
          "SELECT provider FROM owner ORDER BY id LIMIT 1"
        )).scalar_one_or_none()
  try:
    global_settings = json.loads(
      (Path(os.environ.get("DATA_DIR", "/data"))
       / "shared" / "agent-settings.json").read_text(encoding="utf-8")
    )
  except (OSError, TypeError, ValueError):
    global_settings = {}
  global_model = (
    global_settings.get("model") if isinstance(global_settings, dict) else None
  )
  global_model_provider = model_provider(global_model)
  if (
    owner_provider in provider_ids
    and isinstance(global_model, str)
    and global_model.strip()
    and global_model_provider in {None, owner_provider}
  ):
    selected_models[owner_provider] = global_model.strip()

  optional_columns = [
    name for name in (
      "deleted_at", "created_by_app_id", "activity_at", "updated_at",
      "created_at",
    ) if name in columns
  ]
  select_columns = [
    "id", "provider", "messages", "agent_settings_json", *optional_columns,
  ]
  source_where = " WHERE deleted_at IS NULL" if "deleted_at" in columns else ""
  order_columns = [
    name for name in ("activity_at", "updated_at", "created_at")
    if name in columns
  ]
  if len(order_columns) > 1:
    source_order = " ORDER BY COALESCE(" + ", ".join(order_columns) + ") DESC"
  elif order_columns:
    source_order = f" ORDER BY {order_columns[0]} DESC"
  else:
    source_order = " ORDER BY id DESC"

  with eng.begin() as conn:
    rows = conn.execute(text(
      "SELECT " + ", ".join(select_columns) + " FROM chats"
      + source_where + source_order
    )).mappings().all()

    # Fill any provider the shared file did not cover from the latest explicit
    # owner chat. Unknown ids remain valid evidence unless they are visibly a
    # model from the other supported provider.
    for row in rows:
      provider = row["provider"]
      if provider not in provider_ids or provider in selected_models:
        continue
      if (
        "created_by_app_id" in columns
        and row.get("created_by_app_id") is not None
      ):
        continue
      settings = decoded_json(row["agent_settings_json"])
      if settings is invalid_json or not isinstance(settings, dict):
        continue
      model = settings.get("model")
      classified = model_provider(model)
      if (
        isinstance(model, str)
        and model.strip()
        and classified in {None, provider}
      ):
        selected_models[provider] = model.strip()

    update_settings = text(
      "UPDATE chats SET agent_settings_json = :settings WHERE id = :chat_id"
    ).bindparams(bindparam("settings", type_=SAJSON))
    for row in rows:
      provider = row["provider"]
      selected_model = selected_models.get(provider)
      if selected_model is None:
        continue
      settings = decoded_json(row["agent_settings_json"])
      if settings is invalid_json:
        # A malformed JSON value is partner data, not an empty settings object.
        continue
      if settings is None:
        settings = {}
      if not isinstance(settings, dict):
        continue
      model = settings.get("model")
      if isinstance(model, str) and model.strip():
        continue
      messages = decoded_json(row["messages"])
      if messages is invalid_json or not isinstance(messages, list) or not any(
        isinstance(message, dict) and message.get("role") == "assistant"
        for message in messages
      ):
        continue
      migrated_settings = dict(settings)
      migrated_settings["model"] = selected_model
      conn.execute(update_settings, {
        "settings": migrated_settings,
        "chat_id": row["id"],
      })


def _pin_all_active_chat_models(eng) -> None:
  """Pin every active non-draft chat to a same-provider model.

  Migration 0019 repaired established conversations with assistant history.
  Other programmatic and app-owned chats could still have escaped creation
  without a model, which now makes their unattended start fail closed. Repair
  those rows without changing provider identity. A genuinely untouched shell
  draft remains lazy so it can inherit the owner's latest picker choice when
  its first turn is admitted, even if another pane changed that choice after
  the empty row was materialized.

  This is a frozen migration: model classification/defaults and the untouched
  draft predicate are copied here and must not drift with live runtime helpers
  after publication.
  """
  from sqlalchemy import JSON as SAJSON, bindparam, inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  tables = set(inspector.get_table_names())
  if "chats" not in tables:
    return
  columns = {column["name"] for column in inspector.get_columns("chats")}
  required = {"id", "title", "provider", "messages", "agent_settings_json"}
  if not required.issubset(columns):
    return

  invalid_json = object()

  def decoded(value):
    if isinstance(value, str):
      try:
        return json.loads(value)
      except (TypeError, ValueError):
        return invalid_json
    return value

  def model_provider(model):
    if not isinstance(model, str) or not model.strip():
      return None
    normalized = model.strip()
    if normalized.endswith("[1m]"):
      normalized = normalized[:-4]
    if normalized.startswith("claude-"):
      return "claude"
    if normalized.startswith("gpt-"):
      return "codex"
    if normalized in {"spark", "inkling"}:
      return "mobius"
    return None

  def compatible_model(model, provider):
    if not isinstance(model, str) or not model.strip():
      return None
    normalized = model.strip()
    if normalized.endswith("[1m]"):
      normalized = normalized[:-4]
    classified = model_provider(normalized)
    return normalized if classified in {None, provider} else None

  defaults = {
    "claude": "claude-opus-4-8",
    "codex": "gpt-5.6-sol",
    "mobius": "inkling",
  }
  provider_ids = set(defaults)
  try:
    global_settings = json.loads(
      (Path(os.environ.get("DATA_DIR", "/data"))
       / "shared" / "agent-settings.json").read_text(encoding="utf-8")
    )
  except (OSError, TypeError, ValueError):
    global_settings = {}
  if not isinstance(global_settings, dict):
    global_settings = {}

  owner_provider = None
  if "owner" in tables:
    owner_columns = {column["name"] for column in inspector.get_columns("owner")}
    if "provider" in owner_columns:
      with eng.connect() as conn:
        owner_provider = conn.execute(text(
          "SELECT provider FROM owner ORDER BY id LIMIT 1"
        )).scalar_one_or_none()
  global_model = global_settings.get("model")
  global_provider = model_provider(global_model)
  if global_provider is None:
    stored_provider = global_settings.get("provider")
    if stored_provider in provider_ids:
      global_provider = stored_provider
    elif owner_provider in provider_ids:
      global_provider = owner_provider
  global_model = compatible_model(global_model, global_provider)

  optional_columns = [name for name in (
    "deleted_at", "created_by_app_id", "project_id", "pending_messages",
    "has_messages", "live_assistant", "active_assistant_message_id",
    "pending_question_id", "session_id", "system_prompt_snapshot_id",
    "activity_at", "updated_at", "created_at",
  ) if name in columns]
  order_columns = [
    name for name in ("activity_at", "updated_at", "created_at") if name in columns
  ]
  if len(order_columns) > 1:
    order_sql = " ORDER BY COALESCE(" + ", ".join(order_columns) + ") DESC"
  elif order_columns:
    order_sql = f" ORDER BY {order_columns[0]} DESC"
  else:
    order_sql = " ORDER BY id DESC"

  with eng.begin() as conn:
    select_columns = [
      "id", "title", "provider", "messages", "agent_settings_json",
      *optional_columns,
    ]
    rows = conn.execute(text(
      "SELECT " + ", ".join(select_columns) + " FROM chats" + order_sql
    )).mappings().all()

    selected_models = {}
    if global_provider in provider_ids and global_model is not None:
      selected_models[global_provider] = global_model
    # The latest explicit same-provider chat is the strongest evidence for a
    # provider not represented by the current global picker choice.
    for row in rows:
      if "deleted_at" in columns and row.get("deleted_at") is not None:
        continue
      provider = row["provider"]
      if provider not in provider_ids or provider in selected_models:
        continue
      settings = decoded(row["agent_settings_json"])
      if not isinstance(settings, dict):
        continue
      candidate = compatible_model(settings.get("model"), provider)
      if candidate is not None:
        selected_models[provider] = candidate
    for provider, model in defaults.items():
      selected_models.setdefault(provider, model)

    update_settings = text(
      "UPDATE chats SET agent_settings_json = :settings WHERE id = :chat_id"
    ).bindparams(bindparam("settings", type_=SAJSON))

    has_runs = "chat_runs" in tables
    usage_rows_by_chat = {}
    run_chat_ids = set()
    if has_runs:
      run_columns = {
        column["name"] for column in inspector.get_columns("chat_runs")
      }
      if "chat_id" in run_columns:
        run_chat_ids = set(conn.execute(text(
          "SELECT DISTINCT chat_id FROM chat_runs"
        )).scalars().all())
      if {
        "chat_id", "usage_json", "status", "id",
      }.issubset(run_columns):
        if {"ended_at", "started_at"}.issubset(run_columns):
          usage_order = (
            " ORDER BY chat_id, COALESCE(ended_at, started_at) DESC, id DESC"
          )
        else:
          usage_order = " ORDER BY chat_id, id DESC"
        usage_rows = conn.execute(text(
          "SELECT chat_id, usage_json FROM chat_runs "
          "WHERE status = 'completed'" + usage_order
        )).mappings().all()
        for usage_row in usage_rows:
          usage_rows_by_chat.setdefault(
            usage_row["chat_id"], []
          ).append(usage_row["usage_json"])
    session_link_chat_ids = set()
    if "chat_session_links" in tables:
      link_columns = {
        column["name"] for column in inspector.get_columns("chat_session_links")
      }
      if "chat_id" in link_columns:
        session_link_chat_ids = set(conn.execute(text(
          "SELECT DISTINCT chat_id FROM chat_session_links"
        )).scalars().all())

    def actual_model_for(row):
      for raw_usage in usage_rows_by_chat.get(row["id"], []):
        usage = decoded(raw_usage)
        if not isinstance(usage, dict):
          continue
        by_model = usage.get("provider_model_usage")
        if not isinstance(by_model, dict):
          continue
        candidates = []
        for raw_model, totals in by_model.items():
          candidate = compatible_model(raw_model, row["provider"])
          if candidate is None:
            continue
          totals = totals if isinstance(totals, dict) else {}
          weight = sum(
            int(totals.get(key) or 0)
            for key in (
              "inputTokens", "outputTokens", "cacheReadInputTokens",
              "cacheCreationInputTokens",
            )
          )
          candidates.append((weight, candidate))
        if candidates:
          return max(candidates)[1]
      return None

    def is_untouched_owner_draft(row, settings, messages):
      """Whether this row may still inherit a future owner picker choice."""
      if row["title"] != "New chat":
        return False
      if "created_by_app_id" in columns and row.get("created_by_app_id") is not None:
        return False
      if "project_id" in columns and row.get("project_id") is not None:
        return False
      if messages is invalid_json or not isinstance(messages, list) or messages:
        return False
      pending = decoded(row.get("pending_messages"))
      if "pending_messages" in columns and (
        pending is invalid_json or not isinstance(pending, list) or pending
      ):
        return False
      if "has_messages" in columns and bool(row.get("has_messages")):
        return False
      for column in (
        "live_assistant", "active_assistant_message_id",
        "pending_question_id", "session_id", "system_prompt_snapshot_id",
      ):
        if column in columns and row.get(column) is not None:
          return False
      if row["id"] in run_chat_ids or row["id"] in session_link_chat_ids:
        return False
      # Picker effort can be chosen before a model. All other settings mark an
      # app/internal/runtime-owned row rather than an untouched shell draft.
      if set(settings) - {"effort", "effort_by_provider"}:
        return False
      return True

    for row in rows:
      if "deleted_at" in columns and row.get("deleted_at") is not None:
        continue
      settings = decoded(row["agent_settings_json"])
      if settings is invalid_json or (settings is not None and not isinstance(settings, dict)):
        continue
      settings = dict(settings or {})
      if isinstance(settings.get("model"), str) and settings["model"].strip():
        continue
      messages = decoded(row["messages"])
      if is_untouched_owner_draft(row, settings, messages):
        continue

      provider = row["provider"]
      chosen = actual_model_for(row)
      if chosen is None:
        chosen = selected_models.get(provider) or defaults.get(provider)
      if chosen is None:
        continue
      settings["model"] = chosen
      conn.execute(update_settings, {
        "settings": settings, "chat_id": row["id"],
      })


def _repair_post_explicit_active_chat_models(eng) -> None:
  """Pin model-less rows left by deployments that kept owner drafts lazy.

  Some installations applied the first active-model migration while ordinary
  shell chat creation still allowed any number of empty rows to defer their
  model choice. Repair those gaps without moving a chat to another provider or
  disturbing its queued/session state. The only nullable exception is one
  genuinely untouched first-install owner chat when no model has ever been
  selected.

  This is a frozen migration: provider classification, defaults, and the
  first-install predicate are deliberately self-contained.
  """
  from sqlalchemy import JSON as SAJSON, bindparam, inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  tables = set(inspector.get_table_names())
  if "chats" not in tables:
    return
  columns = {column["name"] for column in inspector.get_columns("chats")}
  required = {"id", "provider", "messages", "agent_settings_json"}
  if not required.issubset(columns):
    return

  invalid_json = object()

  def decoded(value):
    if isinstance(value, str):
      try:
        return json.loads(value)
      except (TypeError, ValueError):
        return invalid_json
    return value

  def model_provider(model):
    if not isinstance(model, str) or not model.strip():
      return None
    normalized = model.strip()
    if normalized.endswith("[1m]"):
      normalized = normalized[:-4]
    if normalized.startswith("claude-"):
      return "claude"
    if normalized.startswith("gpt-"):
      return "codex"
    if normalized in {"spark", "inkling"}:
      return "mobius"
    return None

  def compatible_model(model, provider):
    if not isinstance(model, str) or not model.strip():
      return None
    normalized = model.strip()
    if normalized.endswith("[1m]"):
      normalized = normalized[:-4]
    classified = model_provider(normalized)
    return normalized if classified in {None, provider} else None

  defaults = {
    "claude": "claude-opus-4-8",
    "codex": "gpt-5.6-sol",
    "mobius": "inkling",
  }
  provider_ids = set(defaults)
  try:
    global_settings = json.loads(
      (Path(os.environ.get("DATA_DIR", "/data"))
       / "shared" / "agent-settings.json").read_text(encoding="utf-8")
    )
  except (OSError, TypeError, ValueError):
    global_settings = {}
  if not isinstance(global_settings, dict):
    global_settings = {}

  owner_provider = None
  if "owner" in tables:
    owner_columns = {column["name"] for column in inspector.get_columns("owner")}
    if "provider" in owner_columns:
      with eng.connect() as conn:
        owner_provider = conn.execute(text(
          "SELECT provider FROM owner ORDER BY id LIMIT 1"
        )).scalar_one_or_none()
  global_model = global_settings.get("model")
  global_provider = model_provider(global_model)
  if global_provider is None:
    stored_provider = global_settings.get("provider")
    if stored_provider in provider_ids:
      global_provider = stored_provider
    elif owner_provider in provider_ids:
      global_provider = owner_provider
  global_model = compatible_model(global_model, global_provider)

  optional_columns = [name for name in (
    "deleted_at", "created_by_app_id", "project_id", "pending_messages",
    "has_messages", "live_assistant", "active_assistant_message_id",
    "pending_question_id", "session_id", "system_prompt_snapshot_id",
    "activity_at", "updated_at", "created_at",
  ) if name in columns]
  order_columns = [
    name for name in ("activity_at", "updated_at", "created_at")
    if name in columns
  ]
  if len(order_columns) > 1:
    order_sql = " ORDER BY COALESCE(" + ", ".join(order_columns) + ") DESC"
  elif order_columns:
    order_sql = f" ORDER BY {order_columns[0]} DESC"
  else:
    order_sql = " ORDER BY id DESC"

  with eng.begin() as conn:
    select_columns = [
      "id", "provider", "messages", "agent_settings_json", *optional_columns,
    ]
    rows = conn.execute(text(
      "SELECT " + ", ".join(select_columns) + " FROM chats" + order_sql
    )).mappings().all()
    total_chat_count = len(rows)

    selected_models = {}
    if global_provider in provider_ids and global_model is not None:
      selected_models[global_provider] = global_model
    for row in rows:
      if "deleted_at" in columns and row.get("deleted_at") is not None:
        continue
      provider = row["provider"]
      if provider not in provider_ids or provider in selected_models:
        continue
      settings = decoded(row["agent_settings_json"])
      if not isinstance(settings, dict):
        continue
      candidate = compatible_model(settings.get("model"), provider)
      if candidate is not None:
        selected_models[provider] = candidate
    for provider, model in defaults.items():
      selected_models.setdefault(provider, model)

    run_chat_ids = set()
    usage_rows_by_chat = {}
    if "chat_runs" in tables:
      run_columns = {
        column["name"] for column in inspector.get_columns("chat_runs")
      }
      if "chat_id" in run_columns:
        run_chat_ids = set(conn.execute(text(
          "SELECT DISTINCT chat_id FROM chat_runs"
        )).scalars().all())
      if {
        "chat_id", "usage_json", "status", "id",
      }.issubset(run_columns):
        if {"ended_at", "started_at"}.issubset(run_columns):
          usage_order = (
            " ORDER BY chat_id, COALESCE(ended_at, started_at) DESC, id DESC"
          )
        else:
          usage_order = " ORDER BY chat_id, id DESC"
        usage_rows = conn.execute(text(
          "SELECT chat_id, usage_json FROM chat_runs "
          "WHERE status = 'completed'" + usage_order
        )).mappings().all()
        for usage_row in usage_rows:
          usage_rows_by_chat.setdefault(
            usage_row["chat_id"], []
          ).append(usage_row["usage_json"])

    session_link_chat_ids = set()
    if "chat_session_links" in tables:
      link_columns = {
        column["name"]
        for column in inspector.get_columns("chat_session_links")
      }
      if "chat_id" in link_columns:
        session_link_chat_ids = set(conn.execute(text(
          "SELECT DISTINCT chat_id FROM chat_session_links"
        )).scalars().all())

    def token_weight(totals):
      weight = 0
      for key in (
        "inputTokens", "outputTokens", "cacheReadInputTokens",
        "cacheCreationInputTokens",
      ):
        try:
          weight += int(totals.get(key) or 0)
        except (TypeError, ValueError):
          continue
      return weight

    def actual_model_for(row):
      for raw_usage in usage_rows_by_chat.get(row["id"], []):
        usage = decoded(raw_usage)
        if not isinstance(usage, dict):
          continue
        by_model = usage.get("provider_model_usage")
        if not isinstance(by_model, dict):
          continue
        candidates = []
        for raw_model, totals in by_model.items():
          candidate = compatible_model(raw_model, row["provider"])
          if candidate is None:
            continue
          totals = totals if isinstance(totals, dict) else {}
          candidates.append((token_weight(totals), candidate))
        if candidates:
          return max(candidates)[1]
      return None

    def genuine_first_install(row, settings, messages):
      if total_chat_count != 1 or global_model is not None:
        return False
      if "deleted_at" in columns and row.get("deleted_at") is not None:
        return False
      if "created_by_app_id" in columns and row.get("created_by_app_id") is not None:
        return False
      if "project_id" in columns and row.get("project_id") is not None:
        return False
      if messages is invalid_json or not isinstance(messages, list) or messages:
        return False
      pending = decoded(row.get("pending_messages"))
      if "pending_messages" in columns and (
        pending is invalid_json or not isinstance(pending, list) or pending
      ):
        return False
      if "has_messages" in columns and bool(row.get("has_messages")):
        return False
      for column in (
        "live_assistant", "active_assistant_message_id",
        "pending_question_id", "session_id", "system_prompt_snapshot_id",
      ):
        if column in columns and row.get(column) is not None:
          return False
      if row["id"] in run_chat_ids or row["id"] in session_link_chat_ids:
        return False
      return not (set(settings) - {"effort", "effort_by_provider"})

    update_settings = text(
      "UPDATE chats SET agent_settings_json = :settings WHERE id = :chat_id"
    ).bindparams(bindparam("settings", type_=SAJSON))
    for row in rows:
      if "deleted_at" in columns and row.get("deleted_at") is not None:
        continue
      settings = decoded(row["agent_settings_json"])
      if settings is invalid_json or (
        settings is not None and not isinstance(settings, dict)
      ):
        continue
      settings = dict(settings or {})
      if isinstance(settings.get("model"), str) and settings["model"].strip():
        continue
      messages = decoded(row["messages"])
      if genuine_first_install(row, settings, messages):
        continue
      provider = row["provider"]
      chosen = actual_model_for(row)
      if chosen is None:
        chosen = selected_models.get(provider) or defaults.get(provider)
      if chosen is None:
        continue
      settings["model"] = chosen
      conn.execute(update_settings, {
        "settings": settings, "chat_id": row["id"],
      })


def _add_shared_app_retention(eng) -> None:
  """Make shared app removal reversible on existing installations."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "shared_app_instances" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("shared_app_instances")}
  with eng.begin() as conn:
    if "deleted_at" not in columns:
      conn.execute(text(
        "ALTER TABLE shared_app_instances ADD COLUMN deleted_at DATETIME NULL"
      ))
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_shared_app_instances_deleted_at "
      "ON shared_app_instances (deleted_at)"
    ))


def _migrate_shared_app_state_files(eng) -> None:
  """Move prototype JSON blobs into the path-based shared storage namespace."""
  import re
  import tempfile
  from sqlalchemy import (
    JSON as SAJSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    bindparam,
    inspect as sa_inspect,
    text,
  )

  inspector = sa_inspect(eng)
  if "shared_app_instances" not in inspector.get_table_names():
    return

  # Keep the published migration independent of today's ORM. A minimal parent
  # declaration resolves the foreign key without asking SQLAlchemy to create or
  # reinterpret the already-deployed shared_app_instances table.
  metadata = MetaData()
  Table(
    "shared_app_instances", metadata,
    Column("id", String(64), primary_key=True),
  )
  changes = Table(
    "shared_app_changes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
      "instance_id",
      String(64),
      ForeignKey("shared_app_instances.id", ondelete="CASCADE"),
      nullable=False,
    ),
    Column("kind", String(16), nullable=False),
    Column("path", String(200), nullable=False),
    Column("version", String(128), nullable=True),
    Column("actor_key", String(72), nullable=False),
    Column("display_name", String(256), nullable=False),
    Column("created_at", DateTime, nullable=False),
  )
  changes.create(bind=eng, checkfirst=True)
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_shared_app_changes_instance_id "
      "ON shared_app_changes (instance_id)"
    ))
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_shared_app_changes_created_at "
      "ON shared_app_changes (created_at)"
    ))

  safe_path = re.compile(r"^[\w._/@+ -]+$")

  def owned_snapshot_root(instance_id: str, snapshot_path: str) -> Path | None:
    data_root = Path(os.environ.get("DATA_DIR", "/data")).resolve()
    instances_root = data_root / "shared" / "app-instances"
    expected = instances_root / str(instance_id)
    stored = Path(snapshot_path)
    lexical = stored if stored.is_absolute() else data_root / stored
    try:
      if lexical.absolute() != (expected / "build").absolute():
        return None
      instances_root.resolve().relative_to(data_root)
    except (OSError, ValueError):
      return None
    return expected

  def validate_state_path(path: str) -> str:
    if (
      not path or len(path) > 200 or path.startswith("/") or "\\" in path
      or ".." in Path(path).parts or not safe_path.fullmatch(path)
    ):
      raise RuntimeError("shared app state has an invalid data path")
    return path

  def atomic_write(file_path: Path, content: bytes) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
      dir=file_path.parent, prefix=f".{file_path.name}.", suffix=".tmp",
    )
    try:
      with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
      os.chmod(temporary, 0o644)
      os.replace(temporary, file_path)
    except BaseException:
      try:
        os.unlink(temporary)
      except OSError:
        pass
      raise

  # Fresh installations use path storage directly and never create the
  # prototype blob columns. Existing installations keep this one-way data
  # migration, but the compatibility shape is not part of normal runtime.
  columns = {column["name"] for column in inspector.get_columns("shared_app_instances")}
  if not {"state_json", "revision"}.issubset(columns):
    return

  clear = text(
    "UPDATE shared_app_instances SET state_json = :empty, revision = 0 WHERE id = :id"
  ).bindparams(bindparam("empty", type_=SAJSON))
  with eng.begin() as conn:
    rows = conn.execute(text(
      "SELECT id, snapshot_path, state_json FROM shared_app_instances"
    )).mappings().all()
    for row in rows:
      values = row["state_json"]
      if isinstance(values, str):
        values = json.loads(values)
      if not isinstance(values, dict) or not values:
        continue
      root = owned_snapshot_root(str(row["id"]), str(row["snapshot_path"]))
      if root is None:
        raise RuntimeError("shared app state has an invalid snapshot path")
      for path, value in values.items():
        validate_state_path(str(path))
        atomic_write(
          root / "data" / Path(str(path)),
          json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
      conn.execute(clear, {"empty": {}, "id": row["id"]})


def _add_project_artifact_drawer_state(eng) -> None:
  """Track built-result opens without treating navigation as a content edit."""
  from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    inspect as sa_inspect,
  )

  if "projects" not in sa_inspect(eng).get_table_names():
    return
  metadata = MetaData()
  Table("projects", metadata, Column("id", String(64), primary_key=True))
  drawer_state = Table(
    "project_artifact_drawer_state", metadata,
    Column(
      "project_id",
      String(64),
      ForeignKey("projects.id", ondelete="CASCADE"),
      primary_key=True,
    ),
    Column("artifact_id", String(64), primary_key=True),
    Column("last_opened_at", DateTime, nullable=False),
  )
  drawer_state.create(bind=eng, checkfirst=True)


def _add_attached_delegation_work(eng) -> None:
  """Make delegated startup recoverable and source-chat work explicit."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "delegations" not in inspector.get_table_names():
    return
  columns = {
    column["name"] for column in inspector.get_columns("delegations")
  }
  with eng.begin() as conn:
    additions = {
      "startup_prompt": "TEXT NULL",
      "source_work_id": "VARCHAR(64) NULL",
      "source_work_intent": "VARCHAR(32) NULL",
      "source_work_context_app_id": "INTEGER NULL",
      "source_work_envelope": "JSON NULL",
      "source_work_status": "VARCHAR(32) NULL",
      "source_work_result": "TEXT NULL",
      "source_work_active_chat_id": "VARCHAR(64) NULL",
    }
    for name, declaration in additions.items():
      if name not in columns:
        conn.execute(text(
          f"ALTER TABLE delegations ADD COLUMN {name} {declaration}"
        ))
    conn.execute(text(
      "CREATE UNIQUE INDEX IF NOT EXISTS ix_delegations_source_work_id "
      "ON delegations (source_work_id)"
    ))
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_delegations_source_work_context_app_id "
      "ON delegations (source_work_context_app_id)"
    ))
    conn.execute(text(
      "CREATE UNIQUE INDEX IF NOT EXISTS "
      "ix_delegations_source_work_active_chat_id "
      "ON delegations (source_work_active_chat_id)"
    ))


def _backfill_chat_app_artifacts(eng) -> None:
  """Lift the last known chat/app build and its acknowledgement cursor."""
  from sqlalchemy import inspect as sa_inspect, text

  tables = set(sa_inspect(eng).get_table_names())
  if not {"apps", "chats", "chat_app_artifacts"}.issubset(tables):
    return
  preview_join = (
    "LEFT JOIN app_preview_state p ON p.app_id = a.id"
    if "app_preview_state" in tables else ""
  )
  preview_projection = "p.seen_updated_at" if preview_join else "NULL"

  def same_instant(left, right) -> bool:
    if left is None or right is None:
      return False
    def parsed(value):
      if isinstance(value, datetime):
        current = value
      else:
        current = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
      if current.tzinfo is not None:
        current = current.astimezone(UTC).replace(tzinfo=None)
      return current
    return parsed(left) == parsed(right)

  with eng.begin() as conn:
    rows = conn.execute(text(
      f"SELECT a.chat_id, a.id AS app_id, a.updated_at, {preview_projection} "
      "AS preview_seen_at "
      "FROM apps a JOIN chats c ON c.id = a.chat_id "
      f"{preview_join} "
      "WHERE a.chat_id IS NOT NULL"
    )).mappings().all()
    for row in rows:
      seen_at = (
        row["updated_at"]
        if same_instant(row["preview_seen_at"], row["updated_at"])
        else None
      )
      conn.execute(text(
        "INSERT INTO chat_app_artifacts "
        "(chat_id, app_id, touched_at, seen_at) "
        "SELECT :chat_id, :app_id, :touched_at, :seen_at "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM chat_app_artifacts "
        "WHERE chat_id = :chat_id AND app_id = :app_id"
        ")"
      ), {
        "chat_id": row["chat_id"],
        "app_id": row["app_id"],
        "touched_at": row["updated_at"],
        "seen_at": seen_at,
      })


def _repair_chat_run_goal_identity_index(eng) -> None:
  """Converge the Goal lookup index without rewriting historical identity."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chat_runs" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chat_runs")}
  if "goal_id" not in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_chat_runs_goal_id ON chat_runs (goal_id)"
    ))


def _migrate_project_agent_messages(eng) -> None:
  """Copy the project-only mailbox into the provider-neutral room ledger.

  The legacy table stays in place as a recovery compatibility surface for an
  older baked backend. New code writes only the generalized table; id-based
  insertion makes this migration safe to retry before its ledger row commits.
  """
  from sqlalchemy import inspect as sa_inspect, text

  # Published migrations cannot import the mutable ORM: the table definition
  # that upgrades an old checkout must remain the same even as current models
  # evolve. These declarations mirror the initial coordination-room schema and
  # use SQL understood by both supported databases.
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS agent_coordination_messages ("
      "id VARCHAR(64) NOT NULL PRIMARY KEY, "
      "room_kind VARCHAR(16) NOT NULL, "
      "room_id VARCHAR(64) NOT NULL, "
      "from_chat_id VARCHAR(64) NOT NULL REFERENCES chats(id), "
      "from_run_id VARCHAR(64) NULL, "
      "to_chat_id VARCHAR(64) NULL REFERENCES chats(id), "
      "kind VARCHAR(16) NOT NULL DEFAULT 'note', "
      "body TEXT NOT NULL, "
      "created_at TIMESTAMP NOT NULL"
      ")"
    ))
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS ix_agent_coordination_room_created "
      "ON agent_coordination_messages "
      "(room_kind, room_id, created_at, id)"
    ))
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS "
      "ix_agent_coordination_messages_from_chat_id "
      "ON agent_coordination_messages (from_chat_id)"
    ))
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS "
      "ix_agent_coordination_messages_to_chat_id "
      "ON agent_coordination_messages (to_chat_id)"
    ))
  inspector = sa_inspect(eng)
  if "project_agent_messages" not in inspector.get_table_names():
    return
  columns = {
    column["name"]
    for column in inspector.get_columns("project_agent_messages")
  }
  required = {
    "id", "project_id", "from_chat_id", "to_chat_id", "body", "created_at",
  }
  if not required.issubset(columns):
    return
  with eng.begin() as conn:
    conn.execute(text(
      "INSERT INTO agent_coordination_messages "
      "(id, room_kind, room_id, from_chat_id, from_run_id, to_chat_id, kind, "
      "body, created_at) "
      "SELECT legacy.id, 'project', legacy.project_id, legacy.from_chat_id, "
      "NULL, legacy.to_chat_id, 'note', legacy.body, legacy.created_at "
      "FROM project_agent_messages AS legacy "
      "WHERE NOT EXISTS ("
      "SELECT 1 FROM agent_coordination_messages AS current "
      "WHERE current.id = legacy.id"
      ")"
    ))


def _add_agent_coordination_send_identity(eng) -> None:
  """Add stable multi-recipient send identity without rewriting old mail."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "agent_coordination_messages" not in inspector.get_table_names():
    return
  columns = {
    column["name"]
    for column in inspector.get_columns("agent_coordination_messages")
  }
  if "send_id" not in columns:
    with eng.begin() as conn:
      conn.execute(text(
        "ALTER TABLE agent_coordination_messages "
        "ADD COLUMN send_id VARCHAR(64) NULL"
      ))
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE INDEX IF NOT EXISTS "
      "ix_agent_coordination_messages_send_id "
      "ON agent_coordination_messages (send_id)"
    ))
    conn.execute(text(
      "CREATE UNIQUE INDEX IF NOT EXISTS "
      "uq_agent_coordination_run_send_target "
      "ON agent_coordination_messages (from_run_id, send_id, to_chat_id)"
    ))


def _add_agent_coordination_send_target(eng) -> None:
  """Make direct and broadcast retry identity enforceable under concurrency."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "agent_coordination_messages" not in inspector.get_table_names():
    return
  columns = {
    column["name"]
    for column in inspector.get_columns("agent_coordination_messages")
  }
  if "send_target_key" not in columns:
    with eng.begin() as conn:
      conn.execute(text(
        "ALTER TABLE agent_coordination_messages "
        "ADD COLUMN send_target_key VARCHAR(64) NULL"
      ))
  with eng.begin() as conn:
    conn.execute(text(
      "DROP INDEX IF EXISTS uq_agent_coordination_run_send_target"
    ))
    conn.execute(text(
      "CREATE UNIQUE INDEX IF NOT EXISTS "
      "uq_agent_coordination_run_send_target "
      "ON agent_coordination_messages "
      "(from_run_id, send_id, send_target_key)"
    ))


def _add_agent_coordination_delivery(eng) -> None:
  """Persist peer delivery intent independently from semantic message kind."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "agent_coordination_messages" not in inspector.get_table_names():
    return
  columns = {
    column["name"]
    for column in inspector.get_columns("agent_coordination_messages")
  }
  if "delivery" not in columns:
    with eng.begin() as conn:
      conn.execute(text(
        "ALTER TABLE agent_coordination_messages "
        "ADD COLUMN delivery VARCHAR(16) NOT NULL DEFAULT 'next_turn'"
      ))


def _add_peer_context_delivery_cursor(eng) -> None:
  """Persist the exact peer inbox boundary admitted to each provider turn."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chat_runs" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chat_runs")}
  with eng.begin() as conn:
    if "peer_message_through_created_at" not in columns:
      conn.execute(text(
        "ALTER TABLE chat_runs "
        "ADD COLUMN peer_message_through_created_at DATETIME NULL"
      ))
    if "peer_message_through_id" not in columns:
      conn.execute(text(
        "ALTER TABLE chat_runs "
        "ADD COLUMN peer_message_through_id VARCHAR(64) NULL"
      ))
    if "peer_message_delivery_pending" not in columns:
      conn.execute(text(
        "ALTER TABLE chat_runs "
        "ADD COLUMN peer_message_delivery_pending BOOLEAN NULL"
      ))


def _add_chat_wait_condition_owner(eng) -> None:
  """Persist the executor named by each observable command wait."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chat_waits" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chat_waits")}
  if "condition_owner" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE chat_waits ADD COLUMN condition_owner VARCHAR(200) NULL"
    ))


def _make_agent_work_claim_history_durable(eng) -> None:
  """Release tombstoned owners and preserve claim history after chat purge.

  Exact-action completion is an idempotency fact owned by the workspace, not
  by the chat that performed it. SQLite cannot alter a foreign key in place,
  so rebuild the two small coordination tables while preserving every row.
  """
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  tables = set(inspector.get_table_names())
  if "agent_work_claims" not in tables:
    return

  with eng.begin() as conn:
    conn.execute(text(
      "UPDATE agent_work_claims SET "
      "released_at = COALESCE(released_at, CURRENT_TIMESTAMP), "
      "updated_at = CURRENT_TIMESTAMP, "
      "outcome = COALESCE(outcome, "
      "'Owning chat was deleted before this action completed.'), "
      "revision = revision + 1 "
      "WHERE completed_at IS NULL AND released_at IS NULL "
      "AND owner_chat_id IN (SELECT id FROM chats WHERE deleted_at IS NOT NULL)"
    ))

  foreign_keys = inspector.get_foreign_keys("agent_work_claims")
  owner_chat_fk = next((
    item for item in foreign_keys
    if item.get("constrained_columns") == ["owner_chat_id"]
  ), None)
  columns = {
    column["name"]: column
    for column in inspector.get_columns("agent_work_claims")
  }
  already_current = (
    columns.get("owner_chat_id", {}).get("nullable") is True
    and owner_chat_fk is not None
    and str(owner_chat_fk.get("options", {}).get("ondelete", "")).upper()
    == "SET NULL"
  )
  if already_current:
    return

  if eng.dialect.name == "sqlite":
    raw = eng.raw_connection()
    try:
      cursor = raw.cursor()
      cursor.execute("PRAGMA foreign_keys=OFF")
      cursor.execute("BEGIN IMMEDIATE")
      has_interests = "agent_work_interests" in tables
      if has_interests:
        cursor.execute(
          "ALTER TABLE agent_work_interests "
          "RENAME TO agent_work_interests__pre_0039"
        )
      cursor.execute(
        "ALTER TABLE agent_work_claims "
        "RENAME TO agent_work_claims__pre_0039"
      )
      cursor.execute(
        "CREATE TABLE agent_work_claims ("
        "id VARCHAR(64) NOT NULL PRIMARY KEY, "
        "owner_id INTEGER NOT NULL, "
        "work_key VARCHAR(256) NOT NULL, summary VARCHAR(500) NOT NULL, "
        "owner_chat_id VARCHAR(64) NULL, "
        "owner_run_id VARCHAR(64) NOT NULL, owner_goal_id VARCHAR(64) NULL, "
        "previous_owner_chat_id VARCHAR(64) NULL, "
        "takeover_reason VARCHAR(1000) NULL, "
        "revision INTEGER NOT NULL DEFAULT '1', "
        "notification_revision INTEGER NOT NULL DEFAULT '1', "
        "claimed_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
        "released_at DATETIME NULL, completed_at DATETIME NULL, "
        "outcome VARCHAR(1000) NULL, "
        "CONSTRAINT uq_agent_work_claim_key UNIQUE (owner_id, work_key), "
        "FOREIGN KEY(owner_id) REFERENCES owner(id) ON DELETE CASCADE, "
        "FOREIGN KEY(owner_chat_id) REFERENCES chats(id) ON DELETE SET NULL"
        ")"
      )
      cursor.execute(
        "INSERT INTO agent_work_claims SELECT * "
        "FROM agent_work_claims__pre_0039"
      )
      if has_interests:
        cursor.execute(
          "CREATE TABLE agent_work_interests ("
          "id VARCHAR(64) NOT NULL PRIMARY KEY, "
          "claim_id VARCHAR(64) NOT NULL, chat_id VARCHAR(64) NOT NULL, "
          "goal_id VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL, "
          "resolved_at DATETIME NULL, "
          "CONSTRAINT uq_agent_work_interest_goal "
          "UNIQUE (claim_id, chat_id, goal_id), "
          "FOREIGN KEY(claim_id) REFERENCES agent_work_claims(id) ON DELETE CASCADE, "
          "FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE)"
        )
        cursor.execute(
          "INSERT INTO agent_work_interests SELECT * "
          "FROM agent_work_interests__pre_0039"
        )
        cursor.execute("DROP TABLE agent_work_interests__pre_0039")
      cursor.execute("DROP TABLE agent_work_claims__pre_0039")
      cursor.execute(
        "CREATE INDEX ix_agent_work_claims_owner_id "
        "ON agent_work_claims (owner_id)"
      )
      cursor.execute(
        "CREATE INDEX ix_agent_work_claims_owner_chat_id "
        "ON agent_work_claims (owner_chat_id)"
      )
      cursor.execute(
        "CREATE INDEX ix_agent_work_claims_owner_goal_id "
        "ON agent_work_claims (owner_goal_id)"
      )
      if has_interests:
        cursor.execute(
          "CREATE INDEX ix_agent_work_interests_claim_id "
          "ON agent_work_interests (claim_id)"
        )
        cursor.execute(
          "CREATE INDEX ix_agent_work_interests_chat_id "
          "ON agent_work_interests (chat_id)"
        )
        cursor.execute(
          "CREATE INDEX ix_agent_work_interests_goal_id "
          "ON agent_work_interests (goal_id)"
        )
      raw.commit()
      cursor.execute("PRAGMA foreign_keys=ON")
      cursor.close()
    except Exception:
      raw.rollback()
      raise
    finally:
      raw.close()
    return

  if owner_chat_fk and owner_chat_fk.get("name"):
    with eng.begin() as conn:
      conn.execute(text(
        f"ALTER TABLE agent_work_claims DROP CONSTRAINT "
        f"{owner_chat_fk['name']}"
      ))
      conn.execute(text(
        "ALTER TABLE agent_work_claims ALTER COLUMN owner_chat_id DROP NOT NULL"
      ))
      conn.execute(text(
        "ALTER TABLE agent_work_claims ADD FOREIGN KEY (owner_chat_id) "
        "REFERENCES chats(id) ON DELETE SET NULL"
      ))


def _add_provider_execution_admission(eng) -> None:
  """Keep legacy execution unknown; only new runs can prove non-admission."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chat_runs" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("chat_runs")}
  if "provider_execution_admitted" not in columns:
    with eng.begin() as conn:
      conn.execute(text(
        "ALTER TABLE chat_runs ADD COLUMN provider_execution_admitted BOOLEAN NULL"
      ))


def _add_app_runtime_revision(eng) -> None:
  """Separate accepted runtime bytes from source-only Git revision identity."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "apps" not in inspector.get_table_names():
    return
  columns = {column["name"] for column in inspector.get_columns("apps")}
  if "runtime_revision" not in columns:
    with eng.begin() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN runtime_revision VARCHAR(64) NULL"))


def _link_app_project_runtime(eng):
  """Retire duplicate app previews without deleting source, outputs or history."""
  from sqlalchemy import inspect as sa_inspect, text

  if "projects" not in sa_inspect(eng).get_table_names():
    return
  with eng.begin() as conn:
    rows = conn.execute(text(
      "SELECT id, template_snapshot_json, artifacts_json FROM projects"
    )).all()
    for project_id, raw_template, raw_artifacts in rows:
      try:
        template = json.loads(raw_template) if isinstance(raw_template, str) else raw_template
        artifacts = json.loads(raw_artifacts) if isinstance(raw_artifacts, str) else raw_artifacts
      except (TypeError, ValueError):
        # Preserve malformed agent-authored metadata rather than blocking startup.
        continue
      if not isinstance(template, dict):
        continue
      imported = template.get("imported_from")
      if not isinstance(imported, dict) or imported.get("kind") != "app" or imported.get("management") != "linked":
        continue
      entries = artifacts if isinstance(artifacts, list) else []
      retired = [entry["id"] for entry in entries
                 if isinstance(entry, dict) and entry.get("builder") == "app" and isinstance(entry.get("id"), str)]
      remaining = [entry for entry in entries
                   if not isinstance(entry, dict) or entry.get("builder") != "app"]
      template = dict(template)
      template["previews"] = []
      previous = template.get("retired_app_previews")
      previous = [value for value in previous if isinstance(value, str)] if isinstance(previous, list) else []
      template["retired_app_previews"] = list(dict.fromkeys([*previous, *retired]))
      template["guidance"] = (
        "This Project edits the installed app's existing source folder, not a copy. "
        "Saving source never updates the running app. The owner's explicit Build & update app "
        "action uses the ordinary app apply workflow; a failed build keeps the last working app. "
        "Open the installed app for its real runtime, theme and data; do not create a duplicate "
        "App Creation or standalone app preview. Collaborators edit the same linked files, "
        "but project membership does not grant app runtime, private data or update authority."
      )
      conn.execute(text(
        "UPDATE projects SET template_snapshot_json = :template, artifacts_json = :artifacts WHERE id = :id"
      ), {"id": project_id, "template": json.dumps(template), "artifacts": json.dumps(remaining)})


def _separate_chat_live_assistants(eng):
  """Move live bytes off SQLite's historical overflow row without losing a turn."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chats" not in inspector.get_table_names():
    return
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS chat_live_assistants ("
      "chat_id VARCHAR(64) PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE, "
      "snapshot JSON NULL)"
    ))
    if "live_assistant" not in {
      column["name"] for column in inspector.get_columns("chats")
    }:
      return
    # Old workers are drained before migration. Copy with SQL, not Python, so
    # upgrade memory does not scale with chat history or all live snapshots.
    # A retry after a completed migration sees no old column and does nothing.
    conflicts = conn.execute(text(
      "SELECT c.id FROM chats c JOIN chat_live_assistants s ON s.chat_id = c.id "
      "WHERE c.live_assistant IS NOT NULL AND "
      "(s.snapshot IS NULL OR CAST(s.snapshot AS TEXT) <> CAST(c.live_assistant AS TEXT)) "
      "LIMIT 1"
    )).first()
    if conflicts is not None:
      raise RuntimeError("Conflicting live snapshot during chat migration; both copies preserved")
    conn.execute(text(
      "INSERT INTO chat_live_assistants (chat_id, snapshot) "
      "SELECT id, live_assistant FROM chats "
      "WHERE live_assistant IS NOT NULL "
      "AND NOT EXISTS (SELECT 1 FROM chat_live_assistants s WHERE s.chat_id = chats.id)"
    ))
    conn.execute(text("ALTER TABLE chats DROP COLUMN live_assistant"))

def _add_typed_platform_activation_waits(eng) -> None:
  """Add the payload and declaring identities owned by typed activation waits."""
  from sqlalchemy import inspect as sa_inspect, text

  inspector = sa_inspect(eng)
  if "chat_waits" not in inspector.get_table_names():
    return
  columns = {column["name"]: column for column in inspector.get_columns("chat_waits")}
  statements = []
  if "condition_json" not in columns:
    statements.append("ALTER TABLE chat_waits ADD COLUMN condition_json JSON NULL")
  if "root_run_id" not in columns:
    statements.append("ALTER TABLE chat_waits ADD COLUMN root_run_id VARCHAR(64) NULL")
  if "goal_id" not in columns:
    statements.append("ALTER TABLE chat_waits ADD COLUMN goal_id VARCHAR(64) NULL")
  if "linked_question_id" not in columns:
    statements.append("ALTER TABLE chat_waits ADD COLUMN linked_question_id VARCHAR(64) NULL")
  if "action_approved_at" not in columns:
    statements.append("ALTER TABLE chat_waits ADD COLUMN action_approved_at TIMESTAMP NULL")
  kind_length = getattr(columns.get("kind", {}).get("type"), "length", None)
  if eng.dialect.name == "postgresql" and kind_length is not None and kind_length < 32:
    statements.append(
      "ALTER TABLE chat_waits ALTER COLUMN kind TYPE VARCHAR(32)"
    )
  if statements:
    with eng.begin() as conn:
      for statement in statements:
        conn.execute(text(statement))
  index_names = {
    index["name"] for index in sa_inspect(eng).get_indexes("chat_waits")
  }
  indexes = {
    "ix_chat_waits_root_run_id": "root_run_id",
    "ix_chat_waits_goal_id": "goal_id",
    "ix_chat_waits_linked_question_id": "linked_question_id",
  }
  with eng.begin() as conn:
    for name, column in indexes.items():
      if name not in index_names:
        unique = "UNIQUE " if column == "linked_question_id" else ""
        conn.execute(text(
          f"CREATE {unique}INDEX {name} ON chat_waits ({column})"
        ))


def _add_chat_run_activity_delivery(eng):
  """Retain exact non-transcript activity delivered to each provider run."""
  from sqlalchemy import inspect as sa_inspect, text

  if "chat_runs" not in sa_inspect(eng).get_table_names():
    return
  columns = {
    column["name"] for column in sa_inspect(eng).get_columns("chat_runs")
  }
  if "activity_delivery_json" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE chat_runs ADD COLUMN activity_delivery_json JSON NULL"
    ))


def _add_chat_activity_positions(eng):
  """Add nullable, exact-chat activity display evidence without guessing history."""
  from sqlalchemy import text

  with eng.begin() as conn:
    conn.execute(text("""
      CREATE TABLE IF NOT EXISTS chat_activity_positions (
        chat_id VARCHAR(64) NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        event_id VARCHAR(128) NOT NULL,
        position JSON NULL,
        PRIMARY KEY (chat_id, event_id)
      )
    """))


def _add_delegation_result_incorporation(eng):
  """Add nullable proof without guessing acceptance from historical latches."""
  from sqlalchemy import inspect as sa_inspect, text

  if "delegations" not in sa_inspect(eng).get_table_names():
    return
  columns = {
    column["name"] for column in sa_inspect(eng).get_columns("delegations")
  }
  if "result_incorporated_at" in columns:
    return
  with eng.begin() as conn:
    conn.execute(text(
      "ALTER TABLE delegations "
      "ADD COLUMN result_incorporated_at DATETIME NULL"
    ))


_SCHEMA_MIGRATIONS = (
  # Full IDs are permanent identities, not sequence positions. Append new
  # work in execution order; never renumber a shipped ID to reconcile sources.
  ("0001_legacy_schema_convergence", _converge_legacy_schema),
  ("0002_chat_run_goal_objective", _add_chat_run_goal_objective),
  ("0003_chat_run_root_identity", _add_chat_run_root_identity),
  ("0004_app_identity_required", _require_app_identity),
  ("0005_connectors", _add_connectors_table),
  ("0006_connector_capability_identity", _add_connector_capability_identity),
  ("0007_chat_has_messages", _add_chat_has_messages),
  ("0008_chat_search_documents", _create_chat_search_tables),
  ("0009_app_connections_manage", _add_app_connections_manage),
  ("0010_chat_pending_question_id", _add_chat_pending_question_id),
  ("0011_delegation_parent_wake", _add_delegation_parent_wake),
  ("0012_connector_oauth_gcloud", _add_connector_oauth_gcloud_fields),
  ("0013_app_hosted_publication", _add_app_hosted_publication),
  ("0014_chat_run_goal_plan", _add_chat_run_goal_plan),
  ("0015_chat_run_goal_identity", _add_chat_run_goal_identity),
  ("0016_app_connect_manage", _add_app_connect_manage),
  ("0017_retire_restart_resume_toggle", _retire_restart_resume_toggle),
  ("0018_explicit_legacy_chat_models", _pin_established_legacy_chat_models),
  ("0019_chat_active_assistant_identity", _add_chat_active_assistant_identity),
  ("0020_app_project_templates", _add_app_project_templates),
  ("0021_project_chat_collection", _add_project_chat_collection),
  ("0022_project_artifacts", _add_project_artifacts),
  ("0023_project_color", _add_project_color),
  ("0024_chat_goal_dismissal", _add_chat_goal_dismissal),
  ("0025_attached_delegation_work", _add_attached_delegation_work),
  ("0026_chat_wait_condition_owner", _add_chat_wait_condition_owner),
  ("0027_chat_run_goal_identity_index", _repair_chat_run_goal_identity_index),
  ("0028_agent_coordination_rooms", _migrate_project_agent_messages),
  ("0029_agent_coordination_send_identity", _add_agent_coordination_send_identity),
  ("0030_agent_coordination_send_target", _add_agent_coordination_send_target),
  ("0031_chat_retention_orphan_repair", _repair_chat_retention_orphans),
  ("0032_owner_auth_mode", _add_owner_auth_mode),
  ("0033_shared_app_retention", _add_shared_app_retention),
  ("0034_shared_app_path_state", _migrate_shared_app_state_files),
  ("0035_project_artifact_drawer_state", _add_project_artifact_drawer_state),
  ("0036_chat_app_artifacts", _backfill_chat_app_artifacts),
  ("0037_explicit_active_chat_models", _pin_all_active_chat_models),
  ("0038_repair_active_chat_model_gaps", _repair_post_explicit_active_chat_models),
  ("0039_agent_work_claim_history", _make_agent_work_claim_history_durable),
  ("0040_provider_execution_admission", _add_provider_execution_admission),
  ("0041_app_runtime_revision", _add_app_runtime_revision),
  ("0042_linked_app_project_runtime", _link_app_project_runtime),
  ("0043_agent_coordination_delivery", _add_agent_coordination_delivery),
  ("0044_peer_context_delivery_cursor", _add_peer_context_delivery_cursor),
  ("0045_chat_live_assistants", _separate_chat_live_assistants),
  ("0046_chat_run_activity_delivery", _add_chat_run_activity_delivery),
  ("0047_chat_activity_positions", _add_chat_activity_positions),
  ("0048_delegation_result_incorporation", _add_delegation_result_incorporation),
  ("0048_typed_platform_activation_waits", _add_typed_platform_activation_waits),
)


def schema_migration_history(eng) -> list[dict]:
  """Return the durable migration ledger in application order."""
  from sqlalchemy import inspect as sa_inspect, text

  if "schema_migrations" not in sa_inspect(eng).get_table_names():
    return []
  with eng.connect() as conn:
    rows = conn.execute(text(
      "SELECT version, applied_at FROM schema_migrations ORDER BY applied_at, version"
    )).all()
  return [
    {"version": version, "applied_at": applied_at}
    for version, applied_at in rows
  ]


def _ensure_migration_ledger(eng) -> None:
  """Create the durable one-shot ledger if it does not exist yet."""
  from sqlalchemy import text

  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, "
      "applied_at TIMESTAMP NOT NULL"
      ")"
    ))


def _applied_migrations(eng) -> set[str]:
  """Read the completed versions once for one ordered migration pass."""
  from sqlalchemy import text

  with eng.connect() as conn:
    return {
      str(version)
      for (version,) in conn.execute(text(
        "SELECT version FROM schema_migrations"
      ))
    }


def _record_migration(eng, version: str) -> None:
  """Record one completed version after its idempotent body succeeds."""
  from sqlalchemy import text

  with eng.begin() as conn:
    conn.execute(text(
      "INSERT INTO schema_migrations (version, applied_at) "
      "VALUES (:version, :applied_at)"
    ), {
      "version": version,
      "applied_at": datetime.now(UTC).replace(tzinfo=None),
    })


def run_migrations(eng) -> None:
  """Apply pending migrations without replaying recorded completions.

  The first migration freezes the historical inspector-based convergence path.
  Existing installs run it once and record the outcome; fresh installs record
  the same baseline after ``create_all``. Future schema work appends a named
  function to ``_SCHEMA_MIGRATIONS`` instead of extending a boot-time scan.

  Each migration remains internally idempotent so a crash before its ledger
  insert safely retries it. The ledger row is committed only after the migration
  returns successfully.

  Before upgrading a pre-cutover local database or restoring its backup, run
  scripts/normalize-migration-ledger-20260908.py explicitly (see
  scripts/MIGRATION-CUTOVER.md). Startup does not infer historical equivalence.

  One runner owns the ledger: it reads the applied set once, executes pending
  entries in registry order, and records each success before continuing.
  """
  from sqlalchemy import inspect as sa_inspect

  if "apps" not in sa_inspect(eng).get_table_names():
    return
  _ensure_migration_ledger(eng)
  applied = _applied_migrations(eng)
  for version, migration in _SCHEMA_MIGRATIONS:
    if version in applied:
      continue
    migration(eng)
    _record_migration(eng, version)
    applied.add(version)
