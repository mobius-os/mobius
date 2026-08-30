"""Pinned project builds that people can use together without source access."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import (
  auth,
  models,
  project_builders,
  shared_app_releases,
  shared_app_retention,
  shared_app_state,
)
from app.config import get_settings
from app.database import get_db
from app.deps import SharedAppPrincipal, get_current_owner, get_shared_app_principal, reject_cross_site
from app.project_retention import PROJECT_LIFECYCLE_LOCK
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc


router = APIRouter(prefix="/api/shared-apps", tags=["shared-apps"])
_INVITE_TTL = timedelta(days=7)
_ROLES = {"viewer": 0, "editor": 1, "owner": 2}


class SharedAppCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")
  project_id: str = Field(min_length=1, max_length=64)
  artifact_id: str = Field(min_length=1, max_length=64)
  name: str | None = Field(default=None, max_length=256)


class SharedAppInviteCreate(BaseModel):
  model_config = ConfigDict(extra="forbid")
  invitee_name: str | None = Field(default=None, max_length=128)
  role: str = "editor"

  @field_validator("role")
  @classmethod
  def validate_role(cls, value: str) -> str:
    if value not in {"viewer", "editor"}:
      raise ValueError("role must be viewer or editor")
    return value


class SharedAppInviteRedeem(BaseModel):
  model_config = ConfigDict(extra="forbid")
  invite: str = Field(min_length=20, max_length=256)
  display_name: str = Field(min_length=1, max_length=128)


class SharedStateWrite(BaseModel):
  model_config = ConfigDict(extra="forbid")
  expected_version: str | None = Field(default=None, max_length=128)
  value: object | None = None
  delete: bool = False


class SharedAppMemberUpdate(BaseModel):
  model_config = ConfigDict(extra="forbid")
  role: str

  @field_validator("role")
  @classmethod
  def validate_role(cls, value: str) -> str:
    if value not in {"viewer", "editor"}:
      raise ValueError("role must be viewer or editor")
    return value


def _invite_hash(secret: str) -> str:
  return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _instance_for(
  db: Session, instance_id: str, principal: SharedAppPrincipal, role: str = "viewer",
) -> models.SharedAppInstance:
  if not principal.is_owner and principal.instance_id != instance_id:
    raise HTTPException(404, "Shared app not found.")
  if _ROLES.get(principal.role, -1) < _ROLES[role]:
    raise HTTPException(403, "This shared app role cannot do that.")
  row = db.query(models.SharedAppInstance).filter(models.SharedAppInstance.id == instance_id).first()
  if row is None or row.deleted_at is not None:
    raise HTTPException(404, "Shared app not found.")
  project = db.query(models.Project.id).filter(
    models.Project.id == row.project_id,
    models.Project.deleted_at.is_(None),
  ).first()
  if project is None:
    raise HTTPException(404, "Shared app not found.")
  return row


def _built_artifact(project: models.Project, artifact_id: str) -> tuple[dict, Path, str]:
  artifact = next(
    (item for item in project_builders.read_artifacts(project) if item.get("id") == artifact_id),
    None,
  )
  if artifact is None:
    raise HTTPException(404, "Artifact not found.")
  output_rel = artifact.get("output_rel")
  if not isinstance(output_rel, str) or not output_rel:
    artifact_type = project_builders.resolve_artifact_type(project, str(artifact.get("builder")))
    output_rel = project_builders.default_output_rel(
      artifact_id, str(artifact.get("builder")), str(artifact.get("source") or ""), artifact_type,
    )
  project_root = (Path(get_settings().data_dir) / project.root_path).resolve()
  output_root = (project_root / "artifacts" / artifact_id / "output").resolve()
  try:
    entry = (project_root / output_rel.lstrip("/")).resolve()
    entry_rel = entry.relative_to(output_root).as_posix()
  except (ValueError, OSError) as exc:
    raise HTTPException(422, "Artifact output is invalid.") from exc
  if not entry.is_file():
    raise HTTPException(409, "Build the artifact before sharing it.")
  total = 0
  for candidate in output_root.rglob("*"):
    if candidate.is_symlink():
      raise HTTPException(422, "Shared builds cannot contain symbolic links.")
    if candidate.is_file():
      total += candidate.stat().st_size
      if total > shared_app_releases.SNAPSHOT_BYTES_MAX:
        raise HTTPException(413, "This build is too large to share as an app.")
  return artifact, output_root, entry_rel


def _view(
  row: models.SharedAppInstance,
  principal: SharedAppPrincipal,
  release: shared_app_releases.SharedAppRelease | None = None,
) -> dict:
  release = release or shared_app_releases.current_release(row)
  return {
    "id": row.id,
    "name": release.name,
    "project_id": row.project_id if principal.is_owner else None,
    "artifact_id": row.artifact_id if principal.is_owner else None,
    "entry_path": release.entry_path,
    "release_id": release.release_id,
    "role": principal.role,
    "member_id": principal.member_id,
    "created_at": row.created_at,
    "updated_at": release.published_at,
  }


@router.post("", status_code=201, dependencies=[Depends(reject_cross_site)])
def create_shared_app(
  body: SharedAppCreate,
  response: Response,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  with PROJECT_LIFECYCLE_LOCK:
    return _create_shared_app(body, response, owner, db)


def _create_shared_app(
  body: SharedAppCreate,
  response: Response,
  owner: models.Owner,
  db: Session,
):
  project = db.query(models.Project).filter(
    models.Project.id == body.project_id, models.Project.deleted_at.is_(None),
  ).first()
  if project is None:
    raise HTTPException(404, "Project not found.")
  existing = db.query(models.SharedAppInstance).filter(
    models.SharedAppInstance.project_id == project.id,
    models.SharedAppInstance.artifact_id == body.artifact_id,
    models.SharedAppInstance.deleted_at.is_(None),
  ).order_by(models.SharedAppInstance.updated_at.desc()).first()
  principal = SharedAppPrincipal(owner=owner, role="owner", display_name=owner.username)
  if existing is not None:
    response.status_code = 200
    return _view(existing, principal)
  artifact, output_root, entry_rel = _built_artifact(project, body.artifact_id)
  instance_id = str(uuid.uuid4())
  name = str(body.name or artifact.get("name") or project.name)[:256]
  release = None
  try:
    release = shared_app_releases.create_initial_release(
      instance_id=instance_id,
      output_root=output_root,
      entry_path=entry_rel,
      name=name,
    )
    row = models.SharedAppInstance(
      id=instance_id,
      project_id=project.id,
      artifact_id=body.artifact_id,
      name=name,
      entry_path=entry_rel,
      snapshot_path=release.snapshot_path,
    )
    db.add(row)
    db.commit()
  except Exception:
    db.rollback()
    try:
      persisted_snapshot = db.query(models.SharedAppInstance.snapshot_path).filter(
        models.SharedAppInstance.id == instance_id,
      ).scalar()
    except Exception:
      # The commit outcome cannot be established while the database is down.
      # Leave the fsynced tree for startup's canonical-pointer reconciliation.
      raise
    if persisted_snapshot is None:
      cleanup_path = (
        release.snapshot_path
        if release is not None
        else (Path("shared") / "app-instances" / instance_id / "build").as_posix()
      )
      cleanup_root = shared_app_retention.owned_snapshot_root(instance_id, cleanup_path)
      if cleanup_root is not None:
        shared_app_retention.remove_snapshot_root(cleanup_root)
    raise
  return _view(row, principal, release)


@router.get("")
def list_shared_apps(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  principal = SharedAppPrincipal(owner=owner, role="owner", display_name=owner.username)
  rows = db.query(models.SharedAppInstance).join(
    models.Project, models.Project.id == models.SharedAppInstance.project_id,
  ).filter(
    models.SharedAppInstance.deleted_at.is_(None),
    models.Project.deleted_at.is_(None),
  ).order_by(models.SharedAppInstance.updated_at.desc()).all()
  return [_view(row, principal) for row in rows]


@router.post("/invites/redeem", dependencies=[Depends(reject_cross_site)])
def redeem_shared_app_invite(body: SharedAppInviteRedeem, db: Session = Depends(get_db)):
  with PROJECT_LIFECYCLE_LOCK:
    return _redeem_shared_app_invite(body, db)


def _redeem_shared_app_invite(body: SharedAppInviteRedeem, db: Session):
  now = now_naive_utc()
  invite = db.query(models.SharedAppInvite).filter(
    models.SharedAppInvite.token_hash == _invite_hash(body.invite),
    models.SharedAppInvite.revoked_at.is_(None),
    models.SharedAppInvite.consumed_at.is_(None),
    models.SharedAppInvite.expires_at > now,
  ).first()
  if invite is None:
    raise HTTPException(410, "This app invitation is invalid or has expired.")
  instance = db.query(models.SharedAppInstance).filter(
    models.SharedAppInstance.id == invite.instance_id,
    models.SharedAppInstance.deleted_at.is_(None),
  ).first()
  if instance is None:
    raise HTTPException(410, "This shared app is no longer available.")
  if db.query(models.Project.id).filter(
    models.Project.id == instance.project_id,
    models.Project.deleted_at.is_(None),
  ).first() is None:
    raise HTTPException(410, "This shared app is no longer available.")
  owner = db.query(models.Owner).first()
  if owner is None:
    raise HTTPException(503, "The app owner is unavailable.")
  member_id = str(uuid.uuid4())
  member = models.SharedAppMember(
    id=member_id, instance_id=instance.id, display_name=body.display_name,
    role=invite.role, token_epoch=0, joined_at=now,
  )
  db.add(member)
  db.flush()
  claimed = db.query(models.SharedAppInvite).filter(
    models.SharedAppInvite.id == invite.id,
    models.SharedAppInvite.consumed_at.is_(None),
    models.SharedAppInvite.revoked_at.is_(None),
  ).update({
    models.SharedAppInvite.consumed_at: now,
    models.SharedAppInvite.accepted_member_id: member_id,
  }, synchronize_session=False)
  if claimed != 1:
    db.rollback()
    raise HTTPException(410, "This app invitation has already been used.")
  try:
    db.commit()
  except IntegrityError as exc:
    db.rollback()
    raise HTTPException(409, "Shared app access could not be created.") from exc
  token = auth.create_shared_app_collaborator_token(
    owner_username=owner.username, owner_epoch=owner.token_epoch,
    instance_id=instance.id, member_id=member_id, member_epoch=0,
  )
  return {
    "access_token": token, "token_type": "bearer",
    "instance": _view(instance, SharedAppPrincipal(
      owner=owner, role=member.role, instance_id=instance.id,
      member_id=member.id, display_name=member.display_name,
    )),
  }


@router.get("/{instance_id}")
def get_shared_app(
  instance_id: str,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  return _view(_instance_for(db, instance_id, principal), principal)


@router.put("/{instance_id}/release", dependencies=[Depends(reject_cross_site)])
def publish_shared_app_release(
  instance_id: str,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  with PROJECT_LIFECYCLE_LOCK:
    return _publish_shared_app_release(instance_id, principal, db)


def _publish_shared_app_release(
  instance_id: str, principal: SharedAppPrincipal, db: Session,
):
  row = _instance_for(db, instance_id, principal, "owner")
  project = db.query(models.Project).filter(
    models.Project.id == row.project_id,
    models.Project.deleted_at.is_(None),
  ).first()
  if project is None:
    raise HTTPException(409, "The source project is no longer available.")
  artifact, output_root, entry_rel = _built_artifact(project, row.artifact_id)
  name = str(artifact.get("name") or row.name)[:256]
  release = shared_app_releases.publish_release(
    db,
    row,
    output_root=output_root,
    entry_path=entry_rel,
    name=name,
  )
  return _view(row, principal, release)


@router.delete("/{instance_id}", status_code=204, dependencies=[Depends(reject_cross_site)])
def delete_shared_app(
  instance_id: str,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  with PROJECT_LIFECYCLE_LOCK:
    row = _instance_for(db, instance_id, principal, "owner")
    now = now_naive_utc()
    row.deleted_at = now
    db.query(models.SharedAppInvite).filter(
      models.SharedAppInvite.instance_id == row.id,
      models.SharedAppInvite.revoked_at.is_(None),
    ).update({models.SharedAppInvite.revoked_at: now}, synchronize_session=False)
    db.query(models.SharedAppMember).filter(
      models.SharedAppMember.instance_id == row.id,
      models.SharedAppMember.revoked_at.is_(None),
    ).update({
      models.SharedAppMember.revoked_at: now,
      models.SharedAppMember.token_epoch: models.SharedAppMember.token_epoch + 1,
    }, synchronize_session=False)
    db.commit()


@router.post("/{instance_id}/recover", dependencies=[Depends(reject_cross_site)])
def recover_shared_app(
  instance_id: str,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  with PROJECT_LIFECYCLE_LOCK:
    cutoff = now_naive_utc() - SOFT_DELETE_TTL
    row = db.query(models.SharedAppInstance).filter(
      models.SharedAppInstance.id == instance_id,
      models.SharedAppInstance.deleted_at.isnot(None),
      models.SharedAppInstance.deleted_at >= cutoff,
    ).first()
    if row is None:
      raise HTTPException(404, "Shared app not found or recovery expired.")
    project = db.query(models.Project.id).filter(
      models.Project.id == row.project_id,
      models.Project.deleted_at.is_(None),
    ).first()
    if project is None:
      raise HTTPException(409, "Recover the source project before this shared app.")
    active = db.query(models.SharedAppInstance.id).filter(
      models.SharedAppInstance.project_id == row.project_id,
      models.SharedAppInstance.artifact_id == row.artifact_id,
      models.SharedAppInstance.deleted_at.is_(None),
    ).first()
    if active is not None:
      raise HTTPException(409, "A newer shared app already exists for this creation.")
    row.deleted_at = None
    row.updated_at = now_naive_utc()
    db.commit()
    return _view(row, SharedAppPrincipal(
      owner=owner, role="owner", display_name=owner.username,
    ))


@router.get("/{instance_id}/output/{release_id}/{path:path}")
def get_shared_app_output(
  instance_id: str,
  release_id: str,
  path: str,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  with shared_app_releases.release_lock(instance_id):
    row = _instance_for(db, instance_id, principal)
    handle, media_type = shared_app_releases.open_release_file(row, release_id, path)

  def stream_body():
    with handle:
      while chunk := handle.read(64 * 1024):
        yield chunk

  return StreamingResponse(
    stream_body(),
    media_type=media_type,
    background=BackgroundTask(handle.close),
  )


@router.get("/{instance_id}/state")
def get_shared_app_state(
  instance_id: str,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  row = _instance_for(db, instance_id, principal)
  return shared_app_state.read_state_snapshot(db, row)


@router.get("/{instance_id}/changes")
def get_shared_app_changes(
  instance_id: str,
  after: int | None = Query(default=None, ge=0),
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  return shared_app_state.list_changes(
    db, _instance_for(db, instance_id, principal), after,
  )


@router.put("/{instance_id}/state/{path:path}", dependencies=[Depends(reject_cross_site)])
def put_shared_app_state(
  instance_id: str,
  path: str,
  body: SharedStateWrite,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  row = _instance_for(db, instance_id, principal, "editor")
  return shared_app_state.write_state(
    db, row, principal,
    path=path, value=body.value, delete=body.delete,
    expected_version=body.expected_version,
  )


@router.get("/{instance_id}/collaboration")
def get_shared_app_collaboration(
  instance_id: str,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  row = _instance_for(db, instance_id, principal)
  members = db.query(models.SharedAppMember).filter(
    models.SharedAppMember.instance_id == row.id,
    models.SharedAppMember.revoked_at.is_(None),
  ).order_by(models.SharedAppMember.joined_at.asc()).all()
  payload = {
    "role": principal.role,
    "members": [
      {"id": "owner", "display_name": principal.owner.username, "role": "owner", "you": principal.is_owner},
      *[
        {"id": item.id, "display_name": item.display_name, "role": item.role, "you": item.id == principal.member_id}
        for item in members
      ],
    ],
  }
  if principal.is_owner:
    payload["invites"] = [{
      "id": item.id,
      "invitee_name": item.invitee_name,
      "role": item.role,
      "expires_at": item.expires_at,
    } for item in db.query(models.SharedAppInvite).filter(
      models.SharedAppInvite.instance_id == row.id,
      models.SharedAppInvite.revoked_at.is_(None),
      models.SharedAppInvite.consumed_at.is_(None),
      models.SharedAppInvite.expires_at > now_naive_utc(),
    ).order_by(models.SharedAppInvite.created_at.desc()).all()]
  return payload


@router.post("/{instance_id}/invites", dependencies=[Depends(reject_cross_site)])
def create_shared_app_invite(
  instance_id: str,
  body: SharedAppInviteCreate,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  with PROJECT_LIFECYCLE_LOCK:
    return _create_shared_app_invite(instance_id, body, principal, db)


def _create_shared_app_invite(
  instance_id: str,
  body: SharedAppInviteCreate,
  principal: SharedAppPrincipal,
  db: Session,
):
  row = _instance_for(db, instance_id, principal, "owner")
  secret = secrets.token_urlsafe(32)
  now = now_naive_utc()
  invite = models.SharedAppInvite(
    id=str(uuid.uuid4()), instance_id=row.id, token_hash=_invite_hash(secret),
    invitee_name=body.invitee_name, role=body.role,
    created_at=now, expires_at=now + _INVITE_TTL,
  )
  db.add(invite)
  db.commit()
  return {
    "id": invite.id,
    "invitee_name": invite.invitee_name,
    "role": invite.role,
    "expires_at": invite.expires_at,
    "join_url": get_settings().frontend_origin.rstrip("/") + "/app-invite#" + secret,
  }


@router.delete(
  "/{instance_id}/invites/{invite_id}", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def revoke_shared_app_invite(
  instance_id: str,
  invite_id: str,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  row = _instance_for(db, instance_id, principal, "owner")
  invite = db.query(models.SharedAppInvite).filter(
    models.SharedAppInvite.id == invite_id,
    models.SharedAppInvite.instance_id == row.id,
    models.SharedAppInvite.revoked_at.is_(None),
    models.SharedAppInvite.consumed_at.is_(None),
  ).first()
  if invite is None:
    raise HTTPException(404, "Invitation not found.")
  invite.revoked_at = now_naive_utc()
  db.commit()


@router.patch(
  "/{instance_id}/members/{member_id}", dependencies=[Depends(reject_cross_site)],
)
def update_shared_app_member(
  instance_id: str,
  member_id: str,
  body: SharedAppMemberUpdate,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  row = _instance_for(db, instance_id, principal, "owner")
  member = db.query(models.SharedAppMember).filter(
    models.SharedAppMember.id == member_id,
    models.SharedAppMember.instance_id == row.id,
    models.SharedAppMember.revoked_at.is_(None),
  ).first()
  if member is None:
    raise HTTPException(404, "Member not found.")
  member.role = body.role
  db.commit()
  return {"id": member.id, "display_name": member.display_name, "role": member.role}


@router.delete(
  "/{instance_id}/members/{member_id}", status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def revoke_shared_app_member(
  instance_id: str,
  member_id: str,
  principal: SharedAppPrincipal = Depends(get_shared_app_principal),
  db: Session = Depends(get_db),
):
  row = _instance_for(db, instance_id, principal, "owner")
  member = db.query(models.SharedAppMember).filter(
    models.SharedAppMember.id == member_id,
    models.SharedAppMember.instance_id == row.id,
    models.SharedAppMember.revoked_at.is_(None),
  ).first()
  if member is None:
    raise HTTPException(404, "Member not found.")
  member.revoked_at = now_naive_utc()
  member.token_epoch += 1
  db.commit()
