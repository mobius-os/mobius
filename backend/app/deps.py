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
  cross-app access (storage, app-attributed chats) read `app_id` to decide whether
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


def resolve_media_or_header_owner(
  token: str, db: Session, *, chat_id: str, from_query: bool,
) -> models.Owner:
  """Resolves an owner for media-serving routes.

  The serve routes (uploads, generated images) accept the token from two
  sources: the Authorization header (Bearer) OR a `?token=` query param.
  The security fix is the asymmetry:

  - Header tokens may be any valid owner token (full scope, no chat_id check).
  - Query-param tokens MUST be media-scoped (`scope == "media"`) for the
    exact chat_id. An owner JWT in `?token=` is explicitly rejected — that's
    the point of this hardening.

  This prevents the 30-day owner JWT from leaking into server access logs,
  browser history, and Referer headers. A media token is 15 minutes, scoped
  to one chat, and only appears in URLs for that chat's own resources.

  App tokens are rejected on both paths.
  """
  owner, payload = _resolve_owner(token, db)
  scope = payload.get("scope")
  if scope == "app":
    raise HTTPException(
      status_code=403,
      detail="App tokens cannot access media routes.",
    )
  if from_query:
    # Query-param path: only short-lived media tokens are accepted.
    # Owner JWTs on ?token= are the vulnerability being fixed.
    if scope != "media":
      raise HTTPException(
        status_code=403,
        detail=(
          "Owner JWTs must not be passed as query parameters. "
          "Use a media token (POST /api/chats/{id}/media-token)."
        ),
      )
    if payload.get("media_chat") != chat_id:
      raise HTTPException(
        status_code=403,
        detail="Media token is not valid for this chat.",
      )
  # Header path (from_query=False): any valid non-app owner token is accepted.
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


def _enforce_app_scope(payload: dict, db: Session) -> int | None:
  """Validates an app-scoped token's app identity; returns its app_id.

  Returns the int app_id for an app-scoped token, or None for an owner
  token. Raises 401 when the token is app-scoped but:
    - it carries no integer app_id (malformed — it would otherwise read
      as an owner caller downstream), or
    - the app no longer exists (uninstalled: the token must stop working
      at once so it can't keep touching the orphan storage tree), or
    - the token's stamped `app_nonce` no longer matches the row's
      `token_nonce` (the app was deleted and its integer id reused by a
      DIFFERENT app — the replacement has a fresh nonce, so the old
      token can't authenticate against it).

  Centralized here so EVERY app-accepting dependency (numeric per-app
  routes via get_principal AND shared/other routes via
  resolve_owner_or_app) enforces it identically — there is no
  app-token path that skips the check (Codex review #1, #2). A legacy
  token minted before the `app_nonce` claim existed has no nonce and
  falls back to row-existence only; such tokens expire within 8h.
  """
  if payload.get("scope") != "app":
    return None
  app_id = payload.get("app_id")
  if not isinstance(app_id, int):
    raise HTTPException(status_code=401, detail="Malformed app token.")
  # A tombstoned (soft-deleted) app has no live authority: its token must stop
  # working immediately, the same as a hard-deleted one did, so it can't write
  # storage during the recovery window. Revive (reinstall/recover) issues fresh
  # tokens. See feature 110.
  app = (
    db.query(models.App)
    .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if not app:
    raise HTTPException(status_code=401, detail="App no longer exists.")
  stamped = payload.get("app_nonce")
  if stamped is not None and stamped != app.token_nonce:
    raise HTTPException(status_code=401, detail="App token no longer valid.")
  return app_id


def resolve_owner_or_app(token: str, db: Session) -> models.Owner:
  """Resolves an owner (from a full OR app-scoped token string).

  The owner-or-app counterpart to `resolve_owner_only`, exposed for the
  module route in routes/apps.py, which takes the token on a `?token=`
  query param (iframe `import()` can't set headers) and deliberately
  accepts any valid token. Going through here applies the same
  revocation check the header dependencies use, so a signed-out token
  can't still pull module source, plus the app-scope validation so a
  deleted/reused-id app token can't read shared storage either.
  """
  owner, payload = _resolve_owner(token, db)
  _enforce_app_scope(payload, db)
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
  """Same as get_current_owner_or_app but also exposes the token's app_id.

  The app-scope validation (malformed app_id, deleted app, reused-id
  nonce mismatch) is shared with resolve_owner_or_app via
  _enforce_app_scope, so the numeric per-app routes and the shared
  routes reject a stale app token identically.
  """
  owner, payload = _resolve_owner(token, db)
  app_id = _enforce_app_scope(payload, db)
  return Principal(owner=owner, app_id=app_id)


# Ordered tiers for permission keys whose values form a ladder. Each
# request asks for a minimum level; the app passes iff its granted
# level is at or above it. `chat_log_access` is the first such ladder
# routed through require_app_permission; `cross_app_access` /
# `share_with_apps` keep their own bespoke min(A,B) check in
# routes/storage.py (two-sided, not a single-principal gate) and are
# deliberately NOT folded in here.
_PERMISSION_LADDERS: dict[str, dict[str, int]] = {
  "chat_log_access": {"none": 0, "summary": 1, "full": 2},
}


def require_app_permission(
  principal: Principal,
  key: str,
  level: str,
  db: Session,
) -> None:
  """Asserts the caller may use capability `key` at `level`, else 403.

  Owner tokens always pass — the permission map governs APPS, not the
  owner. For an app token, the granted level is read from the App row
  at request time (`getattr(app, key)`), so flipping the column revokes
  access on the very next call without rotating the 8h app JWT. This is
  the single gate every app-capability route should call; don't scatter
  inline `getattr(app, ...)` checks.

  Honest scope (design §0b): a same-origin app holds the owner JWT and
  could call owner routes directly. This gate is consent/attribution/
  audit for honest apps plus the enforceable half — the gated surface
  itself returns redacted/scoped data and refuses the un-consented app.
  It is not a sandbox.

  Raises:
    HTTPException: 403 if the app's granted level is below `level`, or
      if `app_id` no longer resolves to a live App row.
    KeyError: if `key`/`level` aren't a known ladder — a programming
      error at the call site, surfaced loudly rather than silently
      passing.
  """
  if principal.app_id is None:
    return  # owner token — the map governs apps, not the owner
  ladder = _PERMISSION_LADDERS[key]
  need = ladder[level]
  app = db.query(models.App).filter(models.App.id == principal.app_id).first()
  if app is None:
    raise HTTPException(status_code=401, detail="App not found.")
  granted = ladder.get((getattr(app, key, None) or "none").lower(), 0)
  if granted < need:
    raise HTTPException(
      status_code=403,
      detail=(
        f"This app's {key} is '{getattr(app, key, 'none')}' — "
        f"'{level}' is required for this request."
      ),
    )


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
