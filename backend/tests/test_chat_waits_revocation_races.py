"""Exact-Goal receipts cannot be stolen by a later turn or revoked at admission.

Unlike the former chat-wide contract, an unrelated successful turn must leave
another open Goal's result owed. Goal-less Waits still ride any chat turn.
"""
import asyncio

import pytest

from app import chat as chat_mod, chat_waits, models
from app.chat_writer import (
  AcknowledgeProviderSuccess, AdmitProviderExecution, AppendPending,
  PromotePending, StartContinuation, StartContinuationBlocked, get_writer, _PersistFailed,
)
from app.timeutil import now_naive_utc


def _run(db, chat, name, goal_id=None, *, admitted=True):
  if goal_id and db.get(models.ChatGoal, goal_id) is None:
    db.add(models.ChatGoal(id=goal_id, chat_id=chat.id, objective=goal_id))
  row = models.ChatRun(id=name, root_run_id=name, chat_id=chat.id,
    goal_id=goal_id, status="completed", provider="claude",
    provider_execution_admitted=admitted)
  db.add(row)
  db.commit()
  return row


def _wait(db, chat, run):
  row = chat_waits.declare_wait(db, chat_id=chat.id, created_by_run_id=run.id,
    description="Result for " + run.id, kind="timer", delay_secs=60)
  row.status = "met"
  row.met_at = now_naive_utc()
  # Most unit tests model a provider that carried this exact result.
  run.admitted_wait_results_json = list(run.admitted_wait_results_json or []) + [row.id]
  db.commit()
  return row


def _owed(db, chat, run):
  return chat_waits.owed_wait_results(db, chat.id, "", run_id=run.id)


def _ack(chat, run, ids):
  get_writer().submit(AcknowledgeProviderSuccess(
    chat_id=chat.id, run_token=run.id, wait_results=ids,
  )).result(timeout=5)


def test_two_open_goals_and_goalless_turn_only_receive_their_own_results(db, chat):
  a = _run(db, chat, "run-a", "goal-a")
  b = _run(db, chat, "run-b", "goal-b")
  plain = _run(db, chat, "plain")
  wa, wb, unowned = (_wait(db, chat, run) for run in (a, b, plain))
  all_ids = (wa.id, wb.id, unowned.id)

  text, ids = _owed(db, chat, plain)
  assert ids == (unowned.id,) and wa.description not in text and wb.description not in text
  # Even a stale/incorrect caller-supplied ID list cannot acknowledge A or B.
  _ack(chat, plain, all_ids)
  db.expire_all()
  assert db.get(models.ChatWait, unowned.id).received_run_id == plain.id
  assert db.get(models.ChatWait, wa.id).result_disposition is None
  assert db.get(models.ChatWait, wb.id).result_disposition is None

  assert _owed(db, chat, b)[1] == (wb.id,)
  _ack(chat, b, all_ids)
  db.expire_all()
  assert db.get(models.ChatWait, wb.id).received_run_id == b.id
  assert db.get(models.ChatWait, wa.id).result_disposition is None
  assert _owed(db, chat, a)[1] == (wa.id,)
  _ack(chat, a, all_ids)
  db.expire_all()
  assert db.get(models.ChatWait, wa.id).received_run_id == a.id
  assert all(db.get(models.ChatWait, wid).result_disposition == "delivered" for wid in all_ids)
  assert _owed(db, chat, a) == ("", ())


def test_unrelated_turn_does_not_lose_the_exact_goal_continuation(db, chat, monkeypatch):
  a = _run(db, chat, "source-a", "goal-a")
  wait = _wait(db, chat, a)
  b = _run(db, chat, "unrelated-b", "goal-b")
  assert _owed(db, chat, b) == ("", ())
  _ack(chat, b, (wait.id,))
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert asyncio.run(chat_waits._deliver_resume(wait.id)) is True
  db.expire_all()
  receiver = db.get(models.ChatRun, "wait-resume-" + wait.id)
  assert len(scheduled) == 1 and receiver.goal_id == a.goal_id
  assert _owed(db, chat, receiver)[1] == (wait.id,)
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=receiver.id,
    wait_results=(wait.id,))).result(timeout=5)
  _ack(chat, receiver, (wait.id,))
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).received_run_id == receiver.id
  assert not asyncio.run(chat_waits._deliver_resume(wait.id))


def test_hold_after_context_selection_keeps_result_owed_until_own_continuation(db, chat):
  a = _run(db, chat, "run-a", "goal-a")
  wait = _wait(db, chat, a)
  assert _owed(db, chat, a)[1] == (wait.id,)
  db.get(models.ChatGoal, a.goal_id).status = "stopped"
  db.commit()
  _ack(chat, a, (wait.id,))
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).received_run_id is None
  assert db.get(models.ChatWait, wait.id).result_disposition is None
  assert _owed(db, chat, a) == ("", ())
  assert not asyncio.run(chat_waits._deliver_resume(wait.id))
  assert db.get(models.ChatGoal, a.goal_id).status == "stopped"
  # Models the later owner-authorized reopening, not an automatic transition.
  db.get(models.ChatGoal, a.goal_id).status = "open"
  db.commit()
  continued = _run(db, chat, "owner-continued-a", a.goal_id)
  continued.admitted_wait_results_json = list(_owed(db, chat, continued)[1])
  db.commit()
  _ack(chat, continued, tuple(continued.admitted_wait_results_json))
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).received_run_id == continued.id


@pytest.mark.parametrize("change", ["hold", "retire", "withdraw"])
def test_changed_result_cannot_cross_provider_admission(db, chat, change):
  a = _run(db, chat, "run-a", "goal-a", admitted=False)
  wait = _wait(db, chat, a)
  a.status = "running"
  if change == "hold":
    db.get(models.ChatGoal, a.goal_id).status = "stopped"
  elif change == "retire":
    chat_waits.stage_retire_fired_goal_waits(db, chat.id, a.goal_id)
  else:
    wait.result_disposition = "withdrawn"
    wait.result_disposed_at = now_naive_utc()
  db.commit()
  with pytest.raises(_PersistFailed, match="Wait result is no longer receivable"):
    get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=a.id,
      wait_results=(wait.id,))).result(timeout=5)
  db.expire_all()
  assert not db.get(models.ChatRun, a.id).provider_execution_admitted
  assert db.get(models.ChatWait, wait.id).received_run_id is None


def test_chat_admission_reselects_after_selected_wait_is_withdrawn(db, chat):
  run = _run(db, chat, "ordinary-turn", "goal-a", admitted=False)
  wait = _wait(db, chat, run)
  run.status = "running"
  db.commit()
  selected, wait_ids, dependency_ids = chat_mod._selected_result_context(
    db, chat.id, run.id, "Continue work",
  )
  assert wait.id in wait_ids and wait.description in selected
  wait.result_disposition = "withdrawn"
  wait.result_disposed_at = now_naive_utc()
  db.commit()
  wait_id, description, run_id = wait.id, wait.description, run.id
  admitted, prompt, delivered_waits, delivered_dependencies = asyncio.run(
    chat_mod._admit_with_selected_results(
      db, chat.id, run.id, None, raw_message="Continue work",
      user_message="before <results> Continue work", context_marker="<results>",
      selected_context=selected, wait_results=wait_ids,
      dependency_results=dependency_ids, has_peer_context_delivery=False,
      activity_results=(),
    )
  )
  assert admitted and "Continue work" in prompt
  assert description not in prompt and wait_id not in prompt
  assert delivered_waits == delivered_dependencies == ()
  db.expire_all()
  assert db.get(models.ChatRun, run_id).provider_execution_admitted


def test_held_wait_notice_cannot_start_or_promote_goalless_work(db, chat):
  a = _run(db, chat, "run-a", "goal-a")
  wait = _wait(db, chat, a)
  notice = {"role": "user", "kind": "wait_result", "hidden": True,
    "cid": "wait-result-" + wait.id, "source_work_id": a.id,
    "content": "The result", "ts": 1}
  get_writer().submit(AppendPending(chat_id=chat.id, user_msg=notice)).result(timeout=5)
  db.get(models.ChatGoal, a.goal_id).status = "stopped"
  db.commit()
  direct = get_writer().submit(StartContinuation(chat_id=chat.id,
    root_run_id=a.id, run_token="wait-resume-" + wait.id,
    cid=notice["cid"], message_kind="wait_result", source_work_id=a.id,
    content=notice["content"], reason="wait_result", hidden=True)).result(timeout=5)
  assert isinstance(direct, StartContinuationBlocked) and direct.reason == "goal_held"
  queued = get_writer().submit(PromotePending(chat_id=chat.id, run_token="no-goal" )).result(timeout=5)
  assert queued["promoted"] is None
  db.expire_all()
  assert db.get(models.ChatRun, "no-goal") is None
  assert db.get(models.ChatWait, wait.id).result_disposition is None
  assert db.get(models.ChatGoal, a.goal_id).status == "stopped"
  db.get(models.ChatGoal, a.goal_id).status = "open"
  db.commit()
  continued = _run(db, chat, "owner-resumes-queued", a.goal_id)
  assert _owed(db, chat, continued)[1] == (wait.id,)


def test_success_cannot_receipt_a_result_not_in_its_admitted_prompt(db, chat):
  a = _run(db, chat, "run-a", "goal-a", admitted=False)
  a.status = "running"
  db.commit()
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=a.id)).result(timeout=5)
  wait = _wait(db, chat, a)
  # Undo the fixture's simulated prompt snapshot: this result fired later.
  a.admitted_wait_results_json = []
  db.commit()
  _ack(chat, a, (wait.id,))
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).received_run_id is None
  assert db.get(models.ChatWait, wait.id).result_disposition is None


def test_activation_keeps_scheduling_receipt_without_claiming_provider_delivery(db, chat):
  source = _run(db, chat, "activation-source")
  wait = _wait(db, chat, source)
  wait.kind = "platform_activation"
  db.commit()
  notice = {"kind": "platform_activation_result", "cid": "activation-result-" + wait.id,
    "source_work_id": source.id}
  from app.continuations import PLATFORM_ACTIVATION_RESULT_MESSAGE_KIND
  notice["kind"] = PLATFORM_ACTIVATION_RESULT_MESSAGE_KIND
  assert chat_waits.claim_scheduled_wait_result(chat.id, notice)
  assert not chat_waits.claim_scheduled_wait_result(chat.id, notice)
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).result_disposition == "scheduled"
  assert db.get(models.ChatWait, wait.id).received_run_id is None
  assert db.get(models.ChatWait, wait.id).resume_delivered_at is not None


def test_new_receipt_anchors_to_exact_run_not_overlapping_execution_times(db, chat):
  from datetime import timedelta, UTC
  a = _run(db, chat, "receiver-a", "goal-a")
  b = _run(db, chat, "unrelated-b", "goal-b")
  wait = _wait(db, chat, a)
  _ack(chat, a, (wait.id,))
  db.expire_all()
  now = now_naive_utc()
  # Both intervals overlap the receipt; a timestamp guess cannot identify it.
  for run in (a, b):
    run.started_at = now - timedelta(seconds=10)
    run.ended_at = now + timedelta(seconds=10)
  db.commit()
  messages = [{"role": "assistant", "id": run.id,
    "ts": (now - timedelta(seconds=5)).replace(tzinfo=UTC).timestamp() * 1000}
    for run in (b, a)]
  result = chat_waits.terminal_wait_summaries_by_message_index(db, chat.id, messages)
  assert set(result) == {1}
  assert result[1][0]["id"] == wait.id


def test_provider_admission_rejects_another_open_goals_wait(db, chat):
  a = _run(db, chat, "run-a", "goal-a")
  wait = _wait(db, chat, a)
  b = _run(db, chat, "run-b", "goal-b", admitted=False)
  b.status = "running"
  db.commit()
  with pytest.raises(_PersistFailed, match="Wait result is no longer receivable"):
    get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=b.id,
      wait_results=(wait.id,))).result(timeout=5)
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).result_disposition is None
  assert db.get(models.ChatRun, b.id).provider_execution_admitted is False


def test_goal_retirement_cannot_overwrite_an_existing_provider_receipt(db, chat):
  a = _run(db, chat, "run-a", "goal-a")
  wait = _wait(db, chat, a)
  _ack(chat, a, (wait.id,))
  assert chat_waits.stage_retire_fired_goal_waits(db, chat.id, a.goal_id) == 0
  db.commit()
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).result_disposition == "delivered"
  assert db.get(models.ChatWait, wait.id).received_run_id == a.id


def test_unrelated_owner_superseding_unscheduled_carrier_cannot_strand_its_goal(db, chat, monkeypatch):
  from app.chat_writer import StartTurn, FinishRun
  a = _run(db, chat, "source-a", "goal-a")
  wait = _wait(db, chat, a)
  def fail_schedule(**kw):
    # Match the real scheduler's failed-task-creation cleanup.
    chat_mod.discard_starting(kw["chat_id"])
    return False
  monkeypatch.setattr(chat_mod, "_schedule_continuation", fail_schedule)
  assert not asyncio.run(chat_waits._deliver_resume(wait.id))
  original_id = "wait-resume-" + wait.id
  get_writer().submit(StartTurn(chat_id=chat.id, run_token="unrelated-owner", owner_input=True,
    user_msg={"role": "user", "content": "Different work", "ts": 2})).result(timeout=5)
  db.expire_all()
  owner = db.get(models.ChatRun, "unrelated-owner")
  assert owner.goal_id is None and _owed(db, chat, owner) == ("", ())
  original = db.get(models.ChatRun, original_id)
  assert original.status == "interrupted" and original.provider_execution_admitted is False
  assert original.owner_superseded_by_run_id == owner.id
  get_writer().submit(FinishRun(chat_id=chat.id, run_token=owner.id,
    terminal_status="completed")).result(timeout=5)
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert asyncio.run(chat_waits._deliver_resume(wait.id))
  db.expire_all()
  retry = db.get(models.ChatRun, original_id + ".retry")
  assert len(scheduled) == 1 and retry.goal_id == a.goal_id
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=retry.id,
    wait_results=(wait.id,))).result(timeout=5)
  _ack(chat, retry, (wait.id,))
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).received_run_id == retry.id


def test_crash_interruption_is_not_owner_supersession_permission(db, chat, monkeypatch):
  a = _run(db, chat, "source-a", "goal-a")
  wait = _wait(db, chat, a)
  carrier = _run(db, chat, "wait-resume-" + wait.id, a.goal_id, admitted=False)
  carrier.status = "interrupted"
  db.commit()
  monkeypatch.setattr(chat_mod, "_schedule_continuation",
    lambda **kw: pytest.fail("crash repair did not authorize a retry"))
  assert not asyncio.run(chat_waits._deliver_resume(wait.id))
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).result_disposition is None
  assert db.get(models.ChatRun, carrier.id + ".retry") is None
  assert chat_waits.wait_resume_blocker(db, wait) == "manual_resume"
  from app.goal_plans import _goal_handoff
  assert _goal_handoff(db, carrier, db.get(models.ChatGoal, a.goal_id)) == {
    "kind": "recovery", "reason": "manual_resume",
  }


def test_exhausted_owner_supersession_retry_stays_owed_with_manual_recovery(db, chat):
  a = _run(db, chat, "source-a", "goal-a")
  wait = _wait(db, chat, a)
  original = _run(db, chat, "wait-resume-" + wait.id, a.goal_id, admitted=False)
  original.status = "interrupted"
  original.owner_superseded_by_run_id = "first-owner"
  retry = _run(db, chat, original.id + ".retry", a.goal_id, admitted=False)
  retry.status = "interrupted"
  retry.owner_superseded_by_run_id = "second-owner"
  db.commit()
  assert not asyncio.run(chat_waits._deliver_resume(wait.id))
  assert chat_waits.wait_resume_blocker(db, wait) == "resume_failed"
  assert _owed(db, chat, a)[1] == (wait.id,)
  assert db.get(models.ChatWait, wait.id).result_disposition is None


@pytest.mark.parametrize("origin", ["failed", "owner-superseded"])
def test_completed_retry_with_failed_receipt_exposes_recovery_and_manual_resume_receives(
  db, chat, monkeypatch, origin,
):
  from app.chat_writer import ChatWriterActor, FinishRun, StartTurn
  from app.goal_plans import _goal_handoff
  source = _run(db, chat, "source", "goal")
  wait = _wait(db, chat, source)
  original = _run(db, chat, "wait-resume-" + wait.id, source.goal_id, admitted=False)
  original.status = "failed" if origin == "failed" else "interrupted"
  original.owner_superseded_by_run_id = "owner" if origin == "owner-superseded" else None
  db.commit()
  scheduled = []
  def schedule(**kw):
    scheduled.append(kw)
    chat_mod.discard_starting(kw["chat_id"])
    return True
  monkeypatch.setattr(chat_mod, "_schedule_continuation", schedule)
  assert asyncio.run(chat_waits._deliver_resume(wait.id))
  retry_id = original.id + ".retry"
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=retry_id,
    wait_results=(wait.id,))).result(timeout=5)

  def fail_receipt(*args):
    raise _PersistFailed("receipt transaction unavailable")
  # Exercise the real successful-provider boundary's best-effort failure path,
  # followed by successful writer finalization, rather than seeding completion.
  with monkeypatch.context() as patch:
    patch.setattr(ChatWriterActor, "_acknowledge_provider_success", fail_receipt)
    asyncio.run(chat_mod._acknowledge_provider_success(chat_id=chat.id,
      run_token=retry_id, delivered_through=None, wait_results=(wait.id,)))
  get_writer().submit(FinishRun(chat_id=chat.id, run_token=retry_id,
    terminal_status="completed")).result(timeout=5)
  db.expire_all()
  retry = db.get(models.ChatRun, retry_id)
  goal = db.get(models.ChatGoal, source.goal_id)
  assert retry.status == "completed" and retry.provider_execution_admitted
  assert goal.status == "open" and wait.result_disposition is None
  assert not asyncio.run(chat_waits._deliver_resume(wait.id))
  assert len(scheduled) == 1  # No second retry, including after success without receipt.
  assert chat_waits.wait_resume_blocker(db, wait) == "manual_resume"
  assert _goal_handoff(db, retry, goal) == {"kind": "recovery", "reason": "manual_resume"}

  get_writer().submit(StartTurn(chat_id=chat.id, run_token="manual-owner", owner_input=True,
    user_msg={"kind": "continuation", "continuation_reason": "manual", "cid": "manual-owner"},
    resume_goal_id=goal.id, resume_goal_revision=goal.revision)).result(timeout=5)
  db.expire_all()
  resumed = db.get(models.ChatRun, "manual-owner")
  assert resumed.goal_id == goal.id and _owed(db, chat, resumed)[1] == (wait.id,)
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=resumed.id,
    wait_results=(wait.id,))).result(timeout=5)
  _ack(chat, resumed, (wait.id,))
  db.expire_all()
  assert wait.result_disposition == "delivered" and wait.received_run_id == resumed.id


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "withdrawn", "withdrawn_after_attempt"])
def test_non_receipt_history_stays_with_owning_answer_not_unrelated_next_answer(db, chat, outcome):
  from datetime import timedelta, UTC
  source = _run(db, chat, "declaring", "goal")
  wait = _wait(db, chat, source)
  unrelated = _run(db, chat, "unrelated", "other-goal")
  base = now_naive_utc()
  wait.created_at = base - timedelta(seconds=10)
  wait.met_at = base
  if outcome in {"withdrawn", "withdrawn_after_attempt"}:
    wait.result_disposition = outcome
    wait.result_disposed_at = base + timedelta(seconds=10)
  else:
    chat_waits.stage_retire_fired_goal_waits(db, chat.id, source.goal_id)
    db.get(models.ChatGoal, source.goal_id).status = outcome
  db.commit()
  db.expire_all()
  def answer(run, seconds):
    return {"role": "assistant", "id": run.id,
      "ts": (base + timedelta(seconds=seconds)).replace(tzinfo=UTC).timestamp() * 1000}
  messages = [answer(source, -20), answer(unrelated, 2)]
  summaries = chat_waits.terminal_wait_summaries_by_message_index(db, chat.id, messages)
  assert set(summaries) == {0}
  summary = summaries[0][0]
  assert summary["result_disposition"] == wait.result_disposition
  assert summary["delivery_pending"] is False
  assert wait.received_run_id is None and wait.resume_delivered_at is None
  # A page lacking this Goal's answer cannot promote unrelated work to owner.
  assert chat_waits.terminal_wait_summaries_by_message_index(db, chat.id, messages[1:]) == {}


@pytest.mark.parametrize("disposition", [None, "legacy_consumed", "scheduled"])
def test_legacy_and_activation_history_keep_timestamp_receipt_fallback(db, chat, disposition):
  from datetime import timedelta, UTC
  source = _run(db, chat, "declaring")
  wait = _wait(db, chat, source)
  base = now_naive_utc()
  wait.met_at = base
  wait.resume_delivered_at = base + timedelta(seconds=1)
  wait.result_disposition = disposition
  if disposition == "scheduled":
    wait.kind = "platform_activation"
  db.commit()
  messages = [{"role": "assistant", "ts": (base + timedelta(seconds=seconds)).replace(
    tzinfo=UTC).timestamp() * 1000} for seconds in (-10, 2)]
  summaries = chat_waits.terminal_wait_summaries_by_message_index(db, chat.id, messages)
  assert set(summaries) == {1}
  assert summaries[1][0]["result_disposition"] == disposition
  assert summaries[1][0]["delivery_pending"] is False


def test_retirement_history_can_use_visible_own_goal_answer_when_declaration_is_absent(db, chat):
  from datetime import timedelta, UTC
  source = _run(db, chat, "declaring", "goal")
  wait = _wait(db, chat, source)
  retiring = _run(db, chat, "retiring", source.goal_id)
  unrelated = _run(db, chat, "unrelated", "other-goal")
  chat_waits.stage_retire_fired_goal_waits(db, chat.id, source.goal_id)
  db.commit()
  db.expire_all()
  before_disposal = now_naive_utc() - timedelta(seconds=2)
  messages = [{"role": "assistant", "id": run.id,
    "ts": before_disposal.replace(tzinfo=UTC).timestamp() * 1000}
    for run in (retiring, unrelated)]
  summaries = chat_waits.terminal_wait_summaries_by_message_index(db, chat.id, messages)
  assert set(summaries) == {0}
  assert summaries[0][0]["result_disposition"] == "retired_by_goal_outcome"
  assert summaries[0][0]["id"] == wait.id


def test_a_wait_result_starts_while_a_stop_holds_the_owner_queue(db, chat, monkeypatch):
  """Held rows wait for the owner, not for this result: it starts now and
  leaves them held, instead of waiting for the idle-queue sweep."""
  a = _run(db, chat, "held-source", "goal-held")
  wait = _wait(db, chat, a)
  chat.pending_messages = [{"role": "user", "content": "later", "cid": "held", "ts": 1,
                            "held": True, "_owner_input_at": "2020-01-01T00:00:00+00:00"}]
  db.commit()
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert asyncio.run(chat_waits._deliver_resume(wait.id)) is True
  db.expire_all()
  assert len(scheduled) == 1
  assert [(m["cid"], m.get("held")) for m in db.get(models.Chat, chat.id).pending_messages] == [
    ("held", True),
  ]
