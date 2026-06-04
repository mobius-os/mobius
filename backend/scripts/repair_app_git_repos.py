"""Audit/backfill per-app git repos for installed app source dirs.

Dry-run by default. `--apply --yes` initializes only missing nested repos
under `/data/apps/<slug>` and seeds the current `index.jsx` as a
`migrated-base` upstream commit so the next manifest update has a real
merge base. Existing `.git` dirs are never rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


def _seed_secret_key_from_data() -> None:
  """Mirror entrypoint.sh for ad-hoc `docker exec` script runs.

  Docker Compose leaves SECRET_KEY blank in the container environment; the
  entrypoint exports the persisted /data/.secret-key only for the server
  process. A later `docker exec python -m scripts...` starts fresh and would
  fail Settings validation before this script can do anything useful.
  """
  if os.environ.get("SECRET_KEY"):
    return
  data_dir = Path(os.environ.get("DATA_DIR") or "/data")
  secret_file = data_dir / ".secret-key"
  try:
    secret = secret_file.read_text(encoding="utf-8").strip()
  except OSError:
    return
  if secret:
    os.environ["SECRET_KEY"] = secret


_seed_secret_key_from_data()

from app import app_git, models
from app.config import get_settings
from app.database import SessionLocal


@dataclass
class AuditRow:
  app_id: int
  slug: str
  source_dir: str
  status: str
  detail: str


def _is_safe_source_dir(source_dir: Path, data_dir: Path) -> tuple[bool, str]:
  """Return whether source_dir is exactly one non-numeric child of /data/apps."""
  try:
    resolved = source_dir.resolve()
    apps_root = (data_dir / "apps").resolve()
  except OSError as exc:
    return False, f"cannot resolve source_dir: {exc}"
  if resolved.parent != apps_root:
    return False, "source_dir is not an immediate child of data_dir/apps"
  if resolved.name.isdigit():
    return False, "source_dir basename is numeric"
  return True, str(resolved)


def _app_rows(db, source_dirs: set[str] | None) -> list[models.App]:
  query = (
    db.query(models.App)
    .filter(models.App.source_dir.isnot(None))
    .filter(models.App.manifest_url.isnot(None))
    .order_by(models.App.id)
  )
  rows = list(query.all())
  if source_dirs is None:
    return rows
  wanted = {str(Path(s).resolve()) for s in source_dirs}
  return [row for row in rows if str(Path(row.source_dir).resolve()) in wanted]


def _audit_or_repair_app(
  db,
  app: models.App,
  data_dir: Path,
  apply: bool,
) -> AuditRow:
  source_dir = Path(app.source_dir)
  ok, detail = _is_safe_source_dir(source_dir, data_dir)
  slug = app.slug or f"app-{app.id}"
  if not ok:
    return AuditRow(app.id, slug, str(source_dir), "skip", detail)

  source_dir = Path(detail)
  if not source_dir.is_dir():
    return AuditRow(app.id, slug, str(source_dir), "skip", "source_dir missing")
  index = source_dir / "index.jsx"
  if not index.is_file():
    return AuditRow(app.id, slug, str(source_dir), "skip", "index.jsx missing")

  has_repo = app_git.is_repo(source_dir)
  if has_repo:
    if app.upstream_commit:
      return AuditRow(app.id, slug, str(source_dir), "ok", "repo exists")
    return AuditRow(
      app.id, slug, str(source_dir), "manual",
      "repo exists but DB upstream_commit is empty; not rewriting existing repo",
    )

  if app.upstream_commit:
    return AuditRow(
      app.id, slug, str(source_dir), "manual",
      "DB has upstream_commit but source_dir has no .git; not overwriting provenance",
    )

  if not apply:
    return AuditRow(
      app.id, slug, str(source_dir), "would-repair",
      "missing nested .git; would seed current index.jsx as migrated-base",
    )

  base_bytes = index.read_bytes()
  app_git.record_upstream(
    source_dir,
    base_bytes,
    app.manifest_url or "mobius://repair-app-git-repos",
    "migrated-base",
  )
  app_git.align_local_to_upstream(source_dir)
  app_git.commit_local(
    source_dir,
    f"repair: seed app git repo for {slug}",
  )
  app.upstream_jsx_sha = hashlib.sha256(base_bytes).hexdigest()
  app.upstream_commit = app_git.head_sha(source_dir, app_git.UPSTREAM_BRANCH)
  db.add(app)
  db.commit()
  return AuditRow(
    app.id, slug, str(source_dir), "repaired",
    f"seeded upstream {app.upstream_commit}",
  )


def run(
  *,
  data_dir: Path,
  source_dirs: set[str] | None,
  apply: bool,
) -> list[AuditRow]:
  db = SessionLocal()
  try:
    rows = _app_rows(db, source_dirs)
    return [
      _audit_or_repair_app(db, app, data_dir, apply)
      for app in rows
    ]
  finally:
    db.close()


def _parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Audit or backfill missing per-app git repos.",
  )
  parser.add_argument(
    "--data-dir",
    default=get_settings().data_dir,
    help="Mobius data dir; defaults to configured DATA_DIR.",
  )
  parser.add_argument(
    "--source-dir",
    action="append",
    help="Limit to one app source dir. Repeat for multiple dirs.",
  )
  parser.add_argument(
    "--apply",
    action="store_true",
    help="Write missing repos and DB provenance. Dry-run when omitted.",
  )
  parser.add_argument(
    "--yes",
    action="store_true",
    help="Required with --apply.",
  )
  return parser.parse_args()


def main() -> int:
  args = _parse_args()
  if args.apply and not args.yes:
    print("Refusing --apply without --yes.")
    return 2

  rows = run(
    data_dir=Path(args.data_dir),
    source_dirs=set(args.source_dir) if args.source_dir else None,
    apply=args.apply,
  )
  for row in rows:
    print(
      f"{row.status}\tapp_id={row.app_id}\tslug={row.slug}\t"
      f"source_dir={row.source_dir}\t{row.detail}"
    )
  if not rows:
    print("No matching installed apps with source_dir.")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
