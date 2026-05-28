"""Helpers for emitting "unknown" SDK events on the SSE wire.

Both `claude_sdk_runner` and `codex_appserver` classify every event
they see from the underlying SDK and either translate it into a known
Möbius event shape or emit it as `unknown_sdk_event`. The default is to
EMIT unknowns rather than drop them — silently dropping data the SDKs
already provide costs us all observability into rate limits, usage,
warnings, thinking, etc. The `MOBIUS_EMIT_UNKNOWN` env var (default ON)
exists so a noisy session can be quieted at runtime without code edits.

The payload is repr-truncated and best-effort JSON-serialized so the
SSE wire stays cheap and resistant to weird object types. We do NOT
persist unknown events in the chat row's `blocks` (events.py's
`process_event` doesn't recognize the type — returns False — so the
sink broadcasts but skips DB write).
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Cap raw payload to keep SSE wire cheap and DB-immune. Unknown events
# are observational; the full SDK message is in chat.log if anyone
# really needs it.
_RAW_REPR_LIMIT = 400


def emit_unknown_enabled() -> bool:
  """Returns True when unknown events should be emitted on the wire.

  Default ON. Set ``MOBIUS_EMIT_UNKNOWN=0`` to suppress emission and
  keep only the DEBUG log line. Useful when a specific SDK version is
  flooding the wire with an event we haven't yet given a named home.
  """
  return os.getenv("MOBIUS_EMIT_UNKNOWN", "1") != "0"


def safe_payload(obj: Any) -> Any:
  """Returns a JSON-friendly, size-bounded snapshot of an SDK object.

  Dataclass instances are converted via ``dataclasses.asdict`` (which
  recurses into nested dataclasses); dicts/lists/primitives pass
  through; anything else falls back to ``repr()`` capped at
  ``_RAW_REPR_LIMIT``. The result is safe to pass through ``json.dumps``
  without raising for typical SDK message types.
  """
  if obj is None or isinstance(obj, (bool, int, float, str)):
    return obj
  if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
    try:
      return dataclasses.asdict(obj)
    except (TypeError, ValueError):
      pass
  if isinstance(obj, dict):
    return {str(k): safe_payload(v) for k, v in obj.items()}
  if isinstance(obj, (list, tuple)):
    return [safe_payload(v) for v in obj]
  text = repr(obj)
  if len(text) > _RAW_REPR_LIMIT:
    text = text[:_RAW_REPR_LIMIT] + "...(truncated)"
  return text


def unknown_event(kind: str, raw: Any) -> dict:
  """Builds the wire-shape dict for an unknown SDK event.

  Returned shape: ``{"type": "unknown_sdk_event", "kind": <str>, "raw":
  <safe_payload>}``. Callers publish (or include in a list of events)
  only when ``emit_unknown_enabled()`` returns True; the DEBUG log
  fires either way so noisy sessions are still inspectable.
  """
  log.debug("unknown SDK event: kind=%s raw=%r", kind, raw)
  return {
    "type": "unknown_sdk_event",
    "kind": kind,
    "raw": safe_payload(raw),
  }
