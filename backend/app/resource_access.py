"""Shared resource-access helpers for route handlers.

Centralizes the `db.query(Chat).filter(Chat.id == ..., Chat.deleted_at
IS NULL).first()` pattern that multiple route files copy. A single
implementation means a future correctness fix (e.g. tightening the
soft-delete check) propagates everywhere instead of needing N edits.

Scope is intentionally narrow — ACTIVE chat reads only. Routes whose
lookup intentionally diverges from the soft-delete filter (the
delete flow at `routes/chats.py:376` queries by id without the
filter because it is actively setting `deleted_at`; the recover
flow at `routes/chats.py:392-395` queries with the INVERSE filter)
stay inline. This module is not the place to capture both behaviors
behind a flag — a flag would just push the special-case detail to
every caller.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.deps import Principal


def get_active_chat_for_principal(
  db: Session, chat_id: str, principal: Principal,
) -> models.Chat:
  """Fetches an active chat the principal may DRIVE, else 404/403.

  The actor gate for the app-attributed chat contract (design §1):
    - Owner tokens may drive ANY active chat (the column is an actor
      tag, not a fence against the owner).
    - An app token may drive ONLY a chat it created — i.e.
      `chat.created_by_app_id == principal.app_id`. Sending to or
      streaming a foreign chat (owner-created or another app's) is 403.

  This is the enforceable boundary that lets an app open and converse in
  its own chat without holding the keys to the owner's whole history.
  Reuse it everywhere an app-driven mutation touches a chat — don't
  re-derive the `created_by_app_id` comparison inline.

  Raises:
    HTTPException: 404 when the chat is missing/soft-deleted (same shape
      the owner sees, so an app can't probe existence of chats it can't
      reach); 403 when an app token targets a chat it doesn't own.
  """
  chat = get_active_chat_or_404(db, chat_id)
  if principal.scope == "chat_embed" and principal.chat_id != chat_id:
    raise HTTPException(
      status_code=403,
      detail="This embedded session is not valid for that chat.",
    )
  if principal.app_id is None:
    return chat  # owner drives anything
  if chat.created_by_app_id != principal.app_id:
    raise HTTPException(
      status_code=403,
      detail="This chat is not owned by your app.",
    )
  return chat


def get_active_chat_or_404(
  db: Session, chat_id: str,
) -> models.Chat:
  """Fetches a non-soft-deleted Chat by id, raising 404 otherwise.

  Sync (not async) because the underlying SQLAlchemy `Session` is
  sync — there is no I/O await to surface here, and a sync helper
  is callable from both sync and async route handlers (most chat
  routes are sync `def`; a few like `send_message` are `async def`).

  The Chat model has no `owner_id` column (single-owner installation;
  see `models.py:24-50`), so owner-scoping is not this helper's job —
  it happens upstream via `deps.get_current_owner` on the route.

  Args:
    db: SQLAlchemy session.
    chat_id: The chat id (string primary key).

  Returns:
    The matching Chat row.

  Raises:
    HTTPException: 404 when no row matches OR the row is soft-deleted.
  """
  chat = db.query(models.Chat).filter(
    models.Chat.id == chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if chat is None:
    raise HTTPException(status_code=404, detail="Chat not found.")
  return chat


def live_app(
  db: Session, app_id: int, *, populate: bool = False,
) -> models.App | None:
  """Fetches a non-tombstoned App by id, or None.

  The App analogue of `get_active_chat_or_404`'s filter, factored out because
  feature 110 made uninstall a reversible tombstone (`App.deleted_at`) and the
  "hide a tombstoned app" filter then scattered to ~10 sites in `routes/apps.py`
  plus `deps`/`standalone`/`main` — and a review still missed `validate_app`. A
  single definition makes the next app-read endpoint correct by construction.

  Non-raising (returns None) so token/scope checks that raise their OWN 401 can
  reuse it; route reads that want a 404 use `live_app_or_404`.

  The deliberate tombstone-VISIBLE paths stay inline so they read as special:
  `delete_app` (SETS deleted_at), `recover_app` (INVERSE filter), the purge
  sweep, and `allocate_unique_slug` (which MUST see tombstones to avoid reusing
  a still-claimed id/slug). Do not route those through here.

  `populate=True` forces `populate_existing()` so a caller about to MUTATE the
  row (update_app, update_icon) refreshes its identity-map copy instead of
  acting on a stale in-session object.
  """
  q = db.query(models.App)
  if populate:
    q = q.populate_existing()
  return q.filter(
    models.App.id == app_id,
    models.App.deleted_at.is_(None),
  ).first()


def live_app_or_404(
  db: Session, app_id: int, *, populate: bool = False,
) -> models.App:
  """Fetches a non-tombstoned App by id, raising 404 otherwise.

  Same 404 shape a missing app returns, so a tombstoned app can't be probed
  for existence by a caller that shouldn't see it.
  """
  app = live_app(db, app_id, populate=populate)
  if app is None:
    raise HTTPException(status_code=404, detail="App not found.")
  return app
