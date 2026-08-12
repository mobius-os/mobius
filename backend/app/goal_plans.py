"""Durable, sequential goal-plan state.

The provider remains the owner of one active native goal.  This module owns
only the ordered plan around it: parsing owner intent, deriving the current
stage prompt, and the narrow plan-row mutations used by the chat writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
import uuid

from sqlalchemy.orm import Session

from app import models


_PLAN_HEADER = re.compile(r"^/goals[ \t]+([^\n]+)(?:\n([\s\S]*))?$", re.I)
_STAGE_LINE = re.compile(r"^(?:[-*]|\d+[.)])[ \t]+(.+?)\s*$")
_MAX_STAGES = 12
_MAX_FIELD_CHARS = 2_000


@dataclass(frozen=True)
class GoalPlanSpec:
  overall_objective: str
  stages: tuple[str, ...]


def parse_goal_plan(text: str | None) -> GoalPlanSpec | None:
  """Parse one explicit ``/goals`` command, or return ordinary prose.

  The first line is the outcome; every following non-empty line must be a
  bullet or numbered stage.  Requiring list markers keeps a pasted paragraph
  from silently becoming a plan with arbitrary sentence boundaries.
  """
  normalized = (text or "").lstrip("\n")
  match = _PLAN_HEADER.fullmatch(normalized)
  if match is None:
    return None
  overall = match.group(1).strip()
  body = match.group(2) or ""
  stages: list[str] = []
  for raw_line in body.splitlines():
    if not raw_line.strip():
      continue
    stage_match = _STAGE_LINE.fullmatch(raw_line.strip())
    if stage_match is None:
      return None
    stage = " ".join(stage_match.group(1).split())
    if not stage or len(stage) > _MAX_FIELD_CHARS:
      return None
    stages.append(stage)
  if (
    not overall
    or len(overall) > _MAX_FIELD_CHARS
    or len(stages) < 2
    or len(stages) > _MAX_STAGES
  ):
    return None
  return GoalPlanSpec(overall, tuple(stages))


def stages_for(row: models.GoalPlan) -> list[str]:
  values = row.stages if isinstance(row.stages, list) else []
  return [value for value in values if isinstance(value, str) and value]


def stage_label(row: models.GoalPlan) -> str | None:
  stages = stages_for(row)
  if not 0 <= row.current_stage < len(stages):
    return None
  return (
    f"{row.overall_objective} · Stage {row.current_stage + 1}/{len(stages)} "
    f"· {stages[row.current_stage]}"
  )


def stage_prompt(row: models.GoalPlan) -> str | None:
  label = stage_label(row)
  if label is None:
    return None
  return (
    f"Overall outcome: {row.overall_objective}\n"
    f"Current sequential goal: {label}\n\n"
    "Complete this stage only. Do not begin a later stage; Möbius will start "
    "the next one after this goal completes."
  )


def active_plan_for_run(
  db: Session, chat_id: str, run_token: str | None,
) -> models.GoalPlan | None:
  if not chat_id or not run_token:
    return None
  return (
    db.query(models.GoalPlan)
    .filter(
      models.GoalPlan.chat_id == chat_id,
      models.GoalPlan.status == "active",
      models.GoalPlan.current_run_token == run_token,
    )
    .order_by(models.GoalPlan.created_at.desc(), models.GoalPlan.id.desc())
    .first()
  )


def active_plan_summary(
  db: Session, chat_id: str, run_token: str | None = None,
) -> dict | None:
  query = db.query(models.GoalPlan).filter(
    models.GoalPlan.chat_id == chat_id,
    models.GoalPlan.status == "active",
  )
  if run_token is not None:
    query = query.filter(models.GoalPlan.current_run_token == run_token)
  row = query.order_by(models.GoalPlan.created_at.desc(), models.GoalPlan.id.desc()).first()
  if row is None:
    return None
  label = stage_label(row)
  if label is None:
    return None
  stages = stages_for(row)
  return {
    "id": row.id,
    "overall_objective": row.overall_objective,
    "stage_index": row.current_stage,
    "stage_count": len(stages),
    "stage_objective": stages[row.current_stage],
    "stage_label": label,
  }


def start_plan(
  db: Session,
  *,
  chat_id: str,
  run_token: str,
  spec: GoalPlanSpec,
) -> models.GoalPlan:
  """Create a plan for an already-started run, superseding any active plan."""
  now = datetime.now(UTC)
  (
    db.query(models.GoalPlan)
    .filter(
      models.GoalPlan.chat_id == chat_id,
      models.GoalPlan.status == "active",
    )
    .update(
      {"status": "superseded", "updated_at": now},
      synchronize_session=False,
    )
  )
  row = models.GoalPlan(
    id=str(uuid.uuid4()),
    chat_id=chat_id,
    overall_objective=spec.overall_objective,
    stages=list(spec.stages),
    current_stage=0,
    current_run_token=run_token,
    status="active",
    created_at=now,
    updated_at=now,
  )
  db.add(row)
  return row


def claim_active_plan_run(
  db: Session,
  *,
  chat_id: str,
  run_token: str,
  message: dict | None,
) -> models.GoalPlan | None:
  """Attach an explicit plan continuation or retry to its new ChatRun.

  The chat writer calls this in the same transaction that opens the run, so a
  paused stage can recover without a separate plan scheduler or a window with
  two eligible runs.
  """
  if not isinstance(message, dict):
    return None
  content = message.get("content")
  kind = message.get("kind")
  continuation = (
    kind == "goal_plan_step"
    or kind == "continuation"
    or (isinstance(content, str) and content.strip().lower() == "continue")
  )
  if not continuation:
    return None
  row = (
    db.query(models.GoalPlan)
    .filter(
      models.GoalPlan.chat_id == chat_id,
      models.GoalPlan.status == "active",
    )
    .order_by(models.GoalPlan.created_at.desc(), models.GoalPlan.id.desc())
    .first()
  )
  if row is None or stage_label(row) is None:
    return None
  row.current_run_token = run_token
  row.updated_at = datetime.now(UTC)
  return row


def stop_active_plan(db: Session, chat_id: str) -> bool:
  """Stop an active plan without altering historical stages."""
  now = datetime.now(UTC)
  changed = (
    db.query(models.GoalPlan)
    .filter(
      models.GoalPlan.chat_id == chat_id,
      models.GoalPlan.status == "active",
    )
    .update(
      {"status": "stopped", "current_run_token": None, "updated_at": now},
      synchronize_session=False,
    )
  )
  return bool(changed)
