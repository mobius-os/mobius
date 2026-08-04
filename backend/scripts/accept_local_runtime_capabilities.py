"""Review or accept local runtime capabilities for one Store-installed app.

Ordinary source apply deliberately compiles local edits without widening the
Store-reviewed permission contract.  When the owner explicitly approves the
precise local runtime declaration, this command preserves every other reviewed
fact and replaces only ``capability_contract.runtime``.  Acceptance requires
the digest printed by a prior review, binding the write to the exact manifest
bytes that were inspected.

Run from ``/data/platform/backend``:

  python -m scripts.accept_local_runtime_capabilities --app-id 61
  python -m scripts.accept_local_runtime_capabilities --app-id 61 \
    --accept-digest <digest-from-review>
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from app import models, timeutil
from app.app_capabilities import (
  capability_digest,
  contract_with_runtime_capabilities,
  diff_contracts,
)
from app.config import get_settings
from app.database import SessionLocal


def _manifest_for(app: models.App) -> dict:
  if not app.source_dir:
    raise ValueError("App has no local source directory.")
  data_dir = Path(get_settings().data_dir).resolve()
  apps_root = (data_dir / "apps").resolve()
  # Validate the stored path lexically before touching the filesystem. Resolving
  # it would turn a symlink to a sibling app into an apparently valid direct
  # child and lose the Store app's source identity.
  source_dir = Path(os.path.abspath(app.source_dir))
  if source_dir.parent != apps_root or source_dir.name.isdigit():
    raise ValueError("App source directory is outside the reviewed apps root.")
  root_descriptor = None
  source_descriptor = None
  try:
    directory_flags = (
      os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
      | getattr(os, "O_CLOEXEC", 0)
    )
    root_descriptor = os.open(apps_root, directory_flags)
    try:
      source_descriptor = os.open(
        source_dir.name,
        directory_flags,
        dir_fd=root_descriptor,
      )
    except OSError as exc:
      raise ValueError(
        "App source directory must be a direct, regular directory under the apps root."
      ) from exc
    try:
      descriptor = os.open(
        "mobius.json",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
        dir_fd=source_descriptor,
      )
    except OSError as exc:
      raise ValueError(
        "mobius.json must be a regular file inside the app source."
      ) from exc
    with os.fdopen(descriptor, "r", encoding="utf-8") as manifest_file:
      if not stat.S_ISREG(os.fstat(manifest_file.fileno()).st_mode):
        raise ValueError("mobius.json must be a regular file inside the app source.")
      value = json.load(manifest_file)
  except ValueError:
    raise
  except (OSError, json.JSONDecodeError) as exc:
    raise ValueError(f"Could not read a valid mobius.json: {exc}") from exc
  finally:
    if source_descriptor is not None:
      os.close(source_descriptor)
    if root_descriptor is not None:
      os.close(root_descriptor)
  if not isinstance(value, dict):
    raise ValueError("mobius.json must contain an object.")
  return value


def review(app: models.App) -> tuple[dict, dict]:
  if app.manifest_url is None:
    raise ValueError("This command is only for Store-installed apps.")
  candidate = contract_with_runtime_capabilities(
    app.capability_contract,
    _manifest_for(app),
  )
  if candidate is None:
    raise ValueError("The installed app has no reviewed capability contract.")
  return candidate, {
    "app_id": app.id,
    "app": app.name,
    "current_runtime": (app.capability_contract or {}).get("runtime", {}),
    "candidate_runtime": candidate.get("runtime", {}),
    "diff": diff_contracts(app.capability_contract, candidate),
    "accept_digest": capability_digest(candidate),
  }


def _accept(db, app: models.App, candidate: dict, report: dict) -> None:
  """Commit only if the reviewed App row is still the current revision."""
  snapshot_updated_at = app.updated_at
  current = models.App.updated_at
  revision_match = (
    current.is_(None) if snapshot_updated_at is None
    else current == snapshot_updated_at
  )
  changed = (
    db.query(models.App)
    .filter(
      models.App.id == app.id,
      models.App.deleted_at.is_(None),
      revision_match,
    )
    .update({
      models.App.capability_contract: candidate,
      models.App.updated_at: timeutil.now_naive_utc(),
    }, synchronize_session=False)
  )
  if changed != 1:
    raise ValueError(
      "The app changed while capabilities were being accepted; review again."
    )
  db.commit()
  report["status"] = "accepted"


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--app-id", required=True, type=int)
  parser.add_argument("--accept-digest")
  args = parser.parse_args(argv)

  db = SessionLocal()
  try:
    app = (
      db.query(models.App)
      .filter(models.App.id == args.app_id, models.App.deleted_at.is_(None))
      .one_or_none()
    )
    if app is None:
      raise ValueError("App not found.")
    candidate, report = review(app)
    if args.accept_digest is None:
      report["status"] = "review"
      print(json.dumps(report, ensure_ascii=False, sort_keys=True))
      return
    if args.accept_digest != report["accept_digest"]:
      raise ValueError(
        "Acceptance digest does not match the current local manifest; review again."
      )
    _accept(db, app, candidate, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
  except ValueError as exc:
    db.rollback()
    parser.exit(2, f"error: {exc}\n")
  except Exception:
    db.rollback()
    raise
  finally:
    db.close()


if __name__ == "__main__":
  main()
