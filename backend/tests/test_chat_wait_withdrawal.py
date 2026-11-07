"""Ordinary Wait withdrawal is ordered with admission, never provider rollback."""

import pytest

from app import chat_waits, models
from app.chat_writer import (
  AcknowledgeProviderSuccess, AdmitProviderExecution, AppendPending,
  PromotePending, StartContinuation, StartContinuationBlocked, _PersistFailed,
  get_writer,
)
from app.timeutil import now_naive_utc


def _wait(db, chat, status="met"):
  row = chat_waits.declare_wait(db, chat_id=chat.id,
    description="Observe a synthetic result", kind="timer", delay_secs=60)
  row.status = status
  if status == "met":
    row.met_at = now_naive_utc()
  db.commit()
  return row


def _cancel(client, auth, wait):
  response = client.post(f"/api/chat-waits/{wait.id}/cancel", headers=auth)
  assert response.status_code == 200, response.text
  return response.json()


def _receiver(db, chat, token="receiving-run"):
  run = models.ChatRun(id=token, root_run_id=token, chat_id=chat.id,
    status="running", provider="claude", provider_execution_admitted=False)
  db.add(run)
  db.commit()
  return run


def _admit(chat, run, wait):
  return get_writer().submit(AdmitProviderExecution(chat_id=chat.id,
    run_token=run.id, wait_results=(wait.id,)))


def _receipt(chat, run, wait):
  return get_writer().submit(AcknowledgeProviderSuccess(chat_id=chat.id,
    run_token=run.id, wait_results=(wait.id,)))


def _direct_carrier(db, chat, wait, suffix=""):
  token = "wait-resume-" + wait.id + suffix
  source_id = "source-" + wait.id
  db.add(models.ChatRun(id=source_id, chat_id=chat.id, status="completed",
    provider="claude", provider_execution_admitted=False))
  db.commit()
  result = get_writer().submit(StartContinuation(chat_id=chat.id,
    root_run_id=source_id, run_token=token, cid="wait-result-" + wait.id,
    message_kind="wait_result", content="Synthetic result", reason="wait_result",
    hidden=True)).result(timeout=5)
  assert isinstance(result, dict)
  db.expire_all()
  assert chat.live_assistant["id"] == chat.active_assistant_message_id == token
  assert get_writer()._run_token_owner[chat.id] == token
  return db.get(models.ChatRun, token)


@pytest.mark.parametrize("suffix", ["", ".retry"])
def test_withdrawal_retires_never_admitted_carrier_and_its_live_message(client, auth, db, chat, suffix):
  wait = _wait(db, chat)
  carrier = _direct_carrier(db, chat, wait, suffix)
  assert carrier.status == "running" and carrier.provider_execution_admitted is False
  assert _cancel(client, auth, wait)["disposition"] == "withdrawn"
  db.expire_all()
  assert carrier.status == "interrupted" and carrier.end_reason == "wait_withdrawn"
  assert carrier.ended_at is not None and carrier.provider_execution_admitted is False
  assert chat.live_assistant is None and chat.active_assistant_message_id is None
  assert chat.id not in get_writer()._run_token_owner
  with pytest.raises(_PersistFailed, match="run is not eligible"):
    _admit(chat, carrier, wait).result(timeout=5)


def test_withdrawal_keeps_an_unrelated_running_turn_and_live_message(client, auth, db, chat):
  wait = _wait(db, chat)
  other = _wait(db, chat)
  carrier = _direct_carrier(db, chat, other)
  assert _cancel(client, auth, wait)["disposition"] == "withdrawn"
  db.expire_all()
  assert carrier.status == "running" and carrier.ended_at is None
  assert chat.live_assistant["id"] == chat.active_assistant_message_id == carrier.id
  assert get_writer()._run_token_owner[chat.id] == carrier.id


@pytest.mark.parametrize("admitted", [True, None])
def test_refused_withdrawal_keeps_live_admitted_carrier_and_its_message(client, auth, db, chat, admitted):
  wait = _wait(db, chat)
  carrier = _direct_carrier(db, chat, wait)
  carrier.provider_execution_admitted = admitted
  db.commit()
  assert _cancel(client, auth, wait)["disposition"] == "not_withdrawable"
  db.expire_all()
  assert carrier.status == "running" and carrier.ended_at is None
  assert chat.live_assistant["id"] == chat.active_assistant_message_id == carrier.id
  assert get_writer()._run_token_owner[chat.id] == carrier.id


@pytest.mark.parametrize("status", ["armed", "met", "expired", "failed"])
def test_cancel_withdraws_armed_or_owed_result_and_is_idempotent(client, auth, db, chat, status):
  wait = _wait(db, chat, status)
  result = _cancel(client, auth, wait)
  assert result["disposition"] == "withdrawn"
  assert result["result_disposition"] == "withdrawn"
  assert result["status"] == ("cancelled" if status == "armed" else status)
  assert result["resume_delivered_at"] is None
  assert not result["delivery_pending"]
  db.expire_all()
  assert chat_waits.owed_wait_results(db, chat.id, "", run_id=None) == ("", ())
  assert chat_waits.outstanding_waits_for_chat(db, chat.id) == []
  assert _cancel(client, auth, wait) == result


@pytest.mark.parametrize("status", ["met", "expired", "failed"])
@pytest.mark.parametrize("carrier", ["queued", "direct"])
def test_cancel_owed_result_prevents_queued_or_direct_carrier_admission(
  client, auth, db, chat, status, carrier,
):
  wait = _wait(db, chat, status)
  notice = {"role": "user", "kind": "wait_result", "hidden": True,
    "cid": "wait-result-" + wait.id, "content": "Synthetic result", "ts": 1}
  if carrier == "queued":
    get_writer().submit(AppendPending(chat_id=chat.id, user_msg=notice)).result(timeout=5)
  _cancel(client, auth, wait)
  if carrier == "queued":
    result = get_writer().submit(PromotePending(chat_id=chat.id,
      run_token="queued-carrier")).result(timeout=5)
    assert result["promoted"] is None
  else:
    result = get_writer().submit(StartContinuation(chat_id=chat.id,
      root_run_id="source", run_token="wait-resume-" + wait.id,
      cid=notice["cid"], message_kind="wait_result", content=notice["content"],
      reason="wait_result", hidden=True)).result(timeout=5)
    assert isinstance(result, StartContinuationBlocked) and result.reason == "withdrawn"
  db.expire_all()
  assert db.query(models.ChatRun).filter_by(chat_id=chat.id).count() == 0


@pytest.mark.parametrize("carrier", ["ordinary", "direct"])
def test_cancel_after_selection_but_before_provider_entry_refuses_stale_prompt(
  client, auth, db, chat, carrier,
):
  wait = _wait(db, chat)
  run = _receiver(db, chat, "wait-resume-" + wait.id if carrier == "direct" else "ordinary")
  assert chat_waits.owed_wait_results(db, chat.id, "", run_id=run.id)[1] == (wait.id,)
  assert _cancel(client, auth, wait)["disposition"] == "withdrawn"
  refusal = "run is not eligible" if carrier == "direct" else "Wait result is no longer receivable"
  with pytest.raises(_PersistFailed, match=refusal):
    _admit(chat, run, wait).result(timeout=5)
  with pytest.raises(_PersistFailed, match="admitted run not found"):
    _receipt(chat, run, wait).result(timeout=5)
  db.expire_all()
  db.refresh(run)
  assert not run.provider_execution_admitted
  assert run.status == ("interrupted" if carrier == "direct" else "running")
  assert run.end_reason == ("wait_withdrawn" if carrier == "direct" else None)
  assert db.get(models.ChatWait, wait.id).result_disposition == "withdrawn"
  assert db.get(models.ChatWait, wait.id).received_run_id is None


def test_provider_entry_wins_cancel_without_claiming_rollback_or_losing_receipt(client, auth, db, chat):
  wait = _wait(db, chat)
  run = _receiver(db, chat)
  # Admission commits before success; cancel may not claim that prompt was undone.
  _admit(chat, run, wait).result(timeout=5)
  result = _cancel(client, auth, wait)
  assert result["disposition"] == "not_withdrawable"
  assert "provider" in result["detail"]
  assert result["result_disposition"] is None
  assert result["delivery_pending"]
  _receipt(chat, run, wait).result(timeout=5)
  result = _cancel(client, auth, wait)
  assert result["disposition"] == "already_delivered"
  assert result["result_disposition"] == "delivered"
  assert result["received_run_id"] == run.id


def test_cancel_after_provider_success_preserves_delivered_history(client, auth, db, chat):
  wait = _wait(db, chat)
  run = _receiver(db, chat)
  _admit(chat, run, wait).result(timeout=5)
  _receipt(chat, run, wait).result(timeout=5)
  db.refresh(wait)
  before = chat_waits.serialize_wait(wait, db=db)
  result = _cancel(client, auth, wait)
  assert result.pop("disposition") == "already_delivered"
  assert result == before
  db.refresh(wait)
  assert wait.cancelled_at is None


def test_retired_result_is_not_reported_as_withdrawn_or_delivered(client, auth, db, chat):
  wait = _wait(db, chat)
  wait.result_disposition = "retired_by_goal_outcome"
  wait.result_disposed_at = now_naive_utc()
  db.commit()
  result = _cancel(client, auth, wait)
  assert result["disposition"] == "not_withdrawable"
  assert result["result_disposition"] == "retired_by_goal_outcome"


@pytest.mark.parametrize("status", ["armed", "met"])
def test_activation_wait_withdrawal_remains_restart_card_owned(client, auth, db, chat, status):
  wait = _wait(db, chat, status)
  wait.kind = "platform_activation"
  db.commit()
  response = client.post(f"/api/chat-waits/{wait.id}/cancel", headers=auth)
  assert response.status_code == 409
  db.refresh(wait)
  assert wait.status == status and wait.result_disposition is None


def test_cancel_and_provider_entry_are_serialized_in_writer_order(db, chat):
  from app.chat_writer import WithdrawWait
  wait = _wait(db, chat)
  run = _receiver(db, chat)
  writer = get_writer()
  withdrawn = writer.submit(WithdrawWait(chat_id=chat.id, wait_id=wait.id))
  admission = _admit(chat, run, wait)
  assert withdrawn.result(timeout=5)["disposition"] == "withdrawn"
  with pytest.raises(_PersistFailed, match="Wait result is no longer receivable"):
    admission.result(timeout=5)


def test_provider_entry_then_cancel_then_receipt_keep_truthful_order(db, chat):
  from app.chat_writer import WithdrawWait
  wait = _wait(db, chat)
  run = _receiver(db, chat)
  writer = get_writer()
  admission = _admit(chat, run, wait)
  cancel = writer.submit(WithdrawWait(chat_id=chat.id, wait_id=wait.id))
  receipt = _receipt(chat, run, wait)
  admission.result(timeout=5)
  result = cancel.result(timeout=5)
  assert result["disposition"] == "not_withdrawable"
  assert result["result_disposition"] is None  # The response is the cancellation snapshot.
  receipt.result(timeout=5)
  final = writer.submit(WithdrawWait(chat_id=chat.id, wait_id=wait.id)).result(timeout=5)
  assert final["disposition"] == "already_delivered"
  assert final["result_disposition"] == "delivered"


@pytest.mark.parametrize("status", [
  "running", "parked", "resume_pending", "unknown", "failed", "interrupted",
  "completed", "stopped", "parked_notified",
])
@pytest.mark.parametrize("admitted", [True, None])
def test_only_live_or_unknown_admitting_carriers_refuse_future_withdrawal(
  client, auth, db, chat, status, admitted,
):
  wait = _wait(db, chat)
  run = _receiver(db, chat, "wait-resume-" + wait.id + ".retry")
  run.status = status
  run.provider_execution_admitted = admitted
  db.commit()
  terminal = status in {"failed", "interrupted", "completed", "stopped", "parked_notified"}
  result = _cancel(client, auth, wait)
  assert result["disposition"] == ("withdrawn_after_attempt" if terminal else "not_withdrawable")
  assert not result["can_withdraw"]
  db.refresh(wait)
  if terminal:
    assert wait.result_disposition == "withdrawn_after_attempt"
    assert wait.cancelled_at is not None
    assert wait.resume_delivered_at is None and wait.received_run_id is None
  else:
    assert wait.result_disposition is None and wait.cancelled_at is None
    if status != "running":
      assert "Use Stop" not in result["detail"]


def test_other_results_admitted_on_same_chat_do_not_prevent_this_withdrawal(client, auth, db, chat):
  wait = _wait(db, chat)
  run = _receiver(db, chat)
  run.provider_execution_admitted = True
  run.admitted_wait_results_json = ["some-other-wait"]
  db.commit()
  assert _cancel(client, auth, wait)["disposition"] == "withdrawn"


def test_writer_withdrawal_cannot_cross_chat_or_restart_card_ownership(db, chat):
  from app.chat_writer import WithdrawWait
  wait = _wait(db, chat)
  result = get_writer().submit(WithdrawWait(chat_id="another-chat", wait_id=wait.id)).result(timeout=5)
  assert result["disposition"] == "not_withdrawable"
  db.refresh(wait)
  assert wait.result_disposition is None
  wait.kind = "platform_activation"
  db.commit()
  result = get_writer().submit(WithdrawWait(chat_id=chat.id, wait_id=wait.id)).result(timeout=5)
  assert result["disposition"] == "not_withdrawable"
  db.refresh(wait)
  assert wait.result_disposition is None


def test_failed_withdrawal_commit_does_not_report_or_publish_success(db, chat, monkeypatch):
  from app import chat_writer
  wait = _wait(db, chat)
  carrier = _direct_carrier(db, chat, wait)
  published = []
  monkeypatch.setattr(chat_waits, "_broadcast_changed", published.append)
  monkeypatch.setattr(chat_writer, "_commit_or_rollback", lambda session: session.rollback() or False)
  with pytest.raises(_PersistFailed, match="WithdrawWait did not persist"):
    get_writer().submit(chat_writer.WithdrawWait(chat_id=chat.id, wait_id=wait.id)).result(timeout=5)
  db.expire_all()
  assert wait.result_disposition is None and wait.cancelled_at is None
  assert carrier.status == "running" and carrier.ended_at is None
  assert chat.live_assistant["id"] == chat.active_assistant_message_id == carrier.id
  assert get_writer()._run_token_owner[chat.id] == carrier.id
  assert published == []


@pytest.mark.parametrize("status", ["met", "expired", "failed"])
def test_promoted_wait_notice_is_still_withdrawable_before_provider_entry(client, auth, db, chat, status):
  wait = _wait(db, chat, status)
  writer = get_writer()
  writer.submit(AppendPending(chat_id=chat.id, user_msg={
    "role": "user", "kind": "wait_result", "hidden": True, "ts": 1,
    "cid": "wait-result-" + wait.id, "content": "Synthetic result",
  })).result(timeout=5)
  result = writer.submit(PromotePending(chat_id=chat.id,
    run_token="provisional")).result(timeout=5)
  token = result["promoted"]["_run_token"]
  assert token == "wait-resume-" + wait.id
  assert _cancel(client, auth, wait)["disposition"] == "withdrawn"
  # Even if prompt construction now finds no owed IDs, the promoted raw
  # notice cannot slip through provider admission under a provisional ID.
  with pytest.raises(_PersistFailed, match="run is not eligible"):
    writer.submit(AdmitProviderExecution(chat_id=chat.id,
      run_token=token)).result(timeout=5)
  db.expire_all()
  carrier = db.get(models.ChatRun, token)
  assert carrier.provider_execution_admitted is False
  assert carrier.status == "interrupted" and carrier.end_reason == "wait_withdrawn"


@pytest.mark.parametrize("status", ["failed", "interrupted", "completed", "stopped", "parked_notified"])
def test_withdraw_after_ended_attempt_stops_injection_into_the_next_ordinary_turn(
  client, auth, db, chat, status,
):
  wait = _wait(db, chat)
  attempted = _receiver(db, chat, "attempt-a")
  _admit(chat, attempted, wait).result(timeout=5)
  attempted.status = status
  attempted.ended_at = now_naive_utc()
  db.commit()
  assert chat_waits.owed_wait_results(db, chat.id, "", run_id=attempted.id)[1] == (wait.id,)
  assert chat_waits.serialize_wait(wait, db=db)["can_withdraw"]
  result = _cancel(client, auth, wait)
  assert result["disposition"] == result["result_disposition"] == "withdrawn_after_attempt"
  assert "not undone" in result["detail"]
  assert not result["delivery_pending"]
  assert _cancel(client, auth, wait) == result
  # A late acknowledgement cannot convert withdrawal of future delivery into
  # a receipt, and cancellation never rewrites the earlier execution record.
  _receipt(chat, attempted, wait).result(timeout=5)
  db.refresh(attempted)
  assert attempted.status == status and attempted.provider_execution_admitted
  assert attempted.admitted_wait_results_json == [wait.id]
  receiving = _receiver(db, chat, "ordinary-b")
  assert chat_waits.owed_wait_results(db, chat.id, "Continue", run_id=receiving.id) == ("", ())
  assert chat_waits.wait_delivery_blocker(db, db.get(models.ChatWait, wait.id)) == "withdrawn"
  with pytest.raises(_PersistFailed, match="Wait result is no longer receivable"):
    _admit(chat, receiving, wait).result(timeout=5)
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id,
    run_token=receiving.id)).result(timeout=5)
  db.expire_all()
  assert db.get(models.ChatRun, receiving.id).admitted_wait_results_json == []
  assert db.get(models.ChatWait, wait.id).result_disposition == "withdrawn_after_attempt"


def test_one_live_admitting_run_blocks_withdrawal_until_every_attempt_ends(client, auth, db, chat):
  wait = _wait(db, chat)
  ended = _receiver(db, chat, "failed-attempt")
  _admit(chat, ended, wait).result(timeout=5)
  ended.status = "failed"
  db.commit()
  live = _receiver(db, chat, "live-attempt")
  _admit(chat, live, wait).result(timeout=5)
  assert not chat_waits.serialize_wait(wait, db=db)["can_withdraw"]
  assert _cancel(client, auth, wait)["disposition"] == "not_withdrawable"
  live.status = "interrupted"
  db.commit()
  assert chat_waits.serialize_wait(wait, db=db)["can_withdraw"]
  assert _cancel(client, auth, wait)["disposition"] == "withdrawn_after_attempt"


@pytest.mark.parametrize("withdraw_first", [True, False])
def test_future_withdrawal_after_an_ended_attempt_orders_against_new_provider_entry(db, chat, withdraw_first):
  from app.chat_writer import WithdrawWait
  wait = _wait(db, chat)
  ended = _receiver(db, chat, "ended-attempt")
  _admit(chat, ended, wait).result(timeout=5)
  ended.status = "failed"
  db.commit()
  next_run = _receiver(db, chat, "new-attempt")
  writer = get_writer()
  command = WithdrawWait(chat_id=chat.id, wait_id=wait.id)
  if withdraw_first:
    cancel = writer.submit(command)
    admission = _admit(chat, next_run, wait)
    assert cancel.result(timeout=5)["disposition"] == "withdrawn_after_attempt"
    with pytest.raises(_PersistFailed, match="Wait result is no longer receivable"):
      admission.result(timeout=5)
  else:
    admission = _admit(chat, next_run, wait)
    cancel = writer.submit(command)
    admission.result(timeout=5)
    assert cancel.result(timeout=5)["disposition"] == "not_withdrawable"
  db.expire_all()
  assert db.get(models.ChatRun, next_run.id).provider_execution_admitted is not withdraw_first


def test_never_entered_terminal_carrier_does_not_claim_an_earlier_attempt(client, auth, db, chat):
  wait = _wait(db, chat)
  run = _receiver(db, chat, "wait-resume-" + wait.id)
  run.status = "failed"
  db.commit()
  assert chat_waits.serialize_wait(wait, db=db)["can_withdraw"]
  assert _cancel(client, auth, wait)["disposition"] == "withdrawn"
