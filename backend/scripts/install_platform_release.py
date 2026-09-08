"""Install an operator-selected image's source through the ordinary Apply owner.

The host passes a local Git bundle from the preflighted image, never a moving
remote ref. Run inside the live container as mobius with cwd the served backend;
no startup marker or replacement-specific source updater is introduced.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--bundle", required=True)
  parser.add_argument("--target", required=True)
  args = parser.parse_args()
  if not re.fullmatch(r"[0-9a-f]{40}", args.target):
    parser.error("target must be the image's complete source commit")

  from app import platform_update
  from app.database import SessionLocal

  repo = platform_update.PLATFORM_REPO
  # Import the exact object without changing origin/main or choosing a release.
  # Apply revalidates its reviewed tip under the same lock before any mutation.
  with platform_update._reconcile_flock():
    platform_update._git("fetch", "--no-tags", args.bundle, args.target, repo=repo)
  preview = platform_update.platform_update_preview(repo, target_sha=args.target)
  with SessionLocal() as db:
    result = asyncio.run(platform_update.apply_platform_update(
      db, repo=repo, plan_id=preview["plan_id"],
      current_sha=preview["current_sha"], target_sha=args.target,
    ))
  print(json.dumps(result, ensure_ascii=False))
  return 0 if result["state"] in {"up_to_date", "restart_needed", "activation_needed"} else 1


if __name__ == "__main__":
  raise SystemExit(main())
