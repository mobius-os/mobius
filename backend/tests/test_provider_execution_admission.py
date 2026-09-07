"""Only an exact, never-admitted current run can cross provider entry."""

import pytest

from app import models
from app.chat_writer import AdmitProviderExecution, StartTurn, _PersistFailed, get_writer


def _start(chat_id, token):
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=token,
    user_msg={"role": "user", "content": "Do work", "ts": 1},
  )).result(timeout=5)


@pytest.mark.parametrize("ineligible", ["foreign", "missing", "superseded", "completed", "admitted", "legacy"])
def test_admission_rejects_ineligible_physical_identity(chat, db, ineligible):
  token = "admission-run"
  _start(chat.id, token)
  command = AdmitProviderExecution(chat_id=chat.id, run_token=token)
  if ineligible == "foreign":
    command.chat_id = "different-chat"
  elif ineligible == "missing":
    command.run_token = "missing-run"
  elif ineligible == "superseded":
    _start(chat.id, "new-run")
  else:
    run = db.get(models.ChatRun, token)
    if ineligible == "completed":
      run.status = "completed"
    else:
      run.provider_execution_admitted = True if ineligible == "admitted" else None
    db.commit()
  with pytest.raises(_PersistFailed, match="not eligible"):
    get_writer().submit(command).result(timeout=5)
  db.expire_all()
  if ineligible == "superseded":
    assert db.get(models.ChatRun, "new-run").provider_execution_admitted is False


def test_admission_is_a_one_way_commit_before_provider_entry(chat, db):
  token = "admission-once"
  _start(chat.id, token)
  get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=token)).result(timeout=5)
  db.expire_all()
  assert db.get(models.ChatRun, token).provider_execution_admitted is True
  with pytest.raises(_PersistFailed, match="not eligible"):
    get_writer().submit(AdmitProviderExecution(chat_id=chat.id, run_token=token)).result(timeout=5)
