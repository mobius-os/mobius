"""Clone-native platform reconcile — preserve the final local tree across an
upstream update without replaying every historical local commit.

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
left untouched with an explicit normalization error.
"""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import stat
import textwrap
import threading
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


def _write_frontend_build(root: Path) -> None:
  dist = root / "frontend" / "dist"
  (dist / "assets").mkdir(parents=True, exist_ok=True)
  (dist / "assets" / "app.js").write_text("console.log('test')\n")
  (dist / "index.html").write_text("<main>test</main>\n")
  (dist / "sw.js").write_text("// test\n")
  (dist / "manifest.webmanifest").write_text("{}\n")


def _make_origin(tmp: Path) -> Path:
  """A bare ``origin`` repo with an initial commit carrying an importable
  backend, plus a working checkout used to push new commits ('deploys')."""
  origin = tmp / "origin.git"
  _git(tmp, "init", "--bare", "-b", "main", str(origin))
  work = tmp / "origin-work"
  _git(tmp, "clone", str(origin), str(work))
  (work / ".gitignore").write_text("__pycache__/\n*.pyc\nfrontend/dist/\n")
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
  monkeypatch.setattr(pu, "LATE_CHANGES_FLAG", tmp_path / ".late-changes")
  monkeypatch.setattr(pu, "LATE_SNAPSHOT_FLAG", tmp_path / ".late-snapshot")
  monkeypatch.setattr(pu, "OFFLINE_FLAG", tmp_path / ".offline")
  monkeypatch.setattr(pu, "RECONCILE_LOCK", tmp_path / ".reconcile.lock")
  monkeypatch.setattr(
    pu,
    "ACTIVATION_V2_CUTOVER_RECEIPT",
    tmp_path / ".platform-activation-v2",
  )
  monkeypatch.setattr(
    pu,
    "UPDATE_PROGRESS_PATH",
    tmp_path / ".update-progress.json",
  )
  monkeypatch.setenv("BUILD_SHA", "test-sha")
  origin = _make_origin(tmp_path)
  platform = _clone_platform(tmp_path, origin)
  _write_frontend_build(platform)
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
  # One commit carries the final local tree on the exact target, regardless of
  # how many historical local commits produced it.
  assert _parents(platform, res.new_sha) == [target]
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  assert pu.recorded_upstream_sha(platform) == target
  assert res.overlay["mode"] == "net"
  assert _git(platform, "rev-parse", f"refs/mobius/platform-pre-update/{res.pre_sha}").stdout.strip() == res.pre_sha
  status = pu.platform_status(platform)
  assert status["overlay"]["linear"] is True
  assert [(u["id"], u["disposition"]) for u in status["overlay"]["units"]] == [
    ("platform-local-tree", "local-only"),
  ]
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu._overlay_candidate_path(platform).exists()


def test_net_merge_omits_local_change_already_present_upstream(clone_env):
  """The same change arriving upstream makes the local copy vanish."""
  origin, platform = clone_env
  same = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'SAME'")
  _local_commit(platform, edits={"backend/app/main.py": same}, msg="local same")
  _local_commit(
    platform, edits={"backend/app/foo.py": "VALUE = 'keep'\n"}, msg="local keep",
  )
  target = _advance_origin(origin, edits={"backend/app/main.py": same})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
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
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  assert res.overlay["mode"] == "net"
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
  # Reviewed provenance removes the obsolete main.py conflict; the only
  # actionable net-tree conflict is the genuine foo.py overlap.
  assert set(res.conflict_paths) == {"backend/app/foo.py"}
  parked = res.overlay
  assert parked["paths"] == ["backend/app/foo.py"]
  assert parked["mode"] == "net"
  assert parked["stage"] == "committed"
  assert parked["served"] == pre
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

  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
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

  # One off-tree merge parks all genuine conflicts together, not one per
  # historical local commit.
  assert res.status == "conflict"
  assert set(res.conflict_paths) == {
    "backend/app/main.py", "backend/app/foo.py",
  }
  assert set(res.overlay["paths"]) == set(res.conflict_paths)
  assert res.overlay["mode"] == "net"
  assert not pu._reconcile_in_progress(platform)


def test_long_local_history_merges_once_and_never_replays_commits(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  original = _served_sha(platform)
  for number in range(35):
    _local_commit(
      platform,
      edits={"backend/app/foo.py": f"VALUE = 'LOCAL {number}'\n"},
      msg=f"historical local step {number}",
    )
  target = _advance_origin(
    origin,
    edits={"backend/app/main.py": _MAIN_PY.replace("LINE_C = 3", "LINE_C = 45")},
  )
  monkeypatch.setattr(
    app_git, "overlay_commits",
    lambda *_args, **_kwargs: pytest.fail("platform update enumerated history"),
  )

  result = pu.reconcile_clone(platform)

  assert result.status == "updated"
  assert _parents(platform) == [target]
  assert len(_overlay_subjects(platform, target)) == 1
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'LOCAL 34'\n"
  assert "LINE_C = 45" in (platform / "backend/app/main.py").read_text()
  assert _git(platform, "rev-parse", f"refs/mobius/platform-pre-update/{result.pre_sha}").stdout.strip() == result.pre_sha
  assert original != result.pre_sha


def _park_resolved_line_a_conflict(platform: Path, origin: Path) -> tuple[str, str, Path]:
  """Park a committed LINE_A conflict and stage the resolver's answer.

  Returns ``(served, target, worktree)`` with the resolution staged but not
  yet continued, so a test can make late live-source changes first.
  """
  served = _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'"),
  })
  target = _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
  })
  first = pu.reconcile_clone(platform)
  assert first.status == "conflict"
  worktree = Path(first.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED'"),
  )
  _git(worktree, "add", "backend/app/main.py")
  return served, target, worktree


def test_resolver_refuses_unstaged_final_bytes_without_losing_them(clone_env):
  origin, platform = clone_env
  _served, target, worktree = _park_resolved_line_a_conflict(platform, origin)
  main = worktree / "backend/app/main.py"
  # The index briefly contains a staged marker, then the resolver fixes the
  # working file without staging again. A second, new file is also unstaged.
  main.write_text(_MAIN_PY.replace("LINE_A = 1", "<<<<<<< ours\n"))
  _git(worktree, "add", "backend/app/main.py")
  main.write_text(_MAIN_PY.replace("LINE_A = 1", "LINE_A = 'FINAL'"))
  (worktree / "notes.txt").write_text("resolved with context\n")

  with pytest.raises(pu.PlatformUpdateError, match="Stage every intended"):
    pu.continue_platform_overlay_update(platform)
  assert "LINE_A = 'FINAL'" in main.read_text()
  assert (worktree / "notes.txt").read_text() == "resolved with context\n"
  assert "LINE_A = 'LOCAL'" in (platform / "backend/app/main.py").read_text()

  _git(worktree, "add", "backend/app/main.py", "notes.txt")
  assert pu.continue_platform_overlay_update(platform) == "updated"
  assert "LINE_A = 'FINAL'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "notes.txt").read_text() == "resolved with context\n"
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]


def _release_commit(platform: Path, target: str) -> str:
  """The one resolved local commit directly on the reviewed target."""
  return _git(
    platform, "rev-list", "--reverse", "--ancestry-path", f"{target}..HEAD",
  ).stdout.split()[0]


def test_late_clean_commits_wait_for_post_boot_merge(clone_env):
  origin, platform = clone_env
  served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  _local_commit(platform, edits={"backend/app/late.py": "LATE = 1\n"}, msg="late 1")
  late = _local_commit(
    platform, edits={"backend/app/late.py": "LATE = 2\n"}, msg="late 2",
  )

  assert pu.continue_platform_overlay_update(platform) == "updated_late_changes_pending"

  # The reviewed release is one commit; later work is not mixed into it.
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  release = _release_commit(platform, target)
  assert _parents(platform, release) == [target]
  assert _served_sha(platform) == release
  assert _git(platform, "ls-tree", "--name-only", release, "backend/app/").stdout.count(
    "late.py",
  ) == 0
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert f"Previous local source: {served}" in _git(
    platform, "show", "-s", "--format=%B", release,
  ).stdout
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["late_sha"] == late
  assert pending["paths"] == ["backend/app/late.py"]
  assert _git(platform, "show", f"{pending['ref']}:backend/app/late.py").stdout == "LATE = 2\n"


def test_late_conflicting_commit_is_pinned_while_the_frozen_release_activates(
  clone_env,
):
  origin, platform = clone_env
  _served, target, worktree = _park_resolved_line_a_conflict(platform, origin)
  late = _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LATE'"),
  }, msg="late conflicting edit")

  assert pu.continue_platform_overlay_update(platform) == (
    "updated_late_changes_pending"
  )

  # The release activates exactly as resolved; the late edit is not claimed.
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert pu.recorded_upstream_sha(platform) == target
  assert not pu.CONFLICT_FLAG.exists()
  assert not worktree.exists()
  ref = f"{pu._LATE_CHANGES_REF_PREFIX}{late}"
  assert _git(platform, "rev-parse", ref).stdout.strip() == late
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["ref"] == ref
  assert pending["committed_sha"] == late
  assert pending["uncommitted"] is False
  assert pending["release_sha"] == _served_sha(platform)
  assert pending["paths"] == ["backend/app/main.py"]

  # Merely making the ref an ancestor does not prove its content was kept.
  _git(platform, "merge", "-q", "-s", "ours", "-m", "take late later", ref)
  assert pu.platform_status(platform)["late_changes"]["ref"] == ref
  # Retiring the recovery ref is the explicit completion step.
  _git(platform, "update-ref", "-d", ref)
  assert pu.platform_status(platform)["late_changes"] is None


def test_another_late_edit_cannot_replace_an_unresolved_recovery_record(clone_env):
  origin, platform = clone_env
  _park_resolved_line_a_conflict(platform, origin)
  _local_commit(platform, edits={"backend/app/late.py": "FIRST = True\n"})
  assert pu.continue_platform_overlay_update(platform) == "updated_late_changes_pending"
  first = pu.platform_status(platform)["late_changes"]
  served = _served_sha(platform)

  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'NEXT'"),
  })
  parked = pu.reconcile_clone(platform)
  assert parked.status == "conflict"
  worktree = Path(parked.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH AGAIN'"),
  )
  _git(worktree, "add", "backend/app/main.py")
  (platform / "backend/app/foo.py").write_text("VALUE = 'SECOND'\n")

  with pytest.raises(pu.PlatformUpdateError, match="Saved edits from an earlier"):
    pu.continue_platform_overlay_update(platform)
  assert _served_sha(platform) == served
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'SECOND'\n"
  assert pu.platform_status(platform)["late_changes"]["ref"] == first["ref"]
  assert _git(platform, "show", f"{first['ref']}:backend/app/late.py").stdout == (
    "FIRST = True\n"
  )
  assert _git(
    platform, "for-each-ref", "--format=%(refname)",
    "refs/mobius/platform-late-changes/",
  ).stdout.splitlines() == [first["ref"]]
  assert not pu.LATE_SNAPSHOT_FLAG.exists()


def test_failed_post_activation_cleanup_keeps_the_newer_dirty_recovery_pin(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'"),
  })
  dirty = platform / "backend/app/foo.py"
  dirty.write_text("VALUE = 'BEFORE APPLY'\n")
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
  })
  parked = pu.reconcile_clone(platform)
  assert parked.status == "conflict"
  worktree = Path(parked.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED'"),
  )
  _git(worktree, "add", "backend/app/main.py")
  dirty.write_text("VALUE = 'AFTER APPLY'\n")

  def fail_cleanup(_repo, _worktree):
    raise TimeoutError("candidate cleanup interrupted after activation")

  monkeypatch.setattr(app_git, "remove_overlay_worktree", fail_cleanup)
  assert pu.continue_platform_overlay_update(platform) == (
    "updated_late_changes_pending"
  )

  assert dirty.read_text() == "VALUE = 'BEFORE APPLY'\n"
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["state"] == "needs_merge"
  assert _git(platform, "show", f"{pending['ref']}:backend/app/foo.py").stdout == (
    "VALUE = 'AFTER APPLY'\n"
  )
  assert not pu.LATE_SNAPSHOT_FLAG.exists()


def test_boot_rollback_does_not_claim_committed_late_work_is_missing(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _park_resolved_line_a_conflict(platform, origin)
  late = _local_commit(platform, edits={"backend/app/late.py": "LATE = 1\n"})
  original_probe = pu._import_probe

  def crash_after_activation(_repo):
    raise SystemExit("interrupted after branch activation")

  monkeypatch.setattr(pu, "_import_probe", crash_after_activation)
  with pytest.raises(SystemExit, match="interrupted after branch activation"):
    pu.continue_platform_overlay_update(platform)
  assert pu.platform_status(platform)["late_changes"]["state"] == "needs_merge"

  pu.boot_guard_clean_served_tree(platform)
  assert _served_sha(platform) == late
  assert pu.platform_status(platform)["late_changes"] is None

  monkeypatch.setattr(pu, "_import_probe", original_probe)
  assert pu.continue_platform_overlay_update(platform) == (
    "updated_late_changes_pending"
  )
  assert pu.platform_status(platform)["late_changes"]["late_sha"] == late


def test_unresolved_crash_snapshot_cannot_be_overwritten_by_another_continuation(
  clone_env,
):
  origin, platform = clone_env
  _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/foo.py").write_text("VALUE = 'SAVED'\n")
  carried = pu._snapshot_late_working_edits(platform, "main")
  pu._write_late_snapshot(carried)
  marker = pu.LATE_SNAPSHOT_FLAG.read_text()

  with pytest.raises(pu.PlatformUpdateError, match="earlier interrupted update"):
    pu.continue_platform_overlay_update(platform)
  assert pu.LATE_SNAPSHOT_FLAG.read_text() == marker
  assert pu.platform_status(platform)["late_changes"]["state"] == (
    "restore_pending"
  )
  assert _git(platform, "show", f"{pu._LATE_CHANGES_REF_PREFIX}{carried.pre}:backend/app/foo.py").stdout == (
    "VALUE = 'SAVED'\n"
  )


def test_ordinary_apply_cannot_hide_an_unrecovered_snapshot(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  (platform / "backend/app/foo.py").write_text("VALUE = 'SAVED'\n")
  carried = pu._snapshot_late_working_edits(platform, "main")
  pu._write_late_snapshot(carried)
  marker = pu.LATE_SNAPSHOT_FLAG.read_text()
  _advance_origin(origin, edits={"backend/app/main.py": _MAIN_PY.replace(
    "LINE_C = 3", "LINE_C = 4",
  )})

  result = pu.reconcile_clone(platform)
  assert result.status == "error"
  assert result.error == "saved_work_recovery_pending"
  assert _served_sha(platform) == before
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'SAVED'\n"
  assert pu.LATE_SNAPSHOT_FLAG.read_text() == marker
  assert pu.platform_status(platform)["late_changes"]["state"] == (
    "restore_pending"
  )


def test_saved_late_work_remains_visible_after_another_release(clone_env):
  origin, platform = clone_env
  _served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  late = _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LATE'"),
  })
  assert pu.continue_platform_overlay_update(platform) == (
    "updated_late_changes_pending"
  )
  next_target = _advance_origin(origin, edits={
    "backend/app/new.py": "VALUE = 42\n",
  })
  assert pu.reconcile_clone(platform).status == "updated"
  assert pu.platform_status(platform)["late_changes"]["late_sha"] == late
  assert _git(platform, "merge-base", "--is-ancestor", next_target, "HEAD").returncode == 0


def test_late_conflicting_dirty_edit_is_pinned_and_release_still_activates(
  clone_env,
):
  origin, platform = clone_env
  _served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY LATE'"),
  )
  (platform / "backend/app/notes.txt").write_text("untracked dirty\n")

  assert pu.continue_platform_overlay_update(platform) == (
    "updated_late_changes_pending"
  )

  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["uncommitted"] is True
  # Every dirty byte stays reachable from the recovery ref.
  pinned = pending["ref"]
  assert "LINE_A = 'DIRTY LATE'" in _git(
    platform, "show", f"{pinned}:backend/app/main.py",
  ).stdout
  assert _git(
    platform, "show", f"{pinned}:backend/app/notes.txt",
  ).stdout == "untracked dirty\n"
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_late_clean_dirty_edit_is_pinned_after_the_frozen_release(clone_env):
  origin, platform = clone_env
  _served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")

  assert pu.continue_platform_overlay_update(platform) == "updated_late_changes_pending"

  assert _parents(platform) == [target]
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'foo'\n"
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["uncommitted"] is True
  assert _git(platform, "show", f"{pending['ref']}:backend/app/foo.py").stdout == "VALUE = 'DIRTY'\n"


def test_freezing_dirty_late_edits_does_not_move_live_main_before_activation(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  original = pu._frozen_release_tip

  def inspect_live_source(repo, frozen, carried):
    assert _served_sha(platform) == served
    assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
    assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
      " M backend/app/foo.py",
    ]
    return original(repo, frozen, carried)

  monkeypatch.setattr(pu, "_frozen_release_tip", inspect_live_source)
  assert pu.continue_platform_overlay_update(platform) == "updated_late_changes_pending"


def test_rejected_frozen_release_restores_uncommitted_late_edits(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  monkeypatch.setattr(pu, "_import_probe", lambda _repo: (False, "probe failed"))

  assert pu.continue_platform_overlay_update(platform) == "rolled_back"

  assert _served_sha(platform) == served
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]


def test_rejected_freeze_keeps_existing_dirty_tree_without_warning(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  dirty = platform / "backend/app/foo.py"
  dirty.write_text("VALUE = 'DIRTY'\n")
  monkeypatch.setattr(pu, "_changes_python_dependencies", lambda *_args: True)

  with pytest.raises(pu.PlatformUpdateError, match="image_rebuild_required"):
    pu.continue_platform_overlay_update(platform)

  assert dirty.read_text() == "VALUE = 'DIRTY'\n"
  assert not pu.LATE_SNAPSHOT_FLAG.exists()
  assert pu.platform_status(platform)["late_changes"] is None


def test_integrated_dirty_edit_retires_snapshot_after_update(clone_env):
  origin, platform = clone_env
  _served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED'"),
  )

  assert pu.continue_platform_overlay_update(platform) == "updated"
  assert not pu.LATE_SNAPSHOT_FLAG.exists()
  assert pu.platform_status(platform)["late_changes"] is None


def test_boot_restores_dirty_late_snapshot_after_interrupted_settlement(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  monkeypatch.setattr(pu, "_import_probe", lambda _repo: (False, "probe failed"))
  monkeypatch.setattr(pu, "_settle_late_snapshot", lambda *_args, **_kwargs: False)

  assert pu.continue_platform_overlay_update(platform) == "rolled_back"
  assert _served_sha(platform) == served
  assert pu.LATE_SNAPSHOT_FLAG.is_file()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'foo'\n"
  assert pu.platform_status(platform)["late_changes"]["state"] == "restore_pending"

  assert "late=restored" in pu.boot_guard_clean_served_tree(platform)
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]
  assert not pu.LATE_SNAPSHOT_FLAG.exists()


def test_boot_keeps_pending_dirty_snapshot_after_successful_activation(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  monkeypatch.setattr(pu, "_settle_late_snapshot", lambda *_args, **_kwargs: False)

  assert pu.continue_platform_overlay_update(platform) == "updated_late_changes_pending"
  assert pu.LATE_SNAPSHOT_FLAG.is_file()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'foo'\n"

  assert "late=pending" in pu.boot_guard_clean_served_tree(platform)
  assert _served_sha(platform) != target
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'foo'\n"
  pending = pu.platform_status(platform)["late_changes"]
  assert _git(platform, "show", f"{pending['ref']}:backend/app/foo.py").stdout == "VALUE = 'DIRTY'\n"
  assert not pu.LATE_SNAPSHOT_FLAG.exists()


def test_edit_after_frozen_snapshot_cannot_be_overwritten_by_activation(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served, _target, worktree = _park_resolved_line_a_conflict(platform, origin)
  original = pu._activate_candidate

  def edit_at_activation(repo, local, pre_sha, tip, **kwargs):
    (platform / "backend/app/foo.py").write_text("VALUE = 'VERY LATE'\n")
    return original(repo, local, pre_sha, tip, **kwargs)

  monkeypatch.setattr(pu, "_activate_candidate", edit_at_activation)
  with pytest.raises(pu.PlatformUpdateError, match="activation_tree_changed"):
    pu.continue_platform_overlay_update(platform)

  assert _served_sha(platform) == served
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'VERY LATE'\n"
  assert worktree.exists()
  assert pu._read_conflict_flag()["overlay"]["ready"] is True
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_concurrent_branch_move_keeps_frozen_release_for_one_late_commit(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _served, target, worktree = _park_resolved_line_a_conflict(platform, origin)
  original = pu._activate_candidate
  raced: dict[str, str] = {}

  def concurrent_then_activate(repo, local, pre_sha, tip, **kwargs):
    if not raced:
      raced["sha"] = _local_commit(
        platform, edits={"backend/app/raced.py": "RACED = True\n"},
        msg="concurrent writer",
      )
    original(repo, local, pre_sha, tip, **kwargs)

  monkeypatch.setattr(pu, "_activate_candidate", concurrent_then_activate)
  with pytest.raises(pu.PlatformUpdateError, match="activation_ref_changed"):
    pu.continue_platform_overlay_update(platform)

  # The newer writer keeps the branch; the resolution stays frozen and ready.
  assert _served_sha(platform) == raced["sha"]
  assert not pu.RECONCILE_PRE_FLAG.exists()
  frozen = pu._read_conflict_flag()["overlay"]
  assert frozen["ready"] is True and worktree.exists()
  release = frozen["release"]

  # A later Apply reuses the frozen release and pins the raced commit.
  result = pu.reconcile_clone(platform, target_ref=target, fetch_remote=False)

  assert result.status == "updated"
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  assert _served_sha(platform) == release
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["late_sha"] == raced["sha"]
  assert _git(platform, "show", f"{pending['ref']}:backend/app/raced.py").stdout == "RACED = True\n"
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert not pu.CONFLICT_FLAG.exists()


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

  with pytest.raises(pu.PlatformUpdateError, match="Unresolved files"):
    pu.continue_platform_overlay_update(platform)
  flag = pu._read_conflict_flag()
  assert set(flag["overlay"]["paths"]) == {
    "backend/app/main.py", "backend/app/foo.py",
  }
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


def test_net_merge_failure_serves_old_without_a_resolver_flag(
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
    raise RuntimeError("net merge wedged")

  monkeypatch.setattr(app_git, "merge_refs", explode)

  res = pu.reconcile_clone(platform)

  assert res.status == "error"
  assert "net merge wedged" in res.error
  assert res.target_sha == target
  assert _served_sha(platform) == pre
  assert not pu._reconcile_in_progress(platform)
  assert not pu.CONFLICT_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists()
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_merge_shaped_history_keeps_its_final_tree_without_replay(clone_env):
  """Old merge commits are archived; the new local delta is one linear commit."""
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'LOCAL'\n"},
                msg="old local")
  first = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 30")})
  _git(platform, "fetch", "-q", "origin")
  _git(platform, "merge", "--no-ff", "-m", "platform: merge upstream", first)
  before = _served_sha(platform)
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 300")})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  assert _parents(platform) == [res.target_sha]
  assert _git(platform, "rev-parse", f"refs/mobius/platform-pre-update/{before}").stdout.strip() == before
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'LOCAL'\n"


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


def test_activation_compare_and_swap_never_rewinds_a_concurrent_writer(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'update'\n"})
  original = pu._activate_candidate
  raced: dict[str, str] = {}

  def concurrent_then_activate(repo, local, pre_sha, tip, **kwargs):
    raced["sha"] = _local_commit(
      platform, edits={"concurrent.txt": "owned elsewhere\n"},
      msg="concurrent writer",
    )
    original(repo, local, pre_sha, tip, **kwargs)

  monkeypatch.setattr(pu, "_activate_candidate", concurrent_then_activate)
  result = pu.reconcile_clone(platform)

  assert result.status == "error"
  assert _served_sha(platform) == raced["sha"]
  assert (platform / "concurrent.txt").read_text() == "owned elsewhere\n"


def test_failed_candidate_never_rolls_back_a_newer_concurrent_writer(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'update'\n"})
  raced: dict[str, str] = {}

  def fail_after_concurrent_commit(repo=platform, timeout=pu._PROBE_TIMEOUT):
    raced["sha"] = _local_commit(
      platform, edits={"concurrent.txt": "newer owner\n"},
      msg="concurrent writer after activation",
    )
    return False, "candidate rejected"

  monkeypatch.setattr(pu, "_import_probe", fail_after_concurrent_commit)
  result = pu.reconcile_clone(platform)

  assert result.status == "error"
  assert "rollback_ref_changed" in result.error
  assert _served_sha(platform) == raced["sha"]
  assert (platform / "concurrent.txt").read_text() == "newer owner\n"


def test_stale_rollback_flag_is_ignored_once_its_target_landed(clone_env):
  origin, platform = clone_env
  # A rollback flag left from a prior failed attempt whose target is now already
  # contained in local main (it landed or was superseded) is a stale ghost: it
  # must not keep projecting "needs repair"/available, and an explicit check
  # clears it for good. Regression: previously the flag's mere existence forced
  # state=rolled_back + available=True forever, with no read/check path to undo.
  landed = _served_sha(platform)
  pu._write_rolled_back_flag(landed, "old frontend build failure")

  status = pu.platform_status(platform)
  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
  assert status["available"] is False
  assert status["rollback_error"] is None
  # Status is read-only, so the file lingers until an explicit check/reconcile.
  assert pu.ROLLED_BACK_FLAG.exists()

  # The owner's explicit "Check for updates" removes the ghost under the lock.
  pu.check_for_updates(platform)
  assert not pu.ROLLED_BACK_FLAG.exists()


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

def test_original_dirty_edit_survives_a_committed_conflict_resolution(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'"),
  })
  (platform / "backend/app/foo.py").write_text("VALUE = 'BEFORE APPLY'\n")
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
  })
  parked = pu.reconcile_clone(platform)
  assert parked.status == "conflict" and parked.overlay["stage"] == "committed"
  worktree = Path(parked.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "updated"
  assert "LINE_A = 'BOTH'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'BEFORE APPLY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]
  assert pu.platform_status(platform)["late_changes"] is None


def test_original_dirty_edit_conflicting_with_resolution_stops_before_activation(
  clone_env,
):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'"),
  })
  (platform / "backend/app/foo.py").write_text("VALUE = 'BEFORE APPLY'\n")
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
    "backend/app/foo.py": "VALUE = 'UPSTREAM'\n",
  })
  parked = pu.reconcile_clone(platform)
  assert parked.status == "conflict" and parked.overlay["stage"] == "committed"
  worktree = Path(parked.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  with pytest.raises(pu.PlatformUpdateError, match="Abandon this parked update"):
    pu.continue_platform_overlay_update(platform)
  assert _served_sha(platform) == pre
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'BEFORE APPLY'\n"
  assert pu.platform_status(platform)["late_changes"] is None
  assert worktree.exists() and pu.CONFLICT_FLAG.exists()
  assert pu.abandon_platform_overlay_update(platform) == "abandoned"
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'BEFORE APPLY'\n"
  assert not pu.CONFLICT_FLAG.exists()


def test_interrupted_freeze_cannot_reclassify_original_dirty_as_committed(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'"),
  })
  dirty = platform / "backend/app/foo.py"
  dirty.write_text("VALUE = 'BEFORE APPLY'\n")
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
  })
  parked = pu.reconcile_clone(platform)
  worktree = Path(parked.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED'"),
  )
  _git(worktree, "add", "backend/app/main.py")
  served = _served_sha(platform)
  original_git = pu._git

  def fail_candidate_reset(*args, **kwargs):
    if args[:3] == ("reset", "-q", "--hard") and kwargs.get("repo") == worktree:
      raise TimeoutError("freeze interrupted before candidate reset")
    return original_git(*args, **kwargs)

  monkeypatch.setattr(pu, "_git", fail_candidate_reset)
  with pytest.raises(TimeoutError, match="freeze interrupted"):
    pu.continue_platform_overlay_update(platform)
  monkeypatch.setattr(pu, "_git", original_git)
  with pytest.raises(pu.PlatformUpdateError, match="prepared release is incomplete"):
    pu.continue_platform_overlay_update(platform)
  assert _served_sha(platform) == served
  assert dirty.read_text() == "VALUE = 'BEFORE APPLY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]

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


def test_new_uncommitted_edits_are_pinned_across_a_parked_conflict(clone_env):
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
  # The committed merge is parked as one unit; the dirty edit is carried
  # separately and may change before the resolver finishes.
  assert res.overlay["stage"] == "committed"
  assert res.overlay["served"] == pre
  assert res.pre_sha != pre

  # A restart (boot reconcile) must not restart the replay under the resolver.
  again = pu.reconcile_clone(platform)
  assert again.status == "conflict"
  assert again.overlay == res.overlay
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]

  # The owner keeps editing while the conflict is parked. The original dirty
  # edit remains in the frozen answer; only the newer version is saved for a
  # later merge.
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY AGAIN'\n")
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "updated_late_changes_pending"

  target = _git(platform, "rev-parse", "origin/main").stdout.strip()
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  assert "LINE_A = 'BOTH'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["uncommitted"] is True
  assert _git(platform, "show", f"{pending['ref']}:backend/app/foo.py").stdout == "VALUE = 'DIRTY AGAIN'\n"
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
  assert res.overlay["stage"] == "working"
  right_ref = pu._PARKED_RIGHT_REF_PREFIX + res.overlay["right"]
  assert _git(platform, "rev-parse", right_ref).stdout.strip() == res.overlay["right"]
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


def test_dirty_resolution_pins_late_committed_and_uncommitted_work(clone_env):
  origin, platform = clone_env
  served = _served_sha(platform)
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY'"),
  )
  target = _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace(
      "LINE_A = 1", "LINE_A = 'UPSTREAM'",
    ),
  })
  parked = pu.reconcile_clone(platform)
  assert parked.status == "conflict"
  assert parked.overlay["stage"] == "working"
  worktree = Path(parked.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  # A later commit is independent of the still-uncommitted dirty resolution.
  (platform / "backend/app/late.py").write_text("LATE = True\n")
  _git(platform, "add", "backend/app/late.py")
  _git(platform, "commit", "-q", "-m", "late commit")
  assert _served_sha(platform) != served
  assert "LINE_A = 'DIRTY'" in (platform / "backend/app/main.py").read_text()

  assert pu.continue_platform_overlay_update(platform) == "updated_late_changes_pending"
  assert _overlay_subjects(platform, target) == []
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/main.py",
  ]
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["uncommitted"] is True
  assert _git(platform, "show", f"{pending['ref']}:backend/app/late.py").stdout == "LATE = True\n"
  assert "LINE_A = 'DIRTY'" in _git(
    platform, "show", f"{pending['ref']}:backend/app/main.py",
  ).stdout


def test_committing_original_dirty_edit_does_not_lose_frozen_resolution(clone_env):
  origin, platform = clone_env
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY'"),
  )
  target = _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
  })
  parked = pu.reconcile_clone(platform)
  assert parked.status == "conflict" and parked.overlay["stage"] == "working"
  worktree = Path(parked.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED'"),
  )
  _git(worktree, "add", "backend/app/main.py")
  committed = _local_commit(platform, edits={}, msg="commit original dirty edit")

  assert pu.continue_platform_overlay_update(platform) == "updated_late_changes_pending"
  # The resolver's answer remains visible as a dirty edit; the later commit
  # is independently reachable for the post-boot merge.
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/main.py",
  ]
  pending = pu.platform_status(platform)["late_changes"]
  assert pending["late_sha"] == committed
  assert _git(platform, "show", f"{pending['ref']}:backend/app/main.py").stdout == (
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY'")
  )
  assert pu.recorded_upstream_sha(platform) == target


def test_continue_runs_the_same_post_replay_gates_as_apply(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
    "frontend/package-lock.json": "new-locked-frontend-deps\n",
    "frontend/src/App.jsx": "export default 'upstream'\n",
  })
  gates, rebuilt = [], []
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
  assert gates == ["frontend", "build"]
  assert rebuilt == [_served_sha(platform)]
  assert not (platform / "frontend" / ".source-build-signature").exists()


def test_continue_refuses_new_python_dependencies_before_source_moves(
  clone_env,
):
  origin, platform = clone_env
  served = _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace(
      "LINE_A = 1", "LINE_A = 'LOCAL'",
    ),
  })
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace(
      "LINE_A = 1", "LINE_A = 'UPSTREAM'",
    ),
    "backend/requirements.lock": "new-package==1\n",
  })
  res = pu.reconcile_clone(platform)
  assert res.status == "conflict"
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  with pytest.raises(pu.PlatformUpdateError, match="image_rebuild_required"):
    pu.continue_platform_overlay_update(platform)

  assert _served_sha(platform) == served
  assert pu.CONFLICT_FLAG.exists()
  assert worktree.exists()


def test_continue_rolls_back_a_candidate_that_fails_a_gate(clone_env, monkeypatch):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'")})
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'UPSTREAM'"),
    "frontend/package-lock.json": "new-locked-frontend-deps\n",
    "frontend/src/App.jsx": "export default 'upstream'\n",
  })
  monkeypatch.setattr(
    pu, "_sync_frontend_dependencies", lambda repo: (False, "npm exploded"),
  )
  res = pu.reconcile_clone(platform)
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "rolled_back"

  assert _served_sha(platform) == pre
  assert "npm exploded" in pu._read_rolled_back_flag()["error"]
  assert not pu.CONFLICT_FLAG.exists()


def test_merge_shaped_history_update_restores_uncommitted_edits(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'LOCAL'\n"},
                msg="old local")
  first = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 30")})
  _git(platform, "fetch", "-q", "origin")
  _git(platform, "merge", "--no-ff", "-m", "platform: merge upstream", first)
  before = _served_sha(platform)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 300")})

  res = pu.reconcile_clone(platform)

  assert res.status == "updated"
  assert _parents(platform) == [res.target_sha]
  assert _git(platform, "rev-parse", f"refs/mobius/platform-pre-update/{res.pre_sha}").stdout.strip() == res.pre_sha
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]


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
  assert res.overlay["mode"] == "net"
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
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

  # The reviewed bytes are in upstream; the distinct local whitespace survives
  # as the one net local delta.
  assert res.status == "updated", res
  assert res.overlay["mode"] == "net"
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
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


def test_boot_guard_preserves_unknown_tip_from_legacy_marker(clone_env):
  _origin, platform = clone_env
  pre = _served_sha(platform)
  advanced = _local_commit(
    platform, edits={"concurrent.txt": "owned elsewhere\n"},
    msg="unknown writer after legacy update",
  )
  pu.RECONCILE_PRE_FLAG.write_text(pre + "\n", encoding="utf-8")

  summary = pu.boot_guard_clean_served_tree(platform)

  assert summary.startswith("boot_guard[preserved]")
  assert _served_sha(platform) == advanced
  assert (platform / "concurrent.txt").read_text() == "owned elsewhere\n"
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
  installed = pu.recorded_upstream_sha(platform)
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
  assert status["checked_target_sha"] == _git(
    origin.parent / "origin-work", "rev-parse", "main",
  ).stdout.strip()
  assert status["installed_release_sha"] == installed
  assert status["recorded_upstream_sha"] == installed
  # This field is deliberately about releases stacked behind a conflict, not
  # ordinary update availability. ``available`` is the ordinary signal.
  assert status["newer_updates_available"] is False
  # A check only advances remote-tracking refs — the served tree is NOT mutated.
  assert _served_sha(platform) == before


def test_check_route_distinguishes_fetched_target_from_installed_release(
  clone_env, client, auth, monkeypatch,
):
  origin, platform = clone_env
  installed = pu.recorded_upstream_sha(platform)
  target = _advance_origin(origin, edits={"backend/app/main.py":
    _MAIN_PY.replace("LINE_C = 3", "LINE_C = 78")})
  original_check = pu.check_for_updates
  monkeypatch.setattr(
    "app.routes.platform.platform_activation.deployment_kind",
    lambda: "self_hosted",
  )
  monkeypatch.setattr(
    "app.routes.platform.platform_update.check_for_updates",
    lambda: original_check(platform),
  )

  response = client.post("/api/platform/check", headers=auth)

  assert response.status_code == 200
  status = response.json()
  assert status["available"] is True
  assert status["checked_target_sha"] == target
  assert status["installed_release_sha"] == installed
  assert status["recorded_upstream_sha"] == installed
  assert status["newer_updates_available"] is False
  assert _served_sha(platform) == installed


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


def test_status_reports_contained_origin_when_updater_marker_is_stale(clone_env, monkeypatch):
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
  # The running image's build commit is surfaced with the same %cI shape so
  # Settings can render "Current system" in the same zone as "Installed update".
  # This clone's BUILD_SHA is a fake ("test-sha"), so it resolves to None; the
  # key is always present.
  assert status["current_build_committed_at"] is None

  # Positive path: when the build sha resolves to a real commit, its %cI surfaces
  # so Settings can render "Current system" from a real instant.
  monkeypatch.setattr(pu, "current_build_sha", lambda: new)
  status_built = pu.platform_status(platform)
  assert status_built["current_build_committed_at"] == _git(
    platform, "show", "-s", "--format=%cI", new,
  ).stdout.strip()


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
  (scripts / "frontend-deps.sh").write_text("#!/bin/sh\n# committed helper\n")
  (scripts / "check-frontend-deps.mjs").write_text(
    "#!/usr/bin/env node\n// committed helper\n"
  )
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
  assert (hooks / "frontend-deps.sh").read_text() == (
    "#!/bin/sh\n# committed helper\n"
  )
  assert (hooks / "check-frontend-deps.mjs").read_text() == (
    "#!/usr/bin/env node\n// committed helper\n"
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


@pytest.mark.asyncio
async def test_saved_late_edits_open_owner_clicked_review_chat(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  _served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY LATE'"),
  )
  assert pu.continue_platform_overlay_update(platform) == (
    "updated_late_changes_pending"
  )
  assert not pu.CONFLICT_FLAG.exists()
  pending = pu.platform_status(platform)["late_changes"]
  calls = []

  async def fake_spawn(db, paths, target_sha, merge_base, overlay=None,
                       *, late_changes=None):
    calls.append((paths, target_sha, late_changes["ref"]))
    return {"chat_id": "late-review-chat", "created": True, "started": True}

  monkeypatch.setattr(pu, "spawn_platform_conflict_chat", fake_spawn)
  result = await pu.create_platform_conflict_resolver_chat(
    SimpleNamespace(), platform,
  )
  assert result["chat_id"] == "late-review-chat"
  assert calls == [([], pending["target_sha"], pending["ref"])]
  assert pu.platform_status(platform)["late_changes"]["chat_id"] == (
    "late-review-chat"
  )


@pytest.mark.asyncio
async def test_interrupted_dirty_snapshot_can_open_review_chat(
  monkeypatch, clone_env,
):
  _origin, platform = clone_env
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY'"),
  )
  carried = pu._snapshot_late_working_edits(platform, "main")
  pu._write_late_snapshot(carried)
  pu._write_conflict_flag(
    _served_sha(platform), [], None, overlay={"ready": True},
  )
  assert pu.platform_status(platform)["late_changes"]["state"] == (
    "restore_pending"
  )

  async def fake_spawn(db, paths, target_sha, merge_base, overlay=None,
                       *, late_changes=None):
    assert late_changes["state"] == "restore_pending"
    return {"chat_id": "restore-review-chat", "created": True, "started": True}

  monkeypatch.setattr(pu, "spawn_platform_conflict_chat", fake_spawn)
  result = await pu.create_platform_conflict_resolver_chat(
    SimpleNamespace(), platform,
  )
  assert result["chat_id"] == "restore-review-chat"
  assert pu.platform_status(platform)["late_changes"]["chat_id"] == (
    "restore-review-chat"
  )


@pytest.mark.asyncio
async def test_platform_conflict_resolver_preserves_background_choice_effort(
  monkeypatch, db, owner_token,
):
  async def fake_start(**kwargs):
    return True

  monkeypatch.setattr(
    "app.background_agents.resolve_background_chat_choice",
    lambda data_dir, session: {
      "provider": "codex",
      "agent_settings": {"model": "gpt-5.5", "effort": "xhigh"},
    },
  )
  monkeypatch.setattr(
    "app.chat_start.start_programmatic_chat_turn", fake_start,
  )
  monkeypatch.setattr("app.push.notify_owner", lambda *args, **kwargs: None)

  result = await pu.spawn_platform_conflict_chat(
    db, ["backend/app/main.py"], "a" * 40,
  )

  from app import models
  chat = db.get(models.Chat, result["chat_id"])
  assert chat.provider == "codex"
  assert chat.agent_settings_json["model"] == "gpt-5.5"
  assert chat.agent_settings_json["effort"] == "xhigh"


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
    "mode": "net",
    "worktree": "/data/platform/.git/mobius-overlay-candidate",
    "served": "c" * 40, "stage": "committed",
    "paths": ["backend/app/main.py"],
  }

  content = pu._platform_conflict_resolver_message(
    target, ["backend/app/main.py", "backend/app/foo.py"], None, parked,
  )

  assert parked["worktree"] in content
  assert "continue_platform_overlay_update" in content
  assert "final local source" in content
  assert "all marked files together" in content
  assert "not replayed" in content
  assert "merge --no-ff" not in content
  assert "materialize_platform_conflict" not in content
  assert "running platform is untouched" in content
  assert "separate image/restart actions" in content


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

@pytest.mark.parametrize("deployment", ["self_hosted", "railway"])
def test_platform_update_uses_explicit_activation_levels(monkeypatch, deployment):
  monkeypatch.setattr(platform_activation, "deployment_kind", lambda: deployment)
  classify = platform_activation.classify_activation
  assert classify(["backend/app/main.py"])["level"] == \
    "server_restart"
  assert classify(["backend/config_helper.py"])["level"] == \
    "server_restart"
  assert classify(["skill/core.md"])["level"] == \
    "server_restart"
  assert classify(["backend/requirements.txt"])["level"] == \
    "image_rebuild"
  assert classify(["backend/scripts/entrypoint.sh"])["level"] == \
    "image_rebuild"
  assert classify(["Caddyfile"])["level"] == (
    "proxy_reload" if deployment == "self_hosted" else "live"
  )
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


def test_served_runtime_module_is_not_an_image_replacement_blocker(
  tmp_path, monkeypatch,
):
  """The frozen launcher starts the broker from the served checkout, so a local
  edit there is activated by a restart rather than by a new image."""
  marker = tmp_path / "activation.json"
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  pu._write_activation_marker(
    "a" * 40,
    ["Dockerfile", "backend/runtime/identity_broker.py"],
    upstream_sha="b" * 40,
    image_paths=[],
  )

  assert pu.container_replacement_blockers() == ["Dockerfile"]


def test_image_owned_runtime_is_an_image_replacement_blocker(
  tmp_path, monkeypatch,
):
  """Every other module under backend/runtime still comes verbatim from the
  image, including anything newly added there."""
  marker = tmp_path / "activation.json"
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  pu._write_activation_marker(
    "a" * 40,
    ["Dockerfile", "backend/runtime/restart_ledger.py"],
    upstream_sha="b" * 40,
    image_paths=[],
  )

  assert pu.container_replacement_blockers() == [
    "Dockerfile", "backend/runtime/restart_ledger.py",
  ]


def test_stale_image_owned_runtime_restores_image_activation_without_marker(
  clone_env, monkeypatch, tmp_path,
):
  _, platform = clone_env
  head = _local_commit(
    platform,
    edits={"backend/runtime/restart_ledger.py": "wanted\n"},
  )
  deployed = tmp_path / "deployed-runtime"
  deployed.mkdir()
  (deployed / "restart_ledger.py").write_text("old\n", encoding="utf-8")
  monkeypatch.setenv("MOBIUS_PROTECTED_RUNTIME_DIR", str(deployed))
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(head + "\n")

  status = pu.platform_status(platform)

  assert status["state"] == pu.PlatformUpdateState.ACTIVATION_NEEDED.value
  assert status["activation"]["level"] == "image_rebuild"
  assert status["activation"]["reasons"] == [{
    "code": "baked_runtime",
    "summary": "Baked scripts, supervisors, or protected-file rules changed.",
    "paths": ["backend/runtime/restart_ledger.py"],
  }]


def test_served_runtime_edit_asks_for_a_restart_not_a_new_image(
  clone_env, monkeypatch, tmp_path,
):
  """The broker is served source: editing it after boot owes a restart that
  reloads it, and never blocks the container replacement."""
  _, platform = clone_env
  before = _served_sha(platform)
  _local_commit(
    platform, edits={"backend/runtime/identity_broker.py": "wanted\n"},
  )
  deployed = tmp_path / "deployed-runtime"
  deployed.mkdir()
  (deployed / "identity_broker.py").write_text("old\n", encoding="utf-8")
  monkeypatch.setenv("MOBIUS_PROTECTED_RUNTIME_DIR", str(deployed))
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(before + "\n")

  status = pu.platform_status(platform)

  # The served/image difference is excluded from parity, so the restart comes
  # from the served revision advancing after boot rather than from an image
  # remainder.
  assert status["activation"]["required_actions"] == ["server_restart"]
  assert pu.container_replacement_blockers(_served_sha(platform), platform) == []


def test_replacement_blocks_local_image_owned_runtime_drift(
  clone_env,
):
  _, platform = clone_env
  official = _git(platform, "rev-parse", "HEAD").stdout.strip()
  _local_commit(
    platform,
    edits={"backend/runtime/restart_ledger.py": "local-only\n"},
  )

  assert pu.container_replacement_blockers(official, platform) == [
    "backend/runtime/restart_ledger.py",
  ]


def test_replacement_carries_a_local_served_runtime_edit(
  clone_env,
):
  """A local broker edit is served source the replacement does not own, so it
  neither blocks the rebuild nor gets reverted by it."""
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
  assert status["checked_target_sha"] == missing_release
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


def test_legacy_activation_marker_is_normalized_before_current_runtime_reads(
  tmp_path, monkeypatch,
):
  marker = tmp_path / "activation.json"
  receipt = tmp_path / "activation-v2"
  marker.write_text(
    '{"target_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"paths":["Dockerfile"]}',
    encoding="utf-8",
  )
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  monkeypatch.setattr(pu, "ACTIVATION_V2_CUTOVER_RECEIPT", receipt)

  assert pu._read_activation_marker() is None
  pu._normalize_activation_marker()
  assert pu._read_activation_marker() == {
    "version": 2,
    "target_sha": "a" * 40,
    "upstream_sha": None,
    "paths": ["Dockerfile"],
    "image_paths": [],
  }
  assert pu.container_replacement_blockers() == ["Dockerfile"]
  assert receipt.read_text() == "v2"


def test_bare_sha_activation_marker_normalizes_to_backend_restart(
  tmp_path, monkeypatch,
):
  marker = tmp_path / "activation.json"
  receipt = tmp_path / "activation-v2"
  marker.write_text("b" * 40, encoding="utf-8")
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  monkeypatch.setattr(pu, "ACTIVATION_V2_CUTOVER_RECEIPT", receipt)

  pu._normalize_activation_marker()

  assert pu._read_activation_marker() == {
    "version": 2,
    "target_sha": "b" * 40,
    "upstream_sha": None,
    "paths": ["backend/app"],
    "image_paths": [],
  }


def test_stale_activation_receipt_cannot_hide_restored_legacy_marker(
  tmp_path, monkeypatch,
):
  marker = tmp_path / "activation.json"
  receipt = tmp_path / "activation-v2"
  marker.write_text("d" * 40, encoding="utf-8")
  receipt.write_text("v2", encoding="utf-8")
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  monkeypatch.setattr(pu, "ACTIVATION_V2_CUTOVER_RECEIPT", receipt)

  pu._normalize_activation_marker()

  assert pu._read_activation_marker() == {
    "version": 2,
    "target_sha": "d" * 40,
    "upstream_sha": None,
    "paths": ["backend/app"],
    "image_paths": [],
  }
  assert receipt.read_text(encoding="utf-8") == "v2"


def test_invalid_v2_marker_revokes_stale_activation_proof(
  tmp_path, monkeypatch,
):
  marker = tmp_path / "activation.json"
  receipt = tmp_path / "activation-v2"
  raw = '{"version":2,"target_sha":"' + "e" * 40 + '","paths":["backend/app"]}'
  marker.write_text(raw, encoding="utf-8")
  receipt.write_text("v2", encoding="utf-8")
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  monkeypatch.setattr(pu, "ACTIVATION_V2_CUTOVER_RECEIPT", receipt)

  pu._normalize_activation_marker()

  assert pu._read_activation_marker() is None
  assert marker.read_text(encoding="utf-8") == raw
  assert receipt.exists() is False


@pytest.mark.parametrize("payload", [
  {"version": 2, "target_sha": "short", "paths": ["backend/app"], "image_paths": []},
  {"version": 2, "target_sha": "e" * 40, "paths": [], "image_paths": []},
  {"version": 2, "target_sha": "e" * 40, "paths": ["backend/app"], "image_paths": ["Dockerfile"]},
  {"version": 2, "target_sha": "e" * 40, "upstream_sha": "bad", "paths": ["backend/app"], "image_paths": []},
])
def test_current_activation_reader_rejects_incomplete_or_inconsistent_shapes(
  tmp_path, monkeypatch, payload,
):
  marker = tmp_path / "activation.json"
  marker.write_text(json.dumps(payload), encoding="utf-8")
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)

  assert pu._read_activation_marker() is None


@pytest.mark.parametrize("raw", [
  "not-a-sha",
  '{"version":3,"target_sha":"' + "c" * 40 + '","paths":["Dockerfile"]}',
])
def test_activation_cutover_does_not_invent_meaning_for_unknown_markers(
  tmp_path, monkeypatch, raw,
):
  marker = tmp_path / "activation.json"
  receipt = tmp_path / "activation-v2"
  marker.write_text(raw, encoding="utf-8")
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  monkeypatch.setattr(pu, "ACTIVATION_V2_CUTOVER_RECEIPT", receipt)

  pu._normalize_activation_marker()

  assert pu._read_activation_marker() is None
  assert marker.read_text(encoding="utf-8") == raw
  assert receipt.exists() is False


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


def test_status_ignores_seed_repair_already_matching_running_image(
  clone_env, monkeypatch,
):
  """A served-checkout SHA cannot make an already-baked seed stale.

  Seed templates run from the image, unlike the Python checkout. Restoring one
  to the image's exact bytes must clear a false image-rebuild prompt without
  modifying the separately owner-curated shared skill.
  """
  _, platform = clone_env
  path = "backend/scripts/seed-skills/goal-planning.md"
  seed = platform / path
  seed.parent.mkdir(parents=True)
  seed.write_text("old seed\n")
  _git(platform, "add", path)
  _git(platform, "commit", "-q", "-m", "old seed")
  served = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")

  baked = "running image seed\n"
  _local_commit(platform, edits={path: baked})
  monkeypatch.setattr(pu, "_build_info", lambda: {
    "image_inputs": {path: hashlib.sha256(baked.encode()).hexdigest()},
  })

  status = pu.platform_status(platform)

  assert status["needs_restart"] is False
  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
  assert status["activation"]["level"] == "live"


def test_status_requires_an_image_for_python_dependency_changes(
  clone_env,
):
  _, platform = clone_env
  served = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(served + "\n")
  _local_commit(platform, edits={"backend/requirements.txt": "new-package==1\n"})

  status = pu.platform_status(platform)

  assert status["state"] == pu.PlatformUpdateState.ACTIVATION_NEEDED.value
  assert status["needs_restart"] is False
  assert status["activation"]["level"] == "image_rebuild"


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


@pytest.mark.parametrize(
  "deployment,required_marker",
  [
    ("self_hosted", "deployment/self-hosted-helper.required"),
    ("railway", "deployment/railway-topology.required"),
  ],
)
def test_boot_retires_compatible_host_paths_but_preserves_required_migrations(
  clone_env, monkeypatch, deployment, required_marker,
):
  monkeypatch.setattr(platform_activation, "deployment_kind", lambda: deployment)
  _, platform = clone_env
  target = _served_sha(platform)
  for remainder, expected in (
    ([], None),
    (["scripts/mobius-rebuild-host.py"], None),
    ([required_marker], [required_marker]),
  ):
    pu._write_activation_marker(
      target, ["scripts/deploy-prod.sh", "backend/app/main.py", *remainder],
    )

    pu._complete_boot_activation(platform)

    marker = pu._read_activation_marker()
    if expected:
      assert marker["paths"] == expected
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


@pytest.mark.parametrize(
  "deployment,topology_marker",
  [
    ("self_hosted", "deployment/self-hosted-topology.required"),
    ("railway", "deployment/railway-topology.required"),
  ],
)
def test_image_receipt_does_not_claim_required_topology_migration_was_applied(
  clone_env, monkeypatch, deployment, topology_marker,
):
  monkeypatch.setattr(platform_activation, "deployment_kind", lambda: deployment)
  _, platform = clone_env
  upstream = _served_sha(platform)
  pu._write_activation_marker(
    upstream,
    [topology_marker],
    upstream_sha=upstream,
    image_paths=[topology_marker],
  )
  monkeypatch.setattr(pu, "current_build_sha", lambda: upstream)

  pu._complete_boot_activation(platform)

  marker = pu._read_activation_marker()
  assert marker is not None
  assert marker["paths"] == [topology_marker]


@pytest.mark.parametrize(
  "deployment,topology_marker",
  [
    ("self_hosted", "deployment/self-hosted-topology.required"),
    ("railway", "deployment/railway-topology.required"),
  ],
)
def test_skipped_release_still_carries_required_topology_migration(
  clone_env, monkeypatch, deployment, topology_marker,
):
  monkeypatch.setattr(platform_activation, "deployment_kind", lambda: deployment)
  origin, platform = clone_env
  migration = _advance_origin(
    origin,
    edits={topology_marker: "1\n"},
    msg="require topology migration",
  )
  target = _advance_origin(
    origin,
    edits={"Dockerfile": "FROM python:3.12\n"},
    msg="later image release",
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert migration != target
  assert set(preview["activation"]["required_actions"]) == {
    "container_recreate", "image_rebuild",
  }
  assert preview["activation"]["level"] == "image_rebuild"


def test_boot_preserves_python_dependency_image_work(clone_env, monkeypatch):
  _, platform = clone_env
  target = _served_sha(platform)
  pu.mark_activation_needed(
    target,
    ["backend/app/main.py", "backend/requirements.lock"],
  )

  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  assert "startup[installed]" in pu.reconcile_clone_sync()
  assert pu._read_activation_marker()["paths"] == ["backend/requirements.lock"]


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


def test_frontend_dependency_install_detaches_known_baked_symlink_for_rollback(
  tmp_path, monkeypatch,
):
  platform = tmp_path / "platform"
  frontend = platform / "frontend"
  baked = tmp_path / "baked-node-modules"
  frontend.mkdir(parents=True)
  baked.mkdir()
  (baked / "baked-marker").write_text("immutable")
  (frontend / "package-lock.json").write_text("{}")
  (frontend / "node_modules").symlink_to(baked, target_is_directory=True)
  monkeypatch.setattr(pu, "BAKED_FRONTEND_NODE_MODULES", baked)
  monkeypatch.setattr(pu, "_record_dependency_inputs", lambda *args, **kwargs: None)

  installs = []

  def npm_ci(*args, **kwargs):
    node_modules = frontend / "node_modules"
    installs.append((node_modules.is_dir(), node_modules.is_symlink()))
    if len(installs) == 1:
      return subprocess.CompletedProcess(args[0], 1, "", "candidate failed")
    return subprocess.CompletedProcess(args[0], 0, "", "")

  monkeypatch.setattr(pu.subprocess, "run", npm_ci)

  assert pu._sync_frontend_dependencies(platform) == (False, "candidate failed")
  assert pu._sync_frontend_dependencies(platform) == (True, "")
  assert installs == [(True, False), (True, False)]
  assert (baked / "baked-marker").read_text() == "immutable"


def test_frontend_dependency_install_refuses_an_unexpected_symlink(
  tmp_path, monkeypatch,
):
  platform = tmp_path / "platform"
  frontend = platform / "frontend"
  expected_baked = tmp_path / "expected-baked-node-modules"
  custom = tmp_path / "custom-node-modules"
  frontend.mkdir(parents=True)
  expected_baked.mkdir()
  custom.mkdir()
  (frontend / "package-lock.json").write_text("{}")
  node_modules = frontend / "node_modules"
  node_modules.symlink_to(custom, target_is_directory=True)
  monkeypatch.setattr(pu, "BAKED_FRONTEND_NODE_MODULES", expected_baked)
  monkeypatch.setattr(pu, "_record_dependency_inputs", lambda *args, **kwargs: None)

  def must_not_run(*args, **kwargs):
    raise AssertionError("npm ci must not mutate an unexpected symlink target")

  monkeypatch.setattr(pu.subprocess, "run", must_not_run)

  ok, error = pu._sync_frontend_dependencies(platform)

  assert ok is False
  assert "unexpected symlink" in error
  assert node_modules.is_symlink()
  assert node_modules.resolve() == custom


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


def test_next_reconcile_retires_progress_from_dead_process(clone_env):
  _, platform = clone_env
  target = _served_sha(platform)
  plan_id = "a" * 64
  original = dict(pu._UPDATE_PROGRESS)
  try:
    pu._set_update_progress(
      pu.PlatformUpdatePhase.BUILDING,
      plan_id=plan_id,
      target_sha=target,
      active=True,
    )

    with pu._reconcile_flock():
      pass

    recovered = pu.platform_update_progress()
    assert recovered["phase"] == pu.PlatformUpdatePhase.FAILED.value
    assert recovered["active"] is False
    assert recovered["error"] == (
      "Möbius restarted before this update finished. Review the update again "
      "before retrying."
    )
    assert recovered["plan_id"] == plan_id
    assert recovered["target_sha"] == target
  finally:
    pu._UPDATE_PROGRESS.update(original)


@pytest.mark.asyncio
async def test_live_apply_progress_survives_its_own_reconcile_lock(clone_env):
  _, platform = clone_env
  target = _served_sha(platform)
  plan_id = "b" * 64
  original = dict(pu._UPDATE_PROGRESS)
  try:
    pu._set_update_progress(
      pu.PlatformUpdatePhase.RECONCILING,
      plan_id=plan_id,
      target_sha=target,
      active=True,
    )

    async with pu._APPLY_LOCK:
      with pu._reconcile_flock():
        pass

    current = pu.platform_update_progress()
    assert current["phase"] == pu.PlatformUpdatePhase.RECONCILING.value
    assert current["active"] is True
    assert current["error"] is None
    assert current["plan_id"] == plan_id
    assert current["target_sha"] == target
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
  def observed_lock(*, blocking=True):
    events.append(("entered", blocking))
    try:
      yield
    finally:
      events.append("exited")

  monkeypatch.setattr(pu, "_reconcile_flock", observed_lock)

  pu.platform_update_preview(platform)

  assert events == [("entered", False), "exited"]


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


def test_update_preview_shows_dependency_change_needs_an_image(clone_env):
  origin, platform = clone_env
  _advance_origin(
    origin,
    edits={"backend/requirements.lock": "locked dependency bytes\n"},
    msg="change dependency",
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)

  assert preview["activation"]["level"] == "image_rebuild"
  guidance = " ".join(preview["activation"]["guidance"])
  assert "Rebuild and replace" in guidance


def test_update_preview_accepts_target_dependencies_already_in_running_image(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  base = _advance_origin(origin, edits={
    "backend/requirements.txt": "package-a==1\n",
    "backend/requirements.lock": "package-a==1 --hash=sha256:old\n",
  })
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", base)
  _git(platform, "branch", "-f", "upstream", base)
  target = _advance_origin(origin, edits={
    "backend/requirements.txt": "package-a==2\n",
    "backend/requirements.lock": "package-a==2 --hash=sha256:new\n",
  })
  pu._fetch(platform)

  image_inputs = {
    path: hashlib.sha256(
      _git(platform, "show", f"{target}:{path}").stdout.encode(),
    ).hexdigest()
    for path in pu._PYTHON_DEPENDENCY_INPUTS
  }
  monkeypatch.setattr(pu, "_build_info", lambda: {"image_inputs": image_inputs})

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert pu.target_python_inputs_baked_into_image(platform, target) is True
  assert preview["activation"]["level"] == "live"
  assert all(
    reason["code"] != "python_dependencies"
    for reason in preview["incoming_activation"]["reasons"]
  )


@pytest.mark.parametrize("provenance", ["missing", "malformed", "mismatch"])
def test_target_dependency_proof_fails_closed_for_incomplete_provenance(
  clone_env, monkeypatch, provenance,
):
  origin, platform = clone_env
  base = _advance_origin(origin, edits={
    "backend/requirements.txt": "package-a==1\n",
    "backend/requirements.lock": "package-a==1 --hash=sha256:old\n",
  })
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", base)
  _git(platform, "branch", "-f", "upstream", base)
  target = _advance_origin(origin, edits={
    "backend/requirements.lock": "package-a==2 --hash=sha256:new\n",
  })
  pu._fetch(platform)
  lock_bytes = _git(
    platform, "show", f"{target}:backend/requirements.lock",
  ).stdout.encode()
  image_inputs = {
    path: hashlib.sha256(
      _git(platform, "show", f"{target}:{path}").stdout.encode(),
    ).hexdigest()
    for path in pu._PYTHON_DEPENDENCY_INPUTS
  }
  if provenance == "missing":
    image_inputs.pop("backend/requirements.txt")
  elif provenance == "malformed":
    image_inputs["backend/requirements.txt"] = "not-a-digest"
  else:
    image_inputs["backend/requirements.lock"] = hashlib.sha256(
      lock_bytes + b"different",
    ).hexdigest()
  monkeypatch.setattr(pu, "_build_info", lambda: {"image_inputs": image_inputs})

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert pu.target_python_inputs_baked_into_image(platform, target) is False
  assert preview["activation"]["level"] == "image_rebuild"


@pytest.mark.asyncio
async def test_source_apply_refuses_image_owned_update_before_mutation(clone_env):
  origin, platform = clone_env
  before = _served_sha(platform)
  target = _advance_origin(
    origin,
    edits={"backend/requirements.lock": "new locked dependencies\n"},
    msg="change Python dependencies",
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform)

  with pytest.raises(pu.PlatformUpdateError, match="image_rebuild_required"):
    await pu.apply_platform_update(
      SimpleNamespace(),
      **_apply_plan(before, target, platform),
    )

  assert _served_sha(platform) == before
  assert preview["activation"]["level"] == "image_rebuild"


@pytest.mark.asyncio
async def test_source_apply_uses_target_dependency_proof_from_running_image(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  base = _advance_origin(origin, edits={
    "backend/requirements.txt": "package-a==1\n",
    "backend/requirements.lock": "package-a==1 --hash=sha256:old\n",
  })
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", base)
  _git(platform, "branch", "-f", "upstream", base)
  target = _advance_origin(origin, edits={
    "backend/requirements.txt": "package-a==2\n",
    "backend/requirements.lock": "package-a==2 --hash=sha256:new\n",
  })
  pu._fetch(platform)
  image_inputs = {
    path: hashlib.sha256(
      _git(platform, "show", f"{target}:{path}").stdout.encode(),
    ).hexdigest()
    for path in pu._PYTHON_DEPENDENCY_INPUTS
  }
  monkeypatch.setattr(pu, "_build_info", lambda: {"image_inputs": image_inputs})

  preview = pu.platform_update_preview(platform, target_sha=target)
  result = await pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=base,
    target_sha=target,
    repo=platform,
  )

  assert _served_sha(platform) == target
  assert result["state"] != "activation_needed"
  assert all(
    reason["code"] != "python_dependencies"
    for reason in result["activation"]["reasons"]
  )


@pytest.mark.asyncio
async def test_local_image_drift_does_not_block_unrelated_source_update(clone_env):
  origin, platform = clone_env
  current = _local_commit(
    platform,
    edits={"backend/requirements.lock": "owner-package==1\n"},
  )
  target = _advance_origin(
    origin, edits={"docs/update-note.md": "safe source-only update\n"},
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform)
  result = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(current, target, platform),
  )

  assert preview["activation"]["level"] == "live"
  # Applying unrelated source must not erase an existing image remainder.
  assert result["state"] == pu.PlatformUpdateState.RESTART_NEEDED.value
  assert pu._is_ancestor(platform, target, _served_sha(platform))


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
  assert preview["conflict_paths"] == []


def test_update_preview_does_not_replay_local_overlay(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served = _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 111"),
  })
  _advance_origin(origin, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 222"),
  })
  pu._fetch(platform)
  worktrees_before = _git(platform, "worktree", "list", "--porcelain").stdout

  preview = pu.platform_update_preview(platform)

  # The read-only net-tree check predicts the real same-line conflict without
  # replaying historical commits or materializing a resolver worktree.
  assert preview["conflict_paths"] == ["backend/app/main.py"]
  assert _served_sha(platform) == served
  assert _git(platform, "status", "--porcelain").stdout == ""
  assert _git(platform, "worktree", "list", "--porcelain").stdout == worktrees_before
  assert not pu.CONFLICT_FLAG.exists()


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
async def test_cancelled_apply_finishes_its_admitted_transaction(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  reviewed = _advance_origin(
    origin,
    edits={"backend/app/main.py":
      _MAIN_PY.replace("LINE_C = 3", "LINE_C = 399")},
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform)
  worker_started = threading.Event()
  release_worker = threading.Event()
  reconcile = pu._reconcile_under_lock

  def delayed_reconcile(*args, **kwargs):
    worker_started.set()
    assert release_worker.wait(timeout=5)
    return reconcile(*args, **kwargs)

  monkeypatch.setattr(pu, "_reconcile_under_lock", delayed_reconcile)
  applying = asyncio.create_task(pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=preview["current_sha"],
    target_sha=preview["target_sha"],
    repo=platform,
  ))
  assert await asyncio.to_thread(worker_started.wait, 2)

  applying.cancel()
  await asyncio.sleep(0)
  applying.cancel()
  release_worker.set()
  with pytest.raises(asyncio.CancelledError):
    await applying

  assert _served_sha(platform) == reviewed
  progress = pu.platform_update_progress()
  assert progress["active"] is False
  assert progress["phase"] == pu.PlatformUpdatePhase.COMPLETE.value


@pytest.mark.asyncio
async def test_cancelled_apply_reports_cancellation_after_transaction_failure(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  target = _advance_origin(
    origin,
    edits={"backend/app/main.py":
      _MAIN_PY.replace("LINE_C = 3", "LINE_C = 401")},
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform, target_sha=target)
  worker_started = threading.Event()
  release_worker = threading.Event()

  def fail_reconcile(*args, **kwargs):
    worker_started.set()
    assert release_worker.wait(timeout=5)
    raise RuntimeError("reconcile failed after disconnect")

  monkeypatch.setattr(pu, "_reconcile_under_lock", fail_reconcile)
  applying = asyncio.create_task(pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=preview["current_sha"],
    target_sha=preview["target_sha"],
    repo=platform,
  ))
  assert await asyncio.to_thread(worker_started.wait, 2)

  applying.cancel()
  release_worker.set()
  with pytest.raises(asyncio.CancelledError):
    await applying

  progress = pu.platform_update_progress()
  assert progress["active"] is False
  assert progress["phase"] == pu.PlatformUpdatePhase.FAILED.value
  assert progress["error"] == "reconcile failed after disconnect"


@pytest.mark.asyncio
async def test_apply_keeps_cross_process_lock_through_final_progress(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  target = _advance_origin(
    origin,
    edits={"backend/app/main.py":
      _MAIN_PY.replace("LINE_C = 3", "LINE_C = 402")},
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform, target_sha=target)
  finalizing = threading.Event()
  release_finalizing = threading.Event()

  def delayed_hook_refresh(*_args, **_kwargs):
    finalizing.set()
    assert release_finalizing.wait(timeout=5)
    return None

  monkeypatch.setattr(pu, "_refresh_git_hooks", delayed_hook_refresh)
  applying = asyncio.create_task(pu.apply_platform_update(
    SimpleNamespace(),
    plan_id=preview["plan_id"],
    current_sha=preview["current_sha"],
    target_sha=preview["target_sha"],
    repo=platform,
  ))
  assert await asyncio.to_thread(finalizing.wait, 2)

  with pytest.raises(pu.PlatformUpdateError, match="platform_update_in_progress"):
    with pu._reconcile_flock(blocking=False):
      pass
  progress = pu.platform_update_progress()
  assert progress["active"] is True
  assert progress["phase"] == pu.PlatformUpdatePhase.FINALIZING.value

  release_finalizing.set()
  result = await applying
  assert result["state"] in {
    pu.PlatformUpdateState.UP_TO_DATE.value,
    pu.PlatformUpdateState.RESTART_NEEDED.value,
    pu.PlatformUpdateState.ACTIVATION_NEEDED.value,
  }
  assert pu.platform_update_progress()["active"] is False


def test_preview_reports_an_active_update_instead_of_waiting(clone_env):
  _origin, platform = clone_env

  with pu._reconcile_flock():
    with pytest.raises(pu.PlatformUpdateError) as caught:
      pu.platform_update_preview(platform)

  assert str(caught.value) == "platform_update_in_progress"


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
  pu._write_activation_marker(before, ["backend/app"])
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
  assert pu._read_activation_marker() == {
    "version": 2,
    "target_sha": before,
    "upstream_sha": None,
    "paths": ["backend/app"],
    "image_paths": [],
  }
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


@pytest.mark.asyncio
async def test_frontend_build_failure_never_rewinds_a_concurrent_writer(
  monkeypatch, clone_env,
):
  origin, platform = clone_env
  target = _advance_origin(
    origin,
    edits={"frontend/src/App.jsx": "export default 'broken candidate'\n"},
  )
  pu._fetch(platform)
  preview = pu.platform_update_preview(platform)
  raced: dict[str, str] = {}

  def fail_after_concurrent_commit(_repo, _result):
    raced["sha"] = _local_commit(
      platform,
      edits={"concurrent.txt": "newer owner\n"},
      msg="concurrent writer during frontend build",
    )
    raise RuntimeError("vite exploded")

  monkeypatch.setattr(pu, "_rebuild_frontend", fail_after_concurrent_commit)

  with pytest.raises(pu.PlatformUpdateError, match="rollback_ref_changed"):
    await pu.apply_platform_update(
      SimpleNamespace(),
      plan_id=preview["plan_id"],
      current_sha=preview["current_sha"],
      target_sha=preview["target_sha"],
      repo=platform,
    )

  assert _served_sha(platform) == raced["sha"]
  assert (platform / "concurrent.txt").read_text() == "newer owner\n"
  assert _git(
    platform, "merge-base", "--is-ancestor", target, raced["sha"],
  ).returncode == 0


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

  # A dependency bump needs the same reviewed image boundary as Dockerfile.
  (platform / "backend" / "requirements.lock").write_text("a==2\n")
  assert pu.image_input_drift(platform) == ["backend/requirements.lock"]
  assert pu.platform_status(platform)["activation"]["level"] == "image_rebuild"
  (platform / "Dockerfile").write_text("FROM python:3.13\n")
  assert pu.image_input_drift(platform) == [
    "Dockerfile", "backend/requirements.lock",
  ]
  assert pu.platform_status(platform)["activation"]["level"] == "image_rebuild"


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
  assert preview["incoming_activation"]["level"] == "live"
  assert preview["plan_id"] == pu._update_plan_id(target, target, digest)
  reviewed = pu.reviewed_container_rebuild_plan(
    repo=platform, current_sha=target, target_sha=target,
    image_digest=digest, plan_id=preview["plan_id"],
  )
  assert reviewed["activation"]["level"] == "image_rebuild"


def test_new_source_review_does_not_mix_in_unfinished_image_activation(clone_env):
  origin, platform = clone_env
  current = _served_sha(platform)
  pu.mark_activation_needed(current, ["Dockerfile"], upstream_sha=current, repo=platform)
  target = _advance_origin(origin, edits={"frontend/src/App.jsx": "new shell"})
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert preview["available"] is True
  assert preview["operation"] == "update"
  assert preview["actionable"] is True
  assert preview["activation"]["level"] == "live"
  assert preview["incoming_activation"]["level"] == "live"
  assert pu.platform_status(platform)["activation"]["level"] == "image_rebuild"


@pytest.mark.asyncio
async def test_finish_plan_does_not_reapply_source_or_dependencies(clone_env, monkeypatch):
  _, platform = clone_env
  current = _served_sha(platform)
  pu.mark_activation_needed(current, ["Dockerfile"], upstream_sha=current, repo=platform)
  preview = pu.platform_update_preview(platform)

  def no_install(_repo):
    pytest.fail("A finish-only plan must not reinstall dependencies")

  monkeypatch.setattr(pu, "_sync_frontend_dependencies", no_install)
  result = await pu.apply_platform_update(
    SimpleNamespace(), **_apply_plan(current, current, platform),
    allow_image_activation=True,
  )

  assert preview["operation"] == "finish"
  assert result["activation"]["level"] == "image_rebuild"
  assert _served_sha(platform) == current
  assert result["merge_commit"] is None


def test_image_input_drift_ignores_generated_files_but_keeps_local_source(
  clone_env, monkeypatch,
):
  _, platform = clone_env
  runtime = platform / "backend/runtime"
  runtime.mkdir(parents=True)
  baked = platform_activation.image_input_hashes(platform)
  ignored = runtime / "__pycache__/broker.cpython-312.pyc"
  ignored.parent.mkdir()
  ignored.write_bytes(b"local bytecode")
  local_source = runtime / "local.py"
  local_source.write_text("VALUE = 'local'\n")

  baked[str(ignored.relative_to(platform))] = "0" * 64
  monkeypatch.setattr(pu, "_build_info", lambda: {"image_inputs": baked})

  assert pu.image_input_drift(platform) == ["backend/runtime/local.py"]


def test_image_input_drift_ignores_reclassified_baked_path_but_keeps_deletion(
  clone_env, monkeypatch,
):
  _, platform = clone_env
  runtime = platform / "backend/runtime"
  runtime.mkdir(parents=True, exist_ok=True)
  broker = platform / "backend/runtime/identity_broker.py"
  ledger = platform / "backend/runtime/restart_ledger.py"
  broker.write_text("served broker\n", encoding="utf-8")
  ledger.write_text("image-owned ledger\n", encoding="utf-8")
  _git(platform, "add", "backend/runtime")
  _git(platform, "commit", "-q", "-m", "runtime inputs")
  baked = platform_activation.image_input_hashes(platform)
  # Model an older image whose manifest still classified the broker as frozen.
  baked[str(broker.relative_to(platform))] = hashlib.sha256(
    broker.read_bytes(),
  ).hexdigest()
  monkeypatch.setattr(pu, "_build_info", lambda: {"image_inputs": baked})

  assert pu.image_input_drift(platform) == []

  # A currently image-owned tracked input that disappears must still block a
  # replacement; otherwise the image would silently restore the deleted file.
  ledger.unlink()
  assert pu.image_input_drift(platform) == [
    "backend/runtime/restart_ledger.py",
  ]


@pytest.mark.asyncio
async def test_failed_frontend_build_restores_its_dependency_lock(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  initial = _advance_origin(origin, edits={
    "frontend/package-lock.json": "old frontend",
  })
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", initial)
  _git(platform, "branch", "-f", "upstream", initial)
  target = _advance_origin(origin, edits={
    "frontend/package-lock.json": "new frontend",
  })
  pu._fetch(platform)
  installs = []
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
  assert installs == ["new frontend", "old frontend"]
  assert _served_sha(platform) == initial


def test_frontend_dependency_restore_failure_is_visible_in_durable_rollback(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  initial = _advance_origin(origin, edits={"frontend/package-lock.json": "old lock"})
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", initial)
  _git(platform, "branch", "-f", "upstream", initial)
  _advance_origin(origin, edits={
    "frontend/package-lock.json": "new lock",
    "frontend/src/App.jsx": "export default 'new'\n",
  })
  monkeypatch.setattr(pu, "_sync_frontend_dependencies", lambda repo: (False, "network unavailable"))

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
  for name in ("_fetch", "_fetch_unshallow", "reconcile_clone", "_sync_frontend_dependencies"):
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
  target = _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'candidate'\n"})
  _git(platform, "fetch", "origin")
  pu._write_reconcile_pre(carried.pre, target)
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
  target = _advance_origin(origin, edits={
    "backend/app/foo.py": "VALUE = 'reviewed'\n",
    "Dockerfile": "FROM reviewed-image\n",
  })
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
  assert (platform / "Dockerfile").read_text() == "FROM reviewed-image\n"
  assert (platform / "local.txt").read_text() == "owner working edit"
  assert _git(platform, "status", "--porcelain").stdout.strip() == "M local.txt"


@pytest.mark.parametrize("deployment", ["self_hosted", "railway"])
def test_review_exposes_seed_customization_before_replacement_without_mutation(
  clone_env, monkeypatch, deployment,
):
  origin, platform = clone_env
  monkeypatch.setattr(platform_activation, "deployment_kind", lambda: deployment)
  paths = ["backend/scripts/seed-skills/cron.md", "backend/scripts/seed-skills/waiting.md"]
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
  assert preview["blocking_diff"] is not None
  assert "backend/scripts/seed-skills/cron.md" in preview["blocking_diff"]
  assert "local instructions" in preview["blocking_diff"]
  assert preview["blocking_diff_truncated"] is False
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
  assert preview["blocking_diff"] is None
  assert preview["blocking_diff_truncated"] is False


def test_review_exposes_uncommitted_image_input_blocker(clone_env):
  origin, platform = clone_env
  dockerfile = platform / "Dockerfile"
  dockerfile.write_text("FROM local-owner-image\n")
  target = _advance_origin(
    origin,
    edits={"Dockerfile": "FROM reviewed-official-image\n"},
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert preview["blocking_paths"] == ["Dockerfile"]
  assert "local-owner-image" in preview["blocking_diff"]
  assert "reviewed-official-image" in preview["blocking_diff"]


def test_review_does_not_follow_uncommitted_image_input_symlink(clone_env):
  origin, platform = clone_env
  secret = platform.parent / "outside-secret"
  secret.write_text("do-not-expose\n")
  dockerfile = platform / "Dockerfile"
  dockerfile.symlink_to(secret)
  target = _advance_origin(
    origin,
    edits={"Dockerfile": "FROM reviewed-official-image\n"},
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert preview["blocking_paths"] == ["Dockerfile"]
  assert str(secret) in preview["blocking_diff"]
  assert "do-not-expose" not in preview["blocking_diff"]


def test_review_does_not_block_on_uncommitted_image_input_fifo(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={"Dockerfile": "FROM local-owner-image\n"})
  dockerfile = platform / "Dockerfile"
  dockerfile.unlink()
  os.mkfifo(dockerfile)
  target = _advance_origin(
    origin,
    edits={"Dockerfile": "FROM reviewed-official-image\n"},
  )
  pu._fetch(platform)

  def stalled(_signum, _frame):
    raise AssertionError("Review blocked while opening a local named pipe")

  previous = signal.signal(signal.SIGALRM, stalled)
  signal.alarm(5)
  try:
    preview = pu.platform_update_preview(platform, target_sha=target)
  finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)

  assert preview["blocking_paths"] == ["Dockerfile"]
  assert "reviewed-official-image" in preview["blocking_diff"]


def test_review_describes_a_locally_deleted_image_input(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={"Dockerfile": "FROM local-owner-image\n"})
  (platform / "Dockerfile").unlink()
  target = _advance_origin(
    origin,
    edits={"Dockerfile": "FROM reviewed-official-image\n"},
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert preview["blocking_paths"] == ["Dockerfile"]
  assert "local path is not present: Dockerfile" in preview["blocking_diff"]
  assert "crosses a link" not in preview["blocking_diff"]


def test_review_does_not_follow_image_input_ancestor_symlink(clone_env):
  origin, platform = clone_env
  path = "backend/runtime/private.py"
  _local_commit(platform, edits={path: "VALUE = 'local'\n"})
  runtime = platform / "backend" / "runtime"
  for child in runtime.iterdir():
    child.unlink()
  runtime.rmdir()
  outside = platform.parent / "outside-runtime"
  outside.mkdir()
  (outside / "private.py").write_text("outside-secret\n")
  runtime.symlink_to(outside, target_is_directory=True)
  target = _advance_origin(
    origin, edits={path: "VALUE = 'reviewed'\n"},
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert path in preview["blocking_paths"]
  assert "outside-secret" not in preview["blocking_diff"]
  assert "crosses a link" in preview["blocking_diff"]


def test_finish_review_exposes_local_image_blockers_too(clone_env):
  _, platform = clone_env
  official = _served_sha(platform)
  path = "backend/scripts/seed-skills/cron.md"
  _local_commit(platform, edits={path: "preserve me\n"})
  pu.mark_activation_needed(_served_sha(platform), [path], upstream_sha=official, repo=platform)

  preview = pu.platform_update_preview(platform, target_sha=official)

  assert preview["operation"] == "finish"
  assert preview["blocking_paths"] == [path]
  assert "preserve me" in preview["blocking_diff"]
