"""Failure-path guards for the agent-browser profile reaper.

The reaper runs `--delete` nightly on live prod. The one way it can destroy
value is by misreading which profiles belong to live chats, so these tests pin
the fail-closed contract: an unreadable chat database must delete nothing, and
the planned retirement of the `run_status` column must degrade rather than
flip selection to age-only.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPT = (
  Path(__file__).resolve().parent.parent
  / "scripts"
  / "agent-browser-profile-cleanup.py"
)


def _load_module():
  spec = importlib.util.spec_from_file_location("profile_cleanup", _SCRIPT)
  module = importlib.util.module_from_spec(spec)
  # Register before exec: the module defines dataclasses, and dataclass field
  # type resolution looks the module up in sys.modules by name.
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


cleanup = _load_module()


def _chats_db(path, *, with_run_status=True, rows=()):
  cols = "id, deleted_at, updated_at, activity_at"
  if with_run_status:
    cols += ", run_status"
  conn = sqlite3.connect(str(path))
  conn.execute(f"create table chats({cols})")
  for row in rows:
    conn.execute(
      f"insert into chats({cols}) values ({','.join('?' * len(row))})", row
    )
  conn.commit()
  conn.close()


def test_load_chats_raises_on_missing_db(tmp_path):
  with pytest.raises(cleanup.ChatDbUnavailable):
    cleanup._load_chats(tmp_path / "nope.db")


def test_load_chats_raises_on_corrupt_db(tmp_path):
  db = tmp_path / "corrupt.db"
  db.write_bytes(b"not a sqlite database" * 64)
  with pytest.raises(cleanup.ChatDbUnavailable):
    cleanup._load_chats(db)


def test_load_chats_raises_when_chats_table_absent(tmp_path):
  db = tmp_path / "no_table.db"
  sqlite3.connect(str(db)).close()
  with pytest.raises(cleanup.ChatDbUnavailable):
    cleanup._load_chats(db)


def test_load_chats_degrades_when_run_status_column_retired(tmp_path):
  # Step-3b drops the run_status column. The reaper must keep working with the
  # remaining columns, not read the drop as an unreadable database.
  db = tmp_path / "no_runstatus.db"
  _chats_db(
    db,
    with_run_status=False,
    rows=[("c1", None, "2026-06-01 00:00:00", "2026-06-01 00:00:00")],
  )
  chats = cleanup._load_chats(db)
  assert set(chats) == {"c1"}
  assert chats["c1"]["run_status"] is None


def test_load_chats_reads_a_healthy_db(tmp_path):
  db = tmp_path / "ok.db"
  _chats_db(
    db,
    rows=[("c1", None, "2026-06-01 00:00:00", "2026-06-01 00:00:00", "idle")],
  )
  chats = cleanup._load_chats(db)
  assert chats["c1"]["run_status"] == "idle"


def test_delete_against_unreadable_db_deletes_nothing(tmp_path, capsys):
  # An aged chat profile that WOULD be reaped if the db read succeeded.
  root = tmp_path / "profiles"
  profile = root / "chat-11111111-1111-1111-1111-111111111111"
  profile.mkdir(parents=True)

  rc = cleanup.main([
    "--root", str(root),
    "--db", str(tmp_path / "missing.db"),
    "--older-than-days", "0",
    "--include-existing-chats",
    "--delete",
  ])

  assert rc == 3, "delete against an unreadable db must fail closed"
  assert profile.exists(), "no profile may be removed when chat state is unknown"
  assert "refusing to delete" in capsys.readouterr().err


def test_dry_run_against_unreadable_db_reports_and_exits_zero(tmp_path):
  root = tmp_path / "profiles"
  (root / "chat-22222222-2222-2222-2222-222222222222").mkdir(parents=True)
  rc = cleanup.main([
    "--root", str(root),
    "--db", str(tmp_path / "missing.db"),
    "--include-existing-chats",
    "--json",
  ])
  assert rc == 0, "a report may still run against unknown chat state"


def test_delete_reaps_genuine_orphan_when_db_is_readable(tmp_path):
  # A readable db with zero chats is NOT an error: the profile really is an
  # orphan and may be reaped. This keeps the fail-closed guard from becoming a
  # never-delete guard.
  db = tmp_path / "empty.db"
  _chats_db(db, rows=())
  root = tmp_path / "profiles"
  orphan = root / "chat-33333333-3333-3333-3333-333333333333"
  orphan.mkdir(parents=True)
  rc = cleanup.main([
    "--root", str(root),
    "--db", str(db),
    "--older-than-days", "0",
    "--delete",
  ])
  assert rc == 0
  assert not orphan.exists(), "a true orphan profile should be reaped"
