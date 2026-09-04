"""First-class owner Projects: persistence, templates, and confined files."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import uuid
import weakref
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from sqlalchemy.orm import Session, defer
from sqlalchemy.orm.attributes import flag_modified

from app import (
  auth, github_auth, models, project_builders, project_drawer, project_git, providers,
  questions, workspace_files,
)
from app.broadcast import get_system_broadcast
from app.chat import (
  _finish_run,
  bump_run_generation,
  is_chat_running,
  mark_chat_deleted,
  recover_chat_generation,
  stop_chat_for,
)
from app.config import get_settings
from app.database import get_db
from app.deps import (
  ProjectPrincipal, get_current_owner, get_project_principal, reject_cross_site,
  resolve_project_principal,
)
from app.path_utils import validate_path_within_base
from app.project_activity import append_project_change, project_change_view
from app.project_retention import PROJECT_LIFECYCLE_LOCK
from app.timeutil import now_naive_utc, SOFT_DELETE_TTL


router = APIRouter(prefix="/api/projects", tags=["projects"])
log = logging.getLogger(__name__)

_WRITE_MAX = 10 * 1024 * 1024
# Tail window returned by the build-log endpoint. The on-disk log is bounded
# separately by project_builders; this caps what a single read returns.
_LOG_TAIL_MAX = 64 * 1024
_LEGACY_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROJECT_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_GITHUB_REPO_RE = re.compile(
  r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
  r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)
_PROJECT_ROLES = {"viewer": 0, "editor": 1, "maintainer": 2, "owner": 3}
_PRESENCE_WINDOW = timedelta(seconds=75)
_INVITE_TTL = timedelta(days=7)
_WORK_CLAIM_TTL = timedelta(seconds=75)
_AGENT_WORK_CLAIM_TTL = timedelta(minutes=30)

_FILE_LOCKS_GUARD = threading.Lock()
_FILE_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = (
  weakref.WeakValueDictionary()
)


class ProjectCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  name: str = Field(default="Untitled project", min_length=1, max_length=256)
  template_id: str = Field(default="blank", min_length=1, max_length=128)
  recovery_request_id: str | None = Field(default=None, min_length=1, max_length=128)

  @field_validator("name")
  @classmethod
  def clean_name(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("name must not be blank")
    return value


class ProjectPatch(BaseModel):
  model_config = ConfigDict(extra="forbid")

  name: str | None = Field(default=None, min_length=1, max_length=256)
  color: str | None = Field(default=None, max_length=7)
  pinned: bool | None = None

  @field_validator("name")
  @classmethod
  def clean_name(cls, value: str | None) -> str | None:
    if value is None:
      return None
    value = value.strip()
    if not value:
      raise ValueError("name must not be blank")
    return value

  @field_validator("color")
  @classmethod
  def clean_color(cls, value: str | None) -> str | None:
    if value is None:
      return None
    value = value.strip().lower()
    if not _PROJECT_COLOR_RE.fullmatch(value):
      raise ValueError("color must be a six-digit hex color")
    return value


class GitHubImport(BaseModel):
  model_config = ConfigDict(extra="forbid")

  repository: str = Field(min_length=3, max_length=300)
  name: str | None = Field(default=None, max_length=256)
  recovery_request_id: str | None = Field(default=None, min_length=1, max_length=128)

  @field_validator("repository")
  @classmethod
  def clean_repository(cls, value: str) -> str:
    value = value.strip()
    value = re.sub(r"^https://github\.com/", "", value, flags=re.IGNORECASE)
    value = value.removesuffix(".git").strip("/")
    if not _GITHUB_REPO_RE.fullmatch(value):
      raise ValueError("repository must be owner/name or a GitHub URL")
    return value

  @field_validator("name")
  @classmethod
  def clean_import_name(cls, value: str | None) -> str | None:
    if value is None:
      return None
    value = value.strip()
    if not value:
      raise ValueError("name must not be blank")
    return value


class ProjectCommit(BaseModel):
  model_config = ConfigDict(extra="forbid")

  message: str = Field(min_length=1, max_length=500)
  expected_head: str | None = Field(default=None, min_length=1, max_length=64)

  @field_validator("message")
  @classmethod
  def clean_message(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("message must not be blank")
    return value


class ProjectRemoteConnect(BaseModel):
  model_config = ConfigDict(extra="forbid")

  repository: str = Field(min_length=3, max_length=200)

  @field_validator("repository")
  @classmethod
  def clean_repository(cls, value: str) -> str:
    value = value.strip()
    value = re.sub(r"^https://github\.com/", "", value, flags=re.IGNORECASE)
    return value.removesuffix(".git").strip("/")


class ProjectRemoteAction(BaseModel):
  model_config = ConfigDict(extra="forbid")

  expected_head: str | None = Field(default=None, min_length=1, max_length=64)


class ProjectRemotePush(ProjectRemoteAction):
  confirmed: bool = False


class ProjectChatCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  title: str = Field(default="New chat", min_length=1, max_length=256)
  recovery_request_id: str | None = Field(default=None, min_length=1, max_length=128)

  @field_validator("title")
  @classmethod
  def clean_title(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("title must not be blank")
    return value


class ProjectAgentMessageCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  sender_chat_id: str = Field(min_length=1, max_length=64)
  recipients: list[str] = Field(default_factory=list, max_length=12)
  broadcast: bool = False
  body: str = Field(min_length=1, max_length=4000)

  @field_validator("body")
  @classmethod
  def clean_body(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("body must not be blank")
    return value


class ProjectInviteCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  invitee_name: str | None = Field(default=None, max_length=128)
  role: str = Field(default="editor", min_length=1, max_length=16)

  @field_validator("invitee_name")
  @classmethod
  def clean_invitee_name(cls, value: str | None) -> str | None:
    value = value.strip() if value is not None else None
    return value or None

  @field_validator("role")
  @classmethod
  def valid_role(cls, value: str) -> str:
    value = value.strip().lower()
    if value not in {"viewer", "editor", "maintainer"}:
      raise ValueError("role must be viewer, editor, or maintainer")
    return value


class ProjectInviteRedeem(BaseModel):
  model_config = ConfigDict(extra="forbid")

  invite: str = Field(min_length=20, max_length=256)
  display_name: str = Field(min_length=1, max_length=128)

  @field_validator("display_name")
  @classmethod
  def clean_display_name(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("display name must not be blank")
    return value


class ProjectMemberPatch(BaseModel):
  model_config = ConfigDict(extra="forbid")

  role: str = Field(min_length=1, max_length=16)

  @field_validator("role")
  @classmethod
  def valid_role(cls, value: str) -> str:
    value = value.strip().lower()
    if value not in {"viewer", "editor", "maintainer"}:
      raise ValueError("role must be viewer, editor, or maintainer")
    return value


class LegacyImport(BaseModel):
  model_config = ConfigDict(extra="forbid")

  app_id: int = Field(gt=0)
  legacy_project_id: str = Field(min_length=1, max_length=64)
  name: str | None = Field(default=None, max_length=256)

  @field_validator("legacy_project_id")
  @classmethod
  def valid_legacy_id(cls, value: str) -> str:
    if value != "default" and not _LEGACY_PROJECT_ID_RE.fullmatch(value):
      raise ValueError("legacy_project_id must be `default` or a project slug")
    return value


class FileWrite(BaseModel):
  model_config = ConfigDict(extra="forbid")

  content: str = Field(max_length=_WRITE_MAX)
  # Normal writes must send this field: null means "create only if absent" and
  # a digest means "replace exactly the revision I opened." Owner automation
  # that truly intends to discard a concurrent revision must opt into force.
  expected_revision: str | None = Field(default=None, max_length=64)

  @field_validator("expected_revision")
  @classmethod
  def valid_revision(cls, value: str | None) -> str | None:
    if value is None:
      return None
    value = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
      raise ValueError("expected_revision must be a SHA-256 digest")
    return value


class ProjectWorkClaimWrite(BaseModel):
  model_config = ConfigDict(extra="forbid")

  path: str | None = Field(default=None, max_length=2048)
  summary: str = Field(min_length=1, max_length=300)
  chat_id: str | None = Field(default=None, min_length=1, max_length=64)

  @field_validator("path")
  @classmethod
  def clean_path(cls, value: str | None) -> str | None:
    value = value.strip().lstrip("/") if value is not None else None
    return value or None

  @field_validator("summary")
  @classmethod
  def clean_summary(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("summary must not be blank")
    return value


class FolderCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  path: str = Field(min_length=1, max_length=2048)


class PathMove(BaseModel):
  model_config = ConfigDict(extra="forbid")

  from_path: str = Field(min_length=1, max_length=2048)
  to_path: str = Field(min_length=1, max_length=2048)


class ArtifactCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")

  name: str = Field(min_length=1, max_length=256)
  builder: str = Field(min_length=1, max_length=64)
  source: str = Field(min_length=1, max_length=2048)
  # Optional caller-chosen id; otherwise derived from the name. Validated
  # against the slug charset before it is used as a path component.
  id: str | None = Field(default=None, min_length=1, max_length=64)

  @field_validator("name")
  @classmethod
  def clean_name(cls, value: str) -> str:
    value = value.strip()
    if not value:
      raise ValueError("name must not be blank")
    return value


def _project_response(
  project: models.Project, chats: list[models.Chat] | None = None,
) -> dict[str, Any]:
  return {
    "id": project.id,
    "name": project.name,
    "color": project.color,
    "project_type": project.project_type,
    "chat_id": project.chat_id,
    "source_app_id": project.source_app_id,
    "template": project.template_snapshot_json or {},
    "legacy_source": project.legacy_source_json,
    "artifacts": _project_artifacts_view(project),
    "created_at": project.created_at,
    "updated_at": project.updated_at,
    "last_opened_at": getattr(project, "last_opened_at", None),
    "pinned_at": getattr(project, "pinned_at", None),
    "chats": [_project_chat_response(chat) for chat in (chats or [])],
  }


def _project_chat_response(chat: models.Chat) -> dict[str, Any]:
  return {
    "id": chat.id,
    "title": chat.title,
    "has_messages": bool(chat.has_messages),
    "provider": chat.provider,
    "created_at": chat.created_at,
    "updated_at": chat.updated_at,
    "activity_at": chat.activity_at,
  }


def _project_agent_response(db: Session, chat: models.Chat) -> dict[str, Any]:
  run = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == chat.id,
  ).order_by(models.ChatRun.started_at.desc()).first()
  return {
    **_project_chat_response(chat),
    "run": ({
      "id": run.id,
      "status": run.status,
      "provider": run.provider,
      "goal": run.goal_objective,
      "summary": run.goal_objective,
      "started_at": run.started_at,
      "ended_at": run.ended_at,
    } if run is not None else None),
  }


def _project_agent_message_response(row: models.ProjectAgentMessage) -> dict[str, Any]:
  return {
    "id": row.id,
    "project_id": row.project_id,
    "sender_chat_id": row.from_chat_id,
    "recipient_chat_id": row.to_chat_id,
    "broadcast": row.to_chat_id is None,
    "body": row.body,
    "created_at": row.created_at,
  }


def _live_project_chat_rows(db: Session, project_id: str) -> list[models.Chat]:
  return db.query(models.Chat).filter(
    models.Chat.project_id == project_id,
    models.Chat.deleted_at.is_(None),
  ).order_by(
    models.Chat.activity_at.desc(),
    models.Chat.updated_at.desc(),
    models.Chat.created_at.desc(),
  ).all()


def _live_project(db: Session, project_id: str) -> models.Project:
  project = db.query(models.Project).filter(
    models.Project.id == project_id,
    models.Project.deleted_at.is_(None),
  ).first()
  if project is None:
    raise HTTPException(404, "Project not found.")
  return project


def _project_for(
  db: Session,
  project_id: str,
  principal: ProjectPrincipal,
  minimum_role: str,
) -> models.Project:
  """Load one live Project and enforce its role at the owning boundary."""
  project = _live_project(db, project_id)
  if principal.is_owner:
    return project
  if principal.project_id != project.id:
    # A scoped collaborator must not learn whether another project exists.
    raise HTTPException(404, "Project not found.")
  if _PROJECT_ROLES.get(principal.role, -1) < _PROJECT_ROLES[minimum_role]:
    raise HTTPException(403, f"{minimum_role.capitalize()} access is required.")
  return project


def _invite_hash(secret: str) -> str:
  return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _presence_actor(principal: ProjectPrincipal) -> str:
  return f"member:{principal.member_id}" if principal.member_id else "owner"


def _claim_view(row: models.ProjectWorkClaim) -> dict[str, Any]:
  return {
    "id": row.id,
    "actor_key": row.actor_key,
    "actor_kind": row.actor_kind,
    "display_name": row.display_name,
    "chat_id": row.chat_id,
    "path": row.path,
    "summary": row.summary,
    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
  }


def _change_view(row: models.ProjectChange) -> dict[str, Any]:
  return project_change_view(row)


def _change_actor(principal: ProjectPrincipal) -> tuple[str, str]:
  return (
    _presence_actor(principal),
    principal.display_name or principal.owner.username or "Collaborator",
  )


def _record_project_change(
  db: Session,
  project: models.Project,
  principal: ProjectPrincipal,
  *,
  kind: str,
  path: str | None,
  prior_path: str | None = None,
  revision: str | None = None,
) -> models.ProjectChange:
  actor_key, display_name = _change_actor(principal)
  return append_project_change(
    db,
    project_id=project.id,
    kind=kind,
    path=path,
    prior_path=prior_path,
    revision=revision,
    actor_key=actor_key,
    display_name=display_name,
  )


def _publish_project_change(project_id: str, row: models.ProjectChange) -> None:
  get_system_broadcast().publish({
    "type": "project_file_changed",
    "projectId": str(project_id),
    "change": _change_view(row),
  })


def _project_collaboration_view(
  db: Session, project: models.Project, principal: ProjectPrincipal,
) -> dict[str, Any]:
  now = now_naive_utc()
  online_since = now - _PRESENCE_WINDOW
  active_presence = {
    row.actor_key: row.last_seen_at
    for row in db.query(models.ProjectPresence).filter(
      models.ProjectPresence.project_id == project.id,
      models.ProjectPresence.last_seen_at >= online_since,
    ).all()
  }
  members = [{
    "id": "owner",
    "display_name": principal.owner.username,
    "role": "owner",
    "joined_at": project.created_at,
    "online": "owner" in active_presence,
    "you": principal.is_owner,
  }]
  for member in db.query(models.ProjectMember).filter(
    models.ProjectMember.project_id == project.id,
    models.ProjectMember.revoked_at.is_(None),
  ).order_by(models.ProjectMember.joined_at.asc()).all():
    members.append({
      "id": member.id,
      "display_name": member.display_name,
      "role": member.role,
      "joined_at": member.joined_at,
      "online": f"member:{member.id}" in active_presence,
      "you": member.id == principal.member_id,
    })
  payload: dict[str, Any] = {
    "role": principal.role,
    "member_id": principal.member_id,
    "members": members,
  }
  if principal.is_owner:
    payload["invites"] = [{
      "id": invite.id,
      "invitee_name": invite.invitee_name,
      "role": invite.role,
      "created_at": invite.created_at,
      "expires_at": invite.expires_at,
    } for invite in db.query(models.ProjectInvite).filter(
      models.ProjectInvite.project_id == project.id,
      models.ProjectInvite.revoked_at.is_(None),
      models.ProjectInvite.consumed_at.is_(None),
      models.ProjectInvite.expires_at > now,
    ).order_by(models.ProjectInvite.created_at.desc()).all()]
  return payload


def _project_root(project: models.Project) -> Path:
  data_root = Path(get_settings().data_dir).resolve()
  stored = Path(project.root_path)
  # Absolute values are accepted only as a rolling-upgrade compatibility path;
  # all new rows store a logical locator so moving the data volume preserves it.
  root = (stored if stored.is_absolute() else data_root / stored).resolve()
  try:
    root.relative_to(data_root)
  except ValueError as exc:
    raise HTTPException(500, "Project root is outside the data directory.") from exc
  return root


def _resolve_project_path(project: models.Project, path: str) -> tuple[Path, Path]:
  root = _project_root(project)
  try:
    target = workspace_files.resolve_path(
      root, path, hidden_dirs=workspace_files.GIT_METADATA_NAMES,
    )
  except workspace_files.InvalidWorkspacePath as exc:
    raise HTTPException(400, "Invalid project path.") from exc
  except workspace_files.UnavailableWorkspacePath as exc:
    raise HTTPException(403, "Path is not available in Projects.")
  return root, target


@contextmanager
def _locked_project_mutation(root: Path):
  """Serialize saves, moves, and deletes for one project in this process.

  A path-only lock cannot protect a child save while its parent is moving. The
  project-wide critical section gives all mutators one coherent order.
  """
  key = str(root)
  with _FILE_LOCKS_GUARD:
    lock = _FILE_LOCKS.get(key)
    if lock is None:
      lock = threading.Lock()
      _FILE_LOCKS[key] = lock
  with lock:
    yield


def _require_expected_revision(
  target: Path,
  path: str,
  expected_revision: str | None,
) -> None:
  current_revision = workspace_files.file_revision(target)
  if current_revision == expected_revision:
    return
  raise HTTPException(status_code=409, detail={
    "code": "file_revision_conflict",
    "message": "This file changed elsewhere. Your draft was not overwritten.",
    "path": path,
    "expected_revision": expected_revision,
    "current_revision": current_revision,
  })


def _artifacts_root(root: Path) -> Path:
  """The reserved build-output area under a project root."""
  return (root / "artifacts").resolve()


def _within_artifacts(root: Path, path: Path) -> bool:
  """Whether a resolved path is the reserved artifacts area or inside it."""
  return path.resolve().is_relative_to(_artifacts_root(root))


def _reject_artifacts_mutation(root: Path, path: Path) -> None:
  """Keep generated artifact output behind the artifact lifecycle API."""
  if _within_artifacts(root, path):
    raise HTTPException(409, "The artifacts area is managed by builds.")


def _artifact_view(
  project: models.Project, root: Path, entry: dict[str, Any],
) -> dict[str, Any]:
  """Owner-facing artifact row: registry fields plus reconciled/derived state.

  Lenient by construction — a hand-edited entry with a missing source or an
  unknown builder surfaces ``source_missing`` / a null builder rather than
  raising. ``status`` is reconciled against the live task registry so a stale
  ``building`` reads as ``error`` and a queued build reads as ``building``.
  """
  artifact_id = entry.get("id")
  builder = entry.get("builder")
  source = entry.get("source")
  artifact_type = project_builders.resolve_artifact_type(project, str(builder))
  output_rel = entry.get("output_rel")
  if not (isinstance(output_rel, str) and output_rel):
    output_rel = project_builders.default_output_rel(
      str(artifact_id), str(builder), str(source or ""), artifact_type,
    )
  log_rel = entry.get("log_rel")
  if not (isinstance(log_rel, str) and log_rel):
    log_rel = project_builders.default_log_rel(str(artifact_id))
  source_exists = False
  if isinstance(source, str) and source:
    try:
      _source_root, source_path = _resolve_project_path(project, source)
      source_exists = source_path.is_file()
    except HTTPException:
      source_exists = False
  has_output = False
  try:
    output_path = (root / output_rel.lstrip("/")).resolve()
    if _within_artifacts(root, output_path) and output_path.is_file():
      has_output = True
  except (OSError, ValueError):
    has_output = False
  return {
    "id": artifact_id,
    "name": entry.get("name") or artifact_id,
    "builder": builder if artifact_type is not None else None,
    "type_name": (
      artifact_type.get("name") if artifact_type else entry.get("type_name")
    ),
    "preview": (
      artifact_type.get("preview") if artifact_type else entry.get("preview")
    ),
    "source": source,
    "output_rel": output_rel,
    "log_rel": log_rel,
    "status": project_builders.effective_status(project.id, entry),
    "updated_at": entry.get("updated_at"),
    "duration_ms": entry.get("duration_ms"),
    "has_output": has_output,
    "source_missing": not source_exists,
  }


def _project_artifacts_view(project: models.Project) -> list[dict[str, Any]]:
  """Artifact rows for a project payload; never raises into the response."""
  try:
    root = _project_root(project)
  except HTTPException:
    return []
  return [
    _artifact_view(project, root, entry)
    for entry in project_builders.read_artifacts(project)
  ]


def _previews_to_artifacts(
  snapshot: dict, root: Path,
) -> list[dict[str, Any]]:
  """Map a template's website/latex previews to buildable artifact entries.

  A preview declares its OUTPUT path and kind; the artifact needs the SOURCE.
  A pdf preview (``main.pdf``) builds from the matching ``.tex`` source; an
  html preview's path is both source and output entry. A preview is registered
  only when its mapped source file actually exists in the freshly scaffolded
  project, so no artifact points at a file that was never copied.
  """
  artifacts: list[dict[str, Any]] = []
  seen: set[str] = set()
  for preview in snapshot.get("previews") or []:
    if not isinstance(preview, dict):
      continue
    kind = str(preview.get("kind") or "").lower()
    output_path = str(preview.get("path") or "").lstrip("/")
    if not output_path:
      continue
    if kind == "pdf":
      source = Path(output_path).with_suffix(".tex").as_posix()
    elif kind in ("html", "website"):
      source = output_path
      kind = "html"
    else:
      continue
    artifact_type = project_builders.artifact_type_for_source(
      snapshot, source, preview=kind,
    )
    if artifact_type is None:
      continue
    builder = artifact_type["id"]
    if not (root / source).is_file():
      continue
    artifact_id = project_builders.slug_artifact_id(
      str(preview.get("id") or preview.get("name") or builder),
    )
    if not project_builders.ARTIFACT_ID_RE.match(artifact_id) or artifact_id in seen:
      continue
    seen.add(artifact_id)
    artifacts.append(project_builders.new_artifact_entry(
      artifact_id,
      str(preview.get("name") or artifact_id),
      builder,
      source,
      artifact_type,
    ))
  return artifacts


def _resolve_output_principal(
  request: Request,
  db: Session = Depends(get_db),
) -> ProjectPrincipal:
  """Header-only auth for a confined artifact-output GET.

  The shell always fetches artifact output through the owner's Bearer header:
  pdfjs for a latex document, and for a website the shell fetches the built
  files and inlines them into a sandboxed ``srcDoc``. The owner token is never
  carried on the URL, so a sandboxed artifact's JS cannot read it from
  ``window.location``. App-scoped tokens are rejected (owner-only).
  """
  authorization = request.headers.get("Authorization", "")
  scheme, _, header_token = authorization.partition(" ")
  if scheme.lower() != "bearer" or not header_token:
    raise HTTPException(401, "Not authenticated.")
  return resolve_project_principal(header_token, db)


def _template_key(template: dict, app: models.App | None) -> str:
  if app is None:
    return "blank"
  return f"{app.slug}:{template.get('id')}"


def _safe_template(template: dict, app: models.App | None = None) -> dict:
  return {
    "key": _template_key(template, app),
    "id": str(template.get("id") or "blank"),
    "name": str(template.get("name") or "Blank project"),
    "description": str(template.get("description") or ""),
    "guidance": str(template.get("guidance") or ""),
    "skills": [str(value) for value in template.get("skills") or []],
    "dependencies": [str(value) for value in template.get("dependencies") or []],
    "previews": [
      {
        "id": str(value.get("id") or "preview"),
        "name": str(value.get("name") or "Preview"),
        "kind": str(value.get("kind") or "html"),
        "path": str(value.get("path") or ""),
      }
      for value in template.get("previews") or []
      if isinstance(value, dict)
    ],
    "actions": [
      {
        "id": str(value.get("id") or "action"),
        "name": str(value.get("name") or "Run"),
        "prompt": str(value.get("prompt") or ""),
      }
      for value in template.get("actions") or []
      if isinstance(value, dict)
    ],
    "artifact_types": [
      {
        "id": str(value.get("id") or "artifact"),
        "name": str(value.get("name") or "Artifact"),
        "extensions": [
          str(extension) for extension in value.get("extensions") or []
        ],
        "preview": str(value.get("preview") or "html"),
        "script": str(value.get("script") or ""),
        "output": str(value.get("output") or "{source}"),
      }
      for value in template.get("artifact_types") or []
      if isinstance(value, dict)
    ],
    "files": dict(template.get("files") or {}),
    "source_app_id": app.id if app is not None else None,
    "source_app_name": app.name if app is not None else None,
    "source_app_version": app.version if app is not None else None,
  }


def _templates(db: Session) -> list[tuple[dict, models.App | None]]:
  rows: list[tuple[dict, models.App | None]] = [({
    "id": "blank",
    "name": "Blank project",
    "description": "Start with an empty folder.",
    "guidance": "Work only inside this project's root unless the user asks otherwise.",
    "skills": [],
    "dependencies": [],
    "files": {},
  }, None)]
  apps = db.query(models.App).options(
    defer(models.App.jsx_source),
    defer(models.App.icon_png),
    defer(models.App.icon_override_png),
  ).filter(
    models.App.deleted_at.is_(None),
    models.App.project_templates_json.isnot(None),
  ).order_by(models.App.name, models.App.id).all()
  for app in apps:
    for template in app.project_templates_json or []:
      if isinstance(template, dict):
        rows.append((template, app))
  return rows


def _template_by_id(db: Session, template_id: str) -> tuple[dict, models.App | None]:
  matches = [row for row in _templates(db) if _template_key(*row) == template_id]
  if not matches:
    raise HTTPException(422, "That project type is not installed.")
  return matches[0]


def _new_chat(
  db: Session, *, chat_id: str, title: str, owner: models.Owner,
  project_id: str | None = None,
) -> models.Chat:
  # Provider follows the last-selected model (the single source of truth),
  # matching owner chat creation.
  provider = providers.owner_default_provider(
    get_settings().data_dir, owner.provider if owner else None,
  )
  return models.Chat(
    id=chat_id,
    title=title,
    messages=[],
    provider=provider,
    agent_settings_json=None,
    auto_resume_on_limit=bool(owner.auto_resume_on_limit_default),
    # Restart continuation is an always-on platform invariant. The retired
    # owner toggle must not leak back in through project-created chats.
    auto_resume_on_restart=True,
    project_id=project_id,
  )


def _copy_template_files(root: Path, template: dict, app: models.App | None) -> None:
  files = template.get("files") or {}
  if not files:
    return
  if app is None:
    raise HTTPException(422, "Blank projects cannot declare template files.")
  app_root = Path(app.source_dir).resolve()
  for destination, source in files.items():
    source_path = validate_path_within_base(source, app_root)
    if not source_path.is_file() or source_path.is_symlink():
      raise HTTPException(409, f"Project template file is unavailable: {source}")
    target = validate_path_within_base(destination, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target)


@router.get("/templates")
def list_project_templates(
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  return [_safe_template(template, app) for template, app in _templates(db)]


@router.get("")
def list_projects(
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  rows = db.query(models.Project).filter(
    models.Project.deleted_at.is_(None),
  ).order_by(models.Project.updated_at.desc(), models.Project.created_at.desc()).all()
  project_ids = [row.id for row in rows]
  chats_by_project: dict[str, list[models.Chat]] = {
    project_id: [] for project_id in project_ids
  }
  if project_ids:
    chat_rows = db.query(models.Chat).filter(
      models.Chat.project_id.in_(project_ids),
      models.Chat.deleted_at.is_(None),
    ).order_by(
      models.Chat.activity_at.desc(),
      models.Chat.updated_at.desc(),
      models.Chat.created_at.desc(),
    ).all()
    for chat in chat_rows:
      chats_by_project.setdefault(str(chat.project_id), []).append(chat)
  project_drawer.annotate_projects(db, rows)
  return [
    _project_response(row, chats_by_project.get(str(row.id), [])) for row in rows
  ]


@router.post("", dependencies=[Depends(reject_cross_site)])
def create_project(
  body: ProjectCreate,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  with PROJECT_LIFECYCLE_LOCK:
    template, app = _template_by_id(db, body.template_id)
    if body.recovery_request_id:
      project_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"mobius:project:{body.recovery_request_id}",
      ))
      existing = db.query(models.Project).filter(models.Project.id == project_id).first()
      if existing is not None:
        if existing.deleted_at is not None:
          raise HTTPException(409, "Project was deleted.")
        return _project_response(existing)
    else:
      project_id = str(uuid.uuid4())

    root_locator = Path("projects") / project_id
    root = (Path(get_settings().data_dir) / root_locator).resolve()
    if root.exists():
      raise HTTPException(409, "Project root already exists.")
    root.mkdir(parents=True)
    try:
      _copy_template_files(root, template, app)
      snapshot = _safe_template(template, app)
      # A template's website/latex previews become buildable artifacts up
      # front, so a new project is never left half-wired: the file grid, the
      # artifact list, and the build button all reference the same registry.
      artifacts = _previews_to_artifacts(snapshot, root)
      project = models.Project(
        id=project_id,
        name=body.name,
        project_type=_template_key(template, app),
        root_path=root_locator.as_posix(),
        chat_id=None,
        source_app_id=app.id if app is not None else None,
        template_snapshot_json=snapshot,
        artifacts_json=artifacts or None,
      )
      db.add(project)
      db.commit()
    except Exception:
      db.rollback()
      shutil.rmtree(root, ignore_errors=True)
      raise
  db.refresh(project)
  return _project_response(project)


@router.post("/import-github", dependencies=[Depends(reject_cross_site)])
def import_github_project(
  body: GitHubImport,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Clone one GitHub repository into a new project-owned workspace."""
  token = github_auth.get_token()
  if not token:
    raise HTTPException(409, "Connect GitHub before importing a repository.")
  with PROJECT_LIFECYCLE_LOCK:
    if body.recovery_request_id:
      project_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"mobius:github-project:{body.recovery_request_id}",
      ))
      existing = db.query(models.Project).filter(models.Project.id == project_id).first()
      if existing is not None:
        if existing.deleted_at is not None:
          raise HTTPException(409, "Project was deleted.")
        return _project_response(existing, _live_project_chat_rows(db, existing.id))
    else:
      project_id = str(uuid.uuid4())

    root_locator = Path("projects") / project_id
    root = (Path(get_settings().data_dir) / root_locator).resolve()
    if root.exists():
      raise HTTPException(409, "Project root already exists.")
    env = {
      **os.environ,
      "GH_TOKEN": token,
      "GIT_TERMINAL_PROMPT": "0",
      "LC_ALL": "C",
    }
    try:
      result = subprocess.run(
        ["gh", "repo", "clone", body.repository, str(root), "--", "--depth=1"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
      )
    except (OSError, subprocess.TimeoutExpired) as exc:
      shutil.rmtree(root, ignore_errors=True)
      raise HTTPException(502, "GitHub import did not finish in time.") from exc
    if result.returncode != 0:
      shutil.rmtree(root, ignore_errors=True)
      lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
      detail = lines[-1][:300] if lines else "GitHub could not clone that repository."
      raise HTTPException(502, detail)

    repository_url = f"https://github.com/{body.repository}"
    snapshot: dict[str, Any] = {
      "key": "github:repository",
      "id": "repository",
      "name": "GitHub repository",
      "description": f"Imported from {body.repository}",
      "guidance": (
        "Work inside this project root. Preview and test changes locally. "
        "The owner controls commits and any later publication."
      ),
      "skills": [],
      "dependencies": [],
      "previews": [],
      "actions": [],
      "artifact_types": [],
      "files": {},
      "repository": {"slug": body.repository, "url": repository_url},
      "source_app_id": None,
      "source_app_name": None,
      "source_app_version": None,
    }
    if (root / "index.html").is_file():
      snapshot["previews"] = [{
        "id": "website", "name": "Website", "kind": "html", "path": "index.html",
      }]
      snapshot["artifact_types"] = [{
        "id": "website", "name": "Website", "extensions": ["html", "htm"],
        "preview": "html", "script": "", "output": "{source}",
      }]
    else:
      top_level_tex = next(iter(sorted(root.glob("*.tex"))), None)
      if top_level_tex is not None:
        snapshot["previews"] = [{
          "id": "document", "name": "Document", "kind": "pdf",
          "path": top_level_tex.name,
        }]
        snapshot["artifact_types"] = [{
          "id": "latex", "name": "PDF", "extensions": ["tex"],
          "preview": "pdf", "script": "", "output": "{stem}.pdf",
        }]
    try:
      artifacts = _previews_to_artifacts(snapshot, root)
      project = models.Project(
        id=project_id,
        name=body.name or body.repository.split("/", 1)[1],
        project_type="github:repository",
        root_path=root_locator.as_posix(),
        chat_id=None,
        source_app_id=None,
        template_snapshot_json=snapshot,
        artifacts_json=artifacts or None,
      )
      db.add(project)
      db.commit()
    except Exception:
      db.rollback()
      shutil.rmtree(root, ignore_errors=True)
      raise
  db.refresh(project)
  return _project_response(project)


def _legacy_storage_root(app: models.App, legacy_id: str) -> Path:
  data_root = Path(get_settings().data_dir).resolve()
  app_storage = (data_root / "apps" / str(app.id)).resolve()
  base = app_storage if legacy_id == "default" else app_storage / "projects" / legacy_id
  root = (base / "files").resolve()
  try:
    root.relative_to(app_storage)
  except ValueError as exc:
    raise HTTPException(400, "Invalid legacy project root.") from exc
  return root


def _read_legacy_projects(app: models.App) -> list[dict]:
  storage = Path(get_settings().data_dir) / "apps" / str(app.id)
  metadata: dict[str, str] = {}
  try:
    raw = json.loads((storage / "projects.json").read_text(encoding="utf-8"))
    if isinstance(raw, list):
      for row in raw:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
          metadata[row["id"]] = str(row.get("name") or row["id"])
  except (OSError, ValueError, TypeError):
    pass
  ids = set(metadata)
  if (storage / "files").is_dir():
    ids.add("default")
  projects_dir = storage / "projects"
  if projects_dir.is_dir():
    for child in projects_dir.iterdir():
      if child.is_dir() and not child.is_symlink() and _LEGACY_PROJECT_ID_RE.fullmatch(child.name):
        ids.add(child.name)
  return [
    {"legacy_project_id": project_id, "name": metadata.get(project_id) or (
      "Default project" if project_id == "default" else project_id.replace("-", " ").title()
    )}
    for project_id in sorted(ids, key=lambda value: (value != "default", value))
    if _legacy_storage_root(app, project_id).is_dir()
  ]


@router.get("/legacy")
def list_legacy_projects(
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  imported = {
    (int(source.get("app_id")), str(source.get("project_id")))
    for (source,) in db.query(models.Project.legacy_source_json).filter(
      models.Project.legacy_source_json.isnot(None),
    ).all()
    if isinstance(source, dict) and source.get("app_id") is not None
  }
  out = []
  apps = db.query(models.App).filter(
    models.App.deleted_at.is_(None),
    models.App.slug.in_(("latex", "webstudio")),
  ).order_by(models.App.name).all()
  for app in apps:
    for row in _read_legacy_projects(app):
      key = (app.id, row["legacy_project_id"])
      out.append({
        **row,
        "app_id": app.id,
        "app_name": app.name,
        "imported": key in imported,
      })
  return out


def _legacy_chat_id(app: models.App, legacy_id: str, db: Session) -> str | None:
  storage = Path(get_settings().data_dir) / "apps" / str(app.id)
  base = storage if legacy_id == "default" else storage / "projects" / legacy_id
  try:
    raw = json.loads((base / "chat_id.json").read_text(encoding="utf-8"))
  except (OSError, ValueError, TypeError):
    return None
  value = raw.get("id") if isinstance(raw, dict) else None
  if not isinstance(value, str):
    return None
  chat = db.query(models.Chat).filter(
    models.Chat.id == value,
    models.Chat.deleted_at.is_(None),
  ).first()
  if chat is None:
    return None
  linked = db.query(models.Project.id).filter(
    (models.Project.chat_id == value)
    | (models.Project.id == chat.project_id)
  ).first()
  return None if linked else value


@router.post("/import-legacy", dependencies=[Depends(reject_cross_site)])
def import_legacy_project(
  body: LegacyImport,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  app = db.query(models.App).filter(
    models.App.id == body.app_id,
    models.App.deleted_at.is_(None),
  ).first()
  if app is None or app.slug not in ("latex", "webstudio"):
    raise HTTPException(404, "Compatible legacy app not found.")
  root = _legacy_storage_root(app, body.legacy_project_id)
  if not root.is_dir():
    raise HTTPException(404, "Legacy project files were not found.")
  for project in db.query(models.Project).filter(
    models.Project.legacy_source_json.isnot(None),
  ).all():
    source = project.legacy_source_json or {}
    if source.get("app_id") == app.id and source.get("project_id") == body.legacy_project_id:
      if project.deleted_at is not None:
        raise HTTPException(409, "This imported project is in recovery.")
      return _project_response(project, _live_project_chat_rows(db, project.id))

  legacy_rows = _read_legacy_projects(app)
  legacy = next(
    (row for row in legacy_rows if row["legacy_project_id"] == body.legacy_project_id),
    None,
  )
  name = (body.name or (legacy or {}).get("name") or body.legacy_project_id).strip()
  templates = app.project_templates_json or []
  template = next((row for row in templates if isinstance(row, dict)), {
    "id": app.slug,
    "name": app.name,
    "description": app.description,
    "skills": [],
    "dependencies": [],
    "files": {},
  })
  snapshot = _safe_template(template, app)
  artifacts = _previews_to_artifacts(snapshot, root)
  project_id = str(uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"mobius:legacy-project:{app.id}:{body.legacy_project_id}",
  ))
  chat_id = _legacy_chat_id(app, body.legacy_project_id, db)
  legacy_chat = db.get(models.Chat, chat_id) if chat_id else None
  if legacy_chat is not None:
    legacy_chat.project_id = project_id
  project = models.Project(
    id=project_id,
    name=name,
    project_type=_template_key(template, app),
    root_path=root.relative_to(Path(get_settings().data_dir).resolve()).as_posix(),
    chat_id=None,
    source_app_id=app.id,
    template_snapshot_json=snapshot,
    artifacts_json=artifacts or None,
    legacy_source_json={
      "app_id": app.id,
      "project_id": body.legacy_project_id,
      "storage_root": root.parent.relative_to(
        Path(get_settings().data_dir).resolve(),
      ).as_posix(),
    },
  )
  db.add(project)
  try:
    db.commit()
  except IntegrityError:
    db.rollback()
    existing = db.query(models.Project).filter(models.Project.id == project_id).first()
    if existing is not None:
      return _project_response(existing, _live_project_chat_rows(db, existing.id))
    raise
  db.refresh(project)
  return _project_response(
    project, [legacy_chat] if legacy_chat is not None else [],
  )


@router.post(
  "/invites/redeem", dependencies=[Depends(reject_cross_site)],
)
def redeem_project_invite(
  body: ProjectInviteRedeem,
  db: Session = Depends(get_db),
):
  """Atomically spend an invitation and mint one project-only session."""
  now = now_naive_utc()
  invite = db.query(models.ProjectInvite).filter(
    models.ProjectInvite.token_hash == _invite_hash(body.invite),
    models.ProjectInvite.revoked_at.is_(None),
    models.ProjectInvite.consumed_at.is_(None),
    models.ProjectInvite.expires_at > now,
  ).first()
  if invite is None:
    raise HTTPException(410, "This project invitation is invalid or has expired.")
  project = _live_project(db, str(invite.project_id))
  member_id = str(uuid.uuid4())
  member = models.ProjectMember(
    id=member_id,
    project_id=project.id,
    display_name=body.display_name,
    role=invite.role,
    token_epoch=0,
    joined_at=now,
  )
  db.add(member)
  db.flush()
  claimed = db.query(models.ProjectInvite).filter(
    models.ProjectInvite.id == invite.id,
    models.ProjectInvite.revoked_at.is_(None),
    models.ProjectInvite.consumed_at.is_(None),
    models.ProjectInvite.expires_at > now,
  ).update({
    models.ProjectInvite.consumed_at: now,
    models.ProjectInvite.accepted_member_id: member_id,
  }, synchronize_session=False)
  if claimed != 1:
    db.rollback()
    raise HTTPException(410, "This project invitation has already been used.")
  try:
    db.commit()
  except IntegrityError as exc:
    db.rollback()
    raise HTTPException(409, "Project access could not be created.") from exc
  owner = db.query(models.Owner).first()
  if owner is None:
    raise HTTPException(503, "The project owner is unavailable.")
  token = auth.create_project_collaborator_token(
    owner_username=owner.username,
    owner_epoch=owner.token_epoch,
    project_id=str(project.id),
    member_id=member_id,
    member_epoch=0,
  )
  return {
    "access_token": token,
    "token_type": "bearer",
    "project": _project_response(project, []),
    "role": member.role,
    "member_id": member.id,
  }


@router.get("/{project_id}/collaboration")
def get_project_collaboration(
  project_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  return _project_collaboration_view(db, project, principal)


@router.post(
  "/{project_id}/presence", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def heartbeat_project_presence(
  project_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  actor_key = _presence_actor(principal)
  now = now_naive_utc()
  row = db.query(models.ProjectPresence).filter(
    models.ProjectPresence.project_id == project.id,
    models.ProjectPresence.actor_key == actor_key,
  ).first()
  if row is None:
    row = models.ProjectPresence(
      id=str(uuid.uuid4()), project_id=project.id, actor_key=actor_key,
      display_name=principal.display_name or "Collaborator",
      role=principal.role, last_seen_at=now,
    )
    db.add(row)
  else:
    row.display_name = principal.display_name or row.display_name
    row.role = principal.role
    row.last_seen_at = now
  db.query(models.ProjectPresence).filter(
    models.ProjectPresence.project_id == project.id,
    models.ProjectPresence.last_seen_at < now - timedelta(days=1),
  ).delete(synchronize_session=False)
  db.commit()
  return Response(status_code=204)


@router.get("/{project_id}/work-claims")
def list_project_work_claims(
  project_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  now = now_naive_utc()
  rows = db.query(models.ProjectWorkClaim).filter(
    models.ProjectWorkClaim.project_id == project.id,
    models.ProjectWorkClaim.expires_at > now,
  ).order_by(models.ProjectWorkClaim.updated_at.desc()).limit(24).all()
  return {"claims": [_claim_view(row) for row in rows]}


@router.put(
  "/{project_id}/work-claim", dependencies=[Depends(reject_cross_site)],
)
def put_project_work_claim(
  project_id: str,
  body: ProjectWorkClaimWrite,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  now = now_naive_utc()
  if body.chat_id is not None:
    if not principal.is_owner:
      raise HTTPException(403, "Only the owner can claim work for an agent.")
    chat = db.query(models.Chat).filter(
      models.Chat.id == body.chat_id,
      models.Chat.project_id == project.id,
      models.Chat.deleted_at.is_(None),
    ).first()
    if chat is None:
      raise HTTPException(404, "Project agent not found.")
    actor_key = f"agent:{chat.id}"
    actor_kind = "agent"
    display_name = chat.title or "Project agent"
    chat_id = str(chat.id)
  else:
    actor_key = _presence_actor(principal)
    actor_kind = "human"
    display_name = principal.display_name or principal.owner.username or "Collaborator"
    chat_id = None
    presence = db.query(models.ProjectPresence).filter(
      models.ProjectPresence.project_id == project.id,
      models.ProjectPresence.actor_key == actor_key,
    ).first()
    if presence is None:
      db.add(models.ProjectPresence(
        id=str(uuid.uuid4()), project_id=project.id, actor_key=actor_key,
        display_name=display_name, role=principal.role, last_seen_at=now,
      ))
    else:
      presence.display_name = display_name
      presence.role = principal.role
      presence.last_seen_at = now
  row = db.query(models.ProjectWorkClaim).filter(
    models.ProjectWorkClaim.project_id == project.id,
    models.ProjectWorkClaim.actor_key == actor_key,
  ).first()
  if row is None:
    row = models.ProjectWorkClaim(
      id=str(uuid.uuid4()), project_id=project.id, actor_key=actor_key,
      actor_kind=actor_kind, display_name=display_name, chat_id=chat_id,
      path=body.path, summary=body.summary, updated_at=now,
      expires_at=now + (
        _AGENT_WORK_CLAIM_TTL if actor_kind == "agent" else _WORK_CLAIM_TTL
      ),
    )
    db.add(row)
  else:
    row.actor_kind = actor_kind
    row.display_name = display_name
    row.chat_id = chat_id
    row.path = body.path
    row.summary = body.summary
    row.updated_at = now
    row.expires_at = now + (
      _AGENT_WORK_CLAIM_TTL if actor_kind == "agent" else _WORK_CLAIM_TTL
    )
  db.query(models.ProjectWorkClaim).filter(
    models.ProjectWorkClaim.project_id == project.id,
    models.ProjectWorkClaim.actor_key != actor_key,
    models.ProjectWorkClaim.expires_at <= now,
  ).delete(synchronize_session=False)
  db.commit()
  db.refresh(row)
  get_system_broadcast().publish({
    "type": "project_work_claim_changed",
    "projectId": str(project.id),
  })
  return _claim_view(row)


@router.delete(
  "/{project_id}/work-claim", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def delete_project_work_claim(
  project_id: str,
  chat_id: str | None = Query(default=None, max_length=64),
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  if chat_id is not None:
    if not principal.is_owner:
      raise HTTPException(403, "Only the owner can release an agent claim.")
    actor_key = f"agent:{chat_id}"
  else:
    actor_key = _presence_actor(principal)
  db.query(models.ProjectWorkClaim).filter(
    models.ProjectWorkClaim.project_id == project.id,
    models.ProjectWorkClaim.actor_key == actor_key,
  ).delete(synchronize_session=False)
  db.commit()
  get_system_broadcast().publish({
    "type": "project_work_claim_changed",
    "projectId": str(project.id),
  })
  return Response(status_code=204)


@router.post(
  "/{project_id}/invites", dependencies=[Depends(reject_cross_site)],
)
def create_project_invite(
  project_id: str,
  body: ProjectInviteCreate,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  active_count = db.query(models.ProjectInvite).filter(
    models.ProjectInvite.project_id == project.id,
    models.ProjectInvite.revoked_at.is_(None),
    models.ProjectInvite.consumed_at.is_(None),
    models.ProjectInvite.expires_at > now_naive_utc(),
  ).count()
  if active_count >= 50:
    raise HTTPException(409, "Revoke an unused invitation before creating another.")
  secret = secrets.token_urlsafe(32)
  now = now_naive_utc()
  invite = models.ProjectInvite(
    id=str(uuid.uuid4()), project_id=project.id,
    token_hash=_invite_hash(secret), invitee_name=body.invitee_name,
    role=body.role, created_at=now, expires_at=now + _INVITE_TTL,
  )
  db.add(invite)
  db.commit()
  return {
    "id": invite.id,
    "invitee_name": invite.invitee_name,
    "role": invite.role,
    "created_at": invite.created_at,
    "expires_at": invite.expires_at,
    # The secret stays in the fragment, so it never reaches access logs or a
    # Referer before the recipient deliberately exchanges it.
    "join_url": (
      get_settings().frontend_origin.rstrip("/")
      + "/project-invite#" + secret
    ),
  }


@router.delete(
  "/{project_id}/invites/{invite_id}", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def revoke_project_invite(
  project_id: str,
  invite_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  _live_project(db, project_id)
  invite = db.query(models.ProjectInvite).filter(
    models.ProjectInvite.id == invite_id,
    models.ProjectInvite.project_id == project_id,
    models.ProjectInvite.revoked_at.is_(None),
    models.ProjectInvite.consumed_at.is_(None),
  ).first()
  if invite is None:
    raise HTTPException(404, "Invitation not found.")
  invite.revoked_at = now_naive_utc()
  db.commit()
  return Response(status_code=204)


@router.patch(
  "/{project_id}/members/{member_id}",
  dependencies=[Depends(reject_cross_site)],
)
def update_project_member(
  project_id: str,
  member_id: str,
  body: ProjectMemberPatch,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  _live_project(db, project_id)
  member = db.query(models.ProjectMember).filter(
    models.ProjectMember.id == member_id,
    models.ProjectMember.project_id == project_id,
    models.ProjectMember.revoked_at.is_(None),
  ).first()
  if member is None:
    raise HTTPException(404, "Project member not found.")
  member.role = body.role
  db.commit()
  return {"id": member.id, "display_name": member.display_name, "role": member.role}


@router.delete(
  "/{project_id}/members/{member_id}", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def revoke_project_member(
  project_id: str,
  member_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  _live_project(db, project_id)
  member = db.query(models.ProjectMember).filter(
    models.ProjectMember.id == member_id,
    models.ProjectMember.project_id == project_id,
    models.ProjectMember.revoked_at.is_(None),
  ).first()
  if member is None:
    raise HTTPException(404, "Project member not found.")
  member.revoked_at = now_naive_utc()
  member.token_epoch += 1
  db.commit()
  return Response(status_code=204)


@router.get("/{project_id}/chats")
def list_project_chats(
  project_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  _live_project(db, project_id)
  rows = _live_project_chat_rows(db, project_id)
  return [_project_chat_response(row) for row in rows]


@router.get("/{project_id}/agents")
def list_project_agents(
  project_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  return [
    _project_agent_response(db, chat)
    for chat in _live_project_chat_rows(db, project.id)
  ]


@router.get("/{project_id}/agent-messages")
def list_project_agent_messages(
  project_id: str,
  chat_id: str = Query(min_length=1, max_length=64),
  limit: int = Query(default=50, ge=1, le=100),
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.project_id == project.id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if chat is None:
    raise HTTPException(404, "Project chat not found.")
  rows = db.query(models.ProjectAgentMessage).filter(
    models.ProjectAgentMessage.project_id == project.id,
    or_(
      models.ProjectAgentMessage.to_chat_id.is_(None),
      models.ProjectAgentMessage.to_chat_id == chat.id,
      models.ProjectAgentMessage.from_chat_id == chat.id,
    ),
  ).order_by(models.ProjectAgentMessage.created_at.desc()).limit(limit).all()
  return [_project_agent_message_response(row) for row in reversed(rows)]


@router.post(
  "/{project_id}/agent-messages", dependencies=[Depends(reject_cross_site)],
)
def send_project_agent_message(
  project_id: str,
  body: ProjectAgentMessageCreate,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  project_chats = {
    str(chat.id): chat for chat in _live_project_chat_rows(db, project.id)
  }
  if body.sender_chat_id not in project_chats:
    raise HTTPException(404, "Sender is not a live chat in this project.")
  recipients = list(dict.fromkeys(body.recipients))
  if body.broadcast and recipients:
    raise HTTPException(400, "Choose a broadcast or specific recipients, not both.")
  if not body.broadcast and not recipients:
    raise HTTPException(400, "Choose at least one recipient or broadcast to the project.")
  if any(chat_id not in project_chats for chat_id in recipients):
    raise HTTPException(404, "A recipient is not a live chat in this project.")
  targets: list[str | None] = [None] if body.broadcast else recipients
  rows = [
    models.ProjectAgentMessage(
      id=str(uuid.uuid4()),
      project_id=project.id,
      from_chat_id=body.sender_chat_id,
      to_chat_id=target,
      body=body.body,
    )
    for target in targets
  ]
  db.add_all(rows)
  db.commit()
  for row in rows:
    db.refresh(row)
  # The injected mailbox is deliberately a recent coordination surface, not
  # a second transcript. Keep the durable project history bounded too.
  stale_rows = db.query(models.ProjectAgentMessage).filter(
    models.ProjectAgentMessage.project_id == project.id,
  ).order_by(models.ProjectAgentMessage.created_at.desc()).offset(1000).all()
  for stale in stale_rows:
    db.delete(stale)
  if stale_rows:
    db.commit()
  get_system_broadcast().publish({
    "type": "project_agent_message",
    "projectId": project.id,
    "senderChatId": body.sender_chat_id,
    "recipientChatIds": recipients,
    "broadcast": body.broadcast,
  })
  return [_project_agent_message_response(row) for row in rows]


@router.post(
  "/{project_id}/chats", status_code=201,
  dependencies=[Depends(reject_cross_site)],
)
def create_project_chat(
  project_id: str,
  body: ProjectChatCreate,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  chat_id = (
    str(uuid.uuid5(uuid.UUID(project.id), body.recovery_request_id))
    if body.recovery_request_id
    else str(uuid.uuid4())
  )
  existing = db.get(models.Chat, chat_id)
  if existing is not None:
    if existing.deleted_at is not None:
      raise HTTPException(409, "Project chat was deleted.")
    if existing.project_id != project.id:
      raise HTTPException(409, "Chat identity is already in use.")
    return _project_chat_response(existing)
  chat = _new_chat(
    db, chat_id=chat_id, title=body.title, owner=owner,
    project_id=project.id,
  )
  db.add(chat)
  project.updated_at = now_naive_utc()
  try:
    db.commit()
  except IntegrityError:
    db.rollback()
    existing = db.get(models.Chat, chat_id)
    if existing is None or existing.project_id != project.id:
      raise
    return _project_chat_response(existing)
  db.refresh(chat)
  get_system_broadcast().publish({
    "type": "project_chat_created",
    "projectId": str(project.id),
    "chatId": str(chat.id),
  })
  return _project_chat_response(chat)


@router.get("/{project_id}")
def get_project(
  project_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  if principal.is_owner:
    project_drawer.annotate_projects(db, [project])
  chats = _live_project_chat_rows(db, project.id) if principal.is_owner else []
  return _project_response(project, chats)


@router.post(
  "/{project_id}/opened", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def mark_project_opened(
  project_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  project_drawer.mark_opened(db, project.id)
  db.commit()
  return Response(status_code=204)


@router.post(
  "/{project_id}/folder", dependencies=[Depends(reject_cross_site)],
)
def create_project_folder(
  project_id: str,
  body: FolderCreate,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "editor")
  root, target = _resolve_project_path(project, body.path)
  if target == root:
    raise HTTPException(400, "The project root already exists.")
  _reject_artifacts_mutation(root, target)
  with _locked_project_mutation(root):
    try:
      target.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
      raise HTTPException(409, "A file or folder already uses that path.") from exc
  project.updated_at = now_naive_utc()
  change = _record_project_change(
    db, project, principal, kind="folder_created",
    path=target.relative_to(root).as_posix(),
  )
  db.commit()
  _publish_project_change(project.id, change)
  return {"ok": True, "path": target.relative_to(root).as_posix()}


@router.patch("/{project_id}", dependencies=[Depends(reject_cross_site)])
def patch_project(
  project_id: str,
  body: ProjectPatch,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  if body.name is not None:
    project.name = body.name
  if "color" in body.model_fields_set:
    project.color = body.color
  if body.pinned is not None:
    project_drawer.set_pinned(db, project.id, body.pinned)
  try:
    db.commit()
  except IntegrityError as exc:
    db.rollback()
    raise HTTPException(409, "Project could not be updated.") from exc
  db.refresh(project)
  project_drawer.annotate_projects(db, [project])
  return _project_response(project, _live_project_chat_rows(db, project.id))


@router.delete(
  "/{project_id}", status_code=204, dependencies=[Depends(reject_cross_site)],
)
async def delete_project(
  project_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  chats = db.query(models.Chat).filter(
    models.Chat.project_id == project.id,
    models.Chat.deleted_at.is_(None),
  ).all()
  for chat in chats:
    if is_chat_running(chat.id):
      try:
        stopped, _ = await stop_chat_for(chat.id, db=db)
      except Exception:
        log.warning("Failed to stop project chat %s during delete", chat.id)
        stopped = False
      if not stopped:
        raise HTTPException(
          409, "Could not stop an active project agent; retry."
        )
  # One timestamp and one commit make the project and all currently-live chats
  # a recovery unit. Chats deleted earlier keep their own tombstone and are not
  # unexpectedly recovered with the project.
  for chat in chats:
    bump_run_generation(chat.id)
  with PROJECT_LIFECYCLE_LOCK:
    deleted_at = now_naive_utc()
    project.deleted_at = deleted_at
    from app.chat_waits import stage_cancel_waits_for_chat
    for chat in chats:
      stage_cancel_waits_for_chat(db, chat.id)
      chat.deleted_at = deleted_at
    db.commit()
  for chat in chats:
    questions.cancel(chat.id)
    mark_chat_deleted(chat.id)
    try:
      await _finish_run(chat.id, terminal_status="stopped")
    except Exception:
      log.exception(
        "Project %s was deleted but chat %s run cleanup failed",
        project.id, chat.id,
      )
    get_system_broadcast().publish(
      {"type": "chat_deleted", "chatId": str(chat.id)}
    )
  get_system_broadcast().publish({
    "type": "project_deleted",
    "projectId": str(project.id),
    "chatIds": [str(chat.id) for chat in chats],
  })
  return Response(status_code=204)


@router.post("/{project_id}/recover", dependencies=[Depends(reject_cross_site)])
def recover_project(
  project_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  with PROJECT_LIFECYCLE_LOCK:
    project = db.query(models.Project).filter(
      models.Project.id == project_id,
      models.Project.deleted_at.isnot(None),
    ).first()
    if project is None:
      raise HTTPException(404, "Project not found or not deleted.")
    if now_naive_utc() - project.deleted_at >= SOFT_DELETE_TTL:
      raise HTTPException(410, "Recovery window has expired.")
    if not _project_root(project).is_dir():
      raise HTTPException(409, "Project files are unavailable.")
    deleted_at = project.deleted_at
    chats = db.query(models.Chat).filter(
      models.Chat.project_id == project.id,
      models.Chat.deleted_at == deleted_at,
    ).all()
    project.deleted_at = None
    for chat in chats:
      chat.deleted_at = None
    db.commit()
  for chat in chats:
    recover_chat_generation(chat.id)
    get_system_broadcast().publish(
      {"type": "chat_recovered", "chatId": str(chat.id)}
    )
  get_system_broadcast().publish({
    "type": "project_recovered",
    "projectId": str(project.id),
    "chatIds": [str(chat.id) for chat in chats],
  })
  return _project_response(project, chats)


@router.get("/{project_id}/files")
def list_project_files(
  project_id: str,
  path: str = Query(default="", max_length=2048),
  recursive: bool = False,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """One folder's entries, or — with ``recursive`` — every file under it.

  The recursive form exists for whole-project features (the composer's
  @-mention file search); it returns files only, in breadth-first order, with
  the same entry shape and the same symlink/`artifacts/` exclusions as the
  flat listing, capped by the shared list limit.
  """
  project = _project_for(db, project_id, principal, "viewer")
  root, directory = _resolve_project_path(project, path)
  if not directory.exists():
    return {"path": path, "entries": []}
  if not directory.is_dir():
    raise HTTPException(400, "Path is not a directory.")

  # `artifacts/` at the root is managed build output surfaced above the source
  # tree, not a second editable copy of the same result.
  return workspace_files.list_entries(
    root, directory,
    path=path,
    recursive=recursive,
    hidden_dirs=workspace_files.GIT_METADATA_NAMES,
    hidden_root_dirs=frozenset({"artifacts"}),
  )


@router.get("/{project_id}/changes")
def list_project_changes(
  project_id: str,
  after: int | None = Query(default=None, ge=0),
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """Return a reconnect cursor and path-only changes after it.

  Omitting ``after`` establishes a baseline without replaying project history.
  A cursor older than the bounded log receives the retained tail; clients treat
  a project-wide or truncated result as a full workspace refresh.
  """
  project = _project_for(db, project_id, principal, "viewer")
  latest = db.query(models.ProjectChange.id).filter(
    models.ProjectChange.project_id == project.id,
  ).order_by(models.ProjectChange.id.desc()).limit(1).scalar() or 0
  if after is None:
    return {"cursor": latest, "changes": [], "truncated": False}
  oldest = db.query(models.ProjectChange.id).filter(
    models.ProjectChange.project_id == project.id,
  ).order_by(models.ProjectChange.id.asc()).limit(1).scalar()
  rows = db.query(models.ProjectChange).filter(
    models.ProjectChange.project_id == project.id,
    models.ProjectChange.id > after,
  ).order_by(models.ProjectChange.id.asc()).limit(101).all()
  # An explicit cursor older than the retained tail means the client missed a
  # pruned segment even when fewer than 101 current rows remain.
  truncated = len(rows) > 100 or (oldest is not None and after < oldest)
  rows = rows[:100]
  cursor = rows[-1].id if rows else max(after, latest)
  return {
    "cursor": cursor,
    "changes": [_change_view(row) for row in rows],
    "truncated": truncated,
  }


@router.get("/{project_id}/file")
def read_project_file(
  project_id: str,
  path: str = Query(min_length=1, max_length=2048),
  download: bool = False,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  _root, target = _resolve_project_path(project, path)
  try:
    payload, media_type = workspace_files.read_file(target, path)
  except FileNotFoundError as exc:
    raise HTTPException(404, "File not found.") from exc
  except OverflowError as exc:
    raise HTTPException(413, "File is too large to open in Projects.")
  if not download and payload is not None:
    return payload
  revision = workspace_files.file_revision(target)
  return FileResponse(
    target,
    media_type=media_type,
    filename=target.name if download else None,
    headers={"ETag": revision} if revision else None,
  )


@router.get("/{project_id}/git/status")
def get_project_git_status(
  project_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  return project_git.project_status(_project_root(project))


@router.post(
  "/{project_id}/git/init", dependencies=[Depends(reject_cross_site)],
)
def initialize_project_git(
  project_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "maintainer")
  try:
    return project_git.initialize_project(_project_root(project))
  except project_git.GitProjectError as exc:
    raise HTTPException(409, str(exc)) from exc


@router.post(
  "/{project_id}/git/commit", dependencies=[Depends(reject_cross_site)],
)
def commit_project_git(
  project_id: str,
  body: ProjectCommit,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "maintainer")
  try:
    return project_git.commit_project(
      _project_root(project), body.message, body.expected_head,
    )
  except project_git.GitProjectError as exc:
    raise HTTPException(409, str(exc)) from exc


@router.get("/{project_id}/git/diff")
def get_project_git_diff(
  project_id: str,
  path: str = Query(min_length=1, max_length=2048),
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "viewer")
  root, target = _resolve_project_path(project, path)
  if not target.is_file():
    raise HTTPException(404, "File not found.")
  return project_git.project_file_diff(root, target)


@router.get("/{project_id}/git/remote")
def get_project_git_remote(
  project_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  return {
    **project_git.project_remote_status(_project_root(project)),
    "github_connected": github_auth.get_token() is not None,
  }


@router.post(
  "/{project_id}/git/remote", dependencies=[Depends(reject_cross_site)],
)
def connect_project_git_remote(
  project_id: str,
  body: ProjectRemoteConnect,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  try:
    return {
      **project_git.connect_github_remote(_project_root(project), body.repository),
      "github_connected": github_auth.get_token() is not None,
    }
  except project_git.GitProjectError as exc:
    raise HTTPException(409, str(exc)) from exc


def _require_project_github_connection() -> None:
  if github_auth.get_token() is None:
    raise HTTPException(409, "Connect GitHub before synchronizing this project.")


@router.post(
  "/{project_id}/git/fetch", dependencies=[Depends(reject_cross_site)],
)
def fetch_project_git_remote(
  project_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  _require_project_github_connection()
  try:
    return {
      **project_git.fetch_project(_project_root(project)),
      "github_connected": True,
    }
  except project_git.GitProjectError as exc:
    raise HTTPException(409, str(exc)) from exc


@router.post(
  "/{project_id}/git/pull", dependencies=[Depends(reject_cross_site)],
)
def pull_project_git_remote(
  project_id: str,
  body: ProjectRemoteAction,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  _require_project_github_connection()
  root = _project_root(project)
  try:
    with _locked_project_mutation(root):
      result = project_git.pull_project(root, body.expected_head)
  except project_git.GitProjectError as exc:
    raise HTTPException(409, str(exc)) from exc
  project.updated_at = now_naive_utc()
  change = append_project_change(
    db,
    project_id=project.id,
    kind="git_pulled",
    path=None,
    actor_key="owner",
    display_name=owner.username or "Owner",
  )
  db.commit()
  _publish_project_change(project.id, change)
  return {**result, "github_connected": True}


@router.post(
  "/{project_id}/git/push", dependencies=[Depends(reject_cross_site)],
)
def push_project_git_remote(
  project_id: str,
  body: ProjectRemotePush,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  project = _live_project(db, project_id)
  if body.confirmed is not True:
    raise HTTPException(400, "Confirm the reviewed commits before pushing.")
  _require_project_github_connection()
  try:
    return {
      **project_git.push_project(_project_root(project), body.expected_head),
      "github_connected": True,
    }
  except project_git.GitProjectError as exc:
    raise HTTPException(409, str(exc)) from exc


@router.put(
  "/{project_id}/file", dependencies=[Depends(reject_cross_site)],
)
def write_project_file(
  project_id: str,
  body: FileWrite,
  path: str = Query(min_length=1, max_length=2048),
  force: bool = Query(default=False),
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "editor")
  root, target = _resolve_project_path(project, path)
  if target == root:
    raise HTTPException(400, "A project root is not a file.")
  _reject_artifacts_mutation(root, target)
  if force and not principal.is_owner:
    raise HTTPException(403, "Only the project owner can force a file save.")
  if force and "expected_revision" in body.model_fields_set:
    raise HTTPException(400, "Choose a revision check or an owner force save, not both.")
  if not force and "expected_revision" not in body.model_fields_set:
    raise HTTPException(status_code=428, detail={
      "code": "file_revision_required",
      "message": "Open the latest file revision before saving.",
      "path": target.relative_to(root).as_posix(),
    })
  encoded = body.content.encode("utf-8")
  if len(encoded) > _WRITE_MAX:
    raise HTTPException(413, "File is too large to save in Projects.")
  with _locked_project_mutation(root):
    target.parent.mkdir(parents=True, exist_ok=True)
    if not force:
      _require_expected_revision(
        target, target.relative_to(root).as_posix(), body.expected_revision,
      )
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
      temp.write_bytes(encoded)
      os.replace(temp, target)
    finally:
      try:
        temp.unlink()
      except FileNotFoundError:
        pass
  revision = hashlib.sha256(encoded).hexdigest()
  project.updated_at = now_naive_utc()
  change = _record_project_change(
    db, project, principal, kind="file_saved",
    path=target.relative_to(root).as_posix(), revision=revision,
  )
  db.commit()
  _publish_project_change(project.id, change)
  return {
    "ok": True,
    "path": target.relative_to(root).as_posix(),
    "revision": revision,
  }


@router.put(
  "/{project_id}/file-bytes", dependencies=[Depends(reject_cross_site)],
)
async def write_project_file_bytes(
  project_id: str,
  request: Request,
  path: str = Query(min_length=1, max_length=2048),
  force: bool = Query(default=False),
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """Write an image/PDF/asset without coercing it through JSON text."""
  length = request.headers.get("content-length")
  if length:
    try:
      parsed_length = int(length)
    except ValueError as exc:
      raise HTTPException(400, "Invalid Content-Length header.") from exc
    if parsed_length < 0:
      raise HTTPException(400, "Invalid Content-Length header.")
    if parsed_length > _WRITE_MAX:
      raise HTTPException(413, "File is too large to save in Projects.")
  content = await request.body()
  if len(content) > _WRITE_MAX:
    raise HTTPException(413, "File is too large to save in Projects.")
  project = _project_for(db, project_id, principal, "editor")
  root, target = _resolve_project_path(project, path)
  if target == root:
    raise HTTPException(400, "A project root is not a file.")
  _reject_artifacts_mutation(root, target)
  if force and not principal.is_owner:
    raise HTTPException(403, "Only the project owner can force a file save.")
  expected_revision: str | None = None
  enforce_revision = False
  if request.headers.get("if-none-match") == "*":
    enforce_revision = True
  elif request.headers.get("if-match"):
    candidate = request.headers["if-match"].strip().strip('"').lower()
    if not re.fullmatch(r"[0-9a-f]{64}", candidate):
      raise HTTPException(400, "If-Match must contain a file revision.")
    expected_revision = candidate
    enforce_revision = True
  if force and enforce_revision:
    raise HTTPException(400, "Choose a revision check or an owner force save, not both.")
  if not force and not enforce_revision:
    raise HTTPException(status_code=428, detail={
      "code": "file_revision_required",
      "message": "Open the latest file revision before saving.",
      "path": target.relative_to(root).as_posix(),
    })
  with _locked_project_mutation(root):
    target.parent.mkdir(parents=True, exist_ok=True)
    if not force:
      _require_expected_revision(
        target, target.relative_to(root).as_posix(), expected_revision,
      )
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
      temp.write_bytes(content)
      os.replace(temp, target)
    finally:
      try:
        temp.unlink()
      except FileNotFoundError:
        pass
  revision = hashlib.sha256(content).hexdigest()
  project.updated_at = now_naive_utc()
  change = _record_project_change(
    db, project, principal, kind="file_saved",
    path=target.relative_to(root).as_posix(), revision=revision,
  )
  db.commit()
  _publish_project_change(project.id, change)
  return {
    "ok": True,
    "path": target.relative_to(root).as_posix(),
    "revision": revision,
  }


@router.delete(
  "/{project_id}/file", dependencies=[Depends(reject_cross_site)],
)
def delete_project_file(
  project_id: str,
  path: str = Query(min_length=1, max_length=2048),
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  project = _project_for(db, project_id, principal, "editor")
  root, target = _resolve_project_path(project, path)
  if target == root:
    raise HTTPException(400, "The project root cannot be deleted.")
  _reject_artifacts_mutation(root, target)
  with _locked_project_mutation(root):
    if target.is_dir():
      shutil.rmtree(target)
    elif target.is_file():
      target.unlink()
    else:
      raise HTTPException(404, "File not found.")
  project.updated_at = now_naive_utc()
  change = _record_project_change(
    db, project, principal, kind="path_deleted",
    path=target.relative_to(root).as_posix(),
  )
  db.commit()
  _publish_project_change(project.id, change)
  return {"ok": True}


@router.post(
  "/{project_id}/move", dependencies=[Depends(reject_cross_site)],
)
def move_project_path(
  project_id: str,
  body: PathMove,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """Rename or move one confined file or directory within a project.

  Both endpoints resolve through ``_resolve_project_path`` (confinement +
  symlink rejection). The guards refuse moving the root, a missing source, an
  occupied destination, a folder into its own descendant, and any move touching
  the reserved ``artifacts/`` build-output area (managed by the artifact
  registry, not hand-moves). An ``os.replace`` failure maps to 409, never 500.
  """
  project = _project_for(db, project_id, principal, "editor")
  root, source = _resolve_project_path(project, body.from_path)
  _root, dest = _resolve_project_path(project, body.to_path)
  if source == root or dest == root:
    raise HTTPException(400, "The project root cannot be moved.")
  if dest == source or dest.is_relative_to(source):
    raise HTTPException(400, "Cannot move a path into itself or a descendant.")
  _reject_artifacts_mutation(root, source)
  _reject_artifacts_mutation(root, dest)
  with _locked_project_mutation(root):
    if not source.exists():
      raise HTTPException(404, "Source path not found.")
    if dest.exists():
      raise HTTPException(409, "A file or folder already uses the destination.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
      os.replace(source, dest)
    except OSError as exc:
      raise HTTPException(409, "Could not move the path.") from exc
    revision = workspace_files.file_revision(dest)
  project.updated_at = now_naive_utc()
  change = _record_project_change(
    db, project, principal, kind="path_moved",
    path=dest.relative_to(root).as_posix(),
    prior_path=source.relative_to(root).as_posix(),
    revision=revision,
  )
  db.commit()
  _publish_project_change(project.id, change)
  return {
    "ok": True,
    "from": source.relative_to(root).as_posix(),
    "to": dest.relative_to(root).as_posix(),
  }


@router.get("/{project_id}/artifacts")
def list_project_artifacts(
  project_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """List a project's artifacts, reconciling live/stale build status."""
  project = _project_for(db, project_id, principal, "viewer")
  root = _project_root(project)
  return {
    "artifacts": [
      _artifact_view(project, root, entry)
      for entry in project_builders.read_artifacts(project)
    ],
  }


@router.post(
  "/{project_id}/artifacts", status_code=201,
  dependencies=[Depends(reject_cross_site)],
)
async def create_project_artifact(
  project_id: str,
  body: ArtifactCreate,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """Register a new buildable artifact.

  Async so its ``artifacts_json`` read-update-commit is ordered on the single
  event loop against a concurrent build's status write — the loop runs this
  handler with no ``await`` between the read and the commit, so neither write is
  lost. Validates the builder and that the source resolves to a real project
  file before recording it.
  """
  project = _project_for(db, project_id, principal, "editor")
  root = _project_root(project)
  artifact_type = project_builders.resolve_artifact_type(project, body.builder)
  if artifact_type is None:
    raise HTTPException(422, "Unknown builder.")
  _root, source_path = _resolve_project_path(project, body.source)
  if not source_path.is_file():
    raise HTTPException(422, "Source file does not exist.")
  source_rel = source_path.relative_to(root).as_posix()
  artifact_id = body.id or project_builders.slug_artifact_id(body.name)
  if not project_builders.ARTIFACT_ID_RE.match(artifact_id):
    raise HTTPException(422, "Invalid artifact id.")
  entries = project_builders.read_artifacts(project)
  if any(entry.get("id") == artifact_id for entry in entries):
    raise HTTPException(409, "An artifact with that id already exists.")
  entry = project_builders.new_artifact_entry(
    artifact_id, body.name, body.builder, source_rel, artifact_type,
  )
  entries.append(entry)
  project.artifacts_json = entries
  flag_modified(project, "artifacts_json")
  project.updated_at = now_naive_utc()
  db.commit()
  return _artifact_view(project, root, entry)


@router.delete(
  "/{project_id}/artifacts/{artifact_id}", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def delete_project_artifact(
  project_id: str,
  artifact_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """Remove an artifact entry and its on-disk ``artifacts/<id>/`` tree."""
  project = _project_for(db, project_id, principal, "editor")
  root = _project_root(project)
  if not project_builders.ARTIFACT_ID_RE.match(artifact_id):
    raise HTTPException(400, "Invalid artifact id.")
  if project_builders.is_build_live(project.id, artifact_id):
    raise HTTPException(409, "A build is in progress for this artifact.")
  entries = project_builders.read_artifacts(project)
  remaining = [entry for entry in entries if entry.get("id") != artifact_id]
  if len(remaining) == len(entries):
    raise HTTPException(404, "Artifact not found.")
  artifact_dir = (root / "artifacts" / artifact_id).resolve()
  if _within_artifacts(root, artifact_dir) and artifact_dir != _artifacts_root(root):
    shutil.rmtree(artifact_dir, ignore_errors=True)
  project.artifacts_json = remaining or None
  flag_modified(project, "artifacts_json")
  project.updated_at = now_naive_utc()
  db.commit()
  return Response(status_code=204)


@router.post(
  "/{project_id}/artifacts/{artifact_id}/build",
  dependencies=[Depends(reject_cross_site)],
)
async def build_project_artifact(
  project_id: str,
  artifact_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """Start a build for one artifact.

  409 only when a LIVE build task already exists for this artifact; a stale
  ``building`` marker (no live task) is reconciled and a rebuild is allowed.
  Returns the artifact row, which now reports ``building`` because the task is
  registered before this returns.
  """
  project = _project_for(db, project_id, principal, "editor")
  root = _project_root(project)
  if not project_builders.ARTIFACT_ID_RE.match(artifact_id):
    raise HTTPException(400, "Invalid artifact id.")
  entry = next(
    (e for e in project_builders.read_artifacts(project)
     if e.get("id") == artifact_id),
    None,
  )
  if entry is None:
    raise HTTPException(404, "Artifact not found.")
  if project_builders.resolve_artifact_type(project, str(entry.get("builder"))) is None:
    raise HTTPException(422, "This artifact has an unknown builder.")
  source = entry.get("source")
  try:
    _source_root, source_path = _resolve_project_path(
      project, source if isinstance(source, str) else "",
    )
  except HTTPException:
    source_path = None
  if source_path is None or not source_path.is_file():
    raise HTTPException(422, "The artifact source file is missing.")
  if project_builders.is_build_live(project.id, artifact_id):
    raise HTTPException(409, "A build is already in progress.")
  project_builders.start_build(project.id, artifact_id)
  return _artifact_view(project, root, entry)


@router.get("/{project_id}/artifacts/{artifact_id}/log")
def read_project_artifact_log(
  project_id: str,
  artifact_id: str,
  principal: ProjectPrincipal = Depends(get_project_principal),
  db: Session = Depends(get_db),
):
  """Return the tail of an artifact's build log (capped ~64 KB)."""
  project = _project_for(db, project_id, principal, "viewer")
  root = _project_root(project)
  if not project_builders.ARTIFACT_ID_RE.match(artifact_id):
    raise HTTPException(400, "Invalid artifact id.")
  log_path = (root / "artifacts" / artifact_id / "build.log").resolve()
  if not _within_artifacts(root, log_path):
    raise HTTPException(400, "Invalid artifact path.")
  if not log_path.is_file():
    return {"log": "", "truncated": False}
  data = log_path.read_bytes()
  truncated = len(data) > _LOG_TAIL_MAX
  tail = data[-_LOG_TAIL_MAX:]
  return {"log": tail.decode("utf-8", errors="replace"), "truncated": truncated}


@router.get("/{project_id}/artifacts/{artifact_id}/output/{path:path}")
def serve_project_artifact_output(
  project_id: str,
  artifact_id: str,
  path: str,
  principal: ProjectPrincipal = Depends(_resolve_output_principal),
  db: Session = Depends(get_db),
):
  """Stream a confined artifact-output file.

  A dedicated FileResponse with NO 10 MB read cap — a real thesis PDF or a
  multi-asset website is large by design. Confined to ``artifacts/<id>/output/``
  with symlink rejection. A website entry (any served HTML) gets a strict
  per-response CSP so the sandboxed iframe cannot reach the shell token.
  """
  project = _project_for(db, project_id, principal, "viewer")
  root = _project_root(project)
  if not project_builders.ARTIFACT_ID_RE.match(artifact_id):
    raise HTTPException(400, "Invalid artifact id.")
  output_root = (root / "artifacts" / artifact_id / "output")
  rel = (path or "").lstrip("/")
  if "\x00" in rel:
    raise HTTPException(400, "Invalid path.")
  try:
    target = validate_path_within_base(rel, output_root)
  except ValueError as exc:
    raise HTTPException(400, "Invalid artifact output path.") from exc
  candidate = output_root / rel
  if candidate.is_symlink():
    raise HTTPException(403, "Symbolic links are not available in output.")
  if not target.is_file():
    raise HTTPException(404, "Artifact output not found.")
  media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
  # The per-response CSP for this namespace (the exact policy the spec gives) is
  # applied authoritatively by main._SecurityHeadersMiddleware — it strips any
  # CSP a route sets, so setting it here would be dead. See _ARTIFACT_OUTPUT_CSP.
  return FileResponse(target, media_type=media_type)
