"""Owner grants: proof that the owner confirmed one exact action, spent once.

An app that wants a sensitive owner action asks the shell. The shell shows its
own confirm sheet, outside the app's frame, and only on the owner's tap mints a
grant with the owner's own bearer (routes/contribution_owner_grants.py). The
grant is bound to the app, the action and the exact request, and is never
given to an app: the app repeats its normal request and the route spends the
matching grant through ``consume_owner_grant``.

Enforcement is opt-in per app: an app token needs a grant at a guarded route
only when the app's installed manifest declares ``"owner_confirm": true``
(``declares_owner_confirm``). Every other app keeps working as before.

Whatever did not match (no grant, another target, expired, already spent, an
app that was reinstalled, a sign-out everywhere) is refused with the same
words, so a refusal never says which part was wrong.

This proves a tap to apps and to agents acting through their own bearer. It
cannot stop code that can read the owner's token file: the same limit as the
platform's other owner-input routes.
"""

import hashlib
import json
from datetime import timedelta

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import activity, models
from app.applied_app_runtime import AppliedRuntimeUnavailable, runtime_root
from app.deps import Principal, acts_for_guest, is_owner_input_principal
from app.timeutil import now_naive_utc

ROUTE_TTL = timedelta(seconds=120)
# An agent may start its run well after the owner taps Post.
AGENT_TTL = timedelta(minutes=15)
RETENTION = timedelta(days=1)
# Actions spent by the agent run of the chat the grant names, not by an app's route.
AGENT_SPENDS = frozenset({"respond_post"})

OWNER_GRANT_REQUIRED = {
  "code": "owner_grant_required",
  "message": "This needs your confirmation in Möbius. Confirm it there, then try again.",
}


def sha256_json(value) -> str:
  canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def binding_sha256(action: str, app_id: int, scope: dict, body: BaseModel) -> str:
  """The exact request a grant covers.

  ``scope`` is the route's path parameters other than the app id and ``body``
  its own parsed request model, so preview, mint and the route cannot drift.
  """
  return sha256_json({
    "v": 1, "action": action, "app": app_id, "scope": scope, "body": body.model_dump(mode="json"),
  })


def mint(db: Session, owner: models.Owner, app_id: int, app_nonce: str, action: str,
         binding: str, seen: str) -> models.OwnerGrant:
  """Store one live grant for this exact request; asking again returns the live one, never a second."""
  now = now_naive_utc()
  db.query(models.OwnerGrant).filter(models.OwnerGrant.created_at < now - RETENTION).delete(
    synchronize_session=False)
  live = _live(db, now, owner.token_epoch, app_id, app_nonce, action, binding).first()
  if live is None:
    ttl = AGENT_TTL if action in AGENT_SPENDS else ROUTE_TTL
    live = models.OwnerGrant(
      owner_epoch=owner.token_epoch, app_id=app_id, app_nonce=app_nonce, action=action,
      binding_sha256=binding, seen_sha256=seen, expires_at=now + ttl)
    db.add(live)
  db.commit()
  activity.log_event("owner_grant", app_id=app_id, action=action, outcome="minted")
  return live


def _live(db: Session, now, owner_epoch: int, app_id: int, app_nonce: str, action: str, binding: str):
  """The grants for this request that are unspent, unexpired and from this owner epoch and app install."""
  return db.query(models.OwnerGrant).filter(
    models.OwnerGrant.app_id == app_id, models.OwnerGrant.action == action,
    models.OwnerGrant.binding_sha256 == binding, models.OwnerGrant.app_nonce == app_nonce,
    models.OwnerGrant.owner_epoch == owner_epoch, models.OwnerGrant.consumed_at.is_(None),
    models.OwnerGrant.expires_at > now)


def is_owner_hands(principal: Principal) -> bool:
  """The installation owner's own browser session: the one caller that may mint a grant and the one that needs none.

  Owner scope and owner input, so never an app, an agent or a helper, and not a
  shared-browser guest. A chat embed has owner input elsewhere, but its token
  sits in an app's frame, so it is not owner hands.
  """
  return principal.scope == "owner" and is_owner_input_principal(principal) and not acts_for_guest(principal)


def declares_owner_confirm(db: Session, app_id: int) -> bool:
  """Whether the app's installed manifest declares ``"owner_confirm": true``.

  Read from the accepted manifest in the app's frozen runtime tree, never from
  its editable source or anything the app sends, so only installing or
  updating the app changes it. It is not part of the capability contract, so
  declaring it adds no consent row and cannot make an installed app dormant.
  An app whose accepted manifest cannot be read has declared nothing.
  """
  app = db.get(models.App, app_id)
  if app is None:
    return False
  try:
    manifest = json.loads((runtime_root(app) / "mobius.json").read_bytes())
  except (AppliedRuntimeUnavailable, OSError, ValueError):
    return False
  return isinstance(manifest, dict) and manifest.get("owner_confirm") is True


def _spender(principal: Principal, app_id: int, action: str) -> str | None:
  """Who may spend a grant for this action, as the audit names them; None when this caller may not."""
  lineage = principal.chat_id or principal.run_id or principal.delegation_id
  if action in AGENT_SPENDS:
    agent_run = (principal.scope == "owner" and principal.app_id is None and principal.chat_id
                 and principal.run_id and principal.delegation_id is None)
    return f"agent:{principal.chat_id}:{principal.run_id}" if agent_run else None
  return f"app:{app_id}" if principal.app_id == app_id and principal.scope == "app" and not lineage else None


def _needs_no_grant(db: Session, principal: Principal) -> bool:
  """The owner's own session, and an app token of an app that has not declared ``owner_confirm``."""
  if is_owner_hands(principal):
    return True
  return principal.scope == "app" and principal.app_id is not None and not declares_owner_confirm(db, principal.app_id)


def consume_owner_grant(db: Session, principal: Principal, app_id: int | None, action: str,
                        body: BaseModel, **scope: str) -> None:
  """Spend the owner's grant for exactly this request, or raise 403 owner_grant_required.

  A guarded route calls it at the top, right after ``_require_owner_hands`` and
  before any GitHub validation, with the route's path parameters as ``scope``
  and its parsed request model as ``body``. The grant is bound to that exact
  body, so spending first is safe, and it means a request the route then
  refuses (the head moved, a stop, the PR machine's no) has burned the grant:
  the owner confirms again.

  The owner's own session needs no grant for a route action: it is the session
  that mints them. An app token needs one only when its app declares
  ``owner_confirm``; any other app token is left to the route's own guards, as
  before. Every other caller is refused. An agent spends only the actions in
  AGENT_SPENDS, and only for its own chat, which ``body`` names.
  """
  if action not in AGENT_SPENDS and _needs_no_grant(db, principal):
    return
  spender = _spender(principal, app_id, action)
  spent = spender is not None and _spend(
    db, principal.owner, app_id, action, binding_sha256(action, app_id, scope, body), spender)
  activity.log_event("owner_grant", app_id=app_id, action=action,
                     outcome="consumed" if spent else "refused", by=spender)
  if not spent:
    raise HTTPException(403, OWNER_GRANT_REQUIRED)


def _spend(db: Session, owner: models.Owner, app_id: int, action: str, binding: str, spender: str) -> bool:
  """Burn the matching grant. The conditional UPDATE decides a race: only one caller sees it unspent."""
  nonce = db.query(models.App.token_nonce).filter(
    models.App.id == app_id, models.App.deleted_at.is_(None)).scalar()
  if nonce is None:
    return False
  now = now_naive_utc()
  burned = _live(db, now, owner.token_epoch, app_id, nonce, action, binding).update(
    {models.OwnerGrant.consumed_at: now, models.OwnerGrant.consumed_by: spender},
    synchronize_session=False)
  db.commit()
  return burned > 0
