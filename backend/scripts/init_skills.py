#!/usr/bin/env python3
"""Bootstraps the agent-editable skills layer at /data/shared/skills/ on boot.

The system prompt (skill/core.md, baked + owner-curated) is the stable
"constitution"; the detailed how-to skills live here, under /data, so the agent
(and the nightly Reflection agent) can IMPROVE them and write new ones. Like the
knowledge graph, this is CREATE-IF-ABSENT — reseeding would clobber the agent's
own skill edits.

Propagation policy, precisely (the code below is the contract): on first boot
the whole seed tree is copied and `.seed-version` is stamped. On every later
boot we add missing seed skills and apply only explicit, hash-gated migrations:
an existing file is replaced when it is byte-for-byte a known baked predecessor,
while every owner/agent-edited copy is preserved. A normal baked-seed edit does
not propagate until its predecessor hash is deliberately registered below; this
keeps urgent fix-forward migrations possible without blind overwrites. App-owned
skills are not part of this seed tree; they arrive through manifests and their
generic ownership sidecar.
`.seed-version`/`SEED_VERSION` are kept as a
record of the baked seed generation for that future, merge-aware migration; the
sentinel is written but not yet read.

Seed source: /app/scripts/seed-skills/ (baked), falling back to the in-repo
backend/scripts/seed-skills/ for dev. Run from entrypoint after
init_chat_summaries.py.
"""

import os
import pwd
import shutil
import hashlib
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SKILLS = DATA_DIR / "shared" / "skills"
VERSION_FILE = SKILLS / ".seed-version"
SEED_VERSION = "17"  # v17: inert-safe visual mode + ref-based iframe testing
# Update only byte-for-byte baked copies; an owner/agent-edited file is never
# touched. A set preserves every known unmodified predecessor when one skill
# needs more than one fix-forward migration over its lifetime.
_UNMODIFIED_MIGRATIONS = {
  "reflection.md": {
    "c0f57c227f61cd8539a56b70eadfbbe2212125c23b7137472dd173a578baacd8",
    # Resource-stewardship predecessor: propagate the adaptive analytics
    # and self-throttling contract only to untouched copies.
    "865dd241a99668b026cd9be90c472cfde562210df51f729b2c25929f6b3bd60a",
    # v15 baked copy: route app work through the base + matching extensions.
    "cba6c0c7dd97384bbe3bfa19e78707bfa272085843bab5102279a937467e5d17",
  },
  "cron.md": {
    "289336d78ad4268110360f12faac5512d5a53b66aa31c2a6ddd1a44f538f2559",
    "ed100cb496b887a7951adc967e92cda1449c4f8594f7859fbd32762221d24914",
  },
  "recovery.md": {
    "ef62abb0d03d740f99add1b6f3938f780b34439cb0025616cb9dc5f74f779633",
    "6e6e82e02287e8bb38195fb021ea25cee2dc4e27da1a6ce1e2a0143fb1d82d87",
  },
  "images.md": {
    "248ea31e13d2d2d84a5acfca13526aa8ebfa3d90e9ee4bf55cfb72d47937f7d1",
  },
  "building-apps.md": {
    "4126b40d209c422184e0135f611bb9f4197ea280fa27e63cd71c806f8b5ebd79",
    "91b655952d55b37fda0be82e3914c3b09e67ca7c5f5a575d315fb2ca75ef08f1",
    "563dcd7bfa1ff7cbad074d98462eb9755a010a15bf340c7f594fc7f6825a6a86",
  },
  "building-apps-quickstart.md": {
    "7d8af2664b37a69b88e48c2a28140c15556202c3c7ce30d77816c203d1959fcb",
    # v16 baked copy: replace the unreliable CSS iframe selector.
    "4c2b080bcc91626f761c5823ea00d324667b9710f6757931823e22e9c8b5c2b1",
  },
  "app-component-shapes.md": {
    "0320609ff924a0954c20d5e5db91ed3681d421d76f6804b24552eb6e8fa5eb31",
    # v16 baked copy: keep routine app builds from loading the catalog.
    "91243377242700acb5093165af58c372bed0005f358d3a4b26774aeb2ef8a365",
  },
  "visual-testing.md": {
    "9525b36b945c2a0b4cb02806081bb674f38e865b6e1c3961226112e1dbbc16ec",
    # v16 baked copy: use iframe refs and preserve React's inert cleanup.
    "a0648921b9c9ea2423e8abd52aa57e71e7bebfa1736073fcf3bfcaec3749ad19",
  },
}

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


def _write_index() -> None:
  """Regenerates skills-index.md via app.skills; best-effort.

  This script runs standalone from the entrypoint (before uvicorn), so the
  app package import can fail on a badly broken tree — the index is a
  convenience surface, never worth failing boot over. The server-side install
  paths regenerate it too, so a skipped boot write self-heals on the next
  install.
  """
  try:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.skills import reconcile_installed, write_index

    # Startup half of the installer's crash-recovery contract: repair any
    # install a crash interrupted (finalize published, discard staged) before
    # the index snapshots the tree.
    repaired = reconcile_installed(SKILLS)
    if repaired:
      print(f"init_skills: reconciled interrupted install(s): {repaired}")
    write_index(SKILLS)
    print("init_skills: skills-index.md regenerated")
  except Exception as exc:  # noqa: BLE001 - boot must not fail on the index
    print(f"init_skills: index generation skipped ({exc})")


def init() -> None:
  seed = _seed_dir()
  if seed is None:
    print("init_skills: no seed-skills dir found; skipping")
    return
  SKILLS.parent.mkdir(parents=True, exist_ok=True)
  if not SKILLS.exists():
    SKILLS.mkdir(parents=True)
    for src in seed.glob("*.md"):
      shutil.copy2(src, SKILLS / src.name)
    VERSION_FILE.write_text(SEED_VERSION + "\n", encoding="utf-8")
    n = len(list(SKILLS.glob("*.md")))
    print(f"init_skills: seeded {n} skills (v{SEED_VERSION})")
    _chown_mobius(SKILLS)
    _write_index()
    return
  # Present already — preserve the agent's edits. Only add NEW seed skills the
  # instance doesn't have yet (never overwrite an existing one).
  # Resolve any crash-interrupted /api/skills install first, so a pending
  # directory skill is either published (and seen as a collision below) or
  # gone — the same reconciliation the runtime skills mutations run.
  try:
    from app.skills import reconcile_installed

    reconcile_installed(SKILLS)
  except Exception as exc:  # pragma: no cover - best-effort at boot
    print(f"init_skills: reconcile skipped ({exc})")
  added = 0
  migrated = 0
  skipped = 0
  for src in seed.glob("*.md"):
    dst = SKILLS / src.name
    old_digests = _UNMODIFIED_MIGRATIONS.get(src.name)
    if dst.is_file() and old_digests:
      try:
        digest = hashlib.sha256(dst.read_bytes()).hexdigest()
      except OSError:
        digest = ""
      if digest in old_digests:
        shutil.copy2(src, dst)
        migrated += 1
        continue
    # Both on-disk shapes share one logical id: never add a flat `foo.md` seed
    # when an install-provenance directory skill `foo/` already holds `foo`
    # (the runtime install path enforces the same both-shape rule).
    if (SKILLS / src.stem).is_dir():
      skipped += 1
      continue
    if not dst.exists():
      shutil.copy2(src, dst)
      added += 1
  if skipped:
    print(f"init_skills: skipped {skipped} seed skill(s) colliding with an "
          "installed directory skill of the same id")
  if added:
    print(f"init_skills: added {added} new seed skill(s) (existing kept)")
  if migrated:
    print(f"init_skills: migrated {migrated} unmodified base skill(s)")
  _chown_mobius(SKILLS)
  _write_index()


if __name__ == "__main__":
  init()
