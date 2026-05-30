"""Admin / introspection endpoints (service-token-gated).

Today this is just the activity-log read endpoint feeding introspective
mini-apps (the curated `app-dreaming` cron agent in particular). If
more admin surfaces show up they belong here too — keep them all behind
`get_current_owner`, which rejects app-scoped JWTs so a compromised
mini-app can't pivot to the cross-app event feed.

The service-token at /data/service-token.txt is a 90-day owner JWT
minted at setup time, so authenticating with it passes the same
check the live shell uses. There is no separate "service-token
principal" — keeping one auth model reduces surface area.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import activity, models
from app.deps import get_current_owner

# Event names the emit endpoint will accept. Restricting at the
# boundary keeps the file's vocabulary closed (a typo or stray
# event-name from a future caller can't sneak in and silently grow
# the schema). Read endpoint doesn't filter on this — old log lines
# survive a future schema bump.
_KNOWN_EVENTS = {
  "app_open", "app_install", "storage_write", "cron_outcome",
}

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _parse_iso(value: str, label: str) -> datetime:
  """ISO8601 → tz-aware UTC datetime, or 400 with the field name.
  A naive (no-tz) input is treated as UTC — the read endpoint is for
  agents stitching together times from logs and `datetime.now()`,
  both of which produce UTC strings; forcing the caller to remember
  the suffix would be a footgun without any safety benefit."""
  try:
    dt = datetime.fromisoformat(value)
  except ValueError as exc:
    raise HTTPException(400, f"Invalid {label}: {exc}")
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
  return dt


@router.get("/activity")
def read_activity(
  since: str = Query(..., description="ISO8601 lower bound (inclusive)"),
  until: str | None = Query(None, description="ISO8601 upper bound; defaults to now"),
  app_id: int | None = Query(None, description="Filter to one app"),
  _owner: models.Owner = Depends(get_current_owner),
):
  """Streams the activity log as JSONL within [since, until], optionally
  filtered to one app_id.

  StreamingResponse so a wide time window doesn't buffer the full file
  into memory — the body is generated lazily as the underlying file
  scanner yields events. The response Content-Type is
  `application/x-ndjson` (the conventional MIME for newline-delimited
  JSON) so clients that auto-detect can stream-parse it.

  `since` is required by the ticket (400 if missing); FastAPI's
  `Query(...)` enforces that automatically. `until` defaults to `now`
  inside the handler rather than as a query default so the timestamp
  reflects when the read happened, not when the server started.
  """
  since_dt = _parse_iso(since, "since")
  until_dt = _parse_iso(until, "until") if until else datetime.now(timezone.utc)
  if until_dt < since_dt:
    raise HTTPException(400, "until must be >= since")

  def _iter():
    for ev in activity.read_events(since_dt, until_dt, app_id=app_id):
      yield json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"

  return StreamingResponse(_iter(), media_type="application/x-ndjson")


class ActivityEmit(BaseModel):
  """Body shape accepted by POST /api/admin/activity/emit.

  Fields beyond `ev` are passed straight through to log_event — we
  don't constrain shape here (the schema lives in activity.py's
  docstring + the read-side contract). `ts` is optional; emitter
  fills it in if missing.
  """
  ev: str
  ts: str | None = None
  app_id: int | None = None
  # Anything else (slug, source, path, size_delta, job, exit_code,
  # duration_ms, ...) is allowed; pydantic v2's model_config below
  # opens the model to extras so we don't have to enumerate every
  # field for every event type.
  model_config = {"extra": "allow"}


@router.post("/activity/emit", status_code=204)
def emit_activity_event(
  body: ActivityEmit,
  _owner: models.Owner = Depends(get_current_owner),
):
  """Lets cron scripts (and the rare server-external caller) record an
  activity event via the API instead of writing to the file directly.

  Routing every emitter through one process lets that process own the
  file handle, the rotation check, and the debounce cache — no
  cross-process flock needed. cron-emit.sh is the canonical caller;
  see backend/scripts/cron-emit.sh.

  Auth model. The service-token at /data/service-token.txt and the
  interactive owner JWT are the same shape (signed by SECRET_KEY,
  `sub=<username>`, no scope claim) — there is intentionally no
  separate "service principal" today, since Möbius is single-owner
  and adding a second principal type would multiply auth-surface for
  one endpoint. The trade-off: a logged-in owner *could* POST here
  via the browser fetch path and write events. That's accepted
  because:
    - the events are sidecar telemetry, never load-bearing,
    - _KNOWN_EVENTS bounds the vocabulary so a misuse can only emit
      one of the four already-legitimate event types,
    - rotation + 90-day retention bound disk pressure from spam,
    - same-origin CSRF baseline (Sec-Fetch-Site / CORS preflight)
      keeps cross-origin pages from emitting on the owner's behalf.
  If a future use case needs hard service/owner separation (e.g. an
  external probe with audit requirements), mint service tokens with
  an extra `scope: "service"` claim and gate this endpoint on it.

  Storage_write events are intentionally NOT debounced here —
  debounce is a request-handler concern (the storage PUT/DELETE
  handlers already decide whether to emit). External callers that
  want a debounced emit can implement that policy on their side.
  """
  if body.ev not in _KNOWN_EVENTS:
    raise HTTPException(400, f"Unknown event type: {body.ev!r}")
  fields: dict[str, Any] = body.model_dump(exclude_none=True)
  ev = fields.pop("ev")
  activity.log_event(ev, **fields)
