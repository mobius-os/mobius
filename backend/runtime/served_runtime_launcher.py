#!/usr/bin/env python3
"""Validate and start one protected-runtime module from the served checkout.

Some privileged code runs as root before the app drops privileges, so its
*launcher* has to be frozen in the image. The module it starts does not: the
identity broker is ordinary served source under ``backend/runtime``, so editing
it is a normal platform change and an ordinary restart reloads it.

The launcher never mixes served platform code with a frozen module. Entrypoint
first uses ``--check`` while choosing the whole platform source: an unusable
served module makes that boot select the baked platform. Once the served tree
is selected, the ordinary invocation validates the same path again and execs
it. This keeps one source owner per boot instead of hiding an invalid local edit
behind an older privileged implementation.
"""

from __future__ import annotations

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


def main(argv: list[str]) -> int:
  check_only = len(argv) == 3 and argv[1] == "--check"
  name = argv[2] if check_only else (argv[1] if len(argv) == 2 else "")
  if name not in SERVED_MODULES or len(argv) != (3 if check_only else 2):
    print(
      f"usage: {Path(argv[0]).name} [--check] "
      f"{{{'|'.join(SERVED_MODULES)}}}",
      file=sys.stderr,
    )
    return 2
  target = PLATFORM_DIR / RUNTIME_SUBDIR / f"{name}.py"
  reason = _reject_reason(target)
  if reason is not None:
    print(f"FATAL: served {name} is unusable: {reason}", file=sys.stderr)
    return 1
  if check_only:
    return 0
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
