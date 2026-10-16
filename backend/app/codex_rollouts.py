"""Where Codex keeps one thread's rollout.

Codex writes a live thread to ``sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl``
under its home and moves an archived thread to ``archived_sessions/``. The
home is a literal directory, so it is escaped before the wildcard components
are appended: a home such as ``/srv/[prod]/codex`` still finds its rollouts.
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path

# A plain token: no separator or glob syntax can reach the pattern.
_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def rollout_paths(
  codex_home: str | os.PathLike, thread_id: str, *, archived: bool = False,
) -> list[Path]:
  """Every rollout recorded for ``thread_id``, live sessions first.

  ``archived`` adds matches from ``archived_sessions`` after the live ones.
  An id that is not a plain token matches nothing. Callers decide what
  several matches mean.
  """
  if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
    return []
  home = glob.escape(os.fspath(codex_home))
  name = f"rollout-*-{thread_id}.jsonl"
  patterns = [os.path.join(home, "sessions", "*", "*", "*", name)]
  if archived:
    patterns.append(os.path.join(home, "archived_sessions", name))
  return [Path(match) for pattern in patterns for match in glob.glob(pattern)]
