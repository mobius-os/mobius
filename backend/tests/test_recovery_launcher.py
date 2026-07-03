"""Tests for the recovery launcher — trusted-copy selection + re-exec.

Phase 1 of the recovery self-updating feature (see
docs/superpowers/plans/2026-07-03-recovery-self-updating.md). recoveryd
runs as root and must NEVER exec code from an agent-writable path, so the
launcher prefers a root-owned, integrity-checked /data/recovery-live copy
and otherwise runs the baked floor.

The test process runs as a normal user and cannot create uid-0 files, so
these tests monkeypatch recoveryd._uid_and_mode — the single seam the
production code uses to look up a path's ownership and mode — to simulate
both trusted (root-owned) and untrusted bundles deterministically. The
production path always calls the real os.stat via _uid_and_mode; the
trust decision is never env-bypassable.
"""

import importlib
import os
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
  pick it up. RECOVERY_SKIP_INTEGRITY=1 bypasses only the SELF check;
  bundle_is_trusted (which the launcher tests exercise) ignores it.
  """
  data_dir = tmp_path
  (data_dir / "db").mkdir()
  db_path = data_dir / "db" / "ultimate.db"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
  monkeypatch.setenv("RECOVERY_SKIP_INTEGRITY", "1")
  # Own the exec-loop sentinel so any value the code sets during a test is
  # reverted at teardown (os.environ mutations by the code under test are
  # otherwise invisible to monkeypatch and would leak across tests).
  monkeypatch.delenv("MOBIUS_RECOVERY_EXECED", raising=False)
  for mod in ("recovery_auth", "recovery_db", "recovery_pages", "recoveryd"):
    sys.modules.pop(mod, None)
  return importlib.import_module("recoveryd")


def _write_py(dir_path: Path, name: str = "recoveryd.py",
              content: str = "# recovery\n") -> Path:
  dir_path.mkdir(parents=True, exist_ok=True)
  p = dir_path / name
  p.write_text(content)
  return p


# -- Task 1.1: bundle_is_trusted -------------------------------------------

def test_bundle_is_trusted_missing_dir(recoveryd, tmp_path):
  assert recoveryd.bundle_is_trusted(tmp_path / "nope") is False


def test_bundle_is_trusted_rejects_non_root_owner(recoveryd, tmp_path,
                                                  monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d)
  # Simulate a mobius-owned (non-root) file — the exact thing the agent
  # could plant on /data.
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (1000, 0o644))
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_rejects_group_writable(recoveryd, tmp_path,
                                                  monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d)
  # Root-owned but group-writable: the agent could rewrite it if it shares
  # the group, so it is not trusted.
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o664))
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_rejects_other_writable(recoveryd, tmp_path,
                                                  monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d)
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o646))
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_accepts_root_readonly(recoveryd, tmp_path,
                                                 monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d, "recoveryd.py")
  _write_py(d, "recovery_auth.py")
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o644))
  assert recoveryd.bundle_is_trusted(d) is True


def test_bundle_is_trusted_any_untrusted_file_fails_bundle(recoveryd, tmp_path,
                                                           monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d, "recoveryd.py")
  _write_py(d, "evil.py")
  owners = {"recoveryd.py": (0, 0o644), "evil.py": (1000, 0o644)}
  monkeypatch.setattr(
    recoveryd, "_uid_and_mode", lambda p: owners[os.path.basename(p)])
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_catches_nested_py(recoveryd, tmp_path, monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d, "recoveryd.py")
  _write_py(d / "sub", "planted.py")
  owners = {"recoveryd.py": (0, 0o644), "planted.py": (1000, 0o644)}
  monkeypatch.setattr(
    recoveryd, "_uid_and_mode", lambda p: owners[os.path.basename(p)])
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_never_raises_on_stat_error(recoveryd, tmp_path,
                                                      monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d)

  def boom(p):
    raise OSError("stat failed")

  monkeypatch.setattr(recoveryd, "_uid_and_mode", boom)
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_empty_dir_is_untrusted(recoveryd, tmp_path):
  d = tmp_path / "empty"
  d.mkdir()
  # A directory with no Python is not a runnable bundle — nothing to
  # verify, nothing to run — so it is untrusted rather than vacuously OK.
  assert recoveryd.bundle_is_trusted(d) is False


# -- Task 3.0: directory ownership is part of the trust rule ----------------

def test_bundle_is_trusted_accepts_root_dirs_and_files(recoveryd, tmp_path,
                                                       monkeypatch):
  # Happy path with a subdirectory: every DIR and every FILE is root-owned
  # and not group/other-writable -> trusted.
  d = tmp_path / "bundle"
  _write_py(d, "recoveryd.py")
  _write_py(d / "sub", "helper.py")
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o755))
  assert recoveryd.bundle_is_trusted(d) is True


def test_bundle_is_trusted_rejects_group_writable_subdir(recoveryd, tmp_path,
                                                         monkeypatch):
  # Every *.py is root-owned + read-only (0444), but a SUBDIRECTORY is
  # group/other-writable: the agent could unlink the root-owned file inside
  # it and drop its own, or win a TOCTOU race between this check and exec —
  # so a writable dir voids the trust even with all files locked down.
  d = tmp_path / "bundle"
  _write_py(d, "recoveryd.py")
  _write_py(d / "sub", "helper.py")

  def fake(p):
    if os.path.basename(p) == "sub":
      return (0, 0o775)  # root-owned but group/other-writable directory
    return (0, 0o444)    # root-owned read-only (files + the bundle root)

  monkeypatch.setattr(recoveryd, "_uid_and_mode", fake)
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_rejects_other_writable_subdir(recoveryd, tmp_path,
                                                         monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d, "recoveryd.py")
  _write_py(d / "sub", "helper.py")

  def fake(p):
    if os.path.basename(p) == "sub":
      return (0, 0o707)  # other-writable directory
    return (0, 0o444)

  monkeypatch.setattr(recoveryd, "_uid_and_mode", fake)
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_rejects_non_root_bundle_dir(recoveryd, tmp_path,
                                                       monkeypatch):
  # A root-owned FILE inside an agent-OWNED bundle dir is still unsafe: the
  # agent owns the directory and can replace its entries.
  d = tmp_path / "bundle"
  _write_py(d, "recoveryd.py")

  def fake(p):
    if os.path.basename(p) == "bundle":
      return (1000, 0o755)  # the agent owns the bundle directory itself
    return (0, 0o444)

  monkeypatch.setattr(recoveryd, "_uid_and_mode", fake)
  assert recoveryd.bundle_is_trusted(d) is False


def test_bundle_is_trusted_rejects_non_root_subdir(recoveryd, tmp_path,
                                                   monkeypatch):
  d = tmp_path / "bundle"
  _write_py(d, "recoveryd.py")
  _write_py(d / "sub", "helper.py")

  def fake(p):
    if os.path.basename(p) == "sub":
      return (1000, 0o755)  # the agent owns a subdirectory
    return (0, 0o444)

  monkeypatch.setattr(recoveryd, "_uid_and_mode", fake)
  assert recoveryd.bundle_is_trusted(d) is False


# -- Task 1.1: _assert_self_integrity delegates to bundle_is_trusted -------

def test_assert_self_integrity_skips_with_env(recoveryd, monkeypatch):
  monkeypatch.setenv("RECOVERY_SKIP_INTEGRITY", "1")
  monkeypatch.setattr(recoveryd, "bundle_is_trusted", lambda d: False)
  # The SELF check is bypassable for tests/dev; must NOT raise.
  recoveryd._assert_self_integrity()


def test_assert_self_integrity_raises_when_untrusted(recoveryd, monkeypatch):
  monkeypatch.delenv("RECOVERY_SKIP_INTEGRITY", raising=False)
  monkeypatch.setattr(recoveryd, "bundle_is_trusted", lambda d: False)
  with pytest.raises(SystemExit):
    recoveryd._assert_self_integrity()


def test_assert_self_integrity_passes_when_trusted(recoveryd, monkeypatch):
  monkeypatch.delenv("RECOVERY_SKIP_INTEGRITY", raising=False)
  monkeypatch.setattr(recoveryd, "bundle_is_trusted", lambda d: True)
  recoveryd._assert_self_integrity()


# -- Task 1.2: resolve_run_dir ---------------------------------------------

def _trusted_live(recoveryd, monkeypatch) -> Path:
  """Populates LIVE_DIR with a recoveryd.py and makes ownership look
  root-owned (0644) so bundle_is_trusted passes."""
  live = recoveryd.LIVE_DIR
  live.mkdir(parents=True)
  (live / "recoveryd.py").write_text("# live\n")
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o644))
  return live


def test_resolve_run_dir_baked_when_no_live(recoveryd):
  # No live copy on the fresh isolated volume -> baked self dir.
  assert recoveryd.resolve_run_dir() == str(recoveryd._SELF_DIR)


def test_resolve_run_dir_live_when_trusted(recoveryd, monkeypatch):
  live = _trusted_live(recoveryd, monkeypatch)
  assert recoveryd.resolve_run_dir() == str(live)


def test_resolve_run_dir_baked_when_live_untrusted(recoveryd, monkeypatch):
  live = recoveryd.LIVE_DIR
  live.mkdir(parents=True)
  (live / "recoveryd.py").write_text("# live\n")
  # Mobius-owned -> untrusted -> fall back to the baked floor.
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (1000, 0o644))
  assert recoveryd.resolve_run_dir() == str(recoveryd._SELF_DIR)


def test_resolve_run_dir_baked_when_live_missing_entrypoint(recoveryd,
                                                            monkeypatch):
  live = recoveryd.LIVE_DIR
  live.mkdir(parents=True)
  # Trusted files but no recoveryd.py -> not runnable -> baked.
  (live / "recovery_auth.py").write_text("# not the entrypoint\n")
  monkeypatch.setattr(recoveryd, "_uid_and_mode", lambda p: (0, 0o644))
  assert recoveryd.resolve_run_dir() == str(recoveryd._SELF_DIR)


# -- Task 1.2: launcher re-exec + loop guard -------------------------------

def test_reexec_hands_off_to_trusted_live(recoveryd, monkeypatch):
  live = _trusted_live(recoveryd, monkeypatch)
  calls = []
  monkeypatch.setattr(
    recoveryd.os, "execv", lambda path, args: calls.append((path, args)))
  monkeypatch.delenv("MOBIUS_RECOVERY_EXECED", raising=False)
  recoveryd._maybe_reexec_into_run_dir()
  assert len(calls) == 1
  path, args = calls[0]
  assert path == sys.executable
  # Same interpreter, same -P hardening, into the live recoveryd.py.
  assert args[:3] == [sys.executable, "-P", str(live / "recoveryd.py")]
  # Sentinel is set before exec so the successor process won't loop.
  assert os.environ.get("MOBIUS_RECOVERY_EXECED") == "1"


def test_reexec_guard_blocks_second_exec(recoveryd, monkeypatch):
  # Even with a trusted live copy that differs from the running dir, an
  # already-execed process (sentinel set) must never exec again.
  _trusted_live(recoveryd, monkeypatch)
  calls = []
  monkeypatch.setattr(
    recoveryd.os, "execv", lambda path, args: calls.append((path, args)))
  monkeypatch.setenv("MOBIUS_RECOVERY_EXECED", "1")
  recoveryd._maybe_reexec_into_run_dir()
  assert calls == []


def test_reexec_noop_when_run_dir_is_self(recoveryd, monkeypatch):
  # No live copy -> resolve_run_dir == running dir -> no hand-off.
  calls = []
  monkeypatch.setattr(
    recoveryd.os, "execv", lambda path, args: calls.append((path, args)))
  monkeypatch.delenv("MOBIUS_RECOVERY_EXECED", raising=False)
  recoveryd._maybe_reexec_into_run_dir()
  assert calls == []


def test_main_runs_launcher_first(recoveryd, monkeypatch):
  """main() invokes the launcher before any server setup."""
  marker = []

  def sentinel():
    marker.append("launcher")
    raise RuntimeError("stop-after-launcher")

  monkeypatch.setattr(recoveryd, "_maybe_reexec_into_run_dir", sentinel)
  with pytest.raises(RuntimeError, match="stop-after-launcher"):
    recoveryd.main()
  assert marker == ["launcher"]


# -- Task 3.2: crash-loop quarantine ---------------------------------------

def test_live_attempts_bump_and_reset(recoveryd):
  assert recoveryd._live_attempts() == 0
  recoveryd._bump_live_attempts()
  recoveryd._bump_live_attempts()
  assert recoveryd._live_attempts() == 2
  recoveryd._reset_live_attempts()
  assert recoveryd._live_attempts() == 0


def test_live_attempts_zero_on_garbage(recoveryd):
  # An unreadable/garbage counter reads as 0 rather than raising, so a
  # corrupt attempts file can never wedge the launcher.
  recoveryd.ATTEMPTS_FILE.write_text("not-an-int")
  assert recoveryd._live_attempts() == 0


def test_resolve_run_dir_quarantines_after_max_attempts(recoveryd,
                                                        monkeypatch):
  live = _trusted_live(recoveryd, monkeypatch)
  recoveryd._reset_live_attempts()
  # Below the cap, the trusted live copy is chosen.
  assert recoveryd.resolve_run_dir() == str(live)
  # At/above the cap it is quarantined to the baked floor even though it is
  # still trusted — a trusted-but-crashing copy must not loop past the floor
  # across container restarts (the in-process exec sentinel does not survive
  # a restart, but this on-disk counter does).
  for _ in range(recoveryd._MAX_LIVE_ATTEMPTS):
    recoveryd._bump_live_attempts()
  assert recoveryd._live_attempts() >= recoveryd._MAX_LIVE_ATTEMPTS
  assert recoveryd.resolve_run_dir() == str(recoveryd._SELF_DIR)


def test_reexec_bumps_attempts_before_execv(recoveryd, monkeypatch):
  _trusted_live(recoveryd, monkeypatch)
  recoveryd._reset_live_attempts()
  seen = {}

  def fake_execv(path, args):
    # Capture the counter AT THE MOMENT of exec: the bump must precede it,
    # because execv never returns and the successor may crash before its
    # serve loop resets the counter.
    seen["attempts"] = recoveryd._live_attempts()

  monkeypatch.setattr(recoveryd.os, "execv", fake_execv)
  monkeypatch.delenv("MOBIUS_RECOVERY_EXECED", raising=False)
  recoveryd._maybe_reexec_into_run_dir()
  assert seen["attempts"] == 1


def test_running_live_copy_guard(recoveryd, monkeypatch):
  # Baked run: _SELF_DIR != LIVE_DIR.
  assert recoveryd._running_live_copy() is False
  # Running from the live copy: the launcher's successor has _SELF_DIR
  # resolving to LIVE_DIR, which is when main() may reset the counter.
  monkeypatch.setattr(recoveryd, "_SELF_DIR", recoveryd.LIVE_DIR)
  assert recoveryd._running_live_copy() is True
