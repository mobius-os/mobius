"""Which ids name a public app service, readable without a query per request.

The anonymous service door must refuse an over-limit caller, and answer a
probe of a made-up id, without a database lookup each time. It reads this
snapshot instead: every live id (stable service id, transition alias, or a
legacy slug) whose accepted contract is public, with that service's rate
bucket and allowance.

A committed change to an app or a service alias in this process discards the
snapshot, so an applied, removed, or re-accepted service (including a raised
allowance) counts from its next request. A change written by another process
(a script or scheduled job writing the database; the API itself is a single
uvicorn worker) is picked up within ``OUT_OF_PROCESS_RECHECK_SECONDS``, or
sooner when the door resolves a service that no longer matches its entry and
calls ``discard``.

The door charges only the bucket of the service an id resolves to now, so a
stale entry never spends another service's allowance. It refuses from the
snapshot alone only for an id the snapshot has ``confirm``ed against the
database; an unconfirmed id is resolved first, at most once per snapshot, so
a moved alias or raised allowance is never refused against an old bucket.
"""

from __future__ import annotations

import threading
import time
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app import app_services, models


SERVICE_TABLES = frozenset({
  models.App.__tablename__,
  models.AppServiceAlias.__tablename__,
})
OUT_OF_PROCESS_RECHECK_SECONDS = 30.0
PROBE_BUCKET = "unknown"
_PENDING_KEY = "mobius_public_services_changed"

_lock = threading.Lock()
_rebuild_lock = threading.Lock()
_generation = 0


class Snapshot(NamedTuple):
  generation: int
  built_at: float
  index: dict[str, tuple[str, int]]
  # Ids whose entry a request has matched against the database.
  confirmed: set[str]


_snapshot: Snapshot | None = None


def bucket(app, service: dict) -> tuple[str, int]:
  """The rate bucket and per-minute allowance one request is counted in."""
  if app is None:
    return PROBE_BUCKET, app_services.public_requests_per_minute({})
  return str(app.id), app_services.public_requests_per_minute(service)


def lookup(db: Session, service_id: str) -> tuple[tuple[str, int] | None, Snapshot]:
  """The bucket and allowance of the public service ``service_id`` names.

  Also returns the snapshot that answered, for ``discard``.
  """
  global _snapshot
  current = _current()
  if current is None:
    # One rebuild at a time: requests that find the snapshot stale while
    # another rebuilds wait for it and reuse its result instead of scanning.
    with _rebuild_lock:
      current = _current()
      if current is None:
        with _lock:
          generation = _generation
        current = Snapshot(generation, time.monotonic(), _build(db), set())
        with _lock:
          # A change committed while building leaves this snapshot already stale.
          _snapshot = current
  return current.index.get(service_id), current


def discard(seen: Snapshot) -> None:
  """Drop ``seen`` after finding it out of date, so the next request rebuilds.

  Only that exact snapshot is dropped: a slower request still holding an
  older one must not discard the fresh snapshot another request rebuilt.
  """
  global _snapshot
  with _lock:
    if _snapshot is seen:
      _snapshot = None


def confirm(seen: Snapshot, service_id: str) -> None:
  """Record that ``service_id``'s entry in ``seen`` matches the database."""
  with _lock:
    seen.confirmed.add(service_id)


def confirmed(seen: Snapshot, service_id: str) -> bool:
  """Whether ``seen`` may refuse ``service_id`` without resolving it."""
  with _lock:
    return service_id in seen.confirmed


def _current() -> Snapshot | None:
  with _lock:
    generation, current = _generation, _snapshot
  if (
    current is not None and current.generation == generation
    and time.monotonic() - current.built_at < OUT_OF_PROCESS_RECHECK_SECONDS
  ):
    return current
  return None


def _build(db: Session) -> dict[str, tuple[str, int]]:
  # Mirrors routes.app_services._service_app's precedence: a stable service id
  # wins over a transition alias, which wins over a legacy slug.
  public: dict[int, tuple[str, int]] = {}
  # Plain columns, not App entities: no stored source is loaded, and the
  # request's session keeps no partially loaded app.
  apps = (
    db.query(
      models.App.id, models.App.slug, models.App.service_id,
      models.App.capability_contract,
    )
    .filter(models.App.deleted_at.is_(None))
    .all()
  )
  for app in apps:
    try:
      public[app.id] = bucket(app, app_services.service_contract(app, access="public"))
    except HTTPException:
      continue
  index: dict[str, tuple[str, int]] = {}
  for app in apps:
    if app.service_id is None and app.id in public:
      index[app.slug] = public[app.id]
  aliases = db.query(models.AppServiceAlias.service_id, models.AppServiceAlias.app_id)
  for alias_id, app_id in aliases.all():
    if app_id in public:
      index[alias_id] = public[app_id]
  for app in apps:
    if app.service_id is not None and app.id in public:
      index[app.service_id] = public[app.id]
  return index


def _touches_services(instance) -> bool:
  return getattr(inspect(instance).mapper.local_table, "name", None) in SERVICE_TABLES


@event.listens_for(Session, "after_flush")
def _mark_flushed_change(session, _flush_context) -> None:
  if any(
    _touches_services(obj)
    for obj in (*session.new, *session.dirty, *session.deleted)
  ):
    session.info[_PENDING_KEY] = True


@event.listens_for(Session, "do_orm_execute")
def _mark_statement_change(state) -> None:
  if not (state.is_insert or state.is_update or state.is_delete):
    return
  table = getattr(state.statement, "table", None)
  if getattr(table, "name", None) in SERVICE_TABLES:
    state.session.info[_PENDING_KEY] = True


@event.listens_for(Session, "after_commit")
def _publish_committed_change(session) -> None:
  # A released savepoint is not yet visible to other sessions.
  if session.in_nested_transaction():
    return
  if session.info.pop(_PENDING_KEY, False):
    global _generation
    with _lock:
      _generation += 1


@event.listens_for(Session, "after_transaction_end")
def _forget_uncommitted_change(session, transaction) -> None:
  if transaction.parent is None:
    session.info.pop(_PENDING_KEY, None)
