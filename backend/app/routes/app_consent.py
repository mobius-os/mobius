"""First-open consent: what an app asks for, and the owner's one-time answer."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import activity, app_consent, models
from app.app_skills import deactivate_app_skills, restore_app_skills
from app.app_jobs import launch_app_job
from app.broadcast import get_system_broadcast
from app.database import get_db
from app.deps import (
  Principal, get_current_owner, get_current_owner_for_owner_input, get_principal,
  reject_cross_site, require_installation_owner,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/apps", tags=["app-consent"])


class ConsentAccept(BaseModel):
  digest: str


def _live_app(db: Session, app_id: int) -> models.App:
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if app is None:
    raise HTTPException(status_code=404, detail="App not found.")
  return app


def _start_first_run(app: models.App) -> None:
  """Run the job an install could not start while the app was dormant."""
  background = (app.capability_contract or {}).get("background") or {}
  if not (background.get("initialize_on_install") and background.get("job") and app.source_dir):
    return
  source_dir = Path(app.source_dir)
  try:
    launch_app_job(app.id, source_dir / background["job"], source_dir)
  except Exception:
    log.exception("consent: first run failed to start for app %s", app.id)


@router.get("/{app_id}/consent")
def read_consent(
  app_id: int,
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """The popup's content, or why none is needed."""
  app = _live_app(db, app_id)
  state = app_consent.status(app)
  if state != "required":
    return {"status": state, "app_id": app.id}
  return {"status": state, **app_consent.build_request(db, app)}


@router.post("/{app_id}/consent", dependencies=[Depends(reject_cross_site)])
async def accept_consent(
  app_id: int,
  body: ConsentAccept,
  owner: models.Owner = Depends(get_current_owner_for_owner_input),
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Accept everything the app asks for. Only a human owner can answer.

  Agent runs, delegated helpers, shared-browser guests and apps are refused by
  the owner-input dependency, so an agent can declare access but never approve.
  """
  require_installation_owner(principal)
  app = _live_app(db, app_id)
  if not app_consent.gated(app):
    raise HTTPException(status_code=409, detail="This app does not use first-open consent.")
  first_time = app_consent.never_accepted(app)
  if not app_consent.accept(app, body.digest):
    raise HTTPException(status_code=409, detail={
      "code": "consent_changed",
      "message": "The app's request changed after it was shown. Review it again.",
    })
  db.commit()
  await restore_app_skills(app.id)
  activity.log_event(
    "app_consent", app_id=app.id, slug=app.slug, action="accepted",
    rows=[r["id"] for r in app.accepted_consent["rows"]],
  )
  if first_time:
    _start_first_run(app)
  get_system_broadcast().publish({"type": "app_updated", "appId": str(app.id)})
  return {"status": "accepted", "app_id": app.id}


@router.delete("/{app_id}/consent", dependencies=[Depends(reject_cross_site)])
async def revoke_consent(
  app_id: int,
  owner: models.Owner = Depends(get_current_owner_for_owner_input),
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Take back an app's acceptance: it is dormant again until accepted anew."""
  require_installation_owner(principal)
  app = _live_app(db, app_id)
  if not app_consent.gated(app):
    raise HTTPException(status_code=409, detail="This app does not use first-open consent.")
  app_consent.revoke(app)
  db.commit()
  await deactivate_app_skills(app.id)
  activity.log_event("app_consent", app_id=app.id, slug=app.slug, action="revoked")
  get_system_broadcast().publish({"type": "app_updated", "appId": str(app.id)})
  return {"status": app_consent.status(app), "app_id": app.id}
