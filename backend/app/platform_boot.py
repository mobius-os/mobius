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
  to its saved previous state; exits nonzero when there is none.
``guard``
  The fail-closed boot guard: prove the served tree is a clean committed
  state, or refuse to serve it.
``check-packages [checkout]``
  Read-only: print this image's Python package gap for the served checkout
  (or ``checkout``) as JSON, ``null`` when there is none, and exit 1 only
  when ``activate`` would refuse it. Restart admission asks this, so it
  predicts this image's boot rather than the served code's rule.
``admit``
  After the import probe passed. Read-only: refuses the served commit when it
  already failed to become ready ``UNREADY_BOOT_LIMIT`` boots in a row. The
  import probe cannot see a crash or hang while the server starts; without
  this, the container restarts into the same commit until the host gives up,
  and the instance stays down.
``starting``
  Once the boot's choice to serve ``/data/platform`` is final, before any of
  its code runs. Counts this boot against the served commit. A boot that ends
  up serving the built-in copy for another reason never counts.
``ready``
  Run by the entrypoint's health probe once the served platform answers. The
  commit is good, so its count is forgotten.

A nonzero exit means this image cannot establish a state it may serve from
``/data/platform``; the entrypoint then refuses to serve that tree (for
``admit``: treats it like a failed import probe; for ``starting``: only warns).

Every run is also appended to ``BOOT_LOG``. The container's console is outside
the app container, so this durable record is the only place an agent repairing
the platform can read why a boot transaction failed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# The served worker reads this line from the image's baked copy of this file
# (``restart_util.image_boot_checks_packages``) to learn that this image has
# ``check-packages`` and serves a version-only package gap. Keep its spelling;
# raise it when that command's contract changes.
PACKAGE_CHECK_PROTOCOL = 1

BOOT_LOG = Path("/data/logs/platform-boot.jsonl")
_BOOT_LOG_RECORDS = 200
_STDERR_CHARS = 2000

# One served commit and how many boots in a row started it without reaching
# ready. Kept outside /data/platform so it never dirties the served tree.
UNREADY_BOOTS = Path("/data/.platform-unready-boots.json")
# Two, not one: a single boot can die for reasons that are not the commit's
# (out of memory, the host stopping a deploy). The host's own retry budget for
# a crashing boot (railway.toml restartPolicyMaxRetries = 3) must leave one boot
# to serve the built-in copy after the limit is reached. A hang never exits, so
# it counts only when something else restarts the container.
UNREADY_BOOT_LIMIT = 2


def _read_unready_boots(path: Path) -> tuple[str, int]:
  """The recorded commit and its count; anything unreadable counts as none."""
  try:
    record = json.loads(path.read_text(encoding="utf-8"))
    return str(record["sha"]), int(record["unready_boots"])
  except Exception:  # noqa: BLE001 - a damaged record must never block a boot
    return "", 0


def unready_refusal(sha: str, *, path: Path | None = None) -> str | None:
  """Why ``sha`` is set aside, or None while boot may still serve it."""
  recorded, count = _read_unready_boots(path or UNREADY_BOOTS)
  if recorded != sha or count < UNREADY_BOOT_LIMIT:
    return None
  return (
    f"platform commit {sha} did not finish starting in {count} boots in a row; "
    "setting it aside. An approved restart, or a new commit, tries again."
  )


def count_unready_boot(sha: str, *, path: Path | None = None) -> int:
  """Count a boot that starts ``sha``, until ``ready`` forgets it; the new count.

  Bookkeeping failures only warn: this guard exists to keep a box serving, so
  it must never be the thing that stops a boot.
  """
  path = path or UNREADY_BOOTS
  recorded, count = _read_unready_boots(path)
  count = (count if recorded == sha else 0) + 1
  staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
  try:
    staged.write_text(json.dumps({"sha": sha, "unready_boots": count}), encoding="utf-8")
    os.replace(staged, path)
  except Exception as exc:  # noqa: BLE001 - see docstring
    with contextlib.suppress(OSError):
      staged.unlink(missing_ok=True)
    print(f"platform boot: could not count this boot in {path}: {exc!r}", file=sys.stderr)
  return count


def forget_unready_boots(*, path: Path | None = None) -> None:
  """The served commit started, or the owner asked to retry it."""
  with contextlib.suppress(OSError):
    (path or UNREADY_BOOTS).unlink(missing_ok=True)


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
  """Append this run to the bounded boot log; never fails the boot.

  The log is diagnostics on the boot's critical path: a run that settled the
  platform must not exit nonzero because its record could not be written, or
  every later boot would refuse to start on the same file. Earlier lines are
  read leniently, so a damaged byte ages out instead of stopping the log.
  """
  log = log or BOOT_LOG
  record = {
    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "boot_id": os.environ.get("MOBIUS_BOOT_ID"),
    "command": command,
    "ok": ok,
    "detail": detail,
  }
  staged = log.with_name(f".{log.name}.{os.getpid()}.tmp")
  try:
    lines = (
      log.read_text(encoding="utf-8", errors="replace").splitlines()
      if log.exists() else []
    )
    lines = [*lines[-(_BOOT_LOG_RECORDS - 1):], json.dumps(record)]
    staged.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(staged, log)
  except Exception as exc:  # noqa: BLE001 - recording must never fail the boot
    with contextlib.suppress(OSError):
      staged.unlink(missing_ok=True)
    print(f"platform boot: could not record this run in {log}: {exc!r}", file=sys.stderr)


def check_packages(checkout: str | None) -> int:
  from app import platform_update

  repo = Path(checkout) if checkout else platform_update.PLATFORM_REPO
  gap = platform_update.python_package_gap(repo)
  print(json.dumps(gap))
  return 1 if gap and gap["blocking"] else 0


def main(argv: list[str]) -> int:
  if len(argv) in (2, 3) and argv[1] == "check-packages":
    return check_packages(argv[2] if len(argv) == 3 else None)
  commands = {"activate", "revert", "guard", "admit", "starting", "ready"}
  if len(argv) != 2 or argv[1] not in commands:
    print(
      "usage: python3 -m app.platform_boot "
      "activate|revert|guard|admit|starting|ready|check-packages [checkout]",
      file=sys.stderr,
    )
    return 2
  if argv[1] == "ready":
    forget_unready_boots()
    return 0
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
        record_boot_run(command, ok=False, detail="no swapped-in update to revert")
        return 1
      outcome = "reverted"
    elif command == "admit":
      sha = platform_update._rev(repo, "HEAD")
      refusal = unready_refusal(sha) if sha else None
      if refusal:
        print(f"platform boot admit: {refusal}", file=sys.stderr)
        record_boot_run(command, ok=False, detail=refusal)
        return 1
      outcome = f"admitted {sha or 'unknown commit'}"
    elif command == "starting":
      sha = platform_update._rev(repo, "HEAD")
      if sha:
        outcome = f"starting {sha}; unready boots in a row: {count_unready_boot(sha)}"
      else:
        outcome = "starting an unknown commit; not counted"
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
