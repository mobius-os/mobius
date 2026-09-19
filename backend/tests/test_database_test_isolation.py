"""A test process cannot inherit an unmarked application database."""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app import database


def test_test_database_requires_disposable_runtime_marker(monkeypatch):
  monkeypatch.setenv("MOBIUS_TEST_RUNTIME", "1")
  monkeypatch.delenv("MOBIUS_TEST_DATABASE_ISOLATED", raising=False)

  with pytest.raises(RuntimeError, match="unisolated test process"):
    database._assert_test_database_isolated()


def test_disposable_runtime_marker_allows_test_database(monkeypatch):
  monkeypatch.setenv("MOBIUS_TEST_RUNTIME", "1")
  monkeypatch.setenv("MOBIUS_TEST_DATABASE_ISOLATED", "1")

  database._assert_test_database_isolated()


def test_fixtures_reject_engine_imported_before_their_disposable_database(tmp_path):
  """An isolation flag cannot attest an already-cached engine's destination."""
  sentinel = tmp_path / "unrelated.sqlite"
  with sqlite3.connect(sentinel) as db:
    db.execute("CREATE TABLE keep_me (value TEXT)")
    db.execute("INSERT INTO keep_me VALUES ('untouched')")
  backend = Path(__file__).parents[1]
  result = subprocess.run(
    [sys.executable, "-c", (
      "import runpy; from app.database import engine; "
      "runpy.run_path('tests/conftest.py')"
    )],
    cwd=backend,
    env={**os.environ, "DATABASE_URL": f"sqlite:///{sentinel}",
         "DATA_DIR": str(tmp_path), "MOBIUS_TEST_DATABASE_ISOLATED": "1"},
    capture_output=True, text=True, timeout=30,
  )
  assert result.returncode != 0
  assert "fixture-owned database" in result.stderr
  with sqlite3.connect(sentinel) as db:
    assert db.execute("SELECT value FROM keep_me").fetchall() == [("untouched",)]


def test_host_runner_drops_managed_identity_before_test_collection(tmp_path):
  """Exercise the real shell entrypoint, before any backend fixture imports."""
  probe = tmp_path / "test_managed_environment.py"
  probe.write_text('''import os
from pathlib import Path
from app.config import get_settings

def test_isolated():
  assert os.environ["MOBIUS_SSO_ISSUER"] == ""
  assert os.environ["MOBIUS_SSO_INSTANCE_ID"] == ""
  socket = Path(os.environ["MOBIUS_IDENTITY_BROKER_SOCKET"])
  assert socket.parent.name.startswith("mobius-wt-pytest.")
  assert not socket.exists()
  assert get_settings().api_base_url == "http://127.0.0.1:9"
  assert get_settings().data_dir.startswith(str(socket.parent))
''')
  root = Path(__file__).parents[2]
  result = subprocess.run(
    [str(root / "scripts/wt-pytest.sh"), "--noconftest", str(probe), "-q"],
    cwd=root,
    env={**os.environ, "MOBIUS_SSO_ISSUER": "https://managed.invalid",
         "MOBIUS_SSO_INSTANCE_ID": "synthetic-live-instance",
         "MOBIUS_IDENTITY_BROKER_SOCKET": str(tmp_path / "live-broker.sock")},
    capture_output=True, text=True, timeout=60,
  )
  assert result.returncode == 0, result.stdout + result.stderr
