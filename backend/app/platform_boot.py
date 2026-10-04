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
  to its saved previous state; exits nonzero when there is none, or when the
  checkout is not exactly that update (compare-and-swap: nothing is touched,
  and the entrypoint serves the baked platform).
``guard``
  The fail-closed boot guard: prove the served tree is a clean committed
  state, or refuse to serve it.

A nonzero exit means this image cannot establish a state it may serve from
``/data/platform``; the entrypoint then refuses to serve that tree.
"""

from __future__ import annotations

import logging
import os
import sys


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
        return 1
      outcome = "reverted"
    else:
      outcome = platform_update.boot_guard_sync()
  except Exception as exc:  # noqa: BLE001 - any failure must refuse the tree
    print(f"platform boot {command} failed: {exc!r}", file=sys.stderr)
    return 1
  print(f"platform boot {command}: {outcome}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main(sys.argv))
