#!/usr/bin/env python3
"""Check a review folder before reusing it; never run Git or repair pointers."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.git_checkout import checkout_ownership_error


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("repo", type=Path)
  args = parser.parse_args()
  problem = (
    checkout_ownership_error(args.repo)
    if (args.repo / ".git").exists() else "The review checkout is missing."
  )
  if problem:
    print(f"Review checkout refused: {problem} Preserve the folder; do not repair "
          "its pointers or run Git in it. Prepare a fresh checkout.", file=sys.stderr)
    return 1
  print("Review checkout ownership verified (read-only preflight).")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
