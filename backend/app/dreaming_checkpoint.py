"""Dreaming's last-run marker — the one bookmark for "what to review tonight".

DELIBERATELY NOT an exactly-once engine (an earlier fence/cursor/ledger version was scrapped
after an adversarial philosophy review). Per the Dreaming design
(docs/superpowers/specs/2026-06-02-knowledge-graph-and-dreaming-design.md) + Möbius's
"code empowers the agent; it does not police it": Dreaming is a once-a-night, single-owner,
single-process LLM job whose safety is good instructions + git-reversibility + infra guards
(flock / timeout / outcome-log). So we record the HEAD it last SUCCESSFULLY reviewed, hand the
agent the diff since then, and TRUST the agent to skip its own commits and already-seen chats.
Re-reading is cheap and self-correcting; only MISSING data costs — so a bad/absent marker
bootstraps to a wide window rather than failing closed.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# git pathspecs to drop from any diff the agent reads: binary, databases, logs, browser
# caches, build output. Keeps the diff legible and stops /data git bloat (a 65MB chat.log
# + browser caches had leaked in). Use as trailing `-- <pathspecs>` on `git diff`.
EXCLUDE_PATHSPECS = (
    ":(exclude,glob)**/*.db", ":(exclude,glob)**/*.sqlite", ":(exclude,glob)**/*.sqlite3",
    ":(exclude,glob)**/*.png", ":(exclude,glob)**/*.jpg", ":(exclude,glob)**/*.jpeg",
    ":(exclude,glob)**/*.gif", ":(exclude,glob)**/*.ico", ":(exclude,glob)**/*.webm",
    ":(exclude,glob)**/*.mp4", ":(exclude,glob)**/*.pdf", ":(exclude,glob)**/*.zip",
    ":(exclude,glob)**/*.woff", ":(exclude,glob)**/*.woff2",
    ":(exclude,glob)logs/**", ":(exclude,glob)**/logs/**",
    ":(exclude,glob)agent-browser-profiles/**", ":(exclude,glob)**/Cache/**",
    ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/dist/**",
)


def read_marker(path):
    """The last successfully-reviewed marker, or None to bootstrap. Tolerant by design: a
    missing OR unreadable/corrupt marker returns None (bootstrap a wide window) — we never
    fail closed, because missing data is the only real cost; a re-read is cheap."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_marker(path, marker):
    """Atomically persist the marker (temp + fsync + rename) at the END of a successful run.
    A failed write leaves the PRIOR marker intact rather than a torn one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(marker, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".last-run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
