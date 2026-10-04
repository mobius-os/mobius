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
from app.chat_writer import create_chat

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

from app import app_git, platform_activation, platform_boot
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
  (app_dir / "routes.py").write_text("def require_all_routers_loaded():\n  return None\n")
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
  monkeypatch.setattr(
    pu, "PREPARED_UPDATE_PATH", tmp_path / ".prepared-update.json",
  )
  # The real startup check imports the full platform; these fixture clones
  # carry only a minimal backend, so the check is exercised separately.
  monkeypatch.setattr(
    "app.restart_util.validate_restart_source", lambda platform_root=None, **_kwargs: None,
  )
  monkeypatch.setenv("BUILD_SHA", "test-sha")
  # This boot's image runs the boot transaction; tests of images before it
  # remove the marker. ``_boot_image`` chooses the running image's revision.
  monkeypatch.setattr(pu, "BOOT_TRANSACTION_MARKER", tmp_path / ".boot-transaction")
  pu.BOOT_TRANSACTION_MARKER.write_text("1\n")
  monkeypatch.setenv("MOBIUS_BUILD_INFO_PATH", str(tmp_path / "build-info.json"))
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
  assert _git(platform, "rev-parse", pu._PRE_UPDATE_REF).stdout.strip() == res.pre_sha
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
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "replayed"

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

  result = pu.reconcile_clone(platform)

  assert result.status == "updated"
  assert _parents(platform) == [target]
  assert len(_overlay_subjects(platform, target)) == 1
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'LOCAL 34'\n"
  assert "LINE_C = 45" in (platform / "backend/app/main.py").read_text()
  assert _git(platform, "rev-parse", pu._PRE_UPDATE_REF).stdout.strip() == result.pre_sha
  assert original != result.pre_sha


def _boot_started_server(platform: Path, source: str = "platform") -> None:
  """What the boot script records once it has chosen what uvicorn serves."""
  pu.SERVING_SOURCE_FILE.write_text(source + "\n")
  pu.SERVING_SHA_FILE.write_text(_served_sha(platform) + "\n")


def _boot_image(sha: str) -> None:
  """Boot a container whose image was built from ``sha``."""
  Path(os.environ["MOBIUS_BUILD_INFO_PATH"]).write_text(json.dumps({"sha": sha}))


def _finish_prepared(platform: Path) -> str | None:
  """Finish a prepared update across its cutover: the outgoing server swaps
  in only a restart-only update, and the update's own image swaps in one that
  needs it. That boot merges late edits back before import; then the server
  starts, finishes before resumes, and confirms."""
  record = pu.read_prepared_update()
  swapped = pu.swap_in_prepared_update(cutover=True, repo=platform)
  assert swapped == (not record["requires_image"])
  _boot_image(record["target"])
  before_import = pu.settle_prepared_update_for_this_image(platform)
  _boot_started_server(platform)
  outcome = pu.complete_platform_swap(platform)
  assert before_import == outcome
  pu.confirm_platform_swap_loaded(platform)
  return outcome


def test_a_swapped_version_that_fails_to_start_returns_to_the_previous_state(
  clone_env,
):
  origin, platform = clone_env
  served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  dirty = platform / "backend/app/foo.py"
  dirty.write_text("VALUE = 'KEEP ME'\n")
  assert pu.swap_in_prepared_update(cutover=True, repo=platform)
  record = pu.read_prepared_update()

  # What the boot script does when the swapped-in version fails its check.
  _git(platform, "reset", "-q", "--hard", record["late"])
  pu._write_prepared_update({**record, "state": "reverted"})

  assert pu.complete_platform_swap(platform) == "reverted"
  assert _served_sha(platform) == served
  assert dirty.read_text() == "VALUE = 'KEEP ME'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]
  assert pu._read_rolled_back_flag()["target"] == target
  # The update stays prepared: Finish can be retried or the update cancelled.
  assert pu.read_prepared_update()["state"] == "prepared"
  assert pu.unfinished_update(platform)["cancellable"] is True
  assert not pu.late_edits_pending()


def test_boot_revert_returns_to_the_saved_state_only_after_a_swap(clone_env):
  origin, platform = clone_env
  served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"

  # Nothing swapped yet: the boot revert leaves the checkout alone.
  assert not pu.revert_failed_update(platform)
  assert _served_sha(platform) == served

  assert pu.swap_in_prepared_update(cutover=True, repo=platform)
  assert _served_sha(platform) != served
  assert pu.revert_failed_update(platform)
  assert _served_sha(platform) == served
  # The update stays prepared, and the owner is told why it is not running.
  assert pu.read_prepared_update()["state"] == "prepared"
  assert pu._read_rolled_back_flag()["target"] == _target
  assert pu.complete_platform_swap(platform) is None


def test_a_prepared_update_can_be_cancelled_until_it_is_swapped_in(clone_env):
  origin, platform = clone_env
  served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert pu.unfinished_update(platform)["cancellable"] is True

  pu.cancel_unfinished_update(platform)
  assert pu.unfinished_update(platform) is None
  assert _served_sha(platform) == served
  assert pu.swap_in_prepared_update(cutover=True, repo=platform) is False


def test_a_cancel_racing_the_shutdown_swap_keeps_the_live_source(
  clone_env, monkeypatch,
):
  """The swap re-reads the update under the lock, so a Cancel that lands
  between the drain's first look and the lock is honored."""
  origin, platform = clone_env
  served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  real_lock = pu._reconcile_flock
  cancelled = []

  @contextmanager
  def cancel_before_locking(*args, **kwargs):
    if not cancelled:
      cancelled.append(True)
      pu.cancel_unfinished_update(platform)
    with real_lock(*args, **kwargs):
      yield

  monkeypatch.setattr(pu, "_reconcile_flock", cancel_before_locking)

  assert pu.swap_in_prepared_update(cutover=True, repo=platform) is False
  assert cancelled and pu.unfinished_update(platform) is None
  assert _served_sha(platform) == served


def test_a_parked_resolution_can_be_cancelled_before_its_swap(clone_env):
  """The owner's exit from a resolver that cannot finish: nothing is live yet."""
  origin, platform = clone_env
  served, _target, worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.unfinished_update(platform)["stage"] == "resolve"
  assert pu.unfinished_update(platform)["cancellable"] is True

  pu.cancel_unfinished_update(platform)

  assert pu.unfinished_update(platform) is None
  assert not pu.CONFLICT_FLAG.exists()
  assert not worktree.exists()
  assert _served_sha(platform) == served
  assert pu.platform_status(platform)["available"] is True


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


def test_aborted_parked_merge_cannot_be_committed_as_an_update(clone_env):
  origin, platform = clone_env
  served, _target, worktree = _park_resolved_line_a_conflict(platform, origin)
  _git(worktree, "merge", "--abort")

  with pytest.raises(pu.PlatformUpdateError, match="parked merge is no longer"):
    pu.continue_platform_overlay_update(platform)
  assert _served_sha(platform) == served
  assert pu.CONFLICT_FLAG.exists()


def test_resolver_merge_commit_is_an_accepted_resolution(clone_env):
  origin, platform = clone_env
  _served, _target, worktree = _park_resolved_line_a_conflict(platform, origin)
  _git(worktree, "commit", "-q", "-m", "resolve both sides")

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "replayed"
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()


def test_committed_resolution_with_conflict_markers_is_rejected(clone_env):
  origin, platform = clone_env
  served, _target, worktree = _park_resolved_line_a_conflict(platform, origin)
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace(
      "LINE_A = 1",
      "<<<<<<< ours\nLINE_A = 'LOCAL'\n=======\nLINE_A = 'UPSTREAM'\n>>>>>>> theirs",
    )
  )
  _git(worktree, "add", "backend/app/main.py")
  _git(worktree, "commit", "-q", "-m", "incorrectly accept markers")

  with pytest.raises(pu.PlatformUpdateError, match="Conflict markers remain"):
    pu.continue_platform_overlay_update(platform)
  assert _served_sha(platform) == served
  assert pu.CONFLICT_FLAG.exists()


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
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  # Preparing never touches the live checkout.
  assert "LINE_A = 'LOCAL'" in (platform / "backend/app/main.py").read_text()
  assert _finish_prepared(platform) == "replayed"
  assert "LINE_A = 'FINAL'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "notes.txt").read_text() == "resolved with context\n"
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]


def test_late_commits_stay_out_of_the_update_return_after_boot_and_stay_reachable(clone_env):
  origin, platform = clone_env
  served, target, worktree = _park_resolved_line_a_conflict(platform, origin)
  _local_commit(platform, edits={"backend/app/late.py": "LATE = 1\n"}, msg="late 1")
  late = _local_commit(
    platform, edits={"backend/app/late.py": "LATE = 2\n"}, msg="late 2",
  )

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  # The live checkout keeps serving until the swap; the update is exactly the
  # answer on the reviewed release, without the late commits.
  assert _served_sha(platform) == late
  prepared = pu.read_prepared_update()["prepared"]
  assert _git(platform, "cat-file", "-e", f"{prepared}:backend/app/late.py",
              check=False).returncode != 0
  assert not pu.CONFLICT_FLAG.exists()
  assert not worktree.exists()

  assert _finish_prepared(platform) == "replayed"
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "backend/app/late.py").read_text() == "LATE = 2\n"
  assert _git(platform, "status", "--porcelain").stdout == ""
  assert pu.read_prepared_update() is None
  assert not pu.late_edits_pending()
  assert served != late
  # The replaced local chain, late commits and their messages included, stays
  # reachable for undo after the update squashes it into one commit.
  assert _git(platform, "rev-parse", pu._PRE_UPDATE_REF).stdout.strip() == late
  assert "late 2" in _git(platform, "log", "-1", "--format=%s", pu._PRE_UPDATE_REF).stdout


def test_a_late_commit_that_conflicts_is_parked_after_boot_and_holds_resumes(
  clone_env,
):
  origin, platform = clone_env
  _served, target, worktree = _park_resolved_line_a_conflict(platform, origin)
  _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LATE'"),
  }, msg="late conflicting edit")

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "conflict"

  # The checked update is what booted; the late edit waits on a frozen copy
  # and automatic chat resumes wait for it.
  booted = pu.read_prepared_update()["prepared"]
  assert _served_sha(platform) == booted
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert pu.late_edits_pending()
  flag = pu._read_conflict_flag()
  assert flag["overlay"]["replay"] is True
  assert pu.unfinished_update(platform)["stage"] == "resolve"
  # The update already booted: cancelling would strand the late edit.
  assert pu.unfinished_update(platform)["cancellable"] is False
  with pytest.raises(pu.PlatformUpdateError, match="prepared_update_swapped"):
    pu.cancel_unfinished_update(platform)
  assert pu._read_conflict_flag() == flag
  replay = Path(flag["overlay"]["worktree"])
  marked = (replay / "backend/app/main.py").read_text()
  assert "LINE_A = 'LATE'" in marked and "LINE_A = 'RESOLVED'" in marked

  (replay / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED AND LATE'"),
  )
  _git(replay, "add", "backend/app/main.py")
  assert pu.continue_platform_overlay_update(platform) == "updated"
  assert "LINE_A = 'RESOLVED AND LATE'" in (
    platform / "backend/app/main.py"
  ).read_text()
  assert not pu.late_edits_pending()
  assert pu._is_ancestor(platform, target, _served_sha(platform))


def test_uncommitted_late_edits_survive_a_conflicting_late_commit(clone_env):
  """A late commit's conflict parks first; the uncommitted edits made on top
  of it must still come back afterwards, never be dropped with the park."""
  origin, platform = clone_env
  _served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LATE'"),
  }, msg="late conflicting commit")
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LATE DIRTY'"),
  )
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "conflict"
  replay = Path(pu._read_conflict_flag()["overlay"]["worktree"])
  (replay / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED AND LATE'"),
  )
  _git(replay, "add", "backend/app/main.py")

  # The committed answer is in; the uncommitted edit to the same line now
  # overlaps it, so it parks for the resolver instead of vanishing.
  assert pu.continue_platform_overlay_update(platform) == "conflict"
  assert pu.late_edits_pending()
  flag = pu._read_conflict_flag()
  assert flag["overlay"]["stage"] == "working" and flag["overlay"]["replay"] is True
  replay = Path(flag["overlay"]["worktree"])
  marked = (replay / "backend/app/main.py").read_text()
  assert "LINE_A = 'LATE DIRTY'" in marked and "LINE_A = 'RESOLVED AND LATE'" in marked
  (replay / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED, LATE AND DIRTY'"),
  )
  _git(replay, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "updated"
  assert not pu.late_edits_pending()
  assert "LINE_A = 'RESOLVED, LATE AND DIRTY'" in (
    platform / "backend/app/main.py"
  ).read_text()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  # In-progress edits come back uncommitted, as after a clean replay.
  assert sorted(_git(platform, "status", "--porcelain").stdout.splitlines()) == [
    " M backend/app/foo.py", " M backend/app/main.py",
  ]
  assert pu._is_ancestor(platform, target, _served_sha(platform))
  # The replay is not an update: the chain the swap replaced stays recorded.
  assert "late conflicting commit" in _git(
    platform, "log", "-1", "--format=%s", pu._PRE_UPDATE_REF,
  ).stdout


def _swapped_with_late_commit(platform: Path, origin: Path) -> tuple[str, str]:
  _served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  _local_commit(platform, edits={"backend/app/late.py": "LATE = 1\n"})
  assert pu.swap_in_prepared_update(cutover=True, repo=platform)
  return target, pu.read_prepared_update()["late"]


def test_late_edits_merge_back_before_the_server_imports_the_update(
  clone_env,
):
  """The image's boot transaction merges late edits back, so the server that
  starts next loads the update and the edits together."""
  origin, platform = clone_env
  target, _late = _swapped_with_late_commit(platform, origin)

  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"

  assert (platform / "backend/app/late.py").read_text() == "LATE = 1\n"
  assert pu._is_ancestor(platform, target, _served_sha(platform))
  # Only a started server confirms: repeating boot or startup keeps the
  # rollback record, and so does a server started from the baked fallback,
  # which never loaded the edits, so resumes stay held there.
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  _boot_started_server(platform, source="baked")
  assert pu.complete_platform_swap(platform) == "replayed"
  assert pu.late_edits_pending()
  assert not pu.confirm_platform_swap_loaded(platform)
  assert pu.read_prepared_update()["replayed"] == _served_sha(platform)

  _boot_started_server(platform)
  assert not pu.late_edits_pending()
  assert pu.confirm_platform_swap_loaded(platform)
  assert pu.read_prepared_update() is None


def test_a_merged_back_tree_that_cannot_start_returns_to_the_previous_state(
  clone_env, tmp_path,
):
  origin, platform = clone_env
  target, late = _swapped_with_late_commit(platform, origin)
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"

  # The server could not import it; the boot's probe fails and the boot
  # transaction returns to the saved previous state.
  assert pu.revert_failed_update(platform)

  assert _served_sha(platform) == late
  assert pu.settle_prepared_update_for_this_image(platform) == "waiting"
  assert pu.complete_platform_swap(platform) is None
  assert (platform / "backend/app/late.py").read_text() == "LATE = 1\n"
  assert pu.read_prepared_update()["state"] == "prepared"
  assert pu._read_rolled_back_flag()["target"] == target


@pytest.mark.parametrize("killed_in", ["_clear_reconcile_pre", "_restore_working_edits"])
def test_a_merge_back_killed_midway_finishes_on_the_next_boot(
  clone_env, monkeypatch, killed_in,
):
  origin, platform = clone_env
  target, _late = _swapped_with_late_commit(platform, origin)
  (platform / "notes.txt").write_text("in progress\n")
  real = getattr(pu, killed_in)
  calls = []

  def killed(*args, **kwargs):
    if not calls:
      calls.append(killed_in)
      raise KeyboardInterrupt("killed")
    return real(*args, **kwargs)

  monkeypatch.setattr(pu, killed_in, killed)
  with pytest.raises(KeyboardInterrupt):
    pu.settle_prepared_update_for_this_image(platform)

  # Next boot: the transaction's guard recovers the checkout, then the
  # merge-back runs or is recognised as done; never "not swapped".
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  assert (platform / "backend/app/late.py").read_text() == "LATE = 1\n"
  assert pu._is_ancestor(platform, target, _served_sha(platform))
  assert pu.read_prepared_update()["replayed"] == _served_sha(platform)
  _boot_started_server(platform)
  assert pu.complete_platform_swap(platform) == "replayed"
  assert pu.confirm_platform_swap_loaded(platform)
  assert pu.unfinished_update(platform) is None


def test_startup_merges_late_edits_boot_missed_and_records_the_restart(
  clone_env, tmp_path,
):
  """An older image or a failed boot step leaves the merge-back to the
  started server, which already imported the update: say a restart is owed,
  hold resumes until it, and keep the rollback for a tree that cannot start."""
  origin, platform = clone_env
  _target, late = _swapped_with_late_commit(platform, origin)
  _boot_started_server(platform)  # started on the update without the edits
  booted = _served_sha(platform)

  assert pu.complete_platform_swap(platform) == "replayed"

  assert (platform / "backend/app/late.py").read_text() == "LATE = 1\n"
  marker = pu._read_activation_marker()
  assert marker["target_sha"] == _served_sha(platform) != booted
  assert "backend/app/late.py" in marker["paths"]
  assert pu.late_edits_pending()
  assert not pu.confirm_platform_swap_loaded(platform)
  # The restart's import check fails: the boot can still go back.
  assert pu.revert_failed_update(platform)
  assert _served_sha(platform) == late


def test_a_failed_restart_record_during_startup_keeps_resumes_held(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _swapped_with_late_commit(platform, origin)
  _boot_started_server(platform)

  def unavailable(*args, **kwargs):
    raise OSError("disk full")

  monkeypatch.setattr(pu, "_record_update_activation", unavailable)
  with pytest.raises(OSError):
    pu.complete_platform_swap(platform)

  assert pu.read_prepared_update()["replayed"] == _served_sha(platform)
  assert pu.late_edits_pending()  # this server still runs the update alone


def test_a_different_descendant_of_a_recorded_merge_back_is_not_trusted(
  clone_env,
):
  origin, platform = clone_env
  _swapped_with_late_commit(platform, origin)
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  record = pu.read_prepared_update()
  _git(platform, "reset", "-q", "--hard", record["prepared"])
  _local_commit(platform, edits={"other.txt": "not the late edits\n"})

  # The boot cannot say what this tree is, so it refuses to serve it.
  with pytest.raises(pu.BootTransactionError):
    pu.settle_prepared_update_for_this_image(platform)
  with pytest.raises(pu.BootTransactionError):
    pu.complete_platform_swap(platform)
  assert pu.read_prepared_update()["replayed"] == record["replayed"]


def test_boot_writes_the_merged_back_tree_without_group_write(
  clone_env, monkeypatch,
):
  """A group-writable served tree fails the served-runtime check and boots
  the baked platform instead; boot's umask must not leak into the files."""
  origin, platform = clone_env
  _swapped_with_late_commit(platform, origin)
  monkeypatch.setattr(pu, "PLATFORM_REPO", platform)
  previous = os.umask(0o002)
  try:
    assert platform_boot.main(["platform_boot", "activate"]) == 0
  finally:
    os.umask(previous)

  mode = (platform / "backend/app/late.py").stat().st_mode
  assert not mode & 0o022


def test_a_parked_late_edit_conflict_is_not_merged_again_on_the_next_boot(
  clone_env,
):
  origin, platform = clone_env
  _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY LATE'"),
  )
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "conflict"
  worktree = Path(pu._read_conflict_flag()["overlay"]["worktree"])
  answer = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'HALF RESOLVED'")
  (worktree / "backend/app/main.py").write_text(answer)

  # A restart while the resolver works keeps its parked merge and progress.
  assert pu.settle_prepared_update_for_this_image(platform) == "conflict"
  assert pu.complete_platform_swap(platform) == "conflict"
  assert (worktree / "backend/app/main.py").read_text() == answer
  assert pu.late_edits_pending()


def _resolvable_late_conflict(platform: Path, origin: Path) -> tuple[str, Path]:
  _park_resolved_line_a_conflict(platform, origin)
  _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LATE'"),
  }, msg="late conflicting commit")
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "conflict"
  booted = _served_sha(platform)  # the server started without the edits
  worktree = Path(pu._read_conflict_flag()["overlay"]["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED AND LATE'"),
  )
  _git(worktree, "add", "backend/app/main.py")
  return booted, worktree


def test_finishing_a_late_edit_conflict_records_the_restart_that_loads_it(
  clone_env,
):
  origin, platform = clone_env
  booted, _worktree = _resolvable_late_conflict(platform, origin)

  assert pu.continue_platform_overlay_update(platform) == "updated"

  marker = pu._read_activation_marker()
  assert marker["target_sha"] == _served_sha(platform) != booted
  assert "backend/app/main.py" in marker["paths"]


def test_a_late_edit_resolution_that_cannot_record_its_restart_stays_pending(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _booted, _worktree = _resolvable_late_conflict(platform, origin)

  def unavailable(*args, **kwargs):
    raise OSError("disk full")

  monkeypatch.setattr(pu, "_record_update_activation", unavailable)
  with pytest.raises(OSError):
    pu.continue_platform_overlay_update(platform)

  # Nothing claims the edits are loaded: resumes stay held for the next boot.
  assert pu.read_prepared_update()["state"] == "swapped"
  assert pu.late_edits_pending()


def test_late_uncommitted_edits_stay_uncommitted_after_the_swap(clone_env):
  origin, platform = clone_env
  _served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  (platform / "notes.txt").write_text("untracked dirty\n")

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "replayed"

  assert _parents(platform) == [target]
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  assert sorted(_git(platform, "status", "--porcelain").stdout.splitlines()) == [
    " M backend/app/foo.py", "?? notes.txt",
  ]


def test_a_late_uncommitted_edit_that_conflicts_parks_after_boot(clone_env):
  origin, platform = clone_env
  _served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'DIRTY LATE'"),
  )

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "conflict"

  parked = pu._read_conflict_flag()["overlay"]
  assert parked["stage"] == "working" and parked["replay"] is True
  worktree = Path(parked["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'RESOLVED WITH DIRTY'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "updated"
  # The booted update keeps the first answer; the owner's in-progress edit on
  # top of it comes back uncommitted.
  assert "LINE_A = 'RESOLVED'" in _git(
    platform, "show", "HEAD:backend/app/main.py",
  ).stdout
  assert "LINE_A = 'RESOLVED WITH DIRTY'" in (
    platform / "backend/app/main.py"
  ).read_text()
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/main.py",
  ]
  assert not pu.late_edits_pending()


def test_an_answer_that_fails_the_startup_check_is_never_prepared(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served, _target, worktree = _park_resolved_line_a_conflict(platform, origin)
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY'\n")
  from app import restart_util

  def fails(platform_root=None, **_kwargs):
    raise restart_util.RestartSourceInvalid("startup check failed")

  monkeypatch.setattr("app.restart_util.validate_restart_source", fails)

  with pytest.raises(pu.PlatformUpdateError, match="startup check failed"):
    pu.continue_platform_overlay_update(platform)

  assert pu.read_prepared_update() is None
  assert _served_sha(platform) == served
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'DIRTY'\n"
  assert worktree.exists() and pu.CONFLICT_FLAG.exists()


def test_a_failed_swap_keeps_the_live_checkout_and_the_prepared_update(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  dirty = platform / "backend/app/foo.py"
  dirty.write_text("VALUE = 'KEEP ME'\n")
  (platform / "backend/app/new_mod.py").write_text("NEW = True\n")
  original_git = pu._git
  failed = False

  def fail_first_candidate_checkout(*args, **kwargs):
    nonlocal failed
    if not failed and kwargs.get("repo") == platform and args[:3] == ("read-tree", "-m", "-u"):
      failed = True
      raise RuntimeError("candidate checkout failed after the branch moved")
    return original_git(*args, **kwargs)

  monkeypatch.setattr(pu, "_git", fail_first_candidate_checkout)
  assert pu.swap_in_prepared_update(cutover=True, repo=platform) is False
  monkeypatch.setattr(pu, "_git", original_git)

  assert _served_sha(platform) == served
  assert dirty.read_text() == "VALUE = 'KEEP ME'\n"
  assert (platform / "backend/app/new_mod.py").read_text() == "NEW = True\n"
  assert pu.read_prepared_update()["state"] == "prepared"
  # A fresh owner edit after the failure is not reset by the next boot.
  dirty.write_text("VALUE = 'FRESH AFTER FAILURE'\n")
  pu.boot_guard_clean_served_tree(platform)
  assert dirty.read_text() == "VALUE = 'FRESH AFTER FAILURE'\n"
  assert _finish_prepared(platform) == "replayed"
  assert dirty.read_text() == "VALUE = 'FRESH AFTER FAILURE'\n"
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()


def test_boot_recovers_the_live_state_after_a_swap_killed_midway(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  dirty = platform / "backend/app/foo.py"
  dirty.write_text("VALUE = 'KEEP ME'\n")
  original_git = pu._git

  class Killed(BaseException):
    pass

  def kill_after_branch_move(*args, **kwargs):
    if kwargs.get("repo") == platform and args[:3] == ("read-tree", "-m", "-u"):
      raise Killed()
    return original_git(*args, **kwargs)

  # A SIGKILL runs no cleanup: the branch has moved, the checkout has not.
  monkeypatch.setattr(pu, "_git", kill_after_branch_move)
  with pytest.raises(Killed):
    pu.swap_in_prepared_update(cutover=True, repo=platform)
  monkeypatch.setattr(pu, "_git", original_git)
  assert _served_sha(platform) != served

  pu.boot_guard_clean_served_tree(platform)

  assert _served_sha(platform) == served
  assert dirty.read_text() == "VALUE = 'KEEP ME'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]
  # The swap was recorded before the checkout moved; boot sees it never took
  # and returns the update to prepared rather than holding chats or losing
  # the live edits.
  assert pu.complete_platform_swap(platform) == "not_swapped"
  assert pu.read_prepared_update()["state"] == "prepared"
  assert not pu.late_edits_pending()
  assert dirty.read_text() == "VALUE = 'KEEP ME'\n"


def test_an_incomplete_swap_rollback_leaves_the_live_state_for_boot(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  dirty = platform / "backend/app/foo.py"
  dirty.write_text("VALUE = 'KEEP ME'\n")
  original_git = pu._git
  resets = 0

  def fail_activation_and_rollback(*args, **kwargs):
    nonlocal resets
    if kwargs.get("repo") == platform and args[:3] == ("read-tree", "-m", "-u"):
      resets += 1
      if resets == 1:
        dirty.write_text("VALUE = 'PARTIAL CHECKOUT'\n")
        raise RuntimeError("activation checkout failed")
      return SimpleNamespace(returncode=1, stdout="", stderr="checkout blocked")
    return original_git(*args, **kwargs)

  monkeypatch.setattr(pu, "_git", fail_activation_and_rollback)
  assert pu.swap_in_prepared_update(cutover=True, repo=platform) is False
  monkeypatch.setattr(pu, "_git", original_git)

  assert pu.RECONCILE_PRE_FLAG.exists()
  receipt = pu.boot_guard_clean_served_tree(platform)
  assert _served_sha(platform) == served
  assert dirty.read_text() == "VALUE = 'KEEP ME'\n"
  saved = receipt.split(" saved_work=", 1)[1].split()[0]
  assert _git(platform, "show", saved + ":backend/app/foo.py").stdout == "VALUE = 'PARTIAL CHECKOUT'\n"
  assert saved in pu._read_rolled_back_flag()["error"]


def test_a_concurrent_writer_keeps_the_branch_and_the_update_stays_prepared(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _served, target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  original = pu._activate_candidate
  raced: dict[str, str] = {}

  def concurrent_then_activate(repo, local, pre_sha, tip):
    raced["sha"] = _local_commit(
      platform, edits={"backend/app/raced.py": "RACED = True\n"},
      msg="concurrent writer",
    )
    original(repo, local, pre_sha, tip)

  monkeypatch.setattr(pu, "_activate_candidate", concurrent_then_activate)
  assert pu.swap_in_prepared_update(cutover=True, repo=platform) is False
  monkeypatch.setattr(pu, "_activate_candidate", original)

  # The newer writer keeps the branch; the update stays prepared.
  assert _served_sha(platform) == raced["sha"]
  assert pu.read_prepared_update()["state"] == "prepared"

  assert _finish_prepared(platform) == "replayed"
  assert (platform / "backend/app/raced.py").read_text() == "RACED = True\n"
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert pu._is_ancestor(platform, target, _served_sha(platform))


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
  assert _git(platform, "rev-parse", pu._PRE_UPDATE_REF).stdout.strip() == before
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

  def fail_after_concurrent_commit(repo=platform, timeout=pu._PROBE_TIMEOUT, **_kwargs):
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

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "replayed"
  assert "LINE_A = 'BOTH'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'BEFORE APPLY'\n"
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/foo.py",
  ]


def test_original_dirty_edit_conflicting_with_resolution_parks_again(clone_env):
  origin, platform = clone_env
  pre = _local_commit(platform, edits={
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'LOCAL'"),
  })
  (platform / "backend/app/foo.py").write_text("VALUE = 'BEFORE APPLY'\n")
  target = _advance_origin(origin, edits={
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

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _served_sha(platform) == pre
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'BEFORE APPLY'\n"
  # The in-progress edit is a late edit: it overlaps the update after boot.
  assert _finish_prepared(platform) == "conflict"
  again = pu._read_conflict_flag()["overlay"]
  assert again["stage"] == "working" and again["paths"] == ["backend/app/foo.py"]
  replay = Path(again["worktree"])

  (replay / "backend/app/foo.py").write_text("VALUE = 'UPSTREAM AND LOCAL'\n")
  _git(replay, "add", "backend/app/foo.py")
  assert pu.continue_platform_overlay_update(platform) == "updated"
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  assert "LINE_A = 'BOTH'" in (platform / "backend/app/main.py").read_text()
  assert (platform / "backend/app/foo.py").read_text() == (
    "VALUE = 'UPSTREAM AND LOCAL'\n"
  )
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
  assert res.overlay["stage"] == "committed"
  assert res.overlay["served"] == pre

  # A restart (boot reconcile) must not restart the merge under the resolver.
  again = pu.reconcile_clone(platform)
  assert again.status == "conflict"
  assert again.overlay == res.overlay

  # The owner keeps editing while the conflict is parked; continuing carries
  # the edit as it is now.
  (platform / "backend/app/foo.py").write_text("VALUE = 'DIRTY AGAIN'\n")
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "replayed"

  target = _git(platform, "rev-parse", "origin/main").stdout.strip()
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
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
  assert res.overlay["stage"] == "working"
  assert _git(platform, "rev-parse", pu._CONFLICT_RIGHT_REF).stdout.strip() == (
    res.overlay["right"]
  )
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


def test_dirty_resolution_merges_with_a_late_commit(clone_env):
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

  assert pu.continue_platform_overlay_update(platform) == "updated"
  assert _overlay_subjects(platform, target) == [
    "Reconcile local platform source with reviewed upstream",
  ]
  assert _git(platform, "show", "HEAD:backend/app/late.py").stdout == "LATE = True\n"
  assert "LINE_A = 'RESOLVED'" in (platform / "backend/app/main.py").read_text()
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    " M backend/app/main.py",
  ]


def test_boot_leaves_frontend_dependencies_alone_when_the_lock_did_not_change(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _served, _target, _worktree = _park_resolved_line_a_conflict(platform, origin)
  monkeypatch.setattr(
    pu, "_sync_frontend_dependencies",
    lambda repo: pytest.fail("the dependency lock did not change"),
  )
  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert _finish_prepared(platform) == "replayed"


def test_continue_refuses_new_python_dependencies_before_source_moves(
  clone_env,
):
  """An image without the boot transaction cannot hand new packages' source
  to the image that has them, so it prepares nothing."""
  origin, platform = clone_env
  pu.BOOT_TRANSACTION_MARKER.unlink()
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


def test_boot_installs_changed_frontend_dependencies_before_the_shell_rebuilds(
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
  installs = []
  monkeypatch.setattr(
    pu, "_sync_frontend_dependencies",
    lambda repo: installs.append(repo) or (True, ""),
  )
  res = pu.reconcile_clone(platform)
  worktree = Path(res.overlay["worktree"])
  (worktree / "backend/app/main.py").write_text(
    _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'BOTH'"),
  )
  _git(worktree, "add", "backend/app/main.py")
  stamp = platform / "frontend" / ".source-build-signature"
  stamp.parent.mkdir(parents=True, exist_ok=True)
  stamp.write_text("old build\n")

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  assert installs == []  # nothing installs until the update has booted
  assert _finish_prepared(platform) == "replayed"

  # The lock installs first; dropping the stamp makes the watcher's startup
  # check rebuild the shell against it.
  assert installs == [platform]
  assert not stamp.exists()


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
  assert _git(platform, "rev-parse", pu._PRE_UPDATE_REF).stdout.strip() == res.pre_sha
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
    repo_path, *, target_ref, fetch_remote, progress, candidate_target=None,
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

  async def fake_spawn(db, paths, target_sha, overlay=None):
    calls.append((db, paths, target_sha))
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
  assert calls == [(db, ["backend/app/main.py"], target)]
  flag = pu._read_conflict_flag()
  assert flag["upstream"] == target
  assert flag["paths"] == ["backend/app/main.py"]
  assert flag["chat_id"] == "resolver-chat"


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


def test_legacy_conflict_message_abandons_instead_of_hand_merging():
  target = "a" * 40

  content = pu._platform_conflict_resolver_message(
    target,
    ["backend/app/main.py", "frontend/src/App.jsx"],
  )

  assert target in content
  assert "backend/app/main.py, frontend/src/App.jsx" in content
  assert "abandon_platform_overlay_update" in content
  assert "merge --no-ff" not in content


def test_platform_conflict_resolver_message_points_at_the_parked_worktree():
  target = "a" * 40
  parked = {
    "mode": "net",
    "worktree": "/data/platform/.git/mobius-overlay-candidate",
    "served": "c" * 40, "stage": "committed",
    "paths": ["backend/app/main.py"],
  }

  content = pu._platform_conflict_resolver_message(
    target, ["backend/app/main.py", "backend/app/foo.py"], parked,
  )

  assert parked["worktree"] in content
  assert "continue_platform_overlay_update" in content
  assert "final local source" in content
  assert "all marked files together" in content
  assert "merge --no-ff" not in content
  assert "running platform is untouched" in content
  assert "finish this same update yourself" in content
  assert "update-preview?intent=finish" in content
  assert "/api/platform/rebuild" in content
  assert "without touching the live checkout" in content
  assert "merged back after it boots" in content
  assert "A plain restart never swaps in an update that needs a new image" in content


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


def test_local_image_changes_report_local_only_image_inputs(tmp_path, monkeypatch):
  marker = tmp_path / "activation.json"
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", marker)
  pu._write_activation_marker(
    "a" * 40,
    ["Dockerfile", "backend/app/main.py"],
    upstream_sha="b" * 40,
    image_paths=[],
  )

  assert pu.local_image_changes() == ["Dockerfile"]


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

  assert pu.local_image_changes() == []


def test_served_runtime_module_is_not_a_local_image_change(
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

  assert pu.local_image_changes() == ["Dockerfile"]


def test_image_owned_runtime_is_a_local_image_change(
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

  assert pu.local_image_changes() == [
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
  assert pu.local_image_changes(_served_sha(platform), platform) == []


def test_local_image_changes_report_image_owned_runtime_drift(
  clone_env,
):
  _, platform = clone_env
  official = _git(platform, "rev-parse", "HEAD").stdout.strip()
  _local_commit(
    platform,
    edits={"backend/runtime/restart_ledger.py": "local-only\n"},
  )

  assert pu.local_image_changes(official, platform) == [
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

  assert pu.local_image_changes(official, platform) == []


def test_local_image_changes_report_unmarked_image_input_drift(clone_env):
  """Direct local commits to image inputs never write an activation marker,
  yet the official image does not run them; the report must
  derive that drift from the histories themselves."""
  _, platform = clone_env
  official = _git(platform, "rev-parse", "HEAD").stdout.strip()

  assert pu.local_image_changes(official, platform) == []

  _local_commit(platform, edits={"Dockerfile": "FROM local-only\n"})
  assert pu.local_image_changes(official, platform) == [
    "Dockerfile",
  ]


def test_reviewed_image_target_does_not_treat_incoming_dockerfile_as_local(
  clone_env,
):
  origin, platform = clone_env
  current = _served_sha(platform)
  target = _advance_origin(origin, edits={"Dockerfile": "FROM official-new\n"})
  _git(platform, "fetch", "origin")

  assert pu.local_image_changes(
    target, platform, local_change_base=current,
  ) == []

  _local_commit(platform, edits={"Dockerfile": "FROM local-divergence\n"})
  assert pu.local_image_changes(
    target, platform, local_change_base=current,
  ) == ["Dockerfile"]


def test_local_image_change_already_in_a_further_changed_target_is_not_reported(
  clone_env,
):
  """A local fix the release already contains, plus more release edits to the
  same file, loses nothing on replacement; only the merged file decides."""
  origin, platform = clone_env
  base = _advance_origin(
    origin, edits={"Dockerfile": "FROM base\nRUN one\n\n\n\nRUN two\n"},
    msg="multi-line base",
  )
  _git(platform, "fetch", "origin")
  _git(platform, "merge", "--ff-only", base)
  _local_commit(
    platform, edits={"Dockerfile": "FROM base\nRUN one-fixed\n\n\n\nRUN two\n"},
  )
  target = _advance_origin(
    origin,
    edits={"Dockerfile": "FROM base\nRUN one-fixed\n\n\n\nRUN two-newer\n"},
    msg="release carries the fix and more",
  )
  _git(platform, "fetch", "origin")

  assert pu.local_image_changes(
    target, platform, local_change_base=base,
  ) == []

  _local_commit(
    platform, edits={"Dockerfile": "FROM base\nRUN one-local-only\n\n\n\nRUN two\n"},
  )
  assert pu.local_image_changes(
    target, platform, local_change_base=base,
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


def test_local_image_changes_report_input_renamed_out_of_its_owned_path(
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

  assert pu.local_image_changes(official, platform) == [
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
  assert pu.local_image_changes(official, platform) == [
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

  assert pu.local_image_changes(
    target, platform, local_change_base=applied,
  ) == []

  _local_commit(platform, edits={"Dockerfile": "FROM local-only\n"})
  assert pu.local_image_changes(
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
  assert pu.local_image_changes() == ["Dockerfile"]
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


def test_status_owes_no_image_for_a_local_edit_to_an_image_input(
  clone_env, monkeypatch,
):
  """Only official image inputs the running image lacks are owed.

  The image runs the official release's boot script. A local edit to it stays
  in the checkout, but no official replacement can run it, so it must not
  raise an image-rebuild prompt that a replacement could never clear.
  """
  _, platform = clone_env
  path = "backend/scripts/init_chat_summaries.py"
  baked = "running image script\n"
  official = _local_commit(platform, edits={path: baked}, msg="official seed")
  _git(platform, "branch", "-f", "upstream", official)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(official + "\n")
  monkeypatch.setattr(pu, "_build_info", lambda: {
    "image_inputs": {path: hashlib.sha256(baked.encode()).hexdigest()},
  })

  _local_commit(platform, edits={path: "local customization\n"})
  status = pu.platform_status(platform)

  assert status["needs_restart"] is False
  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
  assert status["activation"]["level"] == "live"

  # An official change the image lacks is still owed.
  _git(platform, "branch", "-f", "upstream", _local_commit(
    platform, edits={path: "newer official script\n"}, msg="official change",
  ))
  status = pu.platform_status(platform)
  assert status["state"] == pu.PlatformUpdateState.ACTIVATION_NEEDED.value
  assert status["activation"]["level"] == "image_rebuild"


def test_status_owes_the_image_of_a_contained_release_past_a_stale_marker(
  clone_env, monkeypatch,
):
  """The image runs release A, the recorded upstream still says A, but the
  checkout contains official release B, which changes an image input. B's
  image is owed: a stale marker must not excuse it."""
  origin, platform = clone_env
  path = "backend/scripts/init_chat_summaries.py"
  release_a = _advance_origin(origin, edits={path: "release a\n"})
  pu._fetch(platform)
  _git(platform, "merge", "-q", "--ff-only", release_a)
  _git(platform, "branch", "-f", "upstream", release_a)
  monkeypatch.setattr(pu, "_build_info", lambda: {
    "image_inputs": {path: hashlib.sha256(b"release a\n").hexdigest()},
  })
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(release_a + "\n")
  assert pu.platform_status(platform)["activation"]["level"] == "live"

  release_b = _advance_origin(origin, edits={path: "release b\n"})
  pu._fetch(platform)
  _git(platform, "merge", "-q", "--ff-only", release_b)
  pu.SERVING_SHA_FILE.write_text(release_b + "\n")
  assert pu.recorded_upstream_sha(platform) == release_a

  status = pu.platform_status(platform)
  assert status["activation"]["level"] == "image_rebuild"
  finish = pu.platform_update_preview(platform, target_sha=release_b)
  assert finish["operation"] == "finish"
  assert "image_rebuild" in finish["activation"]["required_actions"]

  # A newer release fetched but not installed does not hide B's image either.
  _advance_origin(origin, edits={"release-c.txt": "c\n"})
  pu._fetch(platform)
  assert pu._contained_official_source(platform) == release_b
  assert pu.platform_status(platform)["activation"]["level"] == "image_rebuild"
  # Finish cannot offer release A's image for source that holds B; the owner
  # is sent to the update that installs a release containing B.
  with pytest.raises(pu.PlatformUpdateError, match="applied_release_unavailable"):
    pu.applied_release_sha(platform)


def test_damaged_deployed_runtime_stays_owed_even_when_the_release_matches(
  clone_env, monkeypatch, tmp_path,
):
  """Filtering local customizations never hides a deployed protected module
  that differs from what the image itself recorded."""
  _, platform = clone_env
  path = "backend/runtime/restart_ledger.py"
  official = _local_commit(platform, edits={path: "image\n"})
  _git(platform, "branch", "-f", "upstream", official)
  monkeypatch.setattr(pu, "_build_info", lambda: {
    "image_inputs": {path: hashlib.sha256(b"image\n").hexdigest()},
  })
  deployed = tmp_path / "deployed-runtime"
  deployed.mkdir()
  monkeypatch.setenv("MOBIUS_PROTECTED_RUNTIME_DIR", str(deployed))
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(official + "\n")

  # A local edit with the image's own module deployed is a customization.
  (deployed / "restart_ledger.py").write_text("image\n", encoding="utf-8")
  _local_commit(platform, edits={path: "local\n"})
  pu.SERVING_SHA_FILE.write_text(_served_sha(platform) + "\n")
  assert pu.platform_status(platform)["activation"]["level"] == "live"

  # The same checkout with a damaged deployed module still owes the image.
  (deployed / "restart_ledger.py").write_text("damaged\n", encoding="utf-8")
  status = pu.platform_status(platform)
  assert status["activation"]["level"] == "image_rebuild"
  assert status["activation"]["reasons"][0]["paths"] == [path]

  # A link to the image's bytes is not the image's module, and a named pipe
  # is never opened for reading (it would block status forever).
  genuine = tmp_path / "genuine.py"
  genuine.write_text("image\n", encoding="utf-8")
  (deployed / "restart_ledger.py").unlink()
  (deployed / "restart_ledger.py").symlink_to(genuine)
  assert pu.platform_status(platform)["activation"]["level"] == "image_rebuild"
  (deployed / "restart_ledger.py").unlink()
  os.mkfifo(deployed / "restart_ledger.py")

  def stalled(_signum, _frame):
    raise AssertionError("status blocked opening a deployed named pipe")

  previous = signal.signal(signal.SIGALRM, stalled)
  signal.alarm(5)
  try:
    status = pu.platform_status(platform)
  finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)
  assert status["activation"]["level"] == "image_rebuild"


def test_source_only_update_keeping_a_local_dockerfile_needs_no_image(
  clone_env, monkeypatch,
):
  """An agent-finished update that keeps a local Dockerfile customization
  and changes no official image input is a restart, not a replacement."""
  origin, platform = clone_env
  official = _advance_origin(origin, edits={"Dockerfile": "FROM official\n"})
  pu._fetch(platform)
  _git(platform, "merge", "-q", "--ff-only", official)
  _git(platform, "branch", "-f", "upstream", official)
  monkeypatch.setattr(pu, "_build_info", lambda: {
    "image_inputs": {"Dockerfile": hashlib.sha256(b"FROM official\n").hexdigest()},
  })
  _local_commit(platform, edits={"Dockerfile": "FROM official\nRUN local\n"})
  current = _served_sha(platform)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  pu.SERVING_SHA_FILE.write_text(current + "\n")
  target = _advance_origin(origin, edits={"release.txt": "reviewed\n"})
  pu._fetch(platform)
  plan = _apply_plan(current, target, platform)
  plan.pop("repo")

  pu.park_update_for_agent(**plan, repo=platform)
  assert pu.continue_platform_overlay_update(platform) == "prepared"

  prepared = pu.read_prepared_update()
  assert prepared["requires_image"] is False
  assert pu._git_blob(platform, prepared["prepared"], "Dockerfile") == (
    b"FROM official\nRUN local\n"
  )


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
  )
  assert pu._read_conflict_flag() == {
    "upstream": "tgt-sha", "chat_id": "chat-42", "overlay": None,
    "paths": ["backend/app/a.py", "backend/app/b.py"],
  }
  # An older updater's semantic-base line is never mistaken for a path.
  pu.CONFLICT_FLAG.write_text("tgt-sha\nbase:old-tree\nbackend/app/a.py")
  legacy = pu._read_conflict_flag()
  assert legacy["chat_id"] is None
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

  assert preview["activation"]["level"] == "image_rebuild"


_IMAGE_LOCK = (
  "package-a==1 \\\n"
  "    --hash=sha256:linuxwheel \\\n"
  "    --hash=sha256:windowswheelold\n"
  "    # via -r requirements.txt\n"
)
_REPLACED_HASH_TARGET_LOCK = _IMAGE_LOCK.replace("windowswheelold", "windowswheelnew")
_ADDED_HASH_TARGET_LOCK = _IMAGE_LOCK.replace(
  "windowswheelold\n", "windowswheelold \\\n    --hash=sha256:macwheel\n",
)
_DROPPED_HASH_TARGET_LOCK = _IMAGE_LOCK.replace(
  " \\\n    --hash=sha256:windowswheelold", "",
)


def _lock_only_update(clone_env, target_lock: str):
  """A release that changes only the lock, on an image built from ``base``."""
  origin, platform = clone_env
  base = _advance_origin(origin, edits={
    "backend/requirements.txt": "package-a==1\n",
    "backend/requirements.lock": _IMAGE_LOCK,
  })
  _git(platform, "fetch", "origin")
  _git(platform, "reset", "--hard", base)
  _git(platform, "branch", "-f", "upstream", base)
  target = _advance_origin(origin, edits={"backend/requirements.lock": target_lock})
  pu._fetch(platform)
  image_inputs = {
    path: hashlib.sha256(
      _git(platform, "show", f"{base}:{path}").stdout.encode(),
    ).hexdigest()
    for path in pu._PYTHON_DEPENDENCY_INPUTS
  }
  return platform, base, target, image_inputs


@pytest.mark.parametrize("case", ["unproven_image_lock", "no_build_sha"])
def test_hash_only_lock_proof_fails_closed(clone_env, monkeypatch, case):
  platform, base, target, image_inputs = _lock_only_update(
    clone_env, _ADDED_HASH_TARGET_LOCK,
  )
  build_info = {"sha": base, "image_inputs": image_inputs}
  if case == "unproven_image_lock":
    # The named build commit's lock is not the one the image recorded.
    build_info["sha"] = target
  elif case == "no_build_sha":
    build_info.pop("sha")
  monkeypatch.setattr(pu, "_build_info", lambda: build_info)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert any(
    reason["code"] == "python_dependencies"
    for reason in preview["incoming_activation"]["reasons"]
  )


def test_hash_only_lock_update_needs_no_image_from_preview_to_boot(
  clone_env, monkeypatch,
):
  """Preview, prepare, the boot guard and drift give one answer for a lock
  that only adds artifact hashes to what the running image installed."""
  platform, base, target, image_inputs = _lock_only_update(
    clone_env, _ADDED_HASH_TARGET_LOCK,
  )
  monkeypatch.setattr(
    pu, "_build_info", lambda: {"sha": base, "image_inputs": image_inputs},
  )

  preview = pu.platform_update_preview(platform, target_sha=target)
  assert preview["activation"]["level"] == "live"
  plan = _apply_plan(preview["current_sha"], target, platform)
  plan.pop("repo")
  record = pu.prepare_reviewed_update(**plan, repo=platform)
  assert record["requires_image"] is False

  # The outgoing server swaps it in and the same image boots the new source.
  assert pu.swap_in_prepared_update(cutover=True, repo=platform) is True
  pu.settle_prepared_update_for_this_image(platform)
  assert pu.recorded_upstream_sha(platform) == target
  assert pu.release_packages_missing_from_image(platform) is None
  assert pu.image_input_drift(platform) == []


@pytest.mark.parametrize("target_lock", [
  _DROPPED_HASH_TARGET_LOCK,
  _REPLACED_HASH_TARGET_LOCK,  # a replaced hash drops the one the image accepted
  _IMAGE_LOCK.replace("package-a==1", "package-a==2"),
], ids=["dropped_hash", "replaced_hash", "version"])
def test_lock_update_the_image_may_not_install_still_needs_an_image(
  clone_env, monkeypatch, target_lock,
):
  platform, base, target, image_inputs = _lock_only_update(clone_env, target_lock)
  monkeypatch.setattr(
    pu, "_build_info", lambda: {"sha": base, "image_inputs": image_inputs},
  )

  preview = pu.platform_update_preview(platform, target_sha=target)
  assert preview["activation"]["level"] == "image_rebuild"
  plan = _apply_plan(preview["current_sha"], target, platform)
  plan.pop("repo")
  assert pu.prepare_reviewed_update(**plan, repo=platform)["requires_image"] is True

  # Served on the old image anyway, the release is refused and reported.
  _git(platform, "merge", "-q", "--ff-only", target)
  pu._set_upstream(platform, target)
  assert "newer release" in pu.release_packages_missing_from_image(platform)
  assert pu.image_input_drift(platform) == ["backend/requirements.lock"]


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


def test_update_preview_predicts_the_apply_conflict_without_touching_the_checkout(
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

  # Review names the conflict Apply would park, so the update can go straight
  # to an agent, yet stays read-only: no replay, no parked resolver.
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


def test_finish_targets_the_installed_release_when_the_tracking_ref_lags(clone_env):
  origin, platform = clone_env
  older = _served_sha(platform)
  installed = _advance_origin(origin, edits={"release.txt": "installed release\n"})
  assert pu.reconcile_clone(platform).status == "updated"
  # A reviewed Apply can install an exact target fetched outside the tracking
  # ref, so origin/main may still name the older release afterwards.
  _git(platform, "update-ref", "refs/remotes/origin/main", older)

  assert pu.applied_release_sha(platform) == installed
  status = pu.platform_status(platform)
  assert status["available"] is False
  assert status["contained_upstream_sha"] == installed
  preview = pu.platform_update_preview(platform)
  assert preview["target_sha"] == installed
  pu.check_for_updates(platform)
  assert pu.recorded_upstream_sha(platform) == installed


def test_finish_refuses_a_branch_reset_below_the_installed_release(clone_env):
  origin, platform = clone_env
  older = _served_sha(platform)
  _advance_origin(origin, edits={"release.txt": "installed release\n"})
  assert pu.reconcile_clone(platform).status == "updated"
  _git(platform, "update-ref", "refs/remotes/origin/main", older)
  _git(platform, "reset", "-q", "--hard", older)

  # Finish must never target a release the served source does not contain,
  # even though the lagging tracking ref names one it does.
  with pytest.raises(pu.PlatformUpdateError, match="applied_release_unavailable"):
    pu.applied_release_sha(platform)


def test_a_parked_update_is_the_only_one_until_it_finishes(clone_env):
  origin, platform = clone_env
  _local_commit(platform, edits={"backend/scripts/init_chat_summaries.py": "local\n"})
  current = _served_sha(platform)
  pinned = _advance_origin(origin, edits={"release.txt": "pinned\n"})
  pu._fetch(platform)
  plan = _apply_plan(current, pinned, platform)
  plan.pop("repo")

  assert pu.park_update_for_agent(**plan, repo=platform) == {
    "target_sha": pinned, "stage": "resolve", "action": "replace",
    "cancellable": True,
  }
  assert pu.platform_status(platform)["unfinished_update"]["target_sha"] == pinned

  newer = _advance_origin(origin, edits={"release.txt": "newer\n"})
  pu._fetch(platform)
  with pytest.raises(pu.PlatformUpdateError, match="finish_update_first"):
    pu._validate_update_plan(
      platform, plan_id=pu._update_plan_id(current, newer, None),
      current_sha=current, target_sha=newer,
    )
  assert pu.abandon_platform_overlay_update(platform) == "abandoned"
  assert pu.unfinished_update(platform) is None


def test_only_an_owed_container_replacement_blocks_newer_updates(clone_env):
  origin, platform = clone_env
  script = "backend/scripts/init_chat_summaries.py"
  target = _advance_origin(origin, edits={
    script: "release boot\n",
    "backend/app/main.py": _MAIN_PY.replace("LINE_A = 1", "LINE_A = 300"),
  })
  assert pu.reconcile_clone(platform).status == "updated"

  # A pending restart alone never blocks: several updates may share one.
  pu.mark_activation_needed(
    _served_sha(platform), ["backend/app/main.py"], upstream_sha=target,
    repo=platform,
  )
  assert pu.unfinished_update(platform) is None

  pu.mark_activation_needed(
    _served_sha(platform), [script], upstream_sha=target, repo=platform,
  )
  assert pu.unfinished_update(platform) == {
    "target_sha": target, "stage": "finish", "action": "replace",
    "cancellable": False,
  }


def test_local_image_edits_survive_a_parked_update_and_late_edits_return(
  clone_env,
):
  """A local image-owned edit is kept, not reverted: the update finishes
  around it and never asks the agent to discard it."""
  origin, platform = clone_env
  script = "backend/scripts/init_chat_summaries.py"
  _local_commit(platform, edits={script: "local boot tweak\n"})
  current = _served_sha(platform)
  target = _advance_origin(origin, edits={
    "release.txt": "reviewed\n", "Dockerfile": "FROM official-new\n",
  })
  pu._fetch(platform)
  plan = _apply_plan(current, target, platform)
  plan.pop("repo")

  pending = pu.park_update_for_agent(**plan, repo=platform)

  assert pending["stage"] == "resolve"
  parked = pu._read_conflict_flag()["overlay"]
  assert "blockers" not in parked
  content = pu._platform_conflict_resolver_message(target, [], parked)
  assert script not in content and "git checkout" not in content

  # Another chat keeps working on the live checkout meanwhile.
  _local_commit(platform, edits={"notes.txt": "late live edit\n"})

  assert pu.continue_platform_overlay_update(platform) == "prepared"
  # Nothing reached the live checkout: it still serves the local boot tweak.
  assert (platform / script).read_text() == "local boot tweak\n"
  assert not (platform / "release.txt").exists()
  assert pu.unfinished_update(platform)["stage"] == "finish"

  # A restart does not swap in an update that needs a new container.
  assert pu.read_prepared_update()["requires_image"] is True
  assert pu.swap_in_prepared_update(cutover=False, repo=platform) is False

  assert _finish_prepared(platform) == "replayed"
  assert (platform / "release.txt").read_text() == "reviewed\n"
  assert (platform / "notes.txt").read_text() == "late live edit\n"
  assert (platform / script).read_text() == "local boot tweak\n"
  assert pu._is_ancestor(platform, target, _served_sha(platform))


def test_finish_of_already_applied_source_still_requires_the_owed_image(
  clone_env,
):
  """Source that already contains the release has no incoming changes, but
  the running image still owes that release's image work; Finish must keep
  requiring the replacement instead of refusing it."""
  origin, platform = clone_env
  target = _advance_origin(origin, edits={"Dockerfile": "FROM official-new\n"})
  pu._fetch(platform)
  _git(platform, "merge", "--ff-only", target)
  pu._write_activation_marker(
    target, ["Dockerfile"], upstream_sha=target, image_paths=["Dockerfile"],
  )
  current = _served_sha(platform)
  plan = _apply_plan(current, target, platform)
  plan.pop("repo")

  prepared = pu.prepare_reviewed_update(**plan, repo=platform)

  assert prepared["requires_image"] is True


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
def test_review_reports_boot_script_customization_without_mutation(
  clone_env, monkeypatch, deployment,
):
  origin, platform = clone_env
  monkeypatch.setattr(platform_activation, "deployment_kind", lambda: deployment)
  paths = ["backend/scripts/init_agent_context.py", "backend/scripts/init_chat_summaries.py"]
  _local_commit(platform, edits={path: "local instructions\n" for path in paths})
  target = _advance_origin(origin, edits={"Dockerfile": "FROM official-new\n"})
  pu._fetch(platform)
  before = _served_sha(platform)
  dirty = platform / "keep-working.txt"
  dirty.write_text("unfinished owner work")
  before_status = _git(platform, "status", "--porcelain").stdout

  preview = pu.platform_update_preview(platform, target_sha=target)
  pu.reviewed_container_rebuild_plan(
    repo=platform, plan_id=preview["plan_id"], current_sha=before,
    target_sha=target, image_digest=None,
  )

  assert preview["local_image_paths"] == paths
  assert preview["actionable"] is True
  assert preview["activation"]["deployment"] == deployment
  assert _served_sha(platform) == before
  assert _git(platform, "status", "--porcelain").stdout == before_status
  assert dirty.read_text() == "unfinished owner work"
  assert all((platform / path).read_text() == "local instructions\n" for path in paths)


def test_review_does_not_report_image_changes_already_in_the_official_release(clone_env):
  origin, platform = clone_env
  path = "backend/scripts/init_chat_summaries.py"
  _local_commit(platform, edits={path: "same useful instructions\n"})
  target = _advance_origin(origin, edits={path: "same useful instructions\n"})
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert preview["activation"]["level"] == "image_rebuild"
  assert preview["local_image_paths"] == []


def test_review_reports_an_uncommitted_image_input_edit(clone_env):
  origin, platform = clone_env
  dockerfile = platform / "Dockerfile"
  dockerfile.write_text("FROM local-owner-image\n")
  target = _advance_origin(
    origin,
    edits={"Dockerfile": "FROM reviewed-official-image\n"},
  )
  pu._fetch(platform)

  preview = pu.platform_update_preview(platform, target_sha=target)

  assert preview["local_image_paths"] == ["Dockerfile"]


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

  assert preview["local_image_paths"] == ["Dockerfile"]
  assert "do-not-expose" not in json.dumps(preview)


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

  assert preview["local_image_paths"] == ["Dockerfile"]


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

  assert preview["local_image_paths"] == ["Dockerfile"]


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

  assert path in preview["local_image_paths"]
  assert "outside-secret" not in json.dumps(preview)


def test_finish_review_reports_local_image_changes_too(clone_env):
  _, platform = clone_env
  official = _served_sha(platform)
  path = "backend/scripts/init_chat_summaries.py"
  _local_commit(platform, edits={path: "preserve me\n"})
  pu.mark_activation_needed(_served_sha(platform), [path], upstream_sha=official, repo=platform)

  preview = pu.platform_update_preview(platform, target_sha=official)

  assert preview["operation"] == "finish"
  assert preview["local_image_paths"] == [path]


# --- An update that needs a new image is activated by that image's boot -----


def _prepare_package_update(platform: Path, origin: Path) -> dict:
  """Prepare a reviewed release that changes Python packages, with a late
  commit and an in-progress edit made on the old source afterwards."""
  base = _served_sha(platform)
  _local_commit(platform, edits={"backend/app/local.py": "LOCAL = 1\n"})
  target = _advance_origin(origin, edits={
    "backend/requirements.lock": "new-package==1\n",
    "backend/app/uses_new_package.py": "import new_package\n",
  })
  pu._fetch(platform)
  current = _served_sha(platform)
  plan = _apply_plan(current, target, platform)
  plan.pop("repo")
  record = pu.prepare_reviewed_update(**plan, repo=platform)
  assert record["state"] == "prepared" and record["requires_image"] is True
  assert _served_sha(platform) == current != base
  _local_commit(platform, edits={"backend/app/late.py": "LATE = 1\n"})
  (platform / "backend/app/local.py").write_text("LOCAL = 'in progress'\n")
  return record


def _assert_update_active_with_late_work(platform: Path, target: str) -> None:
  assert pu._is_ancestor(platform, target, _served_sha(platform))
  assert (platform / "backend/requirements.lock").read_text() == "new-package==1\n"
  assert (platform / "backend/app/late.py").read_text() == "LATE = 1\n"
  # The in-progress edit returns uncommitted, exactly as it was left.
  assert (platform / "backend/app/local.py").read_text() == "LOCAL = 'in progress'\n"
  assert "backend/app/local.py" in _git(platform, "status", "--porcelain").stdout


def test_the_outgoing_server_never_swaps_in_an_update_that_needs_a_new_image(
  clone_env,
):
  origin, platform = clone_env
  _prepare_package_update(platform, origin)
  before = _served_sha(platform)

  assert pu.swap_in_prepared_update(cutover=True, repo=platform) is False

  assert _served_sha(platform) == before
  assert pu.read_prepared_update()["state"] == "prepared"


def test_an_image_without_the_boot_transaction_still_swaps_at_its_cutover(
  clone_env,
):
  """Images from before the boot transaction keep their own contract: they
  swap an image update in at the cutover, and could not prepare this one."""
  origin, platform = clone_env
  target = _advance_origin(origin, edits={"Dockerfile": "FROM official-new\n"})
  pu._fetch(platform)
  plan = _apply_plan(_served_sha(platform), target, platform)
  plan.pop("repo")
  pu.BOOT_TRANSACTION_MARKER.unlink()
  assert pu.prepare_reviewed_update(**plan, repo=platform)["requires_image"]

  assert pu.swap_in_prepared_update(cutover=True, repo=platform) is True


def test_only_the_update_s_own_image_swaps_it_in_at_boot(clone_env):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  before = _served_sha(platform)

  _boot_image("0" * 40)  # the old image restarting
  assert pu.settle_prepared_update_for_this_image(platform) == "waiting"
  assert _served_sha(platform) == before

  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  _assert_update_active_with_late_work(platform, record["target"])
  # Repeating the boot changes nothing.
  replayed = pu.read_prepared_update()["replayed"]
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  assert pu.read_prepared_update()["replayed"] == replayed == _served_sha(platform)


class _Killed(BaseException):
  pass


def _kill_once(monkeypatch, name: str, *, after: bool) -> None:
  """Kill the boot the first time ``name`` runs, before or after its effect."""
  real = getattr(pu, name)
  calls = []

  def killed(*args, **kwargs):
    if calls:
      return real(*args, **kwargs)
    calls.append(name)
    if after:
      real(*args, **kwargs)
    raise _Killed(name)

  monkeypatch.setattr(pu, name, killed)


@pytest.mark.parametrize(("name", "after"), [
  ("_carry_working_edits", True),  # edits carried, record still prepared
  ("_write_prepared_update", True),  # record swapped, branch not moved
  ("_activate_candidate", True),  # branch moved, reconcile marker still set
  ("_finish_swap", False),  # branch moved, bookkeeping not done
  ("_set_upstream", False),  # bookkeeping half done
  ("_merged_candidate", False),  # merge-back not started
  ("_clear_reconcile_pre", False),  # merge-back moved, marker still set
])
def test_a_boot_killed_at_any_point_of_the_swap_finishes_on_the_next_boot(
  clone_env, monkeypatch, name, after,
):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  _kill_once(monkeypatch, name, after=after)

  with pytest.raises(_Killed):
    pu.settle_prepared_update_for_this_image(platform)

  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  _assert_update_active_with_late_work(platform, record["target"])
  assert pu.recorded_upstream_sha(platform) == record["target"]
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_an_image_that_is_not_kept_returns_to_the_saved_state_and_keeps_new_work(
  clone_env,
):
  """The controller rolled the image back after the update booted on it: the
  old image restores its own source and bookkeeping, and nothing made on the
  update since is lost."""
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  upstream_before = pu.recorded_upstream_sha(platform)
  # Image work the previous version still owes survives every boot.
  pu._write_activation_marker(
    "a" * 40, ["Dockerfile"], upstream_sha="a" * 40, image_paths=["Dockerfile"],
  )
  activation_before = pu.RESTART_NEEDED_FLAG.read_text()
  late_tree = (platform / "backend/app/late.py").read_text()
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  # An agent resumes on the new version and keeps working.
  _local_commit(platform, edits={"backend/app/new_work.py": "NEW = 1\n"})
  (platform / "backend/app/draft.py").write_text("DRAFT = 1\n")

  _boot_image(record["snapshot"])  # the image it replaced
  assert pu.settle_prepared_update_for_this_image(platform) == "reverted"

  assert (platform / "backend/app/late.py").read_text() == late_tree
  assert not (platform / "backend/app/uses_new_package.py").exists()
  assert (platform / "backend/app/local.py").read_text() == "LOCAL = 'in progress'\n"
  assert pu.recorded_upstream_sha(platform) == upstream_before
  assert pu.RESTART_NEEDED_FLAG.read_text() == activation_before
  reverted = pu.read_prepared_update()
  assert reverted["state"] == "prepared" and reverted["operation"] is None
  refs = _git(
    platform, "for-each-ref", "--format=%(refname)", pu._SET_ASIDE_PREFIX,
  ).stdout.split()
  assert len(refs) == 1
  kept = _git(platform, "show", f"{refs[0]}:backend/app/draft.py").stdout
  assert kept == "DRAFT = 1\n"
  assert _git(platform, "show", f"{refs[0]}:backend/app/new_work.py").stdout == "NEW = 1\n"
  assert refs[0] in pu._read_rolled_back_flag()["error"]


def test_a_reverted_image_update_with_nothing_new_sets_nothing_aside(clone_env):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  pu.settle_prepared_update_for_this_image(platform)

  assert pu.revert_failed_update(platform)

  assert not _git(
    platform, "for-each-ref", pu._SET_ASIDE_PREFIX,
  ).stdout.strip()


def test_set_aside_work_is_not_pruned_after_five_reverts(clone_env):
  """A later rollback must never delete an earlier recovery point."""
  _origin, platform = clone_env
  saved = {}
  for number in range(7):
    commit = _local_commit(
      platform, edits={"backend/app/draft.py": f"revision = {number}\n"},
    )
    saved[pu._keep_set_aside(platform, commit)] = commit

  assert len(saved) == 7
  assert {
    ref: _git(platform, "rev-parse", ref).stdout.strip()
    for ref in _git(
      platform, "for-each-ref", "--format=%(refname)", pu._SET_ASIDE_PREFIX,
    ).stdout.split()
  } == saved


def test_a_boot_refuses_a_checkout_it_cannot_place_for_any_update(clone_env):
  """Restart-only or not, an unexplained checkout under a live swap is never
  served: the boot cannot tell which source it would be running."""
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  pu.settle_prepared_update_for_this_image(platform)
  _git(platform, "reset", "-q", "--hard", record["snapshot"])
  _local_commit(platform, edits={"elsewhere.txt": "unrelated\n"})

  with pytest.raises(pu.BootTransactionError):
    pu.settle_prepared_update_for_this_image(platform)


def test_a_record_from_a_newer_boot_protocol_is_refused(clone_env):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  pu._write_prepared_update({**record, "protocol": pu.BOOT_PROTOCOL + 1})
  _boot_image(record["target"])

  with pytest.raises(pu.BootTransactionError, match="protocol"):
    pu.settle_prepared_update_for_this_image(platform)


def test_only_this_boot_s_transaction_certifies_image_updates(clone_env):
  pu.BOOT_TRANSACTION_MARKER.write_text("0\n")
  assert not pu.image_activates_updates()
  pu.BOOT_TRANSACTION_MARKER.unlink()
  assert not pu.image_activates_updates()
  pu.BOOT_TRANSACTION_MARKER.write_text(f"{pu.BOOT_PROTOCOL}\n")
  assert pu.image_activates_updates()


def _activate_bound(platform: Path, origin: Path, operation: dict) -> dict:
  record = _prepare_package_update(platform, origin)
  pu.bind_update_operation(record["target"], operation, repo=platform)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  _boot_started_server(platform)
  return pu.read_prepared_update()


def _succeeded(record: dict, **overrides) -> dict:
  status = {
    "state": "succeeded", "expected_sha": record["target"],
    "operation_id": record["operation"]["id"], "image_digest": None,
    "request_nonce": None,
  }
  return {**status, **overrides}


def test_only_the_bound_replacement_s_success_retires_an_image_update(clone_env):
  origin, platform = clone_env
  record = _activate_bound(
    platform, origin, {"controller": "railway", "id": "replace_1"},
  )

  # The started server does not confirm it: the controller can still roll back.
  assert not pu.confirm_platform_swap_loaded(platform)
  pending = pu.unfinished_update(platform)
  assert pending["stage"] == "settling" and not pending["cancellable"]
  assert not pu.late_edits_pending()  # resumes are not held meanwhile

  for stale in (
    _succeeded(record, operation_id="replace_0"),  # an earlier attempt
    _succeeded(record, expected_sha="b" * 40),
    _succeeded(record, state="verifying"),
  ):
    assert pu.reconcile_bound_operation(stale, platform) is None
  _boot_started_server(platform, source="baked")
  assert pu.reconcile_bound_operation(_succeeded(record), platform) is None
  _boot_image("c" * 40)
  _boot_started_server(platform)
  assert pu.reconcile_bound_operation(_succeeded(record), platform) is None
  assert pu.read_prepared_update() == record

  _boot_image(record["target"])
  assert pu.reconcile_bound_operation(_succeeded(record), platform) == "retired"
  assert pu.read_prepared_update() is None
  assert pu.unfinished_update(platform) is None


def test_a_host_replacement_is_identified_by_the_app_s_nonce(clone_env):
  origin, platform = clone_env
  nonce = "f" * 32
  record = _activate_bound(platform, origin, {"controller": "host", "id": nonce})

  assert pu.reconcile_bound_operation(
    _succeeded(record, operation_id="x", request_nonce="e" * 32), platform,
  ) is None
  assert pu.reconcile_bound_operation(
    _succeeded(record, operation_id="x", request_nonce=nonce), platform,
  ) == "retired"


def test_a_railway_success_for_another_image_digest_does_not_retire(clone_env):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  pu._write_prepared_update({**record, "image_digest": "sha256:" + "1" * 64})
  pu.bind_update_operation(
    record["target"], {"controller": "railway", "id": "replace_1"}, repo=platform,
  )
  _boot_image(record["target"])
  pu.settle_prepared_update_for_this_image(platform)
  _boot_started_server(platform)
  record = pu.read_prepared_update()

  assert pu.reconcile_bound_operation(
    _succeeded(record, image_digest="sha256:" + "2" * 64), platform,
  ) is None
  assert pu.reconcile_bound_operation(
    _succeeded(record, image_digest="sha256:" + "1" * 64), platform,
  ) == "retired"


@pytest.mark.parametrize("state", ["failed", "no_change", "rolled_back"])
def test_a_replacement_that_never_booted_the_target_releases_its_binding(
  clone_env, state,
):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  operation = {"controller": "railway", "id": "replace_1"}
  pu.bind_update_operation(record["target"], operation, repo=platform)
  status = {
    "state": state, "expected_sha": record["target"],
    "operation_id": "replace_1", "image_digest": None,
  }

  assert pu.reconcile_bound_operation(status, platform) == "unbound"
  assert pu.read_prepared_update()["operation"] is None
  assert pu.unfinished_update(platform)["stage"] == "finish"


def test_only_the_exact_binding_is_released_after_a_definitive_failure(clone_env):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  bound = {"controller": "railway", "id": "replace_2"}
  pu.bind_update_operation(record["target"], bound, repo=platform)

  pu.unbind_update_operation(
    record["target"], {"controller": "railway", "id": "replace_1"}, repo=platform,
  )
  assert pu.read_prepared_update()["operation"] == bound
  pu.unbind_update_operation(record["target"], bound, repo=platform)
  assert pu.read_prepared_update()["operation"] is None


def test_the_owner_may_keep_a_settling_update_only_on_its_own_image(clone_env):
  origin, platform = clone_env
  record = _activate_bound(
    platform, origin, {"controller": "railway", "id": "replace_1"},
  )
  _boot_started_server(platform, source="baked")
  with pytest.raises(pu.PlatformUpdateError, match="update_not_settling"):
    pu.keep_settling_update(platform)

  _boot_started_server(platform)
  pu.keep_settling_update(platform)
  assert pu.read_prepared_update() is None
  assert record["target"]


def test_a_settling_update_blocks_preparing_another(clone_env):
  origin, platform = clone_env
  record = _activate_bound(
    platform, origin, {"controller": "host", "id": "d" * 32},
  )
  newer = _advance_origin(origin, edits={"release.txt": "newer\n"})
  pu._fetch(platform)
  current = _served_sha(platform)

  for target in (newer, record["target"]):
    plan = _apply_plan(current, target, platform)
    plan.pop("repo")
    with pytest.raises(pu.PlatformUpdateError, match="finish_update_first"):
      pu.prepare_reviewed_update(**plan, repo=platform)
  with pytest.raises(pu.PlatformUpdateError, match="prepared_update_swapped"):
    pu.cancel_unfinished_update(platform)


def test_finishing_needs_only_a_restart_once_the_target_image_runs(clone_env):
  """The target image booted but the update did not stay in (its probe
  failed): another replacement would change nothing, so Finish restarts."""
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image("0" * 40)
  assert pu.unfinished_update(platform)["action"] == "replace"

  _boot_image(record["target"])
  pu.settle_prepared_update_for_this_image(platform)
  assert pu.revert_failed_update(platform)

  pending = pu.unfinished_update(platform)
  assert (pending["stage"], pending["action"]) == ("finish", "restart")
  # That restart's boot swaps it in again.
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"


def test_a_late_edit_conflict_resolution_keeps_the_replacement_binding(clone_env):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _local_commit(platform, edits={
    "backend/requirements.lock": "late-local-package==1\n",
  }, msg="late conflicting commit")
  pu.bind_update_operation(
    record["target"], {"controller": "host", "id": "d" * 32}, repo=platform,
  )
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "conflict"
  worktree = Path(pu._read_conflict_flag()["overlay"]["worktree"])
  (worktree / "backend/requirements.lock").write_text("new-package==1\n")
  _git(worktree, "add", "backend/requirements.lock")

  assert pu.continue_platform_overlay_update(platform) == "updated"

  kept = pu.read_prepared_update()
  assert kept["state"] == "swapped" and kept["operation"]["id"] == "d" * 32
  assert kept["replayed"] == _served_sha(platform)


def _record_image_inputs(platform: Path, lock: bytes) -> None:
  info = Path(os.environ["MOBIUS_BUILD_INFO_PATH"])
  data = json.loads(info.read_text()) if info.exists() else {}
  data["image_inputs"] = {
    path: hashlib.sha256(
      lock if path.endswith(".lock") else (platform / path).read_bytes(),
    ).hexdigest()
    for path in pu._PYTHON_DEPENDENCY_INPUTS
  }
  info.write_text(json.dumps(data))


def _with_python_inputs(platform: Path, origin: Path) -> None:
  release = _advance_origin(origin, edits={
    "backend/requirements.txt": "fastapi\n",
    "backend/requirements.lock": "fastapi==1\n",
  })
  pu._fetch(platform)
  _git(platform, "merge", "-q", "--ff-only", release)
  pu._set_upstream(platform, release)


def test_an_older_image_refuses_source_with_a_newer_release_s_packages(clone_env):
  """Booting an older image under a newer release's source (a historical
  image-only rollback) is unsupported and fails closed."""
  origin, platform = clone_env
  _with_python_inputs(platform, origin)
  old_image = _served_sha(platform)
  old_lock = (platform / "backend/requirements.lock").read_bytes()
  newer = _advance_origin(origin, edits={"backend/requirements.lock": "new-package==1\n"})
  pu._fetch(platform)
  _git(platform, "merge", "-q", "--ff-only", newer)
  pu._set_upstream(platform, newer)

  _boot_image(old_image)
  _record_image_inputs(platform, old_lock)
  with pytest.raises(pu.BootTransactionError, match="newer release"):
    pu.settle_prepared_update_for_this_image(platform)

  # The release's own image, or a newer one, serves it.
  _boot_image(newer)
  _record_image_inputs(platform, b"new-package==1\n")
  assert pu.settle_prepared_update_for_this_image(platform) == "none"


def test_an_image_newer_than_its_source_or_a_local_package_edit_still_boots(
  clone_env,
):
  origin, platform = clone_env
  _with_python_inputs(platform, origin)
  old_lock = (platform / "backend/requirements.lock").read_bytes()
  newer = _advance_origin(origin, edits={"backend/requirements.lock": "new-package==1\n"})
  pu._fetch(platform)

  _boot_image(newer)  # image-first: the source has not caught up yet
  _record_image_inputs(platform, b"new-package==1\n")
  assert pu.settle_prepared_update_for_this_image(platform) == "none"

  _boot_image(_served_sha(platform))
  _record_image_inputs(platform, old_lock)
  (platform / "backend/requirements.lock").write_text("locally-added==1\n")
  assert pu.settle_prepared_update_for_this_image(platform) == "none"


def test_entrypoint_settles_guards_and_probes_before_any_served_code_runs():
  script = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text(encoding="utf-8")
  body = script[script.index("rm -f /tmp/platform-boot-transaction"):]
  body = body[:body.index('printf \'%s\\n\' "$_serve_source" > /tmp/serving-source')]
  branch = body[body.index("if ! _platform_boot activate 2>&1; then"):]
  order = [
    0,
    branch.index("if ! _platform_boot guard 2>&1; then"),
    branch.index("if _platform_import_probe; then"),
    branch.index("elif _platform_boot revert"),
  ]
  assert order == sorted(order)
  assert "reconcile_clone_sync" not in body
  assert "cd /app/platform-baked/backend" in script


def test_a_second_finish_cannot_rebind_an_update_already_bound(clone_env):
  """Two concurrent Finish requests: the first binding wins, so the request
  that is published is the one that can confirm the update."""
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  first = {"controller": "host", "id": "a" * 32}
  second = {"controller": "host", "id": "b" * 32}
  pu.bind_update_operation(record["target"], first, repo=platform)

  pu.bind_update_operation(record["target"], first, repo=platform)  # idempotent
  with pytest.raises(pu.PlatformUpdateError, match="update_operation_bound"):
    pu.bind_update_operation(record["target"], second, repo=platform)
  # Only a caller that saw the first binding after its replacement ended may
  # replace it.
  pu.bind_update_operation(record["target"], second, repo=platform, replacing=first)
  assert pu.read_prepared_update()["operation"] == second


def test_a_bound_update_cannot_be_cancelled_until_its_binding_is_released(clone_env):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  operation = {"controller": "railway", "id": "replace_1"}
  pu.bind_update_operation(record["target"], operation, repo=platform)

  with pytest.raises(pu.PlatformUpdateError, match="update_operation_bound"):
    pu.cancel_unfinished_update(platform)
  pu.unbind_update_operation(record["target"], operation, repo=platform)
  pu.cancel_unfinished_update(platform)
  assert pu.read_prepared_update() is None


def _bound_late_conflict(platform: Path, origin: Path, *, uncommitted: bool) -> tuple[dict, Path]:
  record = _prepare_package_update(platform, origin)
  conflicting = "late-local-package==1\n"
  if uncommitted:
    (platform / "backend/requirements.lock").write_text(conflicting)
  else:
    _local_commit(platform, edits={"backend/requirements.lock": conflicting})
  pu.bind_update_operation(
    record["target"], {"controller": "host", "id": "d" * 32}, repo=platform,
  )
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "conflict"
  worktree = Path(pu._read_conflict_flag()["overlay"]["worktree"])
  return record, worktree


def test_a_resolved_uncommitted_late_edit_conflict_boots_as_replayed_again(clone_env):
  """The resolution is recorded only after its in-progress edits are unwound,
  so the next boot recognises the checkout instead of refusing it."""
  origin, platform = clone_env
  _record, worktree = _bound_late_conflict(platform, origin, uncommitted=True)
  (worktree / "backend/requirements.lock").write_text("new-package==1\nlocal==1\n")
  _git(worktree, "add", "backend/requirements.lock")

  assert pu.continue_platform_overlay_update(platform) == "updated"

  kept = pu.read_prepared_update()
  assert kept["replayed"] == _served_sha(platform)
  assert kept["operation"]["id"] == "d" * 32
  assert "backend/requirements.lock" in _git(platform, "status", "--porcelain").stdout
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"


@pytest.mark.parametrize(("module", "name", "after"), [
  ("pu", "_set_aside_resolver_work", True),  # answer kept, merge not dropped
  ("app_git", "remove_overlay_worktree", True),  # worktree gone, flag not
  ("pu", "_reset_hard_to", False),  # merge dropped, checkout not reset
  ("pu", "_settle_reverted", False),  # checkout reset, record still swapped
])
def test_an_image_not_kept_during_a_late_edit_conflict_keeps_the_resolver_s_work(
  clone_env, monkeypatch, module, name, after,
):
  origin, platform = clone_env
  record, worktree = _bound_late_conflict(platform, origin, uncommitted=False)
  (worktree / "backend/requirements.lock").write_text("half resolved\n")

  target = pu if module == "pu" else app_git
  real = getattr(target, name)
  calls = []

  def killed(*args, **kwargs):
    if calls:
      return real(*args, **kwargs)
    calls.append(name)
    if after:
      real(*args, **kwargs)
    raise _Killed(name)

  monkeypatch.setattr(target, name, killed)
  _boot_image(record["snapshot"])
  with pytest.raises(_Killed):
    pu.settle_prepared_update_for_this_image(platform)
  # Killed anywhere, the record still describes the swap, never a settled
  # update beside a stale late-edit conflict.
  assert pu.read_prepared_update()["state"] == "swapped"

  assert pu.settle_prepared_update_for_this_image(platform) == "reverted"
  assert pu.read_prepared_update()["state"] == "prepared"
  assert not pu.CONFLICT_FLAG.exists()
  assert not worktree.exists()
  assert pu.unfinished_update(platform)["stage"] == "finish"
  refs = _git(
    platform, "for-each-ref", "--format=%(refname)", pu._SET_ASIDE_PREFIX,
  ).stdout.split()
  kept = [
    ref for ref in refs
    if _git(platform, "show", f"{ref}:backend/requirements.lock", check=False).stdout
    == "half resolved\n"
  ]
  assert kept, "the resolver's in-progress answer was lost"


def test_a_bound_late_edit_conflict_cannot_be_abandoned(clone_env):
  origin, platform = clone_env
  _bound_late_conflict(platform, origin, uncommitted=False)

  with pytest.raises(pu.PlatformUpdateError, match="replay_conflict_must_finish"):
    pu.abandon_platform_overlay_update(platform)
  assert pu.read_prepared_update()["state"] == "swapped"


def test_a_local_requirements_edit_does_not_hide_a_newer_release_lock(clone_env):
  origin, platform = clone_env
  _with_python_inputs(platform, origin)
  old_image = _served_sha(platform)
  old_lock = (platform / "backend/requirements.lock").read_bytes()
  newer = _advance_origin(origin, edits={"backend/requirements.lock": "new-package==1\n"})
  pu._fetch(platform)
  _git(platform, "merge", "-q", "--ff-only", newer)
  pu._set_upstream(platform, newer)
  (platform / "backend/requirements.txt").write_text("fastapi\nlocally-added\n")

  _boot_image(old_image)
  _record_image_inputs(platform, old_lock)
  with pytest.raises(pu.BootTransactionError, match="newer release"):
    pu.settle_prepared_update_for_this_image(platform)


def test_the_boot_protocol_can_only_advance_with_a_new_image():
  path = Path(pu.__file__).resolve().parents[1] / "runtime" / "boot-protocol"
  assert int(path.read_text().strip()) == pu.BOOT_PROTOCOL
  impact = platform_activation.classify_activation(["backend/runtime/boot-protocol"])
  assert impact["level"] == platform_activation.ActivationLevel.IMAGE_REBUILD.value


def test_startup_refuses_a_checkout_it_cannot_place_on_images_without_the_transaction(
  clone_env,
):
  origin, platform = clone_env
  _swapped_with_late_commit(platform, origin)
  pu.settle_prepared_update_for_this_image(platform)
  record = pu.read_prepared_update()
  _git(platform, "reset", "-q", "--hard", record["prepared"])
  _local_commit(platform, edits={"other.txt": "unrelated\n"})

  with pytest.raises(pu.BootTransactionError):
    pu.complete_platform_swap(platform)


def test_startup_takes_no_update_lock_when_no_update_is_pending(clone_env, monkeypatch):
  """Most boots have no update; startup must not need a writable lock then."""
  blocked = Path(os.environ["MOBIUS_BUILD_INFO_PATH"]).parent / "not-a-dir"
  blocked.write_text("")
  monkeypatch.setattr(pu, "RECONCILE_LOCK", blocked / ".reconcile.lock")

  assert pu.complete_platform_swap() is None
  assert pu.confirm_platform_swap_loaded() is False


@pytest.mark.asyncio
async def test_only_an_unplaced_swap_stops_startup(monkeypatch):
  import logging

  from app import startup

  context = SimpleNamespace(logger=logging.getLogger("test"))

  def disk_full():
    raise OSError("disk full")

  monkeypatch.setattr(pu, "complete_platform_swap", disk_full)
  await startup._complete_platform_swap(context)  # best effort, as before

  def unplaced():
    raise pu.BootTransactionError("unplaced")

  monkeypatch.setattr(pu, "complete_platform_swap", unplaced)
  with pytest.raises(pu.BootTransactionError):
    await startup._complete_platform_swap(context)


def test_a_release_marker_on_a_local_commit_still_allows_a_newer_image(clone_env):
  """Older updaters could record a local merge commit as the installed
  release. The image's own history proves the source's packages are not
  newer, so a container-only upgrade still boots."""
  origin, platform = clone_env
  _with_python_inputs(platform, origin)
  old_lock = (platform / "backend/requirements.lock").read_bytes()
  local = _local_commit(platform, edits={"notes.txt": "local reconcile\n"})
  pu._set_upstream(platform, local)
  newer = _advance_origin(origin, edits={"backend/requirements.lock": "new-package==1\n"})
  pu._fetch(platform)

  _boot_image(newer)
  _record_image_inputs(platform, b"new-package==1\n")
  assert (platform / "backend/requirements.lock").read_bytes() == old_lock
  assert pu.settle_prepared_update_for_this_image(platform) == "none"


def test_an_image_whose_history_is_missing_proves_nothing(clone_env):
  origin, platform = clone_env
  _with_python_inputs(platform, origin)
  old_lock = (platform / "backend/requirements.lock").read_bytes()
  newer = _advance_origin(origin, edits={"backend/requirements.lock": "new-package==1\n"})
  pu._fetch(platform)
  _git(platform, "merge", "-q", "--ff-only", newer)
  pu._set_upstream(platform, newer)

  _boot_image("e" * 40)  # an image whose commit this checkout has never seen
  _record_image_inputs(platform, old_lock)
  with pytest.raises(pu.BootTransactionError, match="newer release"):
    pu.settle_prepared_update_for_this_image(platform)


def test_a_committed_local_package_edit_under_a_local_marker_still_boots(clone_env):
  """A local release marker may carry a local package declaration; it is not
  an official newer release, so an older image may still serve it."""
  origin, platform = clone_env
  _with_python_inputs(platform, origin)
  image = _served_sha(platform)
  image_lock = (platform / "backend/requirements.lock").read_bytes()
  local = _local_commit(platform, edits={
    "backend/requirements.lock": "fastapi==1\nlocally-added==1\n",
  })
  pu._set_upstream(platform, local)

  _boot_image(image)
  _record_image_inputs(platform, image_lock)
  assert pu.settle_prepared_update_for_this_image(platform) == "none"


def test_a_shallow_clone_judges_packages_by_the_recorded_release(clone_env, monkeypatch):
  """Absence from shallow history proves nothing, so an input matching the
  recorded release is refused unless the image's history positively declares
  it, even under a local marker."""
  origin, platform = clone_env
  _with_python_inputs(platform, origin)
  image = _served_sha(platform)
  image_lock = (platform / "backend/requirements.lock").read_bytes()
  local = _local_commit(platform, edits={
    "backend/requirements.lock": "fastapi==1\nlocally-added==1\n",
  })
  pu._set_upstream(platform, local)
  monkeypatch.setattr(pu, "_is_shallow", lambda repo=None: True)

  _boot_image(image)
  _record_image_inputs(platform, image_lock)
  with pytest.raises(pu.BootTransactionError, match="newer release"):
    pu.settle_prepared_update_for_this_image(platform)


def test_accepted_working_edit_replay_is_not_pending_when_head_equals_prepared(clone_env):
  """Uncommitted restoration can leave HEAD equal to the prepared update;
  its accepted replay must still win over the unstarted-swap check."""
  _origin, platform = clone_env
  head = _served_sha(platform)
  record = {"prepared": head, "replayed": head, "late": None, "late_committed": None}
  assert pu._swap_position(platform, record, head) == "replayed"


@pytest.mark.parametrize('edit', ['tracked', 'untracked', 'delete'])
def test_activation_preserves_uncommitted_edits_arriving_after_capture(
  clone_env, monkeypatch, edit,
):
  origin, platform = clone_env
  path = platform / ('new.txt' if edit == 'untracked' else 'backend/app/foo.py')
  if edit == 'delete':
    _advance_origin(origin, deletes=['backend/app/foo.py'])
  else:
    _advance_origin(origin, edits={str(path.relative_to(platform)): "VALUE = 'update'\n"})
  before = _served_sha(platform)
  original = pu._activate_candidate

  def dirty_then_activate(repo, local, pre_sha, tip):
    path.write_text("VALUE = 'arrived after snapshot'\n")
    original(repo, local, pre_sha, tip)

  monkeypatch.setattr(pu, '_activate_candidate', dirty_then_activate)
  result = pu.reconcile_clone(platform)

  assert path.read_text() == "VALUE = 'arrived after snapshot'\n"
  assert _served_sha(platform) == before
  assert result.status == 'error'


def test_rollback_preserves_uncommitted_edits_arriving_after_activation(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _advance_origin(origin, edits={'backend/app/foo.py': "VALUE = 'update'\n"})
  path = platform / 'backend/app/foo.py'

  def changed_then_fail(repo=platform, timeout=pu._PROBE_TIMEOUT, *, candidate_target=None):
    path.write_text("VALUE = 'arrived after activation'\n")
    return False, 'candidate rejected'

  monkeypatch.setattr(pu, '_import_probe', changed_then_fail)
  result = pu.reconcile_clone(platform)

  assert path.read_text() == "VALUE = 'arrived after activation'\n"
  assert result.status == 'error'


@pytest.mark.parametrize('staged', [False, True])
def test_activation_keeps_independent_edits_arriving_after_capture(
  clone_env, monkeypatch, staged,
):
  origin, platform = clone_env
  _advance_origin(origin, edits={'backend/app/foo.py': "VALUE = 'update'\n"})
  path = platform / 'independent.txt'
  original = pu._activate_candidate

  def edit_then_activate(repo, local, pre_sha, tip):
    path.write_text('independent work\n')
    if staged:
      _git(platform, 'add', 'independent.txt')
    original(repo, local, pre_sha, tip)

  monkeypatch.setattr(pu, '_activate_candidate', edit_then_activate)
  result = pu.reconcile_clone(platform)
  assert result.status == 'updated'
  assert path.read_text() == 'independent work\n'
  assert _git(platform, 'status', '--porcelain').stdout.splitlines() == [
    'A  independent.txt' if staged else '?? independent.txt',
  ]
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_activation_preserves_new_staged_overlap_without_leaving_a_crash_marker(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _advance_origin(origin, edits={'backend/app/foo.py': "VALUE = 'update'\n"})
  before = _served_sha(platform)
  original = pu._activate_candidate

  def edit_then_activate(repo, local, pre_sha, tip):
    (platform / 'backend/app/foo.py').write_text("VALUE = 'new staged work'\n")
    _git(platform, 'add', 'backend/app/foo.py')
    original(repo, local, pre_sha, tip)

  monkeypatch.setattr(pu, '_activate_candidate', edit_then_activate)
  result = pu.reconcile_clone(platform)
  assert result.status == 'error'
  assert _served_sha(platform) == before
  assert _git(platform, 'show', ':backend/app/foo.py').stdout == "VALUE = 'new staged work'\n"
  assert (platform / 'backend/app/foo.py').read_text() == "VALUE = 'new staged work'\n"
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_checkout_rechecks_a_write_after_its_non_destructive_preflight(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  _advance_origin(origin, edits={'backend/app/foo.py': "VALUE = 'update'\n"})
  before = _served_sha(platform)
  path = platform / 'backend/app/foo.py'
  original = pu._git
  edited = False

  def edit_before_checkout(*args, **kwargs):
    nonlocal edited
    if not edited and kwargs.get('repo') == platform and args[:3] == ('read-tree', '-m', '-u'):
      edited = True
      path.write_text("VALUE = 'newer than preflight'\n")
    return original(*args, **kwargs)

  monkeypatch.setattr(pu, '_git', edit_before_checkout)
  result = pu.reconcile_clone(platform)
  assert result.status == 'error'
  assert _served_sha(platform) == before
  assert path.read_text() == "VALUE = 'newer than preflight'\n"
  # Once the real checkout was attempted, native failure alone cannot prove
  # no partial files changed. Retain recovery ownership and the new bytes.
  assert pu.RECONCILE_PRE_FLAG.exists()


def test_reverse_checkout_success_does_not_clear_partial_source_marker(clone_env, monkeypatch):
  origin, platform = clone_env
  before = _served_sha(platform)
  tip = _advance_origin(origin, edits={'backend/app/foo.py': "VALUE = 'candidate'\n"})
  _git(platform, 'fetch', '-q', 'origin')
  original = pu._git
  forward = True
  def half_written(*args, **kwargs):
    nonlocal forward
    if forward and kwargs.get('repo') == platform and args[:3] == ('read-tree', '-m', '-u'):
      forward = False
      (platform / 'backend/app/foo.py').write_text("VALUE = 'candidate'\n")
      raise RuntimeError('interrupted before index write')
    return original(*args, **kwargs)
  monkeypatch.setattr(pu, '_git', half_written)
  with pytest.raises(RuntimeError, match='interrupted'):
    pu._activate_candidate(platform, pu._local_branch(platform), before, tip)
  assert _served_sha(platform) == before
  assert (platform / 'backend/app/foo.py').read_text() == "VALUE = 'candidate'\n"
  assert pu.RECONCILE_PRE_FLAG.exists()
  monkeypatch.setattr(pu, '_git', original)
  receipt = pu.boot_guard_clean_served_tree(platform)
  saved = receipt.split(' saved_work=', 1)[1].split()[0]
  assert _git(platform, 'show', saved + ':backend/app/foo.py').stdout == "VALUE = 'candidate'\n"
  assert (platform / 'backend/app/foo.py').read_text() == _git(platform, 'show', before + ':backend/app/foo.py').stdout
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_boot_recovery_preserves_staged_only_blob_and_raw_index(clone_env):
  _origin, platform = clone_env
  pre = _served_sha(platform)
  original = (platform / 'backend/app/foo.py').read_text()
  (platform / 'backend/app/foo.py').write_text("VALUE = 'staged only'\n")
  _git(platform, 'add', 'backend/app/foo.py')
  (platform / 'backend/app/foo.py').write_text(original)
  pu._write_reconcile_pre(pre, pre)
  receipt = pu.boot_guard_clean_served_tree(platform)
  saved = receipt.split(' saved_index=', 1)[1].split()[0]
  assert _git(platform, 'show', saved + ':stage-0/backend/app/foo.py').stdout == "VALUE = 'staged only'\n"
  assert _git(platform, 'cat-file', '-s', saved + ':original-index').returncode == 0
  _git(platform, 'reflog', 'expire', '--expire=now', '--all')
  _git(platform, 'gc', '--prune=now')
  assert _git(platform, 'show', saved + ':stage-0/backend/app/foo.py').stdout == "VALUE = 'staged only'\n"


def test_unknown_partial_checkout_retains_marker_instead_of_serving(clone_env):
  _origin, platform = clone_env
  pre = _served_sha(platform)
  advanced = _local_commit(platform, edits={'backend/app/foo.py': "VALUE = 'new owner'\n"})
  (platform / 'backend/app/foo.py').write_text("VALUE = 'half written'\n")
  pu._write_reconcile_pre(pre, pre)
  with pytest.raises(pu.BootTransactionError, match='marker retained'):
    pu.boot_guard_clean_served_tree(platform)
  assert pu.RECONCILE_PRE_FLAG.exists()
  assert _served_sha(platform) == advanced
  assert (platform / 'backend/app/foo.py').read_text() == "VALUE = 'half written'\n"


def test_boot_recovery_keeps_ignored_untracked_file_obstructing_old_source(clone_env):
  _origin, platform = clone_env
  pre = _local_commit(platform, edits={'layout': 'old file\n'})
  (platform / 'layout').unlink()
  (platform / 'layout').mkdir()
  (platform / 'layout/module.py').write_text('NEW = True\n')
  _git(platform, 'add', '-A', '.')
  _git(platform, 'commit', '-q', '-m', 'candidate file to directory')
  tip = _served_sha(platform)
  _git(platform, 'config', 'core.excludesFile', str(platform / '.git/info/exclude'))
  (platform / '.git/info/exclude').write_text('private.bin\n')
  (platform / 'layout/private.bin').write_text('untracked ignored work\n')
  pu._write_reconcile_pre(pre, tip)
  receipt = pu.boot_guard_clean_served_tree(platform)
  saved = receipt.split(' saved_work=', 1)[1].split()[0]
  assert _git(platform, 'show', saved + ':layout/private.bin').stdout == 'untracked ignored work\n'
  assert (platform / 'layout').read_text() == 'old file\n'
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_boot_recovery_keeps_ignored_ancestor_file_obstructing_old_directory(clone_env):
  _origin, platform = clone_env
  pre = _local_commit(platform, edits={'layout/module.py': 'OLD = True\n'})
  (platform / 'layout/module.py').unlink()
  (platform / 'layout').rmdir()
  _git(platform, 'add', '-A', '.')
  _git(platform, 'commit', '-q', '-m', 'candidate removes directory')
  tip = _served_sha(platform)
  (platform / '.git/info/exclude').write_text('layout\n')
  (platform / 'layout').write_text('ignored ancestor work\n')
  pu._write_reconcile_pre(pre, tip)
  receipt = pu.boot_guard_clean_served_tree(platform)
  saved = receipt.split(' saved_work=', 1)[1].split()[0]
  assert _git(platform, 'show', saved + ':layout').stdout == 'ignored ancestor work\n'
  assert (platform / 'layout/module.py').read_text() == 'OLD = True\n'
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_boot_recovery_keeps_every_conflict_stage_and_exact_index(clone_env):
  _origin, platform = clone_env
  _git(platform, "checkout", "-q", "-b", "other")
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'other'\n"})
  _git(platform, "checkout", "-q", "main")
  pre = _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'main'\n"})
  assert _git(platform, "merge", "other", check=False).returncode == 1
  stages = _git(platform, "ls-files", "--stage", "backend/app/foo.py").stdout.splitlines()
  index_oid = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  working = (platform / "backend/app/foo.py").read_text()
  pu._write_reconcile_pre(pre, pre)

  receipt = pu.boot_guard_clean_served_tree(platform)

  saved_index = receipt.split(" saved_index=", 1)[1].split()[0]
  saved_work = receipt.split(" saved_work=", 1)[1].split()[0]
  assert len(stages) == 3
  for entry in stages:
    metadata, path = entry.split("\t", 1)
    _mode, oid, stage = metadata.split()
    assert _git(platform, "rev-parse", f"{saved_index}:stage-{stage}/{path}").stdout.strip() == oid
  assert _git(platform, "rev-parse", saved_index + ":original-index").stdout.strip() == index_oid
  assert _git(platform, "show", saved_work + ":backend/app/foo.py").stdout == working
  assert _served_sha(platform) == pre
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_failed_recovery_snapshot_keeps_source_index_and_marker(clone_env, monkeypatch):
  _origin, platform = clone_env
  pre = _served_sha(platform)
  path = platform / "backend/app/foo.py"
  path.write_text("VALUE = 'must survive'\n")
  _git(platform, "add", "backend/app/foo.py")
  index = (platform / ".git/index").read_bytes()
  pu._write_reconcile_pre(pre, pre)

  def unavailable(*_args):
    raise pu.PlatformUpdateError("recovery snapshot unavailable")

  monkeypatch.setattr(pu, "_keep_set_aside", unavailable)
  with pytest.raises(pu.PlatformUpdateError, match="snapshot unavailable"):
    pu.boot_guard_clean_served_tree(platform)
  assert _served_sha(platform) == pre
  assert path.read_text() == "VALUE = 'must survive'\n"
  assert (platform / ".git/index").read_bytes() == index
  assert pu.RECONCILE_PRE_FLAG.exists()


def test_boot_recovery_preserves_intent_to_add_index_entry(clone_env):
  _origin, platform = clone_env
  pre = _served_sha(platform)
  (platform / "new.txt").write_text("new unstaged work\n")
  _git(platform, "add", "-N", "new.txt")
  assert _git(platform, "diff", "--cached", "--quiet", pre, "--").returncode == 0
  index_oid = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  pu._write_reconcile_pre(pre, pre)

  receipt = pu.boot_guard_clean_served_tree(platform)

  saved = receipt.split(" saved_index=", 1)[1].split()[0]
  assert _git(platform, "rev-parse", saved + ":original-index").stdout.strip() == index_oid
  assert (platform / "new.txt").read_text() == "new unstaged work\n"
  assert saved in pu._read_rolled_back_flag()["error"]


def test_marker_free_interrupted_merge_reports_saved_work_and_index(clone_env):
  _origin, platform = clone_env
  _git(platform, "checkout", "-q", "-b", "other")
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'other'\n"})
  _git(platform, "checkout", "-q", "main")
  pre = _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'main'\n"})
  assert _git(platform, "merge", "other", check=False).returncode == 1
  assert not pu.RECONCILE_PRE_FLAG.exists()
  working = (platform / "backend/app/foo.py").read_text()

  receipt = pu.boot_guard_clean_served_tree(platform)

  saved_work = receipt.split(" saved_work=", 1)[1].split()[0]
  saved_index = receipt.split(" saved_index=", 1)[1].split()[0]
  assert _git(platform, "show", saved_work + ":backend/app/foo.py").stdout == working
  assert saved_work in pu._read_rolled_back_flag()["error"]
  assert saved_index in pu._read_rolled_back_flag()["error"]
  assert _served_sha(platform) == pre


# Composition regressions: a native checkout refusal is not enough if a later
# final gate drops recovery ownership or unwinding an earlier WIP loses staging.

def _reported_private_recovery_refs(platform: Path, receipt: str = "") -> list[str]:
  """Recovery is useful only when its durable private ref is reported back."""
  report = receipt + " " + str(pu.platform_status(platform).get("rollback_error") or "")
  return [
    ref for ref in _git(platform, "for-each-ref", "--format=%(refname)", "refs/mobius").stdout.splitlines()
    if ref in report
  ]


@pytest.mark.parametrize("gate", ["backend_import", "frontend_install", "frontend_build"])
def test_failed_final_gate_recovers_late_work_instead_of_booting_a_mixed_checkout(
  clone_env, monkeypatch, gate,
):
  """A late write rejects the reverse checkout *after* its ref moved to PRE.

  Every final gate must still own recovery. Boot must either recover the known
  transition with reachable work/index receipts, or refuse to serve it; saying
  'clean' while the index and files still carry the rejected candidate is unsafe.
  """
  origin, platform = clone_env
  before = _served_sha(platform)
  original_foo = (platform / "backend/app/foo.py").read_text()
  edits = {
    "backend/app/foo.py": "VALUE = 'candidate'\n",
    "frontend/src/App.jsx": "export default 'candidate'\n",
  }
  if gate == "frontend_install":
    edits["frontend/package-lock.json"] = "{}\n"
  _advance_origin(origin, edits=edits)
  staged = "VALUE = 'late staged during final gate'\n"
  working = "VALUE = 'late working during final gate'\n"
  observed = {}

  def late_write():
    observed["marker_at_gate"] = pu.RECONCILE_PRE_FLAG.exists()
    (platform / "backend/app/foo.py").write_text(staged)
    _git(platform, "add", "backend/app/foo.py")
    (platform / "backend/app/foo.py").write_text(working)
    observed["index_oid"] = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()

  def import_gate(*_args, **_kwargs):
    if gate == "backend_import":
      late_write()
      return False, "final import rejected"
    return True, ""

  def dependency_gate(_repo):
    if gate == "frontend_install":
      late_write()
      return False, "final dependency install rejected"
    return True, ""

  def build_gate(_repo, _result):
    assert gate == "frontend_build"
    late_write()
    raise RuntimeError("final frontend build rejected")

  monkeypatch.setattr(pu, "_import_probe", import_gate)
  monkeypatch.setattr(pu, "_sync_frontend_dependencies", dependency_gate)
  monkeypatch.setattr(pu, "_rebuild_frontend", build_gate)
  result = pu.reconcile_clone(platform)

  assert result.status == "error"  # overlapping late work makes rollback refuse
  assert _served_sha(platform) == before  # CAS succeeded; the checkout did not
  assert (platform / "backend/app/foo.py").read_text() == working
  assert _git(platform, "show", ":backend/app/foo.py").stdout == staged
  problems = []
  if not observed["marker_at_gate"]:
    problems.append(f"{gate} ran after recovery ownership had already been cleared")
  if not pu.RECONCILE_PRE_FLAG.exists():
    problems.append("ref is PRE but refused reverse checkout has no recovery marker")

  receipt = pu.boot_guard_clean_served_tree(platform)
  refs = _reported_private_recovery_refs(platform, receipt)
  if receipt.startswith("boot_guard[clean]"):
    problems.append(f"boot incorrectly accepted the mixed checkout: {receipt}")
  if (platform / "backend/app/foo.py").read_text() != original_foo:
    problems.append("boot left late/candidate working bytes on the old served ref")
  if _git(platform, "diff", "--cached", "--quiet", before, "--", check=False).returncode:
    problems.append("boot left a candidate/late index on the old served ref")
  if (platform / "frontend/src/App.jsx").exists():
    problems.append("boot left rejected frontend source on the old served ref")
  assert _served_sha(platform) == before
  assert not pu.RECONCILE_PRE_FLAG.exists()

  # Reflogs are not a preservation receipt. The reported private refs must
  # retain both distinct versions and the exact index even after immediate GC.
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  work_refs = [ref for ref in refs if _git(
    platform, "show", ref + ":backend/app/foo.py", check=False,
  ).stdout == working]
  index_refs = [ref for ref in refs if (
    _git(platform, "show", ref + ":stage-0/backend/app/foo.py", check=False).stdout == staged
    and _git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == observed["index_oid"]
    and _git(platform, "cat-file", "-e", ref + ":original-index", check=False).returncode == 0
  )]
  if not work_refs:
    problems.append("no reported GC-durable private ref retains the late working bytes")
  if not index_refs:
    problems.append("no reported GC-durable private ref retains the late staged bytes/raw index")
  assert not problems, "\n".join(problems)


@pytest.mark.parametrize("index_flag", [None, "--assume-unchanged", "--skip-worktree"])
def test_wip_preflight_refusal_preserves_late_staged_only_content_and_flags(
  clone_env, monkeypatch, index_flag,
):
  """An earlier uncommitted edit must not authorize losing a *later* index.

  A clean-start staged-overlap test misses this: the final WIP unwind is a
  mixed reset. The later staged-only version is absent from the working tree
  and from every commit, so a lost index makes it genuinely GC-unreachable.
  """
  origin, platform = clone_env
  before = _served_sha(platform)
  earlier = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'earlier uncommitted work'")
  (platform / "backend/app/main.py").write_text(earlier)
  original_foo = (platform / "backend/app/foo.py").read_text()
  _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'candidate'\n"})
  staged = "VALUE = 'late staged-only owner work'\n"
  captured = {}
  activate = pu._activate_candidate

  def stage_after_capture(repo, local, pre_sha, tip):
    (platform / "backend/app/foo.py").write_text(staged)
    _git(platform, "add", "backend/app/foo.py")
    (platform / "backend/app/foo.py").write_text(original_foo)
    if index_flag:
      _git(platform, "update-index", index_flag, "backend/app/foo.py")
    captured["blob"] = _git(platform, "rev-parse", ":backend/app/foo.py").stdout.strip()
    captured["flags"] = _git(platform, "ls-files", "-v", "backend/app/foo.py").stdout
    captured["raw_index"] = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
    activate(repo, local, pre_sha, tip)

  monkeypatch.setattr(pu, "_activate_candidate", stage_after_capture)
  result = pu.reconcile_clone(platform)

  assert result.status == "error"
  assert _served_sha(platform) == before
  assert (platform / "backend/app/main.py").read_text() == earlier
  assert (platform / "backend/app/foo.py").read_text() == original_foo
  assert " M backend/app/main.py" in _git(platform, "status", "--porcelain").stdout.splitlines()
  actual_staged = _git(platform, "show", ":backend/app/foo.py").stdout
  actual_flags = _git(platform, "ls-files", "-v", "backend/app/foo.py").stdout
  in_index = actual_staged == staged and actual_flags == captured["flags"]
  refs = _reported_private_recovery_refs(platform)
  saved = [ref for ref in refs if (
    _git(platform, "rev-parse", ref + ":stage-0/backend/app/foo.py", check=False).stdout.strip() == captured["blob"]
    and _git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == captured["raw_index"]
  )]
  problems = []
  if not (in_index or saved):
    problems.append(
      f"late staging/flags lost without a reported recovery receipt: "
      f"stage={actual_staged!r}, flags={actual_flags!r}, expected_flags={captured['flags']!r}"
    )
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  if _git(platform, "cat-file", "-p", captured["blob"], check=False).stdout != staged:
    problems.append("the late staged-only blob was pruned: neither the live index nor a durable private ref retained it")
  for ref in saved:
    assert _git(platform, "show", ref + ":stage-0/backend/app/foo.py").stdout == staged
    assert _git(platform, "cat-file", "-e", ref + ":original-index").returncode == 0
  assert not problems, "\n".join(problems)


@pytest.mark.parametrize("carried_wip", [False, True])
def test_successful_update_keeps_unrelated_late_staged_and_working_versions(
  clone_env, monkeypatch, carried_wip,
):
  """The collision fix must not reject safe paths or flatten their staging."""
  origin, platform = clone_env
  earlier = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'earlier work'") if carried_wip else _MAIN_PY
  (platform / "backend/app/main.py").write_text(earlier)
  target = _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'candidate'\n"})
  staged = "STAGED = True\n"
  unstaged = "STAGED = False  # newer working version\n"
  late_main = earlier.replace("LINE_B = 2", "LINE_B = 'late working edit'")
  activate = pu._activate_candidate

  def independent_writes(repo, local, pre_sha, tip):
    (platform / "backend/app/__init__.py").write_text(staged)
    _git(platform, "add", "backend/app/__init__.py")
    (platform / "backend/app/__init__.py").write_text(unstaged)
    (platform / "backend/app/main.py").write_text(late_main)
    (platform / "independent.txt").write_text("late untracked work\n")
    activate(repo, local, pre_sha, tip)

  monkeypatch.setattr(pu, "_activate_candidate", independent_writes)
  result = pu.reconcile_clone(platform)

  assert result.status == "updated"
  assert _served_sha(platform) == target == result.new_sha
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'candidate'\n"
  assert (platform / "backend/app/__init__.py").read_text() == unstaged
  assert (platform / "backend/app/main.py").read_text() == late_main
  assert (platform / "independent.txt").read_text() == "late untracked work\n"
  assert _git(platform, "show", ":backend/app/__init__.py").stdout == staged
  assert _git(platform, "status", "--porcelain").stdout.splitlines() == [
    "MM backend/app/__init__.py", " M backend/app/main.py", "?? independent.txt",
  ]
  assert not pu.RECONCILE_PRE_FLAG.exists()


@pytest.mark.parametrize("collision", ["same_path", "ignored_ancestor", "ignored_child"])
@pytest.mark.parametrize("arrival", ["after_capture", "after_preflight"])
def test_activation_preserves_late_ignored_file_and_directory_collisions(
  clone_env, monkeypatch, collision, arrival,
):
  """Ignored does not mean disposable, including either side of a D/F swap.

  Native two-tree checkout protects ordinary untracked files, but treats
  ignored ones as overwriteable even when they arrive after its dry run.
  """
  origin, platform = clone_env
  before = _served_sha(platform)
  candidate_path = "new.txt/module.py" if collision == "ignored_ancestor" else "new.txt"
  owner_path = "new.txt/private.bin" if collision == "ignored_child" else "new.txt"
  _advance_origin(origin, edits={candidate_path: "candidate addition\n"})
  (platform / ".git/info/exclude").write_text("new.txt\n")
  owner_bytes = b"ignored owner bytes\x00not disposable\xff\n"
  path = platform / owner_path
  arrived = False

  def write_ignored_work():
    nonlocal arrived
    assert not arrived
    arrived = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(owner_bytes)
    assert _git(platform, "check-ignore", owner_path).returncode == 0
    assert _git(platform, "ls-files", "--", owner_path).stdout == ""

  if arrival == "after_capture":
    activate = pu._activate_candidate

    def write_then_activate(repo, local, pre_sha, tip):
      write_ignored_work()
      activate(repo, local, pre_sha, tip)

    monkeypatch.setattr(pu, "_activate_candidate", write_then_activate)
  else:
    git = pu._git

    def write_after_native_preflight(*args, **kwargs):
      proc = git(*args, **kwargs)
      if kwargs.get("repo") == platform and args[:4] == ("read-tree", "-n", "-m", "-u"):
        write_ignored_work()
      return proc

    monkeypatch.setattr(pu, "_git", write_after_native_preflight)

  result = pu.reconcile_clone(platform)

  assert arrived
  assert path.is_file() and path.read_bytes() == owner_bytes, (
    f"{collision} {arrival}: ignored local bytes overwritten; "
    f"result={result.status}, HEAD={_served_sha(platform)}, PRE={before}"
  )
  if result.status != "updated":
    assert result.status == "error"
    assert _served_sha(platform) == before


def test_wip_unwind_refuses_overlapping_staging_without_discarding_it(clone_env):
  """Unwind is not permission to reset staging on the WIP's own paths."""
  _origin, platform = clone_env
  path = platform / "backend/app/main.py"
  earlier = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'captured work'")
  path.write_text(earlier)
  carried = pu._carry_working_edits(platform, pu._local_branch(platform))
  staged = earlier.replace("LINE_B = 2", "LINE_B = 'later staging'")
  path.write_text(staged)
  _git(platform, "add", "backend/app/main.py")
  path.write_text(earlier)
  blob = _git(platform, "rev-parse", ":backend/app/main.py").stdout.strip()

  assert not pu._restore_working_edits(platform, pu._local_branch(platform))
  assert _served_sha(platform) == carried.pre
  assert path.read_text() == earlier
  assert _git(platform, "show", ":backend/app/main.py").stdout == staged
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  assert _git(platform, "cat-file", "-p", blob).stdout == staged


def test_wip_unwind_keeps_late_intent_to_add_entry(clone_env):
  """Tree equality alone does not capture an intent-to-add index entry."""
  _origin, platform = clone_env
  before = _served_sha(platform)
  earlier = _MAIN_PY.replace("LINE_A = 1", "LINE_A = 'captured work'")
  (platform / "backend/app/main.py").write_text(earlier)
  pu._carry_working_edits(platform, pu._local_branch(platform))
  (platform / "unfinished.txt").write_text("not staged yet\n")
  _git(platform, "add", "-N", "unfinished.txt")
  flags = _git(platform, "ls-files", "--debug", "unfinished.txt").stdout.split("flags: ")[-1]

  assert pu._restore_working_edits(platform, pu._local_branch(platform))
  assert _served_sha(platform) == before
  assert (platform / "backend/app/main.py").read_text() == earlier
  assert (platform / "unfinished.txt").read_text() == "not staged yet\n"
  assert _git(platform, "ls-files", "--debug", "unfinished.txt").stdout.split("flags: ")[-1] == flags
  assert _git(platform, "diff", "--cached", "--quiet", check=False).returncode == 0


@pytest.mark.parametrize("edit", ["staged_only", "intent_to_add", "ignored_ancestor", "ignored_child"])
def test_image_revert_preserves_index_and_ignored_work_before_forced_checkout(clone_env, edit):
  """An image rollback has the same preservation duty as interrupted boot."""
  origin, platform = clone_env
  if edit == "ignored_ancestor":
    _local_commit(platform, edits={"layout/module.py": "old directory source\n"})
  elif edit == "ignored_child":
    _local_commit(platform, edits={"layout": "old file source\n"})
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  original_foo = (platform / "backend/app/foo.py").read_text()
  staged = "VALUE = 'staged-only after candidate boot'\n"
  ignored = b"ignored rollback work\x00\xff\n"
  if edit == "staged_only":
    (platform / "backend/app/foo.py").write_text(staged)
    _git(platform, "add", "backend/app/foo.py")
    (platform / "backend/app/foo.py").write_text(original_foo)
  elif edit == "intent_to_add":
    (platform / "unfinished.txt").write_text("intent to add after candidate boot\n")
    _git(platform, "add", "-N", "unfinished.txt")
  else:
    _git(platform, "rm", "-q", "layout/module.py" if edit == "ignored_ancestor" else "layout")
    if edit == "ignored_child":
      (platform / "layout").mkdir()
      (platform / "layout/module.py").write_text("candidate directory source\n")
      _git(platform, "add", "layout/module.py")
    _git(platform, "commit", "-q", "-m", "candidate layout change")
    (platform / ".git/info/exclude").write_text("layout\n")
    path = platform / ("layout" if edit == "ignored_ancestor" else "layout/private.bin")
    path.write_bytes(ignored)
  raw_index = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()

  assert pu.revert_failed_update(platform)
  assert pu.read_prepared_update()["state"] == "prepared"
  refs = _reported_private_recovery_refs(platform)
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  if edit in {"staged_only", "intent_to_add"}:
    indexes = [ref for ref in refs if _git(
      platform, "rev-parse", ref + ":original-index", check=False,
    ).stdout.strip() == raw_index]
    assert indexes, "forced image rollback lost the exact index without a reported recovery ref"
    if edit == "staged_only":
      assert _git(platform, "show", indexes[0] + ":stage-0/backend/app/foo.py").stdout == staged
  else:
    owner_path = "layout" if edit == "ignored_ancestor" else "layout/private.bin"
    assert any(subprocess.run(
      ["git", "-C", str(platform), "show", ref + ":" + owner_path],
      capture_output=True,
    ).stdout == ignored for ref in refs), "forced image rollback discarded ignored owner bytes"


@pytest.mark.parametrize("killed", [False, True])
def test_partial_image_revert_retains_checkout_ownership_until_boot_repairs_it(
  clone_env, monkeypatch, killed,
):
  origin, platform = clone_env
  _advance_origin(origin, edits={"backend/app/foo.py": "VALUE = 'candidate'\n"})
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  late = pu.read_prepared_update()["late"]
  original = _git(platform, "show", late + ":backend/app/foo.py").stdout
  reset = pu._reset_hard_to

  def partial(repo, local, sha):
    reset(repo, local, sha)
    (repo / "backend/app/foo.py").write_text("VALUE = 'incomplete image revert'\n")
    if killed:
      raise _Killed("revert checkout")

  monkeypatch.setattr(pu, "_reset_hard_to", partial)
  with pytest.raises(_Killed if killed else pu.BootTransactionError):
    pu.revert_failed_update(platform)
  assert pu.RECONCILE_PRE_FLAG.exists()
  assert pu.read_prepared_update()["state"] == "swapped"
  monkeypatch.setattr(pu, "_reset_hard_to", reset)
  _boot_image(record["snapshot"])
  assert pu.settle_prepared_update_for_this_image(platform) == "reverted"
  assert (platform / "backend/app/foo.py").read_text() == original
  assert not pu.RECONCILE_PRE_FLAG.exists()


@pytest.mark.parametrize("index_flag", ["--skip-worktree", "--assume-unchanged", "sparse_checkout"])
def test_forced_image_revert_restores_unchanged_tracked_source_hidden_by_flags(
  clone_env, index_flag,
):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  late = pu.read_prepared_update()["late"]
  original = _git(platform, "show", late + ":backend/app/foo.py").stdout
  bad = "raise ImportError('candidate-only hidden source')\n"
  if index_flag == "sparse_checkout":
    _git(platform, "config", "core.sparseCheckout", "true")
    (platform / ".git/info/sparse-checkout").write_text("/backend/app/main.py\n")
    _git(platform, "read-tree", "-m", "-u", "HEAD")
  else:
    _git(platform, "update-index", index_flag, "backend/app/foo.py")
  (platform / "backend/app/foo.py").write_text(bad)
  raw_index = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  (platform / "independent.txt").write_text("independent untracked work\n")

  assert pu.revert_failed_update(platform)
  _boot_image(record["snapshot"])
  assert pu.settle_prepared_update_for_this_image(platform) == "waiting"
  assert (platform / "backend/app/foo.py").read_text() == original
  assert _git(platform, "show", ":backend/app/foo.py").stdout == original
  assert _git(platform, "ls-files", "-v", "backend/app/foo.py").stdout.startswith("H ")
  assert (platform / "independent.txt").read_text() == "independent untracked work\n"
  if index_flag == "sparse_checkout":
    assert _git(platform, "config", "core.sparseCheckout").stdout.strip() == "true"
  assert not pu.RECONCILE_PRE_FLAG.exists()
  assert not (platform / "backend/app/uses_new_package.py").exists()
  ok, error = pu._import_probe(platform)
  assert ok, error
  refs = _reported_private_recovery_refs(platform)
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  assert any(_git(platform, "show", ref + ":backend/app/foo.py", check=False).stdout == bad
             for ref in refs)
  assert any(_git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == raw_index
             for ref in refs)


@pytest.mark.parametrize("name", [
  "_write_reconcile_pre", "_keep_set_aside", "_reset_hard_to",
  "_write_rolled_back_flag", "_write_prepared_update", "_clear_reconcile_pre",
])
@pytest.mark.parametrize("after", [False, True])
def test_image_revert_receipt_keeps_saved_identity_across_transaction_death(
  clone_env, monkeypatch, name, after,
):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  original = (platform / "backend/app/foo.py").read_text()
  staged = "VALUE = 'cached-only before forced revert'\n"
  (platform / "backend/app/foo.py").write_text(staged)
  _git(platform, "add", "backend/app/foo.py")
  (platform / "backend/app/foo.py").write_text(original)
  (platform / "draft.txt").write_text("working version before forced revert\n")
  raw_index = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  _kill_once(monkeypatch, name, after=after)

  with pytest.raises(_Killed):
    pu.revert_failed_update(platform)
  refs = _git(platform, "for-each-ref", "--format=%(refname)", pu._SET_ASIDE_PREFIX).stdout.split()
  ownership_retained = pu.RECONCILE_PRE_FLAG.exists()
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  _boot_image(record["snapshot"])
  pu.settle_prepared_update_for_this_image(platform)

  assert pu.read_prepared_update()["state"] == "prepared"
  assert not pu.RECONCILE_PRE_FLAG.exists()
  report = pu.platform_status(platform)["rollback_error"] or ""
  assert all(ref in report for ref in refs), "a durable snapshot with no surfaced identity is not recovery"
  if name in {"_reset_hard_to", "_write_rolled_back_flag", "_write_prepared_update"}:
    assert ownership_retained, "source repair does not retire receipt ownership"
  refs = _reported_private_recovery_refs(platform)
  assert any(_git(platform, "show", ref + ":stage-0/backend/app/foo.py", check=False).stdout == staged
             and _git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == raw_index
             for ref in refs)
  assert any(_git(platform, "show", ref + ":draft.txt", check=False).stdout == "working version before forced revert\n"
             for ref in refs)
  assert (platform / "backend/app/foo.py").read_text() == original
  assert not (platform / "backend/app/uses_new_package.py").exists()


@pytest.mark.parametrize("edit", ["staged_only", "staged_and_working", "intent_to_add", "flagged_staging"])
def test_initial_index_is_gc_recoverable_after_disjoint_platform_capture(clone_env, edit):
  origin, platform = clone_env
  path = platform / "backend/app/foo.py"
  original = path.read_text()
  staged = "VALUE = 'initial cached-only owner version'\n"
  if edit == "intent_to_add":
    (platform / "unfinished.txt").write_text("initial intent to add\n")
    _git(platform, "add", "-N", "unfinished.txt")
    stage_path = "unfinished.txt"
  else:
    path.write_text(staged)
    _git(platform, "add", "backend/app/foo.py")
    path.write_text(original)
    stage_path = "backend/app/foo.py"
    if edit == "flagged_staging":
      _git(platform, "update-index", "--skip-worktree", stage_path)
  if edit == "staged_and_working":
    original = "VALUE = 'different initial working version'\n"
    path.write_text(original)
  blob = _git(platform, "rev-parse", ":" + stage_path).stdout.strip()
  raw_index = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  target = _advance_origin(origin, edits={"readme.txt": "disjoint release\n"})

  result = pu.reconcile_clone(platform)

  assert result.status == "updated"
  assert _served_sha(platform) == target
  assert path.read_text() == original
  assert not pu.RECONCILE_PRE_FLAG.exists()
  assert not pu.ROLLED_BACK_FLAG.exists(), "index preservation is not a failed update"
  status = pu.platform_status(platform)
  refs = status.get("recovery_refs", [])
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  assert _git(platform, "cat-file", "-e", blob, check=False).returncode == 0, "capture discarded the initial staged blob"
  assert any(_git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == raw_index
             and _git(platform, "rev-parse", ref + ":stage-0/" + stage_path, check=False).stdout.strip() == blob
             for ref in refs), "initial index needs a surfaced exact recovery copy"


@pytest.mark.parametrize("after", [False, True])
def test_initial_index_capture_is_gc_recoverable_if_commit_process_dies(
  clone_env, monkeypatch, after,
):
  _origin, platform = clone_env
  path = platform / "backend/app/foo.py"
  original = path.read_text()
  staged = "VALUE = 'cached-only before capture process death'\n"
  path.write_text(staged)
  _git(platform, "add", "backend/app/foo.py")
  path.write_text(original)
  raw_index = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  capture = app_git.commit_local

  def die(repo, message):
    if after:
      capture(repo, message)
    raise _Killed("platform capture")

  monkeypatch.setattr(app_git, "commit_local", die)
  with pytest.raises(_Killed):
    pu._carry_working_edits(platform, pu._local_branch(platform))
  refs = pu.platform_status(platform)["recovery_refs"]
  assert not pu.ROLLED_BACK_FLAG.exists()
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  assert any(_git(platform, "show", ref + ":stage-0/backend/app/foo.py", check=False).stdout == staged
             and _git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == raw_index
             for ref in refs)
  assert path.read_text() == original


@pytest.mark.parametrize("disjoint_update", [False, True])
def test_initial_conflict_stages_and_flags_are_kept_before_capture_or_abort(clone_env, disjoint_update):
  origin, platform = clone_env
  _git(platform, "checkout", "-q", "-b", "other")
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'other'\n"})
  _git(platform, "checkout", "-q", "main")
  pre = _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'main'\n"})
  assert _git(platform, "merge", "other", check=False).returncode == 1
  _git(platform, "update-index", "--assume-unchanged", "backend/app/__init__.py")
  stages = _git(platform, "ls-files", "--stage", "backend/app/foo.py").stdout.splitlines()
  raw_index = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  working = (platform / "backend/app/foo.py").read_text()

  if disjoint_update:
    _advance_origin(origin, edits={"readme.txt": "disjoint release while merge is interrupted\n"})
    assert pu.reconcile_clone(platform).status == "updated"
    assert not pu._merge_in_progress(platform)
  else:
    carried = pu._carry_working_edits(platform, pu._local_branch(platform))
    assert carried.pre == carried.served == pre and carried.working is None
    assert _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip() == raw_index
  refs = pu.platform_status(platform)["recovery_refs"]
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  saved = [ref for ref in refs if _git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == raw_index]
  assert saved
  assert len(stages) == 3
  for entry in stages:
    metadata, path = entry.split("\t", 1)
    _mode, oid, stage = metadata.split()
    assert _git(platform, "rev-parse", f"{saved[0]}:stage-{stage}/{path}").stdout.strip() == oid
  assert (platform / "backend/app/foo.py").read_text() == ("VALUE = 'main'\n" if disjoint_update else working)
  assert any(_git(platform, "show", ref + ":backend/app/foo.py", check=False).stdout == working for ref in refs)


def test_forced_revert_preserves_a_newer_branch_owner_after_journal_admission(clone_env, monkeypatch):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  preserve = pu._set_aside_unsaved_update_work
  advanced = {}

  def newer_writer(repo, local, swapped):
    refs = preserve(repo, local, swapped)
    advanced["head"] = _local_commit(platform, edits={"newer.txt": "newer writer source\n"})
    advanced["index"] = (platform / ".git/index").read_bytes()
    return refs

  monkeypatch.setattr(pu, "_set_aside_unsaved_update_work", newer_writer)
  with pytest.raises(pu.BootTransactionError, match="branch changed"):
    pu.revert_failed_update(platform)
  assert _served_sha(platform) == advanced["head"]
  assert (platform / ".git/index").read_bytes() == advanced["index"]
  assert (platform / "newer.txt").read_text() == "newer writer source\n"
  assert pu.RECONCILE_PRE_FLAG.exists()
  assert pu.read_prepared_update()["state"] == "swapped"
  monkeypatch.setattr(pu, "_set_aside_unsaved_update_work", preserve)
  with pytest.raises(pu.BootTransactionError, match="branch changed"):
    pu.boot_guard_clean_served_tree(platform)
  assert _served_sha(platform) == advanced["head"]
  assert (platform / ".git/index").read_bytes() == advanced["index"]


@pytest.mark.parametrize("after", [False, True])
def test_boot_checkout_receipt_survives_death_before_marker_retirement(clone_env, monkeypatch, after):
  _origin, platform = clone_env
  pre = _served_sha(platform)
  original = (platform / "backend/app/foo.py").read_text()
  staged = "VALUE = 'boot cached-only recovery'\n"
  (platform / "backend/app/foo.py").write_text(staged)
  _git(platform, "add", "backend/app/foo.py")
  (platform / "backend/app/foo.py").write_text(original)
  raw_index = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  pu._write_reconcile_pre(pre, pre)
  _kill_once(monkeypatch, "_write_rolled_back_flag", after=after)
  with pytest.raises(_Killed):
    pu.boot_guard_clean_served_tree(platform)
  assert pu.RECONCILE_PRE_FLAG.exists()
  refs = pu.platform_status(platform)["recovery_refs"]
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  pu.boot_guard_clean_served_tree(platform)
  receipt = pu._read_rolled_back_flag()["error"]
  assert all(ref in receipt for ref in refs)
  assert any(_git(platform, "show", ref + ":stage-0/backend/app/foo.py", check=False).stdout == staged
             and _git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == raw_index
             for ref in refs)
  assert not pu.RECONCILE_PRE_FLAG.exists()


@pytest.mark.parametrize("entry", ["revert_failed_update", "complete_platform_swap"])
@pytest.mark.parametrize("after", [False, True])
def test_every_swap_settlement_entry_resumes_the_owning_revert_receipt(
  clone_env, monkeypatch, entry, after,
):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  original = (platform / "backend/app/foo.py").read_text()
  staged = "VALUE = 'staged before settlement entry death'\n"
  (platform / "backend/app/foo.py").write_text(staged)
  _git(platform, "add", "backend/app/foo.py")
  (platform / "backend/app/foo.py").write_text(original)
  _kill_once(monkeypatch, "_write_prepared_update", after=after)
  with pytest.raises(_Killed):
    pu.revert_failed_update(platform)
  refs = pu.platform_status(platform)["recovery_refs"]
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")

  assert getattr(pu, entry)(platform) == (True if entry == "revert_failed_update" else "reverted")

  receipt = pu.platform_status(platform)["rollback_error"]
  assert all(ref in receipt for ref in refs)
  assert not pu.RECONCILE_PRE_FLAG.exists()
  assert pu.read_prepared_update()["state"] == "prepared"
  assert (platform / "backend/app/foo.py").read_text() == original
  assert any(_git(platform, "show", ref + ":stage-0/backend/app/foo.py", check=False).stdout == staged for ref in refs)


@pytest.mark.parametrize("working", [False, True])
def test_ordinary_platform_capture_needs_no_private_index_recovery_or_rollback(clone_env, working):
  origin, platform = clone_env
  before = _served_sha(platform)
  if working:
    (platform / "backend/app/foo.py").write_text("VALUE = 'ordinary unstaged edit'\n")
  target = _advance_origin(origin, edits={"readme.txt": "independent release\n"})

  result = pu.reconcile_clone(platform)

  assert result.status == "updated"
  assert _served_sha(platform) == target
  assert pu.platform_status(platform)["recovery_refs"] == []
  assert not pu.ROLLED_BACK_FLAG.exists()
  assert not pu.RECONCILE_PRE_FLAG.exists()
  assert (platform / "backend/app/foo.py").read_text() == (
    "VALUE = 'ordinary unstaged edit'\n" if working else _FOO_PY
  )
  assert pu._is_ancestor(platform, before, _served_sha(platform))


def test_forced_checkout_cas_refuses_a_writer_after_snapshot_before_branch_move(clone_env, monkeypatch):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  git = pu._git
  advanced = {}

  def writer_before_cas(*args, **kwargs):
    if not advanced and args[:2] == ("update-ref", "refs/heads/main"):
      advanced["head"] = _local_commit(platform, edits={"newer.txt": "a newer branch owner\n"})
      advanced["index"] = (platform / ".git/index").read_bytes()
    return git(*args, **kwargs)

  monkeypatch.setattr(pu, "_git", writer_before_cas)
  with pytest.raises(subprocess.CalledProcessError):
    pu.revert_failed_update(platform)
  assert _served_sha(platform) == advanced["head"]
  assert (platform / ".git/index").read_bytes() == advanced["index"]
  assert (platform / "newer.txt").read_text() == "a newer branch owner\n"
  assert pu.RECONCILE_PRE_FLAG.exists()
  assert pu.read_prepared_update()["state"] == "swapped"


def test_boot_forced_checkout_keeps_marker_free_conflict_stages_and_hidden_flags(clone_env):
  _origin, platform = clone_env
  _git(platform, "checkout", "-q", "-b", "other")
  _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'other'\n"})
  _git(platform, "checkout", "-q", "main")
  pre = _local_commit(platform, edits={"backend/app/foo.py": "VALUE = 'main'\n"})
  assert _git(platform, "merge", "other", check=False).returncode == 1
  # An unmerged index may survive without a sequencer (e.g. read-tree -m).
  (platform / ".git/MERGE_HEAD").unlink()
  _git(platform, "update-index", "--skip-worktree", "backend/app/__init__.py")
  (platform / "backend/app/__init__.py").write_text("raise ImportError('hidden owner work')\n")
  stages = _git(platform, "ls-files", "--stage", "backend/app/foo.py").stdout.splitlines()
  raw_index = _git(platform, "hash-object", str(platform / ".git/index")).stdout.strip()
  pu._write_reconcile_pre(pre, pre)

  pu.boot_guard_clean_served_tree(platform)

  refs = pu.platform_status(platform)["recovery_refs"]
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  saved = [ref for ref in refs if _git(platform, "rev-parse", ref + ":original-index", check=False).stdout.strip() == raw_index]
  assert saved
  for entry in stages:
    metadata, path = entry.split("\t", 1)
    _mode, oid, stage = metadata.split()
    assert _git(platform, "rev-parse", f"{saved[0]}:stage-{stage}/{path}").stdout.strip() == oid
  assert (platform / "backend/app/foo.py").read_text() == "VALUE = 'main'\n"
  assert (platform / "backend/app/__init__.py").read_text() == ""
  assert _git(platform, "ls-files", "-v", "backend/app/__init__.py").stdout.startswith("H ")
  assert not _git(platform, "ls-files", "--unmerged").stdout
  assert not pu.RECONCILE_PRE_FLAG.exists()
  ok, error = pu._import_probe(platform)
  assert ok, error


@pytest.mark.parametrize("history", ["change_revert", "empty"])
@pytest.mark.parametrize("boot_state", ["once", "repeat", "missing_tree"])
def test_image_revert_keeps_same_tree_owner_commits_reachable_after_gc(
  clone_env, history, boot_state,
):
  """Content equality cannot stand in for the identity of owner history."""
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  booted = pu.read_prepared_update()
  path = platform / "backend/app/foo.py"
  original = path.read_text()
  commits = []
  if history == "change_revert":
    commits.append(_local_commit(platform, edits={
      "backend/app/foo.py": "VALUE = 'owner history after boot'\n",
    }))
    commits.append(_local_commit(platform, edits={"backend/app/foo.py": original}))
  else:
    _git(platform, "commit", "-q", "--allow-empty", "-m", "owner checkpoint after boot")
    commits.append(_served_sha(platform))
  assert pu._working_tree_oid(platform, _served_sha(platform)) == booted["booted_tree"]
  if boot_state != "once":
    if boot_state == "missing_tree":
      # An older/incomplete record may know the merge-back without its tree.
      pu._write_prepared_update({**pu.read_prepared_update(), "booted_tree": None})
    assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
    assert pu.read_prepared_update()["replayed"] == booted["replayed"], (
      "a repeat boot must not redefine new owner commits as the update's merge-back"
    )
  _boot_image(record["snapshot"])
  assert pu.settle_prepared_update_for_this_image(platform) == "reverted"
  refs = pu.platform_status(platform)["recovery_refs"]
  receipt = pu.platform_status(platform)["rollback_error"]
  assert refs and all(ref in receipt for ref in refs)
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  for commit in commits:
    assert _git(platform, "cat-file", "-t", commit).stdout.strip() == "commit"
    assert any(pu._is_ancestor(platform, commit, _git(platform, "rev-parse", ref).stdout.strip())
               for ref in refs), "owner history must have a named GC-durable recovery root"
  assert pu.read_prepared_update()["state"] == "prepared"
  assert not pu.RECONCILE_PRE_FLAG.exists()
  assert path.read_text() == original


def test_platform_status_recovery_details_are_additive_to_existing_response_contract(clone_env):
  """An optional diagnostic must not reject an otherwise valid old producer."""
  from pydantic import TypeAdapter

  _origin, platform = clone_env
  status = pu.platform_status(platform)
  assert status["recovery_refs"] == []
  existing_response = {key: value for key, value in status.items() if key != "recovery_refs"}
  adapter = TypeAdapter(pu.PlatformStatus)
  assert adapter.validate_python(existing_response) == existing_response
  with_details = {**existing_response, "recovery_refs": ["refs/mobius/set-aside/synthetic-recovery"]}
  assert adapter.validate_python(with_details)["recovery_refs"] == with_details["recovery_refs"]
  assert "recovery_refs" not in adapter.json_schema().get("required", [])


@pytest.mark.parametrize("index_kind", ["cached_only", "skip_worktree", "assume_unchanged", "intent_to_add"])
@pytest.mark.parametrize("death", [None, "snapshot", "removal"])
def test_image_revert_keeps_all_displaced_resolver_inputs_after_gc_and_death(
  clone_env, monkeypatch, index_kind, death,
):
  """Removing a resolver displaces its index and ignored files, not just source."""
  origin, platform = clone_env
  record, resolver = _bound_late_conflict(platform, origin, uncommitted=False)
  original = (resolver / "backend/app/foo.py").read_text()
  staged = "VALUE = 'cached-only resolver owner input'\n"
  if index_kind == "intent_to_add":
    (resolver / "unfinished.txt").write_text("resolver intent to add\n")
    _git(resolver, "add", "-N", "unfinished.txt")
    stage_path = "unfinished.txt"
  else:
    (resolver / "backend/app/foo.py").write_text(staged)
    _git(resolver, "add", "backend/app/foo.py")
    (resolver / "backend/app/foo.py").write_text(original)
    stage_path = "backend/app/foo.py"
    if index_kind != "cached_only":
      _git(resolver, "update-index", "--" + index_kind.replace("_", "-"), stage_path)
  blob = _git(resolver, "rev-parse", ":" + stage_path).stdout.strip()
  raw_path = _git(resolver, "rev-parse", "--git-path", "index").stdout.strip()
  raw_index = _git(resolver, "hash-object", raw_path).stdout.strip()
  # The original resolver conflict stages are also owner inputs to recovery.
  stages = _git(resolver, "ls-files", "--stage", "backend/requirements.lock").stdout.splitlines()
  assert {entry.split("\t", 1)[0].split()[2] for entry in stages} >= {"2", "3"}
  (platform / ".git/info/exclude").write_text("resolver-only.bin\n")
  ignored_bytes = b"resolver ignored owner bytes\x00\xff\n"
  (resolver / "resolver-only.bin").write_bytes(ignored_bytes)
  assert _git(resolver, "check-ignore", "resolver-only.bin").returncode == 0
  if death == "snapshot":
    _kill_once(monkeypatch, "_set_aside_resolver_work", after=True)
  elif death == "removal":
    remove = app_git.remove_overlay_worktree
    killed = False

    def killed_after_removal(*args, **kwargs):
      nonlocal killed
      remove(*args, **kwargs)
      if not killed:
        killed = True
        raise _Killed("resolver removed")

    monkeypatch.setattr(app_git, "remove_overlay_worktree", killed_after_removal)
  _boot_image(record["snapshot"])
  if death:
    with pytest.raises(_Killed):
      pu.settle_prepared_update_for_this_image(platform)
    assert pu.RECONCILE_PRE_FLAG.exists()
  assert pu.settle_prepared_update_for_this_image(platform) == "reverted"
  assert not resolver.exists()
  refs = pu.platform_status(platform)["recovery_refs"]
  receipt = pu.platform_status(platform)["rollback_error"]
  assert refs and all(ref in receipt for ref in refs)
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  saved_indexes = [ref for ref in refs if _git(
    platform, "rev-parse", ref + ":original-index", check=False,
  ).stdout.strip() == raw_index]
  assert saved_indexes, "resolver removal needs an exact reported, GC-reachable index copy"
  assert _git(platform, "cat-file", "-e", blob, check=False).returncode == 0
  for ref in saved_indexes:
    assert _git(platform, "rev-parse", ref + ":stage-0/" + stage_path).stdout.strip() == blob
    for entry in stages:
      metadata, path = entry.split("\t", 1)
      _mode, oid, stage = metadata.split()
      assert _git(platform, "rev-parse", f"{ref}:stage-{stage}/{path}").stdout.strip() == oid
  assert any(subprocess.run(
    ["git", "-C", str(platform), "show", ref + ":resolver-only.bin"], capture_output=True,
  ).stdout == ignored_bytes for ref in refs), "ignored resolver owner bytes must survive deletion"
  assert pu.read_prepared_update()["state"] == "prepared"
  assert not pu.RECONCILE_PRE_FLAG.exists()


def test_failed_resolver_preservation_leaves_its_worktree_and_index_intact(clone_env, monkeypatch):
  origin, platform = clone_env
  record, resolver = _bound_late_conflict(platform, origin, uncommitted=False)
  raw_index = Path(_git(resolver, "rev-parse", "--git-path", "index").stdout.strip()).read_bytes()
  working = (resolver / "backend/requirements.lock").read_bytes()
  preserve = pu._preserve_checkout_state

  def refuse_resolver(repo, *args, **kwargs):
    if repo == resolver:
      raise pu.PlatformUpdateError("resolver preservation unavailable")
    return preserve(repo, *args, **kwargs)

  monkeypatch.setattr(pu, "_preserve_checkout_state", refuse_resolver)
  _boot_image(record["snapshot"])
  with pytest.raises(pu.PlatformUpdateError, match="resolver preservation unavailable"):
    pu.settle_prepared_update_for_this_image(platform)
  assert resolver.exists()
  assert Path(_git(resolver, "rev-parse", "--git-path", "index").stdout.strip()).read_bytes() == raw_index
  assert (resolver / "backend/requirements.lock").read_bytes() == working
  assert pu.RECONCILE_PRE_FLAG.exists()
  assert pu._read_conflict_flag()["overlay"]["worktree"] == str(resolver)


@pytest.mark.parametrize("direction", ["file_to_directory", "directory_to_file"])
@pytest.mark.parametrize("gate", ["pass", "frontend_failure"])
def test_native_directory_replacement_keeps_final_gate_and_recovery_contract(
  clone_env, monkeypatch, direction, gate,
):
  origin, platform = clone_env
  old_path = "layout" if direction == "file_to_directory" else "layout/module.py"
  new_path = "layout/module.py" if direction == "file_to_directory" else "layout"
  _advance_origin(origin, edits={old_path: "previous layout source\n"})
  assert pu.reconcile_clone(platform).status == "updated"
  before = _served_sha(platform)
  source = origin.parent / "origin-work"
  (source / old_path).unlink()
  if direction == "directory_to_file":
    (source / "layout").rmdir()
  target = _advance_origin(origin, edits={
    new_path: "candidate layout source\n", "frontend/src/App.jsx": "export default 'candidate';\n",
    "backend/app/foo.py": "VALUE = 'candidate directory update'\n",
  })
  observed = {}
  staged = "VALUE = 'late staging at directory final gate'\n"
  working = "VALUE = 'late working at directory final gate'\n"

  def final_gate(repo, result):
    observed["called"] = True
    assert pu.RECONCILE_PRE_FLAG.exists()
    if gate == "frontend_failure":
      (platform / "backend/app/foo.py").write_text(staged)
      _git(platform, "add", "backend/app/foo.py")
      (platform / "backend/app/foo.py").write_text(working)
      raise RuntimeError("directory update frontend rejected")

  monkeypatch.setattr(pu, "_rebuild_frontend", final_gate)
  result = pu.reconcile_clone(platform)
  assert observed.get("called"), "a valid D/F checkout must reach its final gate"
  if gate == "pass":
    assert result.status == "updated", result.error
    assert _served_sha(platform) == target
    assert (platform / new_path).read_text() == "candidate layout source\n"
  else:
    assert result.status == "error"
    assert _served_sha(platform) == before
    assert pu.RECONCILE_PRE_FLAG.exists()
    pu.boot_guard_clean_served_tree(platform)
    assert (platform / old_path).read_text() == "previous layout source\n"
    refs = pu.platform_status(platform)["recovery_refs"]
    _git(platform, "reflog", "expire", "--expire=now", "--all")
    _git(platform, "gc", "--prune=now")
    assert any(_git(platform, "show", ref + ":stage-0/backend/app/foo.py", check=False).stdout == staged
               for ref in refs)
    assert any(_git(platform, "show", ref + ":backend/app/foo.py", check=False).stdout == working
               for ref in refs)
  assert not pu.RECONCILE_PRE_FLAG.exists()


@pytest.mark.parametrize("leftover", ["ignored_file", "directory_symlink"])
def test_native_directory_coherence_still_rejects_retired_files_and_symlinks(clone_env, leftover):
  _origin, platform = clone_env
  before = _local_commit(platform, edits={"layout": "old leaf\n"})
  _git(platform, "rm", "-q", "layout")
  target = _local_commit(platform, edits={"readme.txt": "release without layout\n"})
  (platform / ".git/info/exclude").write_text("layout\n")
  if leftover == "ignored_file":
    (platform / "layout").write_text("retired candidate bytes\n")
  else:
    (platform / "layout").symlink_to(platform / "backend", target_is_directory=True)
  assert not pu._checkout_matches_transition_target(platform, target, before)


@pytest.mark.parametrize("checkout", ["served", "resolver"])
@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_saved_split_index_closure_is_portable_after_image_revert_and_gc(
  clone_env, tmp_path, checkout, index_flag,
):
  origin, platform = clone_env
  if checkout == "resolver":
    record, source = _bound_late_conflict(platform, origin, uncommitted=False)
  else:
    record = _prepare_package_update(platform, origin)
    _boot_image(record["target"])
    assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
    source = platform
  path = "backend/app/foo.py"
  _git(source, "update-index", index_flag, path)
  _git(source, "update-index", "--split-index")
  shared = Path(_git(source, "rev-parse", "--shared-index-path").stdout.strip())
  if not shared.is_absolute():
    shared = source / shared
  assert shared.is_file()
  companion = shared.read_bytes()
  raw_path = Path(_git(source, "rev-parse", "--git-path", "index").stdout.strip())
  if not raw_path.is_absolute():
    raw_path = source / raw_path
  raw_oid = _git(source, "hash-object", str(raw_path)).stdout.strip()
  flags = _git(source, "ls-files", "-v", path).stdout
  _boot_image(record["snapshot"])
  assert pu.settle_prepared_update_for_this_image(platform) == "reverted"
  if checkout == "resolver":
    assert not source.exists() and not shared.exists()
  refs = pu.platform_status(platform)["recovery_refs"]
  _git(platform, "reflog", "expire", "--expire=now", "--all")
  _git(platform, "gc", "--prune=now")
  saved = next(ref for ref in refs if _git(
    platform, "rev-parse", ref + ":original-index", check=False,
  ).stdout.strip() == raw_oid)
  # Loading while the original companion still exists is not recovery.
  # A fresh repo proves the reported copy carries its own index dependencies.
  recovered = tmp_path / "recovered-index"
  recovered.mkdir()
  _git(recovered, "init", "-q")
  def saved_bytes(name):
    return subprocess.run(
      ["git", "-C", str(platform), "show", saved + ":" + name],
      capture_output=True, check=True,
    ).stdout
  (recovered / ".git/index").write_bytes(saved_bytes("original-index"))
  assert _git(recovered, "ls-files", "-v", path, check=False).returncode == 128
  assert _git(platform, "cat-file", "-e", saved + ":" + shared.name, check=False).returncode == 0, (
    "exact index recovery requires its GC-durable shared-index companion"
  )
  assert saved_bytes(shared.name) == companion
  (recovered / ".git" / shared.name).write_bytes(saved_bytes(shared.name))
  assert _git(recovered, "ls-files", "-v", path).stdout == flags


def test_failed_split_index_companion_capture_refuses_resolver_removal(clone_env, monkeypatch):
  origin, platform = clone_env
  record, resolver = _bound_late_conflict(platform, origin, uncommitted=False)
  _git(resolver, "update-index", "--assume-unchanged", "backend/app/foo.py")
  _git(resolver, "update-index", "--split-index")
  raw_path = Path(_git(resolver, "rev-parse", "--git-path", "index").stdout.strip())
  raw = raw_path.read_bytes()
  git = pu._git

  def cannot_save_companion(*args, **kwargs):
    if args[0] == "hash-object" and Path(args[-1]).name.startswith("sharedindex."):
      raise pu.PlatformUpdateError("shared index snapshot unavailable")
    return git(*args, **kwargs)

  monkeypatch.setattr(pu, "_git", cannot_save_companion)
  _boot_image(record["snapshot"])
  with pytest.raises(pu.PlatformUpdateError, match="shared index snapshot unavailable"):
    pu.settle_prepared_update_for_this_image(platform)
  assert resolver.exists()
  assert raw_path.read_bytes() == raw
  assert pu.RECONCILE_PRE_FLAG.exists()


def test_forced_revert_refuses_detached_owner_history_before_journal_admission(clone_env):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  _git(platform, "checkout", "--detach", "HEAD")
  _local_commit(platform, edits={"detached-owner.txt": "owner work\n"})
  detached = _served_sha(platform)
  index = (platform / ".git/index").read_bytes()
  with pytest.raises(pu.BootTransactionError, match="HEAD changed"):
    pu.revert_failed_update(platform)
  assert _git(platform, "rev-parse", "HEAD").stdout.strip() == detached
  assert pu._head_detached(platform)
  assert (platform / ".git/index").read_bytes() == index
  assert not pu.RECONCILE_PRE_FLAG.exists()
  assert pu.read_prepared_update()["state"] == "swapped"


@pytest.mark.parametrize("marker", ["journal", "{}", "[]"])
def test_forced_revert_refuses_another_checkout_journal_without_overwriting_it(clone_env, marker):
  origin, platform = clone_env
  record = _prepare_package_update(platform, origin)
  _boot_image(record["target"])
  assert pu.settle_prepared_update_for_this_image(platform) == "replayed"
  head = _served_sha(platform)
  pu._write_reconcile_pre(head, head, saved_refs=["refs/mobius/other-owner"])
  if marker != "journal":
    pu.RECONCILE_PRE_FLAG.write_text(marker)
  journal = pu.RECONCILE_PRE_FLAG.read_bytes()
  index = (platform / ".git/index").read_bytes()
  with pytest.raises(pu.BootTransactionError, match="Another checkout transaction"):
    pu.revert_failed_update(platform)
  assert pu.RECONCILE_PRE_FLAG.read_bytes() == journal
  assert _served_sha(platform) == head
  assert (platform / ".git/index").read_bytes() == index
  assert pu.read_prepared_update()["state"] == "swapped"
