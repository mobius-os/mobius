"""Private cross-seam rehearsal: real transcript gate -> Host rollback floor.

No Docker, systemd, installed helper, or live data is touched. This is not a
substitute for the disposable-host recipe in scripts/TRANSCRIPT_HOST_TEST.md.
"""

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from app import one_way_upgrades as upgrades
from app.database import Base
from app.schema_migrations import _create_chat_search_tables


ROOT = Path(__file__).resolve().parents[2]


def _host():
  spec = importlib.util.spec_from_file_location(
    "private_transcript_host", ROOT / "scripts/mobius-rebuild-host.py")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _historical_host(tmp_path):
  """Load the committed prior worker, without copying a fake implementation."""
  source = subprocess.run(
    ["git", "show", "36c0ce1016:scripts/mobius-rebuild-host.py"],
    cwd=ROOT, capture_output=True, check=True).stdout
  path = tmp_path / "worker-revision-2.py"
  path.write_bytes(source)
  spec = importlib.util.spec_from_file_location("private_prior_host_worker", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  assert module.WORKER_REVISION == 2
  return module


def _legacy(tmp_path):
  # Step-level hybrid fixture, not a complete previous-release image database.
  path = tmp_path / "ultimate.db"
  engine = create_engine(f"sqlite:///{path}")
  Base.metadata.create_all(engine)
  _create_chat_search_tables(engine)
  # Noncanonical JSON bytes and duplicate IDs deliberately exercise exact
  # archive survival, not a normalized JSON equality shortcut.
  raw = b'[ {"role":"user", "id":"same", "content":"before"},\n' \
        b'  {"role":"assistant", "id":"same", "content":"after"} ]'
  with engine.begin() as connection:
    connection.exec_driver_sql("ALTER TABLE chats RENAME COLUMN messages_v1 TO messages")
    connection.exec_driver_sql(
      "INSERT INTO chats(id,title,title_locked,messages,has_messages,"
      "pending_messages,uploads,provider,auto_resume_on_limit,"
      "auto_resume_on_restart,created_at,updated_at) "
      "VALUES('cutover','test',0,?,1,'[]','[]','claude',0,1,"
      "'2026-10-03','2026-10-03')", (raw.decode(),))
  seen = upgrades.preflight(engine)
  upgrades.ensure_compat_record(str(path), seen)
  engine.dispose()
  return path, raw, seen


def _floor(host, path):
  result = subprocess.run(
    [sys.executable, "-I", "-c", host.FLOOR_PROBE, str(path)],
    capture_output=True, text=True, check=True)
  return int(result.stdout.strip().removeprefix("floor="))


def test_exact_transcript_archive_survives_and_old_host_rollback_is_refused(
    tmp_path, monkeypatch):
  path, raw, seen = _legacy(tmp_path)
  host = _host()
  assert _floor(host, path) == 0
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as connection:
    rows = [json.loads(body) for (body,) in connection.execute(
      "SELECT body FROM chat_messages WHERE chat_id='cutover' ORDER BY seq")]
    assert rows == json.loads(raw)
    assert connection.execute(
      "SELECT messages_v1 FROM chats WHERE id='cutover'").fetchone()[0].encode() == raw
    assert upgrades.verified_legacy_copy(connection, 1, "cutover") == raw
  assert _floor(host, path) == 1

  # Exercise the actual Host rollback policy after activation. Docker's
  # image/readonly-mount probes are represented by their exact results; no
  # replacement may be attempted once the baked old image is below the floor.
  events = []
  monkeypatch.setattr(host, "fence_app", lambda *_: events.append("fence") or True)
  monkeypatch.setattr(host, "rollback_image_level", lambda *_: 0)
  monkeypatch.setattr(host, "read_database_floor", lambda *_: 1)
  monkeypatch.setattr(host, "write_status", lambda *a, **kw: events.append(kw))
  monkeypatch.setattr(host, "compose", lambda *a, **kw: events.append(("compose", a)))
  monkeypatch.setattr(host.subprocess, "run", lambda *a, **kw: events.append("start"))
  # The refused rollback settles forward; here the new container never
  # becomes ready again, so the outcome stays needs_recovery.
  monkeypatch.setattr(host, "TRANSACTION", tmp_path / "transaction.json")
  monkeypatch.setattr(host, "restart_ledger", lambda *a, **kw: True)
  monkeypatch.setattr(host, "wait_ready", lambda *a, **kw: ("timeout", None))
  # No stopped app container is found, so nothing may be started.
  monkeypatch.setattr(host, "app_container_any", lambda *_: "")
  outcome = host.rollback({"data_dir": str(tmp_path)}, "op", "a" * 40,
                          "health_check_failed", "candidate unhealthy")
  assert outcome == 1
  assert "fence" in events
  assert not any(isinstance(event, tuple) and "up" in event[1] for event in events)
  assert events[-1]["state"] == "needs_recovery"
  assert events[-1]["code"] == "newer_version_required"


class UnsafeFirstUpgradeRollback(AssertionError):
  """Only the observed old-image start is this rehearsal's known failure."""


@pytest.mark.xfail(
  strict=True,
  raises=UnsafeFirstUpgradeRollback,
  reason="Known first-upgrade defect: installed 36c0ce1016 worker REV2 has no rollback floor preflight",
)
def test_installed_prior_worker_must_not_start_level_zero_after_activation(
    tmp_path, monkeypatch):
  """Red until a floor-aware worker is active *before* the first level-1 gate.

  The previous release's real worker (not the candidate REV4 worker) performs
  the first replacement. Its Docker boundary is mocked, but the gate and
  SQLite floor probe are real. No host/container side effects occur.
  """
  path, _raw, seen = _legacy(tmp_path)
  upgrades.run_gate(str(path), seen.existing_tables)
  prior = _historical_host(tmp_path)
  # REV2 has no FLOOR_PROBE; use the current identical host-side probe solely
  # to establish the disposable DB's activated floor before invoking REV2.
  assert _floor(_host(), path) == 1
  calls = []
  monkeypatch.setattr(prior, "write_status", lambda *a, **kw: None)
  monkeypatch.setattr(prior, "app_container", lambda *_: ("failed-candidate", "sha256:candidate"))
  monkeypatch.setattr(prior, "restart_ledger", lambda *a, **kw: True)
  monkeypatch.setattr(prior, "compose", lambda *a, **kw: calls.append(a) or None)
  monkeypatch.setattr(prior, "wait_healthy", lambda *a, **kw: False)
  prior.rollback({"data_dir": str(tmp_path)}, "operation", "a" * 40,
                 "health_check_failed", "candidate failed", "sha256:previous")
  if any("up" in args and "--force-recreate" in args for args in calls):
    raise UnsafeFirstUpgradeRollback(
      "installed REV2 worker attempted to start its level-0 rollback image after floor 1"
    )
