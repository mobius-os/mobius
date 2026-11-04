"""Read-only identity checks for Git checkout folders.

Git may reuse a pruned linked-worktree administrative directory. A surviving
folder's .git pointer then resolves successfully, but names another checkout's
HEAD and index. The reciprocal pointer, not a successful Git command, proves
which folder owns that state. This is a point-in-time preflight, not a lock.
"""

from pathlib import Path


def linked_worktree_owns_checkout(repo: Path, git_dir: Path) -> bool:
  """Whether a linked administrative directory still registers this folder."""
  back_reference = git_dir / "gitdir"
  if not back_reference.is_file() or back_reference.is_symlink():
    return False
  try:
    raw = back_reference.read_text().strip()
    if not raw:
      return False
    registered = Path(raw)
    if not registered.is_absolute():
      registered = back_reference.parent / registered
    return registered.resolve() == (repo / ".git").resolve()
  except (OSError, RuntimeError, ValueError):
    return False


def checkout_ownership_error(repo: Path) -> str | None:
  """Explain a stale Git pointer without running Git or changing any files.

  Ordinary repositories and separate-Git-directory clones have no linked
  administrative state to alias. Missing checkouts retain their caller's
  existing missing-repository handling; this is not a Git validity checker.
  """
  marker = repo / ".git"
  if not marker.is_file():
    return None
  try:
    value = marker.read_text().strip()
    if not value.startswith("gitdir:") or not value[7:].strip():
      return "The checkout's .git pointer is invalid."
    git_dir = Path(value[7:].strip())
    if not git_dir.is_absolute():
      git_dir = marker.parent / git_dir
    git_dir = git_dir.resolve()
    if not git_dir.is_dir():
      return "The checkout's Git administrative directory is missing."
    # A separate-git-dir clone has neither pointer. Check either so damage to
    # a linked worktree's commondir cannot disguise it as a separate clone.
    if (git_dir / "commondir").exists() or (git_dir / "gitdir").exists():
      if not linked_worktree_owns_checkout(repo, git_dir):
        return "The checkout's Git state is registered to another or missing folder."
  except (OSError, RuntimeError, ValueError):
    return "The checkout's Git ownership could not be read."
  return None
