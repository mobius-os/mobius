"""Durable project navigation state without mutating project content dates."""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc


def _state(db: Session, project_id: str) -> models.ProjectDrawerState:
  state = db.get(models.ProjectDrawerState, project_id)
  if state is not None:
    return state
  try:
    with db.begin_nested():
      state = models.ProjectDrawerState(project_id=project_id)
      db.add(state)
      db.flush()
      return state
  except IntegrityError:
    # A simultaneous first open/pin inserted the singleton in another request.
    state = db.get(models.ProjectDrawerState, project_id)
    if state is None:
      raise
    return state


def mark_opened(db: Session, project_id: str) -> datetime:
  """Advance one project's open marker inside the caller's transaction."""
  opened_at = now_naive_utc()
  advanced = db.execute(
    update(models.ProjectDrawerState)
    .where(
      models.ProjectDrawerState.project_id == project_id,
      (
        models.ProjectDrawerState.last_opened_at.is_(None)
        | (models.ProjectDrawerState.last_opened_at < opened_at)
      ),
    )
    .values(last_opened_at=opened_at)
  )
  if not advanced.rowcount:
    state = _state(db, project_id)
    if state.last_opened_at is None or state.last_opened_at < opened_at:
      state.last_opened_at = opened_at
  return opened_at


def mark_artifact_opened(db: Session, project_id: str, artifact_id: str) -> datetime:
  """Advance one built result's open marker inside the caller's transaction."""
  opened_at = now_naive_utc()
  key = (project_id, artifact_id)
  state = db.get(models.ProjectArtifactDrawerState, key)
  if state is None:
    try:
      with db.begin_nested():
        state = models.ProjectArtifactDrawerState(
          project_id=project_id,
          artifact_id=artifact_id,
          last_opened_at=opened_at,
        )
        db.add(state)
        db.flush()
        return opened_at
    except IntegrityError:
      state = db.get(models.ProjectArtifactDrawerState, key)
      if state is None:
        raise
  if state.last_opened_at < opened_at:
    state.last_opened_at = opened_at
  return opened_at


def set_pinned(db: Session, project_id: str, pinned: bool) -> datetime | None:
  """Set project pin state without advancing project ``updated_at``."""
  pinned_at = now_naive_utc() if pinned else None
  state = _state(db, project_id)
  state.pinned_at = pinned_at
  return pinned_at


def annotate_projects(
  db: Session, projects: list[models.Project],
) -> list[models.Project]:
  """Attach response-only drawer fields to project ORM rows."""
  ids = [project.id for project in projects]
  states = {}
  if ids:
    states = {
      row.project_id: row
      for row in db.query(models.ProjectDrawerState).filter(
        models.ProjectDrawerState.project_id.in_(ids),
      ).all()
    }
  for project in projects:
    state = states.get(project.id)
    project.last_opened_at = state.last_opened_at if state else None
    project.pinned_at = state.pinned_at if state else None
  artifact_states = db.query(models.ProjectArtifactDrawerState).filter(
    models.ProjectArtifactDrawerState.project_id.in_(ids),
  ).all() if ids else []
  opened_by_project: dict[str, dict[str, datetime]] = {}
  for state in artifact_states:
    opened_by_project.setdefault(state.project_id, {})[state.artifact_id] = state.last_opened_at
  for project in projects:
    project.artifact_last_opened_at = opened_by_project.get(project.id, {})
  return projects
