"""Per-app git module — init, upstream recording, and the merge verdict.

These exercise `app_git` directly against a throwaway repo in `tmp_path`
(no DB, no HTTP, no install endpoint) so the git plumbing is pinned in
isolation. The clean/conflict cases are the load-bearing ones: a clean
merge must hand back the merged tree oid (whose `index.jsx` carries the
combined edits) and a conflict must name the file WITHOUT touching the
working tree.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import app_git


def _write(repo: Path, text: str) -> None:
  (repo / "index.jsx").write_text(text, encoding="utf-8")


def _commit_all(repo: Path, msg: str) -> str:
  subprocess.run(
    [
      "git",
      "-c", "user.name=Test",
      "-c", "user.email=test@example.invalid",
      "-C", str(repo),
      "add", ".",
    ],
    check=True,
    env=app_git._git_env(repo),
  )
  subprocess.run(
    [
      "git",
      "-c", "user.name=Test",
      "-c", "user.email=test@example.invalid",
      "-C", str(repo),
      "commit", "-q", "-m", msg,
    ],
    check=True,
    env=app_git._git_env(repo),
  )
  return subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True, env=app_git._git_env(repo),
  ).stdout.strip()


def test_clone_upstream_uses_real_origin_and_app_gitignore(tmp_path):
  """clone_upstream checks out main at a real origin/<ref> commit."""
  fixture = tmp_path / "fixture"
  bare = tmp_path / "fixture.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text(
    "export default function App() { return <div>clone</div>; }\n",
    encoding="utf-8",
  )
  (fixture / ".gitignore").write_text(
    "# app-owned ignore\ntmp-output/\n", encoding="utf-8",
  )
  subprocess.run(
    [
      "git",
      "-c", "user.name=Test",
      "-c", "user.email=test@example.invalid",
      "-C", str(fixture),
      "add", ".",
    ],
    check=True,
    env=app_git._git_env(fixture),
  )
  subprocess.run(
    [
      "git",
      "-c", "user.name=Test",
      "-c", "user.email=test@example.invalid",
      "-C", str(fixture),
      "commit", "-q", "-m", "fixture",
    ],
    check=True,
    env=app_git._git_env(fixture),
  )
  fixture_head = subprocess.run(
    ["git", "-C", str(fixture), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True, env=app_git._git_env(fixture),
  ).stdout.strip()
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True,
    env=app_git._git_env(fixture),
  )

  source_dir = tmp_path / "source"
  source_dir.mkdir()
  returned = app_git.clone_upstream(source_dir, bare.as_uri(), "main")

  assert returned == fixture_head
  origin_url = app_git._run(
    source_dir, "remote", "get-url", "origin",
  ).stdout.strip()
  origin_head = app_git._run(
    source_dir, "rev-parse", "origin/main",
  ).stdout.strip()
  branch = app_git._run(
    source_dir, "branch", "--show-current",
  ).stdout.strip()
  local_head = app_git._run(
    source_dir, "rev-parse", app_git.LOCAL_BRANCH,
  ).stdout.strip()
  upstream_head = app_git._run(
    source_dir, "rev-parse", app_git.UPSTREAM_BRANCH,
  ).stdout.strip()
  assert origin_url == bare.as_uri()
  assert origin_head == fixture_head
  assert branch == app_git.LOCAL_BRANCH
  assert local_head == fixture_head
  assert upstream_head == fixture_head
  assert (source_dir / ".gitignore").read_text(encoding="utf-8") == (
    "# app-owned ignore\ntmp-output/\n"
  )


def test_clone_upstream_neutralizes_symlinks_and_layers_managed_ignore(tmp_path):
  """A tracked symlink in an (untrusted) catalog repo must NOT check out as a
  real filesystem symlink — that would escape the app dir. And the Möbius
  managed-artifact ignores are layered via .git/info/exclude, leaving the
  app's own committed .gitignore untouched so it still travels upstream."""
  fixture = tmp_path / "fixture"
  bare = tmp_path / "fixture.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text("export default () => null\n", "utf-8")
  (fixture / ".gitignore").write_text("node_modules/\n", "utf-8")
  # a malicious symlink pointing outside the eventual app dir
  (fixture / "evil").symlink_to("/data/service-token.txt")
  env = app_git._git_env(fixture)
  for args in (["add", "."], ["commit", "-q", "-m", "fixture"]):
    subprocess.run(
      ["git", "-c", "user.name=Test", "-c", "user.email=t@t.invalid",
       "-C", str(fixture), *args], check=True, env=env,
    )
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True, env=env,
  )

  source_dir = tmp_path / "source"
  source_dir.mkdir()
  app_git.clone_upstream(source_dir, bare.as_uri(), "main")

  evil = source_dir / "evil"
  assert not evil.is_symlink()  # neutralized: plain file, not a live symlink
  assert evil.read_text().strip() == "/data/service-token.txt"
  # app's own .gitignore is committed + untouched (travels upstream)
  assert (source_dir / ".gitignore").read_text() == "node_modules/\n"
  # managed artifacts are excluded locally, not committed
  exclude = (source_dir / ".git" / "info" / "exclude").read_text()
  assert "static/" in exclude and "init-cron.sh" in exclude
  # the static-asset MANIFEST itself must be excluded too (not just static/):
  # if it is tracked, an app that declares static_assets diverges from origin
  # on its first commit and every update is forced through a three-way merge,
  # breaking the clean-diff PR property. check-ignore, not substring, so the
  # exclude rule actually fires against a real repo.
  for name, want in ((".mobius-static-assets.json", True),
                     ("index.jsx", False)):
    r = subprocess.run(
      ["git", "-C", str(source_dir), "check-ignore", name],
      env=env, capture_output=True,
    )
    assert (r.returncode == 0) is want, name


def test_origin_repo_refresh_preserves_custom_info_exclude(tmp_path):
  """Mobius owns only its marked block in origin-backed repos."""
  fixture = tmp_path / "fixture"
  bare = tmp_path / "fixture.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text("export default () => null\n")
  (fixture / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
  _commit_all(fixture, "fixture")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True,
    env=app_git._git_env(fixture),
  )

  source_dir = tmp_path / "source"
  source_dir.mkdir()
  app_git.clone_upstream(source_dir, bare.as_uri(), "main")
  exclude = source_dir / ".git" / "info" / "exclude"
  exclude.write_text("# user scratch\nscratch/\n", encoding="utf-8")
  (source_dir / "last-run.json").write_text("{}\n", encoding="utf-8")
  app_git._run(source_dir, "add", "-f", "last-run.json")
  app_git._run(source_dir, "commit", "-q", "-m", "old tracked runtime")
  (source_dir / "README.md.mobius-drop-bak").write_text("old\n", encoding="utf-8")
  app_git._run(source_dir, "add", "-f", "README.md.mobius-drop-bak")
  app_git._run(source_dir, "commit", "-q", "-m", "old tracked backup")
  (source_dir / "api.js").write_text("export const api = true\n", encoding="utf-8")

  app_git.commit_local(source_dir, "local edit")

  exclude_text = exclude.read_text(encoding="utf-8")
  assert "# user scratch\nscratch/\n" in exclude_text
  assert app_git._EXCLUDE_BEGIN in exclude_text
  assert "last-run.json" in exclude_text
  assert (source_dir / ".gitignore").read_text(encoding="utf-8") == "node_modules/\n"
  assert (source_dir / "last-run.json").read_text(encoding="utf-8") == "{}\n"
  assert (source_dir / "README.md.mobius-drop-bak").read_text(encoding="utf-8") == "old\n"
  tracked = set(app_git._run(source_dir, "ls-files").stdout.split())
  assert "api.js" in tracked
  assert "last-run.json" not in tracked
  assert "README.md.mobius-drop-bak" not in tracked


def test_origin_repo_refresh_drops_stale_managed_gitignore(tmp_path):
  fixture = tmp_path / "fixture"
  bare = tmp_path / "fixture.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text("export default () => null\n", encoding="utf-8")
  _commit_all(fixture, "fixture")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True, env=app_git._git_env(fixture),
  )

  source_dir = tmp_path / "source"
  source_dir.mkdir()
  app_git.clone_upstream(source_dir, bare.as_uri(), "main")
  (source_dir / ".gitignore").write_text(app_git._GITIGNORE, encoding="utf-8")
  (source_dir / "index.jsx").write_text(
    "export default () => <div>local</div>\n", encoding="utf-8",
  )

  app_git.commit_local(source_dir, "local edit")

  assert not (source_dir / ".gitignore").exists()
  tracked = app_git._run(source_dir, "ls-files").stdout.split()
  assert ".gitignore" not in tracked
  changed = app_git._run(
    source_dir, "diff", "--name-only", f"{app_git.UPSTREAM_BRANCH}..main",
  ).stdout.split()
  assert changed == ["index.jsx"]


def test_origin_repo_refresh_keeps_app_authored_gitignore(tmp_path):
  fixture = tmp_path / "fixture"
  bare = tmp_path / "fixture.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text("export default () => null\n", encoding="utf-8")
  _commit_all(fixture, "fixture")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True, env=app_git._git_env(fixture),
  )

  source_dir = tmp_path / "source"
  source_dir.mkdir()
  app_git.clone_upstream(source_dir, bare.as_uri(), "main")
  (source_dir / ".gitignore").write_text("local-cache/\n", encoding="utf-8")
  (source_dir / "index.jsx").write_text(
    "export default () => <div>local</div>\n", encoding="utf-8",
  )

  app_git.commit_local(source_dir, "local edit")

  assert (source_dir / ".gitignore").read_text(encoding="utf-8") == "local-cache/\n"
  tracked = app_git._run(source_dir, "ls-files").stdout.split()
  assert ".gitignore" in tracked
  changed = app_git._run(
    source_dir, "diff", "--name-only", f"{app_git.UPSTREAM_BRANCH}..main",
  ).stdout.split()
  assert changed == [".gitignore", "index.jsx"]


def test_has_origin_distinguishes_clone_from_synthetic_repo(tmp_path):
  """Cloned apps have origin; record_upstream synthetic apps do not."""
  fixture = tmp_path / "fixture"
  bare = tmp_path / "fixture.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text("export default () => null\n")
  _commit_all(fixture, "fixture")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True,
    env=app_git._git_env(fixture),
  )

  cloned = tmp_path / "cloned"
  cloned.mkdir()
  app_git.clone_upstream(cloned, bare.as_uri(), "main")
  synthetic = tmp_path / "synthetic"
  app_git.record_upstream(
    synthetic, {"index.jsx": b"export default () => null\n"},
    "https://x/mobius.json", "1.0.0",
  )

  assert app_git.has_origin(cloned) is True
  assert app_git.has_origin(synthetic) is False


def test_fetch_upstream_advances_real_origin_and_reads_full_tree(tmp_path):
  """fetch_upstream moves upstream to origin/<ref>; read_ref_tree returns
  every file at that commit, including sibling modules."""
  fixture = tmp_path / "fixture"
  bare = tmp_path / "fixture.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text("import './cards.js'\n")
  (fixture / "cards.js").write_text("export const label = 'v1'\n")
  _commit_all(fixture, "v1")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True,
    env=app_git._git_env(fixture),
  )

  source_dir = tmp_path / "source"
  source_dir.mkdir()
  app_git.clone_upstream(source_dir, bare.as_uri(), "main")

  (fixture / "index.jsx").write_text("import './cards.js'\n// v2\n")
  (fixture / "cards.js").write_text("export const label = 'v2'\n")
  new_head = _commit_all(fixture, "v2")
  subprocess.run(
    ["git", "-C", str(fixture), "push", "-q", str(bare), "main"],
    check=True,
    env=app_git._git_env(fixture),
  )

  fetched = app_git.fetch_upstream(source_dir, "main")
  tree = app_git.read_ref_tree(source_dir, app_git.UPSTREAM_BRANCH)

  assert fetched == new_head
  assert app_git.head_sha(source_dir, app_git.UPSTREAM_BRANCH) == new_head
  origin_head = app_git._run(
    source_dir, "rev-parse", "origin/main",
  ).stdout.strip()
  assert origin_head == new_head
  assert tree["index.jsx"] == b"import './cards.js'\n// v2\n"
  assert tree["cards.js"] == b"export const label = 'v2'\n"


def test_fetch_upstream_rejects_unrelated_origin_without_moving_ref(tmp_path):
  """A synthetic app that accidentally has an origin remote must not have its
  installer-owned upstream branch moved onto unrelated real-repo history."""
  source_dir = tmp_path / "synthetic"
  app_git.record_upstream(
    source_dir,
    {"index.jsx": b"export default () => <div>v1</div>\n"},
    "https://example.invalid/mobius.json",
    "1.0.0",
  )
  app_git.align_local_to_upstream(source_dir)
  old_upstream = app_git.head_sha(source_dir, app_git.UPSTREAM_BRANCH)

  fixture = tmp_path / "fixture"
  bare = tmp_path / "fixture.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text("export default () => <div>real</div>\n")
  _commit_all(fixture, "real repo root")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True,
    env=app_git._git_env(fixture),
  )
  app_git._run(source_dir, "remote", "add", "origin", bare.as_uri())

  with pytest.raises(RuntimeError, match="unrelated"):
    app_git.fetch_upstream(source_dir, "main")

  assert app_git.head_sha(source_dir, app_git.UPSTREAM_BRANCH) == old_upstream


def test_read_tree_exec_paths_reports_only_executables(tmp_path):
  """read_tree_exec_paths returns the 100755 paths in a tree — the bit the
  cloned-update byte-write loop must restore so a repo-tracked helper script
  (not just the manifest's schedule.job) doesn't diverge 644-vs-755 from
  origin and break the clean-diff PR property."""
  fixture = tmp_path / "fixture"
  subprocess.run(["git", "init", "-q", "-b", "main", str(fixture)], check=True)
  (fixture / "index.jsx").write_text("export default () => null\n")
  helper = fixture / "scripts" / "build.sh"
  helper.parent.mkdir()
  helper.write_text("#!/bin/sh\necho hi\n")
  helper.chmod(0o755)
  job = fixture / "fetch.sh"
  job.write_text("#!/bin/sh\n")
  job.chmod(0o755)
  head = _commit_all(fixture, "v1")

  execs = app_git.read_tree_exec_paths(fixture, head)
  assert execs == frozenset({"scripts/build.sh", "fetch.sh"})
  assert "index.jsx" not in execs
  # also resolvable by branch name, not just commit sha
  assert app_git.read_tree_exec_paths(fixture, "main") == execs


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
    repo, {"index.jsx": b"INSTALLED V1\n"}, "https://x/mobius.json", "1.0.0",
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
    repo, {"index.jsx": b"UPSTREAM V2"}, "https://x/mobius.json", "2.0.0",
  )
  assert sha == app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  # The working tree (main) still holds the local edit — recording the
  # upstream version must not check out the upstream bytes.
  assert (repo / "index.jsx").read_text() == before == "LOCAL EDIT"


def test_record_upstream_stages_job_script_on_upstream_tree(tmp_path):
  """record_upstream commits the schedule job script into the `upstream`
  tree alongside index.jsx (when the job is passed as another key in `files`
  and named in `exec_paths`), so a later update can three-way-merge a locally
  edited job. The checked-out `main` working tree is left untouched — only
  `upstream` advances."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  _write(repo, "LOCAL EDIT")
  app_git.commit_local(repo, "local edit")
  before = (repo / "index.jsx").read_text()

  app_git.record_upstream(
    repo,
    {"index.jsx": b"UPSTREAM V2", "fetch.sh": b"#!/bin/bash\necho upstream\n"},
    "https://x/mobius.json", "2.0.0",
    exec_paths=frozenset({"fetch.sh"}),
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
  """A locally edited job script flows through a clean merge: the merged tree
  carries the local edit forward when an upstream v2 changes a DISJOINT region
  of the same script. The job is just another key in the merged tree, read via
  read_merged_tree like any other file."""
  repo = tmp_path / "app"
  base_jsx = b"export default () => null\n"
  base_job = "#!/bin/bash\nstep one\nstep two\nstep three\nstep four\nstep five\n"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, {"index.jsx": base_jsx, "fetch.sh": base_job.encode()},
    "https://x/mobius.json", "1.0.0",
    exec_paths=frozenset({"fetch.sh"}),
  )
  app_git.align_local_to_upstream(repo)

  # Agent edits the FIRST step of the job locally on main.
  (repo / "fetch.sh").write_text(
    base_job.replace("step one", "step ONE LOCAL")
  )
  app_git.commit_local(repo, "local job edit")
  # Upstream v2 edits the LAST step — disjoint from the local change.
  app_git.record_upstream(
    repo,
    {"index.jsx": base_jsx,
     "fetch.sh": base_job.replace("step five", "step FIVE UPSTREAM").encode()},
    "https://x/mobius.json", "2.0.0",
    exec_paths=frozenset({"fetch.sh"}),
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "clean"
  tree = app_git.read_merged_tree(repo, result.merged_tree_oid)
  merged_job = tree["fetch.sh"].decode()
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
  app_git.record_upstream(
    repo, {"index.jsx": base_jsx}, "https://x/mobius.json", "1.0.0",
  )
  app_git.align_local_to_upstream(repo)
  # The agent's job script lands on disk EXECUTABLE (the install chmod 0o755),
  # and commit_local records it at 100755 on main.
  (repo / "fetch.sh").write_bytes(job)
  os.chmod(repo / "fetch.sh", 0o755)
  app_git.commit_local(repo, "agent adds executable job")
  # v2 update now tracks the job with IDENTICAL bytes. The only possible delta
  # against main is the recorded mode.
  app_git.record_upstream(
    repo, {"index.jsx": base_jsx, "fetch.sh": job},
    "https://x/mobius.json", "2.0.0",
    exec_paths=frozenset({"fetch.sh"}),
  )

  # Both branches must record the job EXECUTABLE so no mode skew exists.
  assert _job_mode(repo, app_git.LOCAL_BRANCH, "fetch.sh") == "100755"
  assert _job_mode(repo, app_git.UPSTREAM_BRANCH, "fetch.sh") == "100755"

  result = app_git.merge_upstream(repo)
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
    repo,
    {"index.jsx": b"export default () => null\n",
     "fetch.sh": b"#!/bin/bash\necho hi\n"},
    "https://x/mobius.json", "1.0.0",
    exec_paths=frozenset({"fetch.sh"}),
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
    repo,
    {"index.jsx": b"export default () => null\n",
     "fetch.sh": b"#!/bin/bash\necho hi\n"},
    "https://x/mobius.json", "1.0.0",
    exec_paths=frozenset({"fetch.sh"}),
  )
  app_git.align_local_to_upstream(repo)

  assert (repo / "fetch.sh").stat().st_mode & stat.S_IXUSR


def test_merge_clean_tree_has_no_job_when_none_recorded(tmp_path):
  """When no job script was ever recorded, the clean merge's tree carries
  index.jsx but no fetch.sh — the job is just a tree key, so a manifest with
  no schedule.job simply never puts one in the merged tree."""
  repo = tmp_path / "app"
  base = "line A\nline B\nline C\n"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, {"index.jsx": base.encode()}, "https://x/mobius.json", "1.0.0",
  )
  app_git.align_local_to_upstream(repo)
  _write(repo, "line A LOCAL\nline B\nline C\n")
  app_git.commit_local(repo, "local edit")
  app_git.record_upstream(
    repo, {"index.jsx": b"line A\nline B\nline C UPSTREAM\n"},
    "https://x/mobius.json", "2.0.0",
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "clean"
  tree = app_git.read_merged_tree(repo, result.merged_tree_oid)
  assert "index.jsx" in tree
  assert "fetch.sh" not in tree


def _install(repo: Path, bytes_v1: bytes) -> None:
  """The install sequence app_git models: record the pristine v1 bytes on
  `upstream`, then align `main` to it so the working branch starts at the
  installed version (a shared merge base for the next update)."""
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, {"index.jsx": bytes_v1}, "https://x/mobius.json", "1.0.0",
  )
  app_git.align_local_to_upstream(repo)


def test_merge_clean_returns_merged_bytes(tmp_path):
  """Install v1, edit one region locally, then an upstream v2 edits a
  DISJOINT region — the three-way merge is clean and the merged tree's
  index.jsx carries the combined bytes (local edit + upstream edit)."""
  repo = tmp_path / "app"
  base = "line A\nline B\nline C\nline D\nline E\n"
  _install(repo, base.encode())

  # Local edits line A on `main`.
  _write(repo, "line A LOCAL\nline B\nline C\nline D\nline E\n")
  app_git.commit_local(repo, "local edit A")
  # Upstream v2 edits line E — disjoint from the local change.
  app_git.record_upstream(
    repo,
    {"index.jsx": b"line A\nline B\nline C\nline D\nline E UPSTREAM\n"},
    "https://x/mobius.json",
    "2.0.0",
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "clean"
  tree = app_git.read_merged_tree(repo, result.merged_tree_oid)
  merged = tree["index.jsx"].decode()
  assert "line A LOCAL" in merged
  assert "line E UPSTREAM" in merged


def test_merge_clean_exposes_full_merged_tree(tmp_path):
  """A clean merge exposes the merged tree oid; read_merged_tree reads the
  whole tree back, with index.jsx carrying the combined edits."""
  repo = tmp_path / "app"
  base = "line A\nline B\nline C\nline D\nline E\n"
  _install(repo, base.encode())
  _write(repo, "line A LOCAL\nline B\nline C\nline D\nline E\n")
  app_git.commit_local(repo, "local edit A")
  app_git.record_upstream(
    repo,
    {"index.jsx": b"line A\nline B\nline C\nline D\nline E UPSTREAM\n"},
    "https://x/mobius.json",
    "2.0.0",
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "clean"
  assert result.merged_tree_oid
  tree = app_git.read_merged_tree(repo, result.merged_tree_oid)
  # The whole tree comes back (index.jsx plus the repo's tracked .gitignore),
  # not just the entry file — that is the point of read_merged_tree.
  assert "index.jsx" in tree
  assert b"line A LOCAL" in tree["index.jsx"]
  assert b"line E UPSTREAM" in tree["index.jsx"]


def test_read_merged_tree_returns_every_file_in_a_multi_file_tree(tmp_path):
  """read_merged_tree reads ALL files of a multi-file tree (the platform
  case), at any depth — pinned via a hand-built tree so the reader holds
  independent of the single-entry-file record path."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)

  def _blob(content: bytes) -> str:
    return subprocess.run(
      ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
      input=content, capture_output=True, check=True,
    ).stdout.decode().strip()

  scratch = dict(os.environ)
  scratch["GIT_INDEX_FILE"] = str(repo / ".git" / "scratch-index")
  expected = {
    "index.jsx": b"entry\n",
    "lib/util.js": b"util\n",
    "backend/main.py": b"backend\n",
  }
  for path, content in expected.items():
    subprocess.run(
      ["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
       f"100644,{_blob(content)},{path}"],
      env=scratch, capture_output=True, check=True,
    )
  tree_oid = subprocess.run(
    ["git", "-C", str(repo), "write-tree"],
    env=scratch, capture_output=True, check=True,
  ).stdout.decode().strip()

  assert app_git.read_merged_tree(repo, tree_oid) == expected


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
    repo, {"index.jsx": b"shared line UPSTREAM\n"},
    "https://x/mobius.json", "2.0.0",
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "conflict"
  assert "index.jsx" in result.conflict_paths
  assert result.merged_tree_oid is None
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
    repo, {"index.jsx": b"shared line UPSTREAM\n"},
    "https://x/mobius.json", "2.0.0",
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
  finalizes a single-parent replay (linear) so upstream becomes an ancestor
  of main — the next update merges clean (the B1 base-advance the watcher
  gives for free)."""
  repo = tmp_path / "app"
  _install(repo, b"l1\nshared\nl3\n")
  _write(repo, "l1\nshared LOCAL\nl3\n")
  app_git.commit_local(repo, "local edit")
  app_git.record_upstream(
    repo, {"index.jsx": b"l1\nshared UPSTREAM\nl3\n"},
    "https://x/mobius.json", "2.0.0",
  )
  app_git.start_conflict_merge(repo)
  # Agent resolves the markers (keeps both sides), marker-free.
  _write(repo, "l1\nshared LOCAL+UPSTREAM\nl3\n")
  sha = app_git.commit_local(repo, "resolved merge")

  assert sha is not None
  assert not (repo / ".git" / "MERGE_HEAD").exists()
  # The finalize is single-parent (linear history), not a 2-parent merge.
  assert app_git._run(
    repo, "rev-parse", "--verify", "-q", f"{app_git.LOCAL_BRANCH}^2",
    check=False,
  ).returncode != 0
  # upstream is now an ancestor of main → a re-merge is clean (no re-conflict).
  assert app_git.merge_upstream(repo).status == "clean"


def _has_second_parent(repo: Path) -> bool:
  """Whether main's tip is a merge commit (has a second parent).

  `git rev-parse --verify -q main^2` exits non-zero when `main^2` does not
  resolve (the tip is single-parent), so a linear replay history makes this
  False.
  """
  return app_git._run(
    repo, "rev-parse", "--verify", "-q", f"{app_git.LOCAL_BRANCH}^2",
    check=False,
  ).returncode == 0


def _apply_clean_update(
  repo: Path, new_index: bytes, version: str,
) -> str | None:
  """Run a full clean-update apply the way install.py does, then replay.

  Records `new_index` as the next upstream version, takes the in-memory
  clean verdict, materialises the whole merged tree back onto disk, and
  finalizes with `commit_replay` parented on the NEW upstream tip. Returns
  the replay sha (None if there was nothing to record). Asserts the verdict
  was clean so a caller that expected a clean update fails loudly here.
  """
  app_git.record_upstream(
    repo, {"index.jsx": new_index}, "https://x/mobius.json", version,
  )
  new_upstream = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  merge = app_git.merge_upstream(repo)
  assert merge.status == "clean", merge.conflict_paths
  for rel, data in app_git.read_merged_tree(repo, merge.merged_tree_oid).items():
    (repo / rel).write_bytes(data)
  return app_git.commit_replay(repo, new_upstream, f"update {version}")


def test_clean_update_replay_is_linear_and_advances_base(tmp_path):
  """A clean update via commit_replay keeps history linear (no merge commit)
  and still makes upstream an ancestor of main, with both the local edit and
  the disjoint upstream change present in the served source."""
  repo = tmp_path / "app"
  base = "line A\nline B\nline C\nline D\nline E\n"
  _install(repo, base.encode())
  # Local edits line A; upstream v2 edits the disjoint line E.
  _write(repo, "line A LOCAL\nline B\nline C\nline D\nline E\n")
  app_git.commit_local(repo, "local edit A")

  sha = _apply_clean_update(
    repo, b"line A\nline B\nline C\nline D\nline E UPSTREAM\n", "2.0.0",
  )

  assert sha is not None
  # main is linear: its tip has no second parent.
  assert _has_second_parent(repo) is False
  # upstream is an exact ancestor of main (the base advanced for the next update).
  assert app_git._run(
    repo, "merge-base", "--is-ancestor", app_git.UPSTREAM_BRANCH,
    app_git.LOCAL_BRANCH, check=False,
  ).returncode == 0
  # Both the local edit and the disjoint upstream change landed in the source.
  merged = (repo / "index.jsx").read_text()
  assert "line A LOCAL" in merged
  assert "line E UPSTREAM" in merged
  assert "<<<<<<<" not in merged


def test_clean_update_replay_second_update_does_not_conflict(tmp_path):
  """A SECOND clean update after the first must merge cleanly on a disjoint
  change: the first replay advanced the base to v2, so v3's three-way merge
  diffs only the genuinely-new upstream delta — never re-litigates v1->v2."""
  repo = tmp_path / "app"
  base = "line A\nline B\nline C\nline D\nline E\n"
  _install(repo, base.encode())
  _write(repo, "line A LOCAL\nline B\nline C\nline D\nline E\n")
  app_git.commit_local(repo, "local edit A")

  # v2 edits line E (disjoint from the local line-A edit).
  assert _apply_clean_update(
    repo, b"line A\nline B\nline C\nline D\nline E UPSTREAM\n", "2.0.0",
  ) is not None
  # v3 edits line E AGAIN. With the base advanced to v2 this is still disjoint
  # from the local line-A edit, so the verdict is clean — not a spurious
  # conflict against the stale install-point base.
  sha = _apply_clean_update(
    repo, b"line A\nline B\nline C\nline D\nline E UPSTREAM v3\n", "3.0.0",
  )

  assert sha is not None
  assert _has_second_parent(repo) is False
  merged = (repo / "index.jsx").read_text()
  assert "line A LOCAL" in merged       # local edit still preserved
  assert "line E UPSTREAM v3" in merged  # latest upstream landed
  assert "<<<<<<<" not in merged


def test_resolved_conflict_finalize_single_parent_clears_merge_head(tmp_path):
  """A conflict update resolved on disk and finalized via commit_local lands
  as a single-parent replay: main^2 does not resolve, upstream is an ancestor
  of main, and .git/MERGE_HEAD (plus MERGE_MSG/MERGE_MODE) is gone."""
  repo = tmp_path / "app"
  _install(repo, b"l1\nshared\nl3\n")
  _write(repo, "l1\nshared LOCAL\nl3\n")
  app_git.commit_local(repo, "local edit")
  app_git.record_upstream(
    repo, {"index.jsx": b"l1\nshared UPSTREAM\nl3\n"},
    "https://x/mobius.json", "2.0.0",
  )
  paths = app_git.start_conflict_merge(repo)
  assert "index.jsx" in paths
  assert (repo / ".git" / "MERGE_HEAD").exists()
  # Agent resolves the markers on disk (keeps both sides), marker-free.
  _write(repo, "l1\nshared LOCAL+UPSTREAM\nl3\n")

  sha = app_git.commit_local(repo, "resolved merge")

  assert sha is not None
  assert _has_second_parent(repo) is False
  assert app_git._run(
    repo, "merge-base", "--is-ancestor", app_git.UPSTREAM_BRANCH,
    app_git.LOCAL_BRANCH, check=False,
  ).returncode == 0
  for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
    assert not (repo / ".git" / name).exists()


def test_partial_conflict_isolates_to_the_overlapping_line(tmp_path):
  """Models the real prod webstudio case: the agent edits an app (including
  bumping its version constant) while upstream releases a version that bumps
  the SAME constant. The three-way merge must conflict ONLY on the version
  line — the agent's other edit and upstream's disjoint edit both merge
  cleanly — and resolving that one line finalizes as a single-parent replay
  carrying both disjoint edits. This is the everyday "APP_VERSION collision"
  that drives the resolver chats, and it is a genuine conflict the engine is
  right to surface, not a spurious whole-file one."""
  repo = tmp_path / "app"
  base = (
    "const APP_VERSION = '0.10.2'\n"
    "function header() { return 'hi' }\n"
    "function footer() { return 'bye' }\n"
  )
  _install(repo, base.encode())

  # Agent bumps the version AND edits an unrelated function (header).
  _write(repo, (
    "const APP_VERSION = '0.10.4'\n"
    "function header() { return 'HELLO agent' }\n"
    "function footer() { return 'bye' }\n"
  ))
  app_git.commit_local(repo, "agent edit")

  # Upstream v2 bumps the version (same line) and edits a DIFFERENT function
  # (footer) — disjoint from the agent's header edit.
  app_git.record_upstream(
    repo,
    {"index.jsx": (
      "const APP_VERSION = '0.12.0'\n"
      "function header() { return 'hi' }\n"
      "function footer() { return 'BYE upstream' }\n"
    ).encode()},
    "https://x/mobius.json",
    "2.0.0",
  )

  result = app_git.merge_upstream(repo)
  assert result.status == "conflict"
  assert result.conflict_paths == ["index.jsx"]

  # The materialized markers must be ISOLATED to the version line: the agent's
  # header edit and upstream's footer edit are already merged in cleanly.
  app_git.start_conflict_merge(repo)
  materialized = (repo / "index.jsx").read_text()
  assert "HELLO agent" in materialized
  assert "BYE upstream" in materialized
  assert "<<<<<<<" in materialized and "0.12.0" in materialized

  # Agent resolves by taking upstream's version, keeping both disjoint edits.
  _write(repo, (
    "const APP_VERSION = '0.12.0'\n"
    "function header() { return 'HELLO agent' }\n"
    "function footer() { return 'BYE upstream' }\n"
  ))
  sha = app_git.commit_local(repo, "resolve version conflict")

  assert sha is not None
  assert _has_second_parent(repo) is False
  assert app_git._run(
    repo, "merge-base", "--is-ancestor", app_git.UPSTREAM_BRANCH,
    app_git.LOCAL_BRANCH, check=False,
  ).returncode == 0
  final = (repo / "index.jsx").read_text()
  assert "0.12.0" in final
  assert "HELLO agent" in final and "BYE upstream" in final
  assert "<<<<<<<" not in final


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
    repo, {"index.jsx": base_jsx, "fetch.sh": base_job},
    "https://x/mobius.json", "1.0.0",
    exec_paths=frozenset({"fetch.sh"}),
  )
  app_git.align_local_to_upstream(repo)
  # Agent edits the job (and only the job) locally on main.
  (repo / "fetch.sh").write_text("#!/bin/bash\nLOCAL step\n")
  app_git.commit_local(repo, "local job edit")
  # Upstream v2 edits the SAME line of the job — index.jsx is unchanged.
  app_git.record_upstream(
    repo, {"index.jsx": base_jsx, "fetch.sh": b"#!/bin/bash\nUPSTREAM step\n"},
    "https://x/mobius.json", "2.0.0",
    exec_paths=frozenset({"fetch.sh"}),
  )
  assert app_git.merge_upstream(repo).status == "conflict"
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


def test_has_unresolved_conflicts_false_for_resolved_file_with_separator(tmp_path):
  """A resolution whose content legitimately contains a bare `=======` line
  (a heredoc divider, a setext rule) UNDER a live merge must NOT read as an
  unresolved conflict. `git diff --check` flags any 7-char marker line, which
  would deadlock the update forever; we match only the labeled boundaries a
  real conflict carries, so a lone `=======` is ignored."""
  repo = tmp_path / "app"
  _stage_non_entry_conflict(repo)
  # Agent resolves the job; the resolved content has a bare `=======` line.
  (repo / "fetch.sh").write_text("#!/bin/bash\ncat <<'EOF'\n=======\nEOF\n")
  app_git._run(repo, "add", "fetch.sh")
  assert (repo / ".git" / "MERGE_HEAD").exists()
  assert "<<<<<<<" not in (repo / "fetch.sh").read_text()
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
  staged), commit_local DOES finalize as a single-parent replay: HEAD
  advances, MERGE_HEAD clears, and the clean bytes are committed."""
  repo = tmp_path / "app"
  resolved = _stage_non_entry_conflict(repo)
  head_before = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  (repo / "fetch.sh").write_text(resolved)

  sha = app_git.commit_local(repo, "resolved merge")

  assert sha is not None
  assert sha != head_before
  assert not (repo / ".git" / "MERGE_HEAD").exists()
  # The finalize is single-parent (linear), so `main^2` does not resolve.
  assert app_git._run(
    repo, "rev-parse", "--verify", "-q", f"{app_git.LOCAL_BRANCH}^2",
    check=False,
  ).returncode != 0
  # upstream still became an ancestor of main, so a re-merge is clean.
  assert app_git.merge_upstream(repo).status == "clean"
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
    repo, {"index.jsx": b"INSTALLED V1\n"}, "https://x/mobius.json", "1.0.0",
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


def test_commit_local_upgrades_managed_gitignore_and_untracks_runtime_files(tmp_path):
  """Old synthetic app repos had stale managed .gitignore files.

  The next commit must repair those rules before staging: sibling source files
  like api.js should become tracked again, while runtime workspaces already
  captured by old repos are removed from the index without deleting them.
  """
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  (repo / ".gitignore").write_text(
    "# old managed ignore\n*.js\n*.bak\n[0-9]*/\n",
    encoding="utf-8",
  )
  _write(repo, "export default function App() { return null }\n")
  app_git._run(repo, "add", ".gitignore", "index.jsx")
  app_git._run(repo, "commit", "-q", "-m", "old synthetic app")

  (repo / "api.js").write_text("export const api = true\n", encoding="utf-8")
  (repo / "inputs").mkdir()
  (repo / "inputs" / "activity.jsonl").write_text("runtime\n", encoding="utf-8")
  (repo / "last-run.json").write_text("{}\n", encoding="utf-8")
  (repo / "init-cron.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  (repo / "README.md.mobius-drop-bak").write_text("old\n", encoding="utf-8")
  app_git._run(
    repo, "add",
    "inputs/activity.jsonl", "last-run.json", "init-cron.sh",
    "README.md.mobius-drop-bak",
  )

  app_git.commit_local(repo, "agent edit")

  tracked = set(app_git._run(repo, "ls-files").stdout.split())
  assert "api.js" in tracked
  assert "inputs/activity.jsonl" not in tracked
  assert "last-run.json" not in tracked
  assert "init-cron.sh" not in tracked
  assert "README.md.mobius-drop-bak" not in tracked
  assert (repo / "inputs" / "activity.jsonl").exists()
  assert (repo / "last-run.json").exists()
  assert (repo / "init-cron.sh").exists()
  assert (repo / "README.md.mobius-drop-bak").exists()
  gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
  assert "*.js" not in gitignore
  assert "inputs/" in gitignore
  assert "last-run.json" in gitignore


def test_record_upstream_upgrades_stale_managed_gitignore(tmp_path):
  """The pristine upstream branch must also stop carrying old ignore rules."""
  repo = tmp_path / "app"
  app_git.ensure_repo(repo)
  (repo / ".gitignore").write_text(
    "# old managed ignore\n*.js\n*.bak\n[0-9]*/\n",
    encoding="utf-8",
  )
  _write(repo, "export default function App() { return null }\n")
  app_git._run(repo, "add", ".gitignore", "index.jsx")
  app_git._run(repo, "commit", "-q", "-m", "old synthetic app")
  app_git._run(repo, "branch", "-f", app_git.UPSTREAM_BRANCH, app_git.LOCAL_BRANCH)

  app_git.record_upstream(
    repo,
    {
      "index.jsx": b"export default function App() { return null }\n",
      "api.js": b"export const api = true\n",
    },
    "https://x/mobius.json",
    "2.0.0",
  )

  upstream_gitignore = app_git._run(
    repo, "cat-file", "-p", f"{app_git.UPSTREAM_BRANCH}:.gitignore",
  ).stdout
  assert "*.js" not in upstream_gitignore
  assert "inputs/" in upstream_gitignore
  assert "last-run.json" in upstream_gitignore
  tracked = set(app_git._run(
    repo, "ls-tree", "-r", "--name-only", app_git.UPSTREAM_BRANCH,
  ).stdout.split())
  assert "api.js" in tracked


def test_align_local_to_upstream_preserves_tracked_runtime_files(tmp_path):
  """Legacy repos may reach install with runtime files already tracked."""
  repo = tmp_path / "app"
  app_git.record_upstream(
    repo,
    {"index.jsx": b"export default function App() { return <div>v1</div> }\n"},
    "https://x/mobius.json",
    "1.0.0",
  )
  app_git.align_local_to_upstream(repo)
  (repo / ".gitignore").write_text(
    "# old managed ignore\n*.js\n*.bak\n[0-9]*/\n",
    encoding="utf-8",
  )
  (repo / "inputs").mkdir()
  (repo / "inputs" / "activity.jsonl").write_text("keep\n", encoding="utf-8")
  (repo / "last-run.json").write_text("{}\n", encoding="utf-8")
  app_git._run(repo, "add", ".gitignore", "inputs/activity.jsonl", "last-run.json")
  app_git._run(repo, "commit", "-q", "-m", "old tracked runtime")

  app_git.record_upstream(
    repo,
    {"index.jsx": b"export default function App() { return <div>v2</div> }\n"},
    "https://x/mobius.json",
    "2.0.0",
  )
  app_git.align_local_to_upstream(repo)

  assert (repo / "index.jsx").read_text(encoding="utf-8").endswith("v2</div> }\n")
  assert (repo / "inputs" / "activity.jsonl").read_text(encoding="utf-8") == "keep\n"
  assert (repo / "last-run.json").read_text(encoding="utf-8") == "{}\n"
  tracked = set(app_git._run(repo, "ls-files").stdout.split())
  assert "inputs/activity.jsonl" not in tracked
  assert "last-run.json" not in tracked
  gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
  assert "*.js" not in gitignore
  assert "inputs/" in gitignore


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


def test_commit_local_refuses_during_in_progress_cherry_pick(tmp_path):
  """commit_local must REFUSE (before staging) while a rebase/cherry-pick is
  mid-flight — no MERGE_HEAD, but CHERRY_PICK_HEAD + unmerged index entries.

  A watcher's commit-on-save calling `git add` there would mark the conflicted
  paths resolved in the index, and a later commit could bake conflict markers
  into tracked source. The refusal must leave the operation entirely intact:
  HEAD unmoved, CHERRY_PICK_HEAD still present, the paths still unmerged, and
  the marker-bearing working tree untouched.
  """
  repo = tmp_path / "app"
  _install(repo, b"line1\nORIG\nline3\n")
  # main advances with one edit to the shared line.
  _write(repo, "line1\nMAIN\nline3\n")
  base = app_git.commit_local(repo, "main edit")
  assert base is not None
  # A sibling commit off the install point edits the SAME line a different way,
  # so cherry-picking it onto main conflicts.
  install_point = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  app_git._run(repo, "checkout", "-q", "-b", "side", install_point)
  _write(repo, "line1\nSIDE\nline3\n")
  app_git._run(repo, "add", "-A")
  app_git._run(repo, "commit", "-q", "-m", "side edit")
  side = app_git.head_sha(repo, "side")
  app_git._run(repo, "checkout", "-q", app_git.LOCAL_BRANCH)
  cp = app_git._run(repo, "cherry-pick", side, check=False)
  assert cp.returncode != 0
  assert (repo / ".git" / "CHERRY_PICK_HEAD").exists()
  assert app_git._run(repo, "ls-files", "-u").stdout.strip()
  body_before = (repo / "index.jsx").read_text()
  assert "<<<<<<<" in body_before

  result = app_git.commit_local(repo, "watcher save mid-cherry-pick")

  # Refused before staging: no commit, HEAD unmoved, cherry-pick still in
  # progress, conflicted paths NOT staged/resolved, working tree untouched.
  assert result is None
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == base
  assert (repo / ".git" / "CHERRY_PICK_HEAD").exists()
  assert app_git._run(repo, "ls-files", "-u").stdout.strip()
  assert (repo / "index.jsx").read_text() == body_before


# ---------------------------------------------------------------------------
# resolve_version_only_conflict — a conflict CONFINED to the version identifier
# auto-resolves to upstream (take-upstream is always right for a version label);
# any real code conflict falls through to None so the owner resolves it.
# ---------------------------------------------------------------------------

_MANIFEST = (
  '{\n'
  '  "id": "demo",\n'
  '  "name": "Demo",\n'
  '  "version": "%s",\n'
  '  "entry": "index.jsx"\n'
  '}\n'
)


def _manifest(version: str) -> bytes:
  return (_MANIFEST % version).encode()


def _diverge(repo, local_files, upstream_files, *, base_files):
  """Set up base → local edit + upstream v2 so a merge can be verdicted.

  Records `base_files` as the shared ancestor, commits `local_files` on main,
  then records `upstream_files` as the new upstream. Returns nothing; the caller
  runs `merge_upstream` / `resolve_version_only_conflict`.
  """
  app_git.ensure_repo(repo)
  app_git.record_upstream(repo, base_files, "https://x/mobius.json", "1.0.0")
  app_git.align_local_to_upstream(repo)
  for name, data in local_files.items():
    (repo / name).write_bytes(data)
  app_git.commit_local(repo, "local edit")
  app_git.record_upstream(repo, upstream_files, "https://x/mobius.json", "2.0.0")


def test_version_only_conflict_resolves_to_upstream(tmp_path):
  """Both sides bumped only mobius.json's version: auto-resolve, take upstream."""
  repo = tmp_path / "app"
  jsx = b"export default () => null\n"
  _diverge(
    repo,
    local_files={"index.jsx": jsx, "mobius.json": _manifest("1.0.1")},
    upstream_files={"index.jsx": jsx, "mobius.json": _manifest("2.0.0")},
    base_files={"index.jsx": jsx, "mobius.json": _manifest("1.0.0")},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  assert "mobius.json" in merge.conflict_paths

  res = app_git.resolve_version_only_conflict(repo, merge.conflict_paths)
  assert res is not None
  tree = res.tree
  assert b'"version": "2.0.0"' in tree["mobius.json"]  # upstream won
  assert b'"version": "1.0.1"' not in tree["mobius.json"]
  assert tree["index.jsx"] == jsx


def test_version_only_conflict_preserves_disjoint_local_edit(tmp_path):
  """A version bump AND a disjoint local code edit: resolve to upstream version
  while carrying the unrelated local edit forward (never silently dropped)."""
  repo = tmp_path / "app"
  base_jsx = b"export default function App() {\n  return one\n}\n"
  local_jsx = b"export default function App() {\n  return LOCAL\n}\n"
  _diverge(
    repo,
    # local bumped version AND edited a line upstream leaves alone
    local_files={"index.jsx": local_jsx, "mobius.json": _manifest("1.0.1")},
    upstream_files={"index.jsx": base_jsx, "mobius.json": _manifest("2.0.0")},
    base_files={"index.jsx": base_jsx, "mobius.json": _manifest("1.0.0")},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"

  res = app_git.resolve_version_only_conflict(repo, merge.conflict_paths)
  assert res is not None
  tree = res.tree
  assert b'"version": "2.0.0"' in tree["mobius.json"]
  assert tree["index.jsx"] == local_jsx  # disjoint local edit carried forward


def test_in_code_app_version_conflict_resolves(tmp_path):
  """An in-code APP_VERSION const clash resolves the same way as the manifest."""
  repo = tmp_path / "app"
  base = b"const APP_VERSION = '1.0.0'\nexport default () => null\n"
  local = b"const APP_VERSION = '1.0.1'\nexport default () => null\n"
  upstream = b"const APP_VERSION = '2.0.0'\nexport default () => null\n"
  _diverge(
    repo,
    local_files={"index.jsx": local},
    upstream_files={"index.jsx": upstream},
    base_files={"index.jsx": base},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"

  res = app_git.resolve_version_only_conflict(repo, merge.conflict_paths)
  assert res is not None
  tree = res.tree
  assert b"APP_VERSION = '2.0.0'" in tree["index.jsx"]


def test_real_code_conflict_is_not_auto_resolved(tmp_path):
  """A version bump alongside a genuine code conflict must NOT auto-resolve —
  the code clash is the owner's call, so return None (fall through to resolver)."""
  repo = tmp_path / "app"
  base_jsx = b"export default function App() {\n  return BASE\n}\n"
  _diverge(
    repo,
    local_files={
      "index.jsx": b"export default function App() {\n  return LOCAL\n}\n",
      "mobius.json": _manifest("1.0.1"),
    },
    upstream_files={
      "index.jsx": b"export default function App() {\n  return UPSTREAM\n}\n",
      "mobius.json": _manifest("2.0.0"),
    },
    base_files={"index.jsx": base_jsx, "mobius.json": _manifest("1.0.0")},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  # index.jsx has a real both-edited-same-line conflict → not version-only.
  assert app_git.resolve_version_only_conflict(repo, merge.conflict_paths) is None


def test_conflict_touching_a_non_version_line_is_not_resolved(tmp_path):
  """A single file whose conflict includes a NON-version line must not resolve:
  normalising the version line still leaves a residual conflict → None."""
  repo = tmp_path / "app"
  base = (
    '{\n  "name": "Demo",\n  "version": "1.0.0",\n  "note": "base"\n}\n'
  ).encode()
  local = (
    '{\n  "name": "Demo",\n  "version": "1.0.1",\n  "note": "LOCAL"\n}\n'
  ).encode()
  upstream = (
    '{\n  "name": "Demo",\n  "version": "2.0.0",\n  "note": "UPSTREAM"\n}\n'
  ).encode()
  _diverge(
    repo,
    local_files={"index.jsx": b"x\n", "mobius.json": local},
    upstream_files={"index.jsx": b"x\n", "mobius.json": upstream},
    base_files={"index.jsx": b"x\n", "mobius.json": base},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  assert app_git.resolve_version_only_conflict(repo, merge.conflict_paths) is None


# Adversarial regressions (Codex review 2026-07-13) — a version bump that SHARES
# its line or file with a real edit must never be auto-resolved (data loss).

def test_same_line_code_edit_is_not_dropped(tmp_path):
  """APP_VERSION and a real const on the SAME line: the whole line conflicts, so
  resolving it would drop the local const. Must bail to owner-resolution."""
  repo = tmp_path / "app"
  _diverge(
    repo,
    local_files={"index.jsx": b'const APP_VERSION = "1.0.1"; const FEATURE = true\n'},
    upstream_files={"index.jsx": b'const APP_VERSION = "2.0.0"; const FEATURE = false\n'},
    base_files={"index.jsx": b'const APP_VERSION = "1.0.0"; const FEATURE = false\n'},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  # Local FEATURE=true must NOT be silently dropped to upstream's line.
  assert app_git.resolve_version_only_conflict(repo, merge.conflict_paths) is None


def test_nested_dependency_version_is_not_resolved(tmp_path):
  """A conflict on a NESTED `version` (a dependency pin), with no top-level
  version, is a real local edit — must not be taken-upstream."""
  repo = tmp_path / "app"
  base = b'{\n  "dependencies": {\n    "version": "1.0.0"\n  }\n}\n'
  local = b'{\n  "dependencies": {\n    "version": "1.0.1"\n  }\n}\n'
  upstream = b'{\n  "dependencies": {\n    "version": "2.0.0"\n  }\n}\n'
  _diverge(
    repo,
    local_files={"index.jsx": b"x\n", "package.json": local},
    upstream_files={"index.jsx": b"x\n", "package.json": upstream},
    base_files={"index.jsx": b"x\n", "package.json": base},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  assert app_git.resolve_version_only_conflict(repo, merge.conflict_paths) is None


def test_bare_const_version_is_not_matched(tmp_path):
  """A generic `const VERSION` (not APP_VERSION) is not a recognised app-version
  identifier — a conflict on it must not be auto-resolved."""
  repo = tmp_path / "app"
  _diverge(
    repo,
    local_files={"index.jsx": b'const VERSION = "1.0.1"\nexport default () => null\n'},
    upstream_files={"index.jsx": b'const VERSION = "2.0.0"\nexport default () => null\n'},
    base_files={"index.jsx": b'const VERSION = "1.0.0"\nexport default () => null\n'},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  assert app_git.resolve_version_only_conflict(repo, merge.conflict_paths) is None


def test_minified_manifest_version_resolves(tmp_path):
  """A single-line (minified) mobius.json whose only difference is the top-level
  version still auto-resolves via the structured JSON check."""
  repo = tmp_path / "app"
  base = b'{"id":"demo","version":"1.0.0","entry":"index.jsx"}\n'
  local = b'{"id":"demo","version":"1.0.1","entry":"index.jsx"}\n'
  upstream = b'{"id":"demo","version":"2.0.0","entry":"index.jsx"}\n'
  _diverge(
    repo,
    local_files={"index.jsx": b"x\n", "mobius.json": local},
    upstream_files={"index.jsx": b"x\n", "mobius.json": upstream},
    base_files={"index.jsx": b"x\n", "mobius.json": base},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  res = app_git.resolve_version_only_conflict(repo, merge.conflict_paths)
  assert res is not None
  assert b'"version":"2.0.0"' in res.tree["mobius.json"]


def test_disjoint_same_file_source_edit_preserved(tmp_path):
  """APP_VERSION bump conflicts while a DISTANT line of the same source file
  carries a local-only edit: resolve to upstream version AND keep the edit.
  (The re-merge cleanly separates the two hunks when they don't touch.)"""
  repo = tmp_path / "app"
  base = b'const APP_VERSION = "1.0.0"\n// a\n// b\n// c\nexport default () => "base"\n'
  local = b'const APP_VERSION = "1.0.1"\n// a\n// b\n// c\nexport default () => "LOCAL"\n'
  upstream = b'const APP_VERSION = "2.0.0"\n// a\n// b\n// c\nexport default () => "base"\n'
  _diverge(
    repo,
    local_files={"index.jsx": local},
    upstream_files={"index.jsx": upstream},
    base_files={"index.jsx": base},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  res = app_git.resolve_version_only_conflict(repo, merge.conflict_paths)
  assert res is not None
  assert b'APP_VERSION = "2.0.0"' in res.tree["index.jsx"]  # upstream version
  assert b'"LOCAL"' in res.tree["index.jsx"]                # local edit kept


def test_adjacent_disjoint_source_edit_bails_safely(tmp_path):
  """A local edit on the line ADJACENT to the version bump groups into the same
  merge hunk, so the re-merge conflicts. That must bail (None) — the edit is
  never dropped; the owner resolves it. Fail-safe over cleverness."""
  repo = tmp_path / "app"
  base = b'const APP_VERSION = "1.0.0"\nexport default () => "base"\n'
  local = b'const APP_VERSION = "1.0.1"\nexport default () => "LOCAL"\n'
  upstream = b'const APP_VERSION = "2.0.0"\nexport default () => "base"\n'
  _diverge(
    repo,
    local_files={"index.jsx": local},
    upstream_files={"index.jsx": upstream},
    base_files={"index.jsx": base},
  )
  merge = app_git.merge_upstream(repo)
  assert merge.status == "conflict"
  assert app_git.resolve_version_only_conflict(repo, merge.conflict_paths) is None
