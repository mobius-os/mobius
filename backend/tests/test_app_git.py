"""Per-app git module — init, upstream recording, and the merge verdict.

These exercise `app_git` directly against a throwaway repo in `tmp_path`
(no DB, no HTTP, no install endpoint) so the git plumbing is pinned in
isolation. The clean/conflict cases are the load-bearing ones: a clean
merge must hand back the merged bytes and a conflict must name the file
WITHOUT touching the working tree.
"""

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
