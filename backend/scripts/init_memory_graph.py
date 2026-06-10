#!/usr/bin/env python3
"""Bootstraps the agent's knowledge graph at /data/shared/memory/ on boot.

CREATE-IF-ABSENT, never blind-overwrite. Unlike the flat experience file
(`init_agent_context.py`, reseeded every boot so prod tracks the seed), the
graph is the agent's *persistent, growing* memory — reseeding it would destroy
every learned note (Codex review R1). So:

  - first boot (no graph dir): stage-copy the seed graph, ensure a persistent
    inbox.md, lint, then atomically publish (rename staging -> live) and write
    `.seed-version` + `.ready` LAST. A failed lint leaves no `.ready`, so the
    injector keeps the legacy flat-file fallback (review R2).
  - subsequent boots (graph present): leave the agent's notes untouched; only
    ensure inbox.md exists and re-publish `.ready` if a prior boot crashed
    mid-publish. Seed-version migrations for existing instances are a dreaming
    task, not a boot-time overwrite.

This is pure file I/O (no agent process). Run from entrypoint after
init_agent_context.py.
"""

import os
import pwd
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory_graph import build_graph, write_graph  # noqa: E402

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MEMORY = DATA_DIR / "shared" / "memory"
INBOX = MEMORY / "inbox.md"
READY = MEMORY / ".ready"
VERSION_FILE = MEMORY / ".seed-version"

_SEED_CANDIDATES = [
  Path("/app/scripts/seed-memory"),
  Path(__file__).resolve().parent / "seed-memory",
]
SEED_VERSION = "2"  # bump when the seed graph's authored content changes

INBOX_HEADER = (
  "# Inbox\n\n"
  "Raw, unconsolidated observations land here during the day (your skill's\n"
  "append recipe writes to this file). The nightly dreaming pass folds these\n"
  "into atomic notes under `notes/` and then truncates this file. Anything\n"
  "here is recalled next session, so nothing is lost before consolidation.\n\n"
)


def _seed_dir() -> Path | None:
  return next((p for p in _SEED_CANDIDATES if p.is_dir()), None)


def _chown_mobius(path: Path) -> None:
  """Match init_agent_context: make the tree mobius-owned + traversable so the
  agent (running as mobius) can edit notes; best-effort on dev hosts."""
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


def _publish_from_staging() -> bool:
  """First-boot path: copy seed -> staging, ensure inbox, lint the staging
  tree directly, then atomically rename into place and write the sentinels
  LAST. Returns True on success (graph mode active), False to stay legacy."""
  seed = _seed_dir()
  if seed is None:
    print("init_memory_graph: no seed-memory dir found; skipping (legacy mode)")
    return False
  staging = MEMORY.parent / "memory.staging"
  if staging.exists():
    shutil.rmtree(staging, ignore_errors=True)
  shutil.copytree(seed, staging)
  if not (staging / "inbox.md").is_file():
    (staging / "inbox.md").write_text(INBOX_HEADER, encoding="utf-8")

  res = build_graph(root=staging)  # lint the staging tree in place
  if res.errors:
    print("init_memory_graph: seed graph failed lint; staying in legacy mode:")
    for p in res.errors:
      print(f"  [error] {p['kind']}: {p['detail']}")
    shutil.rmtree(staging, ignore_errors=True)
    return False

  write_graph(root=staging)  # graph.json built before publish
  staging.rename(MEMORY)  # atomic; MEMORY must not exist (checked by caller)
  VERSION_FILE.write_text(SEED_VERSION + "\n", encoding="utf-8")
  READY.write_text("", encoding="utf-8")  # the gate, written LAST
  print(f"init_memory_graph: published seed graph v{SEED_VERSION} "
        f"({len(res.nodes)} nodes)")
  return True


def init() -> None:
  MEMORY.parent.mkdir(parents=True, exist_ok=True)
  if not MEMORY.exists():
    if _publish_from_staging():
      _chown_mobius(MEMORY)
    return

  # Graph already present — preserve agent edits. Only self-heal.
  if not INBOX.is_file():
    INBOX.write_text(INBOX_HEADER, encoding="utf-8")
  if not READY.is_file():
    # A prior boot crashed mid-publish, or the graph was hand-created. Lint
    # and re-arm the sentinel only if it's actually valid.
    res = build_graph(DATA_DIR)
    if not res.errors:
      write_graph(DATA_DIR)
      READY.write_text("", encoding="utf-8")
      print("init_memory_graph: re-armed .ready on existing valid graph")
    else:
      print("init_memory_graph: existing graph has errors; staying legacy")
  _chown_mobius(MEMORY)


if __name__ == "__main__":
  init()
