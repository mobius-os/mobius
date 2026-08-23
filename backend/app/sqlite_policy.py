"""SQLite connection policy for the live runtime.

One place that decides how this platform's SQLite connections behave, so the
SQLAlchemy engine and the standalone scripts that open the same database
cannot drift apart. Deliberately stdlib-only and import-light: `database`
builds an engine at module load, so a script needing this policy must be able
to reach it without paying for that.
"""

import sqlite3
from datetime import date, datetime

# Waits for a lock instead of raising "database is locked" the moment two
# writers overlap. Must be installed before any pragma that can itself take a
# lock — see connection_pragmas.
BUSY_TIMEOUT_MS = 5000

# Caps RETAINED journal size, not live growth. SQLite defaults this to -1
# (never truncate), so a WAL only ratchets upward: a checkpoint returns pages
# to the database but leaves the file at its high-water mark, and that
# allocation is kept forever. With a limit set, a checkpoint that resets the
# WAL also truncates the file back down.
#
# It is not a runtime ceiling. Truncation happens only on a reset, and a reset
# needs a gap where no reader holds an older snapshot — so a single long-lived
# read transaction is enough to let the file exceed this, regardless of how
# many sessions are running. If it grows again, look for the transaction that
# never ends rather than the session count.
#
# 64 MiB trades a little repeated growth work for a bound small against the
# data volume. No formula: raise it if checkpoint churn shows up in write
# latency, lower it if retained journal space becomes material.
RETAINED_JOURNAL_LIMIT_BYTES = 64 * 1024 * 1024


def install_adapters() -> None:
  """Keep raw SQL datetime parameters stable after Python removes defaults.

  SQLAlchemy converts values for typed columns itself, but migration and
  maintenance statements intentionally use ``text()`` and therefore reach the
  DBAPI without column-type processors. Python 3.12 deprecated sqlite3's
  implicit date/datetime adapters. Register the same ISO encodings explicitly
  so those durable statements do not depend on a disappearing interpreter
  default.
  """
  sqlite3.register_adapter(date, lambda value: value.isoformat())
  sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))


def connection_pragmas() -> tuple[str, ...]:
  """The ordered PRAGMAs every live-runtime SQLite connection must run.

  Returned as statements rather than applied to a connection because the
  callers hold different objects — SQLAlchemy hands out a DBAPI cursor, the
  standalone scripts a `sqlite3.Connection` — and the ordering is the part
  that must not be duplicated.

  Order is load-bearing: `busy_timeout` first, because `journal_mode=WAL`
  takes locks and with no busy handler installed it fails immediately instead
  of waiting when another connection holds them.

  `synchronous=FULL` fsyncs the WAL on every commit, so an OOM kill (which
  this host suffers periodically) or power loss cannot leave the last commits
  in the page cache but not on disk. NORMAL skips that fsync and risks losing
  the last committed transaction; FULL costs roughly one fsync per write
  transaction, acceptable at this platform's write rate.
  """
  return (
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    f"PRAGMA journal_size_limit={RETAINED_JOURNAL_LIMIT_BYTES}",
  )
