"""Clone-native platform reconcile — the git plumbing that fetches origin and
replays the local overlay onto it without ever losing an edit or serving a
broken tree.

These drive ``platform_update.reconcile_clone`` against throwaway repos in
``tmp_path``: a bare ``origin`` repo, a ``platform`` clone of it (mirroring the
entrypoint bootstrap: local ``main`` + an ``upstream`` marker branch at HEAD),
and the module's ``/data`` flag paths monkeypatched into ``tmp_path`` so no real
platform tree is touched. Each platform tree carries a trivially-importable
``backend/app`` package so the post-replay import probe (a real ``import
app.main`` subprocess) exercises for real.

The load-bearing cases: a clean fast-forward advances the served tree; a
disjoint local edit is replayed as the same linear commit on the new base; a
contribution that landed upstream under a squash identity disappears from the
overlay instead of conflicting; a conflict parks the candidate worktree and
keeps serving the old code until the resolver finishes it; a clean replay
whose result fails to import rolls back; uncommitted edits come back
uncommitted; an offline fetch keeps serving unchanged; a crash-interrupted
reconcile is cleaned up on the next pass; and a merge-shaped legacy history is
folded into one linear overlay unit.
"""

import hashlib
import json
import os
import subprocess
import stat
import textwrap
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import app_git, platform_activation
from app import platform_update as pu


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


def _parents(platform: Path, ref: str = "HEAD") -> list[str]:
  return _git(platform, "show", "-s", "--format=%P", ref).stdout.split()


def _overlay_subjects(platform: Path, base: str) -> list[str]:
  """Subjects of the linear overlay ``base..HEAD`` in replay order."""
  assert _git(
    platform, "rev-list", "--merges", "--count", f"{base}..HEAD",
  ).stdout.strip() == "0"
  return [
    line for line in _git(
      platform, "log", "--reverse", "--format=%s", f"{base}..HEAD",
    ).stdout.splitlines() if line
  ]


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
  monkeypatch.setattr(pu, "DEPENDENCY_RECEIPT_PATH", tmp_path / ".dependency-inputs")
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

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  assert _served_sha(platform) == new == res.new_sha
  assert "LINE_C = 300" in (platform / "backend/app/main.py").read_text()
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists()
  # upstream marker advanced to the reconciled target.
  assert pu.recorded_upstream_sha(platform) == new
  # A second boot with no new deploy is a no-op.
  assert pu.reconcile_clone(platform).status == "up_to_date"


def test_up_to_date_retires_integrated_provenance(clone_env, monkeypatch):
  _origin, platform = clone_env
  target = _served_sha(platform)
  calls = []
  monkeypatch.setattr(
    app_git,
    "retire_landed_equivalent_changes",
    lambda repo, upstream: calls.append((repo, upstream)) or 2,
  )

  result = pu.reconcile_clone(platform)

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

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  assert _served_sha(platform) == new


# --- V-B2: disjoint local edit replayed as a linear overlay -----------------

def test_local_edit_preserved_across_update(clone_env):
  origin, platform = clone_env
  _local_commit(
    platform,
    edits={"backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 111")},
    msg=app_git.overlay_message(
      "keep line A local", unit="line-a", disposition="local-only",
    ),
  )
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 333")})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 111" in served  # local edit
  assert "LINE_C = 333" in served  # upstream edit
  # The overlay is replayed ON TOP of the exact upstream target: one linear
  # commit with its original subject and trailers, no merge parent.
  assert _parents(platform, res.new_sha) == [target]
  assert _overlay_subjects(platform, target) == ["keep line A local"]
  assert pu.recorded_upstream_sha(platform) == target
  assert res.overlay == {
    "replayed_units": ["line-a"], "dropped_units": [], "dropped_commits": [],
  }
  status = pu.platform_status(platform)
  assert status["overlay"]["linear"] is True
  assert [(u["id"], u["disposition"]) for u in status["overlay"]["units"]] == [
    ("line-a", "local-only"),
  ]
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu._overlay_candidate_path(platform).exists()


def test_replay_drops_a_local_commit_already_present_upstream(clone_env):
  """The same change arriving upstream makes the local copy vanish."""
  origin, platform = clone_env
  same = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'SAME'")
  _local_commit(platform, edits={"backend/app/main.py": same}, msg="local same")
  keep = _local_commit(
    platform, edits={"backend/app/foo.py": "VALUE = 'keep'\n"}, msg="local keep",
  )
  target = _advance_origin(origin, edits={"backend/app/main.py": same})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  assert _overlay_subjects(platform, target) == ["local keep"]
  assert res.overlay["dropped_commits"] == [
    _git(platform, "rev-parse", f"{keep}~1").stdout.strip(),
  ]
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'keep'\n"


def test_up_to_date_overlay_has_no_local_units(clone_env):
  _origin, platform = clone_env
  status = pu.platform_status(platform)
  assert status["overlay"]["linear"] is True
  assert status["overlay"]["units"] == []
  assert status["overlay"]["commits"] == 0


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

  res = pu.reconcile_clone(platform)

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

  res = pu.reconcile_clone(platform)

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
  # Freshly pinned: origin/main IS the conflicting target, nothing newer yet.
  assert status["newer_updates_available"] is False


def test_conflict_flags_newer_updates_when_origin_advances(clone_env):
  """A pinned conflict reports ``newer_updates_available`` once origin/main moves
  past the version it is pinned to, so Settings can offer one combined
  review+resolve instead of forcing a resolve per stacked release."""
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})

  assert pu.reconcile_clone(platform).status == "conflict"
  assert pu.platform_status(platform)["newer_updates_available"] is False

  # A later deploy lands upstream; the owner's fetch advances origin/main past
  # the version the conflict is pinned to.
  _advance_origin(origin, edits={"docs/RELEASE.md": "note\n"}, msg="later deploy")
  _git(platform, "fetch", "-q", "origin")

  status = pu.platform_status(platform)
  assert status["state"] == pu.PlatformUpdateState.CONFLICT.value
  assert status["newer_updates_available"] is True


def test_contributed_squash_uses_provenance_for_both_histories(clone_env):
  """The shell auto-reconciles a reviewed change returned under a new SHA.

  The local line evolves after review, while upstream squash-merges the reviewed
  predecessor and adds a separate release edit.  A per-commit replay of the
  reviewed original would conflict; the landed provenance anchor identifies it
  by patch-id and drops it, so only the follow-up is replayed onto the target.
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

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 'LOCAL FOLLOWUP'" in served
  assert "LINE_C = 9001" in served
  assert _overlay_subjects(platform, target) == ["local followup"]
  assert res.overlay["dropped_commits"] == [reviewed]
  assert pre not in _git(platform, "rev-list", "HEAD").stdout.split()
  assert not pu.CONFLICT_FLAG.exists()
  assert not app_git.ref_exists(platform, landed)


def test_partial_provenance_parks_only_the_genuine_conflict(clone_env):
  """A genuine later conflict keeps the proven contribution out of resolution.

  The reviewed commit is dropped by provenance; the follow-up commit conflicts
  on foo.py only. The replay parks in the candidate worktree with main.py
  already carrying both sides, the served tree stays at the old code, and the
  resolver finishes the update from that worktree into a linear result.
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
  res = pu.reconcile_clone(platform)

  assert res.status == "conflict"
  # The net conflict set is reported for context; the parked pick is the
  # actionable one and covers foo.py only — the reviewed main.py change was
  # dropped by provenance and the follow-up applied cleanly.
  assert set(res.conflict_paths) == {"backend/app/main.py", "backend/app/foo.py"}
  parked = res.overlay
  assert parked["paths"] == ["backend/app/foo.py"]
  assert parked["sha"] == pre
  assert parked["remaining"] == []
  assert parked["skip"] == [reviewed]
  assert _served_sha(platform) == pre
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'LOCAL'\n"
  assert not pu._reconcile_in_progress(platform)
  flag = pu._read_conflict_flag()
  assert flag["overlay"]["worktree"] == str(pu._overlay_candidate_path(platform))
  worktree = Path(flag["overlay"]["worktree"])
  assert "LINE_A = 'LOCAL FOLLOWUP'" in (worktree / "backend/app/main.py").read_text()
  assert "LINE_C = 9001" in (worktree / "backend/app/main.py").read_text()
  assert "<<<<<<< " in (worktree / "backend/app/foo.py").read_text()
  assert app_git.ref_exists(platform, landed)

  # Still unresolved: the continuation refuses rather than committing markers.
  with pytest.raises(pu.PlatformUpdateError):
    pu.continue_platform_overlay_update(platform)

  (worktree / "backend/app/foo.py").write_text("VALUE = 'RESOLVED'\n")
  _git(worktree, "add", "backend/app/foo.py")
  assert pu.continue_platform_overlay_update(platform) == "updated"

  assert _overlay_subjects(platform, target) == ["later local edits"]
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 'LOCAL FOLLOWUP'" in served and "LINE_C = 9001" in served
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'RESOLVED'\n"
  assert pu.recorded_upstream_sha(platform) == target
  assert not pu.CONFLICT_FLAG.exists()
  assert not worktree.exists()
  assert not app_git.ref_exists(platform, landed)


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

  res = pu.reconcile_clone(platform)

  # The replay parks at the first conflicting commit, but the flag still
  # reports every net conflict so the resolver knows what lies behind it.
  assert res.status == "conflict"
  assert set(res.conflict_paths) == {
    "backend/app/main.py", "backend/app/foo.py",
  }
  assert res.overlay["paths"] == ["backend/app/main.py"]
  assert len(res.overlay["remaining"]) == 1
  assert not pu._reconcile_in_progress(platform)


@pytest.mark.asyncio
async def test_second_conflict_parks_again_and_abandon_serves_old(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")}, msg="local main")
  pre = _local_commit(
    platform, edits={"backend/app/foo.py": "VALUE = 'LOCAL'\n"}, msg="local foo",
  )
  target = _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
    "backend/app/foo.py": "VALUE = 'UPSTREAM'\n",
  })

  res = pu.reconcile_clone(platform)
  assert res.status == "conflict"
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "conflict"
  flag = pu._read_conflict_flag()
  assert flag["overlay"]["subject"] == "local foo"
  assert flag["overlay"]["paths"] == ["backend/app/foo.py"]
  assert flag["overlay"]["remaining"] == []
  assert _served_sha(platform) == pre

  # A repeated owner Apply must neither restart the replay under the resolver
  # nor erase the parked continuation when it rewrites the conflict flag.
  applied = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(pre, target, platform),
  )
  assert applied["state"] == pu.PlatformUpdateState.CONFLICT.value
  assert pu._read_conflict_flag() == flag
  assert _served_sha(platform) == pre

  assert pu.abandon_platform_overlay_update(platform) == "abandoned"
  assert not pu.CONFLICT_FLAG.exists()
  assert not worktree.exists()
  assert _served_sha(platform) == pre
  assert pu.recorded_upstream_sha(platform) != target
  assert pu.platform_status(platform)["available"] is True


def test_replay_failure_serves_old_without_a_resolver_flag(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 'UPSTREAM'")})
  # A previous attempt may have left either durable review state behind. A git
  # failure with nothing a resolver can act on must return "error" and clear
  # them so the next status read does not lie about what just happened.
  pu._write_conflict_flag("b" * 40, ["backend/app/old.py"])
  pu._write_rolled_back_flag("c" * 40, "old import failure")

  def explode(*_args, **_kwargs):
    raise RuntimeError("cherry-pick wedged")

  monkeypatch.setattr(app_git, "replay_overlay", explode)

  res = pu.reconcile_clone(platform)

  assert res.status == "error"
  assert "cherry-pick wedged" in res.error
  assert res.target_sha == target
  assert _served_sha(platform) == pre
  assert not pu._reconcile_in_progress(platform)
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists()
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_legacy_merge_history_folds_into_one_linear_unit(clone_env):
  """An installation shaped by the old merge model converges on its first
  update: the net local delta becomes one ``legacy-overlay`` commit on the exact
  new target, and the next update is an ordinary replay."""
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'LOCAL'\n"},
                msg="old local")
  first = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 30")})
  _git(platform, "fetch", "-q", "origin")
  _git(platform, "merge", "--no-ff", "-m", "platform: merge upstream", first)
  assert len(_parents(platform)) == 2
  second = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 300")})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  assert res.overlay["legacy"] is True
  assert _parents(platform) == [second]
  assert _overlay_subjects(platform, second) == [
    f"platform: local overlay carried onto {second[:12]}",
  ]
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'LOCAL'\n"
  assert "LINE_C = 300" in (platform / "backend/app/main.py").read_text()
  status = pu.platform_status(platform)
  assert status["overlay"]["linear"] is True
  assert [u["id"] for u in status["overlay"]["units"]] == ["legacy-overlay"]

  third = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 3000")})
  res2 = pu.reconcile_clone(platform)
  assert res2.status == "updated"
  assert res2.overlay["replayed_units"] == ["legacy-overlay"]
  assert _parents(platform) == [third]


# --- V-B4: import-broken text-clean merge -> rollback -----------------------

def test_import_broken_merge_rolls_back(clone_env):
  origin, platform = clone_env
  # A disjoint local edit (so the merge is text-clean), while upstream DELETES
  # foo.py — which main.py still imports. Textually clean, import-broken.
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY + "LOCAL = 'kept'\n"})
  _advance_origin(origin, deletes=["backend/app/foo.py"], msg="drop foo")

  res = pu.reconcile_clone(platform)

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
  res2 = pu.reconcile_clone(platform)
  assert res2.status == "rolled_back"
  assert _served_sha(platform) == pre


# --- V-B5: offline fetch keeps serving --------------------------------------

def test_offline_fetch_serves_current_unchanged(clone_env, monkeypatch):
  origin, platform = clone_env
  before = _served_sha(platform)
  # Point origin at a dead path so the fetch fails.
  _git(platform, "remote", "set-url", "origin", str(platform.parent / "does-not-exist.git"))

  res = pu.reconcile_clone(platform)

  assert res.status == "offline"
  assert _served_sha(platform) == before  # unchanged, no crash, no data loss
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists()


# --- uncommitted working-tree edits are never lost --------------------------

def test_uncommitted_edits_come_back_uncommitted(clone_env):
  origin, platform = clone_env
  # An uncommitted local edit on disk (no commit) + a disjoint upstream deploy.
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY'"))
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 999")})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  served = (platform / "backend/app/main.py").read_text()
  assert "LINE_A = 'DIRTY'" in served  # uncommitted edit preserved
  assert "LINE_C = 999" in served
  # ...and it is still an uncommitted edit: the served commit is exactly the
  # upstream target and `git status` reads as it did before the update.
  assert _served_sha(platform) == target == res.new_sha
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/main.py",
  ]
  assert pu.platform_status(platform)["overlay"]["units"] == []


def test_uncommitted_edits_stay_uncommitted_across_a_parked_conflict(clone_env):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})

  res = pu.reconcile_clone(platform)

  assert res.status == "conflict"
  # The served checkout is the committed local code plus the same dirty edit;
  # the transient working-tree commit never stays in served history.
  assert _served_sha(platform) == pre
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]
  # The edit rode along as a transient commit that is NOT remaining work: it
  # is carried afresh when the replay continues, so later edits are not lost.
  assert res.overlay["working"] == res.pre_sha
  assert res.overlay["served"] == pre
  assert res.overlay["remaining"] == []
  assert res.pre_sha != pre

  # A restart (boot reconcile) must not restart the replay under the resolver.
  again = pu.reconcile_clone(platform)
  assert again.status == "conflict"
  assert again.overlay == res.overlay
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]

  # The owner keeps editing while the conflict is parked; the resolver then
  # finishes the replay. Both the parked commit and the newest working edits
  # land, and the edits come back uncommitted on the updated tree.
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY AGAIN'\n")
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "updated"

  target = _git(platform, "rev-parse", "origin/main").stdout.strip()
  assert _overlay_subjects(platform, target) == ["local edit"]
  assert "LINE_A = 'BOTH'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY AGAIN'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]
  assert pu.recorded_upstream_sha(platform) == target
  assert not pu.CONFLICT_FLAG.exists()
  assert not worktree.exists()


def test_a_dirty_edit_that_itself_conflicts_is_parked_and_continued(clone_env):
  origin, platform = clone_env
  served = _served_sha(platform)
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY'"))
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})

  res = pu.reconcile_clone(platform)

  assert res.status == "conflict"
  assert res.overlay["sha"] == res.overlay["working"]
  assert _served_sha(platform) == served
  assert "LINE_A = 'DIRTY'" in (platform / "backend/app/main.py").read_text()

  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'MERGED'"),
  )
  _git(worktree, "add", "backend/app/main.py")
  assert pu.continue_platform_overlay_update(platform) == "updated"

  # The resolved edit is still an uncommitted edit on the exact new target.
  assert _served_sha(platform) == target
  assert "LINE_A = 'MERGED'" in (platform / "backend/app/main.py").read_text()
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/main.py",
  ]


def test_continue_runs_the_same_post_replay_gates_as_apply(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
    "backend/requirements.lock": "pkg==2\n",
    "frontend/package-lock.json": "new-locked-frontend-deps\n",
    "frontend/src/App.jsx": "export default 'upstream'\n",
  })
  gates, rebuilt = [], []
  monkeypatch.setattr(
    pu, "_sync_python_dependencies",
    lambda repo: gates.append("python") or (True, ""),
  )
  monkeypatch.setattr(
    pu, "_sync_frontend_dependencies",
    lambda repo: gates.append("frontend") or (True, ""),
  )
  monkeypatch.setattr(
    pu, "_rebuild_frontend",
    lambda repo, res: gates.append("build") or rebuilt.append(res.new_sha),
  )

  res = pu.reconcile_clone(platform)
  assert res.status == "conflict"
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "updated"

  # The same gates, in the same order, as an owner Apply: the frontend deps
  # land before the build so it never compiles against stale node_modules.
  assert gates == ["python", "frontend", "build"]
  assert rebuilt == [_served_sha(platform)]
  assert not (platform / "frontend" / ".source-build-signature").exists()


def test_continue_rolls_back_a_candidate_that_fails_a_gate(clone_env, monkeypatch):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
    "backend/requirements.lock": "pkg==2\n",
  })
  monkeypatch.setattr(
    pu, "_sync_python_dependencies", lambda repo: (False, "pip exploded"),
  )
  res = pu.reconcile_clone(platform)
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "rolled_back"

  assert _served_sha(platform) == pre
  assert "pip exploded" in pu._read_rolled_back_flag()["error"]
  assert not pu.CONFLICT_FLAG.exists()


def test_legacy_fold_keeps_uncommitted_edits_uncommitted(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'LOCAL'\n"},
                msg="old local")
  first = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 30")})
  _git(platform, "fetch", "-q", "origin")
  _git(platform, "merge", "--no-ff", "-m", "platform: merge upstream", first)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  second = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 300")})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated", res
  assert res.overlay["legacy"] is True
  # The fold is one committed unit on the exact target; the edit is not in it.
  assert _overlay_subjects(platform, second) == [
    f"platform: local overlay carried onto {second[:12]}",
  ]
  assert "LINE_C = 300" in (platform / "backend/app/main.py").read_text()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]
  assert _git(platform, "show", "HEAD:backend/app/foo.py").stdout == "VALUE = 'LOCAL'\n"


def _landed_review(platform, base, head, contribution_id, target):
  diff = app_git._canonical_diff(platform, base, head)
  digest = hashlib.sha256(diff).hexdigest()
  assert app_git.record_pending_equivalent_change(
    platform, base_sha=base, head_sha=head, source_sha=head,
    diff_sha256=digest, contribution_id=contribution_id,
  )
  landed = app_git.mark_equivalent_change_landed(
    platform, digest, upstream_sha=target,
  )
  assert landed
  return landed


def test_a_multi_commit_unit_that_landed_as_one_review_is_dropped(clone_env):
  origin, platform = clone_env
  base = _served_sha(platform)
  step_one = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'ONE'")
  step_two = step_one.replace("LINE_C = 3", "LINE_C = 'TWO'")
  _local_commit(platform, edits={"backend/app/main.py": step_one},
                msg=app_git.overlay_message("part one", unit="pair", disposition="local-only"))
  head = _local_commit(platform, edits={"backend/app/main.py": step_two},
                       msg=app_git.overlay_message("part two", unit="pair", disposition="local-only"))
  keep = _local_commit(platform, edits={"backend/app/bar.py": "VALUE = 'keep'\n"},
                       msg=app_git.overlay_message("keep", unit="keep", disposition="local-only"))
  # Upstream squashed both parts into one commit and added a release edit.
  target = _advance_origin(origin, edits={
    "backend/app/main.py": step_two,
    "backend/app/foo.py": "VALUE = 'release'\n",
  }, msg="squash both parts")
  _landed_review(platform, base, head, "pair-review", target)

  res = pu.reconcile_clone(platform)

  assert res.status == "updated", res
  assert res.overlay["dropped_units"] == ["pair"]
  assert res.overlay["replayed_units"] == ["keep"]
  assert _overlay_subjects(platform, target) == ["keep"]
  assert (platform / "backend/app/bar.py").read_text() == "VALUE = 'keep'\n"
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'release'\n"
  assert keep not in _git(platform, "rev-list", "HEAD").stdout.split()


def test_a_whitespace_different_change_is_not_mistaken_for_the_review(clone_env):
  """Byte-exact proof: an indentation-sensitive difference must conflict
  honestly instead of being dropped as "already landed"."""
  origin, platform = clone_env
  base = _served_sha(platform)
  reviewed_text = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'X'")
  reviewed = _local_commit(platform, edits={"backend/app/main.py": reviewed_text},
                           msg="reviewed")
  # The local line evolves to a whitespace-only variant of the reviewed one.
  _local_commit(platform, edits={"backend/app/main.py":
    reviewed_text.replace("LINE_A = 'X'", "LINE_A =  'X'")}, msg="respaced")
  target = _advance_origin(origin, edits={"backend/app/main.py": reviewed_text})
  _landed_review(platform, base, reviewed, "exact-review", target)

  res = pu.reconcile_clone(platform)

  # `reviewed` is exactly the review and vanishes; `respaced` is a real,
  # different change and is replayed as its own overlay commit.
  assert res.status == "updated", res
  assert res.overlay["dropped_commits"] == [reviewed]
  assert _overlay_subjects(platform, target) == ["respaced"]
  assert "LINE_A =  'X'" in (platform / "backend/app/main.py").read_text()


def test_detached_head_uncommitted_edit_survives_reconcile(clone_env):
  origin, platform = clone_env
  _git(platform, "checkout", "-q", "--detach", "HEAD")
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DETACHED_DIRTY'"))
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 1001")})

  res = pu.reconcile_clone(platform)

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
  res = pu.reconcile_clone(platform)
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

  res = pu.reconcile_clone(platform)

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
  assert status["contained_upstream_committed_at"] == _git(
    platform, "show", "-s", "--format=%cI", new,
  ).stdout.strip()
  assert status["upstream_checked_at"] is not None


def test_successful_update_check_refreshes_the_reported_fetch_time(clone_env):
  _, platform = clone_env
  assert pu._fetch(platform)
  fetch_head = Path(_git(
    platform, "rev-parse", "--git-path", "FETCH_HEAD",
  ).stdout.strip())
  if not fetch_head.is_absolute():
    fetch_head = platform / fetch_head
  os.utime(fetch_head, (1_700_000_000, 1_700_000_000))
  before = pu.platform_status(platform)["upstream_checked_at"]

  status = pu.check_for_updates(platform)

  assert before == "2023-11-14T22:13:20+00:00"
  assert status["upstream_checked_at"] is not None
  assert status["upstream_checked_at"] > before


# --- owner Apply rebuilds stale frontend dist after frontend updates ----------

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

  def fake_reconcile(repo, **kwargs):
    result = pu.ReconcileResult(
      "updated", served, new, new, hook_source_sha=new,
    )
    pu._rebuild_frontend(repo, result)
    return result

  monkeypatch.setattr(pu, "_reconcile_under_lock", fake_reconcile)
  monkeypatch.setattr(
    pu, "_refresh_git_hooks",
    lambda repo, source_oid: hook_calls.append((repo, source_oid)) or "",
  )
  monkeypatch.setattr(pu, "_rebuild_frontend", fake_rebuild)

  res = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(served, new, platform),
  )

  # The frontend rebuilds into dist (served per-request), but the served uvicorn
  # imports no frontend — so no restart is prompted. Owner's exact complaint.
  assert res["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
  assert res["needs_restart"] is False
  assert calls == [(platform, new)]
  assert hook_calls == [(platform, new)]


def test_boot_refreshes_hooks_from_installed_upstream(clone_env, monkeypatch):
  _, platform = clone_env
  trusted = _served_sha(platform)
  _local_commit(platform, edits={"local.txt": "local"})
  calls = []
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  monkeypatch.setattr(pu, "_refresh_git_hooks", lambda repo, source: calls.append((repo, source)) or "")
  summary = pu.reconcile_clone_sync()
  assert calls == [(platform, trusted)]
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
    repo_path, *, target_ref, fetch_remote, progress,
  ):
    assert events == ["locked"]
    assert repo_path == repo
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

  result = pu._reconcile_under_lock(repo)

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

  def fake_reconcile(repo, **kwargs):
    result = pu.ReconcileResult("updated", served, new, new)
    pu._rebuild_frontend(repo, result)
    return result

  monkeypatch.setattr(pu, "_reconcile_under_lock", fake_reconcile)
  monkeypatch.setattr(pu, "_rebuild_frontend", fake_rebuild)
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
  monkeypatch.setattr(pu, "_reconcile_under_lock", lambda repo, **kwargs: (
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


@pytest.mark.asyncio
async def test_apply_conflict_preserves_proven_merge_base(
  monkeypatch, clone_env,
):
  """The apply-path conflict rewrite must carry the equivalence engine's
  proven semantic base into the flag, or the resolver chat re-surfaces
  conflicts already proven to have landed upstream."""
  origin, platform = clone_env
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})
  merge_base = "b" * 40

  monkeypatch.setattr(pu, "_reconcile_under_lock", lambda repo, **kwargs: (
    pu.ReconcileResult(
      "conflict", _served_sha(platform), _served_sha(platform), target,
      ["backend/app/main.py"],
      merge_base=merge_base,
    )
  ))

  current = _served_sha(platform)
  res = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(current, target, platform),
  )

  assert res["state"] == pu.PlatformUpdateState.CONFLICT.value
  flag = pu._read_conflict_flag()
  assert flag["upstream"] == target
  assert flag["merge_base"] == merge_base
  assert flag["chat_id"] is None


@pytest.mark.asyncio
async def test_platform_conflict_resolver_chat_is_click_gated(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'")})
  pu._fetch(platform)
  # Legacy/incomplete flags fall back to moving origin/main.
  pu._write_conflict_flag(None, ["backend/app/main.py"])
  calls = []

  async def fake_spawn(db, paths, target_sha, merge_base, overlay=None):
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


def test_platform_conflict_resolver_message_points_at_the_parked_worktree():
  target = "a" * 40
  parked = {
    "worktree": "/data/platform/.git/mobius-overlay-candidate",
    "sha": "c" * 40, "subject": "keep line A local", "unit": "line-a",
    "paths": ["backend/app/main.py"], "remaining": ["d" * 40, "e" * 40],
  }

  content = pu._platform_conflict_resolver_message(
    target, ["backend/app/main.py", "backend/app/foo.py"], None, parked,
  )

  assert parked["worktree"] in content
  assert "continue_platform_overlay_update" in content
  assert "abandon_platform_overlay_update" in content
  assert "keep line A local" in content
  assert "remaining 2 local commit(s)" in content
  assert "merge --no-ff" not in content
  assert "materialize_platform_conflict" not in content
  # The resolver must never edit the served checkout.
  assert "not in `/data/platform`" in content


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

  monkeypatch.setattr(pu, "_reconcile_under_lock", lambda repo, **kwargs: (
    pu.ReconcileResult("up_to_date", head, head, pu.recorded_upstream_sha(platform))
  ))

  target = pu.recorded_upstream_sha(platform)
  res = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(head, target, platform),
  )

  assert res["state"] == pu.PlatformUpdateState.RESTART_NEEDED.value
  assert res["needs_restart"] is True
  assert pu._read_activation_marker() == {
    "version": 2,
    "target_sha": head,
    "upstream_sha": target,
    "paths": ["backend/app/main.py"],
    "image_paths": [],
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


def test_container_replacement_blocks_local_only_image_inputs(tmp_path, monkeypatch):
  marker = tmp_path / "activation.json"
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  pu._write_activation_marker(
    "a" * 40,
    ["Dockerfile", "backend/app/main.py"],
    upstream_sha="b" * 40,
    image_paths=[],
  )

  assert pu.container_replacement_blockers() == ["Dockerfile"]


def test_container_replacement_accepts_image_input_covered_by_upstream(
  tmp_path, monkeypatch,
):
  marker = tmp_path / "activation.json"
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  pu._write_activation_marker(
    "a" * 40,
    ["Dockerfile"],
    upstream_sha="a" * 40,
    image_paths=["Dockerfile"],
  )

  assert pu.container_replacement_blockers() == []


def test_active_runtime_overlay_owns_protected_runtime_blockers(
  tmp_path, monkeypatch,
):
  marker = tmp_path / "activation.json"
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  pu._write_activation_marker(
    "a" * 40,
    ["Dockerfile", "backend/runtime/identity_broker.py"],
    upstream_sha="b" * 40,
    image_paths=[],
  )

  assert pu.container_replacement_blockers() == ["Dockerfile"]


def test_stale_protected_runtime_restores_image_activation_without_marker(
  clone_env, monkeypatch, tmp_path,
):
  _, platform = clone_env
  head = _local_commit(
    platform,
    edits={"backend/runtime/identity_broker.py": "wanted\n"},
  )
  deployed = tmp_path / "deployed-runtime"
  deployed.mkdir()
  (deployed / "identity_broker.py").write_text("old\n", encoding="utf-8")
  monkeypatch.setenv("MOBIUS_PROTECTED_RUNTIME_DIR", str(deployed))
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(head + "\n")

  status = pu.platform_status(platform)

  assert status["state"] == pu.PlatformUpdateState.ACTIVATION_NEEDED.value
  assert status["activation"]["level"] == "image_rebuild"
  assert status["activation"]["reasons"] == [{
    "code": "baked_runtime",
    "summary": "Baked scripts, supervisors, or protected-file rules changed.",
    "paths": ["backend/runtime/identity_broker.py"],
  }]


def test_replacement_leaves_local_runtime_drift_to_the_active_runtime_overlay(
  clone_env,
):
  _, platform = clone_env
  official = _git(platform, "rev-parse", "HEAD").stdout.strip()
  _local_commit(
    platform,
    edits={"backend/runtime/identity_broker.py": "local-only\n"},
  )

  assert pu.container_replacement_blockers(official, platform) == []


def test_replacement_blocks_unmarked_local_image_input_drift(clone_env):
  """Direct local commits to image inputs never write an activation marker,
  yet the official image would silently replace them; the blockers check must
  derive that drift from the histories themselves."""
  _, platform = clone_env
  official = _git(platform, "rev-parse", "HEAD").stdout.strip()

  assert pu.container_replacement_blockers(official, platform) == []

  _local_commit(platform, edits={"Dockerfile": "FROM local-only\n"})
  assert pu.container_replacement_blockers(official, platform) == [
    "Dockerfile",
  ]


def test_reviewed_image_target_does_not_treat_incoming_dockerfile_as_local(
  clone_env,
):
  origin, platform = clone_env
  current = _served_sha(platform)
  target = _advance_origin(origin, edits={"Dockerfile": "FROM official-new\n"})
  _git(platform, "fetch", "origin")

  assert pu.container_replacement_blockers(
    target, platform, local_change_base=current,
  ) == []

  _local_commit(platform, edits={"Dockerfile": "FROM local-divergence\n"})
  assert pu.container_replacement_blockers(
    target, platform, local_change_base=current,
  ) == ["Dockerfile"]


def test_reviewed_image_plan_binds_digest_and_preserves_live_tree(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  current = _served_sha(platform)
  target = _advance_origin(origin, edits={"Dockerfile": "FROM official-new\n"})
  _git(platform, "fetch", "origin")
  digest = "sha256:" + "d" * 64
  monkeypatch.setattr(
    pu,
    "_protected_runtime_status",
    lambda _repo: {
      "state": "current",
      "source_sha256": "match",
      "deployed_sha256": "match",
      "mismatched_paths": [],
    },
  )

  preview = pu.platform_update_preview(
    platform, target_sha=target, image_digest=digest,
  )
  reviewed = pu.reviewed_container_rebuild_plan(
    repo=platform,
    plan_id=preview["plan_id"],
    current_sha=current,
    target_sha=target,
    image_digest=digest,
  )

  assert preview["image_digest"] == digest
  assert preview["plan_id"] == pu._update_plan_id(current, target, digest)
  assert reviewed["target_sha"] == target
  assert reviewed["activation"]["level"] == "image_rebuild"
  assert reviewed["blockers"] == []
  assert _served_sha(platform) == current

  with pytest.raises(pu.PlatformUpdateError, match="update_plan_invalid"):
    pu.reviewed_container_rebuild_plan(
      repo=platform,
      plan_id=preview["plan_id"],
      current_sha=current,
      target_sha=target,
      image_digest="sha256:" + "e" * 64,
    )


def test_explicit_ghcr_target_is_available_before_source_object_is_fetched(
  clone_env,
):
  _origin, platform = clone_env
  missing_release = "f" * 40

  status = pu.platform_status(platform, target_sha=missing_release)

  assert status["state"] == pu.PlatformUpdateState.AVAILABLE.value
  assert status["available"] is True
  assert _served_sha(platform) != missing_release


def test_explicit_ghcr_preview_fails_closed_when_source_fetch_fails(
  clone_env, monkeypatch,
):
  _origin, platform = clone_env
  served = _served_sha(platform)
  missing_release = "f" * 40
  monkeypatch.setattr(pu, "_fetch", lambda *_args, **_kwargs: False)

  with pytest.raises(
    pu.PlatformUpdateError,
    match="image_release_source_unavailable",
  ):
    pu.platform_update_preview(
      platform,
      target_sha=missing_release,
      image_digest="sha256:" + "d" * 64,
    )

  assert _served_sha(platform) == served


def test_replacement_blocks_image_input_renamed_out_of_its_owned_path(
  clone_env,
):
  """Rename detection must not hide the removed image-owned source path."""
  _, platform = clone_env
  official = _local_commit(
    platform, edits={"Dockerfile": "FROM official\n"}, msg="add image input",
  )

  (platform / "docs").mkdir()
  _git(platform, "mv", "Dockerfile", "docs/Dockerfile")
  _git(platform, "commit", "-q", "-m", "move image input out")

  assert pu.container_replacement_blockers(official, platform) == [
    "Dockerfile",
  ]


def test_stale_marker_coverage_cannot_excuse_newer_image_input_drift(
  clone_env,
):
  """A marker recorded content parity at Apply time; a later local commit to
  the same image input must still block the replacement."""
  _, platform = clone_env
  official = _git(platform, "rev-parse", "HEAD").stdout.strip()
  pu.RESTART_NEEDED_FLAG.write_text(json.dumps({
    "version": 2,
    "target_sha": official,
    "upstream_sha": official,
    "paths": ["Dockerfile"],
    "image_paths": ["Dockerfile"],
  }), encoding="utf-8")

  _local_commit(platform, edits={"Dockerfile": "FROM drifted-later\n"})
  assert pu.container_replacement_blockers(official, platform) == [
    "Dockerfile",
  ]


def test_marker_coverage_survives_newer_descendant_official_image(
  clone_env,
):
  """An official image path applied from one release is not local drift merely
  because a newer descendant release advances that same path."""
  origin, platform = clone_env
  applied = _advance_origin(
    origin, edits={"Dockerfile": "FROM official-applied\n"}, msg="applied image",
  )
  _git(platform, "fetch", "origin")
  _git(platform, "merge", "--ff-only", applied)
  pu._write_activation_marker(
    applied,
    ["Dockerfile"],
    upstream_sha=applied,
    image_paths=["Dockerfile"],
  )
  target = _advance_origin(
    origin, edits={"Dockerfile": "FROM official-newer\n"}, msg="newer image",
  )
  _git(platform, "fetch", "origin")

  assert pu.container_replacement_blockers(
    target, platform, local_change_base=applied,
  ) == []

  _local_commit(platform, edits={"Dockerfile": "FROM local-only\n"})
  assert pu.container_replacement_blockers(
    target, platform, local_change_base=applied,
  ) == ["Dockerfile"]


def test_legacy_activation_marker_cannot_claim_official_image_coverage(
  tmp_path, monkeypatch,
):
  marker = tmp_path / "activation.json"
  marker.write_text(
    '{"target_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"paths":["Dockerfile"]}',
    encoding="utf-8",
  )
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)

  assert pu.container_replacement_blockers() == ["Dockerfile"]


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

  res = pu.reconcile_clone(platform)

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

  paths = pu._activation_paths_between(platform, before, after)
  assert "backend/app/main.py" in paths  # delete side present
  assert platform_activation.classify_activation(paths)["level"] == "server_restart"


def test_empty_commit_does_not_force_restart(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  _git(platform, "commit", "-q", "--allow-empty", "-m", "empty")
  after = _git(platform, "rev-parse", "HEAD").stdout.strip()

  assert before != after
  assert pu._activation_paths_between(platform, before, after) == []  # genuine empty diff


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


def test_boot_clears_restart_but_preserves_unverified_image_work(clone_env, monkeypatch):
  _, platform = clone_env
  target = _served_sha(platform)
  pu.mark_activation_needed(
    target,
    ["backend/app/main.py", "Dockerfile"],
  )

  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  assert "startup[installed]" in pu.reconcile_clone_sync()
  assert pu._read_activation_marker() == {
    "version": 2,
    "target_sha": target,
    "upstream_sha": None,
    "paths": ["Dockerfile"],
    "image_paths": [],
  }


def test_manual_deploy_source_does_not_hide_pending_server_restart(clone_env):
  _, platform = clone_env
  served = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")
  _local_commit(platform, edits={
    "scripts/deploy-prod.sh": "# optional deployment command\n",
    "backend/app/main.py": _MAIN_PY + "NEW_SETTING = True\n",
  })

  status = pu.platform_status(platform)

  assert status["state"] == pu.PlatformUpdateState.RESTART_NEEDED.value
  assert status["needs_restart"] is True
  assert status["activation"]["level"] == "server_restart"


def test_boot_retires_old_manual_deploy_markers_without_dropping_real_host_work(clone_env):
  _, platform = clone_env
  target = _served_sha(platform)
  for remainder in ([], ["scripts/mobius-rebuild-host.py"]):
    pu._write_activation_marker(
      target, ["scripts/deploy-prod.sh", "backend/app/main.py", *remainder],
    )

    pu._complete_boot_activation(platform)

    marker = pu._read_activation_marker()
    if remainder:
      assert marker["paths"] == remainder
    else:
      assert marker is None


def test_boot_rebuild_retires_only_upstream_covered_image_paths(
  clone_env, monkeypatch,
):
  _, platform = clone_env
  upstream = _served_sha(platform)
  local = _local_commit(
    platform,
    edits={"protected-files.txt": "local-only image input\n"},
  )
  pu._write_activation_marker(
    local,
    ["Dockerfile", "protected-files.txt"],
    upstream_sha=upstream,
    image_paths=["Dockerfile"],
  )
  monkeypatch.setattr(pu, "current_build_sha", lambda: upstream)

  pu._complete_boot_activation(platform)

  assert pu._read_activation_marker() == {
    "version": 2,
    "target_sha": local,
    "upstream_sha": upstream,
    "paths": ["protected-files.txt"],
    "image_paths": [],
  }


def test_image_receipt_does_not_claim_compose_topology_was_applied(
  clone_env, monkeypatch,
):
  _, platform = clone_env
  upstream = _served_sha(platform)
  pu._write_activation_marker(
    upstream,
    ["docker-compose.yml"],
    upstream_sha=upstream,
    image_paths=["docker-compose.yml"],
  )
  monkeypatch.setattr(pu, "current_build_sha", lambda: upstream)

  pu._complete_boot_activation(platform)

  marker = pu._read_activation_marker()
  assert marker is not None
  assert marker["paths"] == ["docker-compose.yml"]


def test_boot_retires_in_place_python_dependency_sync(clone_env, monkeypatch):
  # Owner Apply installs the locked Python deps in place BEFORE writing this
  # marker, so a fresh boot that loads the target already has them — retire like
  # a restart, not preserved as unverified image work.
  _, platform = clone_env
  target = _served_sha(platform)
  pu.mark_activation_needed(
    target,
    ["backend/app/main.py", "backend/requirements.lock"],
  )

  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  assert "startup[installed]" in pu.reconcile_clone_sync()
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


@pytest.mark.asyncio
async def test_apply_installs_frontend_dependencies_in_place(
  clone_env, monkeypatch,
):
  # A frontend dependency bump lands via an in-place `npm ci` during Apply and
  # a live shell rebuild — no container/image rebuild, mirroring Python deps.
  origin, platform = clone_env
  target = _advance_origin(
    origin,
    edits={"frontend/package-lock.json": "new-locked-frontend-deps\n"},
    msg="bump frontend deps",
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform)

  # A frontend dep change is classified as an in-place live activation, not a
  # rebuild.
  assert preview["activation"]["level"] == "live"

  synced = {}

  def fake_sync(repo):
    synced["ran"] = True
    return True, ""

  monkeypatch.setattr(pu, "_sync_frontend_dependencies", fake_sync)
  monkeypatch.setattr(
    pu, "_rebuild_frontend", lambda repo, result: None,
  )

  result = await pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=preview["current_sha"],
    target_sha=preview["target_sha"],
    repo=platform,
  )

  assert result["state"] != pu.PlatformUpdateState.ROLLED_BACK.value
  assert result["state"] != pu.PlatformUpdateState.CONFLICT.value
  assert synced.get("ran") is True  # npm ci ran during Apply, before the build
  assert _served_sha(platform) == target


@pytest.mark.asyncio
async def test_apply_rolls_back_when_frontend_dependency_install_fails(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  before = _served_sha(platform)
  target = _advance_origin(
    origin,
    edits={"frontend/package-lock.json": "new-locked-frontend-deps\n"},
    msg="bump frontend deps",
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform)

  monkeypatch.setattr(
    pu, "_sync_frontend_dependencies", lambda repo: (False, "npm boom"),
  )
  # If the source is not reset, a subsequent real build could run; keep it a
  # no-op so the test isolates the dependency-install failure path.
  monkeypatch.setattr(
    pu, "_rebuild_frontend", lambda repo, result: None,
  )

  result = await pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=preview["current_sha"],
    target_sha=preview["target_sha"],
    repo=platform,
  )

  # A failed in-place frontend install is fail-closed like a failed build: the
  # source resets to the pre-Apply commit and the old tree keeps serving.
  assert result["state"] == pu.PlatformUpdateState.ROLLED_BACK.value
  assert "npm boom" in (result["error"] or "")
  assert _served_sha(platform) == before


# --- restart flag lifecycle -------------------------------------------------

def test_boot_clears_restart_flag(clone_env, monkeypatch):
  origin, platform = clone_env
  pu.mark_activation_needed("some-sha", ["backend/app"])
  assert pu.RESTART_NEEDED_FLAG.exists()
  # A boot with no new deploy is up_to_date; a boot IS the restart the flag asks
  # for, so it clears (the fresh process serves the on-disk code).
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  assert "startup[installed]" in pu.reconcile_clone_sync()
  assert not pu.RESTART_NEEDED_FLAG.exists()


def test_non_boot_reconcile_keeps_restart_flag(clone_env):
  origin, platform = clone_env
  pu.mark_activation_needed("some-sha", ["backend/app"])
  # An owner-apply reconcile (at_boot=False) must NOT clear the flag on an
  # up-to-date pass — the running process is unchanged.
  res = pu.reconcile_clone(platform)
  assert res.status == "up_to_date"
  assert pu.RESTART_NEEDED_FLAG.exists()


# --- regression: an OFFLINE boot still clears a stale restart flag. The clear is
# unconditional and early (not only on the success/up-to-date branches), so an
# owner Apply that set RESTART_NEEDED followed by an offline reboot — whose fetch
# fails and returns 'offline' before any later branch — does not leave a
# permanent "restart needed" prompt. -----------------------------------------

def test_offline_boot_clears_stale_restart_flag(clone_env, monkeypatch):
  origin, platform = clone_env
  pu.mark_activation_needed("some-sha", ["backend/app"])
  assert pu.RESTART_NEEDED_FLAG.exists()
  # Force the fetch to fail so the reconcile returns 'offline' BEFORE reaching any
  # success/up-to-date branch — the flag must still clear (the boot IS the restart
  # the flag asked for; the fresh process already serves the on-disk code).
  _git(platform, "remote", "set-url", "origin",
       str(platform.parent / "does-not-exist.git"))

  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  assert "startup[installed]" in pu.reconcile_clone_sync()
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
    "merge_base": "base-tree", "overlay": None,
    "paths": ["backend/app/a.py", "backend/app/b.py"],
  }
  pu.CONFLICT_FLAG.write_text("tgt-sha\nbackend/app/a.py")
  legacy = pu._read_conflict_flag()
  assert legacy["chat_id"] is None
  assert legacy["merge_base"] is None
  assert legacy["overlay"] is None
  assert legacy["paths"] == ["backend/app/a.py"]

  parked = {"worktree": "/x", "sha": "c" * 40, "paths": ["a"], "remaining": []}
  pu._write_conflict_flag("tgt-sha", ["a"], "chat-42", overlay=parked)
  assert pu._read_conflict_flag()["overlay"] == parked
  assert pu._read_conflict_flag()["paths"] == ["a"]


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


def test_missing_clone_is_unavailable_without_requiring_recovery_lock(tmp_path, monkeypatch):
  # Preserve recovery's no-writable-lock requirement, but return an explicit
  # unavailable result at the route instead of claiming the update is complete.
  @contextmanager
  def fail_if_locked():
    raise AssertionError("non-clone preview attempted to acquire reconcile lock")
    yield

  monkeypatch.setattr(pu, "_reconcile_flock", fail_if_locked)
  with pytest.raises(pu.PlatformUpdateError, match="platform_repo_missing"):
    pu.platform_update_preview(tmp_path)
  with pytest.raises(pu.PlatformUpdateError, match="platform_repo_missing"):
    pu.platform_status(tmp_path)


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

  monkeypatch.setattr(pu, "_rebuild_frontend", fail_build)

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
  assert "frontend_build_failed" in result["error"]
  assert _served_sha(platform) == before
  assert pu.recorded_upstream_sha(platform) == previous_upstream
  assert pu.RESTART_NEEDED_FLAG.read_text() == "preexisting-restart"
  assert not (platform / "frontend/src/App.jsx").exists()
  rollback = pu._read_rolled_back_flag()
  assert rollback["target"] == target
  assert "frontend_build_failed" in rollback["error"]
  assert "vite exploded" in rollback["error"]
  status = pu.platform_status(platform)
  assert status["rollback_target_sha"] == target
  assert "vite exploded" in status["rollback_error"]
  progress = pu.platform_update_progress()
  assert progress["phase"] == pu.PlatformUpdatePhase.BLOCKED.value
  assert progress["active"] is False
  assert "frontend_build_failed" in progress["error"]


def test_boot_policy_ignores_durable_update_progress_from_outer_data_repo():
  scripts = Path(__file__).resolve().parents[1] / "scripts"
  entrypoint = (scripts / "entrypoint.sh").read_text(encoding="utf-8")
  data_repo_helper = (scripts / "init_data_repo.py").read_text(
    encoding="utf-8",
  )

  assert "init_data_repo.py write-ignore /data" in entrypoint
  assert ".platform-update-progress.json" in data_repo_helper


def test_frontend_touching_update_invalidates_the_build_stamp(clone_env, monkeypatch):
  """A checkout moves frontend source without a watcher event. The stamp that
  lets the watcher skip its startup build must not survive such an update, so
  the served bundle is reported (and rebuilt) as behind the source."""
  origin, platform = clone_env
  monkeypatch.setattr(pu, "_rebuild_frontend", lambda repo, result: None)
  stamp = platform / "frontend" / ".source-build-signature"
  stamp.parent.mkdir(parents=True, exist_ok=True)
  stamp.write_text("matches-old-source\n")
  _advance_origin(origin, edits={"frontend/src/App.jsx": "export default 1\n"})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  assert not stamp.exists()

  stamp.write_text("matches-new-source\n")
  _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'v2'\n"})
  assert pu.reconcile_clone(platform).status == "updated"
  # A backend-only update leaves the frontend's own bookkeeping alone.
  assert stamp.read_text() == "matches-new-source\n"


# --- the running image compared with the served source -----------------------

def test_image_input_drift_feeds_the_activation_state(clone_env, monkeypatch, tmp_path):
  _origin, platform = clone_env
  (platform / "Dockerfile").write_text("FROM python:3.12\n")
  (platform / "backend" / "requirements.lock").write_text("a==1\n")
  _git(platform, "add", "-A")
  _git(platform, "commit", "-q", "-m", "image inputs")
  info = tmp_path / "build-info.json"
  monkeypatch.setenv("MOBIUS_BUILD_INFO_PATH", str(info))

  # An image that predates the record says nothing.
  info.write_text(json.dumps({"sha": "x"}))
  assert pu.image_input_drift(platform) is None

  # An image built from exactly these inputs matches.
  info.write_text(json.dumps({
    "sha": "x",
    "image_inputs": platform_activation.image_input_hashes(platform),
  }))
  assert pu.image_input_drift(platform) == []
  assert pu.platform_status(platform)["activation"]["level"] == "live"

  # A dependency bump is an in-place install; a Dockerfile change needs a new
  # image. Both come from state, not from remembering which update did it.
  (platform / "backend" / "requirements.lock").write_text("a==2\n")
  assert pu.image_input_drift(platform) == ["backend/requirements.lock"]
  assert pu.platform_status(platform)["activation"]["level"] == "dependency_sync"
  (platform / "Dockerfile").write_text("FROM python:3.13\n")
  assert pu.image_input_drift(platform) == [
    "Dockerfile", "backend/requirements.lock",
  ]
  assert pu.platform_status(platform)["activation"]["level"] == "image_rebuild"


def test_live_install_drift_reports_only_what_the_image_lacks(tmp_path):
  inventory = tmp_path / "inventory"
  inventory.mkdir()
  (inventory / "pip.txt").write_text("fastapi==0.1\nhttpx==2\n")
  (inventory / "apt.txt").write_text("curl=8\ngit=2\n")
  outputs = {
    "pip": "fastapi==0.1\nhttpx==3\nrich==13\n",
    "dpkg-query": "curl=8\ngit=2\nffmpeg=6\n",
  }

  def fake_run(command, **_kwargs):
    key = "pip" if "pip" in command else "dpkg-query"
    return SimpleNamespace(returncode=0, stdout=outputs[key])

  assert pu.live_install_drift(inventory, run=fake_run) == {
    "pip": ["httpx==3", "rich==13"],
    "apt": ["ffmpeg=6"],
  }
  # No inventories (an older image): nothing is claimed.
  assert pu.live_install_drift(tmp_path / "missing", run=fake_run) == {}
  # A failing query is not reported as drift.
  failing = lambda command, **_k: SimpleNamespace(returncode=1, stdout="")
  assert pu.live_install_drift(inventory, run=failing) == {}


def test_applied_image_update_has_a_finish_plan_without_new_source(clone_env):
  _, platform = clone_env
  target = _served_sha(platform)
  digest = "sha256:" + "d" * 64
  pu.mark_activation_needed(target, ["Dockerfile"], upstream_sha=target, repo=platform)

  preview = pu.platform_update_preview(platform, target_sha=target, image_digest=digest)

  assert preview["available"] is False
  assert preview["actionable"] is True
  assert preview["operation"] == "finish"
  assert preview["files"] == []
  assert preview["activation"]["level"] == "image_rebuild"
  assert preview["plan_id"] == pu._update_plan_id(target, target, digest)
  reviewed = pu.reviewed_container_rebuild_plan(
    repo=platform, current_sha=target, target_sha=target,
    image_digest=digest, plan_id=preview["plan_id"],
  )
  assert reviewed["activation"]["level"] == "image_rebuild"


def test_new_source_review_carries_unfinished_image_activation(clone_env):
  origin, platform = clone_env
  current = _served_sha(platform)
  pu.mark_activation_needed(current, ["Dockerfile"], upstream_sha=current, repo=platform)
  target = _advance_origin(origin, edits={"frontend/src/App.jsx": "new shell"})
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert preview["available"] is True
  assert preview["operation"] == "update"
  assert preview["actionable"] is True
  assert preview["activation"]["level"] == "image_rebuild"


@pytest.mark.asyncio
async def test_finish_plan_does_not_reapply_source_or_dependencies(clone_env, monkeypatch):
  _, platform = clone_env
  current = _served_sha(platform)
  pu.mark_activation_needed(current, ["Dockerfile"], upstream_sha=current, repo=platform)
  preview = pu.platform_update_preview(platform)

  def no_install(_repo):
    pytest.fail("A finish-only plan must not reinstall dependencies")

  monkeypatch.setattr(pu, "_sync_python_dependencies", no_install)
  monkeypatch.setattr(pu, "_sync_frontend_dependencies", no_install)
  result = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(current, current, platform),
  )

  assert preview["operation"] == "finish"
  assert result["activation"]["level"] == "image_rebuild"
  assert _served_sha(platform) == current
  assert result["merge_commit"] is None


def test_successful_live_dependency_sync_survives_restart_not_new_container(
  clone_env, monkeypatch,
):
  _, platform = clone_env
  lock = platform / "backend/requirements.lock"
  lock.write_text("old lock")
  baked = platform_activation.image_input_hashes(platform)
  monkeypatch.setattr(pu, "_build_info", lambda: {"image_inputs": baked})
  lock.write_text("new lock")
  with monkeypatch.context() as install:
    install.setattr(pu.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert pu._sync_python_dependencies(platform) == (True, "")

  assert pu.image_input_drift(platform) == []
  # A fresh module/process reads the same container-local receipt.
  assert pu._dependency_receipt()["backend/requirements.lock"] == hashlib.sha256(b"new lock").hexdigest()
  lock.write_text("later edit")
  assert pu.image_input_drift(platform) == ["backend/requirements.lock"]
  lock.write_text("new lock")
  pu.DEPENDENCY_RECEIPT_PATH.unlink()
  assert pu.image_input_drift(platform) == ["backend/requirements.lock"]


def test_failed_dependency_sync_invalidates_previous_success_receipt(
  clone_env, monkeypatch,
):
  _, platform = clone_env
  (platform / "backend/requirements.lock").write_text("lock")
  pu._record_dependency_inputs(platform, pu._PYTHON_DEPENDENCY_INPUTS, installed=True)
  with monkeypatch.context() as install:
    install.setattr(pu.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
      returncode=1, stdout="", stderr="partial installation failed",
    ))
    assert pu._sync_python_dependencies(platform)[0] is False
  assert pu._dependency_receipt()["backend/requirements.lock"] == ""
  # Even if source matches the original image, a failed install is not proof
  # that the runtime still matches: keep the dependency action visible.
  baked = platform_activation.image_input_hashes(platform)
  monkeypatch.setattr(pu, "_build_info", lambda: {"image_inputs": baked})
  assert pu.image_input_drift(platform) == ["backend/requirements.lock"]


def test_failed_import_restores_previous_declared_dependency_versions(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  initial = _advance_origin(origin, edits={"backend/requirements.lock": "old lock"})
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", initial)
  _git(platform, "branch", "-f", "upstream", initial)
  target = _advance_origin(origin, edits={
    "backend/requirements.lock": "new lock",
    "backend/app/main.py": "raise RuntimeError('broken candidate')",
  })
  installs = []
  monkeypatch.setattr(pu, "_sync_python_dependencies", lambda repo: (
    installs.append((repo / "backend/requirements.lock").read_text()) or (True, "")
  ))

  result = pu.reconcile_clone(platform)

  assert result.status == "rolled_back"
  assert result.target_sha == target
  assert _served_sha(platform) == initial
  assert installs == ["new lock", "old lock"]
  assert "dependency_restore_failed" not in result.error


@pytest.mark.asyncio
async def test_failed_frontend_build_restores_both_dependency_locks(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  initial = _advance_origin(origin, edits={
    "backend/requirements.lock": "old python",
    "frontend/package-lock.json": "old frontend",
  })
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", initial)
  _git(platform, "branch", "-f", "upstream", initial)
  target = _advance_origin(origin, edits={
    "backend/requirements.lock": "new python",
    "frontend/package-lock.json": "new frontend",
  })
  pu._fetch(platform)
  installs = []
  monkeypatch.setattr(pu, "_sync_python_dependencies", lambda repo: (
    installs.append((repo / "backend/requirements.lock").read_text()) or (True, "")
  ))
  monkeypatch.setattr(pu, "_sync_frontend_dependencies", lambda repo: (
    installs.append((repo / "frontend/package-lock.json").read_text()) or (True, "")
  ))
  def failed_build(repo, result):
    raise RuntimeError("cannot build")
  monkeypatch.setattr(pu, "_rebuild_frontend", failed_build)

  result = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(initial, target, platform),
  )

  assert result["state"] == "rolled_back"
  assert installs == ["new python", "new frontend", "old python", "old frontend"]
  assert _served_sha(platform) == initial


def test_dependency_restore_failure_is_visible_in_durable_rollback(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  initial = _advance_origin(origin, edits={"backend/requirements.lock": "old lock"})
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", initial)
  _git(platform, "branch", "-f", "upstream", initial)
  _advance_origin(origin, edits={"backend/requirements.lock": "new lock"})
  monkeypatch.setattr(pu, "_sync_python_dependencies", lambda repo: (False, "network unavailable"))

  result = pu.reconcile_clone(platform)

  assert result.status == "rolled_back"
  assert _served_sha(platform) == initial
  assert "dependency_restore_failed" in result.error
  assert "Repair them before restarting" in pu._read_rolled_back_flag()["error"]


@pytest.mark.parametrize("read", [pu.platform_status, pu.platform_update_preview])
@pytest.mark.parametrize("missing", ["origin", "target", "local"])
def test_unavailable_source_never_reports_update_complete(clone_env, read, missing):
  _origin, platform = clone_env
  if missing == "origin":
    _git(platform, "remote", "remove", "origin")
    code = "platform_origin_missing"
  elif missing == "target":
    _git(platform, "update-ref", "-d", "refs/remotes/origin/main")
    code = "platform_target_unavailable"
  else:
    _git(platform, "symbolic-ref", "HEAD", "refs/heads/missing")
    code = "platform_source_unavailable"
  with pytest.raises(pu.PlatformUpdateError, match=code):
    read(platform)


def test_finish_requires_ancestry_even_when_upstream_marker_survives_reset(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  target = _advance_origin(origin, edits={"release.txt": "new release\n"})
  pu._fetch(platform)
  _git(platform, "branch", "-f", "upstream", target)
  # The release marker moved but served source did not (equivalent to an owner
  # resetting the checkout after Apply). Finish must not reinstall it silently.
  assert _served_sha(platform) == before
  with pytest.raises(pu.PlatformUpdateError, match="applied_release_unavailable"):
    pu.applied_release_sha(platform)
  status = pu.platform_status(platform)
  assert status["contained_upstream_sha"] is None
  assert status["recorded_upstream_sha"] == target


def test_finish_stays_on_applied_release_when_newer_source_is_available(clone_env):
  origin, platform = clone_env
  applied = _served_sha(platform)
  newer = _advance_origin(origin, edits={"release.txt": "new release\n"})
  pu._fetch(platform)
  assert pu.platform_status(platform)["available"] is True
  assert pu.applied_release_sha(platform) == applied
  assert applied != newer


def test_finish_can_prove_applied_source_without_a_recorded_marker(clone_env):
  _origin, platform = clone_env
  applied = _served_sha(platform)
  pu._clear_upstream(platform)
  assert pu.applied_release_sha(platform) == applied


def test_verified_contained_target_is_still_an_empty_completed_review(clone_env):
  _origin, platform = clone_env
  result = pu.platform_update_preview(platform)
  assert result["state"] == "up_to_date"
  assert result["actionable"] is False
  assert result["operation"] == "none"


@pytest.mark.parametrize("cached_newer", [False, True])
def test_startup_never_fetches_or_installs_a_newer_release(clone_env, monkeypatch, cached_newer):
  origin, platform = clone_env
  installed = _served_sha(platform)
  local = _local_commit(platform, edits={"local.txt": "owner commit"})
  (platform / "local.txt").write_text("owner uncommitted edit")
  (platform / "untracked.txt").write_text("owner new file")
  _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'new release'\n"})
  if cached_newer:
    _git(platform, "fetch", "origin")
  before = _git(platform, "status", "--porcelain").stdout
  def forbidden(*args, **kwargs):
    pytest.fail("Startup must not fetch, replay source, or install packages")
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  for name in ("_fetch", "_fetch_unshallow", "reconcile_clone", "_sync_python_dependencies", "_sync_frontend_dependencies"):
    monkeypatch.setattr(pu, name, forbidden)
  assert "startup[installed]" in pu.reconcile_clone_sync()
  assert _served_sha(platform) == local
  assert pu.recorded_upstream_sha(platform) == installed
  assert (platform / "local.txt").read_text() == "owner uncommitted edit"
  assert (platform / "untracked.txt").read_text() == "owner new file"
  assert _git(platform, "status", "--porcelain").stdout == before


def test_startup_restores_interrupted_update_without_selecting_new_release(clone_env, monkeypatch):
  origin, platform = clone_env
  installed = _served_sha(platform)
  (platform / "owner.txt").write_text("unsaved-to-git owner work")
  carried = pu._carry_working_edits(platform, "main")
  pu._write_reconcile_pre(carried.pre)
  target = _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'candidate'\n"})
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", target)
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  assert "boot_guard[reset]" in pu.reconcile_clone_sync()
  assert _served_sha(platform) == installed
  assert (platform / "owner.txt").read_text() == "unsaved-to-git owner work"
  assert not pu.RECONCILE_PRE_FLAG.exists()
  assert _git(platform, "status", "--porcelain").stdout.strip() == "?? owner.txt"


def test_host_installer_replays_exact_bundled_release_and_preserves_local_edits(clone_env, monkeypatch, tmp_path):
  import importlib.util
  from app import database
  origin, platform = clone_env
  installed = _served_sha(platform)
  _local_commit(platform, edits={"local.txt": "owner commit"})
  (platform / "local.txt").write_text("owner working edit")
  target = _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'reviewed'\n"})
  bundle = tmp_path / "image.bundle"
  _git(origin.parent / "origin-work", "bundle", "create", str(bundle), "HEAD")
  _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'later unreviewed'\n"})
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(SimpleNamespace()))
  def unexpected_fetch(*args, **kwargs):
    pytest.fail("the operator installer must not discover another remote release")
  monkeypatch.setattr(pu, "_fetch", unexpected_fetch)
  script = Path(__file__).resolve().parents[1] / "scripts/install_platform_release.py"
  spec = importlib.util.spec_from_file_location("operator_install", script)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr("sys.argv", [str(script), "--target", target, "--bundle", str(bundle)])
  assert module.main() == 0
  assert pu._is_ancestor(platform, target, "HEAD")
  assert pu.recorded_upstream_sha(platform) == target
  assert pu._rev(platform, "origin/main") == installed
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'reviewed'\n"
  assert (platform / "local.txt").read_text() == "owner working edit"
  assert _git(platform, "status", "--porcelain").stdout.strip() == "M local.txt"


@pytest.mark.parametrize("deployment", ["self_hosted", "railway"])
def test_review_exposes_seed_customization_before_replacement_without_mutation(
  clone_env, monkeypatch, deployment,
):
  origin, platform = clone_env
  monkeypatch.setattr(platform_activation, "deployment_kind", lambda: deployment)
  paths = ["backend/scripts/seed-skills/cron.md", "backend/scripts/seed-skills/reflection.md"]
  _local_commit(platform, edits={path: "local instructions\n" for path in paths})
  target = _advance_origin(origin, edits={"Dockerfile": "FROM official-new\n"})
  pu._fetch(platform)
  before = _served_sha(platform)
  dirty = platform / "keep-working.txt"
  dirty.write_text("unfinished owner work")
  before_status = _git(platform, "status", "--porcelain").stdout

  preview = pu.platform_update_preview(platform, target_sha=target)
  reviewed = pu.reviewed_container_rebuild_plan(
    repo=platform, plan_id=preview["plan_id"], current_sha=before,
    target_sha=target, image_digest=None,
  )

  assert preview["blocking_paths"] == paths
  assert reviewed["blockers"] == preview["blocking_paths"]
  assert preview["activation"]["deployment"] == deployment
  assert _served_sha(platform) == before
  assert _git(platform, "status", "--porcelain").stdout == before_status
  assert dirty.read_text() == "unfinished owner work"
  assert all((platform / path).read_text() == "local instructions\n" for path in paths)


def test_review_does_not_block_seed_changes_already_in_the_official_release(clone_env):
  origin, platform = clone_env
  path = "backend/scripts/seed-skills/cron.md"
  _local_commit(platform, edits={path: "same useful instructions\n"})
  target = _advance_origin(origin, edits={path: "same useful instructions\n"})
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert preview["activation"]["level"] == "image_rebuild"
  assert preview["blocking_paths"] == []


def test_finish_review_exposes_local_image_blockers_too(clone_env):
  _, platform = clone_env
  official = _served_sha(platform)
  path = "backend/scripts/seed-skills/cron.md"
  _local_commit(platform, edits={path: "preserve me\n"})
  pu.mark_activation_needed(_served_sha(platform), [path], upstream_sha=official, repo=platform)

  preview = pu.platform_update_preview(platform, target_sha=official)

  assert preview["operation"] == "finish"
  assert preview["blocking_paths"] == [path]
