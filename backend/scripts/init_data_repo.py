#!/usr/bin/env python3
"""Initialize and reconcile the owner-visible ``/data`` safety repository.

The root ignore file is the ownership boundary: paths listed there belong to
runtime or subsystem lifecycles, not to the outer safety repository.  Existing
index entries that now cross that boundary are removed from the index only;
working-tree files and nested Git metadata are never deleted here.

Boot invokes ``write-ignore`` as root so an older root-owned file can be
replaced atomically, then invokes ``reconcile`` as the ``mobius`` user after
the outer repository has been handed back to that user.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


DATA_GITIGNORE = """\
cli-auth/
app-secrets/
push/*.pem
push/*.json
push/*.txt
service-token.txt
.secret-key
# Legacy recovery credentials and chat fragments may remain after an update.
# They are inert, but must never become safety-repository content.
.recovery-secret
.recovery-owner.json
recovery_chat.jsonl
.recover-pending
.pm-commit
compiled/
db/
db.sqlite3
mobius.db
chats/
backups/
# backup-data.py's second-volume target carries encrypted secrets and the DB.
backups-external/
*.bak-*
apps/*/data/
apps/*/.git/
agent-browser-profiles/
generated/
logs/
cron-logs/
# Runtime workspaces own both their contents and their cleanup.
/run/
/agent-scratch/
# Contribution records point at these durable repositories. Their own Git
# history is authoritative; recording them here as gitlinks is both redundant
# and unsafe when a linked worktree is later retired.
/contrib/
/contributions/
# Memory owns this optional repository directly.
shared/memory/repository/
# The platform source owns its own repository and update lifecycle.
platform/
# Bootstrap scratch and preserved upgrade quarantines are not owner content for
# the safety repository. Ignoring a legacy quarantine preserves it; boot never
# deletes or moves it here.
platform.seeding.*
platform.reseeding.*
platform.reseed-prev.*
platform.pre-clone.*
platform.crashloop-prev.*
# Inert markers can remain on volumes upgraded from the retired recovery flow.
.boot-attempt
.last-successful-boot
.platform-restore-active
.platform-upgrade-available
.platform-pre-clone-active
# Transient platform-update markers are runtime signals, never owner content.
.platform-conflict
.platform-offline
.platform-restart-needed
.platform-apply-in-progress
.platform-restart-requested
.platform-rolled-back
.platform-update-progress.json
.platform-reconcile-pre
.platform-reconcile.lock
"""

_REDIRECTING_GIT_ENV = (
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_COMMON_DIR",
  "GIT_NAMESPACE",
)


def write_ignore(data_dir: Path) -> None:
  """Atomically publish the root repository's ownership boundary."""
  data_dir = data_dir.resolve()
  data_dir.mkdir(parents=True, exist_ok=True)
  target = data_dir / ".gitignore"
  descriptor, raw_temp = tempfile.mkstemp(
    dir=data_dir,
    prefix=".gitignore.",
    suffix=".tmp",
  )
  temporary = Path(raw_temp)
  try:
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
      handle.write(DATA_GITIGNORE)
      handle.flush()
      os.fsync(handle.fileno())
    temporary.chmod(0o644)
    os.replace(temporary, target)
  finally:
    try:
      temporary.unlink()
    except FileNotFoundError:
      pass


def _git(
  data_dir: Path,
  *args: str,
  input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in _REDIRECTING_GIT_ENV
  }
  process = subprocess.run(
    ["git", "-C", str(data_dir), *args],
    input=input_bytes,
    capture_output=True,
    env=env,
  )
  if process.returncode != 0:
    detail = (process.stderr or process.stdout).decode(
      "utf-8", errors="replace",
    ).strip()
    raise RuntimeError(detail or f"git {' '.join(args)} exited {process.returncode}")
  return process


def reconcile(data_dir: Path) -> tuple[str, int]:
  """Initialize the safety repo or untrack paths owned by other lifecycles."""
  data_dir = data_dir.resolve()
  if not (data_dir / ".gitignore").is_file():
    raise RuntimeError(f"{data_dir / '.gitignore'} must exist before reconcile")

  if not os.path.lexists(data_dir / ".git"):
    _git(data_dir, "init", "-q")
    _git(data_dir, "config", "user.name", "Mobius Agent")
    _git(data_dir, "config", "user.email", "agent@mobius")
    _git(data_dir, "add", "-A")
    _git(data_dir, "commit", "-q", "-m", "init", "--allow-empty")
    return "initialized", 0

  # Use only the generated root policy. ``--exclude-standard`` would also read
  # nested app .gitignore files and could untrack app-owned source by accident.
  ignored = _git(
    data_dir,
    "ls-files",
    "-ci",
    "--exclude-from=.gitignore",
    "-z",
  ).stdout
  if ignored:
    _git(
      data_dir,
      "update-index",
      "--force-remove",
      "-z",
      "--stdin",
      input_bytes=ignored,
    )
  return "reconciled", ignored.count(b"\0")


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("action", choices=("write-ignore", "reconcile"))
  parser.add_argument("data_dir", nargs="?", default="/data", type=Path)
  args = parser.parse_args()

  if args.action == "write-ignore":
    write_ignore(args.data_dir)
    print(f"Published {args.data_dir / '.gitignore'}")
    return 0

  state, count = reconcile(args.data_dir)
  if state == "initialized":
    print(f"Initialized safety repository at {args.data_dir}")
  else:
    print(f"Reconciled safety repository ({count} ignored index entries removed)")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
