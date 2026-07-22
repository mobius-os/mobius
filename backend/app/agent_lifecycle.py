"""Normalized, append-only subagent lifecycle persistence.

Provider runners emit small lifecycle facts through ``_ChatEventSink``.  This
module turns those wire facts into one provider-independent row shape and owns
the transition rules used by the chat-writer actor:

* semantic event keys make redelivery idempotent;
* stable agent identity is separate from one root-run activation;
* unique facts stay append-only even when provider timestamps arrive late;
* summaries are bounded and secret-scrubbed before persistence;
* prompt bodies are deliberately not part of this table.

``id`` on the model is ingestion order for pagination only.  Consumers order
the visual timeline by ``occurred_at`` when the provider supplied it, then by
``observed_at`` and ``id``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app import models
from app.chat_log_redaction import scrub_secrets
from app.timeutil import now_naive_utc


SUMMARY_CHARS = 500
AGENT_TYPE_CHARS = 64
PROVIDER_ID_CHARS = 160

EVENT_TYPES = frozenset({
  "agent_spawned",
  "agent_started",
  "agent_terminal",
})
def _clip(value: Any, cap: int) -> str | None:
  if value is None:
    return None
  text = str(value).strip()
  if not text:
    return None
  return text[:cap]


def _summary(value: Any) -> str | None:
  text = _clip(value, SUMMARY_CHARS * 2)
  if text is None:
    return None
  text = scrub_secrets(text).replace("\x00", "").strip()
  return text[:SUMMARY_CHARS] or None


def _naive_utc(value: Any) -> datetime | None:
  """Normalize a provider timestamp; absent/unparseable remains unknown."""
  if value is None:
    return None
  if isinstance(value, datetime):
    dt = value
  elif isinstance(value, (int, float)) and not isinstance(value, bool):
    seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
    try:
      dt = datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
      return None
  elif isinstance(value, str) and value.strip():
    raw = value.strip().replace("Z", "+00:00")
    try:
      dt = datetime.fromisoformat(raw)
    except ValueError:
      return None
  else:
    return None
  if dt.tzinfo is not None:
    dt = dt.astimezone(UTC).replace(tzinfo=None)
  return dt


def stable_agent_id(
  provider: str, provider_session_id: str | None, provider_agent_id: str,
) -> str:
  """Opaque stable identity; Codex thread ids are globally scoped.

  Claude task ids are scoped to their provider session, while Codex child
  thread ids are already globally unique and can appear with different
  session-tree identifiers on different notification variants.
  """
  if provider == "codex":
    material = f"{provider}\0{provider_agent_id}"
  else:
    material = f"{provider}\0{provider_session_id or ''}\0{provider_agent_id}"
  return "agent-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def stable_activation_id(
  agent_id: str, chat_run_id: str | None, provider_activation_id: str | None,
) -> str:
  """Opaque identity for one helper activation within a root chat run."""
  material = f"{agent_id}\0{chat_run_id or ''}\0{provider_activation_id or ''}"
  return "activation-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _event_key(
  activation_id: str, event_type: str, state: str,
  source_event_id: str | None, occurred_at: Any,
) -> str:
  """Idempotency for one provider fact without collapsing distinct wrappers."""
  native_or_fallback = source_event_id or f"{event_type}\0{state}\0{occurred_at or ''}"
  return hashlib.sha256(
    f"{activation_id}\0{native_or_fallback}".encode("utf-8")
  ).hexdigest()


def normalize_chat_event(
  *, chat_id: str, chat_run_id: str | None, event: dict[str, Any],
  observed_at: datetime | None = None,
) -> dict[str, Any] | None:
  """Return a DB-ready lifecycle fact for one sink event, or ``None``."""
  raw_type = event.get("type")
  if raw_type in ("task_start", "task_done"):
    provider = "claude"
    provider_agent_id = _clip(event.get("task_id"), PROVIDER_ID_CHARS)
    if provider_agent_id is None:
      return None
    provider_session_id = _clip(
      event.get("provider_session_id") or f"run:{chat_run_id or chat_id}",
      PROVIDER_ID_CHARS,
    )
    if raw_type == "task_start":
      event_type = "agent_started"
      state = "running"
      summary = event.get("description")
    else:
      raw_status = str(event.get("status") or "").lower()
      if raw_status in ("failed", "error", "errored"):
        event_type, state = "agent_terminal", "failed"
      elif raw_status in ("killed", "stopped", "cancelled", "canceled"):
        event_type, state = "agent_terminal", "stopped"
      else:
        event_type, state = "agent_terminal", "done"
      summary = event.get("summary")
    parent_provider_id = None
    parent_agent_id = None
    parent_kind = "unknown"
    parent_source_id = _clip(event.get("tool_use_id"), PROVIDER_ID_CHARS)
    parent_provider_activation_id = None
    provider_activation_id = provider_agent_id
    agent_type = event.get("task_type")
    source_event_id = event.get("source_event_id")
    occurred_at = event.get("occurred_at")
  elif raw_type == "agent_lifecycle":
    provider = str(event.get("provider") or "").lower()
    event_type = str(event.get("event_type") or "")
    if not provider or event_type not in EVENT_TYPES:
      return None
    provider_agent_id = _clip(event.get("provider_agent_id"), PROVIDER_ID_CHARS)
    if provider_agent_id is None:
      return None
    provider_session_id = _clip(
      event.get("provider_session_id") or f"run:{chat_run_id or chat_id}",
      PROVIDER_ID_CHARS,
    )
    parent_provider_id = _clip(
      event.get("parent_provider_agent_id"), PROVIDER_ID_CHARS,
    )
    parent_kind = str(event.get("parent_kind") or "unknown").lower()
    if parent_kind not in ("main", "agent", "unknown"):
      parent_kind = "unknown"
    if parent_kind == "main":
      parent_provider_id = None
    parent_agent_id = (
      stable_agent_id(provider, provider_session_id, parent_provider_id)
      if parent_kind == "agent" and parent_provider_id
      and parent_provider_id != provider_agent_id else None
    )
    parent_source_id = _clip(event.get("parent_source_id"), PROVIDER_ID_CHARS)
    parent_provider_activation_id = _clip(
      event.get("parent_provider_activation_id") or parent_provider_id,
      PROVIDER_ID_CHARS,
    )
    provider_activation_id = _clip(
      event.get("provider_activation_id") or provider_agent_id,
      PROVIDER_ID_CHARS,
    )
    requested_state = str(event.get("state") or "").lower()
    if event_type in ("agent_spawned", "agent_started"):
      state = "running"
    elif requested_state in ("failed", "stopped"):
      state = requested_state
    else:
      state = "done"
    summary = event.get("summary")
    agent_type = event.get("agent_type")
    source_event_id = event.get("source_event_id")
    occurred_at = event.get("occurred_at")
  else:
    return None

  agent_id = stable_agent_id(provider, provider_session_id, provider_agent_id)
  activation_id = stable_activation_id(
    agent_id, chat_run_id, provider_activation_id,
  )
  parent_activation_id = (
    stable_activation_id(
      parent_agent_id, chat_run_id, parent_provider_activation_id,
    )
    if parent_agent_id is not None else None
  )
  occurred = _naive_utc(occurred_at)
  observed = _naive_utc(observed_at) or now_naive_utc()
  return {
    "event_key": _event_key(
      activation_id, event_type, state, _clip(source_event_id, PROVIDER_ID_CHARS),
      occurred_at,
    ),
    "chat_id": chat_id,
    "chat_run_id": chat_run_id,
    "provider": provider,
    "provider_session_id": provider_session_id,
    "provider_agent_id": provider_agent_id,
    "agent_id": agent_id,
    "activation_id": activation_id,
    "parent_agent_id": parent_agent_id,
    "parent_activation_id": parent_activation_id,
    "parent_kind": parent_kind,
    "parent_source_id": parent_source_id,
    "event_type": event_type,
    "state": _clip(state, 16) or "running",
    "agent_type": _clip(agent_type, AGENT_TYPE_CHARS),
    "summary": _summary(summary),
    "occurred_at": occurred,
    "observed_at": observed,
    "time_quality": "exact" if occurred is not None else "observed",
    "source": _clip(event.get("source") or "runner", 32) or "runner",
    "source_event_id": _clip(source_event_id, PROVIDER_ID_CHARS),
  }


def record_event(db, values: dict[str, Any]) -> bool:
  """Append one normalized row; returns whether a new row was committed.

  Stable semantic keys suppress redelivery, while late facts remain append-only.
  Consumers apply terminal monotonicity in their projection rather than losing
  a valid timestamp merely because it arrived after a terminal notification.
  """
  db.add(models.AgentLifecycleEvent(**values))
  try:
    db.commit()
  except IntegrityError as exc:
    db.rollback()
    duplicate = (
      db.query(models.AgentLifecycleEvent)
      .filter(models.AgentLifecycleEvent.event_key == values["event_key"])
      .first()
    )
    if duplicate is not None:
      canonical = (
        "chat_id", "chat_run_id", "provider", "provider_session_id",
        "provider_agent_id", "agent_id", "activation_id", "parent_agent_id",
        "parent_activation_id", "parent_kind", "parent_source_id", "event_type",
        "state", "agent_type", "summary", "occurred_at", "time_quality",
        "source", "source_event_id",
      )
      if all(getattr(duplicate, field) == values.get(field) for field in canonical):
        return False
    raise exc
  return True
