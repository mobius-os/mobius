"""What a restart entry of the request-list card shows the owner.

Every open request is an entry of the one request-list card
(`owner_input_requests.compose_card`); this module supplies the parts only
the platform may derive: a restart entry's exact action, bound to the boot
that is current when it is shown, and the activation wait its card commit
links, plus why an open request can no longer be shown as asked.

Building an entry never grants anything: a restart entry's action is derived
here, never supplied by the agent.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from app import models


class CardUnavailable(RuntimeError):
  """The card cannot be shown now; the message says why, without secrets."""


def activation_wait_for(question_id: str, run: models.ChatRun, requirement: dict) -> dict:
  """The activation wait a restart entry's card commit creates, linked to that card.

  A ready boot after the owner presses Restart now resumes the run that asked,
  so it can verify what loaded.
  """
  return {
    "id": f"activation-{question_id}",
    "description": "Load the exact committed platform changes",
    "condition_owner": "Möbius startup",
    "condition_json": requirement,
    "created_by_run_id": run.id,
    "root_run_id": run.root_run_id or run.id,
    "goal_id": run.goal_id,
    "linked_question_id": question_id,
  }


def unshowable_reason(db, chat_id: str, row: models.OwnerInputRequest) -> str | None:
  """Why this open request can no longer be shown as asked, or None.

  An approval whose claim this chat no longer holds (released by the agent or
  its Goal's end, completed, or transferred) could authorise an action this
  chat no longer owns. A restart needs a restart-loadable boot to bind to.
  Such a request is withdrawn with this reason, never silently dropped.
  """
  from app import owner_input_requests as requests, restart_ledger

  spec = row.spec_json or {}
  kind = requests.request_kind(spec)
  if kind == requests.APPROVAL:
    claim = db.query(models.AgentWorkClaim).filter(
      models.AgentWorkClaim.work_key == spec["work_key"],
    ).first()
    if (claim is None or claim.owner_chat_id != chat_id
        or claim.released_at is not None or claim.completed_at is not None):
      return (
        "this chat no longer holds the work claim for its action; add a new "
        "approval request if the action is still needed"
      )
  if kind == requests.RESTART and not restart_ledger.current_boot_id():
    return "Möbius is not running a restart-loadable platform boot"
  return None


def restart_entry(run_id: str, source_boot_id: str, question_id: str) -> dict:
  """A restart request's entry on the request-list card.

  The caller supplies no command, commit or option identity: the requirement
  names the boot current now and the paths a restart would load, the action
  id binds the entry to this run and boot (`restart_condition_id`), and its
  one option, Restart now, has a platform-derived id. The entry carries its
  own `platform_action`, whose `wait_id` names the activation wait this
  card's commit creates.
  """
  from app.platform_restart import (
    CONDITION_VERSION,
    pending_restart_paths,
    restart_condition_id,
    uncommitted_restart_paths,
  )

  if not source_boot_id:
    raise CardUnavailable("Möbius is not running a restart-loadable platform boot.")
  action_id = restart_condition_id(source_boot_id, run_id)
  requirement = {
    "version": CONDITION_VERSION,
    "source_boot_id": source_boot_id,
    "action_id": action_id,
    "paths": pending_restart_paths(),
  }
  restart_now_id = str(uuid5(NAMESPACE_URL, f"{action_id}:restart-now"))

  def summarize(paths: list[str]) -> str:
    summary = ", ".join(paths[:3])
    if len(paths) > 3:
      summary += f", and {len(paths) - 3} more"
    return summary

  paths = requirement["paths"]
  question = (
    f"Restart to load the tested platform changes in {summarize(paths)}?"
    if paths else "Restart Möbius now?"
  )
  uncommitted = uncommitted_restart_paths()
  if uncommitted:
    question += (
      f" It will also load uncommitted edits in {summarize(uncommitted)}, "
      "which may be another chat's unfinished work."
    )
  question += (
    " This interrupts active turns and Möbius may be unavailable for "
    "tens of seconds."
  )
  return {
    "kind": "restart",
    "header": "Restart Möbius",
    "question": question,
    "options": [{
      "id": restart_now_id,
      "label": "Restart now",
      "on_answer": "close",
      "description": "Drain active work and restart once to load the current platform source.",
    }],
    "platform_action": {
      "version": 2,
      "type": "restart",
      "action_id": action_id,
      "restart_option_id": restart_now_id,
      "requirement": requirement,
      "status": "awaiting_owner",
      "wait_id": f"activation-{question_id}",
    },
  }


def entry_activation_wait(card: dict | None, run: models.ChatRun | None) -> dict | None:
  """The activation wait for the restart entry a request-list card shows."""
  from app.owner_input_requests import RESTART, entry_kind

  entry = next((
    question for question in (card or {}).get("questions") or []
    if entry_kind(question) == RESTART
  ), None)
  if entry is None:
    return None
  if run is None:
    raise CardUnavailable("the publishing run is missing")
  return activation_wait_for(card["question_id"], run, entry["platform_action"]["requirement"])
