"""Shared work admission preserves chat routes independently of owner questions."""

from types import SimpleNamespace

import pytest

from app import chat, models


@pytest.mark.parametrize("state", [
  "idle", "live", "queued", "running", "parked", "resume_pending",
  "owner_question", "live_question",
])
def test_chat_busy_counts_work_but_not_open_owner_questions(monkeypatch, state):
  row = SimpleNamespace(
    id="busy-chat", pending_messages=[{}] if state == "queued" else [],
    pending_question_id="question" if state == "owner_question" else None,
  )
  monkeypatch.setattr(chat, "is_chat_running", lambda cid: state == "live")
  monkeypatch.setattr(chat.questions, "is_waiting", lambda cid: state == "live_question")
  seen = []

  def has_run(db, cid, statuses):
    seen.append(tuple(statuses))
    return state in statuses

  monkeypatch.setattr(chat.run_state, "has_run_in", has_run)
  assert chat.is_chat_busy(None, row) is (state not in {"idle", "owner_question", "live_question"})
  if seen:
    assert seen == [models.NONTERMINAL_RUN_STATUSES]


def test_provider_handoff_can_supersede_a_parked_continuation(monkeypatch):
  row = SimpleNamespace(id="parked-chat", pending_messages=[], pending_question_id=None)
  monkeypatch.setattr(chat, "is_chat_running", lambda cid: False)
  monkeypatch.setattr(chat.questions, "is_waiting", lambda cid: False)
  monkeypatch.setattr(chat.run_state, "has_run_in", lambda db, cid, statuses: "parked" in statuses)
  assert chat.is_chat_busy(None, row)
  assert not chat.is_chat_busy(None, row, run_statuses=("running",))
