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


def _resolve_owner(
  token: str, db: Session
) -> tuple[models.Owner, dict]:
  """Decodes the JWT, loads the owner, and enforces token revocation.

  Returns the (owner, payload) pair every owner-resolving dependency
  needs. Centralizing the decode + lookup + epoch check here is what
  makes revocation unforgettable: there is no token-validation path
  that can skip the epoch comparison, because every dependency below
  goes through this one function.

  Revocation contract: a token carries the owner's `token_epoch` at
  mint time (see auth.create_access_token). "Sign out everywhere"
  bumps owner.token_epoch, so a stale token's stamped epoch falls
  behind and is rejected with 401 — the same status the frontend
  already treats as "clear token and return to login". A token minted
  before the epoch claim existed has no `epoch` and reads as 0, which
  matches a never-revoked owner (token_epoch defaults to 0), so legacy
  tokens keep working until the first bump.
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
  if payload.get("epoch", 0) != owner.token_epoch:
    raise HTTPException(status_code=401, detail="Token revoked.")
  return owner, payload


def resolve_owner_only(token: str, db: Session) -> models.Owner:
  """Resolves an owner from a raw token string, rejecting app scope.

  The owner-only counterpart to `_resolve_owner`, exposed for the two
  routes that take the token on a `?token=` query param instead of the
  Authorization header (img/iframe fetches can't set headers):
  uploads.serve_upload and generate.serve_generated_image. They used to
  hand-roll decode + scope-reject + lookup, which silently skipped the
  revocation check — routing them through here keeps "sign out
  everywhere" effective on those surfaces too. `get_current_owner` is
  the same logic wired to the OAuth2 header dependency.
  """
  owner, payload = _resolve_owner(token, db)
  if payload.get("scope") == "app":
    raise HTTPException(
      status_code=403,
      detail="App tokens cannot access this endpoint.",
    )
  return owner


def get_current_owner(
  token: str = Depends(_oauth2),
  db: Session = Depends(get_db),
) -> models.Owner:
  """Resolves the authenticated owner from the request JWT token.

  Rejects app-scoped tokens — use get_current_owner_or_app for
  routes that should be accessible to mini-apps.
  """
  return resolve_owner_only(token, db)


def resolve_owner_or_app(token: str, db: Session) -> models.Owner:
  """Resolves an owner (from a full OR app-scoped token string).

  The owner-or-app counterpart to `resolve_owner_only`, exposed for the
  module route in routes/apps.py, which takes the token on a `?token=`
  query param (iframe `import()` can't set headers) and deliberately
  accepts any valid token. Going through here applies the same
  revocation check the header dependencies use, so a signed-out token
  can't still pull module source.
  """
  owner, _ = _resolve_owner(token, db)
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
  return resolve_owner_or_app(token, db)


def get_principal(
  token: str = Depends(_oauth2),
  db: Session = Depends(get_db),
) -> Principal:
  """Same as get_current_owner_or_app but also exposes the token's app_id."""
  owner, payload = _resolve_owner(token, db)
  scope = payload.get("scope")
  app_id = payload.get("app_id") if scope == "app" else None
  # Invariant: an app-scoped token MUST carry an integer app_id. A signed
  # token with scope='app' but a null/absent app_id would resolve to
  # app_id=None and then read as an *owner* caller downstream (Principal
  # .app_id is the owner-vs-app discriminator, e.g. the /api/ai tool gate).
  # Reject it so every app-scope route can trust app_id is real.
  if scope == "app" and not isinstance(app_id, int):
    raise HTTPException(status_code=401, detail="Malformed app token.")
  # An app-scoped JWT outlives the app by up to its TTL. If the app was
  # uninstalled, the token must stop working at once — otherwise it could
  # keep reading, recreating, listing, or deleting the (now-orphan)
  # /data/apps/<id> storage tree for hours (Codex review #1). Mandatory row
  # existence IS the revocation mechanism: no row, no access. Owner tokens
  # (app_id is None) skip this — they aren't app-scoped.
  if app_id is not None and (
    not db.query(models.App.id).filter(models.App.id == app_id).first()
  ):
    raise HTTPException(status_code=401, detail="App no longer exists.")
  return Principal(owner=owner, app_id=app_id)


def get_owner_or_app_with_manage_apps(
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> models.Owner:
  """Owner JWT, OR an app-scoped JWT whose App row has manage_apps=true.

  The App Store mini-app is the canonical caller — it ships
  `permissions.manage_apps: true` in its manifest so it can drive
  installs (POST /api/apps/install) and uninstalls (DELETE
  /api/apps/{id}) on the owner's behalf without holding the owner
  JWT directly. Any other app declaring the same permission inherits
  the same trust.

  Permission is gated by the App row, not the manifest the JWT was
  issued for — so revoking manage_apps (PATCH /api/apps/{id}) cuts
  off install access on the next request without rotating the JWT.
  """
  if principal.app_id is None:
    return principal.owner
  app = db.query(models.App).filter(models.App.id == principal.app_id).first()
  if not app:
    raise HTTPException(status_code=401, detail="App not found.")
  if bool(app.manage_apps):
    return principal.owner
  raise HTTPException(
    status_code=403,
    detail=(
      "This app needs permissions.manage_apps=true in its manifest "
      "to install or uninstall apps on your behalf."
    ),
  )
