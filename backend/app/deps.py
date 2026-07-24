"""FastAPI dependency functions."""

from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app import auth, models
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.timeutil import now_naive_utc

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def reject_cross_site(request: Request) -> None:
  """Defense-in-depth CSRF guard for state-changing endpoints.

  Möbius's baseline CSRF posture is "Authorization: Bearer + CORS" —
  cross-origin JS can't read the token from localStorage without a
  preflight that fails (allow_credentials=False, allow_origins is
  pinned). This dependency adds a second layer: reject requests whose
  `Sec-Fetch-Site` claims a cross-site origin, except authenticated fetches
  from Möbius's deliberately opaque app sandbox. Sandboxed app frames have
  `Origin: null` and browsers label even their same-host API calls as
  `Sec-Fetch-Site: cross-site`; requiring a valid app-scoped Bearer token
  keeps that narrow exception on the same boundary as the app APIs themselves.
  On ancient clients without the header we fall back to a same-origin
  Referer check before allowing the request through.

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
      origin = request.headers.get("origin")
      authorization = request.headers.get("authorization", "")
      scheme, _, token = authorization.partition(" ")
      if origin == "null" and scheme.lower() == "bearer" and token:
        payload = auth.decode_access_token(token)
        if payload and payload.get("scope") in {"app", "chat_embed"}:
          return
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
  app-scoped JWT, or None for non-app tokens. `scope` preserves the JWT scope
  so a narrow media token cannot be mistaken for a full owner token. Routes
  that gate on cross-app access (storage, app-attributed chats) read `app_id`
  to decide whether the caller is the app itself, a different app, or the owner.
  """
  owner: models.Owner
  app_id: int | None
  app_instance_id: str | None = None
  scope: str = "owner"
  chat_id: str | None = None
  embed_instance_id: str | None = None
  embed_session_id: str | None = None
  embed_role: str | None = None
  operations: frozenset[str] = frozenset()


def chat_embed_grant_is_latest_consumed(
  db: Session,
  grant: models.ChatEmbedGrant | None,
) -> bool:
  """Whether no higher-order grant for this frame has ever been consumed.

  A merely minted replacement is not authority: the old session remains valid
  until refresh exchange succeeds. Once any higher-id grant is consumed, the
  older row can never become authoritative again—even after that replacement
  expires or is explicitly revoked. This is the correctness rule; eager
  revoked_at updates during exchange are only cleanup.
  """
  if grant is None:
    return False
  newer_consumed = db.query(models.ChatEmbedGrant.token_hash).filter(
    models.ChatEmbedGrant.app_id == grant.app_id,
    models.ChatEmbedGrant.chat_id == grant.chat_id,
    models.ChatEmbedGrant.instance_id == grant.instance_id,
    models.ChatEmbedGrant.id > grant.id,
    models.ChatEmbedGrant.consumed_at.isnot(None),
  ).first()
  return newer_consumed is None


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
  uploads.serve_upload and media.serve_chat_media. They used to
  hand-roll decode + scope-reject + lookup, which silently skipped the
  revocation check — routing them through here keeps "sign out
  everywhere" effective on those surfaces too. `get_current_owner` is
  the same logic wired to the OAuth2 header dependency.
  """
  owner, payload = _resolve_owner(token, db)
  if payload.get("scope") is not None:
    raise HTTPException(
      status_code=403,
      detail="Only an owner token can access this endpoint.",
    )
  return owner


def resolve_media_or_header_owner(
  token: str, db: Session, *, chat_id: str, from_query: bool,
) -> models.Owner:
  """Resolves an owner for media-serving routes.

  The serve routes (uploads and chat media) accept the token from two
  sources: the Authorization header (Bearer) OR a `?token=` query param.
  The security fix is the asymmetry:

  - Header tokens may be any valid owner token (full scope, no chat_id check).
  - Query-param tokens MUST be media-scoped (`scope == "media"`) for the
    exact chat_id. An owner JWT in `?token=` is explicitly rejected — that's
    the point of this hardening.

  This prevents the 30-day owner JWT from leaking into server access logs,
  browser history, and Referer headers. A media token is 15 minutes, scoped
  to one chat, and only appears in URLs for that chat's own resources.

  Genuine unscoped owner tokens remain broad when carried in the header. Media
  and embedded-media tokens are accepted from either transport only for their
  exact ``media_chat``. Every other scoped principal is rejected centrally.
  """
  owner, payload = _resolve_owner(token, db)
  scope = payload.get("scope")
  if scope is None:
    if from_query:
      raise HTTPException(
        status_code=403,
        detail=(
          "Owner JWTs must not be passed as query parameters. "
          "Use a media token (POST /api/chats/{id}/media-token)."
        ),
      )
    return owner
  if scope not in {"media", "chat_embed_media"}:
    raise HTTPException(status_code=403, detail="Token scope is not valid for media.")
  if payload.get("media_chat") != chat_id:
    raise HTTPException(
      status_code=403,
      detail="Media token is not valid for this chat.",
    )
  if scope == "chat_embed_media":
    app_id = payload.get("app_id")
    app_nonce = payload.get("app_nonce")
    session_id = payload.get("embed_session")
    now = now_naive_utc()
    grant = db.query(models.ChatEmbedGrant).filter(
      models.ChatEmbedGrant.session_id == session_id,
    ).first()
    app = db.query(models.App).filter(
      models.App.id == app_id,
      models.App.deleted_at.is_(None),
    ).first() if isinstance(app_id, int) else None
    chat = db.query(models.Chat).filter(
      models.Chat.id == chat_id,
      models.Chat.deleted_at.is_(None),
    ).first()
    if (
      not isinstance(app_id, int)
      or not isinstance(app_nonce, str)
      or not isinstance(session_id, str)
      or grant is None
      or grant.revoked_at is not None
      or grant.session_expires_at is None
      or grant.session_expires_at <= now
      or grant.app_id != app_id
      or grant.app_nonce != app_nonce
      or grant.chat_id != chat_id
      or grant.owner_epoch != owner.token_epoch
      or app is None
      or app.token_nonce != app_nonce
      or chat is None
      or chat.created_by_app_id != app_id
      or not chat_embed_grant_is_latest_consumed(db, grant)
    ):
      raise HTTPException(
        status_code=401,
        detail="Embedded-chat media session is no longer valid.",
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


def _enforce_chat_embed_scope(
  payload: dict,
  db: Session,
  request_instance_id: str | None,
) -> dict | None:
  """Validate a dedicated embedded-chat session against live server state.

  Browser origins, frame/window identity and correlation ids are not consumed
  here as authorization. Authority is the signed ``chat_embed`` token plus the
  database-backed session row; the instance header is an additional binding
  carried by the authorized client, not a substitute for either.
  """
  if payload.get("scope") != "chat_embed":
    return None
  app_id = payload.get("app_id")
  app_nonce = payload.get("app_nonce")
  chat_id = payload.get("chat_id")
  instance_id = payload.get("embed_instance")
  session_id = payload.get("embed_session")
  role = payload.get("embed_role")
  operations = payload.get("embed_ops")
  if (
    not isinstance(app_id, int)
    or not isinstance(app_nonce, str)
    or not isinstance(chat_id, str)
    or not isinstance(instance_id, str)
    or not isinstance(session_id, str)
    or not isinstance(role, str)
    or not isinstance(operations, list)
    or not all(isinstance(op, str) for op in operations)
  ):
    raise HTTPException(status_code=401, detail="Malformed chat embed token.")
  if request_instance_id != instance_id:
    raise HTTPException(status_code=401, detail="Chat embed instance mismatch.")

  app = db.query(models.App).filter(
    models.App.id == app_id,
    models.App.deleted_at.is_(None),
  ).first()
  if app is None or app.token_nonce != app_nonce:
    raise HTTPException(status_code=401, detail="Chat embed app is no longer valid.")
  grant = db.query(models.ChatEmbedGrant).filter(
    models.ChatEmbedGrant.session_id == session_id,
  ).first()
  now = now_naive_utc()
  if (
    grant is None
    or grant.revoked_at is not None
    or grant.consumed_at is None
    or grant.session_expires_at is None
    or grant.session_expires_at <= now
    or grant.app_id != app_id
    or grant.app_nonce != app_nonce
    or grant.chat_id != chat_id
    or grant.instance_id != instance_id
    or grant.role != role
    or grant.owner_epoch != payload.get("epoch", 0)
    or frozenset(grant.operations_json or []) != frozenset(operations)
    or not chat_embed_grant_is_latest_consumed(db, grant)
  ):
    raise HTTPException(status_code=401, detail="Chat embed session is invalid.")
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if chat is None or chat.created_by_app_id != app_id:
    raise HTTPException(status_code=403, detail="Chat embed no longer owns this chat.")
  return {
    "app_id": app_id,
    "app_nonce": app_nonce,
    "chat_id": chat_id,
    "instance_id": instance_id,
    "session_id": session_id,
    "role": role,
    "operations": frozenset(operations),
  }


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
  if payload.get("scope") not in (None, "app"):
    raise HTTPException(status_code=403, detail="Token scope is not valid here.")
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


def authorize_current_owner_or_app_detached(
  token: str = Depends(_oauth2),
) -> None:
  """Authenticates owner/app tokens without holding a DB session afterward.

  Use this on long-running routes that only need an auth gate. The ordinary
  yield-based DB dependency closes after the response, so a slow external fetch
  can keep a pooled DB connection checked out for the whole network wait.
  """
  db = SessionLocal()
  try:
    resolve_owner_or_app(token, db)
  finally:
    db.close()


def get_principal(
  token: str = Depends(_oauth2),
  db: Session = Depends(get_db),
) -> Principal:
  """Resolve only the generic owner/app principal.

  Embedded-chat sessions are deliberately fail-closed here. Only the narrowly
  enumerated ChatView routes use ``get_chat_view_principal`` below; this keeps a
  valid exact-chat bearer from accidentally inheriting storage, app-management,
  GitHub, notification, logging, or future app-token powers.
  """
  owner, payload = _resolve_owner(token, db)
  if payload.get("scope") not in (None, "app"):
    raise HTTPException(status_code=403, detail="Token scope is not valid here.")
  app_id = _enforce_app_scope(payload, db)
  return Principal(
    owner=owner,
    app_id=app_id,
    app_instance_id=payload.get("app_nonce") if app_id is not None else None,
    scope="app" if app_id is not None else "owner",
  )


def get_chat_view_principal(
  token: str = Depends(_oauth2),
  db: Session = Depends(get_db),
  embed_instance_id: str | None = Header(
    default=None, alias="X-Mobius-Embed-Instance",
  ),
) -> Principal:
  """Resolve owner/app or a server-verified exact-chat embed principal.

  This dependency is intentionally private to the explicit ChatView route
  allowlist. Every call site must additionally invoke
  ``require_chat_embed_operation`` with the operation implemented by that
  endpoint; exact-chat ownership alone is never enough.
  """
  owner, payload = _resolve_owner(token, db)
  embed = _enforce_chat_embed_scope(payload, db, embed_instance_id)
  if embed is None:
    if payload.get("scope") not in (None, "app"):
      raise HTTPException(status_code=403, detail="Token scope is not valid here.")
    app_id = _enforce_app_scope(payload, db)
    return Principal(
      owner=owner,
      app_id=app_id,
      app_instance_id=payload.get("app_nonce") if app_id is not None else None,
      scope="app" if app_id is not None else "owner",
    )
  return Principal(
    owner=owner,
    app_id=embed["app_id"],
    app_instance_id=embed["app_nonce"],
    scope="chat_embed",
    chat_id=embed["chat_id"],
    embed_instance_id=embed["instance_id"],
    embed_session_id=embed["session_id"],
    embed_role=embed["role"],
    operations=embed["operations"],
  )


def get_owner_or_chat_embed_principal(
  principal: Principal = Depends(get_chat_view_principal),
) -> Principal:
  """Preserve an owner-only route while admitting its exact embed principal.

  Historically owner-only ChatView endpoints must continue rejecting generic
  app tokens during dependency resolution (before path/body validation). The
  dedicated chat-embed principal is the sole scoped exception.
  """
  if principal.scope == "app":
    raise HTTPException(status_code=403, detail="App token is not valid here.")
  return principal


def require_chat_embed_operation(principal: Principal, operation: str) -> None:
  """Require one operation only when the caller is a chat-embed session."""
  if principal.scope == "chat_embed" and operation not in principal.operations:
    raise HTTPException(
      status_code=403,
      detail=f"Chat embed operation not allowed: {operation}.",
    )


def chat_embed_session_is_active(session_id: str | None) -> bool:
  """Detached liveness check for long-lived embedded SSE responses."""
  if not session_id:
    return False
  db = SessionLocal()
  try:
    grant = db.query(models.ChatEmbedGrant).filter(
      models.ChatEmbedGrant.session_id == session_id,
    ).first()
    app = db.query(models.App).filter(
      models.App.id == grant.app_id,
      models.App.deleted_at.is_(None),
    ).first() if grant is not None else None
    chat = db.query(models.Chat).filter(
      models.Chat.id == grant.chat_id,
      models.Chat.deleted_at.is_(None),
    ).first() if grant is not None else None
    owner = db.query(models.Owner).first()
    return bool(
      grant is not None
      and grant.revoked_at is None
      and grant.consumed_at is not None
      and grant.session_expires_at is not None
      and grant.session_expires_at > now_naive_utc()
      and app is not None
      and app.token_nonce == grant.app_nonce
      and chat is not None
      and chat.created_by_app_id == grant.app_id
      and owner is not None
      and owner.token_epoch == grant.owner_epoch
      and chat_embed_grant_is_latest_consumed(db, grant)
    )
  finally:
    db.close()


def get_owner_app_or_chat_embed_for_models(
  principal: Principal = Depends(get_chat_view_principal),
) -> models.Owner:
  """Read-only model/provider registry dependency for owner/app/embed actors."""
  require_chat_embed_operation(principal, "models:read")
  return principal.owner


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
  the common gate for ladder-style app permissions (none/summary/full);
  the boolean grants below (manage_apps, github_access) use their own
  small owner-or-app gates instead.

  App frames receive only app-scoped JWTs and run in opaque-origin sandboxes.
  This live-row gate is therefore an enforceable authorization boundary, while
  also making the owner's consent visible and immediately revocable.

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


def get_owner_or_app_with_manage_skills(
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> models.Owner:
  """Owner JWT, OR an app-scoped JWT whose App row has manage_skills=true.

  The Skills mini-app is the canonical caller — it ships
  `permissions.manage_skills: true` so its own UI can install a skill from an
  online source (POST /api/skills/install) and uninstall an installed one
  (DELETE /api/skills/{name}) on the owner's behalf without holding the owner
  JWT directly. Any other app declaring the same permission inherits the trust.

  Permission is gated by the live App row, not the manifest the JWT was issued
  for — so revoking manage_skills (PATCH /api/apps/{id}) cuts off install
  access on the next request without rotating the JWT. A boolean gate like
  manage_apps, not a ladder.
  """
  if principal.app_id is None:
    return principal.owner
  app = db.query(models.App).filter(models.App.id == principal.app_id).first()
  if not app:
    raise HTTPException(status_code=401, detail="App not found.")
  if bool(app.manage_skills):
    return principal.owner
  raise HTTPException(
    status_code=403,
    detail=(
      "This app needs permissions.manage_skills=true in its manifest "
      "to install or uninstall skills on your behalf."
    ),
  )


def get_owner_or_app_with_github_access(
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> models.Owner:
  """Owner JWT, OR an app-scoped JWT whose App row has github_access=true.

  This is the GitHub data grant: the GET-only REST proxy, mutation-rejecting
  GraphQL proxy, sanitized local source status, and Contribute's narrow reviewed
  submit endpoints. Credential management is a separate github_connect grant.
  Neither capability can exfiltrate the stored token (INV1).

  Permission is read from the App row at request time (not baked into
  the JWT), so once the column is cleared access stops on the next
  request. Today the only thing that clears it is a reinstall from a
  manifest that no longer declares the grant — AppUpdate has no
  github_access field, so a plain PATCH can't toggle it.
  """
  owner = principal.owner
  if principal.app_id is None:
    # Every route behind this capability may proceed to GitHub network I/O.
    # Authorization is complete, so do not pin its pooled connection for the
    # upstream request's lifetime. Routes that also depend on this same Session
    # can reuse it normally; SQLAlchemy checks out a fresh connection on their
    # next query.
    db.close()
    return owner
  app = db.query(models.App).filter(models.App.id == principal.app_id).first()
  if not app:
    raise HTTPException(status_code=401, detail="App not found.")
  if bool(app.github_access):
    db.close()
    return owner
  raise HTTPException(
    status_code=403,
    detail=(
      "This app needs permissions.github_access=true in its manifest "
      "to read GitHub data or submit reviewed contributions on your behalf."
    ),
  )


def get_owner_or_app_with_github_connect(
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> models.Owner:
  """Owner JWT, or an app token with live GitHub credential authority."""
  owner = principal.owner
  if principal.app_id is None:
    db.close()
    return owner
  app = db.query(models.App).filter(models.App.id == principal.app_id).first()
  if not app:
    raise HTTPException(status_code=401, detail="App not found.")
  if bool(app.github_connect):
    db.close()
    return owner
  raise HTTPException(
    status_code=403,
    detail=(
      "This app needs permissions.github_connect=true in its manifest "
      "to manage the GitHub connection on your behalf."
    ),
  )


def get_owner_or_app_with_filesystem_access(
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> models.Owner:
  """Owner JWT, or an app token with live filesystem_access authority.

  The grant is deliberately narrow in identity but broad in filesystem scope:
  /api/fs still enforces its root, secret deny-list, symlink containment, and
  size limits. Reading the live App row makes reinstall/revocation effective on
  the next request without waiting for the eight-hour app token to expire.
  """
  if principal.app_id is None:
    return principal.owner
  app = db.query(models.App).filter(models.App.id == principal.app_id).first()
  if not app:
    raise HTTPException(status_code=401, detail="App not found.")
  if bool(app.filesystem_access):
    return principal.owner
  raise HTTPException(
    status_code=403,
    detail=(
      "This app needs permissions.filesystem_access=true in its manifest "
      "to use the owner filesystem."
    ),
  )
