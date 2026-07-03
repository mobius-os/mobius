"""Tests for recovery version + update-available (Phase 2).

Phase 2 of the recovery self-updating feature (see
docs/superpowers/plans/2026-07-03-recovery-self-updating.md). recoveryd
learns its own version and whether the pinned upstream has a newer
release — read-only surfacing, no apply yet.

These follow the recovery_env fixture pattern in test_recoveryd.py: the
env is set BEFORE re-importing so module-scope constants (DATA_DIR,
LIVE_DIR) pick it up, and RECOVERY_SKIP_INTEGRITY=1 gates the test-only
upstream-URL override. The upstream tests stand up a LOCAL bare git repo
with tags so no network is ever touched — offline is the default.
"""

import importlib
import subprocess
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
  pick it up. RECOVERY_SKIP_INTEGRITY=1 both bypasses the SELF check and
  gates the test-only upstream-URL override honored by _upstream_url.
  """
  data_dir = tmp_path
  (data_dir / "db").mkdir()
  db_path = data_dir / "db" / "ultimate.db"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
  monkeypatch.setenv("RECOVERY_SKIP_INTEGRITY", "1")
  # A stray override from another test's environment must not leak in.
  monkeypatch.delenv("RECOVERY_UPSTREAM_URL_TEST", raising=False)
  monkeypatch.delenv("MOBIUS_RECOVERY_EXECED", raising=False)
  for mod in ("recovery_auth", "recovery_db", "recovery_pages", "recoveryd"):
    sys.modules.pop(mod, None)
  return importlib.import_module("recoveryd")


def _run_git(*args: str) -> None:
  """Runs a git command, raising with captured output on failure."""
  subprocess.run(
    ["git", *args],
    check=True,
    capture_output=True,
    text=True,
  )


def _init_upstream(tmp_path: Path, tags) -> str:
  """Creates a bare git repo with `tags` pushed to it, returns its file:// URL.

  The launcher's upstream check runs `git ls-remote --tags --refs <url>`;
  a local bare repo reachable over file:// exercises the real command with
  zero network. Tags are created in a scratch working clone and pushed to
  the bare repo (a bare repo has no worktree to tag against directly).
  """
  bare = tmp_path / "upstream.git"
  _run_git("init", "--bare", "-q", str(bare))
  work = tmp_path / "upstream-work"
  _run_git("init", "-q", str(work))
  _run_git("-C", str(work), "config", "user.email", "t@example.com")
  _run_git("-C", str(work), "config", "user.name", "Test")
  (work / "VERSION").write_text("seed\n", encoding="utf-8")
  _run_git("-C", str(work), "add", "-A")
  _run_git("-C", str(work), "commit", "-q", "-m", "init")
  for tag in tags:
    _run_git("-C", str(work), "tag", tag)
  _run_git("-C", str(work), "remote", "add", "origin", str(bare))
  _run_git("-C", str(work), "push", "-q", "origin", "--tags")
  return f"file://{bare}"


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


# -- Task 2.2: _upstream_url seam ------------------------------------------

def test_upstream_url_is_pinned_constant(recoveryd, monkeypatch):
  # With no test override, the pinned constant is used.
  monkeypatch.delenv("RECOVERY_UPSTREAM_URL_TEST", raising=False)
  assert recoveryd._upstream_url() == recoveryd._RECOVERY_UPSTREAM_URL
  assert recoveryd._RECOVERY_UPSTREAM_URL == (
    "https://github.com/mobius-os/recovery.git"
  )


def test_upstream_url_override_honored_under_skip_integrity(recoveryd,
                                                            monkeypatch):
  # The test-only override is honored ONLY together with
  # RECOVERY_SKIP_INTEGRITY=1 (set by the fixture).
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", "file:///tmp/whatever")
  assert recoveryd._upstream_url() == "file:///tmp/whatever"


def test_upstream_url_override_ignored_without_skip_integrity(recoveryd,
                                                              monkeypatch):
  # Production never sets RECOVERY_SKIP_INTEGRITY, so the override is inert
  # and the pinned constant wins — the URL can never be repointed in prod.
  monkeypatch.delenv("RECOVERY_SKIP_INTEGRITY", raising=False)
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", "file:///tmp/evil")
  assert recoveryd._upstream_url() == recoveryd._RECOVERY_UPSTREAM_URL


# -- Task 2.2: latest_upstream_version + update_available ------------------

def test_latest_upstream_version_reads_tags(recoveryd, monkeypatch, tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  # Highest tag wins, returned without the leading 'v'.
  assert recoveryd.latest_upstream_version() == "0.2.0"


def test_latest_upstream_version_accepts_unprefixed_tags(recoveryd, monkeypatch,
                                                         tmp_path):
  url = _init_upstream(tmp_path / "up", ["0.1.0", "0.3.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  assert recoveryd.latest_upstream_version() == "0.3.0"


def test_latest_upstream_version_semver_double_digit(recoveryd, monkeypatch,
                                                     tmp_path):
  # String sort would rank 0.9.0 above 0.10.0; semver compare must not.
  url = _init_upstream(tmp_path / "up", ["v0.9.0", "v0.10.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  assert recoveryd.latest_upstream_version() == "0.10.0"


def test_latest_upstream_version_ignores_non_semver_tags(recoveryd, monkeypatch,
                                                         tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "nightly", "release-2"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  assert recoveryd.latest_upstream_version() == "0.1.0"


def test_latest_upstream_version_none_when_unreachable(recoveryd, monkeypatch,
                                                       tmp_path):
  # A file:// path with no repo behind it fails ls-remote; offline-safe ->
  # None, never a raise.
  missing = tmp_path / "nonexistent.git"
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", f"file://{missing}")
  assert recoveryd.latest_upstream_version() is None


def test_latest_upstream_version_none_on_timeout(recoveryd, monkeypatch):
  # A ls-remote that exceeds the timeout must resolve to None, never hang
  # or raise (offline-safe).
  def boom(*a, **k):
    raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

  monkeypatch.setattr(recoveryd.subprocess, "run", boom)
  assert recoveryd.latest_upstream_version() is None


def test_update_available_true_when_running_older(recoveryd, monkeypatch,
                                                  tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.1.0")
  assert recoveryd.update_available() is True


def test_update_available_false_when_up_to_date(recoveryd, monkeypatch,
                                                tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.2.0")
  # Equal versions is not an update.
  assert recoveryd.update_available() is False


def test_update_available_false_when_running_newer(recoveryd, monkeypatch,
                                                   tmp_path):
  url = _init_upstream(tmp_path / "up", ["v0.1.0", "v0.2.0"])
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", url)
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.9.0")
  assert recoveryd.update_available() is False


def test_update_available_false_when_offline(recoveryd, monkeypatch, tmp_path):
  # No upstream reachable -> latest is None -> no update, never a raise.
  missing = tmp_path / "nonexistent.git"
  monkeypatch.setenv("RECOVERY_UPSTREAM_URL_TEST", f"file://{missing}")
  monkeypatch.setattr(recoveryd, "running_version", lambda: "0.1.0")
  assert recoveryd.update_available() is False
