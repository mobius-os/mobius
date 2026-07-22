"""POST /api/client-error — record an uncaught client/app JS error.

The shell's errorLog.js (and the in-iframe runtime reporter) POST here so
uncaught errors reach the activity log as `app_error` events, where the
nightly Reflection digest surfaces them per app. An app-scoped token attributes
the error to its `app_id` automatically; the owner JWT (a shell-level error)
records no `app_id`. Errors are sidecar telemetry — never load-bearing — so
this route mirrors activity.log_event's swallow-don't-propagate contract.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import activity
from app.chat_log_redaction import scrub_secrets
from app.deps import Principal, get_principal, reject_cross_site

router = APIRouter(prefix="/api/client-error", tags=["client-error"])

# Caps keep one error from bloating an activity.jsonl line: a real stack trace
# can be tens of KB, and the file carries 90 days of events. Truncate rather
# than reject so the signal survives bounded.
_MSG_MAX = 2000
_STACK_MAX = 8000
_WHERE_MAX = 200
_URL_MAX = 2000


class ClientError(BaseModel):
  message: str
  where: str | None = None
  stack: str | None = None
  url: str | None = None


@router.post("", status_code=204, dependencies=[Depends(reject_cross_site)])
def report_client_error(
  body: ClientError,
  principal: Principal = Depends(get_principal),
) -> None:
  """An app token carries `app_id`; the owner JWT does not (shell error).

  Debounced per (app_id, message) — on the truncated message, so two errors
  that differ only past the cap still collapse — so a render loop can't flood
  the log.
  """
  # Treat the server as the final retention boundary. A stale or hostile
  # client can bypass the frame scrubber, so scrub every retained text field
  # before debounce, truncation, and the activity.jsonl write.
  message = scrub_secrets(body.message)[:_MSG_MAX]
  if not activity.should_emit_app_error(principal.app_id, message):
    return  # debounced — already recorded within the window; still 204
  fields: dict[str, object] = {"message": message}
  if principal.app_id is not None:
    fields["app_id"] = principal.app_id
  if body.where:
    fields["where"] = scrub_secrets(body.where)[:_WHERE_MAX]
  if body.stack:
    fields["stack"] = scrub_secrets(body.stack)[:_STACK_MAX]
  if body.url:
    fields["url"] = scrub_secrets(body.url)[:_URL_MAX]
  activity.log_event("app_error", **fields)
