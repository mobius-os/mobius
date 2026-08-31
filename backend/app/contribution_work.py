"""Durable source-chat work owned by the contribution workflow.

The GitHub route owns source/ledger inspection. This module owns the compact
immutable request, revision/envelope identity, deferred child startup, and
restart-safe reconciliation. Keeping those responsibilities separate avoids
turning the already-large GitHub transport module into a second delegation
control plane while also avoiding an import cycle back into its private review
projection helpers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal
from app.delegations import (
  derived_status,
  ensure_delegation_started,
  publish_source_work_changed,
)
from app.github_contributions import _CONTRIBUTION_ID


log = logging.getLogger("moebius.contribution_work")


PRESTART_SOURCE_WORK_STATUSES = frozenset({"accepted", "retrying"})
_PRESTART_RETRY_RESULT = (
  "The contribution helper did not start on its first attempt. "
  "Möbius will retry it automatically."
)
_PRESTART_ATTENTION_RESULT = (
  "The contribution helper could not start after an automatic retry. "
  "Choose the current contribution action in Changes to try again."
)


class ContributionWorkBody(BaseModel):
  """Owner action identity; the server derives every source-work detail."""

  model_config = ConfigDict(extra="forbid")

  intent: Literal["prepare", "finish", "project", "updates", "followup"]
  # Recovery actions fired immediately after a deterministic publication may
  # not yet have a refreshed client projection. An empty value means "bind to
  # the current authoritative revision under the source-chat lock"; the
  # canonical revision, never the empty sentinel, is persisted and hashed.
  expected_revision: str = Field(default="", max_length=200_000)
  project_root: str | None = Field(default=None, max_length=1024)
  record_ids: list[str] = Field(default_factory=list, max_length=32)
  # A retry names the exact terminal helper being superseded. Including it in
  # the immutable identity creates one fresh attempt without making ordinary
  # repeated taps non-idempotent.
  retry_of: str | None = Field(default=None, max_length=64)

  @field_validator("expected_revision", "project_root")
  @classmethod
  def _trim_work_text(cls, value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value

  @field_validator("record_ids")
  @classmethod
  def _valid_work_record_ids(cls, values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
      record_id = value.strip() if isinstance(value, str) else ""
      if not _CONTRIBUTION_ID.fullmatch(record_id):
        raise ValueError("record_ids contains an invalid contribution id")
      if record_id not in normalized:
        normalized.append(record_id)
    return normalized

  @field_validator("retry_of")
  @classmethod
  def _valid_retry_of(cls, value: str | None) -> str | None:
    if value is None:
      return None
    normalized = value.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
      raise ValueError("retry_of must be a contribution work id")
    return normalized


def request_id(app_id: int, chat_id: str, body: ContributionWorkBody) -> str:
  identity_fields = {
    "v": 1,
    "app_id": app_id,
    "source_chat_id": chat_id,
    "intent": body.intent,
    "expected_revision": body.expected_revision,
    "project_root": body.project_root or "",
    "record_ids": sorted(body.record_ids),
  }
  if body.retry_of:
    identity_fields["retry_of"] = body.retry_of
  identity = json.dumps(identity_fields, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(identity.encode()).hexdigest()


def prompt(envelope: dict) -> str:
  intent = envelope["intent"]
  goal = {
    "prepare": "Privately prepare every worthwhile listed edit for review.",
    "project": "Privately prepare every worthwhile listed edit for this project.",
    "finish": "Finish all private preparation and repair represented here.",
    "updates": "Check the listed open contributions for relevant newer edits.",
    "followup": "Resolve the private review or attention state of the listed contributions.",
  }[intent]
  payload = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
  return (
    f"Goal: {goal}\n"
    "Read and follow /data/apps/contribute/attached-work.md. Begin with its "
    "single filtered offline snapshot command; never enumerate Contribute "
    "storage or load the general contribution manual. Verify current source "
    "and scoped ledger state, create or update only private review records, and "
    "settle every intentionally excluded listed path through its exact "
    "reviewed_through value. Do not push, publish, send, comment, or otherwise "
    "mutate GitHub. The source chat remains the contribution owner; never add "
    "this child chat to record provenance. Do not read the source transcript, "
    "chat summaries, Memory, recent chats, or edit-diff sidecars: this compact "
    "manifest is the complete source scope. Inspect or edit source files only "
    "inside its exact project_roots; other /data trees remain out of scope. "
    "Use one bounded pass; fan out only "
    "when genuinely independent work amortizes another helper startup. Return "
    "a concise result naming prepared records, settled local paths, and any "
    "owner decision still required.\n"
    f"<contribution_work>{payload}</contribution_work>"
  )


def source_chat_is_active(db: Session, chat_id: str) -> bool:
  from app.chat import is_chat_running

  return is_chat_running(chat_id) or db.query(models.ChatRun.id).filter(
    models.ChatRun.chat_id == chat_id,
    models.ChatRun.status.in_(models.NONTERMINAL_RUN_STATUSES),
  ).first() is not None


def revision(snapshot: dict, body: ContributionWorkBody) -> str:
  if body.intent in {"prepare", "project"}:
    return snapshot["unsorted_revision"]
  if body.intent == "finish":
    return snapshot["workflow_revision"]
  requested = set(body.record_ids)
  # Byte-for-byte equivalent to the frontend reviewActionKey(record) fast
  # freshness check. Server projections always carry action_key.
  return "|".join(sorted(
    f"{record['id']}:{record['action_key']}"
    for record in snapshot["record_views"]
    if record.get("id") in requested and record.get("action_key")
  ))


def records_exist(snapshot: dict, body: ContributionWorkBody) -> bool:
  if body.intent not in {"updates", "followup"}:
    return True
  requested = set(body.record_ids)
  records = [
    record for record in snapshot["record_views"]
    if str(record.get("id") or "") in requested
  ]
  if {str(record.get("id")) for record in records} != requested:
    return False
  # Empty roots are valid for source-less legacy/comment records. A supplied
  # root, however, must resolve to the same platform/app boundary as an
  # explicit Project request; hostile ledger metadata must never become a
  # delegated write scope.
  return all(
    record.get("source_root_valid") is not False
    and (
      not str(record.get("source_root") or "").strip()
      or bool(project_root(record.get("source_root")))
    )
    for record in records
  )


def unrevisioned_request_matches(
  row: models.Delegation, body: ContributionWorkBody,
) -> bool:
  """Attach an empty-revision retry to the same one active selector."""
  if row.source_work_intent != body.intent:
    return False
  value = row.source_work_envelope or {}
  if body.intent == "project":
    return body.project_root in (value.get("project_roots") or [])
  if body.intent in {"updates", "followup"}:
    persisted = value.get("record_ids")
    return (
      isinstance(persisted, list)
      and sorted(str(item) for item in persisted) == sorted(body.record_ids)
    )
  return True


def project_root(path: object) -> str:
  """Return the one writable platform/app root that owns ``path``.

  Contribution record metadata is agent-authored, so it cannot widen a child
  worker from the same project roots the explicit Project action accepts.
  Keep this lexical and fail closed: trailing slashes are harmless, while dot
  traversal, numeric storage directories, and unrelated /data trees are not
  source projects.
  """
  if not isinstance(path, str):
    return ""
  value = path.strip()
  if not value or "\x00" in value:
    return ""
  segments = value.split("/")
  if any(segment in {".", ".."} for segment in segments):
    return ""
  normalized = value.rstrip("/") or "/"
  if normalized == "/data/platform" or normalized.startswith("/data/platform/"):
    return "/data/platform"
  match = re.fullmatch(
    r"/data/apps/([A-Za-z0-9_.-]+)(?:/.*)?", normalized,
  )
  if match is None:
    return ""
  slug = match.group(1)
  if slug.isdigit() or slug in {".", ".."}:
    return ""
  return f"/data/apps/{slug}"


def _instant_ms(value: object) -> int | None:
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    return int(value)
  if not isinstance(value, str) or not value.strip():
    return None
  try:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
  except ValueError:
    return None
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=UTC)
  return int(parsed.timestamp() * 1000)


def envelope(
  chat_id: str, body: ContributionWorkBody, snapshot: dict,
) -> dict:
  selected_entries = list(snapshot["unsorted_entries"])
  record_views = list(snapshot["record_views"])
  if body.intent == "project":
    root = body.project_root or ""
    selected_entries = [
      {**entry, "paths": [
        path for path in entry["paths"] if project_root(path) == root
      ]}
      for entry in selected_entries
    ]
    selected_entries = [entry for entry in selected_entries if entry["paths"]]

  if body.intent in {"updates", "followup"}:
    record_ids = list(body.record_ids)
    requested = set(record_ids)
    roots = {
      root for record in record_views if record.get("id") in requested
      and (root := project_root(record.get("source_root")))
    }
    selected_entries = [
      {**entry, "paths": [
        path for path in entry["paths"]
        if project_root(path) in roots
      ]}
      for entry in selected_entries
    ]
    selected_entries = [entry for entry in selected_entries if entry["paths"]]
  elif body.intent == "finish":
    record_ids = [
      str(record["id"]) for record in record_views
      if record.get("status") not in {"merged", "superseded", "closed"}
    ]
  else:
    record_ids = []

  reviewed_through: dict[str, int | None] = {}
  for entry in selected_entries:
    stamp = _instant_ms(entry.get("ts"))
    for path in entry["paths"]:
      previous = reviewed_through.get(path)
      if previous is None or (stamp is not None and stamp > previous):
        reviewed_through[path] = stamp
  paths = [
    {"path": path, "reviewed_through": reviewed_through[path]}
    for path in sorted(reviewed_through)
  ]
  project_roots = sorted({
    root for path in reviewed_through if (root := project_root(path))
  } | {
    root
    for record in record_views if record.get("id") in set(record_ids)
    and (root := project_root(record.get("source_root")))
  } | ({project_root(body.project_root)}
       if body.intent == "project" and project_root(body.project_root)
       else set()))
  return {
    "v": 1,
    "intent": body.intent,
    "source_chat_id": chat_id,
    "edit_revision": body.expected_revision,
    "paths": paths,
    "record_ids": sorted(set(record_ids)),
    "project_roots": project_roots,
  }


def body_from_row(row: models.Delegation) -> ContributionWorkBody:
  value = row.source_work_envelope or {}
  roots = value.get("project_roots")
  project_root = None
  if row.source_work_intent == "project" and isinstance(roots, list) and roots:
    project_root = str(roots[0])
  return ContributionWorkBody(
    intent=row.source_work_intent,
    expected_revision=str(value.get("edit_revision") or ""),
    project_root=project_root,
    record_ids=(
      value.get("record_ids") if isinstance(value.get("record_ids"), list) else []
    ),
  )


def mark_needs_review(db: Session, row: models.Delegation, detail: str) -> None:
  row.source_work_status = "needs_review"
  row.source_work_result = detail[:3000]
  row.source_work_active_chat_id = None
  row.startup_prompt = None
  db.commit()
  publish_source_work_changed(row, "needs_review")


def record_prestart_failure(db: Session, row: models.Delegation) -> None:
  """Retry one pre-start failure, then return persistent failure to the owner."""
  if row.source_work_status == "accepted":
    row.source_work_status = "retrying"
    row.source_work_result = _PRESTART_RETRY_RESULT
    db.commit()
    publish_source_work_changed(row, "retrying")
    return
  mark_needs_review(db, row, _PRESTART_ATTENTION_RESULT)


SnapshotLoader = Callable[[Session, int, str], Awaitable[dict]]
EnsureStarter = Callable[[Session, models.Delegation], Awaitable[bool]]


async def start_attached(
  delegation_id: str,
  *,
  snapshot_loader: SnapshotLoader,
  ensure_started: EnsureStarter = ensure_delegation_started,
) -> bool:
  """Start an accepted worker only against the same stable source view."""
  from app import chat_queue

  with SessionLocal() as lookup:
    row = lookup.query(models.Delegation).filter(
      models.Delegation.id == delegation_id,
      models.Delegation.source_work_status.in_(PRESTART_SOURCE_WORK_STATUSES),
      models.Delegation.cancelled_at.is_(None),
    ).first()
    if row is None:
      return False
    chat_id = row.parent_chat_id

  async with chat_queue.get_transition_lock(chat_id):
    with SessionLocal() as db:
      row = db.query(models.Delegation).filter(
        models.Delegation.id == delegation_id,
        models.Delegation.source_work_status.in_(PRESTART_SOURCE_WORK_STATUSES),
        models.Delegation.cancelled_at.is_(None),
      ).first()
      if row is None or source_chat_is_active(db, chat_id):
        return False
      app_id = row.source_work_context_app_id
      if not isinstance(app_id, int):
        mark_needs_review(
          db, row, "The contribution workspace is no longer available.",
        )
        return False
      try:
        body = body_from_row(row)
        snapshot = await snapshot_loader(db, app_id, chat_id)
      except Exception:
        log.warning(
          "attached contribution revalidation failed id=%s",
          delegation_id,
          exc_info=True,
        )
        record_prestart_failure(db, row)
        return False
      candidate = envelope(chat_id, body, snapshot)
      if (
        revision(snapshot, body) != body.expected_revision
        or not records_exist(snapshot, body)
        or candidate != (row.source_work_envelope or {})
        or not (candidate["paths"] or candidate["record_ids"])
      ):
        mark_needs_review(
          db,
          row,
          "The source changed while this work was waiting. Refresh Changes and choose the current action.",
        )
        return False
      try:
        started = await ensure_started(db, row)
      except Exception:
        log.warning(
          "attached contribution startup failed id=%s",
          delegation_id,
          exc_info=True,
        )
        started = False
      if not started:
        # The child StartTurn commits through another session. Re-read before
        # counting a false/failed admission as pre-start failure: if its ChatRun
        # exists, ordinary run reconciliation owns that durable start.
        db.rollback()
        db.expire_all()
        row = db.query(models.Delegation).filter(
          models.Delegation.id == delegation_id,
        ).first()
        if row is None:
          return False
        status, run, _result = derived_status(db, row, load_result=False)
        if run is not None:
          row.startup_prompt = None
          row.source_work_status = None
          row.source_work_result = None
          db.commit()
          status, _run, _result = derived_status(db, row, load_result=False)
          publish_source_work_changed(row, status)
          return status in {"starting", "running", "resuming", "paused"}
        if row.source_work_status in PRESTART_SOURCE_WORK_STATUSES:
          record_prestart_failure(db, row)
        return False
      status, _run, _result = derived_status(db, row, load_result=False)
      publish_source_work_changed(row, status)
      return status in {"starting", "running", "resuming", "paused"}


async def reconcile(
  *,
  snapshot_loader: SnapshotLoader,
  ensure_started: EnsureStarter = ensure_delegation_started,
  parent_chat_id: str | None = None,
) -> int:
  """Live/boot repair for accepted work whose source chat is now idle."""
  with SessionLocal() as db:
    query = db.query(models.Delegation.id).filter(
      models.Delegation.source_work_status.in_(PRESTART_SOURCE_WORK_STATUSES),
      models.Delegation.source_work_id.is_not(None),
      models.Delegation.cancelled_at.is_(None),
    )
    if parent_chat_id is not None:
      query = query.filter(models.Delegation.parent_chat_id == parent_chat_id)
    ids = [row_id for (row_id,) in query.order_by(
      models.Delegation.created_at.asc(),
    ).all()]
  started = 0
  for delegation_id in ids:
    try:
      if await start_attached(
        delegation_id,
        snapshot_loader=snapshot_loader,
        ensure_started=ensure_started,
      ):
        started += 1
    except Exception:
      # One unexpected control-plane fault must not make an older row starve
      # independent work attached to other source chats. Snapshot and provider
      # startup failures are recorded inside start_attached; this guard isolates
      # faults that happen outside those owned boundaries.
      log.warning(
        "attached contribution startup recovery failed id=%s",
        delegation_id,
        exc_info=True,
      )
  return started


def owner_app(db: Session) -> models.App | None:
  """Resolve the installed Subagents app that owns the hidden child chat."""
  return db.query(models.App).filter(
    models.App.deleted_at.is_(None),
    (models.App.slug == "subagents") | (models.App.name == "Subagents"),
  ).order_by(
    (models.App.slug == "subagents").desc(), models.App.id.asc(),
  ).first()
