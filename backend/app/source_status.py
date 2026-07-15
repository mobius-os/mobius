"""Fetch-free aggregate Git status for Contribute's Sources view.

The Contribute app needs one narrow answer — how the live platform and each
installed app relate to the update source already recorded on disk.  Granting
it the general filesystem capability would be much broader than that question,
so this module returns metadata only: refs, ancestry counts, source-diff
magnitudes, and working-tree counts/path names.  It never fetches, writes, or
returns source contents.

Platform compares ``HEAD`` with ``origin/main``.  Apps keep their
installer-owned ``upstream`` branch as the installed baseline, while also
reporting last-fetched ``origin`` and configured GitHub-fork topology.  Nothing
here fetches, so every remote relationship is a view of refs already on disk,
not a promise that GitHub or the catalog was checked just now.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

_GIT_TIMEOUT = 8
# Filenames and numstat counts are safe metadata (never source contents). Keep
# a generous ceiling so Contribute can offer a truthful "Show all files" for
# normal projects while still bounding pathological repositories.
_PATH_LIST_LIMIT = 500
_GITHUB_HTTPS = re.compile(
  r"^https://github\.com/([^/]+)/([^/#]+?)(?:\.git)?/?$", re.IGNORECASE,
)
_GITHUB_SSH = re.compile(
  r"^(?:ssh://git@github\.com/|git@github\.com:)([^/]+)/([^/#]+?)(?:\.git)?$",
  re.IGNORECASE,
)
_RAW_GITHUB = re.compile(
  r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/", re.IGNORECASE,
)


def _git_env(repo: Path) -> dict[str, str]:
  """Scrub inherited git pointers and stop discovery above this repository."""
  env = dict(os.environ)
  for name in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR", "GIT_NAMESPACE",
  ):
    env.pop(name, None)
  env["GIT_CEILING_DIRECTORIES"] = str(repo.resolve().parent)
  return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
  try:
    return subprocess.run(
      ["git", "-C", str(repo), *args], capture_output=True, text=True,
      errors="replace", timeout=_GIT_TIMEOUT, check=False, env=_git_env(repo),
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    return subprocess.CompletedProcess(
      ["git", "-C", str(repo), *args], 1, "", str(exc),
    )


def _rev(repo: Path, ref: str) -> str | None:
  proc = _git(repo, "rev-parse", "--verify", ref)
  value = proc.stdout.strip()
  return value if proc.returncode == 0 and value else None


def _canonical_repo(url: str | None) -> str | None:
  """Turn common GitHub remote/manifest URLs into ``owner/repo``."""
  if not url:
    return None
  raw = url.strip()
  for pattern in (_GITHUB_HTTPS, _GITHUB_SSH, _RAW_GITHUB):
    match = pattern.match(raw)
    if match:
      return f"{match.group(1)}/{match.group(2).removesuffix('.git')}"
  return None


def _last_path_subject(repo: Path, left: str, right: str, path: str) -> str:
  proc = _git(repo, "log", "-1", "--format=%s", f"{left}..{right}", "--", path)
  return proc.stdout.strip() if proc.returncode == 0 else ""


def _install_managed_path(repo: Path, left: str, right: str, path: str) -> bool:
  """Recognize the installer's source-tree adaptations.

  Installed apps deliberately replace ``.gitignore``, normalize executable
  entrypoints, and omit manifest-managed build assets from the editable main
  tree.  Those changes are real Git differences, but presenting them as owner
  customization makes a clean install look modified.  The install commit is
  the durable provenance signal; ``.gitignore`` also survives later merge
  commits, so it is always installation metadata for app comparisons.
  """
  if path == ".gitignore":
    return True
  subject = _last_path_subject(repo, left, right, path).casefold()
  return subject.startswith("install:") or subject.startswith("install ")


def _diff_summary(
  repo: Path,
  left: str,
  right: str,
  *,
  classify_install: bool = False,
) -> dict[str, Any]:
  """Count endpoint tree differences without returning source content."""
  proc = _git(repo, "diff", "--numstat", "--no-renames", left, right, "--")
  if proc.returncode != 0:
    return {
      "available": False, "files": 0, "insertions": 0, "deletions": 0,
      "binary_files": 0, "authored_files": 0, "managed_files": 0,
      "authored_insertions": 0, "authored_deletions": 0,
      "managed_insertions": 0, "managed_deletions": 0,
      "paths": [], "truncated": False,
    }
  files = insertions = deletions = binaries = 0
  authored_files = managed_files = 0
  authored_insertions = authored_deletions = 0
  managed_insertions = managed_deletions = 0
  all_paths: list[dict[str, Any]] = []
  for line in proc.stdout.splitlines():
    parts = line.split("\t", 2)
    if len(parts) != 3:
      continue
    add_raw, del_raw, path = parts
    files += 1
    binary = add_raw == "-" or del_raw == "-"
    if binary:
      binaries += 1
      add = delete = None
    else:
      try:
        add, delete = int(add_raw), int(del_raw)
      except ValueError:
        add = delete = 0
      insertions += add
      deletions += delete
    managed = bool(
      classify_install and _install_managed_path(repo, left, right, path)
    )
    if managed:
      managed_files += 1
      managed_insertions += add or 0
      managed_deletions += delete or 0
    else:
      authored_files += 1
      authored_insertions += add or 0
      authored_deletions += delete or 0
    all_paths.append({
      "path": path, "insertions": add, "deletions": delete,
      "binary": binary, "group": "managed" if managed else "authored",
    })
  # Owner-authored paths are the decision-bearing part of the comparison, so
  # keep them visible before install-managed adaptations when the preview caps.
  all_paths.sort(key=lambda item: (item["group"] == "managed", item["path"]))
  paths = all_paths[:_PATH_LIST_LIMIT]
  return {
    "available": True,
    "files": files,
    "insertions": insertions,
    "deletions": deletions,
    "binary_files": binaries,
    "authored_files": authored_files,
    "managed_files": managed_files,
    "authored_insertions": authored_insertions,
    "authored_deletions": authored_deletions,
    "managed_insertions": managed_insertions,
    "managed_deletions": managed_deletions,
    "paths": paths,
    "truncated": files > len(paths),
  }


def _comparison_counts(repo: Path, left: str, right: str) -> tuple[int | None, int | None]:
  proc = _git(repo, "rev-list", "--left-right", "--count", f"{left}...{right}")
  parts = proc.stdout.split()
  if proc.returncode == 0 and len(parts) == 2:
    try:
      behind, ahead = int(parts[0]), int(parts[1])
      return ahead, behind
    except ValueError:
      pass
  return None, None


def _remote_default_ref(repo: Path, remote: str) -> str | None:
  symbolic = _git(
    repo, "symbolic-ref", "--quiet", "--short",
    f"refs/remotes/{remote}/HEAD",
  )
  value = symbolic.stdout.strip()
  if symbolic.returncode == 0 and value and _rev(repo, value):
    return value
  for branch in ("main", "master"):
    candidate = f"{remote}/{branch}"
    if _rev(repo, candidate):
      return candidate
  return None


def _remote_topology(
  repo: Path,
  origin_repo: str | None,
  *,
  classify_install: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
  """Return sanitized, last-fetched origin/fork positions."""
  origin_ref = _remote_default_ref(repo, "origin")
  origin_sha = _rev(repo, origin_ref) if origin_ref else None
  origin: dict[str, Any] = {
    "repo": origin_repo,
    "ref": origin_ref,
    "sha": origin_sha,
    "local_ahead": None,
    "local_behind": None,
    "local_tree": None,
  }
  if origin_ref and _rev(repo, "HEAD"):
    local_ahead, local_behind = _comparison_counts(repo, origin_ref, "HEAD")
    origin.update({
      "local_ahead": local_ahead,
      "local_behind": local_behind,
      "local_tree": _diff_summary(
        repo, origin_ref, "HEAD", classify_install=classify_install,
      ),
    })
  names = _git(repo, "remote").stdout.splitlines()
  forks: list[dict[str, Any]] = []
  for name in names:
    if name == "origin":
      continue
    remote_url = _git(repo, "remote", "get-url", name)
    fork_repo = _canonical_repo(remote_url.stdout.strip()) if remote_url.returncode == 0 else None
    if not fork_repo or fork_repo.casefold() == (origin_repo or "").casefold():
      continue
    fork_ref = _remote_default_ref(repo, name)
    fork_sha = _rev(repo, fork_ref) if fork_ref else None
    ahead = behind = None
    tree = None
    if origin_ref and fork_ref:
      ahead, behind = _comparison_counts(repo, origin_ref, fork_ref)
      tree = _diff_summary(repo, origin_ref, fork_ref)
    forks.append({
      "repo": fork_repo,
      "ref": fork_ref,
      "sha": fork_sha,
      "ahead": ahead,
      "behind": behind,
      "tree": tree,
    })
  forks.sort(key=lambda item: item["repo"].casefold())
  return origin, forks


def _working_summary(repo: Path) -> dict[str, Any]:
  """Parse porcelain status into staged/unstaged/untracked/conflict groups."""
  proc = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
  if proc.returncode != 0:
    return {
      "available": False, "files": 0, "staged": 0, "unstaged": 0,
      "untracked": 0, "conflicts": 0, "paths": [], "truncated": False,
    }
  records = [item for item in proc.stdout.split("\0") if item]
  files = staged = unstaged = untracked = conflicts = 0
  paths: list[dict[str, str]] = []
  conflict_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
  i = 0
  while i < len(records):
    record = records[i]
    if len(record) < 3:
      i += 1
      continue
    code, path = record[:2], record[3:]
    files += 1
    if code == "??":
      untracked += 1
      group = "untracked"
    elif code in conflict_codes:
      conflicts += 1
      group = "conflict"
    else:
      has_staged = code[0] not in " ?"
      has_unstaged = code[1] not in " "
      staged += int(has_staged)
      unstaged += int(has_unstaged)
      group = "staged" if has_staged and not has_unstaged else "unstaged"
    if len(paths) < _PATH_LIST_LIMIT:
      paths.append({"path": path, "status": code, "group": group})
    # In -z porcelain, rename/copy records carry the old path as the next item.
    if code[0] in "RC" and i + 1 < len(records):
      i += 1
    i += 1
  merge_active = _rev(repo, "MERGE_HEAD") is not None
  return {
    "available": True,
    "files": files,
    "staged": staged,
    "unstaged": unstaged,
    "untracked": untracked,
    "conflicts": conflicts,
    "merge_active": merge_active,
    "paths": paths,
    "truncated": files > len(paths),
  }


def _project_status(
  *, repo: Path, kind: str, key: str, name: str, slug: str | None,
  version: str | None, manifest_url: str | None,
) -> dict[str, Any]:
  response: dict[str, Any] = {
    "key": key,
    "kind": kind,
    "name": name,
    "slug": slug,
    "version": version,
    "available": False,
    "canonical_repo": "mobius-os/mobius" if kind == "platform" else None,
    "origin": None,
    "forks": [],
    "branch": None,
    "head_sha": None,
    "base_ref": "origin/main" if kind == "platform" else "upstream",
    "base_sha": None,
    "ahead": None,
    "behind": None,
    "tree": None,
    "working": None,
    "state": "unavailable",
  }
  if not repo.is_dir() or not (repo / ".git").exists():
    return response

  branch_proc = _git(repo, "branch", "--show-current")
  branch = branch_proc.stdout.strip() or None
  head = _rev(repo, "HEAD")
  base_ref = response["base_ref"]
  base = _rev(repo, base_ref)
  origin_proc = _git(repo, "remote", "get-url", "origin")
  origin_url = origin_proc.stdout.strip() if origin_proc.returncode == 0 else None
  if kind != "platform":
    response["canonical_repo"] = (
      _canonical_repo(origin_url) or _canonical_repo(manifest_url)
    )
  origin, forks = _remote_topology(
    repo,
    response["canonical_repo"],
    classify_install=(kind == "app"),
  )
  response.update({"origin": origin, "forks": forks})

  response.update({
    "available": bool(head),
    "branch": branch,
    "detached": bool(head and not branch),
    "head_sha": head,
    "base_sha": base,
    "has_update_source": bool(base),
  })
  working = _working_summary(repo)
  response["working"] = working
  if not head:
    return response
  if not base:
    response["state"] = "local_only"
    return response

  ahead, behind = _comparison_counts(repo, base_ref, "HEAD")
  tree = _diff_summary(
    repo, base_ref, "HEAD", classify_install=(kind == "app"),
  )
  response.update({"behind": behind, "ahead": ahead, "tree": tree})

  has_working = bool(working.get("files"))
  has_conflict = bool(working.get("conflicts") or working.get("merge_active"))
  has_local = bool(tree.get("authored_files", tree.get("files")))
  has_managed = bool(tree.get("managed_files"))
  if has_conflict:
    state = "conflict"
  elif has_working:
    state = "working"
  elif behind and (ahead or has_local):
    state = "diverged"
  elif behind:
    state = "incoming"
  elif has_local:
    state = "customized"
  elif has_managed:
    state = "adapted"
  else:
    state = "aligned"
  response["state"] = state
  return response


def build_platform_status() -> dict[str, Any]:
  """Inspect the live platform clone against its last-fetched origin/main."""
  settings = get_settings()
  data_dir = Path(settings.data_dir).resolve()
  return _project_status(
    repo=data_dir / "platform",
    kind="platform", key="platform", name="Möbius", slug=None,
    version=None, manifest_url=None,
  )


def build_app_status(app: dict[str, Any]) -> dict[str, Any] | None:
  """Inspect one validated live app source row; invalid paths are excluded."""
  settings = get_settings()
  app_root = Path(settings.data_dir).resolve() / "apps"
  raw_source = app.get("source_dir")
  if not isinstance(raw_source, str) or not raw_source:
    return None
  try:
    raw_path = Path(raw_source)
    if raw_path.is_symlink():
      return None
    source_dir = raw_path.resolve()
    if source_dir.parent != app_root or source_dir.name.isdigit():
      return None
  except (OSError, RuntimeError, ValueError):
    # A corrupt DB path must not turn this narrow metadata surface into an
    # arbitrary filesystem probe.
    return None
  return _project_status(
    repo=source_dir,
    kind="app",
    key=f"app:{app.get('id')}",
    name=str(app.get("name") or app.get("slug") or "Unnamed app"),
    slug=str(app.get("slug") or "") or None,
    version=str(app.get("version") or "") or None,
    manifest_url=(str(app.get("manifest_url")) if app.get("manifest_url") else None),
  )


def build_source_status(apps: list[dict[str, Any]]) -> dict[str, Any]:
  """Return the platform plus every live app source repo in one snapshot.

  Routes should hold ``source_dir_lock`` around each :func:`build_app_status`
  call.  This aggregate remains useful to tests and non-serving callers where
  no watcher can race the inspection.
  """
  platform = build_platform_status()
  app_results: list[dict[str, Any]] = []
  for app in apps:
    status = build_app_status(app)
    if status is not None:
      app_results.append(status)
  app_results.sort(key=lambda item: item["name"].casefold())
  return {
    "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "fetch_free": True,
    "platform": platform,
    "apps": app_results,
  }
