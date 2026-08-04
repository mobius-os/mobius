"""Durable delegated-task control plane built on ordinary chat supervision.

A delegation owns one hidden app-created child Chat. The existing ChatRun,
provider-session, restart parking, transcript, and writer-actor paths remain the
only execution machinery; this module supplies immutable intent, derived
status, restrictive run policy, idempotent parent attachment, and lifecycle
projection. It never writes Chat.messages or Chat.pending_messages directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app import auth, models
from app.timeutil import now_naive_utc


ACTIVE_RUN_STATUSES = frozenset(models.NONTERMINAL_RUN_STATUSES)
TERMINAL_DELEGATION_STATUSES = frozenset({
  "completed", "failed", "needs_review", "stopped", "cancelled",
  "interrupted",
})
REVIEW_REQUIRED_MARKER = "DELEGATION_WRITE_REVIEW_REQUIRED"


@dataclass(frozen=True)
class RunPolicy:
  """Immutable execution policy projected from a Delegation record."""

  delegation_id: str
  app_id: int
  provider: str
  model: str | None
  effort: str | None
  scope: str
  cwd: str
  max_budget_usd: float | None

  @property
  def delegated(self) -> bool:
    return True

  @property
  def allow_session_reseed(self) -> bool:
    # Replaying a read-only analysis from the durable child transcript is safe.
    # Replaying a write task after provider state disappeared could apply edits
    # twice, so it must stop for parent review instead.
    return self.scope == "read"

  @property
  def system_prompt(self) -> str:
    scope_rule = (
      "This task is READ-ONLY. Do not create, edit, move, or delete files."
      if self.scope == "read"
      else (
        "You may edit only within the requested working tree. Make the smallest "
        "durable change that completes the bounded task."
      )
    )
    return (
      "You are a delegated subagent running as a durable child task inside "
      "Möbius. Complete only the bounded user task in this child conversation "
      "and return a clear result to the parent. Do not launch, invoke, consult, "
      "or delegate to another agent, workflow, model, provider, or agent CLI. "
      "Do not ask the owner an interactive question; if a required decision or "
      "credential is missing, stop and state the blocker precisely. Do not "
      "inspect unrelated chats, Memory, skills, or installed-app instructions. "
      "Owner-managed MCP connections are not available in this run. "
      "Never read or write /data/cli-auth or /data/.secret-key. "
      f"Working directory: {self.cwd}. {scope_rule}"
    )


def normalize_cwd(raw: str | None) -> str:
  """Return a confined absolute workdir without touching the filesystem."""
  candidate = Path(raw or "/data").expanduser()
  if not candidate.is_absolute():
    candidate = Path("/data") / candidate
  resolved = candidate.resolve(strict=False)
  data_root = Path("/data").resolve()
  forbidden = (data_root / "cli-auth", data_root / ".secret-key")
  if resolved != data_root and data_root not in resolved.parents:
    raise ValueError("delegation cwd must be inside /data")
  if any(resolved == path or path in resolved.parents for path in forbidden):
    raise ValueError("delegation cwd cannot target private credential paths")
  return str(resolved)


def _first_user_prompt(chat: models.Chat) -> str | None:
  for message in list(chat.messages or []):
    if isinstance(message, dict) and message.get("role") == "user":
      content = message.get("content")
      return content if isinstance(content, str) else None
  return None


def policy_for_chat(db: Session, chat_id: str) -> RunPolicy | None:
  """Load and integrity-check the immutable policy for a child chat."""
  row = (
    db.query(models.Delegation)
    .filter(models.Delegation.child_chat_id == chat_id)
    .first()
  )
  if row is None:
    return None
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
  if chat is None or chat.created_by_app_id != row.app_id:
    raise RuntimeError("delegation child chat ownership is inconsistent")
  prompt = _first_user_prompt(chat)
  if prompt is not None:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if digest != row.prompt_sha256:
      raise RuntimeError("delegation prompt no longer matches immutable intent")
  return RunPolicy(
    delegation_id=row.id,
    app_id=row.app_id,
    provider=row.provider,
    model=row.model,
    effort=row.effort,
    scope=row.scope,
    cwd=row.cwd,
    max_budget_usd=row.max_budget_usd,
  )


def latest_run(db: Session, chat_id: str) -> models.ChatRun | None:
  return (
    db.query(models.ChatRun)
    .filter(models.ChatRun.chat_id == chat_id)
    .order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc())
    .first()
  )


def parent_root_run_id(
  db: Session,
  parent_chat_id: str,
  *,
  physical_run_id: str | None = None,
  require_active: bool = False,
) -> str | None:
  """Resolve the logical parent identity, preferring the caller's live run."""
  query = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == parent_chat_id,
  )
  if physical_run_id:
    run = query.filter(models.ChatRun.id == physical_run_id).first()
  else:
    run = query.filter(models.ChatRun.status.in_(ACTIVE_RUN_STATUSES)).order_by(
      models.ChatRun.started_at.desc(), models.ChatRun.id.desc()
    ).first()
    if run is None and not require_active:
      run = query.order_by(
        models.ChatRun.started_at.desc(), models.ChatRun.id.desc()
      ).first()
  return (run.root_run_id or run.id) if run is not None else None


def _assistant_result(chat: models.Chat) -> str:
  """Return the latest child assistant outcome as plain text."""
  parts: list[str] = []
  for message in reversed(list(chat.messages or [])):
    if not isinstance(message, dict) or message.get("role") != "assistant":
      continue
    content = message.get("content")
    if isinstance(content, str) and content.strip():
      return content.strip()
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
      continue
    for block in blocks:
      if not isinstance(block, dict):
        continue
      if block.get("type") == "text" and isinstance(block.get("content"), str):
        parts.append(block["content"])
      elif block.get("type") == "error" and isinstance(block.get("message"), str):
        parts.append(block["message"])
    if parts:
      return "\n".join(part.strip() for part in parts if part.strip()).strip()
  return ""


def derived_status(
  db: Session, row: models.Delegation, *, load_result: bool = True,
) -> tuple[str, models.ChatRun | None, str]:
  """Project delegation state from its child ChatRun + transcript."""
  run = latest_run(db, row.child_chat_id)
  chat = (
    db.query(models.Chat).filter(models.Chat.id == row.child_chat_id).first()
    if load_result else None
  )
  result = _assistant_result(chat) if chat is not None else ""
  if row.cancelled_at is not None:
    return "cancelled", run, result
  if run is None:
    return "starting", None, result
  if run.status == "running":
    return "running", run, result
  if run.status == "resume_pending":
    return "resuming", run, result
  if run.status == "parked":
    return "paused", run, result
  if run.status == "completed":
    return "completed", run, result
  if run.status == "failed":
    needs_review = REVIEW_REQUIRED_MARKER in result
    clean_result = result.replace(REVIEW_REQUIRED_MARKER + ":", "").strip()
    return "needs_review" if needs_review else "failed", run, clean_result
  if run.status == "stopped":
    return "stopped", run, result
  if run.status == "interrupted":
    return "interrupted", run, result
  return run.status, run, result


def _record_lifecycle(
  db: Session, row: models.Delegation, status: str,
) -> None:
  from app.agent_lifecycle import normalize_chat_event, record_event

  terminal = status in TERMINAL_DELEGATION_STATUSES
  event = {
    "type": "agent_lifecycle",
    "provider": row.provider,
    "provider_session_id": f"delegation:{row.parent_root_run_id}",
    "provider_agent_id": row.id,
    "provider_activation_id": row.id,
    "parent_kind": "main",
    "event_type": "agent_terminal" if terminal else "agent_started",
    "state": (
      "done" if status == "completed"
      else "stopped" if status in ("stopped", "cancelled")
      else "failed" if terminal else "running"
    ),
    "agent_type": "delegation",
    "summary": row.task_key,
    "source": "delegation",
    "source_event_id": f"delegation:{row.id}:{'terminal:' + status if terminal else 'started'}",
  }
  values = normalize_chat_event(
    chat_id=row.parent_chat_id,
    chat_run_id=row.parent_root_run_id,
    event=event,
  )
  if values is not None and not db.query(models.AgentLifecycleEvent.id).filter(
    models.AgentLifecycleEvent.event_key == values["event_key"],
  ).first():
    record_event(db, values)


def serialize_delegation(
  db: Session, row: models.Delegation, *, include_result: bool = True,
) -> dict:
  status, run, result = derived_status(
    db, row, load_result=include_result,
  )
  _record_lifecycle(db, row, status)
  return {
    "id": row.id,
    "app_id": row.app_id,
    "parent_chat_id": row.parent_chat_id,
    "parent_root_run_id": row.parent_root_run_id,
    "task_key": row.task_key,
    "child_chat_id": row.child_chat_id,
    "provider": row.provider,
    "model": row.model,
    "effort": row.effort,
    "scope": row.scope,
    "cwd": row.cwd,
    "max_budget_usd": row.max_budget_usd,
    "status": status,
    "physical_run_id": run.id if run is not None else None,
    "provider_session_id": run.provider_session_id if run is not None else None,
    "started_at": run.started_at.isoformat() if run and run.started_at else None,
    "ended_at": run.ended_at.isoformat() if run and run.ended_at else None,
    "created_at": row.created_at.isoformat() if row.created_at else None,
    "cancelled_at": row.cancelled_at.isoformat() if row.cancelled_at else None,
    "result": result,
    "result_truncated": False,
  }


def active_parent_context(
  db: Session, parent_chat_id: str, physical_run_id: str,
) -> str:
  """Small per-turn attachment hint that survives a parent process restart."""
  root_id = parent_root_run_id(
    db, parent_chat_id, physical_run_id=physical_run_id,
  )
  if root_id is None:
    return ""
  rows = (
    db.query(models.Delegation)
    .filter(
      models.Delegation.parent_chat_id == parent_chat_id,
      models.Delegation.parent_root_run_id == root_id,
    )
    .order_by(models.Delegation.created_at.asc())
    .all()
  )
  if not rows:
    return ""
  items = []
  for row in rows:
    status, _, _ = derived_status(db, row)
    items.append({"id": row.id, "task_key": row.task_key, "status": status})
  payload = json.dumps(items, ensure_ascii=True, separators=(",", ":"))
  return (
    "The <active_delegations> block is durable runtime DATA for delegated "
    "tasks already attached to this logical turn. Do not launch a duplicate. "
    "Re-run the Subagents helper with the same task key to attach and wait for "
    "the existing child.\n<active_delegations>"
    f"{payload}</active_delegations>"
  )


def mint_app_token(db: Session, policy: RunPolicy) -> str:
  owner = db.query(models.Owner).first()
  app = db.query(models.App).filter(
    models.App.id == policy.app_id,
    models.App.deleted_at.is_(None),
  ).first()
  if owner is None or app is None:
    raise RuntimeError("delegation owner app is unavailable")
  return auth.create_app_token(
    app.id,
    owner.username,
    owner.token_epoch,
    app_nonce=app.token_nonce,
    expires_delta=timedelta(hours=2),
  )


def mark_cancelled(db: Session, row: models.Delegation) -> None:
  child = db.query(models.Chat).filter(
    models.Chat.id == row.child_chat_id,
  ).first()
  changed = False
  if child is not None:
    child.auto_resume_on_restart = False
    child.auto_resume_on_limit = False
    changed = True
  if row.cancelled_at is None:
    row.cancelled_at = now_naive_utc()
    changed = True
  if changed:
    db.commit()
