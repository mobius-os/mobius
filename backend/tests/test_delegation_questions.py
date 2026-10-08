"""Correlated helper questions: one per physical run, answered exactly once.

A helper asks its parent with ``ask_parent`` and ends its turn. The settled
run projects ``needs_input`` (neither working nor finished), wakes the parent
once through the ordinary per-run result latch, and the parent's answer
resumes the asking root through one reserved continuation run.
"""

import asyncio
import json
from datetime import timedelta
import hashlib

import pytest

from app.chat_writer import create_chat
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect

import app.chat as chat_mod
import app.chat_start as chat_start_mod
from app import transcript_rows, auth, delegations, goal_plans, models, providers
from app.broadcast import remove_broadcast
from app.chat_writer import StartContinuation, get_writer
from app.database import SessionLocal
from app.timeutil import now_naive_utc
from tests.goal_fixtures import goal_run


PROMPT = "Migrate the settings store."


@pytest.fixture(autouse=True)
def _owner(owner_token):
  """Every bearer here is minted for the one owner account."""
  return owner_token


def _seed(
  db, suffix="q", *, provider="claude", app_id=None, parent_id=None,
  parent_root=None, parent_status="completed",
):
  """A parent chat, one helper, and the helper's running asking run."""
  model = providers.DEFAULT_MODELS[provider]
  if parent_id is None:
    parent_id = f"parent-{suffix}"
    parent_root = parent_root or f"parent-root-{suffix}"
    db.add(create_chat(
      id=parent_id, title="Parent", provider=provider,
      messages=[{"role": "user", "content": "Owner work."}],
      agent_settings_json={"model": model},
    ))
    db.flush()
    db.add(models.ChatRun(
      id=parent_root, root_run_id=parent_root, chat_id=parent_id,
      status=parent_status, provider=provider,
      started_at=now_naive_utc() - timedelta(minutes=5),
    ))
  child_id = f"child-{suffix}"
  db.add(create_chat(
    id=child_id, title="Delegation", provider=provider,
    session_id=f"session-{suffix}",
    messages=[{"role": "user", "content": PROMPT}],
    agent_settings_json={"model": model, "drawer_hidden": True, "owner_visible": False},
    created_by_app_id=app_id, auto_resume_on_restart=True,
  ))
  db.flush()
  row = models.Delegation(
    id=f"helper-{suffix}", app_id=app_id, parent_chat_id=parent_id,
    parent_root_run_id=parent_root, task_key=f"task-{suffix}",
    child_chat_id=child_id, provider=provider, model=model, scope="write",
    cwd="/data", prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
    notify_parent_on_complete=True,
  )
  db.add(row)
  db.add(models.ChatRun(
    id=f"ask-{suffix}", root_run_id=f"ask-{suffix}", chat_id=child_id,
    status="running", provider=provider, initiated_by_app_id=app_id,
    started_at=now_naive_utc() - timedelta(minutes=1),
  ))
  db.commit()
  return row


def _bearer(token):
  return {"Authorization": f"Bearer {token}"}


def _helper_token(db, row, run_id):
  owner = db.query(models.Owner).first()
  return auth.create_agent_token(
    row.child_chat_id, owner.username, owner.token_epoch, run_id=run_id,
    delegation_id=row.id, delegation_chat=row.child_chat_id,
  )


def _ask(client, db, row, run_id, question="Which database?", options=None):
  return client.post(
    f"/api/delegations/{row.id}/questions",
    json={"question": question, "options": options or []},
    headers=_bearer(_helper_token(db, row, run_id)),
  )


def _settle(db, chat_id, run_id, status="completed", text="I need a decision."):
  """End a child run the way its runner would, with its final report."""
  chat_mod.discard_starting(chat_id)
  remove_broadcast(chat_id)
  db.expire_all()
  run = db.get(models.ChatRun, run_id)
  run.status = status
  run.ended_at = now_naive_utc()
  chat = db.get(models.Chat, chat_id)
  transcript_rows.replace_all(db, chat, [*transcript_rows.history(chat), {
    "id": run_id, "role": "assistant",
    "blocks": [{"type": "text", "content": text}],
  }])
  chat.live_assistant = None
  db.commit()


def _asked(client, owner_token, db, suffix="q", **seed):
  row = _seed(db, suffix, **seed)
  response = _ask(client, db, row, f"ask-{suffix}", options=["sqlite", "postgres"])
  assert response.status_code == 200, response.text
  _settle(db, row.child_chat_id, f"ask-{suffix}")
  return row, response.json()["question_id"]


@pytest.fixture
def schedules(monkeypatch):
  """Capture continuation task creation instead of running a provider."""
  calls = []

  def schedule(**kwargs):
    calls.append(kwargs)
    return True

  monkeypatch.setattr(chat_mod, "_schedule_continuation", schedule)
  return calls


def _answer(client, owner_token, row, message, question_id=None):
  body = {"message": message}
  if question_id is not None:
    body["question_id"] = question_id
  return client.post(
    f"/api/delegations/{row.id}/messages", json=body, headers=_bearer(owner_token),
  )


def _status(db, row):
  db.expire_all()
  return delegations.derived_status(db, db.get(models.Delegation, row.id), load_result=False)[0]


# --- Asking ------------------------------------------------------------------


def test_only_the_helpers_own_running_turn_may_ask(client, owner_token, db):
  row = _seed(db, "auth")
  other = _seed(db, "other", parent_id=row.parent_chat_id, parent_root=row.parent_root_run_id)
  owner = db.query(models.Owner).first()
  path = f"/api/delegations/{row.id}/questions"
  body = {"question": "May I drop the table?"}

  assert client.post(path, json=body, headers=_bearer(owner_token)).status_code == 403
  # A sibling's bearer, or a bearer without the run claim, is not this helper.
  assert client.post(
    path, json=body, headers=_bearer(_helper_token(db, other, "ask-other")),
  ).status_code == 403
  route_bearer = auth.create_delegation_token(
    row.id, None, row.child_chat_id, owner.username, owner.token_epoch,
  )
  assert client.post(path, json=body, headers=_bearer(route_bearer)).status_code == 403
  # The parent's run-bound bearer cannot ask on its child's behalf.
  db.add(models.ChatRun(
    id="parent-live-auth", root_run_id="parent-live-auth", chat_id=row.parent_chat_id,
    status="running", provider="claude", started_at=now_naive_utc(),
  ))
  db.commit()
  parent_bearer = auth.create_agent_token(
    row.parent_chat_id, owner.username, owner.token_epoch, run_id="parent-live-auth",
  )
  assert client.post(path, json=body, headers=_bearer(parent_bearer)).status_code == 403
  assert db.query(models.DelegationQuestion).count() == 0


def test_source_attached_work_cannot_ask(client, db):
  row = _seed(db, "source")
  row.source_work_id = "source-work-1"
  db.commit()
  response = _ask(client, db, row, "ask-source")
  assert response.status_code == 403
  assert response.json()["detail"]["code"] == "source_work"


def test_a_run_asks_at_most_once_and_an_exact_retry_returns_the_same_receipt(client, db):
  row = _seed(db, "once")
  first = _ask(client, db, row, "ask-once", options=["a", "b"])
  again = _ask(client, db, row, "ask-once", options=["a", "b"])
  assert first.status_code == again.status_code == 200
  assert first.json() == again.json()
  assert "End your turn now" in first.json()["note"]

  changed = _ask(client, db, row, "ask-once", question="Something else?")
  assert changed.status_code == 409
  assert changed.json()["detail"]["code"] == "question_conflict"
  reordered = _ask(client, db, row, "ask-once", options=["b", "a"])
  assert reordered.status_code == 409
  assert db.query(models.DelegationQuestion).count() == 1
  question = db.query(models.DelegationQuestion).one()
  assert question.asking_run_id == question.root_run_id == "ask-once"
  assert question.answer_run_id == delegations.helper_answer_run_id(
    row.child_chat_id, question.id,
  )


def test_ask_parent_preserves_full_length_question_and_choices(client, db):
  row = _seed(db, "bounds")
  question_text = "Long question " * 1000 + " END"
  options = [("Long option " * 1000).strip(), "Another option"]
  response = _ask(client, db, row, "ask-bounds", question=question_text, options=options)
  assert response.status_code == 200, response.text
  question = db.get(models.DelegationQuestion, response.json()["question_id"])
  assert question.question == question_text
  assert question.options_json == options
  assert _ask(client, db, row, "ask-bounds", options=[" "]).status_code == 422


def test_automatic_question_previews_are_bounded_with_exact_saved_identity(
  client, owner_token, db, schedules,
):
  row = _seed(db, "preview")
  question_text = "Question " * 12000
  options = ["Option " * 10000 for _ in range(16)]
  asked = _ask(
    client, db, row, "ask-preview", question=question_text, options=options,
  )
  assert asked.status_code == 200, asked.text
  question_id = asked.json()["question_id"]
  _settle(db, row.child_chat_id, "ask-preview")

  full = delegations.serialize_delegation(db, row)["question"]
  assert full == {
    "id": question_id, "question": question_text.strip(),
    "options": [option.strip() for option in options],
  }
  assert delegations.own_helper_statuses(
    db, row.parent_chat_id, row.parent_root_run_id,
  )[0]["question"] == full
  hint = delegations.active_parent_context(
    db, row.parent_chat_id, row.parent_root_run_id,
  )
  preview = json.loads(hint.rsplit("<active_delegations>", 1)[1].split(
    "</active_delegations>", 1,
  )[0])[0]["question"]
  assert preview["id"] == question_id
  assert preview["preview_truncated"] is True
  assert preview["full_question_url"] == f"/api/delegations/{row.id}"
  assert len(preview["question"]) <= 1000
  assert len(preview["options"]) == 16
  assert all(len(option) <= 120 for option in preview["options"])
  assert len(hint) < 5000

  notice = delegations._compose_wake_notice(db, [row], {row.id: "ask-preview"})
  item = json.loads(notice.rsplit("<delegation_results>", 1)[1].split(
    "</delegation_results>", 1,
  )[0])[0]
  assert item["question"] == preview
  assert len(notice) < 5000
  assert _answer(client, owner_token, row, "Use the first option.", question_id).status_code == 202



def test_a_recorded_question_ends_only_its_own_live_run(client, db):
  """A question is a turn-ending result, like a saved owner card: the
  receipt is recorded on the asking run's live sink so its end hook stops the
  turn without another model request. Another run's sink never gets one."""
  from app.broadcast import ChatBroadcast
  from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink

  row = _seed(db, "receipt")
  sink = ChatEventSink(ChatBroadcast(row.child_chat_id), row.child_chat_id,
                       run_token="ask-receipt")
  register_active_sink(row.child_chat_id, sink)
  try:
    asked = _ask(client, db, row, "ask-receipt")
    assert asked.status_code == 200
    receipt_id = asked.json()["turn_end_id"]
    assert sink.ends_turn(receipt_id) and not sink.ends_turn("someone-else")
    # An exact retry returns the same question; it may name a fresh receipt
    # for the same live run, and never a different question.
    again = _ask(client, db, row, "ask-receipt")
    assert again.json()["question_id"] == asked.json()["question_id"]
  finally:
    unregister_active_sink(row.child_chat_id, sink)

  other = _seed(db, "receipt-other")
  foreign = ChatEventSink(ChatBroadcast(other.child_chat_id), other.child_chat_id,
                          run_token="a-different-run")
  register_active_sink(other.child_chat_id, foreign)
  try:
    asked = _ask(client, db, other, "ask-receipt-other")
    assert asked.status_code == 200
    assert "turn_end_id" not in asked.json()
  finally:
    unregister_active_sink(other.child_chat_id, foreign)


# --- Status ------------------------------------------------------------------


def test_needs_input_is_shown_only_after_a_clean_settle_of_the_asking_root(client, db):
  row = _seed(db, "status")
  assert _ask(client, db, row, "ask-status").status_code == 200
  # Asking does not end the turn by itself: the run still works.
  assert _status(db, row) == "running"
  _settle(db, row.child_chat_id, "ask-status")
  assert _status(db, row) == "needs_input"

  payload = delegations.serialize_delegation(db, db.get(models.Delegation, row.id))
  assert payload["status"] == "needs_input"
  assert payload["question"]["question"] == "Which database?"
  rows = db.query(models.Delegation).all()
  assert delegations.delegation_statuses(db, rows) == {row.id: "needs_input"}
  assert "needs_input" not in delegations.TERMINAL_DELEGATION_STATUSES
  assert "needs_input" not in delegations.ACTIVE_DELEGATION_STATUSES

  # A fresh follow-up root (not the answer) closes the question.
  db.add(models.ChatRun(
    id="fresh-status", root_run_id="fresh-status", chat_id=row.child_chat_id,
    status="completed", provider="claude", started_at=now_naive_utc(),
  ))
  db.commit()
  assert _status(db, row) == "completed"
  assert delegations.serialize_delegation(db, db.get(models.Delegation, row.id))["question"] is None


@pytest.mark.parametrize("terminal", ["failed", "stopped"])
def test_a_failed_or_stopped_asking_run_reports_how_it_ended_not_a_question(
  client, owner_token, db, terminal, schedules,
):
  row = _seed(db, terminal)
  response = _ask(client, db, row, f"ask-{terminal}")
  _settle(db, row.child_chat_id, f"ask-{terminal}", status=terminal)
  assert _status(db, row) == terminal
  assert delegations.delegation_statuses(db, [db.get(models.Delegation, row.id)]) == {
    row.id: terminal,
  }
  answer = _answer(client, owner_token, row, "sqlite", response.json()["question_id"])
  assert answer.status_code == 409
  assert answer.json()["detail"]["code"] == "question_not_open"
  assert schedules == []


def test_a_restart_continuation_of_the_asking_root_keeps_the_question_open(client, db):
  row = _seed(db, "restart")
  _ask(client, db, row, "ask-restart")
  _settle(db, row.child_chat_id, "ask-restart")
  db.add(models.ChatRun(
    id="restart-cont", root_run_id="ask-restart", chat_id=row.child_chat_id,
    status="completed", provider="claude", started_at=now_naive_utc(),
    continuation_json={"reason": "restart", "supersedes_run_token": "ask-restart"},
  ))
  db.commit()
  assert _status(db, row) == "needs_input"


# --- Parent delivery -----------------------------------------------------------


def _capture_activity_starts(monkeypatch):
  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr(
    chat_start_mod, "start_programmatic_activity_continuation", fake_start,
  )
  return starts


def test_a_question_wakes_an_idle_parent_once_with_the_question(
  client, owner_token, db, monkeypatch,
):
  row, question_id = _asked(client, owner_token, db, "wake")
  starts = _capture_activity_starts(monkeypatch)
  # Waiting on the helper until the question reaches the parent.
  assert row.parent_chat_id in delegations.background_helper_chat_ids(db, {row.parent_chat_id})

  asyncio.run(delegations.wake_parent_after_child_settled(row.child_chat_id))
  assert [start["activity_id"] for start in starts] == [row.id]

  delivery = delegations.build_delegation_result_context(
    db, row.parent_chat_id, source_work_id=row.parent_root_run_id,
  )
  assert delivery.results == ((row.id, "ask-wake"),)
  assert f'"question":{{"id":"{question_id}"' in delivery.text
  assert "paused on its question, not finished" in delivery.text
  assert "message_agent(helper, message, question_id)" in delivery.text

  # The asking run is the result identity: once delivered, no sweep re-wakes.
  assert delegations.mark_results_delivered(db, dict(delivery.results))
  db.commit()
  asyncio.run(delegations.wake_parents_for_completed_delegations())
  asyncio.run(delegations.wake_parent_after_child_settled(row.child_chat_id))
  assert len(starts) == 1
  # The parent now owns the next move; the drawer no longer waits on the helper.
  assert row.parent_chat_id not in delegations.background_helper_chat_ids(
    db, {row.parent_chat_id},
  )
  # Its current state stays visible to the parent's later turns.
  db.add(models.ChatRun(
    id="parent-later", root_run_id=row.parent_root_run_id, chat_id=row.parent_chat_id,
    status="running", provider="claude", started_at=now_naive_utc(),
  ))
  db.commit()
  context = delegations.active_parent_context(db, row.parent_chat_id, "parent-later")
  assert f'"status":"needs_input","question":{{"id":"{question_id}"' in context
  assert "message_agent(helper, message, question_id)" in context


def test_owner_input_and_owner_stop_keep_the_question_from_waking_the_parent(
  client, owner_token, db, schedules,
):
  row, _question_id = _asked(client, owner_token, db, "held")
  parent = db.get(models.Chat, row.parent_chat_id)
  parent.pending_question_id = "owner-card"
  db.commit()
  assert asyncio.run(delegations._deliver_parent_wake_once(
    row.parent_chat_id, row.parent_root_run_id,
  )) is False
  assert schedules == []

  # After an owner Stop of the source work the question stays quiet too.
  parent.pending_question_id = None
  db.get(models.ChatRun, row.parent_root_run_id).status = "stopped"
  db.commit()
  assert delegations.parent_wake_blocker(
    db, row.parent_chat_id, row.parent_root_run_id, row.parent_root_run_id,
  )[0] is not None
  assert asyncio.run(delegations._deliver_parent_wake_once(
    row.parent_chat_id, row.parent_root_run_id,
  )) is False
  assert schedules == []
  assert _status(db, row) == "needs_input"


# --- Answering -----------------------------------------------------------------


def test_the_answer_resumes_the_asking_root_through_its_one_reserved_run(
  client, owner_token, db, schedules,
):
  row, question_id = _asked(client, owner_token, db, "answer")
  question = db.get(models.DelegationQuestion, question_id)

  response = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert response.status_code == 202, response.text
  assert response.json()["already_answered"] is False
  assert response.json()["status"] == "running"
  assert [call["run_token"] for call in schedules] == [question.answer_run_id]
  next_user = schedules[0]["next_user"]
  assert next_user["content"].endswith("\n\nUse sqlite.")
  assert question_id in next_user["content"]

  db.expire_all()
  run = db.get(models.ChatRun, question.answer_run_id)
  assert (run.root_run_id, run.initiated_by_app_id, run.owner_input_at) == (
    "ask-answer", None, None,
  )
  assert run.continuation_json is None
  stored = transcript_rows.history(db.get(models.Chat, row.child_chat_id))[-1]
  assert stored["cid"] == delegations.helper_answer_continuation_id(question_id)
  assert (stored["kind"], stored["continuation_reason"]) == ("continuation", "helper_answer")
  assert not stored.get("hidden")

  # The answer run's own report is the next result: it is not a write repair.
  _settle(db, row.child_chat_id, question.answer_run_id, text="Migrated to sqlite.")
  status, latest, result = delegations.derived_status(db, db.get(models.Delegation, row.id))
  assert (status, latest.id, result) == ("completed", question.answer_run_id, "Migrated to sqlite.")


def test_sequential_duplicate_answers_attach_once_and_a_different_one_is_refused(
  client, owner_token, db, schedules,
):
  row, question_id = _asked(client, owner_token, db, "dup")
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 202
  again = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert again.status_code == 202
  assert again.json()["already_answered"] is True
  different = _answer(client, owner_token, row, "Use postgres.", question_id)
  assert different.status_code == 409
  assert different.json()["detail"]["code"] == "question_already_answered"
  assert len(schedules) == 1

  # After the answer run settles, a retry still never opens a second turn.
  question = db.get(models.DelegationQuestion, question_id)
  _settle(db, row.child_chat_id, question.answer_run_id, text="Done.")
  late = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert late.status_code == 202 and late.json()["already_answered"] is True
  assert len(schedules) == 1
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == row.child_chat_id,
  ).count() == 2


@pytest.mark.parametrize("second_answer", ["Use sqlite.", "Use postgres."])
def test_concurrent_duplicate_answers_start_one_run(
  client, owner_token, db, schedules, second_answer,
):
  from app.deps import Principal
  from app.routes.delegations import DelegationMessage, message_delegation

  row, question_id = _asked(client, owner_token, db, "race")
  owner = db.query(models.Owner).first()
  principal = Principal(owner=owner, app_id=None, scope="owner")

  async def answer(text):
    with SessionLocal() as session:
      try:
        return await message_delegation(
          row.id, DelegationMessage(message=text, question_id=question_id),
          principal=principal, db=session,
        )
      except HTTPException as exc:
        return exc

  async def both():
    return await asyncio.wait_for(asyncio.gather(
      answer("Use sqlite."), answer(second_answer),
    ), timeout=30)

  outcomes = asyncio.run(both())
  accepted = [item for item in outcomes if isinstance(item, dict)]
  refused = [item for item in outcomes if isinstance(item, HTTPException)]
  assert len(schedules) == 1
  assert [item["already_answered"] for item in accepted].count(False) == 1
  if second_answer == "Use sqlite.":
    assert len(accepted) == 2 and not refused
  else:
    assert len(accepted) == 1
    assert refused[0].detail["code"] == "question_already_answered"


def test_a_waiting_helper_requires_the_exact_question_id(
  client, owner_token, db, schedules,
):
  row, question_id = _asked(client, owner_token, db, "needs-id")
  plain = _answer(client, owner_token, row, "Use sqlite.")
  assert plain.status_code == 409
  detail = plain.json()["detail"]
  assert detail["code"] == "question_id_required"
  assert detail["question"]["id"] == question_id
  assert question_id in detail["message"]
  assert _answer(client, owner_token, row, "x", "not-a-question").status_code == 404
  assert schedules == []


def test_ordinary_follow_ups_keep_their_fresh_turn_semantics(
  client, owner_token, db, schedules, monkeypatch,
):
  row, question_id = _asked(client, owner_token, db, "fresh")
  question = db.get(models.DelegationQuestion, question_id)
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 202
  busy = _answer(client, owner_token, row, "Also check tests.")
  assert busy.status_code == 409 and "still working" in busy.json()["detail"]
  _settle(db, row.child_chat_id, question.answer_run_id, text="Done.")

  turns = []

  async def fake_run_chat(*_args, **kwargs):
    turns.append(kwargs["run_token"])

  monkeypatch.setattr(chat_start_mod, "run_chat", fake_run_chat)
  follow_up = _answer(client, owner_token, row, "Also check tests.")
  assert follow_up.status_code == 202, follow_up.text
  db.expire_all()
  fresh = db.get(models.ChatRun, turns[0])
  assert fresh.root_run_id == fresh.id  # a new logical root, as before
  assert _status(db, row) == "running"
  closed = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert closed.json()["already_answered"] is True
  assert len(schedules) == 1


def test_answer_admission_honors_a_draining_server(client, owner_token, db, schedules):
  row, question_id = _asked(client, owner_token, db, "drain")
  chat_mod.draining = True
  try:
    response = _answer(client, owner_token, row, "Use sqlite.", question_id)
  finally:
    chat_mod.draining = False
  assert response.status_code == 409
  assert schedules == []
  question = db.get(models.DelegationQuestion, question_id)
  assert db.get(models.ChatRun, question.answer_run_id) is None
  assert _status(db, row) == "needs_input"


def test_a_helper_answer_is_never_owner_input(client, owner_token, db, schedules):
  row, question_id = _asked(client, owner_token, db, "authority")
  goal = models.ChatGoal(
    id="child-goal", chat_id=row.child_chat_id, objective="Held child work",
    status="stopped",
  )
  db.add(goal)
  db.get(models.ChatRun, "ask-authority").goal_id = "child-goal"
  db.commit()
  revision = goal.revision

  assert _answer(client, owner_token, row, "continue", question_id).status_code == 202
  db.expire_all()
  question = db.get(models.DelegationQuestion, question_id)
  run = db.get(models.ChatRun, question.answer_run_id)
  goal = db.get(models.ChatGoal, "child-goal")
  # admit_goal reopens a stopped Goal only for owner input; this is not that.
  assert (goal.status, goal.revision) == ("stopped", revision)
  assert run.goal_id is None and run.owner_input_at is None


@pytest.mark.parametrize("provider,app_owned", [("claude", False), ("codex", True)])
def test_the_answer_run_keeps_the_helpers_delegated_authority_and_session(
  client, owner_token, db, schedules, monkeypatch, provider, app_owned,
):
  from app import helper_hosts
  from app.deps import get_delegation_principal

  app_id = None
  if app_owned:
    app = models.App(
      slug="question-owner", source_dir="/tmp/mobius-tests/question-owner",
      name="Subagents", description="", jsx_source="",
    )
    db.add(app)
    db.commit()
    app_id = app.id
  row, question_id = _asked(
    client, owner_token, db, f"host-{provider}", provider=provider, app_id=app_id,
  )
  assert _answer(client, owner_token, row, "Proceed.", question_id).status_code == 202
  question = db.get(models.DelegationQuestion, question_id)
  [scheduled] = schedules
  assert (scheduled["session_id"], scheduled["provider_id"]) == (
    f"session-host-{provider}", provider,
  )

  db.expire_all()
  run = db.get(models.ChatRun, question.answer_run_id)
  assert run.initiated_by_app_id == app_id
  assert run.browser_grant_id is None
  policy = delegations.policy_for_chat(db, row.child_chat_id)
  assert policy is not None and policy.delegation_id == row.id
  token = delegations.delegation_execution_token(db, policy, run_id=run.id)
  principal = get_delegation_principal(token, db)
  assert (principal.delegation_id, principal.chat_id, principal.run_id) == (
    row.id, row.child_chat_id, run.id,
  )
  monkeypatch.setattr(helper_hosts, "hosts_enabled", lambda: True)
  key = chat_mod._helper_host_key(db, policy, provider_id=provider, connector_plan=None)
  assert key.parent_chat_id == row.parent_chat_id and key.provider_id == provider


# --- Recovery --------------------------------------------------------------------


def _commit_answer_without_scheduling(db, row, question_id, answer="Use sqlite."):
  question = db.get(models.DelegationQuestion, question_id)
  content = delegations.helper_answer_content(question, answer)
  get_writer().submit(StartContinuation(
    chat_id=row.child_chat_id, run_token=question.answer_run_id,
    root_run_id=question.root_run_id, content=content,
    cid=delegations.helper_answer_continuation_id(question.id),
    reason="helper_answer", initiated_by_app_id=row.app_id,
    message_kind="continuation", hidden=False,
  )).result(timeout=5)
  return question


def test_a_crash_between_answer_commit_and_scheduling_resumes_exactly_once(
  client, owner_token, db, schedules,
):
  row, question_id = _asked(client, owner_token, db, "crash")
  question = _commit_answer_without_scheduling(db, row, question_id)

  recovered = chat_mod.reconcile_startup_chats(db)
  assert row.child_chat_id not in recovered.manual
  db.expire_all()
  assert db.get(models.ChatRun, question.answer_run_id).status == "running"

  assert asyncio.run(delegations.reconcile_unstarted_delegations()) == 1
  assert [call["run_token"] for call in schedules] == [question.answer_run_id]
  # The runner now owns it; neither a sweep nor the parent's retry repeats it.
  assert asyncio.run(delegations.recover_unscheduled_helper_answers()) == 0
  retry = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert retry.json()["already_answered"] is True
  assert len(schedules) == 1
  messages = transcript_rows.history(db.get(models.Chat, row.child_chat_id))
  assert [m.get("cid") for m in messages].count(
    delegations.helper_answer_continuation_id(question_id),
  ) == 1


def test_a_failed_schedule_after_commit_is_still_an_accepted_answer(
  client, owner_token, db, monkeypatch,
):
  row, question_id = _asked(client, owner_token, db, "late-task")
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **_kwargs: False)
  response = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert response.status_code == 202, response.text
  chat_mod.discard_starting(row.child_chat_id)
  remove_broadcast(row.child_chat_id)
  calls = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kwargs: calls.append(kwargs) or True,
  )
  assert asyncio.run(delegations.recover_unscheduled_helper_answers()) == 1
  assert len(calls) == 1


def test_a_stopped_helper_never_revives_from_a_committed_answer(
  client, owner_token, db, schedules,
):
  row, question_id = _asked(client, owner_token, db, "crash-stop")
  question = _commit_answer_without_scheduling(db, row, question_id)
  assert asyncio.run(delegations.cancel_delegation_execution(row.id)) is True
  db.expire_all()
  assert db.get(models.ChatRun, question.answer_run_id).status == "stopped"
  chat_mod.reconcile_startup_chats(db)
  assert asyncio.run(delegations.recover_unscheduled_helper_answers()) == 0
  assert schedules == []
  assert _status(db, row) == "cancelled"


# --- Stop, delete, and Goal settlement ---------------------------------------------


def test_owner_stop_keeps_the_question_but_stop_agent_withdraws_it(
  client, owner_token, db, schedules,
):
  from app.routes.delegations import cancel_active_for_parent

  row, question_id = _asked(client, owner_token, db, "stop")
  with SessionLocal() as session:
    assert asyncio.run(cancel_active_for_parent(session, row.parent_chat_id)) == []
  assert _status(db, row) == "needs_input"

  stopped = client.post(
    f"/api/delegations/{row.id}/cancel", headers=_bearer(owner_token),
  )
  assert stopped.status_code == 200, stopped.text
  assert stopped.json()["status"] == "cancelled"
  assert stopped.json()["question"] is None
  late = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert late.status_code == 409
  assert schedules == []


def test_deleting_the_parent_chat_cancels_a_waiting_helper(client, owner_token, db):
  row, _question_id = _asked(client, owner_token, db, "delete")
  assert delegations.active_delegation_ids_for_chat(db, row.parent_chat_id) == [row.id]
  response = client.delete(
    f"/api/chats/{row.parent_chat_id}", headers=_bearer(owner_token),
  )
  assert response.status_code in (200, 204), response.text
  assert _status(db, row) == "cancelled"


def test_a_waiting_helper_blocks_goal_settlement_until_answered_or_stopped(
  client, owner_token, db,
):
  db.add(create_chat(id="goal-parent", title="Goal", provider="claude", messages=[]))
  db.flush()
  physical = goal_run(
    db, id="goal-root", root_run_id="goal-root", chat_id="goal-parent",
    status="completed", provider="claude", goal_objective="Ship it",
    started_at=now_naive_utc() - timedelta(minutes=5),
  )
  db.add(physical)
  db.commit()
  row = _seed(db, "goal", parent_id="goal-parent", parent_root="goal-root")
  _ask(client, db, row, "ask-goal")
  _settle(db, row.child_chat_id, "ask-goal")
  goal = db.get(models.ChatGoal, "goal-root")
  assert goal_plans.active_goal_helpers(db, physical, goal) == ["task-goal"]
  assert asyncio.run(delegations.cancel_delegation_execution(row.id)) is True
  db.expire_all()
  assert goal_plans.active_goal_helpers(db, physical, goal) == []


# --- Escalation ----------------------------------------------------------------------


def test_a_nested_question_escalates_and_answers_flow_back_down(
  client, owner_token, db, schedules, monkeypatch,
):
  # Top parent -> H1 -> H2. H2 asks H1; H1 cannot decide and asks the top.
  h1 = _seed(db, "h1")
  h2 = _seed(db, "h2", parent_id=h1.child_chat_id, parent_root="ask-h1")
  h2_question = _ask(client, db, h2, "ask-h2").json()["question_id"]
  _settle(db, h2.child_chat_id, "ask-h2")
  _settle(db, h1.child_chat_id, "ask-h1", text="Waiting for my helper.")
  assert _status(db, h2) == "needs_input"

  # A helper acting as a parent (a Claude host is never steered) receives the
  # question once its own turn has ended.
  starts = _capture_activity_starts(monkeypatch)
  asyncio.run(delegations.deliver_results_after_parent_settled(h1.child_chat_id))
  assert [(s["chat_id"], s["activity_id"]) for s in starts] == [(h1.child_chat_id, h2.id)]

  # H1's checkpoint run escalates with its own question.
  db.add(models.ChatRun(
    id="h1-checkpoint", root_run_id="ask-h1", chat_id=h1.child_chat_id,
    status="running", provider="claude", started_at=now_naive_utc(),
  ))
  db.commit()
  h1_question = _ask(
    client, db, h1, "h1-checkpoint", question="Owner: sqlite or postgres?",
  ).json()["question_id"]
  _settle(db, h1.child_chat_id, "h1-checkpoint", text="Asked my parent.")
  assert _status(db, h1) == "needs_input"
  assert _status(db, h2) == "needs_input"

  # The top answers H1; H1's answer run answers H2 with its own bearer.
  assert _answer(client, owner_token, h1, "postgres", h1_question).status_code == 202
  h1_answer = db.get(models.DelegationQuestion, h1_question).answer_run_id
  assert db.get(models.ChatRun, h1_answer).root_run_id == "ask-h1"
  response = client.post(
    f"/api/delegations/{h2.id}/messages",
    json={"message": "Use postgres.", "question_id": h2_question},
    headers=_bearer(_helper_token(db, h1, h1_answer)),
  )
  assert response.status_code == 202, response.text
  h2_answer = db.get(models.DelegationQuestion, h2_question).answer_run_id
  assert [call["run_token"] for call in schedules] == [h1_answer, h2_answer]
  assert _status(db, h1) == "running" and _status(db, h2) == "running"

  # H2's result then flows to H1 like any result.
  _settle(db, h2.child_chat_id, h2_answer, text="Migrated to postgres.")
  assert [r.id for r in delegations.available_delegation_results(db, h1.child_chat_id)] == [h2.id]


# --- Schema ----------------------------------------------------------------------------


def test_the_question_table_is_created_on_an_existing_install_without_migration(tmp_path):
  import sqlite3

  from app import schema_migrations as migrations
  from app.schema_migrations import run_migrations
  from tests.test_db_migrations import PREVIOUS_RELEASE_SCHEMA

  db_path = tmp_path / "previous-release.db"
  with sqlite3.connect(db_path) as connection:
    connection.executescript(PREVIOUS_RELEASE_SCHEMA.read_text(encoding="utf-8"))
  eng = create_engine(f"sqlite:///{db_path}")
  assert "delegation_questions" not in inspect(eng).get_table_names()
  delegation_columns = [c["name"] for c in inspect(eng).get_columns("delegations")]

  models.Base.metadata.create_all(bind=eng)
  run_migrations(eng)
  assert migrations.mapped_schema_gaps(eng) == []
  inspector = inspect(eng)
  assert "delegation_questions" in inspector.get_table_names()
  # The existing table is untouched; the new one carries its own uniqueness.
  assert [c["name"] for c in inspector.get_columns("delegations")][:len(delegation_columns)] == delegation_columns
  unique = {
    tuple(item["column_names"]) for item in inspector.get_unique_constraints("delegation_questions")
  } | {
    tuple(item["column_names"]) for item in inspector.get_indexes("delegation_questions")
    if item.get("unique")
  }
  assert {("asking_run_id",), ("answer_run_id",)} <= unique
  assert inspector.get_foreign_keys("delegation_questions") == []


def test_new_helpers_are_told_to_ask_their_parent_instead_of_ending_blocked():
  prompt = delegations.RunPolicy(
    delegation_id="d", app_id=None, provider="claude", model=None, effort=None,
    cwd="/data",
  ).system_prompt
  assert "call ask_parent with one precise question, then end your turn" in prompt
  assert "Do not ask the owner an interactive question" in prompt
  from app.continuations import continuation_actor_label
  assert continuation_actor_label({
    "kind": "continuation", "continuation_reason": "helper_answer",
  }) == "Parent answer"


def test_retry_of_committed_answer_does_not_cross_a_new_owner_card(
  client, owner_token, db, schedules,
):
  row, question_id = _asked(client, owner_token, db, 'answer-new-card')
  question = _commit_answer_without_scheduling(db, row, question_id)
  child = db.get(models.Chat, row.child_chat_id)
  child.pending_question_id = 'new-owner-card'
  db.commit()
  # This exact answer is already saved; acknowledging a retry is fine, but
  # it must not create a provider task while an owner decision is outstanding.
  response = _answer(client, owner_token, row, 'Use sqlite.', question_id)
  assert response.status_code == 202
  assert response.json()['already_answered'] is True
  assert schedules == []
  db.expire_all()
  assert db.get(models.Chat, row.child_chat_id).pending_question_id == 'new-owner-card'
  db.get(models.Chat, row.child_chat_id).pending_question_id = None
  db.commit()
  assert _answer(client, owner_token, row, 'Use sqlite.', question_id).status_code == 202
  assert [item['run_token'] for item in schedules] == [question.answer_run_id]


@pytest.mark.parametrize('approved', [False, True])
def test_answer_retry_preserves_the_existing_activation_wait_boundary(
  client, owner_token, db, schedules, approved,
):
  row, question_id = _asked(client, owner_token, db, 'answer-activation')
  question = _commit_answer_without_scheduling(db, row, question_id)
  now = now_naive_utc()
  wait = models.ChatWait(
    id='child-activation', chat_id=row.child_chat_id,
    kind='platform_activation', description='Activate child changes',
    condition_owner='Startup', status='armed', next_check_at=now,
    deadline_at=now + timedelta(days=1),
    action_approved_at=now if approved else None,
  )
  db.add(wait)
  db.commit()
  # A passive monitor is not a restart hold. An approved activation is:
  # answering a helper question cannot impersonate its authenticated wake.
  assert _answer(client, owner_token, row, 'Use sqlite.', question_id).status_code == 202
  assert len(schedules) == (0 if approved else 1)
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).status == 'armed'
  if approved:
    wait = db.get(models.ChatWait, wait.id)
    wait.status = 'met'
    db.commit()
    assert _answer(client, owner_token, row, 'Use sqlite.', question_id).status_code == 202
    assert schedules == []  # Ready alone is not activation delivery.
    wait.resume_delivered_at = now_naive_utc()
    db.commit()
    assert _answer(client, owner_token, row, 'Use sqlite.', question_id).status_code == 202
  assert [item['run_token'] for item in schedules] == [question.answer_run_id]


# --- Answering while the helper briefly runs again in the asking root ---------


def _wake_in_asking_root(db, row, run_id, root):
  """The helper is running again in its asking root (e.g. its child's result).

  This is the run a ``_parent_wake_continuation_root`` wake creates: same
  logical root, a new physical run, a live runner marker.
  """
  db.expire_all()
  db.add(models.ChatRun(
    id=run_id, root_run_id=root, chat_id=row.child_chat_id, status="running",
    provider=row.provider, initiated_by_app_id=row.app_id,
    started_at=now_naive_utc(),
  ))
  chat = db.get(models.Chat, row.child_chat_id)
  chat.live_assistant = {"id": run_id, "role": "assistant", "blocks": [], "ts": 1}
  db.commit()
  assert chat_mod.mark_starting(row.child_chat_id)


def _drain(chat_id, ending_run_token, status="completed"):
  """The settling run's own turn-end queue drain (no provider is run)."""
  next_user, _messages, _session, disposition = asyncio.run(
    chat_mod._drain_and_release(
      None, chat_id, None, f"next-{ending_run_token}",
      ending_run_token=ending_run_token, ending_status=status,
    )
  )
  return next_user, disposition


def _sweep_idle_queues(monkeypatch):
  """One pass of the existing age-gated idle-pending sweep, age gate elapsed."""
  monkeypatch.setattr(chat_mod, "_IDLE_PENDING_MIN_AGE_SECS", 0.0)
  with SessionLocal() as session:
    return asyncio.run(chat_mod.sweep_idle_pending_chats(session))


def _queued_answers(db, row, question_id):
  db.expire_all()
  cid = delegations.helper_answer_continuation_id(question_id)
  return [
    message for message in db.get(models.Chat, row.child_chat_id).pending_messages or []
    if message.get("cid") == cid
  ]


def test_an_answer_during_the_helpers_child_result_wake_starts_when_it_settles(
  client, owner_token, db, schedules, monkeypatch,
):
  # Top -> H1 -> H2. H1 asked the top and settled; the top was woken once.
  h1, question_id = _asked(client, owner_token, db, "nest-wake")
  question = db.get(models.DelegationQuestion, question_id)
  starts = _capture_activity_starts(monkeypatch)
  asyncio.run(delegations.wake_parent_after_child_settled(h1.child_chat_id))
  assert len(starts) == 1
  delivery = delegations.build_delegation_result_context(
    db, h1.parent_chat_id, source_work_id=h1.parent_root_run_id,
  )
  assert delegations.mark_results_delivered(db, dict(delivery.results))
  db.commit()

  # H1's own helper H2 finishes; its result wakes H1 in H1's asking root.
  h2 = _seed(db, "nest-wake-h2", parent_id=h1.child_chat_id, parent_root="ask-nest-wake")
  _settle(db, h2.child_chat_id, "ask-nest-wake-h2", text="Inventory done.")
  assert delegations._parent_wake_continuation_root(
    db, h1.child_chat_id, h2.parent_root_run_id,
  ) == question.root_run_id
  _wake_in_asking_root(db, h1, "h1-wake", question.root_run_id)
  assert _status(db, h1) == "running"

  # The top's answer lands now instead of a 409 that loses it.
  response = _answer(client, owner_token, h1, "Use sqlite.", question_id)
  assert response.status_code == 202, response.text
  body = response.json()
  assert (body["already_answered"], body["answer_queued"]) == (False, True)
  assert "starts as soon as" in body["note"]
  assert db.get(models.ChatRun, question.answer_run_id) is None
  [queued] = _queued_answers(db, h1, question_id)
  assert (queued["kind"], queued["continuation_reason"]) == ("continuation", "helper_answer")
  assert not queued.get("hidden")
  assert schedules == []
  # Exactly once: a retry attaches, a different answer is refused.
  again = _answer(client, owner_token, h1, "Use sqlite.", question_id)
  assert again.status_code == 202 and again.json()["already_answered"] is True
  different = _answer(client, owner_token, h1, "Use postgres.", question_id)
  assert different.status_code == 409
  assert different.json()["detail"]["code"] == "question_already_answered"
  assert len(_queued_answers(db, h1, question_id)) == 1 and schedules == []

  # The wake run settles: its drain starts the reserved answer run in the
  # same commit that closes the wake run.
  next_user, disposition = _drain(h1.child_chat_id, "h1-wake")
  assert disposition is chat_mod.chat_queue.TerminalDisposition.CONTINUATION_PROMOTED
  assert next_user["_run_token"] == question.answer_run_id
  assert next_user["content"].endswith("\n\nUse sqlite.")
  db.expire_all()
  assert db.get(models.ChatRun, "h1-wake").status == "completed"
  run = db.get(models.ChatRun, question.answer_run_id)
  assert (run.status, run.root_run_id, run.initiated_by_app_id, run.owner_input_at) == (
    "running", question.root_run_id, None, None,
  )
  child = db.get(models.Chat, h1.child_chat_id)
  assert child.pending_messages == []
  assert transcript_rows.history(child)[-1]["cid"] == delegations.helper_answer_continuation_id(question_id)
  assert delegations.committed_answer_content(db, question) == next_user["content"]

  # No second parent wake for the already-answered question.
  assert _status(db, h1) == "running"
  asyncio.run(delegations.wake_parent_after_child_settled(h1.child_chat_id))
  asyncio.run(delegations.wake_parents_for_completed_delegations())
  assert len(starts) == 1
  late = _answer(client, owner_token, h1, "Use sqlite.", question_id)
  assert late.json()["already_answered"] is True and schedules == []

  # The answer run's own report is the next (and only next) parent wake.
  chat_mod.discard_starting(h1.child_chat_id)
  _settle(db, h1.child_chat_id, question.answer_run_id, text="Migrated.")
  asyncio.run(delegations.wake_parent_after_child_settled(h1.child_chat_id))
  assert len(starts) == 2


def test_a_queued_answer_starts_after_a_failed_wake_run_too(
  client, owner_token, db, schedules,
):
  row, question_id = _asked(client, owner_token, db, "wake-fail")
  question = db.get(models.DelegationQuestion, question_id)
  _wake_in_asking_root(db, row, "wake-fail-run", question.root_run_id)
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 202
  next_user, _ = _drain(row.child_chat_id, "wake-fail-run", status="failed")
  assert next_user["_run_token"] == question.answer_run_id
  db.expire_all()
  assert db.get(models.ChatRun, "wake-fail-run").status == "failed"
  assert db.get(models.ChatRun, question.answer_run_id).root_run_id == question.root_run_id


def test_a_running_fresh_root_still_refuses_and_saves_nothing(
  client, owner_token, db, schedules,
):
  row, question_id = _asked(client, owner_token, db, "wake-fresh")
  _wake_in_asking_root(db, row, "fresh-root-run", "fresh-root-run")
  response = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert response.status_code == 409
  detail = response.json()["detail"]
  assert detail["code"] == "question_not_open"
  assert "running" in detail["message"]
  assert _queued_answers(db, row, question_id) == []
  assert schedules == []


def test_an_older_question_is_not_answered_behind_a_newer_one(
  client, owner_token, db, schedules,
):
  row, first_id = _asked(client, owner_token, db, "wake-old")
  _wake_in_asking_root(db, row, "wake-old-run", "ask-wake-old")
  newer = _ask(client, db, row, "wake-old-run", question="And the cache?")
  assert newer.status_code == 200
  response = _answer(client, owner_token, row, "Use sqlite.", first_id)
  assert response.status_code == 409
  assert response.json()["detail"]["code"] == "question_not_open"
  assert _queued_answers(db, row, first_id) == []


def test_owner_stop_preserves_a_queued_answer_without_starting_it(
  client, owner_token, db, schedules, monkeypatch,
):
  row, question_id = _asked(client, owner_token, db, "wake-stop")
  question = db.get(models.DelegationQuestion, question_id)
  _wake_in_asking_root(db, row, "wake-stop-run", question.root_run_id)
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 202

  _stopped, cleared = asyncio.run(chat_mod.stop_chat_for(row.child_chat_id))
  # Stop wins: nothing starts, and the parent's answer is neither re-sent as
  # owner text nor dropped.
  assert delegations.helper_answer_continuation_id(question_id) not in cleared
  db.expire_all()
  assert db.get(models.ChatRun, "wake-stop-run").status == "stopped"
  assert len(_queued_answers(db, row, question_id)) == 1
  assert _status(db, row) == "stopped"
  chat_mod.reconcile_startup_chats(db)
  assert asyncio.run(delegations.reconcile_unstarted_delegations()) == 0
  assert _sweep_idle_queues(monkeypatch) == []
  assert schedules == []
  db.expire_all()
  assert db.get(models.ChatRun, question.answer_run_id) is None

  # Only the parent explicitly resending that same answer resumes it, like a
  # follow-up to any stopped helper; a different answer is still refused.
  assert _answer(client, owner_token, row, "Use postgres.", question_id).status_code == 409
  resent = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert resent.status_code == 202 and resent.json()["already_answered"] is True
  assert [call["run_token"] for call in schedules] == [question.answer_run_id]
  assert _queued_answers(db, row, question_id) == []


def test_stop_agent_withdraws_a_queued_answer(
  client, owner_token, db, schedules, monkeypatch,
):
  row, question_id = _asked(client, owner_token, db, "wake-cancel")
  question = db.get(models.DelegationQuestion, question_id)
  _wake_in_asking_root(db, row, "wake-cancel-run", question.root_run_id)
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 202
  assert asyncio.run(delegations.cancel_delegation_execution(row.id)) is True
  assert asyncio.run(delegations.reconcile_unstarted_delegations()) == 0
  assert _sweep_idle_queues(monkeypatch) == []
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 409
  assert schedules == []
  assert db.get(models.ChatRun, question.answer_run_id) is None
  assert _status(db, row) == "cancelled"


@pytest.mark.parametrize("second_answer", ["Use sqlite.", "Use postgres."])
def test_concurrent_duplicate_answers_while_running_queue_one_answer(
  client, owner_token, db, schedules, second_answer,
):
  from app.deps import Principal
  from app.routes.delegations import DelegationMessage, message_delegation

  row, question_id = _asked(client, owner_token, db, "wake-race")
  question = db.get(models.DelegationQuestion, question_id)
  _wake_in_asking_root(db, row, "wake-race-run", question.root_run_id)
  owner = db.query(models.Owner).first()
  principal = Principal(owner=owner, app_id=None, scope="owner")

  async def answer(text):
    with SessionLocal() as session:
      try:
        return await message_delegation(
          row.id, DelegationMessage(message=text, question_id=question_id),
          principal=principal, db=session,
        )
      except HTTPException as exc:
        return exc

  async def both():
    return await asyncio.wait_for(asyncio.gather(
      answer("Use sqlite."), answer(second_answer),
    ), timeout=30)

  outcomes = asyncio.run(both())
  accepted = [item for item in outcomes if isinstance(item, dict)]
  refused = [item for item in outcomes if isinstance(item, HTTPException)]
  assert [item["already_answered"] for item in accepted].count(False) == 1
  if second_answer == "Use sqlite.":
    assert len(accepted) == 2 and not refused
  else:
    assert len(accepted) == 1
    assert refused[0].detail["code"] == "question_already_answered"
  assert len(_queued_answers(db, row, question_id)) == 1
  assert schedules == []
  next_user, _ = _drain(row.child_chat_id, "wake-race-run")
  assert next_user["_run_token"] == question.answer_run_id


@pytest.mark.parametrize("order", ["answer_first", "settle_first", "concurrent"])
def test_settling_and_answering_race_to_one_reserved_run_and_no_stale_wake(
  client, owner_token, db, schedules, monkeypatch, order,
):
  from app.deps import Principal
  from app.routes.delegations import DelegationMessage, message_delegation

  row, question_id = _asked(client, owner_token, db, f"wake-order-{order}")
  question = db.get(models.DelegationQuestion, question_id)
  starts = _capture_activity_starts(monkeypatch)
  asyncio.run(delegations.wake_parent_after_child_settled(row.child_chat_id))
  delivery = delegations.build_delegation_result_context(
    db, row.parent_chat_id, source_work_id=row.parent_root_run_id,
  )
  assert delegations.mark_results_delivered(db, dict(delivery.results))
  db.commit()
  assert len(starts) == 1
  wake = f"wake-order-run-{order}"
  _wake_in_asking_root(db, row, wake, question.root_run_id)
  owner = db.query(models.Owner).first()
  principal = Principal(owner=owner, app_id=None, scope="owner")

  async def answer():
    with SessionLocal() as session:
      return await message_delegation(
        row.id, DelegationMessage(message="Use sqlite.", question_id=question_id),
        principal=principal, db=session,
      )

  async def settle():
    return await chat_mod._drain_and_release(
      None, row.child_chat_id, None, f"next-{wake}",
      ending_run_token=wake, ending_status="completed",
    )

  async def run():
    if order == "answer_first":
      return await answer(), await settle()
    if order == "settle_first":
      drained = await settle()
      return await answer(), drained
    return await asyncio.wait_for(asyncio.gather(answer(), settle()), timeout=30)

  answered, drained = asyncio.run(run())
  promoted = drained[0]
  assert answered["already_answered"] is False
  tokens = [call["run_token"] for call in schedules]
  if promoted is not None:
    tokens.append(promoted["_run_token"])
  assert tokens == [question.answer_run_id]
  db.expire_all()
  assert db.get(models.ChatRun, wake).status == "completed"
  assert db.get(models.ChatRun, question.answer_run_id).root_run_id == question.root_run_id
  assert _queued_answers(db, row, question_id) == []
  # The question was answered before anything observed the settled wake run
  # (or the answer run is already current): the parent is not woken again.
  assert _status(db, row) == "running"
  asyncio.run(delegations.wake_parent_after_child_settled(row.child_chat_id))
  asyncio.run(delegations.wake_parents_for_completed_delegations())
  assert len(starts) == 1


def test_a_restart_between_accepting_and_starting_a_queued_answer_recovers_once(
  client, owner_token, db, schedules, monkeypatch,
):
  row, question_id = _asked(client, owner_token, db, "wake-crash")
  question = db.get(models.DelegationQuestion, question_id)
  _wake_in_asking_root(db, row, "wake-crash-run", question.root_run_id)
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 202

  # The process dies while the wake run is still running: no drain happens.
  chat_mod.discard_starting(row.child_chat_id)
  remove_broadcast(row.child_chat_id)
  chat_mod.reconcile_startup_chats(db)
  db.expire_all()
  assert db.get(models.ChatRun, "wake-crash-run").status == "interrupted"
  assert len(_queued_answers(db, row, question_id)) == 1

  # Boot starts no queued work; the existing age-gated idle-pending sweep
  # promotes the exact queued answer under its reserved run id.
  assert asyncio.run(delegations.reconcile_unstarted_delegations()) == 0
  assert schedules == []
  assert _sweep_idle_queues(monkeypatch) == [row.child_chat_id]
  assert [call["next_user"]["_run_token"] for call in schedules] == [
    question.answer_run_id,
  ]
  db.expire_all()
  run = db.get(models.ChatRun, question.answer_run_id)
  assert run.root_run_id == question.root_run_id and run.owner_input_at is None
  assert _queued_answers(db, row, question_id) == []
  assert _sweep_idle_queues(monkeypatch) == []
  assert asyncio.run(delegations.reconcile_unstarted_delegations()) == 0
  retry = _answer(client, owner_token, row, "Use sqlite.", question_id)
  assert retry.json()["already_answered"] is True
  assert len(schedules) == 1


def test_a_queued_answer_does_not_cross_an_owner_card_on_recovery(
  client, owner_token, db, schedules, monkeypatch,
):
  row, question_id = _asked(client, owner_token, db, "wake-card")
  question = db.get(models.DelegationQuestion, question_id)
  _wake_in_asking_root(db, row, "wake-card-run", question.root_run_id)
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 202
  chat_mod.discard_starting(row.child_chat_id)
  remove_broadcast(row.child_chat_id)
  chat_mod.reconcile_startup_chats(db)
  child = db.get(models.Chat, row.child_chat_id)
  child.pending_question_id = "owner-card"
  db.commit()
  assert asyncio.run(delegations.reconcile_unstarted_delegations()) == 0
  assert _sweep_idle_queues(monkeypatch) == []
  assert schedules == []
  assert len(_queued_answers(db, row, question_id)) == 1


def test_a_queued_answer_keeps_the_helpers_app_attribution_and_no_owner_authority(
  client, owner_token, db, schedules,
):
  app = models.App(
    slug="queued-answer-owner", source_dir="/tmp/mobius-tests/queued-answer-owner",
    name="Subagents", description="", jsx_source="",
  )
  db.add(app)
  db.commit()
  row, question_id = _asked(client, owner_token, db, "wake-app", app_id=app.id)
  question = db.get(models.DelegationQuestion, question_id)
  _wake_in_asking_root(db, row, "wake-app-run", question.root_run_id)
  assert _answer(client, owner_token, row, "Use sqlite.", question_id).status_code == 202
  next_user, _ = _drain(row.child_chat_id, "wake-app-run")
  assert next_user["_run_token"] == question.answer_run_id
  db.expire_all()
  run = db.get(models.ChatRun, question.answer_run_id)
  assert (run.initiated_by_app_id, run.owner_input_at, run.goal_id) == (app.id, None, None)
  stored = transcript_rows.history(db.get(models.Chat, row.child_chat_id))[-1]
  assert "_initiated_by_app_id" not in stored and not stored.get("hidden")


def test_nested_question_commit_invalidates_idle_goal_owner_and_direct_parent(client, db, monkeypatch):
  row = _seed(db, "nested-invalidation")
  db.add(create_chat(id="idle-goal-owner", title="Owner", messages=[]))
  db.flush()
  db.add(models.ChatGoal(id="immutable-goal", chat_id="idle-goal-owner", objective="Own nested work"))
  row.goal_id = "immutable-goal"
  db.commit()
  emitted = []
  monkeypatch.setattr("app.broadcast.get_system_broadcast", lambda: type(
    "Bus", (), {"publish": lambda self, event: emitted.append(event)})())
  response = _ask(client, db, row, "ask-nested-invalidation")
  assert response.status_code == 200, response.text
  assert {event["chat_id"] for event in emitted if event.get("source") == "goal"} == {
    "idle-goal-owner", row.parent_chat_id}
  assert db.get(models.ChatRun, "ask-nested-invalidation").status == "running"


def test_batched_listing_preserves_questions_and_constant_read_cost(client, owner_token, db):
  from sqlalchemy import event

  rows = [_asked(client, owner_token, db, suffix=f"batch-question-{index}")[0]
          for index in range(3)]
  # Seed lifecycle receipts before measuring the ordinary polling path.
  delegations.serialize_delegation_list(db, rows)

  def read(selected):
    db.expire_all()
    selected = [db.get(models.Delegation, row.id) for row in selected]
    statements = []

    def capture(_conn, _cursor, statement, *_args):
      if statement.lstrip().upper().startswith("SELECT"):
        statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", capture)
    try:
      result = delegations.serialize_delegation_list(db, selected)
    finally:
      event.remove(db.bind, "before_cursor_execute", capture)
    return result, len(statements)

  one, one_reads = read(rows[:1])
  many, many_reads = read(rows)
  assert one_reads == many_reads
  assert len(one) == 1 and len(many) == 3
  for item, row in zip(many, rows):
    assert item["status"] == "needs_input"
    assert item["question"]["question"] == "Which database?"
    assert item == delegations.serialize_delegation(db, row, include_result=False)
