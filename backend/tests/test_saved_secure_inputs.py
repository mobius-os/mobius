"""Sealed owner pauses persist safely; an execution claim is never replayed."""
import asyncio
from datetime import timedelta
import json
import sys

import pytest

from app import auth as auth_mod, chat as chat_mod, models, saved_secure_inputs
from app.broadcast import create_broadcast
from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
from app.chat_writer import Barrier, ClaimSecureInput, FinishRun, SettleSecureInput, StartTurn, get_writer
from app.database import SessionLocal
from app.memory_recall import EMPTY_RECALL_BINDING


@pytest.fixture
def sealed_run(chat, db):
  run_id = f"sealed-{chat.id}"
  get_writer().submit(StartTurn(chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Connect locally", "ts": 1})).result(timeout=5)
  bc = create_broadcast(chat.id)
  sink = ChatEventSink(bc, chat.id, run_token=run_id, recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink(chat.id, sink)
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(chat_id=chat.id, owner_username=owner.username, token_epoch=owner.token_epoch,
    run_id=run_id, expires_delta=timedelta(minutes=5))
  yield sink, {"Authorization": f"Bearer {token}"}
  unregister_active_sink(chat.id, sink)


def _spec(tmp_path):
  return {"title": "Connect service", "description": "Local sealed operation.",
          "fields": [{"name": "api_key", "type": "password", "label": "API key"}],
          "command": [sys.executable, "-c", "import json,sys; json.load(sys.stdin)"],
          "cwd": str(tmp_path), "action": "run", "mode": "sealed"}


def _create(client, chat, sealed_run, tmp_path):
  response = client.post(f"/api/secure-inputs/{chat.id}/saved", headers=sealed_run[1], json=_spec(tmp_path))
  assert response.status_code == 200, response.text
  return response.json()["request_id"]


def _state(chat_id, request_id):
  get_writer().submit(Barrier()).result(timeout=5)
  with SessionLocal() as db:
    chat = db.get(models.Chat, chat_id)
    row = db.get(models.SavedSecureInput, request_id)
    return row.status, chat.pending_question_id, chat.messages, chat.pending_messages


def test_saved_card_and_private_operation_commit_together_without_values(client, chat, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  status, marker, messages, pending = _state(chat.id, qid)
  assert status == "pending" and marker == qid and pending == []
  block = messages[-1]["blocks"][-1]
  assert block["secure_input"]["fields"][0]["name"] == "api_key"
  assert block["response_mode"] == "continuation"
  assert "command" not in json.dumps(messages)
  assert "values" not in models.SavedSecureInput.__table__.columns
  second = _create(client, chat, sealed_run, tmp_path)
  assert second == qid


def test_finish_preserves_saved_card_and_stop_retires_execution(client, chat, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  get_writer().submit(FinishRun(chat_id=chat.id, run_token=sealed_run[0].run_token, terminal_status="completed")).result(timeout=5)
  assert _state(chat.id, qid)[:2] == ("pending", qid)
  get_writer().submit(FinishRun(chat_id=chat.id, terminal_status="stopped")).result(timeout=5)
  assert _state(chat.id, qid)[:2] == ("cancelled", None)
  claim = get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5)
  assert claim["status"] == "closed"


def test_execution_claim_and_safe_outcome_are_idempotent(client, chat, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  command = ClaimSecureInput(chat_id=chat.id, request_id=qid)
  assert get_writer().submit(command).result(timeout=5)["status"] == "claimed"
  assert get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5) == {"status": "consuming"}
  for _ in range(2):
    get_writer().submit(SettleSecureInput(chat_id=chat.id, request_id=qid, status="completed", outcome="success")).result(timeout=5)
  status, marker, messages, pending = _state(chat.id, qid)
  assert status == "completed" and marker is None and len(pending) == 1
  assert pending[0]["hidden"] is True
  assert messages[-1]["blocks"][-1]["answers"]["Status"] == "completed"


def test_submit_runs_once_and_only_fixed_result_reaches_transcript(client, chat, auth, sealed_run, tmp_path, monkeypatch):
  qid = _create(client, chat, sealed_run, tmp_path)
  seen = []
  async def consume(spec, values, chat_id):
    seen.append(dict(values))
    values.clear()
    return 0
  monkeypatch.setattr(saved_secure_inputs, "_run_consumer", consume)
  response = client.post(f"/api/secure-inputs/{chat.id}/{qid}/submit", headers=auth, json={"fields": {"api_key": "never-record-this-value"}})
  assert response.status_code == 200, response.text
  # TestClient processes an immediate consumer task before returning its loop.
  for _ in range(2):
    response = client.post(f"/api/secure-inputs/{chat.id}/{qid}/submit", headers=auth, json={"fields": {"api_key": "never-record-this-value"}})
    assert response.status_code == 200
  assert len(seen) == 1
  state = _state(chat.id, qid)
  assert "never-record-this-value" not in json.dumps(state)


def test_generic_question_answer_cannot_bypass_sealed_execution(client, chat, auth, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  response = client.post(f"/api/chats/{chat.id}/messages", headers=auth,
    json={"content": "pretend done", "question_id": qid, "answers": {"Connect service": "done"}})
  assert response.status_code == 409
  assert _state(chat.id, qid)[:2] == ("pending", qid)


def test_restart_marks_claim_unknown_without_reexecuting(client, chat, sealed_run, tmp_path, monkeypatch):
  qid = _create(client, chat, sealed_run, tmp_path)
  get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5)
  async def forbidden(*args):
    pytest.fail("crash recovery must never execute the consumer")
  monkeypatch.setattr(saved_secure_inputs, "_run_consumer", forbidden)
  asyncio.run(saved_secure_inputs.recover_interrupted())
  status, marker, messages, pending = _state(chat.id, qid)
  assert status == "interrupted" and marker is None and len(pending) == 1
  assert "unknown" in pending[0]["content"]
  assert "Do not repeat" in pending[0]["content"]


def test_stop_wins_before_submit_admission_and_values_are_discarded(client, chat, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  generation = chat_mod.current_run_generation(chat.id)
  chat_mod.bump_run_generation(chat.id)
  values = {"api_key": "discard-me"}
  result = asyncio.run(saved_secure_inputs.submit(chat.id, qid, values, generation))
  assert result["status"] == "cancelled"
  assert values == {}


def test_consumer_receives_only_stdin_and_no_agent_environment(tmp_path, monkeypatch):
  monkeypatch.setenv("AGENT_TOKEN", "must-not-inherit")
  command = [sys.executable, "-c", "import os,sys,json; assert 'AGENT_TOKEN' not in os.environ; assert json.load(sys.stdin)['api_key']=='stdin-only'; print('stdin-only')"]
  values = {"api_key": "stdin-only"}
  result = asyncio.run(saved_secure_inputs._run_consumer({"command": command, "cwd": str(tmp_path)}, values, "chat"))
  assert result == 0 and values == {}


def test_cancellation_does_not_bypass_an_already_claimed_consumer(client, chat, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5)
  result = get_writer().submit(SettleSecureInput(chat_id=chat.id, request_id=qid, status="cancelled", outcome="cancelled")).result(timeout=5)
  assert result == {"status": "consuming"}
  assert _state(chat.id, qid)[:2] == ("consuming", qid)


def test_stop_after_claim_prevents_outcome_from_resuming_chat(client, chat, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5)
  get_writer().submit(FinishRun(chat_id=chat.id, terminal_status="stopped")).result(timeout=5)
  result = get_writer().submit(SettleSecureInput(chat_id=chat.id, request_id=qid, status="completed", outcome="success")).result(timeout=5)
  assert result == {"status": "cancelled"}
  assert _state(chat.id, qid)[3] == []


def test_nonzero_and_timeout_outcomes_never_include_consumer_output(tmp_path, monkeypatch):
  command = [sys.executable, "-c", "import sys; print('secret-on-stdout'); print('secret-on-stderr', file=sys.stderr); raise SystemExit(3)"]
  values = {"api_key": "stdin-only"}
  code = asyncio.run(saved_secure_inputs._run_consumer({"command": command, "cwd": str(tmp_path)}, values, "chat"))
  assert code == 3 and values == {}
  assert saved_secure_inputs.consumer_outcome("run", code) == (False, 3, saved_secure_inputs.SAFE_OUTCOMES["failed"])
  monkeypatch.setattr(saved_secure_inputs, "CONSUMER_TIMEOUT_SECONDS", 0.02)
  code = asyncio.run(saved_secure_inputs._run_consumer({"command": [sys.executable, "-c", "import time; time.sleep(60)"], "cwd": str(tmp_path)}, {"api_key": "stdin-only"}, "chat"))
  assert code == 124
  assert "do not repeat automatically" in saved_secure_inputs.consumer_outcome("run", code)[2]


def test_cancellation_during_spawn_still_kills_the_owned_process(tmp_path, monkeypatch):
  killed = []
  class Process:
    pid = 7654321
    async def wait(self):
      killed.append("waited")
  async def exercise():
    spawned = asyncio.Event()
    release = asyncio.Event()
    async def spawn(*args, **kwargs):
      spawned.set()
      await release.wait()
      return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(saved_secure_inputs.os, "killpg", lambda pid, sig: killed.append(pid))
    values = {"api_key": "never-persist"}
    task = asyncio.create_task(saved_secure_inputs._run_consumer({"command": ["consumer"], "cwd": str(tmp_path)}, values, "chat"))
    await spawned.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
      await task
    assert values == {}
  asyncio.run(exercise())
  assert killed == [7654321, "waited"]


def test_failed_settlement_stays_nonrepeatable_and_retry_queues_once(client, chat, sealed_run, tmp_path, monkeypatch):
  from app import chat_writer
  qid = _create(client, chat, sealed_run, tmp_path)
  get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5)
  original = chat_writer._commit_or_rollback
  def fail(db):
    db.rollback()
    return False
  with monkeypatch.context() as patch:
    patch.setattr(chat_writer, "_commit_or_rollback", fail)
    with pytest.raises(Exception, match="AppendPending did not persist"):
      get_writer().submit(SettleSecureInput(chat_id=chat.id, request_id=qid, status="completed", outcome="success")).result(timeout=5)
  assert _state(chat.id, qid)[:2] == ("consuming", qid)
  assert get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5) == {"status": "consuming"}
  get_writer().submit(SettleSecureInput(chat_id=chat.id, request_id=qid, status="completed", outcome="success")).result(timeout=5)
  assert len(_state(chat.id, qid)[3]) == 1


def test_failed_question_commit_does_not_leave_a_private_request(client, chat, sealed_run, tmp_path, monkeypatch):
  from app import chat_writer
  def fail(*args, **kwargs):
    raise RuntimeError("test-save-failure")
  monkeypatch.setattr(get_writer(), "_persist_question_required", fail)
  response = client.post(f"/api/secure-inputs/{chat.id}/saved", headers=sealed_run[1], json=_spec(tmp_path))
  assert response.status_code == 503
  with SessionLocal() as db:
    assert db.query(models.SavedSecureInput).filter_by(chat_id=chat.id).count() == 0
  assert "test-save-failure" not in response.text


def test_pending_secure_card_blocks_plain_send(client, chat, auth, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  response = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={"content": "go ahead anyway"})
  assert response.status_code == 409
  assert _state(chat.id, qid)[:2] == ("pending", qid)


def test_safe_state_never_returns_private_command(client, chat, auth, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  response = client.get(f"/api/secure-inputs/{chat.id}/{qid}/saved-state", headers=auth)
  assert response.status_code == 200
  assert response.json() == {"status": "pending", "outcome": None}


def test_legacy_question_answer_cannot_bypass_consumer(client, chat, auth, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  for identity in ({"question_id": qid}, {}):
    response = client.post(f"/api/chats/{chat.id}/question-answers", headers=auth,
      json={**identity, "answers": {"Connect service": "done"}})
    assert response.status_code == 409
  assert _state(chat.id, qid)[:2] == ("pending", qid)


def test_legacy_live_input_cannot_overlap_saved_owner_question(client, chat, auth, sealed_run, tmp_path):
  qid = _create(client, chat, sealed_run, tmp_path)
  response = client.post(f"/api/secure-inputs/{chat.id}", headers=auth, json={
    "title": "Reveal for debugging", "mode": "reveal",
    "fields": [{"name": "key", "type": "password", "label": "Key"}],
  })
  assert response.status_code == 409
  assert _state(chat.id, qid)[:2] == ("pending", qid)


def test_saved_request_table_is_added_to_an_existing_database_without_values(tmp_path):
  from sqlalchemy import create_engine, inspect, text
  from app.database import Base
  engine = create_engine(f"sqlite:///{tmp_path}/existing.db")
  with engine.begin() as connection:
    connection.execute(text("CREATE TABLE owner_existing_fixture (id INTEGER PRIMARY KEY)"))
    connection.execute(text("INSERT INTO owner_existing_fixture VALUES (42)"))
  Base.metadata.create_all(engine)
  columns = {column["name"] for column in inspect(engine).get_columns("saved_secure_inputs")}
  assert columns == {"request_id", "chat_id", "command_json", "cwd", "action", "status", "outcome", "created_at"}
  with engine.connect() as connection:
    assert connection.execute(text("SELECT id FROM owner_existing_fixture")).scalar() == 42
  Base.metadata.create_all(engine)  # Repeat boots are no-op table migrations.
  engine.dispose()


def test_consumer_exception_is_replaced_with_fixed_safe_outcome(client, chat, sealed_run, tmp_path, monkeypatch, caplog):
  qid = _create(client, chat, sealed_run, tmp_path)
  get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5)
  async def fail(*args):
    raise RuntimeError("secret-exception-content")
  monkeypatch.setattr(saved_secure_inputs, "_run_consumer", fail)
  values = {"api_key": "secret-exception-content"}
  asyncio.run(saved_secure_inputs._consume(chat.id, qid, {"action": "run"}, values, chat_mod.current_run_generation(chat.id)))
  assert values == {}
  status, marker, messages, pending = _state(chat.id, qid)
  assert status == "failed" and marker is None
  assert "secret-exception-content" not in json.dumps((messages, pending))
  assert "secret-exception-content" not in caplog.text


def test_owner_credentials_policy_never_trusts_an_arbitrary_command(tmp_path):
  spec = saved_secure_inputs.validate_consumer_spec({**_spec(tmp_path), "action": "owner-credentials", "command": ["untrusted-command"]})
  assert spec["command"][1].endswith("scripts/update-owner-credentials.py")
  assert saved_secure_inputs.consumer_outcome("owner-credentials", 5) == saved_secure_inputs.OWNER_CREDENTIAL_OUTCOMES[5]


def test_failed_outcome_recovers_on_supervised_pass_without_reexecution(client, chat, sealed_run, tmp_path, monkeypatch):
  from app import chat_writer
  qid = _create(client, chat, sealed_run, tmp_path)
  get_writer().submit(ClaimSecureInput(chat_id=chat.id, request_id=qid)).result(timeout=5)
  called = []
  async def consume(*args):
    called.append("executed")
    return 0
  monkeypatch.setattr(saved_secure_inputs, "_run_consumer", consume)
  def fail(db):
    db.rollback()
    return False
  with monkeypatch.context() as patch:
    patch.setattr(chat_writer, "_commit_or_rollback", fail)
    asyncio.run(saved_secure_inputs._consume(chat.id, qid, {"action": "run"}, {"api_key": "private"}, chat_mod.current_run_generation(chat.id)))
  assert _state(chat.id, qid)[:2] == ("consuming", qid)
  asyncio.run(saved_secure_inputs.recover_interrupted())
  asyncio.run(saved_secure_inputs.recover_interrupted())
  status, marker, _, pending = _state(chat.id, qid)
  assert status == "interrupted" and marker is None and len(pending) == 1
  assert called == ["executed"]


def test_supervised_recovery_does_not_interrupt_an_owned_consumer(client, chat, sealed_run, tmp_path, monkeypatch):
  qid = _create(client, chat, sealed_run, tmp_path)
  async def exercise():
    running = asyncio.Event()
    release = asyncio.Event()
    async def consume(*args):
      running.set()
      await release.wait()
      return 0
    monkeypatch.setattr(saved_secure_inputs, "_run_consumer", consume)
    values = {"api_key": "private"}
    result = await saved_secure_inputs.submit(chat.id, qid, values, chat_mod.current_run_generation(chat.id))
    assert result == {"status": "consuming"}
    await running.wait()
    await saved_secure_inputs.recover_interrupted()
    assert _state(chat.id, qid)[:2] == ("consuming", qid)
    task = saved_secure_inputs._tasks[qid][1]
    release.set()
    await task
  asyncio.run(exercise())
  assert _state(chat.id, qid)[0] == "completed"


def test_timeout_kills_consumer_descendants_not_only_parent(tmp_path, monkeypatch):
  import os
  pid_file = tmp_path / "child.pid"
  program = (
    "import json,sys,subprocess,time,pathlib; json.load(sys.stdin); "
    "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
    "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
  )
  monkeypatch.setattr(saved_secure_inputs, "CONSUMER_TIMEOUT_SECONDS", 0.2)
  values = {"api_key": "stdin-only"}
  code = asyncio.run(saved_secure_inputs._run_consumer({
    "command": [sys.executable, "-c", program, str(pid_file)], "cwd": str(tmp_path),
  }, values, "chat"))
  assert code == 124 and values == {}
  child_pid = int(pid_file.read_text())
  try:
    from pathlib import Path
    state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
  except FileNotFoundError:
    state = "gone"
  assert state in {"Z", "gone"}, "consumer descendant survived timeout"


def test_recovery_query_failure_does_not_abort_platform_startup(monkeypatch, caplog):
  def unavailable():
    raise RuntimeError("untrusted-db-detail")
  monkeypatch.setattr(saved_secure_inputs, "SessionLocal", unavailable)
  asyncio.run(saved_secure_inputs.recover_interrupted())
  assert "recovery remains pending" in caplog.text
  assert "untrusted-db-detail" not in caplog.text


def test_sealed_consumer_discards_raw_and_encoded_output(tmp_path, capfd):
  program = (
    "import base64,json,sys,urllib.parse; "
    "value=json.load(sys.stdin)['api_key']; "
    "print(value); print(urllib.parse.quote(value,safe='')); "
    "print(base64.b64encode(value.encode()).decode()); "
    "print(value,file=sys.stderr)"
  )
  values = {"api_key": "output-canary+/with spaces"}
  assert saved_secure_inputs.CONSUMER_TIMEOUT_SECONDS == 120
  code = asyncio.run(saved_secure_inputs._run_consumer({
    "command": [sys.executable, "-c", program], "cwd": str(tmp_path),
  }, values, "chat"))
  assert code == 0 and values == {}
  captured = capfd.readouterr()
  assert captured.out == "" and captured.err == ""
