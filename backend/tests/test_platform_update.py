"""Clone-native platform reconcile — the git plumbing that fetches origin and
merges it once with local edits without ever losing them or serving a broken
tree.

These drive ``platform_update.reconcile_clone`` against throwaway repos in
``tmp_path``: a bare ``origin`` repo, a ``platform`` clone of it (mirroring the
entrypoint bootstrap: local ``main`` + an ``upstream`` marker branch at HEAD),
and the module's ``/data`` flag paths monkeypatched into ``tmp_path`` so no real
platform tree is touched. Each platform tree carries a trivially-importable
``backend/app`` package so the post-merge import probe (a real ``import
app.main`` subprocess) exercises for real.

The load-bearing cases: a clean fast-forward advances the served tree; a
disjoint local edit is preserved with its original commit identity; all net
conflicts surface in one merge; a text-clean merge whose result fails to import
rolls back to the old code; an offline fetch keeps serving unchanged; and a
crash-interrupted merge is aborted on the next pass. A legacy interrupted rebase
is still cleaned up because an update can cross this implementation boundary.
"""

import hashlib
import subprocess
import stat
import textwrap
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import app_git, platform_activation
from app import platform_update as pu
from app import release_channel


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  proc = subprocess.run(
    ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
    capture_output=True, text=True,
  )
  if check and proc.returncode != 0:
    # Preserve the CalledProcessError type, but attach git's stderr as an
    # exception note so the traceback shows git's actual `fatal:` line. The
    # default check=True message is only "... exit status N", which is why the
    # intermittent exit-128 failure this suite has hit in CI was undiagnosable:
    # git's own error text was swallowed. Surfacing it lets the next occurrence
    # name its own cause instead of leaving us to guess.
    err = subprocess.CalledProcessError(
      proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr,
    )
    err.add_note(f"git stderr: {proc.stderr.strip() or '(empty)'}")
    raise err
  return proc


# A trivially-importable backend so the import probe (`import app.main` with cwd
# repo/backend) runs for real. `main.py` imports the sibling `foo` module so a
# test can delete `foo` upstream to make a text-clean merge import-broken.
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


def _apply_plan(current_sha: str, target_sha: str, repo: Path) -> dict:
  return {
    "plan_id": pu._update_plan_id(current_sha, target_sha),
    "current_sha": current_sha,
    "target_sha": target_sha,
    "repo": repo,
  }


@pytest.fixture
def clone_env(tmp_path, monkeypatch):
  """A bare origin + a platform clone of it, with platform_update's flag paths
  retargeted into tmp_path."""
  monkeypatch.setattr(pu, "UPGRADE_FLAG", tmp_path / ".upgrade")
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", tmp_path / ".restart")
  monkeypatch.setattr(pu, "SERVING_SOURCE_FILE", tmp_path / ".serving-source")
  monkeypatch.setattr(pu, "SERVING_SHA_FILE", tmp_path / ".serving-sha")
  monkeypatch.setattr(pu, "CONFLICT_FLAG", tmp_path / ".conflict")
  monkeypatch.setattr(pu, "ROLLED_BACK_FLAG", tmp_path / ".rolled-back")
  monkeypatch.setattr(pu, "RECONCILE_PRE_FLAG", tmp_path / ".reconcile-pre")
  monkeypatch.setattr(pu, "OFFLINE_FLAG", tmp_path / ".offline")
  monkeypatch.setattr(pu, "RECONCILE_LOCK", tmp_path / ".reconcile.lock")
  monkeypatch.setattr(
    pu,
    "UPDATE_PROGRESS_PATH",
    tmp_path / ".update-progress.json",
  )
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


def test_up_to_date_retires_integrated_provenance(clone_env, monkeypatch):
  _origin, platform = clone_env
  target = _served_sha(platform)
  calls = []
  monkeypatch.setattr(
    app_git,
    "retire_landed_equivalent_changes",
    lambda repo, upstream: calls.append((repo, upstream)) or 2,
  )

  result = pu.reconcile_clone(platform, at_boot=True)

  assert result.status == "up_to_date"
  assert calls == [(platform, target)]


def test_clean_shallow_fast_forward_does_not_fetch_full_history(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 301")})
  monkeypatch.setattr(pu, "_is_shallow", lambda _repo: True)

  def fail_unshallow(_repo):
    raise AssertionError("a provable fast-forward must not fetch full history")

  monkeypatch.setattr(pu, "_fetch_unshallow", fail_unshallow)

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "updated"
  assert _served_sha(platform) == new


# --- V-B2: disjoint local edit preserved via one merge ----------------------

def test_local_edit_preserved_across_update(clone_env):
  origin, platform = clone_env
  local = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 111")})
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 333")})

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "updated"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 111" in served  # local edit
  assert "LINE_C = 333" in served  # upstream edit
  # A single merge preserves the original local commit instead of rewriting it
  # through a per-commit rebase. Both complete histories are explicit parents.
  parents = _git(
    platform, "show", "-s", "--format=%P", res.new_sha,
  ).stdout.split()
  assert parents == [local, target]
  assert _git(
    platform, "merge-base", "--is-ancestor", local, res.new_sha,
  ).returncode == 0
  assert _git(
    platform, "merge-base", "--is-ancestor", target, res.new_sha,
  ).returncode == 0
  assert not pu.CONFLICT_FLAG.exists()


# --- regression: a drifted upstream marker never triggers a data-losing
# fast-forward. The ff-vs-merge choice is decided by ANCESTRY, not the upstream
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

  # local is NOT an ancestor of target, so ancestry forces a MERGE (not a reset)
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
  # Served tree is the pre-reconcile local code, intact; no half-merge left.
  assert _served_sha(platform) == pre
  assert "LINE_A = 'LOCAL'" in (platform / "backend/app/main.py").read_text()
  assert not pu._reconcile_in_progress(platform)
  assert pu.CONFLICT_FLAG.exists()
  assert any("main.py" in p for p in res.conflict_paths)
  status = pu.platform_status(platform)
  assert status["state"] == pu.PlatformUpdateState.CONFLICT.value
  assert any("main.py" in p for p in status["conflict_paths"])


def test_contributed_squash_uses_provenance_for_both_histories(clone_env):
  """The shell auto-reconciles a reviewed change returned under a new SHA.

  The local line evolves after review, while upstream squash-merges the reviewed
  predecessor and adds a separate release edit.  Ordinary Git conflicts on the
  line; the two provenance witnesses make the reviewed tree a semantic base,
  preserving the follow-up and recording the real target as merge parent.
  """
  origin, platform = clone_env
  base = _served_sha(platform)
  shared = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'SHARED REVIEW'")
  reviewed = _local_commit(
    platform,
    edits={"backend/app/main.py": shared},
    msg="reviewed contribution",
  )
  diff = app_git._canonical_diff(platform, base, reviewed)
  assert diff is not None
  digest = hashlib.sha256(diff).hexdigest()
  pending = app_git.record_pending_equivalent_change(
    platform,
    base_sha=base,
    head_sha=reviewed,
    source_sha=reviewed,
    diff_sha256=digest,
    contribution_id="shell-reviewed-change",
  )
  assert pending

  followup = shared.replace("SHARED REVIEW", "LOCAL FOLLOWUP")
  pre = _local_commit(
    platform,
    edits={"backend/app/main.py": followup},
    msg="local followup",
  )
  target_body = shared.replace("LINE_C = 3", "LINE_C = 9001")
  target = _advance_origin(
    origin,
    edits={"backend/app/main.py": target_body},
    msg="squash reviewed contribution",
  )
  landed = app_git.mark_equivalent_change_landed(
    platform, digest, upstream_sha=target,
  )
  assert landed

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "updated"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 'LOCAL FOLLOWUP'" in served
  assert "LINE_C = 9001" in served
  parents = _git(platform, "show", "-s", "--format=%P", "HEAD").stdout.split()
  assert parents == [pre, target]
  assert not pu.CONFLICT_FLAG.exists()
  assert not app_git.ref_exists(platform, landed)


def test_partial_provenance_persists_reduced_platform_conflict(clone_env):
  """A genuine later conflict keeps the proven contribution out of resolution."""
  origin, platform = clone_env
  base = _served_sha(platform)
  shared = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'SHARED REVIEW'")
  reviewed = _local_commit(
    platform,
    edits={"backend/app/main.py": shared},
    msg="reviewed contribution",
  )
  diff = app_git._canonical_diff(platform, base, reviewed)
  assert diff is not None
  digest = hashlib.sha256(diff).hexdigest()
  assert app_git.record_pending_equivalent_change(
    platform,
    base_sha=base,
    head_sha=reviewed,
    source_sha=reviewed,
    diff_sha256=digest,
    contribution_id="partial-platform-change",
  )

  followup = shared.replace("SHARED REVIEW", "LOCAL FOLLOWUP")
  pre = _local_commit(
    platform,
    edits={
      "backend/app/main.py": followup,
      "backend/app/foo.py": "VALUE = 'LOCAL'\n",
    },
    msg="later local edits",
  )
  target = _advance_origin(
    origin,
    edits={
      "backend/app/main.py": shared.replace("LINE_C = 3", "LINE_C = 9001"),
      "backend/app/foo.py": "VALUE = 'UPSTREAM'\n",
    },
    msg="squash plus genuine conflict",
  )
  landed = app_git.mark_equivalent_change_landed(
    platform, digest, upstream_sha=target,
  )
  assert landed

  _git(platform, "fetch", "-q", "origin")
  ordinary = app_git.merge_refs(platform, pre, target)
  assert set(ordinary.conflict_paths) == {
    "backend/app/main.py", "backend/app/foo.py",
  }
  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "conflict"
  assert res.conflict_paths == ["backend/app/foo.py"]
  assert res.reconciliation.proven_present == ("partial-platform-change",)
  assert res.reconciliation.provenance_refs_used == (landed,)
  assert res.reconciliation.local_only_paths == ()
  assert res.reconciliation.new_upstream_paths == ()
  assert res.reconciliation.compatible_paths == ("backend/app/main.py",)
  assert res.reconciliation.unresolved_conflict_paths == (
    "backend/app/foo.py",
  )
  assert _served_sha(platform) == pre
  flag = pu._read_conflict_flag()
  assert flag["paths"] == ["backend/app/foo.py"]
  assert flag["merge_base"]

  conflicts = pu.materialize_platform_conflict(
    target, flag["merge_base"], platform,
  )
  assert conflicts == ["backend/app/foo.py"]
  assert "LINE_A = 'LOCAL FOLLOWUP'" in (
    platform / "backend/app/main.py"
  ).read_text()
  assert "LINE_C = 9001" in (platform / "backend/app/main.py").read_text()
  assert "<<<<<<< ours" in (platform / "backend/app/foo.py").read_text()

  _git(platform, "merge", "--abort")
  assert _served_sha(platform) == pre
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'LOCAL'\n"


def test_diverged_update_surfaces_all_net_conflicts_together(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'LOCAL'\n"})
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace(
      "LINE_A = 1", "LINE_A = 'UPSTREAM'",
    ),
    "backend/app/foo.py": "VALUE = 'UPSTREAM'\n",
  })

  res = pu.reconcile_clone(platform, at_boot=True)

  # A per-commit rebase stopped on main.py, then made the resolver discover the
  # foo.py conflict only after continuing. The net merge reports both at once.
  assert res.status == "conflict"
  assert set(res.conflict_paths) == {
    "backend/app/main.py", "backend/app/foo.py",
  }
  assert not pu._reconcile_in_progress(platform)


@pytest.mark.parametrize(
  ("merge_rc", "expected_error"),
  [
    (pu._MERGE_TIMEOUT, "merge_timeout"),
    (2, "merge_failed"),
  ],
)
def test_non_conflict_merge_failure_serves_old_without_a_resolver_flag(
  clone_env, monkeypatch, merge_rc, expected_error,
):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 'UPSTREAM'")})
  # A previous attempt may have left either durable review state behind. A
  # timeout or git failure with no unmerged paths has nothing a resolver can
  # act on, so returning "error" while preserving one of these flags would make
  # the next status read lie about what just happened.
  pu._write_conflict_flag("b" * 40, ["backend/app/old.py"])
  pu._write_rolled_back_flag("c" * 40, "old import failure")
  monkeypatch.setattr(pu, "_merge_target", lambda _repo, _target: merge_rc)

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "error"
  assert res.error == expected_error
  assert res.target_sha == target
  assert _served_sha(platform) == pre
  assert not pu._reconcile_in_progress(platform)
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists()
  assert not pu.RECONCILE_PRE_FLAG.exists()


# --- V-B4: import-broken text-clean merge -> rollback -----------------------

def test_import_broken_merge_rolls_back(clone_env):
  origin, platform = clone_env
  # A disjoint local edit (so the merge is text-clean), while upstream DELETES
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


def test_detached_head_uncommitted_edit_survives_reconcile(clone_env):
  origin, platform = clone_env
  _git(platform, "checkout", "-q", "--detach", "HEAD")
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DETACHED_DIRTY'"))
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 1001")})

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "updated"
  assert _git(platform, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 'DETACHED_DIRTY'" in served
  assert "LINE_C = 1001" in served


# --- crash-safety: interrupted merge + legacy rebase are aborted ------------

def test_stale_merge_aborted_on_next_pass(clone_env):
  origin, platform = clone_env
  # Force a real conflict and leave the merge in progress (no abort), mirroring
  # a crash mid-merge.
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})
  _git(platform, "fetch", "-q", "origin")
  rc = _git(platform, "merge", "--no-ff", new, check=False).returncode
  assert rc != 0 and pu._merge_in_progress(platform)  # left mid-merge

  # Next reconcile must abort the stale merge FIRST, then reconcile cleanly
  # (here: re-conflict and serve old — the point is it does not wedge or corrupt).
  res = pu.reconcile_clone(platform, at_boot=True)
  assert res.status == "conflict"
  assert _served_sha(platform) == pre
  assert not pu._reconcile_in_progress(platform)


def test_boot_guard_aborts_interrupted_merge_before_serving(clone_env):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})
  _git(platform, "fetch", "-q", "origin")
  pu._write_reconcile_pre(pre)
  rc = _git(platform, "merge", "--no-ff", new, check=False).returncode
  assert rc != 0 and pu._merge_in_progress(platform)
  assert "<<<<<<<" in (platform / "backend/app/main.py").read_text()

  summary = pu.boot_guard_clean_served_tree(platform)

  assert summary.startswith("boot_guard[reset]")
  assert _served_sha(platform) == pre
  assert not pu._reconcile_in_progress(platform)
  assert "<<<<<<<" not in (platform / "backend/app/main.py").read_text()
  ok, err = pu._import_probe(platform)
  assert ok, err
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_legacy_stale_rebase_aborted_on_next_pass(clone_env):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})
  _git(platform, "fetch", "-q", "origin")
  rc = _git(platform, "rebase", new, "main", check=False).returncode
  assert rc != 0 and pu._rebase_in_progress(platform)

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "conflict"
  assert _served_sha(platform) == pre
  assert not pu._reconcile_in_progress(platform)


def test_boot_guard_sync_propagates_failure(monkeypatch):
  """The final boot gate must fail closed; callers need a non-zero process,
  not an error-looking success string that the shell can accidentally ignore."""
  monkeypatch.setattr(pu, "_reconcile_flock", lambda: nullcontext())
  monkeypatch.setattr(
    pu,
    "boot_guard_clean_served_tree",
    lambda _repo: (_ for _ in ()).throw(OSError("guard failed")),
  )
  with pytest.raises(OSError, match="guard failed"):
    pu.boot_guard_sync()


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


def test_check_for_updates_offline_is_explicit_error(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  _git(platform, "remote", "set-url", "origin",
       str(platform.parent / "does-not-exist.git"))
  # A stale remote-tracking ref cannot authoritatively mean "no updates".
  with pytest.raises(pu.PlatformUpdateError, match="platform_fetch_failed"):
    pu.check_for_updates(platform)
  assert _served_sha(platform) == before


def test_check_for_updates_requires_a_fetchable_clone(tmp_path):
  with pytest.raises(pu.PlatformUpdateError, match="platform_repo_missing"):
    pu.check_for_updates(tmp_path / "missing")


def test_check_for_updates_syncs_marker_when_local_already_contains_origin(clone_env):
  origin, platform = clone_env
  stale_marker = pu.recorded_upstream_sha(platform)
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 88")})
  _git(platform, "fetch", "-q", "origin")
  _git(platform, "merge", "--ff-only", "-q", "origin/main")
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_B = 2", "LINE_B = 'LOCAL'")})
  _git(platform, "branch", "-f", "upstream", stale_marker)
  assert pu.recorded_upstream_sha(platform) == stale_marker

  status = pu.check_for_updates(platform)

  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
  assert pu.recorded_upstream_sha(platform) == new


def test_status_reports_contained_origin_when_updater_marker_is_stale(clone_env):
  origin, platform = clone_env
  stale_marker = pu.recorded_upstream_sha(platform)
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 89")})
  _git(platform, "fetch", "-q", "origin")
  _git(platform, "merge", "--ff-only", "-q", "origin/main")
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_B = 2", "LINE_B = 'LOCAL STATUS'")})
  _git(platform, "branch", "-f", "upstream", stale_marker)

  status = pu.platform_status(platform)

  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
  assert status["recorded_upstream_sha"] == stale_marker
  assert status["contained_upstream_sha"] == new


# --- owner Apply rebuilds stale frontend dist after frontend updates ----------

def test_touched_frontend_detects_frontend_only_changes(clone_env):
  origin, platform = clone_env
  pre = _served_sha(platform)
  new = _advance_origin(origin, edits={"frontend/src/App.jsx": "export default 1\n"})
  _git(platform, "fetch", "-q", "origin")

  assert pu._touched_frontend(platform, pre, new) is True
  assert pu._touched_frontend(platform, pre, pre) is False


@pytest.mark.asyncio
async def test_apply_rebuilds_frontend_but_no_restart_when_update_is_frontend_only(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  served = _served_sha(platform)  # captured BEFORE the frontend-only commit
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")  # the running uvicorn's sha
  new = _local_commit(platform, edits={"frontend/src/App.jsx": "export default 2\n"})
  calls = []
  hook_calls = []

  def fake_rebuild(repo, res):
    calls.append((repo, res.new_sha))

  def fake_reconcile(repo, at_boot, **kwargs):
    result = pu.ReconcileResult(
      "updated", served, new, new, hook_source_sha=new,
    )
    if kwargs.get("prepare_frontend"):
      pu._rebuild_frontend_after_update_if_needed(repo, result)
    return result

  monkeypatch.setattr(pu, "_reconcile_under_lock", fake_reconcile)
  monkeypatch.setattr(
    pu, "_refresh_git_hooks",
    lambda repo, source_oid: hook_calls.append((repo, source_oid)) or "",
  )
  monkeypatch.setattr(pu, "_rebuild_frontend_after_update_if_needed", fake_rebuild)

  res = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(served, new, platform),
  )

  # The frontend rebuilds into dist (served per-request), but the served uvicorn
  # imports no frontend — so no restart is prompted. Owner's exact complaint.
  assert res["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
  assert res["needs_restart"] is False
  assert calls == [(platform, new)]
  assert hook_calls == [(platform, new)]


def test_boot_reconcile_refreshes_copied_hooks(monkeypatch, tmp_path):
  platform = tmp_path / "platform"
  platform.mkdir()
  calls = []
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  monkeypatch.setattr(pu, "_reconcile_under_lock", lambda repo, at_boot: (
    pu.ReconcileResult(
      "up_to_date", "pre-sha", "pre-sha", "target-sha",
      hook_source_sha="trusted-hook-sha",
    )
  ))
  monkeypatch.setattr(
    pu, "_refresh_git_hooks",
    lambda repo, source_oid: calls.append((repo, source_oid)) or "",
  )

  summary = pu.reconcile_clone_sync()

  assert calls == [(platform, "trusted-hook-sha")]
  assert "hooks=refreshed" in summary


def test_reconcile_pins_upstream_hook_source_before_unlock(monkeypatch, tmp_path):
  repo = tmp_path / "platform"
  repo.mkdir()
  events = []

  @contextmanager
  def fake_lock():
    events.append("locked")
    yield
    events.append("unlocked")

  def fake_reconcile(
    repo_path, *, at_boot, target_ref, fetch_remote, progress,
  ):
    assert events == ["locked"]
    assert repo_path == repo
    assert at_boot is True
    assert target_ref == pu.DEFAULT_TARGET_REF
    assert fetch_remote is True
    assert progress is None
    return pu.ReconcileResult("up_to_date", "pre", "pre", "target")

  def fake_rev(repo_path, ref):
    assert events == ["locked"]
    assert repo_path == repo
    assert ref == pu.UPSTREAM_BRANCH
    return "trusted-upstream-oid"

  monkeypatch.setattr(pu, "_reconcile_flock", fake_lock)
  monkeypatch.setattr(pu, "reconcile_clone", fake_reconcile)
  monkeypatch.setattr(pu, "_rev", fake_rev)

  result = pu._reconcile_under_lock(repo, at_boot=True)

  assert events == ["locked", "unlocked"]
  assert result.hook_source_sha == "trusted-upstream-oid"


def _make_hook_repo(tmp_path: Path, *, complete: bool = True) -> Path:
  tmp_path.mkdir(parents=True, exist_ok=True)
  repo = tmp_path / "hook-repo"
  _git(tmp_path, "init", "-b", "main", str(repo))
  scripts = repo / "scripts"
  (scripts / "githooks").mkdir(parents=True)
  (scripts / "install-hooks.sh").write_text("#!/bin/sh\nexit 99\n")
  (scripts / "pre-commit.sh").write_text("#!/bin/sh\necho committed-pre-commit\n")
  if complete:
    (scripts / "githooks" / "pre-push").write_text(
      "#!/bin/sh\necho committed-pre-push\n"
    )
  _git(repo, "add", "scripts")
  _git(repo, "commit", "-q", "-m", "add hooks")
  return repo


def test_hook_refresh_uses_only_committed_allowlisted_sources(tmp_path):
  repo = _make_hook_repo(tmp_path)
  source_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
  # Neither a dirty managed hook nor a newly dropped executable may run merely
  # because a healthy boot refreshes the installed copies.
  (repo / "scripts" / "pre-commit.sh").write_text("#!/bin/sh\necho DIRTY\n")
  (repo / "scripts" / "githooks" / "post-checkout").write_text(
    "#!/bin/sh\necho UNTRACKED\n"
  )

  assert pu._refresh_git_hooks(repo, source_oid) == ""

  hooks = repo / ".git" / "hooks"
  assert (hooks / "pre-commit").read_text() == (
    "#!/bin/sh\necho committed-pre-commit\n"
  )
  assert (hooks / "pre-push").read_text() == (
    "#!/bin/sh\necho committed-pre-push\n"
  )
  assert not (hooks / "post-checkout").exists()
  assert (hooks / "pre-commit").stat().st_mode & 0o777 == 0o755
  assert (hooks / "pre-push").stat().st_mode & 0o777 == 0o755
  configured = _git(repo, "config", "--local", "--get", "core.hooksPath")
  assert Path(configured.stdout.strip()) == hooks.resolve()


def test_hook_refresh_reads_one_pinned_generation_when_head_moves(
  tmp_path, monkeypatch,
):
  repo = _make_hook_repo(tmp_path)
  source_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
  expected = {
    "pre-commit": b"#!/bin/sh\necho committed-pre-commit\n",
    "pre-push": b"#!/bin/sh\necho committed-pre-push\n",
  }

  (repo / "scripts" / "pre-commit.sh").write_text("#!/bin/sh\necho NEW-commit\n")
  (repo / "scripts" / "githooks" / "pre-push").write_text(
    "#!/bin/sh\necho NEW-push\n"
  )
  _git(repo, "add", "scripts")
  _git(repo, "commit", "-q", "-m", "new hook generation")
  next_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
  _git(repo, "reset", "--hard", "-q", source_oid)

  real_hook_git = pu._hook_git
  moved = False

  def move_head_between_blob_reads(repo_path, *args):
    nonlocal moved
    result = real_hook_git(repo_path, *args)
    if (
      not moved
      and args == (
        "cat-file", "blob", f"{source_oid}:scripts/pre-commit.sh",
      )
    ):
      moved = True
      _git(repo, "reset", "--hard", "-q", next_oid)
    return result

  monkeypatch.setattr(pu, "_hook_git", move_head_between_blob_reads)

  assert pu._refresh_git_hooks(repo, source_oid) == ""
  assert moved is True
  hooks = repo / ".git" / "hooks"
  assert {
    name: (hooks / name).read_bytes()
    for name in expected
  } == expected


def test_hook_refresh_rolls_back_without_absent_destinations(
  tmp_path, monkeypatch,
):
  repo = _make_hook_repo(tmp_path)
  source_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
  assert pu._refresh_git_hooks(repo, source_oid) == ""
  hooks = repo / ".git" / "hooks"
  old = {
    name: (hooks / name).read_bytes()
    for name in ("pre-commit", "pre-push")
  }
  (repo / "scripts" / "pre-commit.sh").write_text("#!/bin/sh\necho new-commit\n")
  (repo / "scripts" / "githooks" / "pre-push").write_text(
    "#!/bin/sh\necho new-push\n"
  )
  _git(repo, "add", "scripts")
  _git(repo, "commit", "-q", "-m", "update hooks")
  source_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()

  real_replace = pu.os.replace
  failed = False

  def fail_second_hook_once(source, destination):
    nonlocal failed
    target = Path(destination)
    if target.name in old:
      assert all((hooks / name).exists() for name in old)
      if target.name == "pre-push" and not failed:
        failed = True
        raise OSError("simulated second replacement failure")
    real_replace(source, destination)
    if target.name in old:
      assert all((hooks / name).exists() for name in old)

  monkeypatch.setattr(pu.os, "replace", fail_second_hook_once)

  result = pu._refresh_git_hooks(repo, source_oid)

  assert "simulated second replacement failure" in result
  assert {(name, (hooks / name).read_bytes()) for name in old} == set(old.items())


def test_hook_refresh_missing_incomplete_and_timeout_are_nonfatal(
  tmp_path, monkeypatch,
):
  missing = tmp_path / "missing"
  _git(tmp_path, "init", "-b", "main", str(missing))
  (missing / "README").write_text("old checkout\n")
  _git(missing, "add", "README")
  _git(missing, "commit", "-q", "-m", "old checkout")
  missing_oid = _git(missing, "rev-parse", "HEAD").stdout.strip()
  assert pu._refresh_git_hooks(missing, missing_oid) is None

  incomplete = _make_hook_repo(tmp_path / "incomplete", complete=False)
  incomplete_oid = _git(incomplete, "rev-parse", "HEAD").stdout.strip()
  result = pu._refresh_git_hooks(incomplete, incomplete_oid)
  assert result
  assert "pre-push" in result

  monkeypatch.setattr(
    pu, "_refresh_git_hooks_impl",
    lambda _repo, _source_oid: (_ for _ in ()).throw(
      subprocess.TimeoutExpired(["git", "show"], timeout=15)
    ),
  )
  assert "TimeoutExpired" in pu._refresh_git_hooks(missing, missing_oid)


def test_hook_refresh_config_failure_keeps_complete_first_population(
  tmp_path, monkeypatch,
):
  repo = _make_hook_repo(tmp_path)
  source_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
  real_hook_git = pu._hook_git

  def fail_config(repo_path, *args):
    if args[:3] == ("config", "--local", "core.hooksPath"):
      return subprocess.CompletedProcess(args, 1, b"", b"config locked")
    return real_hook_git(repo_path, *args)

  monkeypatch.setattr(pu, "_hook_git", fail_config)

  result = pu._refresh_git_hooks(repo, source_oid)

  assert "config locked" in result
  hooks = repo / ".git" / "hooks"
  assert (hooks / "pre-commit").read_text().startswith("#!/bin/sh")
  assert (hooks / "pre-push").read_text().startswith("#!/bin/sh")


@pytest.mark.asyncio
async def test_apply_restarts_and_rebuilds_when_update_touches_backend(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  served = _served_sha(platform)
  new = _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 9"),
    "frontend/src/App.jsx": "export default 3\n",
  })
  calls = []

  def fake_rebuild(repo, res):
    calls.append((repo, res.new_sha))

  def fake_reconcile(repo, at_boot, **kwargs):
    result = pu.ReconcileResult("updated", served, new, new)
    if kwargs.get("prepare_frontend"):
      pu._rebuild_frontend_after_update_if_needed(repo, result)
    return result

  monkeypatch.setattr(pu, "_reconcile_under_lock", fake_reconcile)
  monkeypatch.setattr(pu, "_rebuild_frontend_after_update_if_needed", fake_rebuild)
  # Owner Apply is independent of an image-managed boot channel. A missing
  # build-info file would make any accidental release-channel lookup fail.
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    "refs/heads/release/external-recovery",
  )
  monkeypatch.setattr(
    release_channel, "BUILD_INFO_PATH", platform / "missing-build-info.json",
  )

  res = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(served, new, platform),
  )

  # A backend change (mixed with frontend) still restarts AND rebuilds.
  assert res["state"] == pu.PlatformUpdateState.RESTART_NEEDED.value
  assert res["needs_restart"] is True
  assert res["activation"]["level"] == "server_restart"
  assert calls == [(platform, new)]


@pytest.mark.asyncio
async def test_apply_conflict_waits_for_owner_before_opening_chat(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})

  async def fail_spawn(*args, **kwargs):  # pragma: no cover - should not run
    raise AssertionError("apply must not start a resolver chat")

  monkeypatch.setattr(pu, "spawn_platform_conflict_chat", fail_spawn)
  monkeypatch.setattr(pu, "_reconcile_under_lock", lambda repo, at_boot, **kwargs: (
    pu.ReconcileResult(
      "conflict", _served_sha(platform), _served_sha(platform), target,
      ["backend/app/main.py"],
    )
  ))

  current = _served_sha(platform)
  res = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(current, target, platform),
  )

  assert res["state"] == pu.PlatformUpdateState.CONFLICT.value
  assert res["needs_restart"] is False
  assert res["chat_id"] is None
  flag = pu._read_conflict_flag()
  assert flag["upstream"] == target
  assert flag["paths"] == ["backend/app/main.py"]
  assert flag["chat_id"] is None


@pytest.mark.asyncio
async def test_platform_conflict_resolver_chat_is_click_gated(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})
  pu._fetch(platform)
  # Legacy/incomplete flags fall back to moving origin/main even when boot is
  # managed. No baked build identity is needed on this owner-triggered path.
  pu._write_conflict_flag(None, ["backend/app/main.py"])
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    "refs/heads/release/external-recovery",
  )
  monkeypatch.setattr(
    release_channel, "BUILD_INFO_PATH", platform / "missing-build-info.json",
  )
  calls = []

  async def fake_spawn(db, paths, target_sha, merge_base):
    calls.append((db, paths, target_sha, merge_base))
    return {
      "chat_id": "resolver-chat",
      "created": True,
      "started": True,
    }

  monkeypatch.setattr(pu, "spawn_platform_conflict_chat", fake_spawn)

  db = SimpleNamespace()
  res = await pu.create_platform_conflict_resolver_chat(db, platform)

  assert res == {
    "chat_id": "resolver-chat",
    "created": True,
    "started": True,
  }
  assert calls == [(db, ["backend/app/main.py"], target, None)]
  flag = pu._read_conflict_flag()
  assert flag["upstream"] == target
  assert flag["paths"] == ["backend/app/main.py"]
  assert flag["chat_id"] == "resolver-chat"
  assert flag["merge_base"] is None


def test_platform_conflict_resolver_message_pins_reviewed_target():
  target = "a" * 40

  content = pu._platform_conflict_resolver_message(
    target,
    ["backend/app/main.py", "frontend/src/App.jsx"],
  )

  assert f"merge --no-ff {target}" in content
  assert "merge --no-ff origin/main" not in content
  assert "backend/app/main.py, frontend/src/App.jsx" in content
  # The resolver runs in a headless shell where `git merge --continue` opens an
  # editor and hangs/errors; it must finish non-interactively instead.
  assert "commit --no-edit" in content
  assert "merge --continue" not in content


def test_platform_conflict_resolver_message_preserves_semantic_base():
  target = "a" * 40
  merge_base = "b" * 40

  content = pu._platform_conflict_resolver_message(
    target, ["backend/app/main.py"], merge_base,
  )

  assert "materialize_platform_conflict" in content
  assert target in content
  assert merge_base in content
  assert f"merge --no-ff {target}" not in content


def test_status_restart_needed_when_disk_head_changed_after_boot(clone_env):
  origin, platform = clone_env
  served = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")
  head = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'AFTER_BOOT'")})

  status = pu.platform_status(platform)

  assert status["state"] == pu.PlatformUpdateState.RESTART_NEEDED.value
  assert status["needs_restart"] is True
  assert _served_sha(platform) == head


def test_status_restart_needed_when_constitution_changed_after_boot(clone_env):
  origin, platform = clone_env
  served = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")
  _local_commit(platform, edits={"skill/core.md": "updated constitution\n"})

  status = pu.platform_status(platform)

  assert status["state"] == pu.PlatformUpdateState.RESTART_NEEDED.value
  assert status["needs_restart"] is True


@pytest.mark.asyncio
async def test_apply_marks_restart_when_disk_already_ahead_of_running_backend(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  served = _served_sha(platform)
  head = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'AFTER_BOOT'")})
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")

  monkeypatch.setattr(pu, "_reconcile_under_lock", lambda repo, at_boot, **kwargs: (
    pu.ReconcileResult("up_to_date", head, head, pu.recorded_upstream_sha(platform))
  ))

  target = pu.recorded_upstream_sha(platform)
  res = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(head, target, platform),
  )

  assert res["state"] == pu.PlatformUpdateState.RESTART_NEEDED.value
  assert res["needs_restart"] is True
  assert pu._read_activation_marker() == {
    "target_sha": head,
    "paths": ["backend/app/main.py"],
  }


# --- path-aware activation classifier ---------------------------------------

def test_platform_update_uses_explicit_activation_levels():
  classify = platform_activation.classify_activation
  assert classify(["backend/app/main.py"])["level"] == \
    "server_restart"
  assert classify(["backend/config_helper.py"])["level"] == \
    "server_restart"
  assert classify(["skill/core.md"])["level"] == \
    "server_restart"
  assert classify(["backend/requirements.txt"])["level"] == \
    "dependency_sync"
  assert classify(["backend/scripts/entrypoint.sh"])["level"] == \
    "image_rebuild"
  assert classify(["Caddyfile"])["level"] == "proxy_reload"
  assert classify(["frontend/src/App.jsx"])["level"] == "live"
  assert classify(["backend/tests/test_x.py"])["level"] == "live"
  assert classify(["docs/backend/app/notes.md"])["level"] == "live"


def test_import_probe_classifier_excludes_constitution_only_change():
  assert platform_activation.backend_import_probe_required(
    ["skill/core.md"]
  ) is False
  assert platform_activation.backend_import_probe_required([
    "skill/core.md", "backend/app/chat.py",
  ]) is True


def test_constitution_only_reconcile_skips_backend_import_probe(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  new = _advance_origin(origin, edits={"skill/core.md": "new rules\n"})

  def unexpected_probe(*_args, **_kwargs):
    raise AssertionError("constitution-only update must not boot a probe server")

  monkeypatch.setattr(pu, "_import_probe", unexpected_probe)

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "updated"
  assert res.new_sha == new


def test_changed_paths_no_renames_surfaces_deleted_backend(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  # git mv a runtime module out of backend/app/ — rename detection would hide
  # that the served backend lost it; --no-renames must surface the delete side.
  _git(platform, "mv", "backend/app/main.py", "moved_main.py")
  _git(platform, "commit", "-q", "-m", "move main out of app")
  after = _git(platform, "rev-parse", "HEAD").stdout.strip()

  paths = pu._changed_paths(platform, before, after)
  assert "backend/app/main.py" in paths  # delete side present
  assert platform_activation.classify_activation(paths)["level"] == "server_restart"


def test_empty_commit_does_not_force_restart(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  _git(platform, "commit", "-q", "--allow-empty", "-m", "empty")
  after = _git(platform, "rev-parse", "HEAD").stdout.strip()

  assert before != after
  assert pu._changed_paths(platform, before, after) == []  # genuine empty diff
  assert pu._activation_paths_between(platform, before, after) == []


def test_activation_paths_fail_closed_on_missing_sha(clone_env):
  origin, platform = clone_env
  served = _served_sha(platform)
  # one side unknown → can't prove no backend change → restart (fail closed)
  assert pu._activation_paths_between(platform, served, None) == ["backend/app"]
  assert pu._activation_paths_between(platform, None, served) == ["backend/app"]
  # both missing / equal → nothing changed
  assert pu._activation_paths_between(platform, None, None) == []
  assert pu._activation_paths_between(platform, served, served) == []


def test_status_no_restart_when_only_frontend_changed(clone_env):
  origin, platform = clone_env
  served = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")
  # HEAD advances past the served sha, but only a frontend file changed — the
  # served uvicorn doesn't import frontend, so no restart prompt.
  _local_commit(platform, edits={"frontend/src/App.jsx": "// changed\n"})

  status = pu.platform_status(platform)

  assert status["needs_restart"] is False
  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value


def test_status_no_restart_when_only_tests_changed(clone_env):
  origin, platform = clone_env
  served = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")
  # A test-only advance is the owner's exact complaint ("a single test file …
  # offered a restart") — it must not.
  _local_commit(platform, edits={"backend/tests/test_thing.py": "def test_x():\n  assert True\n"})

  status = pu.platform_status(platform)

  assert status["needs_restart"] is False
  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value


def test_status_offers_in_place_restart_for_dependency_changes(
  clone_env,
):
  _, platform = clone_env
  served = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")
  _local_commit(platform, edits={"backend/requirements.txt": "new-package==1\n"})

  status = pu.platform_status(platform)

  # A Python dependency change is installed in place by Apply and then loaded by
  # a restart — no image rebuild — so it is offered as an in-product restart.
  assert status["state"] == pu.PlatformUpdateState.RESTART_NEEDED.value
  assert status["needs_restart"] is True
  assert status["activation"]["level"] == "dependency_sync"


def test_boot_clears_restart_but_preserves_unverified_image_work(clone_env):
  _, platform = clone_env
  target = _served_sha(platform)
  pu.mark_activation_needed(
    target,
    ["backend/app/main.py", "frontend/package-lock.json"],
  )

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "up_to_date"
  assert pu._read_activation_marker() == {
    "target_sha": target,
    "paths": ["frontend/package-lock.json"],
  }


def test_boot_retires_in_place_python_dependency_sync(clone_env):
  # Owner Apply installs the locked Python deps in place BEFORE writing this
  # marker, so a fresh boot that loads the target already has them — retire like
  # a restart, not preserved as unverified image work.
  _, platform = clone_env
  target = _served_sha(platform)
  pu.mark_activation_needed(
    target,
    ["backend/app/main.py", "backend/requirements.lock"],
  )

  res = pu.reconcile_clone(platform, at_boot=True)

  assert res.status == "up_to_date"
  # Every path retired (deps like a restart, code by the restart) -> no pending
  # activation work at all.
  assert pu._read_activation_marker() is None


def test_apply_installs_python_dependencies_in_place(clone_env, monkeypatch):
  origin, platform = clone_env
  target = _advance_origin(
    origin,
    edits={"backend/requirements.lock": "new-locked-deps\n"},
    msg="bump python deps",
  )
  pu._fetch(platform)

  synced = {}

  def fake_sync(repo):
    synced["ran"] = True
    return True, ""

  monkeypatch.setattr(pu, "_sync_python_dependencies", fake_sync)

  res = pu.reconcile_clone(platform, target_ref=target, fetch_remote=False)

  assert res.status == "updated"
  assert synced.get("ran") is True  # the in-place install ran during Apply
  assert _served_sha(platform) == target


def test_apply_rolls_back_when_dependency_install_fails(clone_env, monkeypatch):
  origin, platform = clone_env
  pre = pu._rev(platform, pu._local_branch(platform))
  target = _advance_origin(
    origin,
    edits={"backend/requirements.lock": "new-locked-deps\n"},
    msg="bump python deps",
  )
  pu._fetch(platform)

  monkeypatch.setattr(
    pu, "_sync_python_dependencies", lambda repo: (False, "boom"),
  )

  res = pu.reconcile_clone(platform, target_ref=target, fetch_remote=False)

  # A dependency install failure is fail-closed: reset to the pre-reconcile
  # commit and serve the old tree, exactly like a failed import probe.
  assert res.status == "rolled_back"
  assert "boom" in (res.error or "")
  assert pu._rev(platform, pu._local_branch(platform)) == pre


# --- restart flag lifecycle -------------------------------------------------

def test_boot_reconcile_clears_restart_flag(clone_env):
  origin, platform = clone_env
  pu.mark_activation_needed("some-sha", ["backend/app"])
  assert pu.RESTART_NEEDED_FLAG.exists()
  # A boot with no new deploy is up_to_date; a boot IS the restart the flag asks
  # for, so it clears (the fresh process serves the on-disk code).
  res = pu.reconcile_clone(platform, at_boot=True)
  assert res.status == "up_to_date"
  assert not pu.RESTART_NEEDED_FLAG.exists()


def test_non_boot_reconcile_keeps_restart_flag(clone_env):
  origin, platform = clone_env
  pu.mark_activation_needed("some-sha", ["backend/app"])
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
  pu.mark_activation_needed("some-sha", ["backend/app"])
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
  pu._write_conflict_flag(
    "tgt-sha",
    ["backend/app/a.py", "backend/app/b.py"],
    "chat-42",
    "base-tree",
  )
  assert pu._read_conflict_flag() == {
    "upstream": "tgt-sha", "chat_id": "chat-42",
    "merge_base": "base-tree",
    "paths": ["backend/app/a.py", "backend/app/b.py"],
  }
  pu.CONFLICT_FLAG.write_text("tgt-sha\nbackend/app/a.py")
  legacy = pu._read_conflict_flag()
  assert legacy["chat_id"] is None
  assert legacy["merge_base"] is None
  assert legacy["paths"] == ["backend/app/a.py"]


def test_rolled_back_flag_roundtrips(clone_env):
  pu._write_rolled_back_flag("tgt-sha", "ModuleNotFoundError: app.foo")
  got = pu._read_rolled_back_flag()
  assert got["target"] == "tgt-sha"
  assert "ModuleNotFoundError" in got["error"]


def test_update_progress_is_durable_across_worker_memory(clone_env):
  _, platform = clone_env
  target = _served_sha(platform)
  original = dict(pu._UPDATE_PROGRESS)
  try:
    pu._set_update_progress(
      pu.PlatformUpdatePhase.BUILDING,
      plan_id="a" * 64,
      target_sha=target,
      active=True,
    )
    pu._UPDATE_PROGRESS.update(
      plan_id=None,
      target_sha=None,
      phase=pu.PlatformUpdatePhase.IDLE.value,
      active=False,
      error=None,
      updated_at=0.0,
    )

    recovered = pu.platform_update_progress()

    assert recovered["phase"] == pu.PlatformUpdatePhase.BUILDING.value
    assert recovered["active"] is True
    assert recovered["plan_id"] == "a" * 64
    assert stat.S_IMODE(pu.UPDATE_PROGRESS_PATH.stat().st_mode) == 0o600
  finally:
    pu._UPDATE_PROGRESS.update(original)


# --- update preview: the read-only "review before Apply" surface ------------
# platform_update_preview is fetch-free (it reads the origin/main left by the
# last fetch), so each test fetches first to mirror the real order: Check
# fetches, then Update opens the preview.


def test_update_preview_up_to_date_is_empty(clone_env):
  origin, platform = clone_env
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)

  assert preview["available"] is False
  assert preview["plan_id"] is None
  assert preview["total_commits"] == 0
  assert preview["commits_truncated"] is False
  assert preview["commits"] == []
  assert preview["files"] == []
  assert preview["diff"] is None
  assert preview["diff_truncated"] is False


def test_update_preview_holds_reconcile_lock_for_consistent_snapshot(
  clone_env, monkeypatch,
):
  _, platform = clone_env
  events = []

  @contextmanager
  def observed_lock():
    events.append("entered")
    try:
      yield
    finally:
      events.append("exited")

  monkeypatch.setattr(pu, "_reconcile_flock", observed_lock)

  pu.platform_update_preview(platform)

  assert events == ["entered", "exited"]


def test_update_preview_clean_fast_forward(clone_env):
  origin, platform = clone_env
  new = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 300")}, msg="bump line c")
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)

  assert preview["available"] is True
  assert preview["target_sha"] == new
  assert preview["plan_id"] == pu._update_plan_id(
    preview["current_sha"], preview["target_sha"],
  )
  assert preview["total_commits"] == 1
  assert preview["commits_truncated"] is False
  assert [c["subject"] for c in preview["commits"]] == ["bump line c"]
  changed = {f["path"] for f in preview["files"]}
  assert "backend/app/main.py" in changed
  assert "LINE_C = 300" in preview["diff"]
  assert preview["diff_truncated"] is False
  assert preview["activation"]["level"] == "server_restart"


def test_update_preview_shows_dependency_change_applies_in_place(clone_env):
  origin, platform = clone_env
  _advance_origin(
    origin,
    edits={"backend/requirements.lock": "locked dependency bytes\n"},
    msg="change dependency",
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)

  assert preview["activation"]["level"] == "dependency_sync"
  guidance = " ".join(preview["activation"]["guidance"])
  assert "in place" in guidance
  assert "rebuild the image" not in guidance


def test_update_preview_excludes_local_edits(clone_env):
  # A committed local edit must NOT appear in the preview — the owner reviews
  # only the upstream-side changes a clean Apply pulls in.
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 111")})
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 333")})
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)

  assert preview["available"] is True
  assert "LINE_C = 333" in preview["diff"]  # upstream change is shown
  assert "LINE_A = 111" not in preview["diff"]  # local edit is excluded


def test_update_preview_reports_file_status(clone_env):
  origin, platform = clone_env
  _advance_origin(
    origin,
    edits={"backend/app/added.py": "NEW = 1\n"},
    deletes=["backend/app/foo.py"],
    msg="add + delete",
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)

  status_by_path = {f["path"]: f["status"] for f in preview["files"]}
  assert status_by_path.get("backend/app/added.py") == "A"
  assert status_by_path.get("backend/app/foo.py") == "D"


def test_update_preview_caps_large_diff(clone_env):
  origin, platform = clone_env
  huge = "x" * (pu.MAX_PREVIEW_DIFF_CHARS + 50_000) + "\n"
  _advance_origin(origin, edits={"backend/app/big.py": huge}, msg="big file")
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)

  assert preview["diff_truncated"] is True
  assert len(preview["diff"]) == pu.MAX_PREVIEW_DIFF_CHARS


def test_update_preview_reports_exact_total_beyond_rendered_commit_cap(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  work = origin.parent / "origin-work"
  # The contract is that counting remains exact beyond the rendered cap, not
  # that every full-suite worker must manufacture the production-sized history.
  # Keeping this fixture small also avoids several parallel CI workers churning
  # hundreds of temporary loose Git objects at once.
  monkeypatch.setattr(pu, "_PREVIEW_COMMIT_LIMIT", 5)
  total = pu._PREVIEW_COMMIT_LIMIT + 7
  for index in range(total):
    (work / "release-counter.txt").write_text(f"{index}\n")
    _git(work, "add", "release-counter.txt")
    _git(work, "commit", "-q", "-m", f"release {index}")
  _git(work, "push", "-q", "origin", "main")
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)

  assert preview["total_commits"] == total
  assert len(preview["commits"]) == pu._PREVIEW_COMMIT_LIMIT
  assert preview["commits_truncated"] is True


def test_update_preview_degrades_when_not_a_clone(tmp_path, monkeypatch):
  # A non-git directory must degrade to an empty preview, never raise, so the
  # route + Settings can't break on a missing/odd platform tree. It also has no
  # source snapshot to protect and therefore must not need the durable /data
  # reconcile lock (which may be unavailable on a recovery/read-only surface).
  @contextmanager
  def fail_if_locked():
    raise AssertionError("non-clone preview attempted to acquire reconcile lock")
    yield

  monkeypatch.setattr(pu, "_reconcile_flock", fail_if_locked)
  preview = pu.platform_update_preview(tmp_path)

  assert preview["available"] is False
  assert preview["diff"] is None
  assert preview["commits"] == []


@pytest.mark.asyncio
async def test_apply_installs_exact_preview_target_without_refetching_moving_origin(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  reviewed = _advance_origin(
    origin,
    edits={"backend/app/main.py":
      _MAIN_PY.replace("LINE_C = 3", "LINE_C = 301")},
    msg="reviewed release",
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform)
  newer = _advance_origin(
    origin,
    edits={"backend/app/main.py":
      _MAIN_PY.replace("LINE_C = 301", "LINE_C = 302")},
    msg="newer release",
  )
  def fail_fetch(_repo):
    raise AssertionError("immutable Apply must not fetch the moving remote")

  monkeypatch.setattr(pu, "_fetch", fail_fetch)

  result = await pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=preview["current_sha"],
    target_sha=preview["target_sha"],
    repo=platform,
  )

  assert result["upstream_commit"] == reviewed
  assert _served_sha(platform) == reviewed
  assert "LINE_C = 301" in (platform / "backend/app/main.py").read_text()
  assert "LINE_C = 302" not in (platform / "backend/app/main.py").read_text()
  # The canonical remote really advanced, but Apply used the already-reviewed
  # object without moving this clone's tracking ref or incurring another fetch.
  assert newer != reviewed
  assert pu._rev(platform, pu.DEFAULT_TARGET_REF) == reviewed


@pytest.mark.asyncio
async def test_apply_rejects_plan_when_local_tip_changed_after_preview(clone_env):
  origin, platform = clone_env
  _advance_origin(
    origin,
    edits={"backend/app/main.py":
      _MAIN_PY.replace("LINE_C = 3", "LINE_C = 303")},
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform)
  changed = _local_commit(
    platform,
    edits={"backend/app/local.py": "LOCAL = True\n"},
  )

  with pytest.raises(pu.PlatformUpdateError, match="update_plan_stale"):
    await pu.apply_platform_update(
      SimpleNamespace(),
      plan_id=preview["plan_id"],
      current_sha=preview["current_sha"],
      target_sha=preview["target_sha"],
      repo=platform,
    )

  assert _served_sha(platform) == changed
  progress = pu.platform_update_progress()
  assert progress["phase"] == pu.PlatformUpdatePhase.FAILED.value
  assert progress["active"] is False
  assert progress["error"] == "update_plan_stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("started_with_upstream_marker", [True, False])
async def test_frontend_build_failure_rolls_back_source_and_is_not_success(
  monkeypatch, clone_env, started_with_upstream_marker,
):
  origin, platform = clone_env
  before = _served_sha(platform)
  if not started_with_upstream_marker:
    pu._clear_upstream(platform)
  previous_upstream = pu.recorded_upstream_sha(platform)
  pu.RESTART_NEEDED_FLAG.write_text("preexisting-restart")
  target = _advance_origin(
    origin,
    edits={"frontend/src/App.jsx": "export default 'broken candidate'\n"},
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform)

  def fail_build(_repo, _result):
    raise RuntimeError("vite exploded")

  monkeypatch.setattr(
    pu, "_rebuild_frontend_after_update_if_needed", fail_build,
  )

  result = await pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=preview["current_sha"],
    target_sha=preview["target_sha"],
    repo=platform,
  )

  assert result["state"] == pu.PlatformUpdateState.ROLLED_BACK.value
  assert result["phase"] == pu.PlatformUpdatePhase.BLOCKED.value
  # The failed newer release does not erase activation already pending before
  # this Apply attempt.
  assert result["needs_restart"] is True
  assert result["activation"]["level"] == "server_restart"
  assert result["upstream_commit"] == target
  assert result["merge_commit"] is None
  assert _served_sha(platform) == before
  assert pu.recorded_upstream_sha(platform) == previous_upstream
  assert pu.RESTART_NEEDED_FLAG.read_text() == "preexisting-restart"
  assert not (platform / "frontend/src/App.jsx").exists()
  rollback = pu._read_rolled_back_flag()
  assert rollback["target"] == target
  assert "frontend_build_failed" in rollback["error"]
  assert "vite exploded" in rollback["error"]
  progress = pu.platform_update_progress()
  assert progress["phase"] == pu.PlatformUpdatePhase.BLOCKED.value
  assert progress["active"] is False
  assert "frontend_build_failed" in progress["error"]


@pytest.mark.asyncio
async def test_stack_release_boot_is_exact_while_owner_updates_follow_main(
  clone_env, monkeypatch, tmp_path,
):
  origin, platform = clone_env
  work = origin.parent / "origin-work"
  release_branch = "stack/external-recovery-v1"
  release_ref = f"refs/heads/{release_branch}"
  _git(work, "checkout", "-q", "-b", release_branch)
  (work / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 303")
  )
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", "release bridge")
  baked_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
  _git(work, "push", "-q", "origin", release_branch)

  # The channel advances after this older image was built. Its boot must fetch
  # the new tip for membership proof but must not ingest that newer commit.
  (work / "newer-release.txt").write_text("must wait for next image\n")
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", "newer release")
  newer_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
  _git(work, "push", "-q", "origin", release_branch)

  _local_commit(
    platform,
    edits={
      "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 111"),
    },
  )
  build_info = tmp_path / "build-info.json"
  build_info.write_text(f'{{"sha":"{baked_sha}"}}\n')
  monkeypatch.setattr(release_channel, "BUILD_INFO_PATH", build_info)
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    release_ref,
  )

  summary = pu.reconcile_clone_sync()

  assert "reconcile[updated]" in summary
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 111" in served
  assert "LINE_C = 303" in served
  assert not (platform / "newer-release.txt").exists()
  assert pu.recorded_upstream_sha(platform) == baked_sha
  assert pu._rev(
    platform, f"refs/remotes/origin/{release_branch}",
  ) == newer_sha
  assert pu._is_ancestor(platform, baked_sha, "main")
  assert not pu._is_ancestor(platform, newer_sha, "main")
  assert "managed_release[ready]" in pu.managed_release_ready_sync()

  # The release channel above is a boot-only constraint. Owner status follows
  # origin/main, which is already contained in the release-derived local tree.
  status = pu.platform_status(platform)
  original_main = pu._rev(platform, pu.DEFAULT_TARGET_REF)
  assert status["contained_upstream_sha"] == original_main
  assert status["available"] is False
  assert "updates_disabled" not in status

  # A later main release is visible, reviewable, and applicable without
  # changing the managed image's exact boot target. Model the real protected
  # stack clone: its configured default fetch refspec does not include main, so
  # the owner check must request main explicitly.
  _git(platform, "config", "--unset-all", "remote.origin.fetch")
  _git(
    platform, "config", "--add", "remote.origin.fetch",
    f"+{release_ref}:refs/remotes/origin/{release_branch}",
  )
  _git(work, "checkout", "-q", "main")
  owner_target = _advance_origin(
    origin,
    edits={"owner-update.txt": "available from Settings\n"},
    msg="owner-visible main update",
  )
  checked = pu.check_for_updates(platform)
  assert pu._rev(platform, pu.DEFAULT_TARGET_REF) == owner_target
  assert checked["available"] is True
  assert checked["contained_upstream_sha"] == baked_sha

  preview = pu.platform_update_preview(platform)
  assert preview["available"] is True
  assert preview["target_sha"] == owner_target
  result = await pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=preview["current_sha"],
    target_sha=preview["target_sha"],
    repo=platform,
  )

  assert result["state"] in {
    pu.PlatformUpdateState.UP_TO_DATE.value,
    pu.PlatformUpdateState.RESTART_NEEDED.value,
  }
  assert (platform / "owner-update.txt").read_text() == (
    "available from Settings\n"
  )
  assert pu._is_ancestor(platform, owner_target, "main")
  assert pu._is_ancestor(platform, baked_sha, "main")
  assert pu.recorded_upstream_sha(platform) == owner_target
  assert "managed_release[ready]" in pu.managed_release_ready_sync()


def test_release_channel_rejects_baked_sha_outside_fetched_ref_without_mutation(
  clone_env, monkeypatch, tmp_path,
):
  origin, platform = clone_env
  work = origin.parent / "origin-work"
  before = _served_sha(platform)

  _git(work, "checkout", "-q", "-b", "release/external-recovery")
  (work / "release-only.txt").write_text("release\n")
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", "release-only")
  _git(work, "push", "-q", "origin", "release/external-recovery")
  _git(work, "checkout", "-q", "main")
  (work / "main-only.txt").write_text("not on release\n")
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", "main-only")
  outside_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
  _git(work, "push", "-q", "origin", "main")
  assert pu._fetch(platform) is True  # make the outside object locally visible

  build_info = tmp_path / "build-info.json"
  build_info.write_text(f'{{"sha":"{outside_sha}"}}\n')
  monkeypatch.setattr(release_channel, "BUILD_INFO_PATH", build_info)
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    "refs/heads/release/external-recovery",
  )

  summary = pu.reconcile_clone_sync()

  assert "release_target_outside_ref" in summary
  assert _served_sha(platform) == before
  assert not (platform / "release-only.txt").exists()
  assert not (platform / "main-only.txt").exists()
  assert pu.recorded_upstream_sha(platform) == before
  with pytest.raises(
    pu.PlatformUpdateError,
    match="managed_release_target_not_integrated",
  ):
    pu.managed_release_ready_sync()


def test_release_channel_deepens_before_rejecting_ambiguous_shallow_ancestry(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  work = origin.parent / "origin-work"
  _git(work, "checkout", "-q", "-b", "release/external-recovery")
  (work / "bridge.txt").write_text("bridge\n")
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", "bridge")
  baked_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
  (work / "next.txt").write_text("next\n")
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", "next")
  tip_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
  _git(work, "push", "-q", "origin", "release/external-recovery")

  tracking = "refs/remotes/origin/release/external-recovery"
  refspec = (
    "+refs/heads/release/external-recovery:"
    "refs/remotes/origin/release/external-recovery"
  )
  real_is_ancestor = pu._is_ancestor
  membership_checks = 0
  deepens = []

  def ambiguous_once(repo, ancestor, descendant):
    nonlocal membership_checks
    if ancestor == baked_sha and descendant == tip_sha:
      membership_checks += 1
      if membership_checks == 1:
        return False
    return real_is_ancestor(repo, ancestor, descendant)

  def deepen(repo, *, refspec=None):
    deepens.append((repo, refspec))

  monkeypatch.setattr(pu, "_is_shallow", lambda _repo: True)
  monkeypatch.setattr(pu, "_is_ancestor", ambiguous_once)
  monkeypatch.setattr(pu, "_fetch_unshallow", deepen)

  result = pu.reconcile_clone(
    platform,
    target_ref=baked_sha,
    fetch_refspec=refspec,
    release_tip_ref=tracking,
    at_boot=True,
  )

  assert result.status == "updated"
  assert deepens == [(platform, refspec)]
  assert membership_checks >= 2
  assert pu.recorded_upstream_sha(platform) == baked_sha
  assert not (platform / "next.txt").exists()


def test_managed_release_offline_prebridge_tree_is_not_ready_to_serve(
  clone_env, monkeypatch, tmp_path,
):
  _origin, platform = clone_env
  before = _served_sha(platform)
  missing_sha = "a" * 40
  build_info = tmp_path / "build-info.json"
  build_info.write_text(f'{{"sha":"{missing_sha}"}}\n')
  monkeypatch.setattr(release_channel, "BUILD_INFO_PATH", build_info)
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    "refs/heads/stack/external-recovery-v1",
  )
  monkeypatch.setattr(pu, "_fetch", lambda *_args, **_kwargs: False)

  summary = pu.reconcile_clone_sync()

  assert "reconcile[offline]" in summary
  assert _served_sha(platform) == before
  with pytest.raises(
    pu.PlatformUpdateError,
    match="managed_release_target_missing",
  ):
    pu.managed_release_ready_sync()


def test_invalid_managed_release_config_cannot_approve_prebridge_tree(
  clone_env, monkeypatch,
):
  _origin, platform = clone_env
  before = _served_sha(platform)
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  monkeypatch.setenv(
    release_channel.PLATFORM_RELEASE_REF_ENV,
    "stack/external-recovery-v1",
  )

  summary = pu.reconcile_clone_sync()

  assert "platform_release_ref_invalid" in summary
  assert _served_sha(platform) == before
  with pytest.raises(
    pu.PlatformUpdateError,
    match="platform_release_ref_invalid",
  ):
    pu.managed_release_ready_sync()


def test_entrypoint_ignores_durable_update_progress_from_outer_data_repo():
  entrypoint = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")

  assert ".platform-update-progress.json" in entrypoint
