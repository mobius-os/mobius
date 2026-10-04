"""The image's boot transaction for the served platform checkout.

The entrypoint runs this module from the image's own baked checkout
(``/app/platform-baked/backend``), never from ``/data/platform``. The image owns
the Python packages and agent CLIs, so the image, not the candidate source,
decides whether served source may run on it:

``activate``
  Before the boot guard and import probe. Swaps in an update prepared for
  exactly this image, returns an update swapped in for another image to its
  saved previous state, finishes an interrupted swap, merges late edits back,
  and completes activation bookkeeping and trusted hooks, so the probe and the
  server see the same final tree. On success it publishes this image's
  ``BOOT_PROTOCOL`` for the running server.
``revert``
  After the served tree failed its import probe. Returns a swapped-in update
  to its saved previous state; exits ``NOTHING_TO_REVERT`` when there is none.
``guard``
  The fail-closed boot guard: prove the served tree is a clean committed
  state, or refuse to serve it.

Exit 1 means the transaction failed: this image cannot establish a state it
may serve from ``/data/platform``. The entrypoint then serves the baked
platform instead, leaves ``/data/platform`` and its update records exactly as
they are for the next boot, and marks the boot unsettled
(``platform_update.BOOT_UNSETTLED_MARKER``) so the fallback server pauses
update work and tells the owner.

Every run is also appended to ``BOOT_LOG``. The container's console is outside
the app container, so this durable record is the only place an agent repairing
the platform can read why a boot transaction failed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BOOT_LOG = Path("/data/logs/platform-boot.jsonl")
_BOOT_LOG_RECORDS = 200
_STDERR_CHARS = 2000
# ``revert`` found no swapped-in update: an answer, not a failed transaction.
NOTHING_TO_REVERT = 3


def failure_detail(exc: BaseException) -> str:
  """The exception, plus the stderr of every failed command in its chain.

  ``CalledProcessError``'s repr names only the exit code and argv; Git's
  explanation ("Entry … not uptodate. Cannot merge.") is in its stderr.
  """
  parts = [repr(exc)]
  seen: set[int] = set()
  current: BaseException | None = exc
  while current is not None and id(current) not in seen:
    seen.add(id(current))
    if isinstance(current, subprocess.CalledProcessError):
      stderr = current.stderr
      if isinstance(stderr, bytes):
        stderr = os.fsdecode(stderr)
      stderr = (stderr or "").strip()
      if stderr:
        parts.append(f"stderr: {stderr[-_STDERR_CHARS:]}")
    current = current.__cause__ or current.__context__
  return "; ".join(parts)


def record_boot_run(command: str, *, ok: bool, detail: str, log: Path | None = None) -> None:
  """Append this run to the bounded boot log; never fails the boot."""
  log = log or BOOT_LOG
  record = {
    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "boot_id": os.environ.get("MOBIUS_BOOT_ID"),
    "command": command,
    "ok": ok,
    "detail": detail,
  }
  try:
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    lines = [*lines[-(_BOOT_LOG_RECORDS - 1):], json.dumps(record)]
    staged = log.with_name(f".{log.name}.{os.getpid()}.tmp")
    staged.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(staged, log)
  except OSError as exc:
    print(f"platform boot: could not record this run in {log}: {exc!r}", file=sys.stderr)


def main(argv: list[str]) -> int:
  if len(argv) != 2 or argv[1] not in {"activate", "revert", "guard"}:
    print("usage: python3 -m app.platform_boot activate|revert|guard", file=sys.stderr)
    return 2
  # Boot writes the served tree through ``su``, whose login umask can leave
  # files group-writable; the served-runtime check then rejects them.
  os.umask(0o022)
  logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
  from app import platform_update

  command = argv[1]
  repo = platform_update.PLATFORM_REPO
  try:
    if command == "activate":
      outcome = platform_update.settle_prepared_update_for_this_image(repo)
      # This boot's image settles image-requiring updates itself; the server
      # reads the protocol to leave such an update for its target image.
      platform_update.BOOT_TRANSACTION_MARKER.write_text(
        f"{platform_update.BOOT_PROTOCOL}\n", encoding="utf-8",
      )
    elif command == "revert":
      if not platform_update.revert_failed_update(repo):
        print("platform boot revert: no swapped-in update to revert", file=sys.stderr)
        record_boot_run(command, ok=True, detail="no swapped-in update to revert")
        return NOTHING_TO_REVERT
      outcome = "reverted"
    else:
      outcome = platform_update.boot_guard_sync()
  except Exception as exc:  # noqa: BLE001 - any failure must refuse the tree
    detail = failure_detail(exc)
    print(f"platform boot {command} failed: {detail}", file=sys.stderr)
    record_boot_run(command, ok=False, detail=detail)
    return 1
  print(f"platform boot {command}: {outcome}")
  record_boot_run(command, ok=True, detail=str(outcome))
  return 0


if __name__ == "__main__":
  raise SystemExit(main(sys.argv))
