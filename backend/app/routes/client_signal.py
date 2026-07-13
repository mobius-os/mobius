"""POST /api/client-signal — ingest app-authored usage signals.

Signals are sidecar telemetry for Reflection, never load-bearing application
state. App identity comes exclusively from the scoped JWT. Batches carry stable
client IDs because the offline outbox may replay a request whose successful
response was lost; consumers deduplicate by that ID.
"""
from __future__ import annotations

import math
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app import activity
from app.deps import Principal, get_principal, reject_cross_site

router = APIRouter(prefix="/api/client-signal", tags=["client-signal"])

_BATCH_MAX = 100
_PAYLOAD_KEYS_MAX = 20
_PAYLOAD_KEY_MAX = 80
_PAYLOAD_STRING_MAX = 500
_EVENT_BYTES_MAX = 4096
_BATCH_BYTES_MAX = 128 * 1024
_RATE_WINDOW_SECONDS = 24 * 3600
_RATE_EVENTS_MAX = 200
_RATE_BYTES_MAX = 1024 * 1024
_rate_lock = threading.Lock()
_ingest_lock = threading.Lock()
_recent_by_app: dict[int, deque[tuple[object, float, int, int]]] = {}
_seen_ids_by_app: dict[int, dict[str, float]] = {}


def _reserve_rate(
  app_id: int, signal_sizes: list[tuple[str, int]],
) -> tuple[object, set[str]] | None:
  now = time.monotonic()
  cutoff = now - _RATE_WINDOW_SECONDS
  with _rate_lock:
    recent = _recent_by_app.setdefault(app_id, deque())
    while recent and recent[0][1] < cutoff:
      recent.popleft()
    seen = _seen_ids_by_app.setdefault(app_id, {})
    for signal_id, accepted_at in list(seen.items()):
      if accepted_at < cutoff:
        del seen[signal_id]
    novel: list[tuple[str, int]] = []
    batch_ids: set[str] = set()
    for signal_id, size in signal_sizes:
      if signal_id not in seen and signal_id not in batch_ids:
        novel.append((signal_id, size))
        batch_ids.add(signal_id)
    if not novel:
      return object(), set()
    count_used = sum(item[2] for item in recent)
    bytes_used = sum(item[3] for item in recent)
    batch_bytes = sum(size for _, size in novel)
    if (
      count_used + len(novel) > _RATE_EVENTS_MAX
      or bytes_used + batch_bytes > _RATE_BYTES_MAX
    ):
      return None
    token = object()
    recent.append((token, now, len(novel), batch_bytes))
    for signal_id, _ in novel:
      seen[signal_id] = now
    return token, {signal_id for signal_id, _ in novel}


def _rollback_rate(app_id: int, token: object, signal_ids: set[str]) -> None:
  with _rate_lock:
    recent = _recent_by_app.get(app_id)
    if recent is not None:
      _recent_by_app[app_id] = deque(row for row in recent if row[0] is not token)
    seen = _seen_ids_by_app.get(app_id, {})
    for signal_id in signal_ids:
      seen.pop(signal_id, None)


def _reset_for_tests() -> None:
  with _rate_lock:
    _recent_by_app.clear()
    _seen_ids_by_app.clear()


class ClientSignal(BaseModel):
  id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
  occurred_at: datetime
  name: str = Field(min_length=1, max_length=80)
  payload: dict[str, str | int | float | bool] = Field(default_factory=dict)

  @field_validator("payload")
  @classmethod
  def validate_payload(cls, value):
    if len(value) > _PAYLOAD_KEYS_MAX:
      raise ValueError(f"payload has more than {_PAYLOAD_KEYS_MAX} fields")
    for key, item in value.items():
      if not key or len(key) > _PAYLOAD_KEY_MAX:
        raise ValueError("payload keys must be 1-80 characters")
      if isinstance(item, str) and len(item) > _PAYLOAD_STRING_MAX:
        raise ValueError(
          f"payload strings must be <= {_PAYLOAD_STRING_MAX} characters"
        )
      if isinstance(item, float) and not math.isfinite(item):
        raise ValueError("payload numbers must be finite")
    return value

  @field_validator("occurred_at")
  @classmethod
  def validate_occurred_at(cls, value: datetime):
    if value.tzinfo is None:
      raise ValueError("occurred_at must include a timezone")
    if value > datetime.now(timezone.utc) + timedelta(minutes=5):
      raise ValueError("occurred_at cannot be more than five minutes in the future")
    return value

  def serialized_size(self) -> int:
    return len(json.dumps(
      self.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8"))

  @model_validator(mode="after")
  def validate_serialized_size(self):
    if self.serialized_size() > _EVENT_BYTES_MAX:
      raise ValueError(f"serialized signal must be <= {_EVENT_BYTES_MAX} bytes")
    return self


class ClientSignalBatch(BaseModel):
  signals: list[ClientSignal] = Field(min_length=1, max_length=_BATCH_MAX)

  @model_validator(mode="after")
  def validate_serialized_size(self):
    ids = [signal.id for signal in self.signals]
    if len(ids) != len(set(ids)):
      raise ValueError("signal IDs must be unique within a batch")
    if sum(signal.serialized_size() for signal in self.signals) > _BATCH_BYTES_MAX:
      raise ValueError(f"serialized signal batch must be <= {_BATCH_BYTES_MAX} bytes")
    return self


@router.post("", status_code=204, dependencies=[Depends(reject_cross_site)])
def report_client_signals(
  body: ClientSignalBatch,
  principal: Principal = Depends(get_principal),
) -> None:
  """Append a bounded signal batch to the platform activity stream.

  `ts` remains server-assigned ingestion time so delayed offline events cannot
  disturb log rotation. `occurred_at` preserves when the app actually emitted
  the signal and is the timestamp Reflection uses for its reporting window.
  """
  if principal.app_id is None:
    raise HTTPException(403, "Client signals require an app-scoped token.")
  # Serialize reserve → durable append → committed-ID visibility. A concurrent
  # replay cannot receive a dedupe 204 while the first copy is still capable of
  # failing its append and rolling back.
  with _ingest_lock:
    reservation = _reserve_rate(
      principal.app_id,
      [(signal.id, signal.serialized_size()) for signal in body.signals],
    )
    if reservation is None:
      raise HTTPException(429, "App signal rate limit exceeded; retry later.")
    token, novel_ids = reservation
    if not novel_ids:
      return
    events = [
      ("app_signal", {"app_id": principal.app_id, **signal.model_dump(mode="json")})
      for signal in body.signals if signal.id in novel_ids
    ]
    if not activity.log_events(events, durable=True):
      _rollback_rate(principal.app_id, token, novel_ids)
      raise HTTPException(503, "Signal activity storage is temporarily unavailable.")
