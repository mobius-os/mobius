"""One-use owner-session handoff for a newly installed iOS PWA shell."""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, models
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_owner, reject_cross_site
from app.shell_install_pass import (
  COOKIE_NAME,
  COOKIE_PATH,
  GRANT_TTL,
  SESSION_TTL,
  ShellInstallPassGrant,
  hash_secret,
)
from app.timeutil import now_naive_utc


router = APIRouter(tags=["auth"])


def _cookie_secure() -> bool:
  return get_settings().frontend_origin.startswith("https://")


def _failure() -> JSONResponse:
  response = JSONResponse(
    {"detail": "This installation sign-in has expired."}, status_code=401,
  )
  response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
  response.headers["Cache-Control"] = "no-store"
  response.headers["Referrer-Policy"] = "no-referrer"
  return response


@router.post(
  "/shell-install-pass",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def prepare_shell_install_pass(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Prepare the owner session iOS copies into a new Home Screen shell."""
  now = now_naive_utc()
  db.query(ShellInstallPassGrant).filter(
    ShellInstallPassGrant.expires_at <= now,
  ).delete(synchronize_session=False)
  secret = ""
  for _attempt in range(3):
    secret = secrets.token_urlsafe(32)
    db.add(ShellInstallPassGrant(
      token_hash=hash_secret(secret),
      owner_id=owner.id,
      owner_epoch=owner.token_epoch,
      expires_at=now + GRANT_TTL,
    ))
    try:
      db.commit()
      break
    except IntegrityError:
      db.rollback()
  else:
    raise HTTPException(
      status_code=503, detail="Could not create a sign-in handoff.",
    )
  response = Response(status_code=204)
  response.set_cookie(
    COOKIE_NAME,
    secret,
    httponly=True,
    secure=_cookie_secure(),
    samesite="strict",
    max_age=int(GRANT_TTL.total_seconds()),
    path=COOKIE_PATH,
  )
  response.headers["Cache-Control"] = "no-store"
  response.headers["Referrer-Policy"] = "no-referrer"
  return response


@router.post(
  "/shell-install-pass/revoke",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def revoke_shell_install_passes(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Revoke copied-but-unspent shell handoffs before explicit sign-out."""
  now = now_naive_utc()
  db.query(ShellInstallPassGrant).filter(
    ShellInstallPassGrant.owner_id == owner.id,
    ShellInstallPassGrant.consumed_at.is_(None),
  ).update(
    {ShellInstallPassGrant.consumed_at: now},
    synchronize_session=False,
  )
  db.commit()
  response = Response(status_code=204)
  response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
  response.headers["Cache-Control"] = "no-store"
  response.headers["Referrer-Policy"] = "no-referrer"
  return response


@router.post(
  "/shell-install-pass/redeem",
  dependencies=[Depends(reject_cross_site)],
)
def redeem_shell_install_pass(
  request: Request,
  db: Session = Depends(get_db),
):
  """Atomically exchange the copied install cookie for an owner session."""
  secret = request.cookies.get(COOKIE_NAME, "")
  now = now_naive_utc()
  grant = db.query(ShellInstallPassGrant).filter(
    ShellInstallPassGrant.token_hash == hash_secret(secret),
  ).first() if secret else None
  if grant is None or grant.consumed_at is not None or grant.expires_at <= now:
    return _failure()

  owner = db.query(models.Owner).filter(models.Owner.id == grant.owner_id).first()
  if owner is None or owner.token_epoch != grant.owner_epoch:
    return _failure()

  consumed = db.query(ShellInstallPassGrant).filter(
    ShellInstallPassGrant.id == grant.id,
    ShellInstallPassGrant.consumed_at.is_(None),
    ShellInstallPassGrant.expires_at > now,
  ).update(
    {ShellInstallPassGrant.consumed_at: now},
    synchronize_session=False,
  )
  if consumed != 1:
    db.rollback()
    return _failure()
  db.commit()

  access_token = auth.create_access_token(
    {"sub": owner.username},
    token_epoch=owner.token_epoch,
    expires_delta=SESSION_TTL,
  )
  response = JSONResponse(
    {"access_token": access_token, "token_type": "bearer"},
  )
  response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
  response.headers["Cache-Control"] = "no-store"
  response.headers["Referrer-Policy"] = "no-referrer"
  return response
