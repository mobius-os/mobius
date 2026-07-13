"""Writes the per-deploy upstream-diff file the agent can read after a deploy.

Per-chat continuity lives under `/data/shared/memory/chats/` (bootstrapped by
`init_chat_summaries.py`); optional installed apps may add other context. This
script only publishes `/data/shared/upstream-diff.txt`, a standalone summary of what
changed in the current deploy, refreshed on every boot.
"""

import os
import pwd
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DIFF_PATH = DATA_DIR / "shared" / "upstream-diff.txt"


def _ensure_mobius_writable(path: Path) -> None:
  """Makes `path` owned+writable by the mobius user.

  The entrypoint runs as root, so a file/dir it creates comes out root-owned
  and often without the execute bit on directories. The agent process (Claude
  SDK / Codex runner, plus the recovery CLI subprocess) runs as `mobius` and
  silently fails any Read/Edit against a root-owned file — or any `cd`/`stat`
  into a directory without the execute bit. Chown to mobius:mobius with the
  correct mode: 775 for directories (rwx + traverse), 664 for files.
  """
  if not path.exists():
    return
  try:
    mobius = pwd.getpwnam("mobius")
  except KeyError:
    return  # not running in the container image; nothing to do
  try:
    os.chown(path, mobius.pw_uid, mobius.pw_gid)
    os.chmod(path, 0o775 if path.is_dir() else 0o664)
  except PermissionError:
    pass  # best effort — skip silently on dev hosts


if __name__ == "__main__":
  DIFF_PATH.parent.mkdir(parents=True, exist_ok=True)
  _ensure_mobius_writable(DIFF_PATH.parent)

  # Write the upstream diff to a standalone file (overwritten each deploy).
  upstream_diff = os.environ.get("UPSTREAM_DIFF", "")
  if os.environ.get("UPSTREAM_CHANGED") == "true" and upstream_diff:
    DIFF_PATH.write_text(upstream_diff, encoding="utf-8")
    _ensure_mobius_writable(DIFF_PATH)
    print(f"Wrote upstream diff to {DIFF_PATH}")
  else:
    # No changes — remove stale diff file if present.
    DIFF_PATH.unlink(missing_ok=True)
