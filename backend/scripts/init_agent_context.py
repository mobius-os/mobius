"""Initializes the agent experience file and writes upstream diffs."""

import os
import pwd
import shutil
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
EXPERIENCE_PATH = DATA_DIR / "shared" / "agent-experience.md"
OLD_CONTEXT_PATH = DATA_DIR / "shared" / "agent-context.md"
SEED_PATH = Path("/app/scripts/seed-agent-experience.md")
DIFF_PATH = DATA_DIR / "shared" / "upstream-diff.txt"


def _ensure_mobius_writable(path: Path) -> None:
  """Makes `path` owned+writable by the mobius user.

  The entrypoint runs as root, so files/dirs it creates come out
  root-owned and often without the execute bit on directories. The
  CLI subprocess runs as `mobius` and silently fails any Edit tool
  call against a root-owned file — or any `cd` / `stat` into a
  directory without the execute bit. The agent never updates its own
  experience log as a result.

  Chown everything to mobius:mobius and apply correct mode: 775 for
  directories (rwx + traverse), 664 for files (rw, no exec).
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


def init():
  EXPERIENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
  _ensure_mobius_writable(EXPERIENCE_PATH.parent)

  # Always copy seed → live on every boot. Rebuild = current state.
  # Instance memory the agent appended is intentionally not preserved —
  # mobius is in active development and we want prod to track the seed.
  # When this changes, add a sentinel-based preservation scheme here.
  if SEED_PATH.exists():
    shutil.copy2(SEED_PATH, EXPERIENCE_PATH)
    print(f"Seeded {EXPERIENCE_PATH} from {SEED_PATH}")
  elif not EXPERIENCE_PATH.exists():
    EXPERIENCE_PATH.write_text(
      "# Agent experience\n\n(No seed file found — start fresh.)\n",
      encoding="utf-8",
    )
    print(f"Created empty {EXPERIENCE_PATH}")

  _ensure_mobius_writable(EXPERIENCE_PATH)


if __name__ == "__main__":
  init()

  # Write upstream diff to a standalone file (overwritten each deploy).
  upstream_diff = os.environ.get("UPSTREAM_DIFF", "")
  if os.environ.get("UPSTREAM_CHANGED") == "true" and upstream_diff:
    DIFF_PATH.write_text(upstream_diff, encoding="utf-8")
    print(f"Wrote upstream diff to {DIFF_PATH}")
  else:
    # No changes — remove stale diff file if present.
    DIFF_PATH.unlink(missing_ok=True)
