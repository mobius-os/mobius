#!/usr/bin/env python3
"""Start one allowlisted protected-runtime module from the served checkout.

Some privileged code runs as root before the app drops privileges, so its
*launcher* has to be frozen in the image. The module it starts does not: the
identity broker is ordinary served source under ``backend/runtime``, so editing
it is a normal platform change and an ordinary restart reloads it.

The image keeps a copy beside this file as a floor. The served copy is used
only when it is a real, non-symlinked, non-group/world-writable file inside the
served runtime directory that compiles; otherwise the frozen copy starts and
the reason is recorded. A wrong local edit is therefore visible and cheap to
undo instead of making boot impossible.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

# The only modules this loader will start from the served checkout. Every other
# ``backend/runtime`` file stays image-owned. Keep in sync with the
# ``served_runtime_module`` rule in ``app/platform_activation.py``, which answers
# the same question for the updater.
SERVED_MODULES = ("identity_broker",)

PLATFORM_DIR = Path(os.environ.get("MOBIUS_PLATFORM_DIR", "/data/platform"))
RUNTIME_SUBDIR = Path("backend") / "runtime"
FROZEN_DIR = Path(__file__).resolve().parent
RECEIPT_PATH = (
  Path(os.environ.get("DATA_DIR", "/data")) / "run" / "protected-runtime.json"
)


def _reject_reason(path: Path) -> str | None:
  """Why the served module must not be started, or None when it may be."""
  runtime_dir = path.parent
  if runtime_dir.is_symlink() or path.is_symlink():
    return "served path contains a symlink"
  try:
    if runtime_dir.resolve(strict=True) != runtime_dir:
      return "served runtime directory does not resolve to itself"
  except OSError:
    return "served runtime directory is unavailable"
  try:
    info = path.stat()
  except OSError:
    return "served module is missing"
  if not stat.S_ISREG(info.st_mode):
    return "served module is not a regular file"
  if info.st_mode & 0o022:
    return "served module is writable by group or other"
  try:
    source = path.read_bytes()
  except OSError:
    return "served module is unreadable"
  try:
    compile(source, str(path), "exec")
  except SyntaxError:
    return "served module does not compile"
  return None


def _choose(name: str) -> tuple[Path, str, str | None]:
  """Return the module to start, which copy it is, and why the other lost."""
  frozen = FROZEN_DIR / f"{name}.py"
  served = PLATFORM_DIR / RUNTIME_SUBDIR / f"{name}.py"
  reason = _reject_reason(served)
  if reason is None:
    return served, "served", None
  if frozen.is_file():
    return frozen, "frozen", reason
  raise SystemExit(
    f"FATAL: {name} is unusable from the served checkout ({reason}) and the "
    "image copy is missing"
  )


def _record(name: str, target: Path, source: str, reason: str | None) -> None:
  """Leave the boot decision where a later reader can find it."""
  payload = {
    "version": 1,
    "module": name,
    "started": source,
    "path": str(target),
    "reason": reason,
    "pid": os.getpid(),
  }
  try:
    RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT_PATH.write_text(
      json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8",
    )
    os.chmod(RECEIPT_PATH, 0o644)
  except OSError:
    # The record is diagnostic; it must never stop the module from starting.
    pass


def main(argv: list[str]) -> int:
  if len(argv) != 2 or argv[1] not in SERVED_MODULES:
    print(
      f"usage: {Path(argv[0]).name} {{{'|'.join(SERVED_MODULES)}}}",
      file=sys.stderr,
    )
    return 2
  name = argv[1]
  target, source, reason = _choose(name)
  if reason is not None:
    print(f"WARNING: starting the image copy of {name}: {reason}", file=sys.stderr)
  _record(name, target, source, reason)
  try:
    # ``-P`` keeps the module's own directory off sys.path, exactly as the
    # entrypoint started it directly before this loader existed.
    os.execv(sys.executable, [sys.executable, "-P", str(target)])
  except OSError as exc:
    print(f"FATAL: could not start {target}: {exc}", file=sys.stderr)
    return 1
  return 1


if __name__ == "__main__":
  raise SystemExit(main(sys.argv))
