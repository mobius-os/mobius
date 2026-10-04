#!/usr/bin/env python3
"""Measure the real transcript gate on a disposable copy, not a serving DB.

Usage: python scripts/test-transcript-migration.py legacy-copy.db new-run-dir
The input is opened read-only; SQLite backup preserves a coherent snapshot.
Only aggregate timings/counts leave the run directory. Exit 3 means conversion
was correct but exceeded the explicit readiness budget (default 120 seconds).
This host-only test does not prove image compatibility or container cutover.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path


def readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def require_offline_copy(source: Path) -> None:
    """Keep inherited serving paths out of this disposable rehearsal."""
    from sqlalchemy.engine import make_url
    live_directory = Path(os.environ.get("DATA_DIR", "/data")) / "db"
    if source.is_relative_to(live_directory.resolve()) or source.is_relative_to(Path("/data/db")):
        raise ValueError("Use an offline copy outside the serving database directory")
    configured = os.environ.get("DATABASE_URL", "")
    if configured:
        url = make_url(configured)
        if url.get_backend_name() == "sqlite" and url.database and source == Path(url.database).resolve():
            raise ValueError("The input is the configured serving database; use an offline copy")


@contextmanager
def copied_snapshot(source: Path, target: Path):
    """Backup and verification share the exact same pinned read snapshot."""
    with closing(readonly(source)) as original:
        original.execute("BEGIN")
        original.execute("SELECT COUNT(*) FROM chats").fetchone()
        with closing(sqlite3.connect(target)) as copied:
            original.backup(copied)
        try:
            yield original
        finally:
            original.rollback()


def same_value(left: object, right: object) -> bool:
    # Python considers True == 1 and 1 == 1.0; stored JSON types matter too.
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False
    )


def verify(original: sqlite3.Connection, target: sqlite3.Connection, upgrades) -> dict:
    chats = messages = damaged = 0
    for cid, raw in original.execute("SELECT id,CAST(messages AS BLOB) FROM chats ORDER BY id"):
        raw = bytes(raw or b"")
        if upgrades.verified_legacy_copy(target, 1, cid) != raw:
            raise AssertionError("Preserved transcript archive does not match")
        try:
            expected = json.loads(raw)
        except (ValueError, UnicodeError):
            expected = None
        if not isinstance(expected, list):
            from app.transcript_upgrade import TranscriptStep
            expected = TranscriptStep().damaged_messages()
            damaged += 1
        cursor = target.execute(
            "SELECT seq,body FROM chat_messages WHERE chat_id=? ORDER BY seq", (cid,)
        )
        for seq, message in enumerate(expected):
            row = cursor.fetchone()
            if row is None or row[0] != seq or not same_value(json.loads(row[1]), message):
                raise AssertionError("Transcript value, type or position changed")
            messages += 1
        if cursor.fetchone() is not None:
            raise AssertionError("Unexpected extra transcript row")
        state = target.execute(
            "SELECT message_count FROM chat_transcript_state WHERE chat_id=?", (cid,)
        ).fetchone()
        if state is None or state[0] != len(expected):
            raise AssertionError("Transcript count changed")
        chats += 1
    if target.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] != messages:
        raise AssertionError("Unexpected message outside the copied chats")
    if target.execute("SELECT floor FROM platform_compat WHERE id=1").fetchone() != (1,):
        raise AssertionError("Conversion did not activate level 1")
    return {"chats": chats, "messages": messages, "damaged_chats": damaged,
            "all_message_values_types_and_positions_equal": True,
            "all_original_bytes_archived_exactly": True}


def run(source: Path, output: Path, budget: float, post_tasks: bool) -> tuple[dict, int]:
    # Neither imports nor schema writes may bind an inherited serving engine.
    source = source.resolve(strict=True)
    require_offline_copy(source)
    with closing(readonly(source)) as original:
        columns = {row[1] for row in original.execute("PRAGMA table_info(chats)")}
        if "messages" not in columns:
            raise ValueError("Input must be a legacy transcript copy with chats.messages")
    output.mkdir(parents=True, exist_ok=False)
    target = output / "migration.db"
    runtime = output / "runtime"
    os.environ.update(
        DATABASE_URL=f"sqlite:///{target}", DATA_DIR=str(runtime),
        MOBIUS_TEST_RUNTIME="1", MOBIUS_TEST_DATABASE_ISOLATED="1",
        MOBIUS_TEST_IMAGE_LEVEL="1", SECRET_KEY="isolated-migration-fixture-not-a-live-secret",
        DOMAIN="localhost", FRONTEND_ORIGIN="http://localhost:5173",
        API_BASE_URL="http://127.0.0.1:9", MOBIUS_APP_BASE=str(runtime / "apps"),
        MOBIUS_SSO_ISSUER="", MOBIUS_SSO_INSTANCE_ID="",
        MOBIUS_IDENTITY_BROKER_SOCKET=str(runtime / "no-broker.sock"),
        MOEBIUS_SKIP_BOOTSTRAP="1",
    )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    copy_start = time.monotonic()
    with copied_snapshot(source, target) as original:
        return measure_copy(original, target, output, budget, post_tasks, copy_start)


def measure_copy(original, target, output, budget, post_tasks, copy_start):
    copy_seconds = time.monotonic() - copy_start
    boot_start = time.monotonic()
    from app import models  # noqa: F401
    from app import one_way_upgrades as upgrades
    from app.database import Base, engine
    from app.schema_migrations import _create_chat_search_tables
    seen = upgrades.preflight(engine)
    upgrades.ensure_compat_record(str(target), seen)
    Base.metadata.create_all(engine)
    _create_chat_search_tables(engine)
    engine.dispose()
    gate_start = time.monotonic()
    upgrades.run_gate(str(target), seen.existing_tables)
    gate_seconds = time.monotonic() - gate_start
    readiness_seconds = time.monotonic() - boot_start
    verification_start = time.monotonic()
    with closing(readonly(target)) as converted:
        values = verify(original, converted, upgrades)
    verification_seconds = time.monotonic() - verification_start
    post_seconds = None
    batches = 0
    if post_tasks:
        post_start = time.monotonic()
        while upgrades.run_post_activation_batch(str(target)):
            batches += 1
        post_seconds = time.monotonic() - post_start
        with closing(readonly(target)) as converted:
            verify(original, converted, upgrades)
            if converted.execute("SELECT COUNT(*) FROM upgrade_tasks WHERE status<>'done'").fetchone()[0]:
                raise AssertionError("Post-activation tasks did not finish")
    report = {**values, "copy_seconds": copy_seconds, "gate_seconds": gate_seconds,
              "application_gate_seconds": readiness_seconds,
              "verification_seconds": verification_seconds,
              "readiness_budget_seconds": budget,
              "application_gate_within_budget": readiness_seconds <= budget,
              "post_seconds": post_seconds, "post_batches": batches,
              "database_bytes": target.stat().st_size,
              "free_disk_bytes": shutil.disk_usage(output).free,
              "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "scope": "host-only application gate; excludes container startup and host replacement",
              "image_capability": "simulated level 1, not image validation"}
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report, 0 if readiness_seconds <= budget else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("legacy_copy", type=Path)
    parser.add_argument("new_run_directory", type=Path)
    parser.add_argument("--readiness-budget-seconds", type=float, default=120)
    parser.add_argument("--post-tasks", action="store_true")
    args = parser.parse_args()
    if not 0 < args.readiness_budget_seconds < float("inf"):
        parser.error("readiness budget must be finite and positive")
    report, code = run(args.legacy_copy, args.new_run_directory.resolve(),
                       args.readiness_budget_seconds, args.post_tasks)
    print(json.dumps(report), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
