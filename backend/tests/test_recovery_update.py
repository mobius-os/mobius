"""Tests for recovery version + update-available (Phase 2).

Phase 2 of the recovery self-updating feature (see
docs/superpowers/plans/2026-07-03-recovery-self-updating.md). recoveryd
learns its own version and whether the pinned upstream has a newer
release — read-only surfacing, no apply yet.

These follow the recovery_env fixture pattern in test_recoveryd.py: the
env is set BEFORE re-importing so module-scope constants (DATA_DIR,
LIVE_DIR) pick it up, and RECOVERY_SKIP_INTEGRITY=1 gates the test-only
upstream-URL override.
"""

import importlib
import sys
from pathlib import Path

import pytest

# The frozen bundle ships at backend/recovery/. Put it on sys.path so the
# stdlib-only modules import the same way they do inside /app/recovery.
_RECOVERY_DIR = Path(__file__).resolve().parents[1] / "recovery"
if str(_RECOVERY_DIR) not in sys.path:
  sys.path.insert(0, str(_RECOVERY_DIR))


@pytest.fixture()
def recoveryd(monkeypatch, tmp_path):
  """Freshly-imported recoveryd bound to an isolated DATA_DIR.

  Mirrors the recovery_env fixture in test_recoveryd.py: sets the env
  BEFORE re-importing so module-scope path constants (DATA_DIR, LIVE_DIR)
  pick it up. RECOVERY_SKIP_INTEGRITY=1 bypasses the SELF check.
  """
  data_dir = tmp_path
  (data_dir / "db").mkdir()
  db_path = data_dir / "db" / "ultimate.db"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
  monkeypatch.setenv("RECOVERY_SKIP_INTEGRITY", "1")
  monkeypatch.delenv("MOBIUS_RECOVERY_EXECED", raising=False)
  for mod in ("recovery_auth", "recovery_db", "recovery_pages", "recoveryd"):
    sys.modules.pop(mod, None)
  return importlib.import_module("recoveryd")


def _trusted_live(recoveryd, monkeypatch, version: str | None) -> Path:
  """Populates LIVE_DIR with a recoveryd.py (+ optional VERSION) and makes
  ownership look root-owned (0644) so bundle_is_trusted passes and
  resolve_run_dir() selects it as the running bundle."""
  live = recoveryd.LIVE_DIR
  live.mkdir(parents=True)
  (live / "recoveryd.py").write_text("# live\n", encoding="utf-8")
  if version is not None:
    (live / "VERSION").write_text(version, encoding="utf-8")
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o644))
  return live


# -- Task 2.1: running_version ---------------------------------------------

def test_running_version_reads_version_file(recoveryd, monkeypatch):
  # A trusted live copy carrying VERSION becomes the running bundle, and
  # running_version reads that file's trimmed contents.
  _trusted_live(recoveryd, monkeypatch, "1.2.3\n")
  assert recoveryd.running_version() == "1.2.3"


def test_running_version_defaults_when_absent(recoveryd, monkeypatch):
  # A running bundle with no VERSION file resolves to the lowest version so
  # any tagged upstream release looks newer.
  _trusted_live(recoveryd, monkeypatch, None)
  assert recoveryd.running_version() == "0.0.0"


def test_running_version_defaults_when_empty(recoveryd, monkeypatch):
  # A blank VERSION file is not a valid version — fall back to "0.0.0".
  _trusted_live(recoveryd, monkeypatch, "   \n")
  assert recoveryd.running_version() == "0.0.0"


def test_baked_bundle_ships_version(recoveryd):
  # The baked floor (no live copy on a fresh volume) must carry a VERSION
  # file so a stock recovery reports a real version, not the "0.0.0"
  # default.
  assert recoveryd.running_version() == "0.1.0"
