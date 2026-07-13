#!/usr/bin/env python3
"""Create the always-on per-chat summary store, and nothing else.

Knowledge-graph initialization belongs to an installed system app. The base
platform only guarantees the directory used by each chat's title/Digest/Summary
note exists and is writable by the agent.
"""

import os
import pwd
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CHATS = DATA_DIR / "shared" / "memory" / "chats"


def init() -> None:
  CHATS.mkdir(parents=True, exist_ok=True)
  try:
    mobius = pwd.getpwnam("mobius")
  except KeyError:
    return
  for path in (CHATS.parent.parent, CHATS.parent, CHATS):
    try:
      os.chown(path, mobius.pw_uid, mobius.pw_gid)
      os.chmod(path, 0o775)
    except (OSError, PermissionError):
      pass


if __name__ == "__main__":
  init()
