"""FastAPI dependency functions."""

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app import auth, models
from app.database import get_db

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def get_current_owner(
  token: str = Depends(_oauth2),
  db: Session = Depends(get_db),
) -> models.Owner:
  """Resolves the authenticated owner from the request JWT token.

  Rejects app-scoped tokens — use get_current_owner_or_app for
  routes that should be accessible to mini-apps.
  """
  payload = auth.decode_access_token(token)
  if not payload:
    raise HTTPException(status_code=401, detail="Invalid token.")
  if payload.get("scope") == "app":
    raise HTTPException(
      status_code=403,
      detail="App tokens cannot access this endpoint.",
    )
  owner = (
    db.query(models.Owner)
    .filter(models.Owner.username == payload.get("sub"))
    .first()
  )
  if not owner:
    raise HTTPException(status_code=401, detail="Owner not found.")
  return owner


def get_current_owner_or_app(
  token: str = Depends(_oauth2),
  db: Session = Depends(get_db),
) -> models.Owner:
  """Resolves the authenticated owner from either a full or app-scoped JWT.

  App-scoped tokens carry scope='app' and app_id claims but still
  resolve to the Owner record — they just have restricted route access
  enforced at the router level.
  """
  payload = auth.decode_access_token(token)
  if not payload:
    raise HTTPException(status_code=401, detail="Invalid token.")
  owner = (
    db.query(models.Owner)
    .filter(models.Owner.username == payload.get("sub"))
    .first()
  )
  if not owner:
    raise HTTPException(status_code=401, detail="Owner not found.")
  return owner
