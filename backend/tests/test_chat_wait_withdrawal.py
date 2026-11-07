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
  with pytest.raises(_PersistFailed, match="Wait (continuation|result) is no longer receivable"):
    _admit(chat, run, wait).result(timeout=5)
  with pytest.raises(_PersistFailed, match="admitted run not found"):
    _receipt(chat, run, wait).result(timeout=5)
  db.expire_all()
  assert not db.get(models.ChatRun, run.id).provider_execution_admitted
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


@pytest.mark.parametrize("status", ["running", "failed", "interrupted", "completed"])
@pytest.mark.parametrize("admitted", [True, None])
def test_admitted_direct_carrier_cannot_be_called_unstarted_even_without_prompt_snapshot(
  client, auth, db, chat, status, admitted,
):
  wait = _wait(db, chat)
  run = _receiver(db, chat, "wait-resume-" + wait.id + ".retry")
  run.status = status
  run.provider_execution_admitted = admitted
  db.commit()
  assert _cancel(client, auth, wait)["disposition"] == "not_withdrawable"
  db.refresh(wait)
  assert wait.result_disposition is None and wait.cancelled_at is None


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
  published = []
  monkeypatch.setattr(chat_waits, "_broadcast_changed", published.append)
  monkeypatch.setattr(chat_writer, "_commit_or_rollback", lambda session: session.rollback() or False)
  with pytest.raises(_PersistFailed, match="WithdrawWait did not persist"):
    get_writer().submit(chat_writer.WithdrawWait(chat_id=chat.id, wait_id=wait.id)).result(timeout=5)
  db.refresh(wait)
  assert wait.result_disposition is None and wait.cancelled_at is None
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
  with pytest.raises(_PersistFailed, match="Wait continuation is no longer receivable"):
    writer.submit(AdmitProviderExecution(chat_id=chat.id,
      run_token=token)).result(timeout=5)
  db.expire_all()
  assert db.get(models.ChatRun, token).provider_execution_admitted is False
