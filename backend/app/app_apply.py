"""Explicit acceptance of a local mini-app source revision.

The editable app directory is a draft. This module captures one immutable Git
tree, compiles that exact tree, commits it, and only then advances the live App
row to the content-addressed bundle. Callers own the lifecycle/app/source lock
span documented by the app lifecycle routes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import (
  app_git, app_python_env, chat_app_artifacts, icon_assets, managed_paths, models,
  service_preload, timeutil, tracing,
)
from app.app_capabilities import (
  contract_from_app_state,
  contract_from_manifest,
  local_manifest_runtime_fields,
)
from app.compiler import (
  _compiled_dir,
  compile_jsx,
  owned_bundle_path,
  publish_staged_bundle,
  unlink_app_bundle,
)
from app.config import get_settings
from app.manifest_identity import requested_manifest_source
from app.manifest_contract import (
  MANIFEST_MAX_BYTES,
  PACKAGE_MAX_BYTES,
  ManifestContractError,
  package_bytes_on_disk,
  package_limit_message,
  static_asset_entries,
  validate_manifest_contract,
  validate_repo_relative_path,
  validate_schedule_job,
)


class AppApplyError(RuntimeError):
  """Client-actionable rejection of a source apply."""

  def __init__(self, code: str, message: str, *, status_code: int = 422):
    super().__init__(message)
    self.code = code
    self.status_code = status_code


log = logging.getLogger("mobius.app_apply")

_STATIC_ASSETS_MANIFEST = managed_paths.STATIC_ASSETS_MANIFEST
_STATIC_ASSETS_BACKUP_SUFFIX = ".mobius-static-bak"
_STATIC_ASSETS_BACKUP_ASSET_PREFIX = "assets"
_STATIC_ASSETS_BACKUP_METADATA_PREFIX = "metadata"
_LOCAL_PACKAGE_WARNING = (
  "Local package declarations are active, including permissions, schedules, "
  "and skills; a future reviewed Store update may replace them."
)

_STORE_LOCAL_PACKAGE_DIVERGED = (
  "This app is Store-managed, so its live package comes from the accepted "
  "manifest. Your local mobius.json changes were NOT applied by this "
  "ordinary apply. Re-apply with --accept-local-package to make the local "
  "manifest authoritative."
)


@dataclass(frozen=True)
class ApplyResult:
  app: models.App
  mode: Literal["created", "updated", "unchanged"]
  warnings: tuple[str, ...] = ()


def _require_confined_non_symlink_path(
  root: Path,
  relative: str,
  *,
  code: str,
  message: str,
) -> None:
  """Reject a relative path if any existing component is a symlink."""
  if root.is_symlink():
    raise AppApplyError(code, message)
  resolved_root = root.resolve()
  candidate = root
  for part in relative.split("/"):
    candidate /= part
    if candidate.is_symlink():
      raise AppApplyError(code, message)
  try:
    candidate.resolve().relative_to(resolved_root)
  except (OSError, ValueError) as exc:
    raise AppApplyError(code, message) from exc


def _validate_static_asset_publish_paths(
  source_dir: Path, assets: dict[str, bytes],
) -> None:
  """Confine local asset outputs before Git or accepted state can advance."""
  metadata_message = "Managed static asset metadata must stay inside the app."
  _require_confined_non_symlink_path(
    source_dir,
    _STATIC_ASSETS_MANIFEST,
    code="static_asset_symlink",
    message=metadata_message,
  )
  metadata_path = source_dir / _STATIC_ASSETS_MANIFEST
  previous_assets: set[str] = set()
  if metadata_path.exists():
    try:
      previous_raw = json.loads(metadata_path.read_text())
      if isinstance(previous_raw, list):
        previous_assets = {
          path for path in previous_raw if isinstance(path, str)
        }
    except (OSError, json.JSONDecodeError):
      previous_assets = set()

  destinations = set(assets).union(previous_assets)
  _require_confined_non_symlink_path(
    source_dir,
    "static",
    code="static_asset_symlink",
    message="Managed static asset root must stay inside the app without symlinks.",
  )
  backup_name = f".{source_dir.name}{_STATIC_ASSETS_BACKUP_SUFFIX}"
  _require_confined_non_symlink_path(
    source_dir.parent,
    backup_name,
    code="static_asset_symlink",
    message="Managed static asset backup must stay beside the app without symlinks.",
  )
  _require_confined_non_symlink_path(
    source_dir.parent,
    (
      f"{backup_name}/{_STATIC_ASSETS_BACKUP_METADATA_PREFIX}/"
      f"{_STATIC_ASSETS_MANIFEST}"
    ),
    code="static_asset_symlink",
    message=metadata_message,
  )
  for destination in destinations:
    try:
      validate_repo_relative_path(
        destination, f"static_assets.{destination}",
      )
    except ManifestContractError as exc:
      raise AppApplyError("static_assets_unavailable", str(exc)) from exc
    message = (
      f"Manifest static asset destination {destination!r} must stay inside "
      "the app without symlinks."
    )
    _require_confined_non_symlink_path(
      source_dir,
      f"static/{destination}",
      code="static_asset_symlink",
      message=message,
    )
    _require_confined_non_symlink_path(
      source_dir.parent,
      f"{backup_name}/{_STATIC_ASSETS_BACKUP_ASSET_PREFIX}/{destination}",
      code="static_asset_symlink",
      message=message,
    )


def _snapshot_static_assets(
  snapshot_dir: Path, source_dir: Path, manifest: dict,
) -> dict[str, bytes]:
  """Read local package assets from the same immutable tree being accepted."""
  assets: dict[str, bytes] = {}
  for destination, source in static_asset_entries(
    manifest.get("static_assets") or {},
  ).items():
    _require_confined_non_symlink_path(
      source_dir,
      source,
      code="static_asset_symlink",
      message=(
        f"Manifest static asset source {source!r} must stay inside the app "
        "without symlinks."
      ),
    )
    path = snapshot_dir / source
    try:
      raw = path.read_bytes()
    except FileNotFoundError as exc:
      raise AppApplyError(
        "static_asset_missing",
        f"Manifest static asset {source!r} does not exist.",
      ) from exc
    except OSError as exc:
      raise AppApplyError(
        "static_asset_unreadable",
        f"Could not read manifest static asset {source!r}: {exc}",
      ) from exc
    assets[destination] = raw
  return assets


def _rollback_static_assets(
  created_paths: list[Path], rollback_actions: list,
) -> None:
  for action in reversed(rollback_actions):
    try:
      action()
    except OSError:
      log.exception("app apply: static asset rollback failed")
  for path in reversed(created_paths):
    try:
      path.unlink(missing_ok=True)
    except OSError:
      log.exception("app apply: static asset cleanup failed")


def _finish_static_assets(commit_actions: list) -> None:
  for action in commit_actions:
    try:
      action()
    except OSError:
      log.exception("app apply: static asset backup cleanup failed")


def _local_folder_skill_members(source_dir: Path, skills: list) -> list[str]:
  """Member paths of approved folder skills, as present in accepted source."""
  from app.manifest_contract import is_folder_skill_member

  members: list[str] = []
  for entry in skills:
    if not isinstance(entry, str) or not entry.endswith("/"):
      continue
    folder = source_dir / entry
    if folder.is_dir():
      members.extend(
        entry + child.name for child in sorted(folder.iterdir())
        if child.is_file() and not child.is_symlink()
        and is_folder_skill_member(child.name)
      )
  return members


async def _sync_accepted_app_skills(
  db: Session, app: models.App, manifest: dict | None,
) -> tuple[str, ...]:
  """Refresh declared skills at the same acceptance boundary as app source."""
  from app import install

  if manifest is None:
    contract = app.capability_contract or {}
    agent = contract.get("agent") if isinstance(contract, dict) else None
    skills = agent.get("skills") if isinstance(agent, dict) else None
    manifest = {"version": "accepted-local-revision"}
  else:
    skills = manifest.get("skills")
  skills = skills if isinstance(skills, list) else []
  # Metadata decides WHICH skills are approved; the accepted local source owns
  # their current bytes, which for an approved `<id>/` folder skill are the
  # member files inside it. A resolved update can add or drop folder members
  # that a remote manifest's file list does not name.
  manifest = {
    **manifest, "skills": skills,
    "source_files": _local_folder_skill_members(Path(app.source_dir), skills),
  }
  warnings: list[str] = []
  try:
    await install._sync_app_skills(db, app, manifest, warnings)
  except Exception as exc:
    log.exception("app apply: skill sync failed post-commit")
    warnings.append(f"skills: sync failed — {exc!r}")
  return tuple(warnings)


async def _sync_accepted_app_side_effects(
  db: Session,
  app: models.App,
  manifest: dict | None,
  *,
  drop_prior_cron: bool,
) -> tuple[str, ...]:
  """Converge post-commit declarations for one accepted local revision.

  The caller holds the app source lock. Store-managed worktrees normally have
  no local manifest authority; the explicit local-package path supplies the
  validated manifest when the owner deliberately accepts that package.
  """
  warnings: list[str] = []
  if manifest is not None:
    from app import install

    schedule = manifest.get("schedule")
    try:
      await install._sync_manifest_cron_unlocked(
        app=app,
        manifest=manifest,
        drop_prior_cron=drop_prior_cron,
        bundled_job=bool(schedule and schedule.get("job")),
        warnings=warnings,
        owner_zone=install.owner_timezone(db),
      )
    except Exception as exc:
      log.exception("app apply: cron sync failed post-commit")
      warnings.append(f"cron: registration failed — {exc!r}")
  from app import app_setup
  app_setup.request_run()
  warnings.extend(await _sync_accepted_app_skills(db, app, manifest))
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


def _read_manifest(snapshot_dir: Path) -> dict:
  path = snapshot_dir / "mobius.json"
  try:
    raw = path.read_bytes()
  except FileNotFoundError as exc:
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
  size = package_bytes_on_disk(snapshot_dir, manifest)
  if size > PACKAGE_MAX_BYTES:
    raise AppApplyError("package_too_large", package_limit_message(size))
  return dict(manifest)


def _store_local_package_divergence(app: models.App, snapshot_dir: Path) -> str | None:
  """Warn when ordinary Store apply drops local mobius.json edits.

  Ordinary apply of a Store-managed app publishes code but keeps the accepted
  package manifest, the runtime root's mobius.json. Any local manifest that
  differs from it was not applied. Best-effort: a read or parse problem
  returns None so a valid apply is never blocked by the check.
  """
  from app import applied_app_runtime

  try:
    local = json.loads((snapshot_dir / "mobius.json").read_bytes())
    accepted = json.loads(
      (applied_app_runtime.runtime_root(app) / "mobius.json").read_bytes()
    )
  except (OSError, ValueError, applied_app_runtime.AppliedRuntimeUnavailable):
    return None
  return _STORE_LOCAL_PACKAGE_DIVERGED if local != accepted else None


def _entry_source(snapshot_dir: Path, relative: str) -> str:
  entry = snapshot_dir / relative
  try:
    raw = entry.read_bytes()
  except FileNotFoundError as exc:
    raise AppApplyError(
      "entry_missing",
      f"App entry {relative!r} does not exist.",
    ) from exc
  except OSError as exc:
    raise AppApplyError(
      "entry_unreadable", f"Could not read {relative}: {exc}",
    ) from exc
  try:
    source = raw.decode("utf-8")
  except UnicodeDecodeError as exc:
    raise AppApplyError(
      "entry_invalid", f"App entry {relative!r} is not UTF-8.",
    ) from exc
  if not source.strip():
    raise AppApplyError("entry_empty", "Manifest entry index.jsx is empty.")
  return source


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
  try:
    return icon_assets.normalize_icon(raw)
  except icon_assets.InvalidIcon as exc:
    raise AppApplyError("icon_invalid", str(exc)) from exc


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


def _validate_local_identity(
  source_dir: Path, manifest: dict, app: models.App | None = None,
) -> None:
  accepted_ids = {source_dir.name}
  if app is not None and app.manifest_url:
    _, bound_id = requested_manifest_source(app.manifest_url)
    if bound_id is not None:
      accepted_ids.add(bound_id)
  if manifest["id"] not in accepted_ids:
    raise AppApplyError(
      "manifest_id_mismatch",
      "For a local app, mobius.json `id` must match the source-directory "
      f"name or its installed package identity ({sorted(accepted_ids)!r}).",
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


def _apply_explicit_package_runtime(
  db: Session, app: models.App, manifest: dict, *, package_icon: bytes | None,
) -> None:
  """Persist every live field owned by an explicitly accepted Store package.

  Store installation and local-source acceptance are two entry points into
  the same App row. Keep their runtime projection aligned so accepting a local
  package cannot compile successfully while silently dropping permissions,
  project templates, offline metadata, or other declarations.
  """
  from app import install

  runtime_fields = local_manifest_runtime_fields(manifest)
  permissions = manifest.get("permissions") or {}
  app.name = manifest["name"]
  app.description = manifest["description"]
  app.version = str(manifest.get("version", "")).strip() or None
  app.theme_color = install._manifest_color(manifest.get("theme_color"))
  app.background_color = (
    install._manifest_color(manifest.get("background_color"))
    or app.theme_color
  )
  app.display = install._manifest_display(manifest.get("display"))
  app.icon_png = package_icon
  app.cross_app_access = permissions.get("cross_app_access", "none")
  app.share_with_apps = permissions.get("share_with_apps", "none")
  app.chat_log_access = permissions.get("chat_log_access", "none")
  # Privileged grants are opt-in on every explicitly accepted package;
  # omission revokes them just as a reviewed Store update does.
  app.manage_apps = bool(permissions.get("manage_apps", False))
  app.manage_skills = bool(permissions.get("manage_skills", False))
  app.github_access = bool(permissions.get("github_access", False))
  app.github_connect = bool(permissions.get("github_connect", False))
  app.filesystem_access = bool(permissions.get("filesystem_access", False))
  app.connections_manage = bool(permissions.get("connections_manage", False))
  app.connect_manage = bool(permissions.get("connect_manage", False))
  if "offline_capable" in runtime_fields:
    app.offline_capable = runtime_fields["offline_capable"]
  if "embeds_agent" in manifest:
    app.embeds_agent = bool(manifest["embeds_agent"])
  app.shell_shortcuts = bool(manifest.get("shell_shortcuts", True))
  app.offline_contract = manifest.get("offline") or None
  app.system_prompt_file = manifest.get("system_prompt") or None
  app.project_templates_json = manifest.get("project_templates") or None
  service = manifest.get("service")
  app.service_id = install._manifest_service_id(manifest, app=app)
  install._sync_service_aliases(db, app=app, manifest=manifest)
  effective_manifest = dict(manifest)
  # The reviewed Store updater preserves these two live fields when an older
  # manifest omits them. Normalize the accepted local package against the same
  # effective state so its durable contract cannot disagree with its App row.
  effective_manifest.setdefault("offline_capable", app.offline_capable)
  effective_manifest.setdefault("embeds_agent", app.embeds_agent)
  if isinstance(effective_manifest.get("service"), dict):
    effective_manifest["service"] = dict(effective_manifest["service"])
    effective_manifest["service"].setdefault("id", app.service_id)
  app.capability_contract = contract_from_manifest(effective_manifest)


def _apply_local_manifest_runtime(
  db: Session, app: models.App, manifest: dict, *, package_icon: bytes | None,
) -> None:
  """Apply owner-authored metadata without granting server permissions.

  A local app's durable row owns its server grants. Its manifest still owns
  display metadata, project templates, and host-mediated capabilities, so
  ordinary source apply can update those fields without becoming a permission
  escalation path.
  """
  from app import install

  runtime_fields = local_manifest_runtime_fields(manifest)
  app.name = manifest["name"]
  app.description = manifest["description"]
  app.version = str(manifest.get("version", "")).strip() or None
  app.theme_color = install._manifest_color(manifest.get("theme_color"))
  app.background_color = (
    install._manifest_color(manifest.get("background_color"))
    or app.theme_color
  )
  app.display = install._manifest_display(manifest.get("display"))
  app.icon_png = package_icon
  if "offline_capable" in runtime_fields:
    app.offline_capable = runtime_fields["offline_capable"]
  app.embeds_agent = bool(manifest.get("embeds_agent", False))
  app.shell_shortcuts = bool(manifest.get("shell_shortcuts", True))
  app.offline_contract = manifest.get("offline") or None
  app.system_prompt_file = manifest.get("system_prompt") or None
  app.project_templates_json = manifest.get("project_templates") or None
  service = manifest.get("service")
  app.service_id = install._manifest_service_id(manifest, app=app)
  install._sync_service_aliases(db, app=app, manifest=manifest)
  if isinstance(service, dict):
    service = dict(service)
    service.setdefault("id", app.service_id)
  app.capability_contract = contract_from_app_state(
    app,
    capabilities=runtime_fields["capabilities"],
    public_access=runtime_fields["public_access"],
    contract_permissions=manifest.get("permissions") or {},
    service=service,
    tools=list(manifest.get("tools") or []),
    model_provider=manifest.get("model_provider"),
  )


def _live_runtime_state(app: models.App) -> tuple:
  """Fields whose manifest-driven changes make an apply non-empty."""
  return (
    app.name,
    app.description,
    app.version,
    app.theme_color,
    app.background_color,
    app.display,
    app.cross_app_access,
    app.share_with_apps,
    app.chat_log_access,
    app.manage_apps,
    app.manage_skills,
    app.github_access,
    app.github_connect,
    app.filesystem_access,
    app.connections_manage,
    app.connect_manage,
    app.offline_capable,
    app.embeds_agent,
    app.shell_shortcuts,
    app.offline_contract,
    app.system_prompt_file,
    app.project_templates_json,
    app.package_id,
    app.source_identity,
    app.service_id,
    app.capability_contract,
    app.chat_id,
    app.jsx_source,
    app.compiled_path,
    app.source_commit,
    app.runtime_revision,
    app.icon_png,
    app.icon_override_png,
    app.published_manifest_url,
  )


async def apply_source_revision(
  db: Session,
  *,
  source_dir: str,
  app: models.App | None,
  chat_id: str | None,
  accept_local_package: bool = False,
) -> ApplyResult:
  """Compile, accept, and publish one source revision (traced as app.apply)."""
  with tracing.span("app.apply", {
    "mobius.app_id": app.id if app is not None else None,
    "mobius.chat_id": chat_id,
  }):
    return await _apply_source_revision(
      db,
      source_dir=source_dir,
      app=app,
      chat_id=chat_id,
      accept_local_package=accept_local_package,
    )


async def _apply_source_revision(
  db: Session,
  *,
  source_dir: str,
  app: models.App | None,
  chat_id: str | None,
  accept_local_package: bool = False,
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
  # A pending Store update is reconciled in its own private checkout, so it
  # never blocks ordinary edits here; finishing it merges them in. Only a
  # half-finished Git merge in this directory itself can't become a revision.
  if await asyncio.to_thread(app_git.merge_in_progress, source_path):
    raise AppApplyError(
      "merge_in_progress",
      "This app directory has an unfinished Git merge. Finish it with "
      "`git commit` or undo it with `git merge --abort`, then apply again.",
      status_code=409,
    )
  if accept_local_package and app is not None:
    from app import install

    if install.pending_update_receipt_file(source_path).is_file():
      # Finishing the pending update installs its reviewed package metadata;
      # accepting local metadata now would be silently overwritten by it.
      raise AppApplyError(
        "update_pending",
        "Finish this app's pending Store update before accepting local "
        "package declarations.",
        status_code=409,
      )
  if accept_local_package and (
    app is None or app.manifest_url is None
  ):
    raise AppApplyError(
      "local_package_requires_store_app",
      "--accept-local-package is only valid for an installed Store app; "
      "ordinary local apps keep their owner-managed permission settings.",
    )

  candidate = await _git_operation(
    "snapshot", app_git.snapshot_worktree, source_path,
  )
  previous_bundle = None
  published = None
  staged = None
  runtime_staged = None
  python_env = None
  published_env = None
  static_created: list[Path] = []
  static_rollback: list = []
  static_commit: list = []
  static_materialized = False
  durable_commit = False
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
      store_managed = app is not None and app.manifest_url is not None
      manifest = (
        _read_manifest(snapshot_dir)
        if not store_managed or accept_local_package
        else None
      )
      store_divergence_warning = (
        _store_local_package_divergence(app, snapshot_dir)
        if store_managed and not accept_local_package and app is not None
        else None
      )
      if manifest is not None:
        _validate_local_identity(source_path, manifest, app)
        provider = manifest.get("model_provider")
        if (not store_managed and isinstance(provider, dict)
            and provider.get("transport") == "identity_broker"):
          # The broker signs with the owner's Möbius account, so only the
          # reviewed Möbius · You package may route turns through it. Local
          # apps declare their own HTTPS endpoint and app-secret key.
          raise AppApplyError(
            "local_model_broker",
            "Local apps may declare an HTTPS model provider with their own "
            "app secret, not the protected Möbius broker transport.",
          )
        schedule = manifest.get("schedule")
        job_name = schedule.get("job") if isinstance(schedule, dict) else None
        if job_name:
          try:
            validate_schedule_job(manifest, (snapshot_dir / job_name).read_bytes())
          except (OSError, ManifestContractError) as exc:
            raise AppApplyError(
              "invalid_schedule_job", str(exc), status_code=422,
            ) from exc
      static_assets = (
        _snapshot_static_assets(snapshot_dir, source_path, manifest)
        if manifest is not None
        else {}
      )
      # Ordinary Store edits intentionally exclude reviewed package metadata.
      # Only the explicit local-package mode reads that manifest; local apps
      # always retain the strict manifest reader and its declared entry.
      entry_relative = "index.jsx" if store_managed else manifest["entry"]
      source = _entry_source(snapshot_dir, entry_relative)
      package_icon = (
        _manifest_icon(snapshot_dir, manifest)
        if manifest is not None
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
          # SQLAlchemy's Python column defaults materialize on INSERT. This
          # value participates in the pre-INSERT capability projection, so it
          # must already match the durable default before compilation.
          chat_log_access="none",
          offline_capable=False,
        )
      assert app is not None
      previous_state = _live_runtime_state(app)
      # Captured explicitly so the published-manifest reset below is decoupled
      # from the runtime-state tuple's field order (the helper carries more
      # fields than upstream's inline tuple did).
      previous_source_commit = app.source_commit
      previous_runtime_revision = app.runtime_revision

      # Explicit apply is serialized by the lifecycle lock, so it can compile
      # under one shared, non-servable name before a new row has a numeric id.
      # Publication still begins only after the bytes move into the app-owned
      # staging contract below.
      staged = _compiled_dir() / "app-apply.js.staging"
      await compile_jsx(
        source,
        out_path=staged,
        source_path=snapshot_dir / entry_relative,
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

      if manifest is not None:
        _validate_static_asset_publish_paths(source_path, static_assets)

      if manifest is not None:
        from app import install

        service_id = install._manifest_service_id(manifest, app=app)
        service_aliases = install._manifest_service_aliases(manifest)
        install._assert_service_identity_available(
          db, service_id=service_id, app_id=app.id,
        )
        install._assert_service_aliases_available(
          db, aliases=service_aliases, service_id=service_id, app_id=app.id,
        )
        install._assert_service_transition_safe(
          app, service_id=service_id, aliases=service_aliases,
        )

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
      if manifest is not None:
        static_materialized = True
        try:
          install._write_static_assets(
            source_path,
            static_assets,
            static_created,
            static_rollback,
            static_commit,
          )
        except (OSError, HTTPException, ValueError) as exc:
          detail = getattr(exc, "detail", None) or str(exc)
          raise AppApplyError(
            "static_assets_unavailable",
            f"Could not publish the accepted static assets: {detail}",
            status_code=409,
          ) from exc
      if (
        app.manifest_url is None
        and app.source_commit != previous_source_commit
      ):
        # A distribution manifest is a statement about one exact accepted package. Once
        # local source advances, require publication verification again rather
        # than silently offering a stale repository to other people.
        app.published_manifest_url = None
      from app import applied_app_runtime
      runtime_options = {}
      runtime_assets = static_assets
      if manifest is None:
        # Ordinary Store source Apply accepts code, not a new package contract.
        # Its ignored generated assets and runtime declarations remain exactly
        # those from the previously applied package.
        previous_runtime = applied_app_runtime.runtime_root(app)
        previous_static = previous_runtime / "static"
        runtime_assets = {
          path.relative_to(previous_static).as_posix(): path.read_bytes()
          for path in previous_static.rglob("*") if path.is_file()
        }
        previous_manifest = previous_runtime / "mobius.json"
        runtime_options["runtime_manifest"] = (
          previous_manifest.read_bytes() if previous_manifest.is_file() else None
        )
      runtime_staged = await asyncio.to_thread(
        applied_app_runtime.prepare_runtime,
        source_path, app.source_commit,
        static_assets=runtime_assets,
        **runtime_options,
      )
      # Build the declared Python environment from the accepted tree before
      # its pointer is published: a failure leaves the previous revision live.
      try:
        python_env = await asyncio.to_thread(
          app_python_env.prepare_env,
          get_settings().data_dir, None if created else app.id,
          runtime_staged.root,
        )
      except app_python_env.PythonEnvBuildError as exc:
        raise AppApplyError(
          "python_env_failed",
          f"Could not build the app's Python environment. {exc}",
          status_code=422,
        ) from exc
      # Begin the SQLite write transaction only now, after the last await.
      # Holding it across the build, Git commit or environment preparation
      # let a writer that waits on SQLite inside the event loop (any async
      # route committing) stall this apply until its busy timeout expired,
      # failing that unrelated request. Everything slow or failure-prone above
      # is independent of the durable row, and nothing below awaits before
      # the commit or rollback that ends the transaction.
      if created:
        db.add(app)
        db.flush()
      if manifest is not None:
        if store_managed and accept_local_package:
          _apply_explicit_package_runtime(
            db, app, manifest, package_icon=package_icon,
          )
        else:
          _apply_local_manifest_runtime(
            db, app, manifest, package_icon=package_icon,
          )
      if chat_id is not None:
        app.chat_id = chat_id
      if python_env is not None:
        published_env = app_python_env.publish_env(
          get_settings().data_dir, app.id, python_env,
        )
        python_env = None
      applied_app_runtime.publish_runtime(app, runtime_staged)
      runtime_staged = None
      app_staged = _compiled_dir() / f"app-{app.id}.js.staging"
      staged.replace(app_staged)
      staged = app_staged
      previous_bundle = owned_bundle_path(app.id, app.compiled_path)
      published = publish_staged_bundle(app.id, staged)
      staged = None

      changed = (
        created
        or committed is not None
        or previous_state != _live_runtime_state(app)
      )
      if not changed:
        db.rollback()
        warnings = await _sync_accepted_app_side_effects(
          db, app, manifest, drop_prior_cron=False,
        )
        _finish_static_assets(static_commit)
        static_materialized = False
        if store_managed and accept_local_package:
          warnings = (*warnings, _LOCAL_PACKAGE_WARNING)
        elif store_divergence_warning:
          warnings = (*warnings, store_divergence_warning)
        return ApplyResult(app=app, mode="unchanged", warnings=warnings)

      app.jsx_source = source
      app.compiled_path = str(published)
      app.updated_at = timeutil.now_naive_utc()
      if chat_id is not None:
        chat_app_artifacts.record_touch(
          db,
          chat_id=chat_id,
          app_id=app.id,
          touched_at=app.updated_at,
        )
      try:
        db.commit()
      except Exception:
        db.rollback()
        if published != previous_bundle:
          unlink_app_bundle(app.id, published)
        raise
      durable_commit = True
      _finish_static_assets(static_commit)
      static_materialized = False
      if previous_bundle != published:
        unlink_app_bundle(app.id, previous_bundle)
      db.refresh(app)
      # A preloaded service host pins the runtime it imported.
      service_preload.retire(app.id, keep_revision=app.runtime_revision)
      try:
        await asyncio.to_thread(
          applied_app_runtime.prune_runtime, app,
          previous_revision=previous_runtime_revision,
        )
      except OSError:
        log.warning("Could not prune older applied runtime trees", exc_info=True)
      warnings = await _sync_accepted_app_side_effects(
        db, app, manifest, drop_prior_cron=not created,
      )
      if store_managed and accept_local_package:
        warnings = (*warnings, _LOCAL_PACKAGE_WARNING)
      elif store_divergence_warning:
        warnings = (*warnings, store_divergence_warning)
      return ApplyResult(
        app=app,
        mode="created" if created else "updated",
        warnings=warnings,
      )
  except Exception:
    db.rollback()
    if runtime_staged is not None:
      shutil.rmtree(runtime_staged.root)
    app_python_env.discard_env(python_env)
    if not durable_commit:
      # No row may reference it; a rolled-back new app has none to drive GC.
      app_python_env.unpublish_env(published_env)
    if not durable_commit and static_materialized:
      _rollback_static_assets(static_created, static_rollback)
    if staged is not None:
      staged.unlink(missing_ok=True)
    if (
      not durable_commit
      and published is not None
      and app is not None
      and published != previous_bundle
    ):
      unlink_app_bundle(app.id, published)
    raise
