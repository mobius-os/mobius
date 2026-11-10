"""Read-only handoff vocabulary; lifecycle owners retain admission authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from sqlalchemy.orm import Session
  from app import models


@dataclass(frozen=True)
class GoalCardHandoff:
  question_id: str
  kind: str  # saved or in_turn


@dataclass(frozen=True)
class GoalWaitHandoff:
  """An armed Wait or an outcome still owed to this exact Goal."""

  wait_id: str
  kind: str
  delivery_pending: bool
  resume_blocker: str | None


@dataclass(frozen=True)
class GoalHelperHandoff:
  delegation_id: str
  kind: str  # self_resuming


@dataclass(frozen=True)
class GoalParkHandoff:
  run_id: str
  kind: str
  reason: str | None


@dataclass(frozen=True)
class GoalDependencyHandoff:
  id: str
  target_kind: str
  condition: str
  source_chat_id: str | None
  source_chat_title: str | None
  automatic: bool
  delivery_pending: bool
  resume_blocker: str | None
  outcome: dict | None
  source_question_id: str | None = None
  source_deleted: bool = False
  # A claim text shown in part; the owner reads it whole on request.
  truncated: bool = False

  def view(self) -> dict:
    """The presented shape: ``truncated`` appears only when it is true."""
    return {key: value for key, value in asdict(self).items()
            if key != "truncated" or value}


@dataclass(frozen=True)
class GoalHandoffs:
  """Exact native and cross-chat handoffs share this read-side seam.

  This is a read-side projection, not permission to admit or deliver a turn.
  The writer must still check its own queue, run and Goal authority.
  """

  card: GoalCardHandoff | None
  running: bool
  waits: tuple[GoalWaitHandoff, ...]
  helpers: tuple[GoalHelperHandoff, ...]
  dependencies: tuple[GoalDependencyHandoff, ...]
  park: GoalParkHandoff | None
  failed_attempt: bool
  queued: tuple[str, ...] = ()

  @property
  def owner_input(self) -> bool:
    return self.card is not None

  @property
  def helper_count(self) -> int:
    return len(self.helpers)

  @property
  def park_presentation(self) -> dict[str, str | None]:
    return ({"kind": self.park.kind, "reason": self.park.reason} if self.park
            else {"kind": "none", "reason": None})

  @property
  def progress_owned(self) -> bool:
    """A real executor or eligible native handoff still owns the next step."""
    return (self.running or self.owner_input or bool(self.queued) or bool(self.helpers)
      or any(not wait.delivery_pending or wait.resume_blocker not in {
        "manual_resume", "resume_failed", "restart",
      } for wait in self.waits)
      or (self.park is not None and self.park.kind in {"automatic", "owner_input"}))

  @property
  def prevents_settlement(self) -> bool:
    """A native owner still owes this Goal a next step or result."""
    return (self.owner_input or bool(self.waits) or bool(self.helpers) or bool(self.queued)
            or any(dep.automatic and (
              dep.outcome is None or dep.delivery_pending
            ) for dep in self.dependencies)
            or (self.park is not None and self.park.kind in {"automatic", "owner_input"}))


def native_goal_handoffs(db: Session, goal: models.ChatGoal) -> GoalHandoffs:
  """Read the native handoffs of one Goal, never another Goal in its chat.

  A NULL-owned historical helper is included only when its immutable root is
  the Goal id; a mutable physical run link is not evidence of ownership.
  """
  return native_handoffs(db, goal.chat_id, goal_id=goal.id)


def native_handoffs(db: Session, chat_id: str, *, goal_id: str | None,
                    root_run_id: str | None = None) -> GoalHandoffs:
  """Native progress owned by an exact Goal or a Goal-less causal root."""
  if goal_id is None and root_run_id is None:
    raise ValueError("Goal-less handoffs require an exact causal root")
  from app import models, questions
  from app.chat_waits import _goal_waits, _FIRED_UNDELIVERED, serialize_wait
  from app.delegations import _self_resuming_helper_rows
  from app.chat import continuation_handoff_for_chat

  latest = db.query(models.ChatRun.id, models.ChatRun.root_run_id, models.ChatRun.goal_id, models.ChatRun.status).filter(
    models.ChatRun.chat_id == chat_id,
  ).order_by(models.ChatRun.started_at.desc(), models.ChatRun.id.desc()).first()
  owns_chat_attempt = (latest is not None and latest.goal_id == goal_id
    and (goal_id is not None or (latest.root_run_id or latest.id) == root_run_id))
  pending = questions.get(chat_id)
  pending_id = db.query(models.Chat.pending_question_id).filter(
    models.Chat.id == chat_id,
  ).scalar()
  # Saved-card admission prevents a successor until this marker is answered
  # or cancelled; native in-turn questions carry their explicit run identity.
  card = None
  if owns_chat_attempt:
    if pending_id:
      card = GoalCardHandoff(question_id=pending_id, kind="saved")
    elif pending is not None and pending.run_token == latest.id:
      card = GoalCardHandoff(question_id=pending.question_id, kind="in_turn")
  wait_query = _goal_waits(db, chat_id, goal_id).filter(
    (models.ChatWait.status == "armed") | _FIRED_UNDELIVERED,
  )
  if goal_id is None:
    wait_query = wait_query.filter(models.ChatWait.root_run_id == root_run_id)
  wait_rows = wait_query.all()
  waits = tuple(GoalWaitHandoff(
    wait_id=saved["id"], kind=saved["kind"],
    delivery_pending=saved["delivery_pending"],
    resume_blocker=saved["resume_blocker"],
  ) for saved in (serialize_wait(row, db=db) for row in wait_rows))
  helpers = tuple(
    GoalHelperHandoff(delegation_id=row.id, kind="self_resuming")
    for row, _status in _self_resuming_helper_rows(db, {chat_id})
    if (goal_id is not None and (row.goal_id == goal_id or (
      row.goal_id is None and row.parent_root_run_id == goal_id
    ))) or (goal_id is None and row.goal_id is None
      and row.parent_root_run_id == root_run_id)
  )
  park = None
  if owns_chat_attempt and latest.status in models.CONTINUATION_RUN_STATUSES:
    projected = continuation_handoff_for_chat(db, chat_id)
    if projected["kind"] != "none":
      park = GoalParkHandoff(
        run_id=latest.id, kind=projected["kind"], reason=projected.get("reason"),
      )
  return GoalHandoffs(
    card=card,
    running=bool(owns_chat_attempt and latest.status == "running"),
    waits=waits,
    helpers=helpers,
    dependencies=(),
    queued=queued_goal_continuations(db, chat_id, goal_id, root_run_id=root_run_id),
    park=park,
    failed_attempt=bool(owns_chat_attempt and latest.status in {"failed", "interrupted"}),
  )

def goal_handoffs(db: Session, goal: models.ChatGoal, *, include_foreign_context=True) -> GoalHandoffs:
  from app.goal_dependencies import goal_dependency_handoffs
  return replace(native_goal_handoffs(db, goal), dependencies=tuple(
    GoalDependencyHandoff(**item) for item in goal_dependency_handoffs(db, goal, include_foreign_context=include_foreign_context)
  ))


def queued_goal_continuations(db, chat_id, goal_id, *, root_run_id=None) -> tuple[str, ...]:
  """Accepted exact-owned queue work, not arbitrary messages in the same chat.

  Question answer commits retire the card before promotion admits its run.
  These reads share the writer's identity/admission predicates. A Goal-less
  source additionally needs exact causal-root evidence, never a newest Goal.
  """
  from app import models
  from app.continuations import is_continuation_message, is_retired_goal_handoff, PRODUCT_RESULT_MESSAGE_KINDS
  from app.run_state import goal_identity_for_run_start, queued_continuation_root
  from app.chat_waits import wait_result_message_blocker
  from app.goal_dependencies import dependency_result_message_blocker
  from app.chat_writer import _PersistFailed, _require_browser_grant
  chat = db.get(models.Chat, chat_id)
  if chat is None or chat.deleted_at is not None:
    return ()
  owned = []
  for message in chat.pending_messages or ():
    if (not isinstance(message, dict) or not message.get("cid")
        or message.get("delivery_status") == "rejected"
        or is_retired_goal_handoff(message)):
      continue
    # Continuations require the writer's explicit identity snapshot. Product
    # results instead resolve their exact saved result identity at admission.
    if not ((is_continuation_message(message) and "goal_id" in message)
            or message.get("kind") in PRODUCT_RESULT_MESSAGE_KINDS):
      continue
    try:
      _require_browser_grant(db, message.get("_browser_grant_id"))
    except _PersistFailed:
      continue
    if (wait_result_message_blocker(db, chat_id, message)
        or dependency_result_message_blocker(db, chat_id, message)):
      continue
    if goal_id is None and message.get("goal_id") is not None:
      continue  # A closed/invalid Goal does not become a Goal-less handoff.
    if goal_identity_for_run_start(db, chat_id, message)[1] != goal_id:
      continue
    if goal_id is None and (root_run_id is None
        or queued_continuation_root(db, chat_id, message) != root_run_id):
      continue
    owned.append(message["cid"])
  return tuple(owned)


def project_handoff(*, owner_input: bool, running: bool, waits: list[dict],
                             helper_count: int, park: dict, goal: dict | None = None) -> dict:
  """One read-side handoff vocabulary for chat and exact-Goal surfaces."""
  if owner_input:
    return {"kind": "owner_input", "reason": "saved_card"}
  blockers = [wait.get("resume_blocker") for wait in waits if wait.get("delivery_pending")]
  if "owner_input" in blockers:
    return {"kind": "owner_input", "reason": "saved_card"}
  if running:
    return {"kind": "working", "reason": None}
  if park["kind"] in {"owner_input", "recovery"}:
    return park
  if any(blocker in {"manual_resume", "resume_failed", "restart"} for blocker in blockers):
    return {"kind": "recovery", "reason": next(
      blocker for blocker in blockers if blocker in {"manual_resume", "resume_failed", "restart"}
    )}
  if park["kind"] == "automatic" or helper_count > 0 or any(
    not wait.get("delivery_pending") or wait.get("resume_blocker") in {
      None, "platform_restart", "restoring_edits", "provider_park", "live_turn",
    }
    for wait in waits
  ):
    return {"kind": "automatic", "reason": park.get("reason")}
  if goal and goal.get("pause_reason") == "deferred":
    return {"kind": "on_hold", "reason": "deferred", "goal_id": goal["id"],
            "hold_reason": goal["hold_reason"]}
  if goal and goal.get("dependencies") and goal.get("handoff", {}).get("kind") in {"automatic", "recovery", "blocked", "owner_hold"}:
    handoff = goal["handoff"]
    if handoff["kind"] == "owner_hold":
      return {"kind": "on_hold", "reason": "owner", "goal_id": goal["id"]}
    return handoff
  return {"kind": "none", "reason": None}
