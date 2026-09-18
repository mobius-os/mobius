#!/usr/bin/env python3
"""Reset the exact capture profile through the shared browser lifecycle owner."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.browser_processes import SessionResetError, reset_profile


def main(argv: list[str]) -> int:
  if len(argv) != 2:
    print(f'Usage: {Path(argv[0]).name} <agent-browser-profile>', file=sys.stderr)
    return 2
  try:
    reset_profile(argv[1])
  except (SessionResetError, OSError) as exc:
    print(f'agent-browser session reset: {exc}', file=sys.stderr)
    return 1
  return 0


if __name__ == '__main__':
  raise SystemExit(main(sys.argv))
