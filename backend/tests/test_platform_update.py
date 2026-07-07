"""Clone-native platform reconcile — the git plumbing that fetches origin and
rebases the local edits onto the new version without ever losing them or serving
a broken tree.

These drive ``platform_update.reconcile_clone`` against throwaway repos in
``tmp_path``: a bare ``origin`` repo, a ``platform`` clone of it (mirroring the
entrypoint bootstrap: local ``main`` + an ``upstream`` marker branch at HEAD),
and the module's ``/data`` flag paths monkeypatched into ``tmp_path`` so no real
platform tree is touched. Each platform tree carries a trivially-importable
``backend/app`` package so the post-rebase import probe (a real ``import
app.main`` subprocess) exercises for real.

The load-bearing cases: a clean fast-forward advances the served tree; a disjoint
local edit is preserved by a rebase; a same-line conflict aborts and serves the
OLD code; a text-clean rebase whose result fails to import rolls back to the old
code; an offline fetch keeps serving unchanged; and a crash-interrupted rebase is
aborted on the next pass.
"""

import subprocess
import textwrap
from pathlib import Path

import pytest

from app import platform_update as pu


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  return subprocess.run(
    ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
    capture_output=True, text=True, check=check,
  )


# A trivially-importable backend so the import probe (`import app.main` with cwd
# repo/backend) runs for real. `main.py` imports the sibling `foo` module so a
# test can delete `foo` upstream to make a text-clean rebase import-broken.
_MAIN_PY = "import app.foo\n\nVALUE = app.foo.VALUE\nLINE_A = 1\nLINE_B = 2\nLINE_C = 3\n"
_FOO_PY = "VALUE = 'foo'\n"


def _write_backend(root: Path, main_py: str = _MAIN_PY, foo_py: str | None = _FOO_PY):
  app_dir = root / "backend" / "app"
  app_dir.mkdir(parents=True, exist_ok=True)
  (app_dir / "__init__.py").write_text("")
  (app_dir / "main.py").write_text(main_py)
  if foo_py is not None:
    (app_dir / "foo.py").write_text(foo_py)


def _make_origin(tmp: Path) -> Path:
  """A bare ``origin`` repo with an initial commit carrying an importable
  backend, plus a working checkout used to push new commits ('deploys')."""
  origin = tmp / "origin.git"
  _git(tmp, "init", "--bare", "-b", "main", str(origin))
  work = tmp / "origin-work"
  _git(tmp, "clone", str(origin), str(work))
  (work / ".gitignore").write_text("__pycache__/\n*.pyc\n")
  _write_backend(work)
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", "init")
  _git(work, "push", "-q", "origin", "main")
  return origin


def _clone_platform(tmp: Path, origin: Path) -> Path:
  """Clone ``origin`` into ``platform`` exactly as the entrypoint bootstrap does:
  local ``main`` checked out, an ``upstream`` marker branch at HEAD."""
  platform = tmp / "platform"
  _git(tmp, "clone", str(origin), str(platform))
  _git(platform, "branch", "-f", "upstream", "HEAD")
  return platform


def _advance_origin(origin: Path, *, edits: dict | None = None,
                    deletes: list[str] | None = None, msg: str = "deploy") -> str:
  """Push a new commit to ``origin/main`` (simulate a deploy). ``edits`` maps
  repo-relative paths to new content; ``deletes`` removes paths. Returns the new
  origin/main sha."""
  work = origin.parent / "origin-work"
  _git(work, "pull", "-q", "origin", "main")
  for rel, content in (edits or {}).items():
    p = work / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
  for rel in (deletes or []):
    (work / rel).unlink(missing_ok=True)
    _git(work, "rm", "-q", "--cached", rel, check=False)
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", msg)
  _git(work, "push", "-q", "origin", "main")
  return _git(work, "rev-parse", "main").stdout.strip()


def _local_commit(platform: Path, *, edits: dict, msg: str = "local edit") -> str:
  for rel, content in edits.items():
    p = platform / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
  _git(platform, "add", "-A")
  _git(platform, "commit", "-q", "-m", msg)
  return _git(platform, "rev-parse", "main").stdout.strip()


def _served_sha(platform: Path) -> str:
  return _git(platform, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def clone_env(tmp_path, monkeypatch):
  """A bare origin + a platform clone of it, with platform_update's flag paths
  retargeted into tmp_path."""
  monkeypatch.setattr(pu, "UPGRADE_FLAG", tmp_path / ".upgrade")
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", tmp_path / ".restart")
  monkeypatch.setattr(pu, "CONFLICT_FLAG", tmp_path / ".conflict")
  monkeypatch.setattr(pu, "ROLLED_BACK_FLAG", tmp_path / ".rolled-back")
  monkeypatch.setattr(pu, "RECONCILE_LOCK", tmp_path / ".reconcile.lock")
  monkeypatch.setenv("BUILD_SHA", "test-sha")
  origin = _make_origin(tmp_path)
  platform = _clone_platform(tmp_path, origin)
  return origin, platform


# --- V-B1: clean update fast-forwards ---------------------------------------

def test_clean_update_fast_forwards(clone_env):
  origin, platform = clone_env
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 300")})

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "updated"
  assert _served_sha(platform) == new == res.new_sha
  assert "LINE_C = 300" in (platform / "backend/app/main.py").read_text()
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists()
  # upstream marker advanced to the reconciled target.
  assert pu.recorded_upstream_sha(platform) == new
  # A second boot with no new deploy is a no-op.
  assert pu.reconcile_clone(platform, at_boot=True).status == "up_to_date"


# --- V-B2: disjoint local edit preserved via rebase -------------------------

def test_local_edit_preserved_across_update(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 111")})
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 333")})

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "updated"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 111" in served  # local edit
  assert "LINE_C = 333" in served  # upstream edit
  assert not pu.CONFLICT_FLAG.exists()


# --- regression: a drifted upstream marker never triggers a data-losing
# fast-forward. The ff-vs-rebase choice is decided by ANCESTRY, not the upstream
# marker, so committed local edits survive even when the marker is set to the
# exact value that would have made the old marker-gated `reset --hard target`
# discard them. This is the headline data-safety invariant of the fix. ---------

def test_drifted_upstream_marker_never_discards_local_commits(clone_env):
  origin, platform = clone_env
  # A committed local edit: main now diverges from the true upstream.
  local = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL_KEPT'")})
  # Drift the marker to main HEAD — the precise value that made the old
  # `pre == upstream_sha` gate take the destructive fast-forward branch.
  _git(platform, "branch", "-f", "upstream", "main")
  assert pu.recorded_upstream_sha(platform) == local  # marker mis-set to HEAD
  # A disjoint upstream deploy (does not contain the local commit).
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 424242")})

  res = pu.reconcile_clone(platform, at_boot=True)

  # local is NOT an ancestor of target, so ancestry forces a REBASE (not a reset)
  # and BOTH survive — the local commit is not discarded despite the bad marker.
  assert res.status == "updated"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 'LOCAL_KEPT'" in served  # committed local edit preserved
  assert "LINE_C = 424242" in served        # upstream deploy applied
  assert res.target_sha == new
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists()


# --- V-B3: same-line conflict -> serve OLD ----------------------------------

def test_conflict_serves_old_and_flags(clone_env):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "conflict"
  # Served tree is the pre-reconcile local code, intact; no half-rebase left.
  assert _served_sha(platform) == pre
  assert "LINE_A = 'LOCAL'" in (platform / "backend/app/main.py").read_text()
  assert not pu._rebase_in_progress(platform)
  assert pu.CONFLICT_FLAG.exists()
  assert any("main.py" in p for p in res.conflict_paths)
  status = pu.platform_status(platform)
  assert status["state"] == pu.PlatformUpdateState.CONFLICT.value
  assert any("main.py" in p for p in status["conflict_paths"])


# --- V-B4: import-broken text-clean rebase -> rollback ----------------------

def test_import_broken_rebase_rolls_back(clone_env):
  origin, platform = clone_env
  # A disjoint local edit (so the rebase is text-clean), while upstream DELETES
  # foo.py — which main.py still imports. Textually clean, import-broken.
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY + "LOCAL = 'kept'\n"})
  _advance_origin(origin, deletes=["backend/app/foo.py"], msg="drop foo")

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "rolled_back"
  # Rolled back to the old, WORKING code: foo.py is present, main.py imports it.
  assert _served_sha(platform) == pre
  assert (platform / "backend/app/foo.py").exists()
  assert "LOCAL = 'kept'" in (platform / "backend/app/main.py").read_text()
  assert pu.ROLLED_BACK_FLAG.exists()
  assert not pu.CONFLICT_FLAG.exists()
  status = pu.platform_status(platform)
  assert status["state"] == pu.PlatformUpdateState.ROLLED_BACK.value
  assert status["available"] is True  # the update is real, just needs repair
  # No boot loop: a second pass with the same broken deploy rolls back again to
  # the same pre sha (idempotent), never advancing onto the broken tree.
  res2 = pu.reconcile_clone(platform, at_boot=True)
  assert res2.status == "rolled_back"
  assert _served_sha(platform) == pre


# --- V-B5: offline fetch keeps serving --------------------------------------

def test_offline_fetch_serves_current_unchanged(clone_env, monkeypatch):
  origin, platform = clone_env
  before = _served_sha(platform)
  # Point origin at a dead path so the fetch fails.
  _git(platform, "remote", "set-url", "origin", str(platform.parent / "does-not-exist.git"))

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "offline"
  assert _served_sha(platform) == before  # unchanged, no crash, no data loss
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists()


# --- uncommitted working-tree edits are never lost --------------------------

def test_uncommitted_edits_committed_before_reconcile(clone_env):
  origin, platform = clone_env
  # An uncommitted local edit on disk (no commit) + a disjoint upstream deploy.
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY'"))
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 999")})

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "updated"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 'DIRTY'" in served  # uncommitted edit preserved
  assert "LINE_C = 999" in served


# --- crash-safety: a stale in-progress rebase is aborted --------------------

def test_stale_rebase_aborted_on_next_pass(clone_env):
  origin, platform = clone_env
  # Force a real conflict and leave the rebase in progress (no abort), mirroring
  # a crash mid-rebase.
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})
  _git(platform, "fetch", "-q", "origin")
  rc = _git(platform, "rebase", new, "main", check=False).returncode
  assert rc != 0 and pu._rebase_in_progress(platform)  # left mid-rebase

  # Next reconcile must abort the stale rebase FIRST, then reconcile cleanly
  # (here: re-conflict and serve old — the point is it does not wedge or corrupt).
  res = pu.reconcile_clone(platform, at_boot=True)
  assert res.status == "conflict"
  assert _served_sha(platform) == pre
  assert not pu._rebase_in_progress(platform)


# --- status availability + up-to-date ---------------------------------------

def test_status_available_when_origin_ahead(clone_env):
  origin, platform = clone_env
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 7")})
  _git(platform, "fetch", "-q", "origin")  # status reads the last-fetched ref

  status = pu.platform_status(platform)
  assert status["available"] is True
  assert status["state"] == pu.PlatformUpdateState.AVAILABLE.value


def test_status_up_to_date_on_fresh_clone(clone_env):
  origin, platform = clone_env
  status = pu.platform_status(platform)
  assert status["available"] is False
  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value


# --- check_for_updates: the on-demand fetch behind "Check for updates" -------

def test_check_for_updates_fetches_then_reports_available(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  # A deploy advances origin AFTER the clone's last fetch. platform_status is
  # fetch-free, so it still reads the stale remote-tracking ref: "up to date".
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 77")})
  assert pu.platform_status(platform)["state"] == \
    pu.PlatformUpdateState.UP_TO_DATE.value  # stale — no fetch happened yet

  # check_for_updates runs the fetch the cheap status read skips -> now visible.
  status = pu.check_for_updates(platform)
  assert status["available"] is True
  assert status["state"] == pu.PlatformUpdateState.AVAILABLE.value
  # A check only advances remote-tracking refs — the served tree is NOT mutated.
  assert _served_sha(platform) == before


def test_check_for_updates_offline_is_safe_noop(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  _git(platform, "remote", "set-url", "origin",
       str(platform.parent / "does-not-exist.git"))
  # An offline check must not raise and must leave the served tree + status intact.
  status = pu.check_for_updates(platform)
  assert status["available"] is False
  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
  assert _served_sha(platform) == before


# --- restart flag lifecycle -------------------------------------------------

def test_boot_reconcile_clears_restart_flag(clone_env):
  origin, platform = clone_env
  pu.mark_restart_needed("some-sha")
  assert pu.RESTART_NEEDED_FLAG.exists()
  # A boot with no new deploy is up_to_date; a boot IS the restart the flag asks
  # for, so it clears (the fresh process serves the on-disk code).
  res = pu.reconcile_clone(platform, at_boot=True)
  assert res.status == "up_to_date"
  assert not pu.RESTART_NEEDED_FLAG.exists()


def test_non_boot_reconcile_keeps_restart_flag(clone_env):
  origin, platform = clone_env
  pu.mark_restart_needed("some-sha")
  # An owner-apply reconcile (at_boot=False) must NOT clear the flag on an
  # up-to-date pass — the running process is unchanged.
  res = pu.reconcile_clone(platform, at_boot=False)
  assert res.status == "up_to_date"
  assert pu.RESTART_NEEDED_FLAG.exists()


# --- regression: an OFFLINE boot still clears a stale restart flag. The clear is
# unconditional and early (not only on the success/up-to-date branches), so an
# owner Apply that set RESTART_NEEDED followed by an offline reboot — whose fetch
# fails and returns 'offline' before any later branch — does not leave a
# permanent "restart needed" prompt. -----------------------------------------

def test_offline_boot_clears_stale_restart_flag(clone_env):
  origin, platform = clone_env
  pu.mark_restart_needed("some-sha")
  assert pu.RESTART_NEEDED_FLAG.exists()
  # Force the fetch to fail so the reconcile returns 'offline' BEFORE reaching any
  # success/up-to-date branch — the flag must still clear (the boot IS the restart
  # the flag asked for; the fresh process already serves the on-disk code).
  _git(platform, "remote", "set-url", "origin",
       str(platform.parent / "does-not-exist.git"))

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "offline"
  assert not pu.RESTART_NEEDED_FLAG.exists()


# --- conflict flag format round-trips (chat id, legacy) ---------------------

def test_conflict_flag_roundtrips_chat_id_and_reads_legacy(clone_env):
  pu._write_conflict_flag("tgt-sha", ["backend/app/a.py", "backend/app/b.py"], "chat-42")
  assert pu._read_conflict_flag() == {
    "upstream": "tgt-sha", "chat_id": "chat-42",
    "paths": ["backend/app/a.py", "backend/app/b.py"],
  }
  pu.CONFLICT_FLAG.write_text("tgt-sha\nbackend/app/a.py")
  legacy = pu._read_conflict_flag()
  assert legacy["chat_id"] is None
  assert legacy["paths"] == ["backend/app/a.py"]


def test_rolled_back_flag_roundtrips(clone_env):
  pu._write_rolled_back_flag("tgt-sha", "ModuleNotFoundError: app.foo")
  got = pu._read_rolled_back_flag()
  assert got["target"] == "tgt-sha"
  assert "ModuleNotFoundError" in got["error"]
