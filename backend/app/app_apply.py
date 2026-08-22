"""Explicit acceptance of a local mini-app source revision.

The editable app directory is a draft. This module captures one immutable Git
tree, compiles that exact tree, commits it, and only then advances the live App
row to the content-addressed bundle. Callers own the lifecycle/app/source lock
span documented by the app lifecycle routes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from app import app_git, icon_assets, models, timeutil
from app.app_capabilities import (
  contract_from_app_state,
  local_manifest_runtime_fields,
)
from app.compiler import (
  _compiled_dir,
  compile_jsx,
  owned_bundle_path,
  publish_staged_bundle,
  unlink_app_bundle,
)
from app.manifest_contract import (
  ICON_MAX_BYTES,
  MANIFEST_MAX_BYTES,
  ManifestContractError,
  validate_manifest_contract,
)


class AppApplyError(RuntimeError):
  """Client-actionable rejection of a source apply."""

  def __init__(self, code: str, message: str, *, status_code: int = 422):
    super().__init__(message)
    self.code = code
    self.status_code = status_code


log = logging.getLogger("mobius.app_apply")


@dataclass(frozen=True)
class ApplyResult:
  app: models.App
  mode: Literal["created", "updated", "unchanged"]
  warnings: tuple[str, ...] = ()


async def _sync_accepted_app_skills(
  db: Session, app: models.App, manifest: dict | None,
) -> tuple[str, ...]:
  """Refresh declared skills at the same acceptance boundary as app source."""
  from app import install

  if manifest is None:
    contract = app.capability_contract or {}
    agent = contract.get("agent") if isinstance(contract, dict) else None
    skills = agent.get("skills") if isinstance(agent, dict) else None
    manifest = {
      # Store metadata remains authoritative for WHICH skills are approved;
      # the accepted local source revision owns their current bytes.
      "skills": skills if isinstance(skills, list) else [],
      "version": "accepted-local-revision",
    }
  warnings: list[str] = []
  try:
    await install._sync_app_skills(db, app, manifest, warnings)
  except Exception as exc:
    log.exception("app apply: skill sync failed post-commit")
    warnings.append(f"skills: sync failed — {exc!r}")
  return tuple(warnings)


async def _git_operation(label: str, fn, *args):
  """Run one app-repository operation with a stable client-facing failure.

  Git failures are usually actionable source state (ownership, corruption, or
  an unsupported tree entry), not an ASGI bug. Preserve the dedicated
  compare-and-swap exception so the route can return its narrower
  ``source_changed`` response; normalize the remaining expected filesystem and
  subprocess failures so agents see what to fix instead of an opaque 500.
  """
  try:
    return await asyncio.to_thread(fn, *args)
  except app_git.SourceTreeChanged:
    raise
  except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
    detail = str(getattr(exc, "stderr", "") or "").strip()
    suffix = f" {detail[-1000:]}" if detail else ""
    raise AppApplyError(
      "source_repository_error",
      f"Could not {label} the app source revision.{suffix}",
      status_code=409,
    ) from exc


def _read_manifest(
  snapshot_dir: Path, *, allow_store_managed_missing: bool = False,
) -> dict:
  path = snapshot_dir / "mobius.json"
  try:
    raw = path.read_bytes()
  except FileNotFoundError as exc:
    if allow_store_managed_missing:
      # Store installs deliberately keep mobius.json out of per-app Git
      # history: the reviewed package metadata and capabilities remain owned
      # by the installer/App row, while this apply path accepts only editable
      # source. The manifest contract fixes the entry at index.jsx, so this is
      # the complete source-facing fact needed to compile a local edit.
      return {"entry": "index.jsx"}
    raise AppApplyError(
      "manifest_missing",
      "mobius.json is required before applying a local app.",
    ) from exc
  except OSError as exc:
    raise AppApplyError(
      "manifest_unreadable", f"Could not read mobius.json: {exc}",
    ) from exc
  if len(raw) > MANIFEST_MAX_BYTES:
    raise AppApplyError(
      "manifest_too_large",
      f"mobius.json exceeds the {MANIFEST_MAX_BYTES}-byte limit.",
    )
  try:
    manifest = json.loads(raw)
  except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise AppApplyError(
      "manifest_invalid", f"Invalid mobius.json: {exc}",
    ) from exc
  try:
    validate_manifest_contract(manifest)
  except ManifestContractError as exc:
    raise AppApplyError("manifest_invalid", str(exc)) from exc
  return dict(manifest)


def _entry_source(snapshot_dir: Path, manifest: dict) -> str:
  entry = snapshot_dir / manifest["entry"]
  try:
    raw = entry.read_bytes()
  except FileNotFoundError as exc:
    raise AppApplyError(
      "entry_missing",
      f"Manifest entry {manifest['entry']!r} does not exist.",
    ) from exc
  except OSError as exc:
    raise AppApplyError(
      "entry_unreadable", f"Could not read {manifest['entry']}: {exc}",
    ) from exc
  try:
    source = raw.decode("utf-8")
  except UnicodeDecodeError as exc:
    raise AppApplyError(
      "entry_invalid", f"Manifest entry {manifest['entry']!r} is not UTF-8.",
    ) from exc
  if not source.strip():
    raise AppApplyError("entry_empty", "Manifest entry index.jsx is empty.")
  return source


def _normalize_manifest_icon(relative: str, raw: bytes) -> bytes:
  if len(raw) > ICON_MAX_BYTES:
    raise AppApplyError(
      "icon_too_large",
      f"Manifest icon {relative!r} exceeds the {ICON_MAX_BYTES}-byte limit.",
    )
  try:
    return icon_assets.normalize_icon(raw)
  except icon_assets.InvalidIcon as exc:
    raise AppApplyError("icon_invalid", str(exc)) from exc


def _manifest_icon(snapshot_dir: Path, manifest: dict) -> bytes | None:
  """Normalize the icon declared by this exact accepted source snapshot."""
  relative = manifest.get("icon")
  if not relative:
    return None
  path = snapshot_dir / relative
  try:
    raw = path.read_bytes()
  except FileNotFoundError as exc:
    raise AppApplyError(
      "icon_missing", f"Manifest icon {relative!r} does not exist.",
    ) from exc
  except OSError as exc:
    raise AppApplyError(
      "icon_unreadable", f"Could not read manifest icon {relative!r}: {exc}",
    ) from exc
  return _normalize_manifest_icon(relative, raw)


def retire_integrated_app_provenance(db: Session) -> tuple[int, list[str]]:
  """Bound app provenance metadata at the existing startup maintenance edge."""
  retired = 0
  warnings: list[str] = []
  apps = (
    db.query(models.App)
    .filter(
      models.App.deleted_at.is_(None),
      models.App.source_dir.isnot(None),
    )
    .order_by(models.App.id)
    .all()
  )
  for app in apps:
    source = Path(app.source_dir)
    try:
      if (
        not app_git.is_repo(source)
        or not app_git.ref_exists(source, app_git.UPSTREAM_BRANCH)
      ):
        continue
      upstream = app_git.head_sha(source, app_git.UPSTREAM_BRANCH)
      retired += app_git.retire_landed_equivalent_changes(source, upstream)
    except Exception as exc:
      warnings.append(f"app {app.id}: {exc}")
  return retired, warnings


def _validate_local_identity(source_dir: Path, manifest: dict) -> None:
  if manifest["id"] != source_dir.name:
    raise AppApplyError(
      "manifest_id_mismatch",
      "For a local app, mobius.json `id` must match the source-directory "
      f"name ({source_dir.name!r}).",
    )
  if len(manifest["id"]) > 128:
    raise AppApplyError(
      "manifest_id_too_long", "Manifest `id` must be at most 128 characters.",
    )
  if len(manifest["name"]) > 128:
    raise AppApplyError(
      "manifest_name_too_long",
      "Manifest `name` must be at most 128 characters.",
    )


async def apply_source_revision(
  db: Session,
  *,
  source_dir: str,
  app: models.App | None,
  chat_id: str | None,
) -> ApplyResult:
  """Compile, accept, and publish one source revision.

  ``app`` is either the live row freshly loaded under its app lock or ``None``
  for a source directory not yet claimed by a row. The caller also holds the
  lifecycle and source-dir locks for the whole call.
  """
  source_path = Path(source_dir)
  if app is not None and app.source_dir != source_dir:
    raise AppApplyError(
      "source_identity_changed",
      "The app no longer owns this source directory.",
      status_code=409,
    )
  if app is not None and app.manifest_url is not None:
    from app import install

    receipt = (
      source_path / ".git" / install._PENDING_UPDATE_DIR / "receipt.json"
    )
    if (
      receipt.is_file()
      or await asyncio.to_thread(app_git.merge_in_progress, source_path)
    ):
      raise AppApplyError(
        "update_resolution_required",
        "This Store app has a pending update. Resolve it with "
        "resolve_app_update.py instead of applying an ordinary edit.",
        status_code=409,
      )

  candidate = await _git_operation(
    "snapshot", app_git.snapshot_worktree, source_path,
  )
  previous_bundle = None
  published = None
  staged = None
  created = app is None
  try:
    with tempfile.TemporaryDirectory(prefix="mobius-app-source-") as tmp:
      snapshot_dir = Path(tmp)
      await _git_operation(
        "materialize",
        app_git.materialize_tree,
        source_path,
        candidate.tree_oid,
        snapshot_dir,
      )
      manifest = _read_manifest(
        snapshot_dir,
        allow_store_managed_missing=(
          app is not None and app.manifest_url is not None
        ),
      )
      if app is None or app.manifest_url is None:
        _validate_local_identity(source_path, manifest)
      source = _entry_source(snapshot_dir, manifest)
      package_icon = (
        _manifest_icon(snapshot_dir, manifest)
        if app is None or app.manifest_url is None
        else None
      )

      if created:
        app = models.App(
          name=manifest["name"],
          description=manifest["description"],
          jsx_source="",
          compiled_path="",
          chat_id=chat_id,
          source_dir=source_dir,
          slug=manifest["id"],
          cross_app_access="none",
          share_with_apps="none",
          offline_capable=False,
        )
        db.add(app)
        db.flush()
      assert app is not None
      previous_state = (
        app.name,
        app.description,
        app.offline_capable,
        app.capability_contract,
        app.chat_id,
        app.jsx_source,
        app.compiled_path,
        app.source_commit,
        app.published_manifest_url,
        app.icon_png,
        app.icon_override_png,
      )

      staged = _compiled_dir() / f"app-{app.id}.js.staging"
      await compile_jsx(
        app.id,
        source,
        out_path=staged,
        source_path=snapshot_dir / manifest["entry"],
      )

      stable = await _git_operation(
        "re-snapshot", app_git.snapshot_worktree, source_path,
      )
      if stable != candidate:
        raise AppApplyError(
          "source_changed",
          "App source changed while it was being applied; retry after the "
          "current edit is complete.",
          status_code=409,
        )

      if app.manifest_url is None:
        runtime_fields = local_manifest_runtime_fields(manifest)
        app.name = manifest["name"]
        app.description = manifest["description"]
        app.icon_png = package_icon
        if "offline_capable" in runtime_fields:
          app.offline_capable = runtime_fields["offline_capable"]
        app.capability_contract = contract_from_app_state(
          app,
          capabilities=runtime_fields["capabilities"],
          public_access=runtime_fields["public_access"],
          contract_permissions=manifest.get("permissions") or {},
        )
      if chat_id is not None:
        app.chat_id = chat_id

      previous_bundle = owned_bundle_path(app.id, app.compiled_path)
      committed = await _git_operation(
        "commit",
        app_git.commit_worktree_tree,
        source_path,
        candidate,
        "create app" if created else "apply app source",
      )
      # Bind SQLite to the exact accepted Git revision before publication. On
      # the accepted-ahead retry, ``committed`` is None and the candidate
      # parent is the already-accepted tip.
      app.source_commit = committed or candidate.parent_sha
      if (
        app.manifest_url is None
        and app.source_commit != previous_state[7]
      ):
        # A distribution manifest is a statement about one exact accepted package. Once
        # local source advances, require publication verification again rather
        # than silently offering a stale repository to other people.
        app.published_manifest_url = None
      published = publish_staged_bundle(app.id, staged)
      staged = None

      changed = (
        created
        or committed is not None
        or previous_state != (
          app.name,
          app.description,
          app.offline_capable,
          app.capability_contract,
          app.chat_id,
          source,
          str(published),
          app.source_commit,
          app.published_manifest_url,
          app.icon_png,
          app.icon_override_png,
        )
      )
      if not changed:
        db.rollback()
        warnings = await _sync_accepted_app_skills(db, app, manifest)
        return ApplyResult(app=app, mode="unchanged", warnings=warnings)

      app.jsx_source = source
      app.compiled_path = str(published)
      app.updated_at = timeutil.now_naive_utc()
      try:
        db.commit()
      except Exception:
        db.rollback()
        if published != previous_bundle:
          unlink_app_bundle(app.id, published)
        raise
      if previous_bundle != published:
        unlink_app_bundle(app.id, previous_bundle)
      db.refresh(app)
      warnings = await _sync_accepted_app_skills(db, app, manifest)
      return ApplyResult(
        app=app,
        mode="created" if created else "updated",
        warnings=warnings,
      )
  except Exception:
    db.rollback()
    if staged is not None:
      staged.unlink(missing_ok=True)
    if published is not None and app is not None and published != previous_bundle:
      unlink_app_bundle(app.id, published)
    raise
