"""Database engine and session configuration.

Served from the editable platform checkout. main.py imports this at module load
to set up the engine and migrations; if a local edit breaks it, normal boot
falls back to the baked platform while preserving the checkout for operator
repair. For ad-hoc DB queries use raw stdlib `sqlite3` instead of changing
this module.
"""

import json
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime
from contextvars import ContextVar, Token
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app import sqlite_policy
from app.config import get_settings


_log = logging.getLogger(__name__)
_request_label: ContextVar[str] = ContextVar(
  "mobius_database_request_label", default="background",
)
_checkout_warn_seconds = float(os.environ.get("DB_CHECKOUT_WARN_SECONDS", "2"))
_pool_metrics_lock = threading.Lock()
_pool_metrics = {
  "checked_out": 0,
  "checkouts": 0,
  "long_checkouts": 0,
  "max_checkout_ms": 0,
  "last_long_checkout": None,
}


def set_database_request_label(label: str) -> Token:
  return _request_label.set(label)


def reset_database_request_label(token: Token) -> None:
  _request_label.reset(token)


def _make_engine():
  """Creates the SQLAlchemy engine, ensuring the DB directory exists."""
  settings = get_settings()
  is_sqlite = settings.database_url.startswith("sqlite")
  if settings.database_url.startswith("sqlite:////"):
    db_path = Path(settings.database_url.replace("sqlite:////", "/"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
  connect_args = {"check_same_thread": False} if is_sqlite else {}
  # Pool hardening for Postgres (Railway et al.). Defaults (QueuePool
  # 5 + 10 overflow) stay, but pre_ping validates a connection before
  # handing it out — Railway silently drops idle Postgres connections,
  # and without this the first query on a stale one raises instead of
  # transparently reconnecting. pool_recycle caps connection age below
  # any server-side idle timeout. Omitted for SQLite, whose pool is
  # process-local and never sees these failure modes.
  # SQLite uses NullPool so temporarily-held request sessions cannot exhaust
  # an artificial QueuePool ceiling. Postgres retains bounded QueuePool reuse.
  pool_kwargs = (
    {"poolclass": NullPool}
    if is_sqlite
    else {"pool_pre_ping": True, "pool_recycle": 1800}
  )
  eng = create_engine(
    settings.database_url, connect_args=connect_args, **pool_kwargs
  )

  @event.listens_for(eng, "checkout")
  def _track_checkout(_dbapi_conn, connection_record, _connection_proxy):
    label = _request_label.get()
    connection_record.info["mobius_checkout"] = (time.monotonic(), label)
    with _pool_metrics_lock:
      _pool_metrics["checked_out"] += 1
      _pool_metrics["checkouts"] += 1

  @event.listens_for(eng, "checkin")
  def _track_checkin(_dbapi_conn, connection_record):
    started = connection_record.info.pop("mobius_checkout", None)
    if not started:
      return
    started_at, label = started
    elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
    is_long = elapsed_ms >= round(_checkout_warn_seconds * 1000)
    with _pool_metrics_lock:
      _pool_metrics["checked_out"] = max(0, _pool_metrics["checked_out"] - 1)
      _pool_metrics["max_checkout_ms"] = max(
        _pool_metrics["max_checkout_ms"], elapsed_ms,
      )
      if is_long:
        _pool_metrics["long_checkouts"] += 1
        _pool_metrics["last_long_checkout"] = {
          "request": label,
          "duration_ms": elapsed_ms,
        }
    if is_long:
      _log.warning(
        "Database connection checked out for %dms by %s",
        elapsed_ms,
        label,
      )
  if is_sqlite:
    # NullPool opens a fresh connection per session, so this runs constantly
    # under load. The policy itself lives in sqlite_policy so the standalone
    # scripts writing this same database apply it identically.
    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
      cur = dbapi_conn.cursor()
      for pragma in sqlite_policy.connection_pragmas():
        cur.execute(pragma)
      cur.close()
  return eng


engine = _make_engine()


def checked_out_connections() -> int:
  """Return live DB checkouts without depending on a concrete pool class."""
  with _pool_metrics_lock:
    return _pool_metrics["checked_out"]


SessionLocal = sessionmaker(
  autocommit=False, autoflush=False, bind=engine
)


def database_pool_snapshot() -> dict:
  """Owner-safe pool pressure and checkout-lifetime diagnostics."""
  pool = engine.pool

  def call_metric(name: str):
    method = getattr(pool, name, None)
    if not callable(method):
      return None
    try:
      return int(method())
    except (TypeError, ValueError):
      return None

  with _pool_metrics_lock:
    tracked_checked_out = _pool_metrics["checked_out"]
    lifetime = {
      "checkouts": _pool_metrics["checkouts"],
      "long_checkouts": _pool_metrics["long_checkouts"],
      "max_checkout_ms": _pool_metrics["max_checkout_ms"],
      "last_long_checkout": _pool_metrics["last_long_checkout"],
    }
  pool_checked_out = call_metric("checkedout")
  current = {
    # NullPool deliberately exposes no checkedout() method. The event-backed
    # counter is the authoritative cross-pool value in that case.
    "checked_out": (
      tracked_checked_out if pool_checked_out is None else pool_checked_out
    ),
    "checked_in": call_metric("checkedin"),
    "size": call_metric("size"),
    "overflow": call_metric("overflow"),
  }
  return {
    "type": type(pool).__name__,
    "current": {key: value for key, value in current.items() if value is not None},
    "lifetime": lifetime,
  }


class Base(DeclarativeBase):
  pass


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


def orm_schema_gaps(eng) -> list[str]:
  """ORM-mapped columns/tables the live database lacks (``table.column``).

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


_SCHEMA_MIGRATIONS = (
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


def ensure_migration_ledger(eng) -> None:
  """Create the durable one-shot ledger if it does not exist yet."""
  from sqlalchemy import text

  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE IF NOT EXISTS schema_migrations ("
      "version VARCHAR(128) PRIMARY KEY, "
      "applied_at TIMESTAMP NOT NULL"
      ")"
    ))


def migration_applied(eng, version: str) -> bool:
  """True when ``version`` has already completed on this database.

  One ledger answers "has this one-shot already run?" for every kind of
  migration. ``run_migrations`` drives the synchronous schema entries in
  ``_SCHEMA_MIGRATIONS`` through these same primitives; migrations that cannot
  live in that tuple — async ones, or ones doing network I/O such as fetching a
  catalog manifest — call them directly. Same table, same question, one
  implementation, so the ledger can never disagree with itself.

  Without a durable marker a "one-shot" migration can only infer completion
  from the shape of the rows it finds, which silently re-arms it for any row
  created LATER that happens to match that shape.
  """
  from sqlalchemy import inspect as sa_inspect, text

  if "schema_migrations" not in sa_inspect(eng).get_table_names():
    return False
  with eng.connect() as conn:
    return conn.execute(text(
      "SELECT 1 FROM schema_migrations WHERE version = :version"
    ), {"version": version}).first() is not None


def record_migration(eng, version: str) -> None:
  """Mark ``version`` complete so it never re-evaluates rows. Idempotent."""
  from sqlalchemy import text
  from sqlalchemy.exc import IntegrityError

  ensure_migration_ledger(eng)
  # Plain INSERT + IntegrityError rather than a dialect-specific upsert: this
  # ledger runs on both SQLite and PostgreSQL (Railway), and re-recording an
  # already-complete migration is a no-op either way.
  try:
    with eng.begin() as conn:
      conn.execute(text(
        "INSERT INTO schema_migrations (version, applied_at) "
        "VALUES (:version, :applied_at)"
      ), {
        "version": version,
        "applied_at": datetime.now(UTC).replace(tzinfo=None),
      })
  except IntegrityError:
    pass


def run_migrations(eng) -> None:
  """Apply each unapplied, append-only schema migration exactly once.

  The first migration freezes the historical inspector-based convergence path.
  Existing installs run it once and record the outcome; fresh installs record
  the same baseline after ``create_all``. Future schema work appends a named
  function to ``_SCHEMA_MIGRATIONS`` instead of extending a boot-time scan.

  Each migration remains internally idempotent so a crash before its ledger
  insert safely retries it. The ledger row is committed only after the migration
  returns successfully.

  Drives the shared ledger primitives (``migration_applied`` /
  ``record_migration``) rather than its own SQL, so a one-shot recorded here and
  one recorded by an async caller are the same fact in the same table.
  """
  from sqlalchemy import inspect as sa_inspect

  if "apps" not in sa_inspect(eng).get_table_names():
    return
  ensure_migration_ledger(eng)
  for version, migration in _SCHEMA_MIGRATIONS:
    if migration_applied(eng, version):
      continue
    migration(eng)
    record_migration(eng, version)


def get_db():
  """Yields a database session and closes it after the request."""
  db = SessionLocal()
  try:
    yield db
  finally:
    db.close()
