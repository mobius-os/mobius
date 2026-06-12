#!/usr/bin/env python3
"""Bootstraps the agent-editable skills layer at /data/shared/skills/ on boot.

The system prompt (skill/core.md, baked + owner-curated) is the stable
"constitution"; the detailed how-to skills live here, under /data, so the agent
(and the nightly Dreaming agent) can IMPROVE them and write new ones. Like the
knowledge graph, this is CREATE-IF-ABSENT — reseeding would clobber the agent's
own skill edits.

Propagation policy, precisely (the code below is the contract): on first boot
the whole seed tree is copied and `.seed-version` is stamped. On every later
boot we ONLY add seed skills the instance is missing — an already-present skill
is NEVER overwritten, so the agent's edits survive. There is deliberately no
automatic content migration of existing skills: an updated baked seed (e.g. a
fix to dreaming.md) does NOT reach an instance that already has that file. Such
an update must be propagated explicitly (copy the new seed over the live
/data/shared/skills/<name>.md), because a blind overwrite can't tell an owner
improvement from an agent edit. `.seed-version`/`SEED_VERSION` are kept as a
record of the baked seed generation for that future, merge-aware migration; the
sentinel is written but not yet read.

Seed source: /app/scripts/seed-skills/ (baked), falling back to the in-repo
backend/scripts/seed-skills/ for dev. Run from entrypoint after
init_memory_graph.py.
"""

import os
import pwd
import shutil
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SKILLS = DATA_DIR / "shared" / "skills"
VERSION_FILE = SKILLS / ".seed-version"
SEED_VERSION = "5"  # bump when the baked seed skills change (v2: added dreaming.md; v3: const-CSS styling standard in building-apps.md + new app-component-shapes.md; v4: added embedded-app-agent.md; v5: SyncPill v2 silent-when-healthy)

_SEED_CANDIDATES = [
  Path("/app/scripts/seed-skills"),
  Path(__file__).resolve().parent / "seed-skills",
]


def _seed_dir() -> Path | None:
  return next((p for p in _SEED_CANDIDATES if p.is_dir()), None)


def _chown_mobius(path: Path) -> None:
  """Make the tree mobius-owned + writable so the agent can edit skills."""
  try:
    m = pwd.getpwnam("mobius")
  except KeyError:
    return
  for p in [path, *path.rglob("*")]:
    try:
      os.chown(p, m.pw_uid, m.pw_gid)
      os.chmod(p, 0o775 if p.is_dir() else 0o664)
    except (PermissionError, OSError):
      pass


def init() -> None:
  seed = _seed_dir()
  if seed is None:
    print("init_skills: no seed-skills dir found; skipping")
    return
  SKILLS.parent.mkdir(parents=True, exist_ok=True)
  if not SKILLS.exists():
    shutil.copytree(seed, SKILLS)
    VERSION_FILE.write_text(SEED_VERSION + "\n", encoding="utf-8")
    n = len(list(SKILLS.glob("*.md")))
    print(f"init_skills: seeded {n} skills (v{SEED_VERSION})")
    _chown_mobius(SKILLS)
    return
  # Present already — preserve the agent's edits. Only add NEW seed skills the
  # instance doesn't have yet (never overwrite an existing one).
  added = 0
  for src in seed.glob("*.md"):
    dst = SKILLS / src.name
    if not dst.exists():
      shutil.copy2(src, dst)
      added += 1
  if added:
    print(f"init_skills: added {added} new seed skill(s) (existing kept)")
  _chown_mobius(SKILLS)


if __name__ == "__main__":
  init()
