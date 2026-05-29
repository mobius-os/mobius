"""Temporary observability endpoint for the PWA install flow.

The PWA install dance is opaque from the server's side — `beforeinstallprompt`
fires (or doesn't), Chrome's installed-app registry suppresses (or doesn't),
the user lands on /apps/<slug>/ in a minibrowser overlay (or a regular tab),
the standalone shell shows (or skips) its install UI. None of that is
visible from the backend, and reproducing it on the dev machine isn't
possible (the gestures and engagement counters are per-real-user-session).

This endpoint accepts small JSON beacons from the standalone shell + the
main Möbius shell, timestamps + IPs them, and appends one line per beacon
to `/data/logs/install.log`. The user does the install flow on their phone;
we read the log on the server to see what actually happened.

Public (no auth) by design — the flow includes pre-auth contexts (the
manifest fetch and the standalone shell paint before localStorage tokens
are guaranteed to be readable). Bodies are size-capped to keep this from
becoming a write amplifier.

Remove this route + its instrumentation once the install UX is stable.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(tags=["install_log"])

_LOG_PATH = Path("/data/logs/install.log")
_MAX_BODY = 4096  # 4 KB per beacon is plenty for {event, ctx} payloads.

_logger = logging.getLogger(__name__)


@router.post("/api/install-log", status_code=204)
async def install_log(request: Request) -> Response:
  """Append one JSON line to `/data/logs/install.log`.

  Body is the client's payload verbatim (parsed once for validation,
  re-serialized to drop any control chars). We add `_ts` (server time,
  ISO-8601 UTC), `_ip` (best-effort, may be the Caddy proxy IP), and
  `_ua` (User-Agent header, truncated).

  Returns 204 with no body. Failures are logged server-side but the
  endpoint still returns 204 so a buggy client doesn't loop on retries.
  """
  raw = await request.body()
  if len(raw) > _MAX_BODY:
    raw = raw[:_MAX_BODY]
  try:
    payload = json.loads(raw or b"{}")
    if not isinstance(payload, dict):
      payload = {"_raw": str(payload)[:200]}
  except json.JSONDecodeError:
    payload = {"_raw": raw[:200].decode("utf-8", errors="replace")}

  payload["_ts"] = datetime.now(timezone.utc).isoformat()
  # `request.client` can be None when behind certain proxies; fall back
  # to the X-Forwarded-For header that Caddy sets, then to "?".
  client_host = request.client.host if request.client else None
  payload["_ip"] = (
    client_host
    or request.headers.get("x-forwarded-for", "?").split(",")[0].strip()
  )
  ua = request.headers.get("user-agent", "")
  if len(ua) > 200:
    ua = ua[:200] + "…"
  payload["_ua"] = ua

  try:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_PATH.open("a", encoding="utf-8") as f:
      f.write(json.dumps(payload, ensure_ascii=False) + "\n")
  except OSError:
    # Disk full or permission flap — log + continue. Beacon loss is
    # acceptable; install flow must not stall on logging failure.
    _logger.exception("install_log: write failed")

  return Response(status_code=204)
