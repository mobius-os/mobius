"""Isolated, owner-authorized browser grants for a remote Möbius instance.

A grant lets one recipient use this instance until the owner revokes it. A
browser holds an HttpOnly refresh cookie for one ``BrowserAccessSession``;
renewal mints a short-lived JWT carrying a ``BrowserLineage``. Every bearer and
every piece of work derived from a guest keeps that lineage, and ``is_live`` is
the one rule that decides whether it may still act.

Revocation is terminal: nothing ever clears ``BrowserAccessGrant.revoked_at``.
That is what makes a grant id alone sufficient lineage (no generation number).
If grants ever need pause/resume, reintroduce a generation instead of clearing
``revoked_at``.

Functions commit their own transitions; a failed transition rolls back.
"""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, case, false, update
from sqlalchemy.orm import Session

from app.database import Base
from app.timeutil import now_naive_utc


SESSION_IDLE_TTL = timedelta(days=30)


class BrowserAccessGrant(Base):
  """Lasting permission for one labeled recipient; no grant expiry."""

  __tablename__ = "browser_access_grants"

  id = Column(String(64), primary_key=True)
  owner_id = Column(Integer, ForeignKey("owner.id"), nullable=False, index=True)
  label = Column(String(128), nullable=False)
  # Retired: revocation is terminal, so the grant id is the whole lineage.
  # The NOT NULL column stays because schema migrations are append-only.
  epoch = Column(Integer, nullable=False, default=0)
  created_at = Column(DateTime, nullable=False, default=now_naive_utc)
  revoked_at = Column(DateTime, nullable=True, default=None)
  kind = Column(String(16), nullable=False)
  issuer = Column(String(255), nullable=True)
  subject = Column(String(128), nullable=True)
  recipient_handle = Column(String(128), nullable=True)
  origin = Column(String(255), nullable=True)
  remote_status = Column(String(24), nullable=True)
  grantor_binding = Column(String(128), nullable=True)
  # The owner's per-recipient choice to also grant installation controls, as
  # Full access does for a connected Möbius. Read live on every check.
  full_access = Column(Boolean, nullable=False, default=False, server_default=false())


class BrowserAccessInvite(Base):
  """One-use, short-lived invitation; only SHA-256 of its secret persists."""

  __tablename__ = "browser_access_invites"

  id = Column(String(64), primary_key=True)
  grant_id = Column(String(64), ForeignKey("browser_access_grants.id"), nullable=False, index=True)
  secret_hash = Column(String(64), nullable=False, unique=True, index=True)
  owner_token_epoch = Column(Integer, nullable=False)
  created_at = Column(DateTime, nullable=False, default=now_naive_utc)
  expires_at = Column(DateTime, nullable=False)
  consumed_at = Column(DateTime, nullable=True, default=None)


class BrowserAccessSession(Base):
  """Server-checked, idle-expiring refresh authority, not a public OAuth token."""

  __tablename__ = "browser_access_sessions"

  id = Column(String(64), primary_key=True)
  grant_id = Column(String(64), ForeignKey("browser_access_grants.id"), nullable=False, index=True)
  secret_hash = Column(String(64), nullable=False, unique=True, index=True)
  owner_token_epoch = Column(Integer, nullable=False)
  created_at = Column(DateTime, nullable=False, default=now_naive_utc)
  idle_expires_at = Column(DateTime, nullable=False)
  revoked_at = Column(DateTime, nullable=True, default=None)


class BrowserAccountPending(Base):
  __tablename__ = "browser_account_pending"
  id = Column(String(64), primary_key=True)
  grant_id = Column(String(64), ForeignKey("browser_access_grants.id"), nullable=False, index=True)
  state_hash = Column(String(64), nullable=False, unique=True)
  cookie_hash = Column(String(64), nullable=False, unique=True, index=True)
  verifier = Column(String(128), nullable=False)
  nonce = Column(String(64), nullable=False)
  # Retired with the grant epoch; NOT NULL column kept by append-only schema.
  grant_epoch = Column(Integer, nullable=False, default=0)
  owner_token_epoch = Column(Integer, nullable=False)
  issuer = Column(String(255), nullable=False)
  subject = Column(String(128), nullable=False)
  expires_at = Column(DateTime, nullable=False, index=True)
  consumed_at = Column(DateTime, nullable=True)
  verified_at = Column(DateTime, nullable=True)
  verified_expires_at = Column(DateTime, nullable=True)


# ── Lineage ──────────────────────────────────────────────────────


def _valid_id(value) -> bool:
  return isinstance(value, str) and 1 <= len(value) <= 64


@dataclass(frozen=True)
class BrowserLineage:
  """The guest authority a bearer or a piece of work acts for.

  ``grant_id`` names the recipient grant. ``session_id`` is present only for
  bearers a browser holds directly, so logging that browser out ends them too;
  durable work (runs, delegations, Connect commands) carries the grant only.
  """

  grant_id: str
  session_id: str | None = field(default=None)

  def __post_init__(self):
    if not _valid_id(self.grant_id) or not (
      self.session_id is None or _valid_id(self.session_id)
    ):
      raise ValueError("Invalid browser lineage")

  @classmethod
  def of(cls, grant_id: str | None, session_id: str | None = None) -> "BrowserLineage | None":
    """Lineage from stored columns; no grant means owner-initiated."""
    return None if grant_id is None else cls(grant_id, session_id)

  def claims(self) -> dict[str, str]:
    claims = {"browser_grant": self.grant_id}
    if self.session_id is not None:
      claims["browser_session"] = self.session_id
    return claims

  @classmethod
  def from_claims(cls, payload: dict) -> "BrowserLineage | None":
    """Read JWT claims; any browser claim makes a malformed one fail closed.

    Tokens minted before the grant epoch retired also carry
    ``browser_grant_epoch``. It is ignored: a revoked grant has ``revoked_at``.
    """
    if not any(key in payload for key in ("browser_grant", "browser_grant_epoch", "browser_session")):
      return None
    try:
      return cls(payload.get("browser_grant"), payload.get("browser_session"))
    except ValueError as exc:
      raise _unauthorized() from exc


def _unauthorized() -> HTTPException:
  return HTTPException(status_code=401, detail="Browser access unavailable.")


def _fresh(db: Session, model, row_id):
  # Never decide liveness from a stale ORM identity-map row.
  return db.query(model).execution_options(populate_existing=True).filter_by(id=row_id).first()


def is_live(db: Session, lineage: BrowserLineage | None, owner_id: int) -> bool:
  """The one liveness rule for guest bearers and guest-attributed work.

  ``None`` lineage is owner-initiated and always live here. Otherwise the grant
  must belong to ``owner_id``, be unrevoked, and (for account grants) still be
  bound to the owner's current account link; a session must also be unrevoked,
  not idle-expired, and minted under the owner's current token epoch.
  """
  if lineage is None:
    return True
  if type(owner_id) is not int:
    return False
  grant = _fresh(db, BrowserAccessGrant, lineage.grant_id)
  if (grant is None or grant.owner_id != owner_id or grant.revoked_at is not None
      or not account_binding_holds(db, grant)):
    return False
  if lineage.session_id is None:
    return True
  session = _fresh(db, BrowserAccessSession, lineage.session_id)
  owner = _owner_or_none(db, owner_id)
  return (
    session is not None and owner is not None
    and session.grant_id == grant.id
    and session.revoked_at is None
    and session.idle_expires_at > now_naive_utc()
    and session.owner_token_epoch == owner.token_epoch
  )


def has_full_access(db: Session, lineage: BrowserLineage | None, owner_id: int) -> bool:
  """Whether this live guest lineage also holds installation controls.

  Read fresh on every check, so turning Full access off or revoking ends it
  at once, including for work the guest already started."""
  if lineage is None or not is_live(db, lineage, owner_id):
    return False
  grant = _fresh(db, BrowserAccessGrant, lineage.grant_id)
  return grant is not None and grant.full_access is True


def require_live(db: Session, lineage: BrowserLineage | None, owner_id: int) -> None:
  """Raise the uniform 401 unless ``is_live``."""
  if not is_live(db, lineage, owner_id):
    raise _unauthorized()


# ── Origins and account binding ──────────────────────────────────


def canonical_https_origin(value: str) -> str:
  """Pin browser grants to the same serialized HTTPS origin as browsers use."""
  if (not isinstance(value, str) or value != value.strip()
      or any(ord(char) <= 32 for char in value)
      or any(char in value for char in ("?", "#", "\\", "%"))):
    raise ValueError("invalid origin")
  parsed = urlsplit(value)
  if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
      or parsed.password is not None or parsed.path not in ("", "/")
      or parsed.query or parsed.fragment):
    raise ValueError("invalid origin")
  port = parsed.port  # Also rejects malformed or out-of-range ports.
  if port == 0 or parsed.netloc.endswith(":"):
    raise ValueError("invalid port")
  host = parsed.hostname
  if ":" in host:
    host = "[" + host + "]"
  return "https://" + host + (f":{port}" if port and port != 443 else "")


def runtime_origin() -> str:
  """This instance's browser origin; sharing requires a direct HTTPS address."""
  from app.config import get_settings
  try:
    return canonical_https_origin(get_settings().frontend_origin)
  except ValueError as exc:
    raise HTTPException(409, "Browser sharing requires a directly reachable HTTPS address.") from exc


def issuer_origin() -> str:
  from app.config import get_settings
  settings = get_settings()
  return (settings.mobius_sso_issuer if settings.mobius_sso_enabled else settings.mobius_account_origin).rstrip("/")


def digest(value: str) -> str:
  return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_secret(secret: str) -> str:
  if not isinstance(secret, str) or not 20 <= len(secret) <= 256:
    raise _unauthorized()
  return digest(secret)


def _owner_or_none(db: Session, owner_id: int):
  # Lazy import avoids an app.models / model-registration cycle.
  from app.models import Owner
  return _fresh(db, Owner, owner_id)


def _owner(db: Session, owner_id: int):
  owner = _owner_or_none(db, owner_id)
  if owner is None:
    raise _unauthorized()
  return owner


def account_binding(db: Session, owner_id: int) -> str | None:
  """Current local credential generation, never a reusable bearer."""
  from app.config import get_settings
  from app.models import IdentityAccountLink
  settings = get_settings()
  if settings.mobius_sso_enabled:
    owner = _owner_or_none(db, owner_id)
    if owner is None or not owner.sso_subject:
      return None
    material = "\0".join((
      settings.mobius_sso_instance_id, settings.mobius_sso_issuer,
      owner.sso_subject,
    ))
    return "managed:" + digest(material)
  link = db.query(IdentityAccountLink).execution_options(
    populate_existing=True,
  ).filter_by(owner_id=owner_id).one_or_none()
  if link is None:
    return None
  return "linked:" + digest(link.access_token_encrypted)


def account_binding_holds(db: Session, grant: BrowserAccessGrant) -> bool:
  """An account grant works only under the account link, issuer and address it
  was created for. A relink or address change leaves it inactive."""
  if grant.kind != "account":
    return True
  try:
    origin = runtime_origin()
  except HTTPException:
    return False
  return bool(
    grant.grantor_binding
    and grant.grantor_binding == account_binding(db, grant.owner_id)
    and grant.issuer == issuer_origin()
    and grant.origin == origin
  )


# ── Grants and sessions ──────────────────────────────────────────


def _lock_active_grant(db: Session, grant_id: str) -> BrowserAccessGrant:
  # A conditional write serializes redemption/renewal with revocation even on
  # SQLite, where SELECT FOR UPDATE has no effect. Never rely on a stale ORM row.
  changed = db.execute(update(BrowserAccessGrant).where(
    BrowserAccessGrant.id == grant_id,
    BrowserAccessGrant.revoked_at.is_(None),
  ).values(label=BrowserAccessGrant.label)).rowcount
  if changed != 1:
    raise _unauthorized()
  grant = _fresh(db, BrowserAccessGrant, grant_id)
  if not account_binding_holds(db, grant):
    raise _unauthorized()
  return grant


def open_session(db: Session, grant: BrowserAccessGrant, owner, previous_session_secret: str | None):
  """Add a session for this browser's single cookie slot; caller commits.

  Switching recipient retires only this browser's previous session, never
  another browser's or the installation owner's.
  """
  now = now_naive_utc()
  secret = secrets.token_urlsafe(32)
  session = BrowserAccessSession(
    id=secrets.token_urlsafe(24), grant_id=grant.id,
    secret_hash=_hash_secret(secret), owner_token_epoch=owner.token_epoch,
    created_at=now, idle_expires_at=now + SESSION_IDLE_TTL,
  )
  db.add(session)
  if previous_session_secret:
    db.execute(update(BrowserAccessSession).where(
      BrowserAccessSession.secret_hash == digest(previous_session_secret),
      BrowserAccessSession.revoked_at.is_(None),
    ).values(revoked_at=now))
  return secret, session


def redeem_invitation(db: Session, secret: str, *, previous_session_secret: str | None = None):
  """Atomically consume an invite; return (session_secret, session, grant, owner).

  Only links issued before link invitations were retired exist; each expires
  one day after it was issued.
  """
  try:
    now = now_naive_utc()
    invite = db.query(BrowserAccessInvite).filter_by(secret_hash=_hash_secret(secret)).first()
    if invite is None:
      raise _unauthorized()
    consumed = db.execute(update(BrowserAccessInvite).where(
      BrowserAccessInvite.id == invite.id,
      BrowserAccessInvite.consumed_at.is_(None),
      BrowserAccessInvite.expires_at > now,
    ).values(consumed_at=now)).rowcount
    if consumed != 1:
      raise _unauthorized()
    grant = _lock_active_grant(db, invite.grant_id)
    if grant.kind != "invitation":
      raise _unauthorized()
    owner = _owner(db, grant.owner_id)
    if owner.token_epoch != invite.owner_token_epoch:
      raise _unauthorized()
    session_secret, session = open_session(db, grant, owner, previous_session_secret)
    db.commit()
    return session_secret, session, grant, owner
  except Exception:
    db.rollback()
    raise


def renew_session(db: Session, secret: str):
  """Touch valid refresh session; return (grant, session, owner) for JWT minting."""
  try:
    now = now_naive_utc()
    session = db.query(BrowserAccessSession).filter_by(
      secret_hash=_hash_secret(secret),
    ).first()
    if session is None:
      raise _unauthorized()
    touched = db.execute(update(BrowserAccessSession).where(
      BrowserAccessSession.id == session.id,
      BrowserAccessSession.revoked_at.is_(None),
      BrowserAccessSession.idle_expires_at > now,
    ).values(idle_expires_at=now + SESSION_IDLE_TTL)).rowcount
    if touched != 1:
      raise _unauthorized()
    grant = _lock_active_grant(db, session.grant_id)
    owner = _owner(db, grant.owner_id)
    if owner.token_epoch != session.owner_token_epoch:
      raise _unauthorized()
    db.commit()
    return grant, session, owner
  except Exception:
    db.rollback()
    raise


def logout_session(db: Session, secret: str, *, grant_id: str | None = None) -> None:
  """Idempotently revoke a refresh session; unknown secrets reveal nothing."""
  if not isinstance(secret, str) or not 20 <= len(secret) <= 256:
    return
  try:
    if grant_id is not None:
      current = db.query(BrowserAccessSession).filter_by(secret_hash=_hash_secret(secret)).first()
      if current is not None and current.grant_id != grant_id:
        raise HTTPException(409, "Another shared session is active in this browser.")
    db.execute(update(BrowserAccessSession).where(
      BrowserAccessSession.secret_hash == _hash_secret(secret),
      BrowserAccessSession.revoked_at.is_(None),
    ).values(revoked_at=now_naive_utc()))
    db.commit()
  except Exception:
    db.rollback()
    raise


# ── Ending grants ────────────────────────────────────────────────


def _deny(db: Session, *where) -> None:
  """Revoke matching live grants in the caller's transaction.

  This is the only write to ``revoked_at`` and it only ever sets it. An account
  grant also becomes ``cleanup_pending`` until the directory confirms removal,
  so a crash before that step stays visible.
  """
  db.execute(update(BrowserAccessGrant).where(
    *where, BrowserAccessGrant.revoked_at.is_(None),
  ).values(
    revoked_at=now_naive_utc(),
    remote_status=case(
      (
        (BrowserAccessGrant.kind == "account")
        & BrowserAccessGrant.remote_status.is_distinct_from("revoked"),
        "cleanup_pending",
      ),
      else_=BrowserAccessGrant.remote_status,
    ),
  ))


def revoke_grant(db: Session, grant_id: str, owner_id: int) -> BrowserAccessGrant:
  """Idempotently and permanently revoke one of the owner's grants."""
  if not isinstance(grant_id, str) or type(owner_id) is not int:
    raise _unauthorized()
  try:
    _deny(db, BrowserAccessGrant.id == grant_id, BrowserAccessGrant.owner_id == owner_id)
    grant = _fresh(db, BrowserAccessGrant, grant_id)
    if grant is None or grant.owner_id != owner_id:
      raise _unauthorized()
    db.commit()
    return grant
  except Exception:
    db.rollback()
    raise


def revoke_account_grants(db: Session, owner_id: int) -> list[BrowserAccessGrant]:
  """Revoke every account grant in one transaction; return all account grants
  (including earlier revocations whose cleanup may still be pending)."""
  try:
    _deny(db, BrowserAccessGrant.owner_id == owner_id, BrowserAccessGrant.kind == "account")
    db.commit()
  except Exception:
    db.rollback()
    raise
  return db.query(BrowserAccessGrant).execution_options(populate_existing=True).filter_by(
    owner_id=owner_id, kind="account",
  ).all()


def stop_pending(db: Session, grant_id: str) -> bool:
  """Whether descendant work of a revoked grant is still being stopped."""
  from app.app_services import browser_grant_has_active_calls
  from app.chat import browser_grant_active_chat_ids
  from app.routes.connect import browser_grant_pending_commands
  return bool(
    browser_grant_active_chat_ids(db, grant_id)
    or browser_grant_has_active_calls(grant_id)
    or browser_grant_pending_commands(grant_id)
  )


@dataclass
class GrantEnd:
  """What remains after ending one revoked grant."""

  pending_commands: list = field(default_factory=list)
  pending_chat_ids: list = field(default_factory=list)
  stop_error: Exception | None = None
  directory_cleanup_pending: bool = False
  directory_credential_rejected: bool = False

  @property
  def stop_pending(self) -> bool:
    return bool(self.pending_commands or self.pending_chat_ids or self.stop_error)


async def end_grant(db: Session, grant: BrowserAccessGrant, *, contact_directory: bool = True) -> GrantEnd:
  """Stop everything a revoked grant started, then remove its directory entry.

  The caller must already have revoked the grant (deny first). Every step runs
  even when an earlier one fails, so one call does all the cleanup it can.
  """
  if grant.revoked_at is None:
    raise RuntimeError("end_grant requires a revoked grant")
  from app.app_services import cancel_browser_grant_calls
  from app.chat import stop_browser_grant_runs
  from app.routes.connect import cancel_browser_grant_commands
  result = GrantEnd()

  def failed(exc: Exception) -> None:
    # A failed step may leave the shared session unusable; the revocation is
    # already committed, so roll back and let the remaining steps still run.
    db.rollback()
    if result.stop_error is None:
      result.stop_error = exc

  try:
    result.pending_commands = cancel_browser_grant_commands(grant.id)
  except Exception as exc:
    failed(exc)
  try:
    await cancel_browser_grant_calls(grant.id)
  except Exception as exc:
    failed(exc)
  try:
    await stop_browser_grant_runs(grant.id, db)
  except HTTPException as exc:
    if isinstance(exc.detail, dict) and exc.detail.get("code") == "browser_grant_stop_incomplete":
      result.pending_chat_ids = exc.detail["chat_ids"]
    else:
      failed(exc)
  except Exception as exc:
    failed(exc)
  if grant.kind == "account" and grant.remote_status != "revoked":
    from app import account_browser_access
    outcome = "credential_rejected"
    if contact_directory:
      outcome = await account_browser_access.unregister(db, grant)
    grant.remote_status = "revoked" if outcome == "revoked" else "cleanup_pending"
    db.commit()
    result.directory_cleanup_pending = outcome != "revoked"
    result.directory_credential_rejected = outcome == "credential_rejected"
  return result
