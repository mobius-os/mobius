"""Bounded Git operations scoped to one Project root.

Reads may project a project nested inside the platform repository. Writes are
stricter: Projects can initialize or commit only when their own root is the
repository root, so an owner action can never stage unrelated Möbius files.
"""

from __future__ import annotations

import os
import json
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_GIT_TIMEOUT_SECONDS = 5
_STATUS_OUTPUT_MAX = 512 * 1024
_DIFF_OUTPUT_MAX = 1024 * 1024
_CHANGE_LIMIT = 1000
_LINE_LIMIT = 5000
_HUNK_RE = re.compile(
  r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
  r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@",
)
_GITHUB_REPOSITORY_RE = re.compile(
  r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
  r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)


@dataclass(frozen=True)
class SourceVisibility:
  """Which workspace paths Git review treats as non-source.

  ``hidden_dirs`` hide a folder name at any depth; ``hidden_root_dirs`` hide it
  only directly under the workspace root, matching ``workspace_files.list_entries``.
  """

  hidden_dirs: frozenset[str] = frozenset()
  hidden_root_dirs: frozenset[str] = frozenset()

  def hides(self, relative: PurePosixPath) -> bool:
    parts = relative.parts
    return bool(parts) and (
      parts[0] in self.hidden_root_dirs
      or any(part in self.hidden_dirs for part in parts)
    )


# The one project-root folder of managed build output. Review, commit staging,
# the file tree and build fingerprints all reserve exactly this folder, so a
# nested `src/artifacts/` is ordinary, reviewable source.
PROJECT_VISIBILITY = SourceVisibility(hidden_root_dirs=frozenset({"artifacts"}))


class GitProjectError(RuntimeError):
  """An owner-actionable repository error safe to surface at the route."""


def _safe_git_error(value: str) -> str:
  value = re.sub(r"https://[^/@\s]+@", "https://***@", value)
  return value[:400]


def _run_git_write(cwd: Path, *args: str, timeout: int = 15) -> str:
  env = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
  }
  try:
    result = subprocess.run(
      ["git", "-C", str(cwd), *args],
      stdin=subprocess.DEVNULL,
      capture_output=True,
      env=env,
      timeout=timeout,
      check=False,
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise GitProjectError("Git did not finish in time.") from exc
  if result.returncode != 0:
    detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
    message = detail[-1] if detail else "Git could not complete that action."
    raise GitProjectError(_safe_git_error(message))
  return result.stdout.decode("utf-8", "replace").strip()


def _stage_project(root: Path) -> None:
  excluded = (f":(exclude){name}" for name in sorted(PROJECT_VISIBILITY.hidden_root_dirs))
  _run_git_write(root, "add", "-A", "--", ".", *excluded)


def _ensure_commit_identity(root: Path) -> None:
  for key, fallback in (
    ("user.name", "Möbius Owner"),
    ("user.email", "owner@mobius.local"),
  ):
    code, output, _ = _run_git(root, "config", "--get", key, limit=1024)
    if code != 0 or not output.strip():
      _run_git_write(root, "config", key, fallback)


def initialize_project(root: Path) -> dict:
  """Create a project-owned repository and its first local snapshot."""
  root = root.resolve()
  # Project roots normally sit beneath /data's safety-net repository. A parent
  # repository is not project versioning: initialize a nested, project-owned
  # repository and let future commits resolve to that nearest root.
  if (root / ".git").exists():
    raise GitProjectError("This project already has its own repository.")
  _run_git_write(root, "init", "-b", "main")
  _ensure_commit_identity(root)
  _stage_project(root)
  staged = subprocess.run(
    ["git", "-C", str(root), "diff", "--cached", "--quiet"],
    stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=10,
  )
  if staged.returncode == 1:
    _run_git_write(root, "commit", "-m", "Start project")
  elif staged.returncode not in (0, 1):
    raise GitProjectError("Git could not inspect the first snapshot.")
  return project_status(root)


def commit_project(root: Path, message: str, expected_head: str | None = None) -> dict:
  """Commit the current project tree without ever staging a parent repo."""
  root = root.resolve()
  context = _discover(root)
  if context is None:
    raise GitProjectError("Start versioning before creating a commit.")
  if context.repo != root:
    raise GitProjectError(
      "This project is inside a shared repository and cannot be committed here."
    )
  _ensure_commit_identity(root)
  _current_branch, current_head = _branch_and_head(context)
  if expected_head is not None and expected_head != current_head:
    raise GitProjectError("The project changed since this view was loaded. Refresh and try again.")
  _stage_project(root)
  staged = subprocess.run(
    ["git", "-C", str(root), "diff", "--cached", "--quiet"],
    stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=10,
  )
  if staged.returncode == 0:
    raise GitProjectError("There are no changes to commit.")
  if staged.returncode != 1:
    raise GitProjectError("Git could not inspect the staged changes.")
  _run_git_write(root, "commit", "-m", message)
  return project_status(root)


def _owned_context(root: Path) -> _GitContext:
  root = root.resolve()
  context = _discover(root)
  if context is None:
    raise GitProjectError("Start versioning before using a remote.")
  if context.repo != root:
    raise GitProjectError(
      "Start project versioning before connecting or publishing this workspace."
    )
  return context


def _github_repository(remote_url: str) -> str | None:
  value = remote_url.strip()
  match = re.fullmatch(
    r"(?:https://github\.com/|git@github\.com:)([^/\s]+/[^/\s]+?)(?:\.git)?/?",
    value,
    flags=re.IGNORECASE,
  )
  if match is None:
    return None
  repository = match.group(1).removesuffix(".git")
  return repository if _GITHUB_REPOSITORY_RE.fullmatch(repository) else None


def connect_github_remote(root: Path, repository: str) -> dict:
  """Attach one validated GitHub origin without replacing an existing remote."""
  context = _owned_context(root)
  repository = repository.strip().removesuffix(".git").strip("/")
  if not _GITHUB_REPOSITORY_RE.fullmatch(repository):
    raise GitProjectError("Use a GitHub repository in owner/name form.")
  code, output, _truncated = _run_git(
    context.repo, "config", "--get", "remote.origin.url", limit=4096,
  )
  if code == 0 and output.strip():
    existing = _github_repository(output.decode("utf-8", "replace"))
    if existing == repository:
      return project_remote_status(root)
    raise GitProjectError(
      "This project already has an origin. Remove or change it outside this flow first."
    )
  _run_git_write(
    context.repo, "remote", "add", "origin",
    f"https://github.com/{repository}.git",
  )
  return project_remote_status(root)


def _branch_upstream(context: _GitContext, branch: str) -> tuple[str | None, str | None]:
  """Return ``(comparison ref, push branch)`` from the branch's upstream config.

  The push branch is the branch on ``origin`` that Push updates. Git's own
  upstream config names it (``branch.<b>.remote`` and ``branch.<b>.merge``);
  never reverse-map it from a tracking-ref name, which a local branch or a
  custom fetch refspec can imitate. An upstream on another remote, a local
  branch, or a remote Git cannot resolve has no GitHub destination: its push
  branch is ``None`` and Push refuses rather than guess one.
  """
  code, output, _truncated = _run_git(
    context.repo, "for-each-ref",
    "--format=%(upstream:remotename)%00%(upstream:remoteref)%00%(upstream)%00%(upstream:short)",
    f"refs/heads/{branch}", limit=4096,
  )
  fields = output.decode("utf-8", "replace").rstrip("\n").split("\0") if code == 0 else []
  remote, remote_ref, tracking_ref, tracking = (fields + ["", "", "", ""])[:4]
  if not remote:
    code, _output, _truncated = _run_git(
      context.repo, "config", "--get", f"branch.{branch}.merge", limit=1024,
    )
    if code == 0:
      return None, None
    tracking_ref, tracking = f"refs/remotes/origin/{branch}", f"origin/{branch}"
    push_branch: str | None = branch
  elif remote == "origin" and remote_ref.startswith("refs/heads/"):
    push_branch = remote_ref.removeprefix("refs/heads/")
  else:
    push_branch = None
  # A configured upstream that has not been fetched yet still names the push
  # branch; there is just nothing local to compare with until the next fetch.
  if not tracking_ref or not tracking:
    return None, push_branch
  code, _output, _truncated = _run_git(
    context.repo, "show-ref", "--verify", "--quiet", tracking_ref, limit=128,
  )
  return (tracking if code == 0 else None), push_branch


def _revision_count(context: _GitContext, revision: str) -> int:
  code, output, _truncated = _run_git(
    context.repo, "rev-list", "--count", revision, limit=128,
  )
  if code != 0:
    return 0
  try:
    return max(0, int(output.strip() or b"0"))
  except ValueError:
    return 0


def project_remote_status(root: Path) -> dict:
  """Fetch-free, credential-free GitHub synchronization projection."""
  root = root.resolve()
  context = _discover(root)
  if context is None or context.repo != root:
    return {
      "available": False,
      "repository_scope": None if context is None else "shared",
      "connected": False,
      "repository": None,
      "web_url": None,
      "branch": None,
      "head": None,
      "upstream": None,
      "push_branch": None,
      "ahead": 0,
      "behind": 0,
      "diverged": False,
      "commits": [],
      "dirty": False,
    }
  branch, head = _branch_and_head(context)
  status = project_status(root)
  code, output, _truncated = _run_git(
    context.repo, "config", "--get", "remote.origin.url", limit=4096,
  )
  repository = (
    _github_repository(output.decode("utf-8", "replace"))
    if code == 0 else None
  )
  upstream, push_branch = _branch_upstream(context, branch) if branch else (None, None)
  ahead = 0
  behind = 0
  commits: list[dict[str, str]] = []
  if upstream:
    code, counts, _truncated = _run_git(
      context.repo, "rev-list", "--left-right", "--count",
      f"HEAD...{upstream}", limit=128,
    )
    if code == 0:
      try:
        ahead, behind = [int(value) for value in counts.split()[:2]]
      except (TypeError, ValueError):
        ahead = behind = 0
    revision_range = f"{upstream}..HEAD"
  else:
    ahead = _revision_count(context, "HEAD") if repository else 0
    revision_range = "HEAD"
  if repository and ahead:
    code, output, _truncated = _run_git(
      context.repo, "log", "-n", "20", "--format=%h%x09%s", revision_range,
      limit=32 * 1024,
    )
    if code == 0:
      for line in output.decode("utf-8", "replace").splitlines():
        commit, _, subject = line.partition("\t")
        if commit:
          commits.append({"id": commit[:16], "subject": subject[:240]})
  return {
    "available": True,
    "repository_scope": "project",
    "connected": repository is not None,
    "repository": repository,
    "web_url": f"https://github.com/{repository}" if repository else None,
    "branch": branch,
    "head": head,
    "upstream": upstream,
    "push_branch": push_branch,
    "ahead": ahead,
    "behind": behind,
    "diverged": ahead > 0 and behind > 0,
    "commits": commits,
    "dirty": bool(status.get("changes")),
  }


def fetch_project(root: Path) -> dict:
  """Refresh remote refs without changing the working project."""
  context = _owned_context(root)
  status = project_remote_status(root)
  if not status["connected"]:
    raise GitProjectError("Connect a GitHub repository before fetching.")
  _run_git_write(context.repo, "fetch", "--prune", "origin", timeout=60)
  return project_remote_status(root)


def pull_project(root: Path, expected_head: str | None = None) -> dict:
  """Fast-forward a clean project; never merge, rebase, or discard work."""
  context = _owned_context(root)
  status = project_remote_status(root)
  if status["dirty"]:
    raise GitProjectError("Commit or discard local changes before pulling.")
  if expected_head is not None and status["head"] != expected_head:
    raise GitProjectError("The project changed since this view was loaded. Refresh and try again.")
  if not status["connected"]:
    raise GitProjectError("Connect a GitHub repository before pulling.")
  _run_git_write(context.repo, "fetch", "--prune", "origin", timeout=60)
  status = project_remote_status(root)
  if status["diverged"]:
    raise GitProjectError("Local and GitHub history diverged. Review them outside the fast-forward flow.")
  if not status["behind"]:
    return status
  if not status["upstream"]:
    raise GitProjectError("Choose an upstream branch before pulling.")
  _run_git_write(context.repo, "merge", "--ff-only", status["upstream"], timeout=60)
  return project_remote_status(root)


def push_project(
  root: Path,
  expected_head: str | None = None,
  expected_push_branch: str | None = None,
) -> dict:
  """Publish the reviewed commit to the reviewed origin branch.

  ``expected_push_branch`` is the destination the owner confirmed. Push never
  force-pushes, and it pushes the verified commit rather than the moving branch
  name, so a commit made after the check cannot ride along.
  """
  context = _owned_context(root)
  status = project_remote_status(root)
  if status["dirty"]:
    raise GitProjectError("Commit local changes before pushing.")
  if expected_head is not None and status["head"] != expected_head:
    raise GitProjectError("The project changed since this view was loaded. Refresh and try again.")
  if not status["connected"]:
    raise GitProjectError("Connect a GitHub repository before pushing.")
  branch = status["branch"]
  if not branch:
    raise GitProjectError("Switch to a named branch before pushing.")
  destination = status["push_branch"]
  if not destination:
    raise GitProjectError("Track a branch on the connected GitHub remote before pushing.")
  if expected_push_branch is not None and expected_push_branch != destination:
    raise GitProjectError("The project changed since this view was loaded. Refresh and try again.")
  code, output, _truncated = _run_git(
    context.repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", limit=128,
  )
  commit = output.decode("ascii", "replace").strip() if code == 0 else ""
  if not commit or not status["head"] or not commit.startswith(status["head"]):
    raise GitProjectError("The project changed since this view was loaded. Refresh and try again.")
  _run_git_write(context.repo, "fetch", "--prune", "origin", timeout=60)
  refreshed = project_remote_status(root)
  reviewed = (branch, status["head"], destination)
  if (refreshed["branch"], refreshed["head"], refreshed["push_branch"]) != reviewed:
    raise GitProjectError("The project changed since this view was loaded. Refresh and try again.")
  if refreshed["behind"]:
    raise GitProjectError("GitHub has newer commits. Pull and review them before pushing.")
  _run_git_write(
    context.repo, "push", "origin", f"{commit}:refs/heads/{destination}", timeout=60,
  )
  # A commit refspec cannot carry --set-upstream; record the same destination
  # so the next status compares with the branch that was just updated.
  _run_git_write(context.repo, "config", f"branch.{branch}.remote", "origin")
  _run_git_write(context.repo, "config", f"branch.{branch}.merge", f"refs/heads/{destination}")
  return project_remote_status(root)


@dataclass(frozen=True)
class _GitContext:
  repo: Path
  root: Path
  scope: str


def _run_git(
  cwd: Path, *args: str, limit: int = _STATUS_OUTPUT_MAX,
) -> tuple[int, bytes, bool]:
  """Run one fetch-free Git read with bounded output and wall time."""
  env = {
    **os.environ,
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
  }
  try:
    process = subprocess.Popen(
      ["git", "-C", str(cwd), *args],
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
      env=env,
    )
  except OSError:
    return 127, b"", False
  assert process.stdout is not None
  selector = selectors.DefaultSelector()
  selector.register(process.stdout, selectors.EVENT_READ)
  deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
  output = bytearray()
  truncated = False
  timed_out = False
  try:
    os.set_blocking(process.stdout.fileno(), False)
    while True:
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        timed_out = True
        truncated = True
        process.kill()
        break
      events = selector.select(timeout=min(remaining, 0.1))
      if not events:
        if process.poll() is not None:
          break
        continue
      try:
        chunk = os.read(
          process.stdout.fileno(), min(64 * 1024, limit + 1 - len(output)),
        )
      except BlockingIOError:
        continue
      if not chunk:
        break
      output.extend(chunk)
      if len(output) > limit:
        del output[limit:]
        truncated = True
        process.terminate()
        break
    try:
      returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
      process.kill()
      process.wait()
      returncode = 124
      truncated = True
    # Hitting our output ceiling is an honest partial projection, not a Git
    # failure. Callers surface `truncated` and parse only complete records.
    if truncated and not timed_out:
      returncode = 0
    return returncode, bytes(output), truncated
  finally:
    selector.close()
    process.stdout.close()


def _discover(root: Path) -> _GitContext | None:
  root = root.resolve()
  code, output, _truncated = _run_git(
    root, "rev-parse", "--show-toplevel", limit=4096,
  )
  if code != 0:
    return None
  try:
    repo = Path(output.decode("utf-8").strip()).resolve()
    relative = root.relative_to(repo)
  except (UnicodeDecodeError, ValueError):
    return None
  return _GitContext(
    repo=repo,
    root=root,
    scope=relative.as_posix() if relative.parts else ".",
  )


def _context_path(context: _GitContext, path: Path) -> str:
  relative = path.resolve().relative_to(context.root).as_posix()
  return relative if context.scope == "." else f"{context.scope}/{relative}"


def _project_relative(
  context: _GitContext,
  repo_path: str,
  *,
  visibility: SourceVisibility,
) -> str | None:
  value = PurePosixPath(repo_path)
  if context.scope == ".":
    relative = value
  else:
    try:
      relative = value.relative_to(PurePosixPath(context.scope))
    except ValueError:
      return None
  path = relative.as_posix()
  if not path or path == "." or visibility.hides(relative):
    return None
  return path


def _status_name(code: str) -> str:
  if "U" in code or code in {"AA", "DD"}:
    return "conflict"
  if code == "??":
    return "untracked"
  if "A" in code:
    return "added"
  if "D" in code:
    return "deleted"
  return "modified"


def _branch_and_head(context: _GitContext) -> tuple[str | None, str | None]:
  code, output, _truncated = _run_git(
    context.repo, "symbolic-ref", "--quiet", "--short", "HEAD", limit=512,
  )
  branch = output.decode("utf-8", "replace").strip() if code == 0 else None
  code, output, _truncated = _run_git(
    context.repo, "rev-parse", "--short=8", "HEAD", limit=512,
  )
  head = output.decode("ascii", "replace").strip() if code == 0 else None
  return branch, head


def project_status(
  root: Path,
  *,
  visibility: SourceVisibility = PROJECT_VISIBILITY,
) -> dict:
  """Return repository identity and working changes confined to ``root``."""
  context = _discover(root)
  if context is None:
    return {
      "available": False,
      "branch": None,
      "head": None,
      "repository_scope": None,
      "changes": [],
      "counts": {},
      "truncated": False,
    }
  code, output, truncated = _run_git(
    context.repo,
    "status", "--porcelain=v1", "-z", "--untracked-files=all",
    "--no-renames", "--", context.scope,
  )
  if code != 0:
    return {
      "available": False,
      "branch": None,
      "head": None,
      "repository_scope": None,
      "changes": [],
      "counts": {},
      "truncated": truncated,
    }
  changes = []
  counts: dict[str, int] = {}
  records = output.split(b"\0")
  if truncated and output and not output.endswith(b"\0"):
    records = records[:-1]
  for raw in records:
    if len(raw) < 4:
      continue
    record = raw.decode("utf-8", "replace")
    code_value = record[:2]
    path = _project_relative(context, record[3:], visibility=visibility)
    if path is None:
      continue
    status = _status_name(code_value)
    counts[status] = counts.get(status, 0) + 1
    if len(changes) < _CHANGE_LIMIT:
      changes.append({
        "path": path,
        "status": status,
        "staged": code_value[0] not in (" ", "?"),
      })
    else:
      truncated = True
  changes.sort(key=lambda row: row["path"].lower())
  branch, head = _branch_and_head(context)
  line_stats = _status_line_stats(context, changes, visibility=visibility)
  return {
    "line_stats": line_stats,
    "available": True,
    "branch": branch,
    "head": head,
    "repository_scope": "project" if context.repo == context.root else "shared",
    "changes": changes,
    "counts": counts,
    "truncated": truncated,
  }


def _untracked_diff(
  target: Path, status: str, *, limit: int | None = None,
) -> dict:
  if limit is None:
    limit = _DIFF_OUTPUT_MAX
  read_failed = False
  try:
    if target.is_symlink():
      content = os.readlink(target).encode()
    else:
      with target.open("rb") as stream:
        content = stream.read(limit + 1)
  except OSError:
    content = b""
    read_failed = True
  truncated = read_failed or len(content) > limit
  content = content[:limit]
  try:
    text = content.decode("utf-8")
  except UnicodeDecodeError:
    text = None
  if text is None or b"\0" in content:
    return {
      "status": status,
      "additions": 0,
      "deletions": 0,
      "changed_lines": [],
      "binary": True,
      "patch": "",
      "truncated": truncated,
    }
  count = 0 if not text else len(text.splitlines())
  return {
    "status": status,
    "patch_lines": text.splitlines(),
    "additions": count,
    "deletions": 0,
    "changed_lines": list(range(1, min(count, _LINE_LIMIT) + 1)),
    "binary": False,
    "truncated": truncated or count > _LINE_LIMIT,
  }


def project_file_diff(
  root: Path,
  target: Path,
  *,
  visibility: SourceVisibility = PROJECT_VISIBILITY,
) -> dict:
  """Return compact line annotations for one confined Project file."""
  context = _discover(root)
  if context is None:
    return {
      "available": False,
      "status": "clean",
      "additions": 0,
      "deletions": 0,
      "changed_lines": [],
      "binary": False,
      "truncated": False,
    }
  repo_path = _context_path(context, target)
  relative = _project_relative(context, repo_path, visibility=visibility)
  pathspec = f":(literal){repo_path}"
  status = None
  status_truncated = False
  if relative is not None:
    code, output, status_truncated = _run_git(
      context.repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
      "--no-renames", "--", pathspec,
    )
    if code == 0:
      for record in output.split(b"\0")[:-1]:
        if record[3:] == os.fsencode(repo_path):
          status = _status_name(record[:2].decode("ascii"))
          break
    else:
      status_truncated = True
  if status is None:
    return {
      "available": True,
      "status": "clean",
      "additions": 0,
      "deletions": 0,
      "changed_lines": [],
      "binary": False,
      "truncated": status_truncated,
    }
  if status == "untracked":
    diff = _untracked_diff(target, status)
    lines = diff.pop("patch_lines", [])
    old_name, new_name = json.dumps("a/" + relative), json.dumps("b/" + relative)
    diff["patch"] = (f"diff --git {old_name} {new_name}\nnew file mode 100644\n--- /dev/null\n+++ {new_name}\n@@ -0,0 +1,{len(lines)} @@\n"
                     + "".join("+" + line + "\n" for line in lines)) if lines else ""
    return {"available": True, **diff}

  code, output, truncated = _run_git(
    context.repo,
    "diff", "--no-ext-diff", "--no-color", "--unified=3", "HEAD",
    "--", pathspec,
    limit=_DIFF_OUTPUT_MAX,
  )
  if code != 0:
    return {
      "available": True,
      "status": status,
      "additions": 0,
      "deletions": 0,
      "changed_lines": [],
      "binary": False,
      "truncated": True,
    }
  text = output.decode("utf-8", "replace")
  binary = "Binary files " in text or "GIT binary patch" in text
  additions = deletions = 0
  changed_lines: list[int] = []
  number = None
  for line in text.splitlines():
    match = _HUNK_RE.match(line)
    if match:
      number = int(match.group("new_start"))
    elif number is not None and line.startswith("+"):
      additions += 1
      if len(changed_lines) < _LINE_LIMIT:
        changed_lines.append(number)
      else:
        truncated = True
      number += 1
    elif number is not None and line.startswith("-"):
      deletions += 1
    elif number is not None and line.startswith(" "):
      number += 1
  return {
    "available": True,
    "status": status,
    "patch": text,
    "additions": additions,
    "deletions": deletions,
    "changed_lines": sorted(set(changed_lines)),
    "binary": binary,
    "truncated": truncated,
  }


def applied_source_status(
  root: Path,
  accepted_commit: str | None,
  status: dict,
  *,
  visibility: SourceVisibility = PROJECT_VISIBILITY,
) -> dict:
  """Compare saved source with the accepted build, not merely with local HEAD.

  A local commit is not a deployment. Unknown baselines and bounded read failures
  must remain unknown, never report a clean working tree as a current build.
  """
  unknown = {"state": "unknown"}
  if not status.get("available") or status.get("truncated"):
    return unknown
  if not isinstance(accepted_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", accepted_commit):
    return unknown
  context = _discover(root)
  if context is None or context.repo != context.root:
    return unknown
  code, output, truncated = _run_git(
    root, "diff", "--name-only", "-z", "--no-ext-diff", accepted_commit, "--", ".",
  )
  if code != 0 or truncated:
    return unknown
  changed = any(_project_relative(context, path.decode("utf-8", "replace"), visibility=visibility)
                for path in output.split(b"\0") if path)
  # Git diff omits new, untracked source files. The existing bounded status read
  # already includes them and applies the Project's source visibility boundary.
  added = any(row["status"] == "untracked" for row in status.get("changes", []))
  return {"state": "pending" if changed or added else "current"}


def _status_line_stats(context, changes, *, visibility: SourceVisibility) -> dict:
  """One bounded numstat read plus a shared read budget for untracked text."""
  code, output, truncated = _run_git(
    context.repo, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "HEAD", "--", context.scope,
  )
  by_path = {}
  for record in output.split(b"\0"):
    fields = record.split(b"\t", 2)
    if len(fields) != 3:
      continue
    path = _project_relative(context, fields[2].decode("utf-8", "replace"), visibility=visibility)
    if path is None:
      continue
    binary = fields[0] == b"-"
    if not binary and not (fields[0].isdigit() and fields[1].isdigit()):
      truncated = True
      continue
    by_path[path] = {"additions": 0 if binary else int(fields[0]), "deletions": 0 if binary else int(fields[1]), "binary": binary}
  budget = _DIFF_OUTPUT_MAX
  for row in changes:
    if row["status"] == "untracked":
      target = context.root / row["path"]
      if budget <= 0 or not target.resolve().is_relative_to(context.root):
        truncated = True
        continue
      diff = _untracked_diff(target, "untracked", limit=budget)
      try:
        budget -= min(target.lstat().st_size, budget)
      except OSError:
        truncated = True
      truncated = truncated or diff["truncated"]
      by_path[row["path"]] = {key: diff[key] for key in ("additions", "deletions", "binary")}
    if row["path"] in by_path:
      row.update(by_path[row["path"]])
  return {"available": code == 0, "additions": sum(row["additions"] for row in by_path.values()),
          "deletions": sum(row["deletions"] for row in by_path.values()), "truncated": truncated}
