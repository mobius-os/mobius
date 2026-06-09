"""Per-app git module — init, upstream recording, and the merge verdict.

These exercise `app_git` directly against a throwaway repo in `tmp_path`
(no DB, no HTTP, no install endpoint) so the git plumbing is pinned in
isolation. The clean/conflict cases are the load-bearing ones: a clean
merge must hand back the merged bytes and a conflict must name the file
WITHOUT touching the working tree.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import app_git


def _write(repo: Path, text: str) -> None:
  (repo / "index.jsx").write_text(text, encoding="utf-8")


def test_ensure_repo_is_idempotent_and_creates_branches(tmp_path):
  """ensure_repo inits the repo with upstream + main branches and is a
  no-op on a second call."""
  repo = tmp_path / "app"
  assert not app_git.is_repo(repo)
  app_git.ensure_repo(repo)
  assert app_git.is_repo(repo)
  # Both branches resolve to a real commit.
  up = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  main = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  assert up and main
  # Second call must not error or rewrite history.
  app_git.ensure_repo(repo)
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == main


def test_ensure_repo_creates_nested_repo_inside_parent_worktree(tmp_path):
  """A source dir inside a larger git worktree still gets its own .git.

  Regression: `git rev-parse --is-inside-work-tree` is true in this
  shape via the parent repo, but per-app git needs a dedicated nested
  repo at source_dir/.git.
  """
  parent = tmp_path / "data"
  repo = parent / "apps" / "news"
  repo.mkdir(parents=True)
  subprocess.run(["git", "-C", str(parent), "init", "-q"], check=True)

  assert not (repo / ".git").exists()
  assert subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip() == "true"

  app_git.ensure_repo(repo)

  assert app_git.is_repo(repo)
  assert subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip() == str(repo)
  assert app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH)


def test_ensure_repo_preserves_existing_app_files_in_parent_worktree(tmp_path):
  """First-time init around an existing app source tree must be non-destructive.

  Production repair may run `ensure_repo` for installed apps that already have
  source files under /data/apps/<slug>, while /data itself is a git worktree.
  The per-app init must create source_dir/.git without deleting or rewriting
  the app's existing files.
  """
  parent = tmp_path / "data"
  repo = parent / "apps" / "news"
  repo.mkdir(parents=True)
  subprocess.run(["git", "-C", str(parent), "init", "-q"], check=True)
  index = repo / "index.jsx"
  job = repo / "fetch.sh"
  index.write_text("export default function News() { return <h1>News</h1>; }\n")
  job.write_text("#!/usr/bin/env bash\nprintf 'ok\\n'\n")

  app_git.ensure_repo(repo)

  assert app_git.is_repo(repo)
  assert index.read_text() == (
    "export default function News() { return <h1>News</h1>; }\n"
  )
  assert job.read_text() == "#!/usr/bin/env bash\nprintf 'ok\\n'\n"
  assert app_git._run(repo, "status", "--porcelain").stdout.splitlines() == [
    "?? fetch.sh",
    "?? index.jsx",
  ]


def test_run_does_not_leak_to_enclosing_repo(tmp_path):
  """A per-app op must never resolve to an ENCLOSING repo (the /data-is-a-git-
  repo trap). A source dir inside a parent worktree but with no dedicated .git
  must not let `git -C` walk up — the GIT_CEILING_DIRECTORIES pin in _run stops
  the search at the app-dir's parent, so the op fails cleanly instead of
  silently operating on the wrong (parent) repo. This is what made the prod
  News app's updates spuriously conflict against /data."""
  parent = tmp_path / "data"
  sub = parent / "apps" / "news"
  sub.mkdir(parents=True)
  subprocess.run(["git", "-C", str(parent), "init", "-q"], check=True)
  res = app_git._run(sub, "rev-parse", "--show-toplevel", check=False)
  assert res.returncode != 0, f"leaked to enclosing repo: {res.stdout!r}"
  assert str(parent) not in res.stdout


def test_ensure_repo_does_not_reinit_existing_per_app_repo(tmp_path):
  """An existing per-app repo with main + upstream history is untouched."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, b"INSTALLED V1\n", "https://x/mobius.json", "1.0.0",
  )
  app_git.align_local_to_upstream(repo)
  _write(repo, "LOCAL V1\n")
  local_commit = app_git.commit_local(repo, "local edit")
  assert local_commit is not None
  upstream_commit = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  branch_list = subprocess.run(
    ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.splitlines()

  app_git.ensure_repo(repo)

  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == local_commit
  assert app_git.head_sha(repo, app_git.UPSTREAM_BRANCH) == upstream_commit
  assert subprocess.run(
    ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
    capture_output=True,
    text=True,
    check=True,
  ).stdout.splitlines() == branch_list
  assert (repo / "index.jsx").read_text() == "LOCAL V1\n"


def test_record_upstream_commits_pristine_bytes_without_touching_worktree(
  tmp_path,
):
  """record_upstream advances `upstream` but leaves the checked-out
  working tree (on main) untouched."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  _write(repo, "LOCAL EDIT")
  app_git.commit_local(repo, "local edit")
  before = (repo / "index.jsx").read_text()

  sha = app_git.record_upstream(
    repo, b"UPSTREAM V2", "https://x/mobius.json", "2.0.0",
  )
  assert sha == app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  # The working tree (main) still holds the local edit — recording the
  # upstream version must not check out the upstream bytes.
  assert (repo / "index.jsx").read_text() == before == "LOCAL EDIT"


def test_record_upstream_stages_job_script_on_upstream_tree(tmp_path):
  """record_upstream commits the schedule job script into the `upstream`
  tree alongside index.jsx (when job_name + job_bytes are given), so a
  later update can three-way-merge a locally edited job. The checked-out
  `main` working tree is left untouched — only `upstream` advances."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  _write(repo, "LOCAL EDIT")
  app_git.commit_local(repo, "local edit")
  before = (repo / "index.jsx").read_text()

  app_git.record_upstream(
    repo, b"UPSTREAM V2", "https://x/mobius.json", "2.0.0",
    job_name="fetch.sh", job_bytes=b"#!/bin/bash\necho upstream\n",
  )

  # The job script is in the upstream tree...
  job_blob = subprocess.run(
    ["git", "-C", str(repo), "cat-file", "blob",
     f"{app_git.UPSTREAM_BRANCH}:fetch.sh"],
    env=app_git._git_env(repo), capture_output=True, check=True,
  ).stdout
  assert job_blob == b"#!/bin/bash\necho upstream\n"
  # ...and the working tree (on main) is untouched — no fetch.sh appeared,
  # and index.jsx still holds the local edit.
  assert not (repo / "fetch.sh").exists()
  assert (repo / "index.jsx").read_text() == before == "LOCAL EDIT"


def test_merge_clean_carries_local_job_edit_forward(tmp_path):
  """A locally edited job script flows through a clean merge: merge_upstream
  returns merged_job carrying the local edit when an upstream v2 changes a
  DISJOINT region of the same script."""
  repo = tmp_path / "app"
  base_jsx = b"export default () => null\n"
  base_job = "#!/bin/bash\nstep one\nstep two\nstep three\nstep four\nstep five\n"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, base_jsx, "https://x/mobius.json", "1.0.0",
    job_name="fetch.sh", job_bytes=base_job.encode(),
  )
  app_git.align_local_to_upstream(repo)

  # Agent edits the FIRST step of the job locally on main.
  (repo / "fetch.sh").write_text(
    base_job.replace("step one", "step ONE LOCAL")
  )
  app_git.commit_local(repo, "local job edit")
  # Upstream v2 edits the LAST step — disjoint from the local change.
  app_git.record_upstream(
    repo, base_jsx, "https://x/mobius.json", "2.0.0",
    job_name="fetch.sh",
    job_bytes=base_job.replace("step five", "step FIVE UPSTREAM").encode(),
  )

  result = app_git.merge_upstream(repo, job_name="fetch.sh")
  assert result.status == "clean"
  assert result.merged_job is not None
  merged_job = result.merged_job.decode()
  assert "step ONE LOCAL" in merged_job       # local edit carried forward
  assert "step FIVE UPSTREAM" in merged_job   # upstream change applied


def _job_mode(repo: Path, ref: str, name: str) -> str:
  """The recorded file mode (e.g. `100755`) of `name` on `ref`, or '' when
  the file is absent in that tree. Used to assert upstream and main agree
  on the executable bit so the merge never sees a spurious mode skew."""
  out = subprocess.run(
    ["git", "-C", str(repo), "ls-tree", ref, name],
    env=app_git._git_env(repo), capture_output=True, text=True, check=True,
  ).stdout
  return out.split()[0] if out.strip() else ""


def test_merge_clean_when_only_the_job_exec_bit_differs(tmp_path):
  """An exec-bit-only difference on a job script must merge CLEAN, not conflict.

  Regression for the 2026-06-08 incident (News, LaTeX, Notes, Web Studio all
  fired spurious "merge conflict" chats at once). The install path writes job
  scripts EXECUTABLE on disk (cron runs the bare path, which needs +x), so
  `commit_local` records them at 100755 on `main`. When an app's earliest
  install predated job-script tracking, the merge base has no job script, so the
  v2 update is an ADD/ADD of `fetch.sh`: 100755 on `main` vs whatever
  `record_upstream` stages on `upstream`. With IDENTICAL bytes the only delta is
  the mode — git reports CONFLICT (add/add) when the modes disagree. The fix
  records the job at the same 100755 the local side uses, so the modes match and
  the merge is clean.
  """
  repo = tmp_path / "app"
  base_jsx = b"export default () => null\n"
  job = b"#!/bin/bash\necho hi\n"
  app_git.ensure_repo(repo)
  # v1 install predates job-script tracking — no job recorded on upstream, so
  # the merge base carries no fetch.sh.
  app_git.record_upstream(repo, base_jsx, "https://x/mobius.json", "1.0.0")
  app_git.align_local_to_upstream(repo)
  # The agent's job script lands on disk EXECUTABLE (the install chmod 0o755),
  # and commit_local records it at 100755 on main.
  (repo / "fetch.sh").write_bytes(job)
  os.chmod(repo / "fetch.sh", 0o755)
  app_git.commit_local(repo, "agent adds executable job")
  # v2 update now tracks the job with IDENTICAL bytes. The only possible delta
  # against main is the recorded mode.
  app_git.record_upstream(
    repo, base_jsx, "https://x/mobius.json", "2.0.0",
    job_name="fetch.sh", job_bytes=job,
  )

  # Both branches must record the job EXECUTABLE so no mode skew exists.
  assert _job_mode(repo, app_git.LOCAL_BRANCH, "fetch.sh") == "100755"
  assert _job_mode(repo, app_git.UPSTREAM_BRANCH, "fetch.sh") == "100755"

  result = app_git.merge_upstream(repo, job_name="fetch.sh")
  assert result.status == "clean"
  assert not result.conflict_paths


def test_record_upstream_keeps_index_jsx_non_executable(tmp_path):
  """Only job scripts gain the exec bit on upstream — index.jsx stays 100644.

  The skew fix targets job scripts (cron runs them by path), not the JSX
  entry, which is read by the compiler and never executed. Recording it
  executable would create the inverse skew against main's 100644.
  """
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, b"export default () => null\n", "https://x/mobius.json", "1.0.0",
    job_name="fetch.sh", job_bytes=b"#!/bin/bash\necho hi\n",
  )

  assert _job_mode(repo, app_git.UPSTREAM_BRANCH, "index.jsx") == "100644"
  assert _job_mode(repo, app_git.UPSTREAM_BRANCH, "fetch.sh") == "100755"


def test_recorded_job_checks_out_executable(tmp_path):
  """A job recorded on upstream lands EXECUTABLE on disk after a checkout.

  cron runs the bare job path (`init-cron-scaffold.sh` puts `<path> [id]` in the
  crontab), so the script MUST keep its +x through an install/update. Because
  record_upstream stamps the job 100755, the align-to-upstream checkout restores
  it executable on its own — the executability rides in git, not only in the
  install path's explicit chmod.
  """
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, b"export default () => null\n", "https://x/mobius.json", "1.0.0",
    job_name="fetch.sh", job_bytes=b"#!/bin/bash\necho hi\n",
  )
  app_git.align_local_to_upstream(repo)

  assert (repo / "fetch.sh").stat().st_mode & stat.S_IXUSR


def test_merge_clean_without_job_name_leaves_merged_job_none(tmp_path):
  """Omitting job_name keeps merged_job None — the job script is only read
  back when the caller asks for it (a manifest with no schedule.job)."""
  repo = tmp_path / "app"
  base = "line A\nline B\nline C\n"
  app_git.ensure_repo(repo)
  app_git.record_upstream(repo, base.encode(), "https://x/mobius.json", "1.0.0")
  app_git.align_local_to_upstream(repo)
  _write(repo, "line A LOCAL\nline B\nline C\n")
  app_git.commit_local(repo, "local edit")
  app_git.record_upstream(
    repo, b"line A\nline B\nline C UPSTREAM\n", "https://x/mobius.json", "2.0.0",
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "clean"
  assert result.merged_bytes is not None
  assert result.merged_job is None


def _install(repo: Path, bytes_v1: bytes) -> None:
  """The install sequence app_git models: record the pristine v1 bytes on
  `upstream`, then align `main` to it so the working branch starts at the
  installed version (a shared merge base for the next update)."""
  app_git.ensure_repo(repo)
  app_git.record_upstream(repo, bytes_v1, "https://x/mobius.json", "1.0.0")
  app_git.align_local_to_upstream(repo)


def test_merge_clean_returns_merged_bytes(tmp_path):
  """Install v1, edit one region locally, then an upstream v2 edits a
  DISJOINT region — the three-way merge is clean and hands back the
  combined bytes (local edit + upstream edit)."""
  repo = tmp_path / "app"
  base = "line A\nline B\nline C\nline D\nline E\n"
  _install(repo, base.encode())

  # Local edits line A on `main`.
  _write(repo, "line A LOCAL\nline B\nline C\nline D\nline E\n")
  app_git.commit_local(repo, "local edit A")
  # Upstream v2 edits line E — disjoint from the local change.
  app_git.record_upstream(
    repo,
    b"line A\nline B\nline C\nline D\nline E UPSTREAM\n",
    "https://x/mobius.json",
    "2.0.0",
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "clean"
  assert result.merged_bytes is not None
  merged = result.merged_bytes.decode()
  assert "line A LOCAL" in merged
  assert "line E UPSTREAM" in merged


def test_merge_conflict_names_paths_and_leaves_worktree_intact(tmp_path):
  """Install v1, then local + upstream v2 BOTH edit the same line →
  conflict. The verdict names index.jsx and the working tree is NOT
  mutated."""
  repo = tmp_path / "app"
  _install(repo, b"shared line\n")

  # Local and upstream edit the same single line in different ways.
  _write(repo, "shared line LOCAL\n")
  app_git.commit_local(repo, "local edit")
  worktree_before = (repo / "index.jsx").read_text()
  app_git.record_upstream(
    repo, b"shared line UPSTREAM\n", "https://x/mobius.json", "2.0.0",
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "conflict"
  assert "index.jsx" in result.conflict_paths
  assert result.merged_bytes is None
  # The verdict must NOT have written conflict markers into the live file.
  assert (repo / "index.jsx").read_text() == worktree_before


def test_start_conflict_merge_leaves_real_markers_and_merge_head(tmp_path):
  """start_conflict_merge runs a REAL merge into the working tree, leaving
  conflict markers + MERGE_HEAD for the agent to resolve like a `git pull`
  conflict — and `git merge --abort` cleanly restores the local version."""
  repo = tmp_path / "app"
  _install(repo, b"shared line\n")
  _write(repo, "shared line LOCAL\n")
  app_git.commit_local(repo, "local edit")
  local_before = (repo / "index.jsx").read_text()
  app_git.record_upstream(
    repo, b"shared line UPSTREAM\n", "https://x/mobius.json", "2.0.0",
  )
  # The in-memory verdict agrees it's a conflict.
  assert app_git.merge_upstream(repo).status == "conflict"

  paths = app_git.start_conflict_merge(repo)

  assert "index.jsx" in paths
  body = (repo / "index.jsx").read_text()
  assert "<<<<<<<" in body and ">>>>>>>" in body
  assert "shared line LOCAL" in body and "shared line UPSTREAM" in body
  assert (repo / ".git" / "MERGE_HEAD").exists()

  # Bail out: git merge --abort restores the pre-update local version.
  app_git._run(repo, "merge", "--abort")
  assert not (repo / ".git" / "MERGE_HEAD").exists()
  assert (repo / "index.jsx").read_text() == local_before


def test_resolved_conflict_commit_advances_base(tmp_path):
  """After start_conflict_merge, resolving the markers + commit_local
  finalizes a 2-parent merge so upstream becomes an ancestor of main — the
  next update merges clean (the B1 base-advance the watcher gives for free)."""
  repo = tmp_path / "app"
  _install(repo, b"l1\nshared\nl3\n")
  _write(repo, "l1\nshared LOCAL\nl3\n")
  app_git.commit_local(repo, "local edit")
  app_git.record_upstream(
    repo, b"l1\nshared UPSTREAM\nl3\n", "https://x/mobius.json", "2.0.0",
  )
  app_git.start_conflict_merge(repo)
  # Agent resolves the markers (keeps both sides), marker-free.
  _write(repo, "l1\nshared LOCAL+UPSTREAM\nl3\n")
  sha = app_git.commit_local(repo, "resolved merge")

  assert sha is not None
  assert not (repo / ".git" / "MERGE_HEAD").exists()
  # upstream is now an ancestor of main → a re-merge is clean (no re-conflict).
  assert app_git.merge_upstream(repo).status == "clean"


def _stage_non_entry_conflict(repo: Path) -> str:
  """Install + diverge so a NON-entry file (fetch.sh) conflicts.

  `index.jsx` is identical on both sides, so it compiles fine; only the
  job script `fetch.sh` carries the conflict. Returns the resolved-clean
  `fetch.sh` body the agent would save to finish the merge. Leaves the
  repo with a real in-progress merge (MERGE_HEAD + markers in fetch.sh).
  """
  base_jsx = b"export default () => null\n"
  base_job = b"#!/bin/bash\nshared step\n"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, base_jsx, "https://x/mobius.json", "1.0.0",
    job_name="fetch.sh", job_bytes=base_job,
  )
  app_git.align_local_to_upstream(repo)
  # Agent edits the job (and only the job) locally on main.
  (repo / "fetch.sh").write_text("#!/bin/bash\nLOCAL step\n")
  app_git.commit_local(repo, "local job edit")
  # Upstream v2 edits the SAME line of the job — index.jsx is unchanged.
  app_git.record_upstream(
    repo, base_jsx, "https://x/mobius.json", "2.0.0",
    job_name="fetch.sh", job_bytes=b"#!/bin/bash\nUPSTREAM step\n",
  )
  assert app_git.merge_upstream(repo, job_name="fetch.sh").status == "conflict"
  app_git.start_conflict_merge(repo)
  return "#!/bin/bash\nRESOLVED step\n"


def test_has_unresolved_conflicts_true_for_unstaged_and_staged_markers(tmp_path):
  """has_unresolved_conflicts catches a marker-bearing non-entry file both
  before AND after the agent stages it. `ls-files -u` reports the unmerged
  index entry; once the agent `git add`s the still-marker-bearing file that
  clears, but `git diff --check` still flags the leftover markers — so both
  signals are needed and the helper must fire in both states."""
  repo = tmp_path / "app"
  _stage_non_entry_conflict(repo)
  # Unstaged: unmerged index entry present.
  assert app_git.has_unresolved_conflicts(repo) is True
  # Agent stages the still-conflicted file (clears ls-files -u, markers remain).
  app_git._run(repo, "add", "fetch.sh")
  assert "<<<<<<<" in (repo / "fetch.sh").read_text()
  assert app_git.has_unresolved_conflicts(repo) is True


def test_has_unresolved_conflicts_false_when_resolved_clean(tmp_path):
  """Once the agent writes the file marker-free and stages it, the merge has
  no unresolved conflicts even though MERGE_HEAD is still set."""
  repo = tmp_path / "app"
  resolved = _stage_non_entry_conflict(repo)
  (repo / "fetch.sh").write_text(resolved)
  app_git._run(repo, "add", "fetch.sh")
  assert (repo / ".git" / "MERGE_HEAD").exists()
  assert app_git.has_unresolved_conflicts(repo) is False


def test_has_unresolved_conflicts_false_without_merge(tmp_path):
  """No merge in progress → no unresolved conflicts, even when a tracked
  markdown/code file legitimately contains a bare `=======` separator (which
  a naive grep would false-positive on). `git diff --check` only runs under a
  live merge, and ls-files -u is empty, so the helper stays False."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  _write(repo, "export default () => null\n")
  (repo / "NOTES.md").write_text("# Heading\n=======\nbody\n")
  app_git.commit_local(repo, "docs with a separator line")
  assert not (repo / ".git" / "MERGE_HEAD").exists()
  assert app_git.has_unresolved_conflicts(repo) is False


def test_commit_local_refuses_to_finalize_non_entry_marker_conflict(tmp_path):
  """The invariant: an update must NEVER finalize while ANY tracked file has
  unresolved conflict markers. A conflict in a NON-entry file (fetch.sh) does
  not break index.jsx's compile, so the watcher would happily call
  commit_local — which must REFUSE, leaving the prior version entirely intact
  (HEAD unmoved, markers never committed, no MERGE_HEAD finalization)."""
  repo = tmp_path / "app"
  _stage_non_entry_conflict(repo)
  head_before = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  # Agent (or watcher) stages the marker-bearing file and tries to commit.
  app_git._run(repo, "add", "fetch.sh")

  result = app_git.commit_local(repo, "agent edit")

  # No commit: the gate refused.
  assert result is None
  # The prior committed version is unchanged — base did not advance.
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == head_before
  # The merge is still in progress for the agent to finish resolving.
  assert (repo / ".git" / "MERGE_HEAD").exists()
  # The committed fetch.sh still holds the PRIOR local content, never the
  # marker-bearing tree.
  committed = app_git._run(
    repo, "show", f"{app_git.LOCAL_BRANCH}:fetch.sh",
  ).stdout
  assert "<<<<<<<" not in committed
  assert "LOCAL step" in committed


def test_commit_local_finalizes_once_markers_resolved(tmp_path):
  """Once ALL markers are resolved (agent writes the file clean + it's
  staged), commit_local DOES finalize the 2-parent merge: HEAD advances,
  MERGE_HEAD clears, and the clean bytes are committed."""
  repo = tmp_path / "app"
  resolved = _stage_non_entry_conflict(repo)
  head_before = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  (repo / "fetch.sh").write_text(resolved)

  sha = app_git.commit_local(repo, "resolved merge")

  assert sha is not None
  assert sha != head_before
  assert not (repo / ".git" / "MERGE_HEAD").exists()
  # The finalize is a true 2-parent merge (upstream became an ancestor).
  assert app_git.merge_upstream(repo, job_name="fetch.sh").status == "clean"
  committed = app_git._run(
    repo, "show", f"{app_git.LOCAL_BRANCH}:fetch.sh",
  ).stdout
  assert "RESOLVED step" in committed
  assert "<<<<<<<" not in committed


def test_commit_local_is_noop_when_unchanged(tmp_path):
  """commit_local returns None and adds no commit when the tree already
  matches main's tip."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  _write(repo, "stable")
  first = app_git.commit_local(repo, "first")
  assert first is not None
  again = app_git.commit_local(repo, "no change")
  assert again is None


def test_local_diverged_from_false_when_main_matches_base(tmp_path):
  """local_diverged_from is false when main still matches the upstream
  base commit."""
  repo = tmp_path / "app"
  _install(repo, b"stable\n")
  base = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)

  assert app_git.local_diverged_from(repo, base) is False


def test_local_diverged_from_true_when_main_has_local_edit(tmp_path):
  """local_diverged_from is true when main has committed local edits."""
  repo = tmp_path / "app"
  _install(repo, b"stable\n")
  base = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  _write(repo, "local\n")
  app_git.commit_local(repo, "local edit")

  assert app_git.local_diverged_from(repo, base) is True


def test_align_local_to_upstream_resets_main_to_upstream_tip(tmp_path):
  """align_local_to_upstream (the install-time step) points `main` at the
  upstream tip so the working branch starts at the installed version."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, b"INSTALLED V1\n", "https://x/mobius.json", "1.0.0",
  )
  app_git.align_local_to_upstream(repo)
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == app_git.head_sha(
    repo, app_git.UPSTREAM_BRANCH
  )
  # The on-disk working tree matches the installed version.
  assert (repo / "index.jsx").read_text() == "INSTALLED V1\n"


def test_gitignore_tracks_sibling_source_modules_not_build_output(tmp_path):
  """Modular apps split into sibling .js/.jsx/.ts/.tsx modules — building-apps.md
  tells the agent to do exactly this (e.g. `cards.js`). Those are hand-written
  SOURCE and must be tracked by per-app git so the merge/conflict-resolution
  model sees them; a former blanket `*.js` silently dropped them. Generated or
  vendored output (dist/, static/, node_modules/), install .bak snapshots, and
  the integer-id storage tree must stay ignored.
  """
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  # Hand-written source: the entry plus every sibling module extension.
  (repo / "index.jsx").write_text("import './cards.js'\nexport default () => null\n")
  (repo / "cards.js").write_text("export const cards = []\n")
  (repo / "Board.jsx").write_text("export const Board = () => null\n")
  (repo / "helpers.ts").write_text("export const x = 1\n")
  (repo / "fetch.sh").write_text("#!/usr/bin/env bash\n")
  # Generated / vendored / install-artifact / storage paths that must NOT track.
  (repo / "dist").mkdir()
  (repo / "dist" / "bundle.js").write_text("// built\n")
  (repo / "static").mkdir()
  (repo / "static" / "game.js").write_text("// prebuilt\n")
  (repo / "node_modules").mkdir()
  (repo / "node_modules" / "dep.js").write_text("// dep\n")
  (repo / "index.jsx.bak").write_text("old\n")
  (repo / "12").mkdir()
  (repo / "12" / "data.json").write_text("{}\n")

  app_git.commit_local(repo, "modular app")
  tracked = set(app_git._run(repo, "ls-files").stdout.split())

  assert {"index.jsx", "cards.js", "Board.jsx", "helpers.ts", "fetch.sh"} <= tracked
  assert "dist/bundle.js" not in tracked
  assert "static/game.js" not in tracked
  assert "node_modules/dep.js" not in tracked
  assert "index.jsx.bak" not in tracked
  assert not any(t.startswith("12/") for t in tracked)


def test_init_cron_is_never_tracked_so_a_reset_cannot_resurrect_it(tmp_path):
  """init-cron.sh must stay OUT of per-app history (card 099).

  The scaffold writes init-cron.sh on every scheduled update; the cron-orphan
  drop unlinks it from the WORKING TREE when a later manifest removes the
  schedule. If git tracked it, a subsequent `merge --abort` / conflict
  hard-reset would restore the committed copy, and the entrypoint boot replay
  would re-arm the orphan cron. Gitignoring it severs that resurrection path:
  the file is never in a commit, so no reset can bring back a copy the drop
  removed.
  """
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  _write(repo, "v1\n")
  (repo / "init-cron.sh").write_text("#!/bin/bash\ncrontab -\n")
  app_git.commit_local(repo, "scheduled v1")

  # Never tracked, even though git add -A swept the whole tree.
  tracked = set(app_git._run(repo, "ls-files").stdout.split())
  assert "init-cron.sh" not in tracked
  assert "index.jsx" in tracked

  # The drop unlinks the working-tree copy when the schedule goes away.
  (repo / "init-cron.sh").unlink()
  app_git.commit_local(repo, "schedule removed")

  # A hard reset back to the scheduled-v1 commit (the merge-abort / conflict
  # reset shape) must NOT resurrect init-cron.sh, because v1 never committed it.
  v1_sha = app_git._run(repo, "rev-list", "--max-parents=1", "HEAD").stdout.split()[-1]
  app_git._run(repo, "reset", "--hard", v1_sha)
  assert not (repo / "init-cron.sh").exists()
