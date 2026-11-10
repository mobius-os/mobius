"""First-open consent: what an app asks for, and the owner's one-time answer."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import activity, app_consent, credits_client, credits_confirmations, models
from app.app_skills import deactivate_app_skills, restore_app_skills
from app.app_jobs import launch_app_job
from app.broadcast import get_system_broadcast
from app.database import get_db
from app.deps import (
  Principal, get_current_owner, get_current_owner_for_owner_input,
  get_owner_principal, get_principal, is_installation_owner_control,
  reject_cross_site, require_installation_owner_control,
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
  require_installation_owner_control(principal)
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
  require_installation_owner_control(principal)
  app = _live_app(db, app_id)
  if not app_consent.gated(app):
    raise HTTPException(status_code=409, detail="This app does not use first-open consent.")
  app_consent.revoke(app)
  db.commit()
  await deactivate_app_skills(app.id)
  activity.log_event("app_consent", app_id=app.id, slug=app.slug, action="revoked")
  get_system_broadcast().publish({"type": "app_updated", "appId": str(app.id)})
  return {"status": app_consent.status(app), "app_id": app.id}


# --- Every credits charge waits for the owner's Pay ---------------------------


class PaymentAnswer(BaseModel):
  pay: bool


@router.get("/{app_id}/credits-charges")
def read_waiting_payments(
  app_id: int,
  principal: Principal = Depends(get_owner_principal),
  db: Session = Depends(get_db),
):
  """The app's charges waiting for the owner's Pay or Cancel.

  These are the owner's private pending payments: the owner and the owner's own
  top-level agent see the list. A shared-browser guest or a delegated helper
  (the callers ``require_installation_owner_control`` refuses) authenticates but
  sees an empty list, never the payments and never an error, since the shell
  reads this for every app it opens and such a caller has nothing to answer.
  """
  app = _live_app(db, app_id)
  if not is_installation_owner_control(principal):
    return {"payments": []}
  return {"payments": credits_confirmations.waiting(app.id)}


@router.post("/{app_id}/credits-charges/{payment_id}", dependencies=[Depends(reject_cross_site)])
def answer_payment(
  app_id: int,
  payment_id: str,
  body: PaymentAnswer,
  owner: models.Owner = Depends(get_current_owner_for_owner_input),
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Pay or Cancel one waiting charge. Only a human owner can answer."""
  require_installation_owner_control(principal)
  app = _live_app(db, app_id)
  gone = HTTPException(status_code=409, detail={
    "code": "payment_gone",
    "message": "That payment is no longer waiting. Try it again in the app.",
  })
  payment = credits_confirmations.waiting_payment(app.id, payment_id)
  if payment is None:
    raise gone
  checkout_url = None
  purchase_id = ""
  if body.pay and payment.method == credits_confirmations.CARD:
    # Pay opens Stripe's page for exactly this price; the app's retries then
    # wait for it. If no page can be opened, the app hears why.
    try:
      started = credits_client.start_purchase(
        payment.seller, payment.priced_app, payment.action, payment.key,
        payment.max_price_micro, buyer_app=payment.buyer_app)
    except credits_client.CreditsUnavailable as error:
      refusal = {"code": error.code or "payment_failed", "message": error.message}
      credits_confirmations.answer(app.id, payment_id, True, error=refusal)
      raise HTTPException(status_code=409, detail=refusal) from error
    purchase_id = str(started.get("purchase_id") or "")
    checkout_url = started.get("checkout_url")
    if not purchase_id or not checkout_url:
      # Without both there is nothing to pay and nothing to follow: refuse now
      # rather than leave the app retrying until the question lapses.
      refusal = {"code": "payment_failed", "message": "Stripe's page could not be opened. Try again."}
      credits_confirmations.answer(app.id, payment_id, True, error=refusal)
      raise HTTPException(status_code=409, detail=refusal)
  if not credits_confirmations.answer(app.id, payment_id, body.pay, purchase_id=purchase_id):
    raise gone
  activity.log_event("app_credits_payment", app_id=app.id, slug=app.slug,
                     action=("paid" if body.pay else "cancelled"), method=payment.method)
  return {"app_id": app.id, "id": payment_id, "state": "confirmed" if body.pay else "declined",
          "checkout_url": checkout_url}
