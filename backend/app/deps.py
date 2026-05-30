"""FastAPI dependency functions."""

from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app import auth, models
from app.config import get_settings
from app.database import get_db

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def reject_cross_site(request: Request) -> None:
  """Defense-in-depth CSRF guard for state-changing endpoints.

  Möbius's baseline CSRF posture is "Authorization: Bearer + CORS" —
  cross-origin JS can't read the token from localStorage without a
  preflight that fails (allow_credentials=False, allow_origins is
  pinned). This dependency adds a second layer that doesn't require
  token plumbing: reject any request whose `Sec-Fetch-Site` claims
  cross-site origin. Modern browsers always send this header; on
  ancient clients without it we fall back to a same-origin Referer
  check before allowing the request through.

  Apply to POST/PATCH/DELETE endpoints that mutate owner state. Read-
  only GETs don't need it (CORS already gates them).

  See CLAUDE.md "CSRF policy for state-changing endpoints" section.
  """
  sec_fetch_site = request.headers.get("sec-fetch-site")
  if sec_fetch_site is not None:
    # Browsers send "same-origin" for fetch from the page, "same-site"
    # for sibling subdomains, "none" for user-initiated navigations
    # (address-bar typing), "cross-site" for genuine cross-origin
    # attacks. Same-origin + none + same-site are all OK; only
    # cross-site is rejected.
    if sec_fetch_site == "cross-site":
      raise HTTPException(
        status_code=403,
        detail="Cross-site request blocked.",
      )
    return
  # Fallback for ancient browsers that don't send Sec-Fetch-Site.
  # Require the Referer (or Origin) to match the configured
  # frontend_origin. Missing both is allowed — same-origin GETs
  # often omit Referer, and a missing header is not by itself
  # evidence of cross-site abuse.
  referer = request.headers.get("referer") or request.headers.get("origin")
  if not referer:
    return
  expected = get_settings().frontend_origin
  try:
    ref_host = urlparse(referer).netloc
    exp_host = urlparse(expected).netloc
  except Exception:
    return
  if exp_host and ref_host and ref_host != exp_host:
    raise HTTPException(
      status_code=403,
      detail="Cross-origin referer blocked.",
    )


@dataclass
class Principal:
  """The authenticated caller, with the token's app scope if any.

  `owner` is always set. `app_id` is the `app_id` claim from an
  app-scoped JWT, or None for full owner tokens. Routes that gate on
  cross-app access (storage, AI proxy) read `app_id` to decide whether
  the caller is the app itself, a different app, or the owner.
  """
  owner: models.Owner
  app_id: int | None


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

  When you need the token's `app_id` claim (e.g. for cross-app
  scoping), use `get_principal` instead — it returns a Principal with
  both the owner and the app_id.
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


def get_principal(
  token: str = Depends(_oauth2),
  db: Session = Depends(get_db),
) -> Principal:
  """Same as get_current_owner_or_app but also exposes the token's app_id."""
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
  scope = payload.get("scope")
  app_id = payload.get("app_id") if scope == "app" else None
  # Invariant: an app-scoped token MUST carry an integer app_id. A signed
  # token with scope='app' but a null/absent app_id would resolve to
  # app_id=None and then read as an *owner* caller downstream (Principal
  # .app_id is the owner-vs-app discriminator, e.g. the /api/ai tool gate).
  # Reject it so every app-scope route can trust app_id is real.
  if scope == "app" and not isinstance(app_id, int):
    raise HTTPException(status_code=401, detail="Malformed app token.")
  return Principal(owner=owner, app_id=app_id)
