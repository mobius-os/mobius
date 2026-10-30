"""Native location tracking: position ingest, history, and tracking state.

The Möbius mobile app exposes ``window.MobiusNative`` to the top-frame page on
the owner's own origin. The page is the only route to the server: it receives
``mobius:location`` batches, queues them, and POSTs them here. Positions arrive
in batches, out of order, and a rare batch may be delivered twice, so storage is
idempotent on ``(owner, device, device-fix timestamp)`` and the endpoint returns
2xx only after the batch is committed.

Writes take the owner session only; reads also accept an app that declares
``permissions.location_history_read``.

Endpoints:
  POST /api/location/batch       store a batch of positions
  GET  /api/location/positions   read stored positions, newest first
  DELETE /api/location/positions delete stored positions (owner only)
  GET  /api/location/tracking    read whether any phone is tracking
  PUT  /api/location/tracking    one phone records its own tracking state
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import (
  get_current_owner,
  get_owner_or_app_with_location_history_read,
  reject_cross_site,
)
from app.timeutil import now_naive_utc

router = APIRouter(prefix="/api/location", tags=["location"])

log = logging.getLogger(__name__)

# A batch may flush the whole on-device backlog (the app keeps up to 5000).
_BATCH_MAX = 5000
_DEVICE_ID_MAX = 128
# Reject fixes dated before 2001 or absurdly far in the future (ms since epoch).
_TS_MIN_MS = 1_000_000_000_000
_TS_MAX_MS = 4_102_444_800_000  # 2100-01-01


def _finite_or_none(value: float | None, name: str) -> float | None:
  if value is None:
    return None
  value = float(value)
  if value != value or value in (float("inf"), float("-inf")):
    raise ValueError(f"{name} must be a finite number")
  return value


class Position(BaseModel):
  """One device location fix. Only latitude/longitude are required; every other
  field is NULL when the device could not determine it."""

  latitude: float
  longitude: float
  timestamp: int = Field(description="ms since the epoch, from the device fix")
  accuracy: float | None = None
  altitude: float | None = None
  speed: float | None = None
  heading: float | None = None

  @field_validator("latitude")
  @classmethod
  def _lat(cls, v: float) -> float:
    v = float(v)
    if not (-90.0 <= v <= 90.0):
      raise ValueError("latitude must be between -90 and 90")
    return v

  @field_validator("longitude")
  @classmethod
  def _lon(cls, v: float) -> float:
    v = float(v)
    if not (-180.0 <= v <= 180.0):
      raise ValueError("longitude must be between -180 and 180")
    return v

  @field_validator("timestamp")
  @classmethod
  def _ts(cls, v: int) -> int:
    if not (_TS_MIN_MS <= int(v) <= _TS_MAX_MS):
      raise ValueError("timestamp must be plausible milliseconds since the epoch")
    return int(v)

  @field_validator("accuracy", "altitude", "speed", "heading")
  @classmethod
  def _optional_finite(cls, v, info):
    return _finite_or_none(v, info.field_name)


class LocationBatch(BaseModel):
  positions: list[Position] = Field(min_length=1, max_length=_BATCH_MAX)
  # The native app is a single device; the page may stamp a stable id so
  # multi-device owners keep per-device fixes distinct. "" when unset.
  device_id: str = Field(default="", max_length=_DEVICE_ID_MAX)


@router.post("/batch", dependencies=[Depends(reject_cross_site)])
def store_location_batch(
  body: LocationBatch,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> dict:
  """Durably store a batch of positions, idempotent on (owner, device, fix ms).

  Deduplicates within the batch (keeping the last fix per timestamp) so a single
  multi-row insert cannot collide with itself, then relies on the unique index
  plus ``ON CONFLICT DO NOTHING`` to drop positions already stored by an earlier
  delivery. Returns only after the insert commits.
  """
  device_id = body.device_id or ""
  # Collapse duplicates inside the batch first; later wins for a given fix time.
  by_ts: dict[int, Position] = {}
  for position in body.positions:
    by_ts[position.timestamp] = position
  rows = [
    {
      "owner_id": owner.id,
      "device_id": device_id,
      "device_timestamp_ms": position.timestamp,
      "latitude": position.latitude,
      "longitude": position.longitude,
      "accuracy": position.accuracy,
      "altitude": position.altitude,
      "speed": position.speed,
      "heading": position.heading,
      "received_at": now_naive_utc(),
    }
    for position in by_ts.values()
  ]
  try:
    result = db.execute(
      insert(models.LocationPosition.__table__)
      .values(rows)
      .on_conflict_do_nothing(
        index_elements=["owner_id", "device_id", "device_timestamp_ms"],
      )
    )
    db.commit()
  except Exception:
    db.rollback()
    log.exception("Failed to store location batch for owner %s", owner.id)
    raise HTTPException(503, "Location storage is temporarily unavailable.")
  stored = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else None
  return {"accepted": len(rows), "stored": stored}


_POSITIONS_MAX = 5000
_POSITIONS_DEFAULT = 1000


@router.get("/positions")
def list_positions(
  limit: int = _POSITIONS_DEFAULT,
  device_id: str | None = None,
  owner: models.Owner = Depends(get_owner_or_app_with_location_history_read),
  db: Session = Depends(get_db),
) -> dict:
  """Return the owner's stored positions, newest fix first.

  Readable by the owner and by an app holding the ``location_history_read``
  grant; an app token still resolves to this owner and sees only their rows.
  Positions are returned newest-first and capped so a long backlog cannot
  return an unbounded payload.
  """
  limit = max(1, min(int(limit), _POSITIONS_MAX))
  query = db.query(models.LocationPosition).filter(
    models.LocationPosition.owner_id == owner.id
  )
  if device_id is not None:
    query = query.filter(models.LocationPosition.device_id == device_id)
  rows = (
    query.order_by(models.LocationPosition.device_timestamp_ms.desc())
    .limit(limit)
    .all()
  )
  positions = [
    {
      "id": row.id,
      "device_id": row.device_id,
      "timestamp": row.device_timestamp_ms,
      "latitude": row.latitude,
      "longitude": row.longitude,
      "accuracy": row.accuracy,
      "altitude": row.altitude,
      "speed": row.speed,
      "heading": row.heading,
    }
    for row in rows
  ]
  return {"positions": positions, "count": len(positions), "limit": limit}


@router.delete("/positions", dependencies=[Depends(reject_cross_site)])
def delete_positions(
  before: int | None = None,
  device_id: str | None = None,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> dict:
  """Delete the owner's stored positions, optionally only some of them.

  ``before`` (ms since the epoch, exclusive) keeps newer fixes; ``device_id``
  limits deletion to one phone. With neither, the whole history is erased.
  Owner session only: ``location_history_read`` lets an app read history,
  never erase it.
  """
  query = db.query(models.LocationPosition).filter(
    models.LocationPosition.owner_id == owner.id
  )
  if before is not None:
    query = query.filter(models.LocationPosition.device_timestamp_ms < before)
  if device_id is not None:
    query = query.filter(models.LocationPosition.device_id == device_id)
  deleted = query.delete(synchronize_session=False)
  db.commit()
  return {"deleted": deleted}


class TrackingReport(BaseModel):
  """One phone's report of its own tracking state."""

  device_id: str = Field(min_length=1, max_length=_DEVICE_ID_MAX)
  enabled: bool
  options: dict = Field(default_factory=dict)
  # The phone's clock (ms) when it decided this change; orders its reports.
  reported_at: int = Field(ge=0)


class DeviceTracking(BaseModel):
  device_id: str
  enabled: bool
  options: dict


class TrackingState(BaseModel):
  """Owner-wide view: on when any phone is tracking.

  ``options`` are those of the most recently reported tracking phone (empty
  when none is tracking); ``devices`` lists every phone's own report.
  """

  enabled: bool
  options: dict = Field(default_factory=dict)
  devices: list[DeviceTracking] = Field(default_factory=list)


def _tracking_state(db: Session, owner_id: int) -> TrackingState:
  rows = (
    db.query(models.LocationTrackingDevice)
    .filter(models.LocationTrackingDevice.owner_id == owner_id)
    .order_by(models.LocationTrackingDevice.reported_at_ms.desc())
    .all()
  )
  devices = [
    DeviceTracking(
      device_id=row.device_id, enabled=bool(row.enabled),
      options=dict(row.options_json or {}),
    )
    for row in rows
  ]
  tracking = [device for device in devices if device.enabled]
  return TrackingState(
    enabled=bool(tracking),
    options=dict(tracking[0].options) if tracking else {},
    devices=devices,
  )


@router.get("/tracking")
def get_tracking_state(
  owner: models.Owner = Depends(get_owner_or_app_with_location_history_read),
  db: Session = Depends(get_db),
) -> TrackingState:
  """Report whether any of the owner's phones is tracking.

  Readable by an app holding the ``location_history_read`` grant so a viewer
  can show the current tracking status; reporting it (PUT) stays owner-only.
  """
  return _tracking_state(db, owner.id)


@router.put("/tracking", dependencies=[Depends(reject_cross_site)])
def report_tracking_state(
  body: TrackingReport,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> TrackingState:
  """Record one phone's own tracking state, for other surfaces to display.

  A report older than the one already stored for that phone is ignored, so a
  delayed request cannot undo a newer start, stop, or pace change.
  """
  devices = models.LocationTrackingDevice.__table__
  report = insert(devices).values(
    owner_id=owner.id,
    device_id=body.device_id,
    enabled=body.enabled,
    options_json=dict(body.options or {}),
    reported_at_ms=body.reported_at,
    updated_at=now_naive_utc(),
  )
  # Compare timestamps in the write so overlapping reports cannot undo a stop.
  db.execute(report.on_conflict_do_update(
    index_elements=["owner_id", "device_id"],
    set_={
      "enabled": report.excluded.enabled,
      "options_json": report.excluded.options_json,
      "reported_at_ms": report.excluded.reported_at_ms,
      "updated_at": report.excluded.updated_at,
    },
    where=report.excluded.reported_at_ms >= devices.c.reported_at_ms,
  ))
  db.commit()
  return _tracking_state(db, owner.id)
