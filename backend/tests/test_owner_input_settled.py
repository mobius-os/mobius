"""What the agent hears about requests settled since its last turn.

The rows record what the owner answered, and who closed a request; the next
turn start, a compaction restore and the tool's list hand that back as data
until a turn that saw it completes.
"""
import json
from datetime import timedelta

import pytest

from app import models, owner_input_requests as requests
from app.chat_writer import AnswerQuestion, get_writer
from app.database import SessionLocal
from tests import owner_input_fixtures as inputs
from tests.owner_input_fixtures import isolated_restart_source, run_auth  # noqa: F401


QUESTION = {"prompt": "How formal should the changelog be?"}


def _settled_block(context: str) -> list[dict]:
  return json.loads(context.rsplit("<settled_owner_requests>", 1)[1].split(
    "</settled_owner_requests>", 1)[0])


def _context(chat_id: str) -> str:
  with SessionLocal() as session:
    return requests.build_open_requests_context(session, chat_id)


def _answer_approval(chat_id: str, card: dict) -> None:
  approval = inputs.entry(card, "publish")
  get_writer().submit(AnswerQuestion(
    chat_id=chat_id, question_id=card["question_id"],
    answers={approval["question"]: "Publish"},
    selected_options={approval["id"]: ["0"]}, require_exact_card=True,
  )).result(timeout=5)


@pytest.mark.asyncio
async def test_an_answer_on_the_card_reaches_the_next_turn_until_a_turn_completes(
  client, chat, db, run_auth, monkeypatch,
):
  inputs.add(client, chat.id, run_auth, "publish", **inputs.APPROVAL)
  inputs.add(client, chat.id, run_auth, "tone", **QUESTION)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  _answer_approval(chat.id, card)
  approval_id = inputs.entry(card, "publish")["id"]
  expected = {
    "key": "publish", "id": approval_id, "kind": "approval", "status": "answered",
    "settled_by": "owner", "answer": {"selected": [{"id": "0", "label": "Publish"}]},
    "option_chosen": True,
  }

  # The next turn starts with it, beside what is still open.
  inputs.start_turn(chat.id, "next-turn")
  context = _context(chat.id)
  assert '"key":"tone"' in context.split("The <settled_owner_requests>")[0]
  assert "an approval's choice is the selected option label" in context
  assert _settled_block(context) == [expected]
  listed = inputs.edit(client, chat.id, inputs.agent_auth(db, chat.id, "next-turn"),
                       op="list").json()
  assert listed["settled"] == [expected]

  # Once a turn that saw it completes, it is no longer reported.
  await inputs.complete_turn(chat.id, monkeypatch, "next-turn")
  with SessionLocal() as session:
    assert requests.settled_since_last_turn(session, chat.id) == []
  assert "<settled_owner_requests>" not in _context(chat.id)


@pytest.mark.asyncio
async def test_a_platform_withdrawal_is_reported_with_its_reason(
  client, chat, db, run_auth, monkeypatch,
):
  from app.agent_work_claims import finish_work

  inputs.add(client, chat.id, run_auth, "publish", **inputs.APPROVAL)
  with SessionLocal() as session:
    owner = session.query(models.Owner).first()
    finish_work(session, owner_id=owner.id, chat_id=chat.id,
                work_key=inputs.APPROVAL["work_key"], outcome="Not needed.", release=True)

  # The turn end cannot show an approval whose claim is gone; it withdraws it.
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  inputs.start_turn(chat.id, "next-turn", owner_input=False)

  context = _context(chat.id)
  assert "<open_owner_requests>" not in context
  [report] = _settled_block(context)
  assert report["key"] == "publish" and report["status"] == "withdrawn"
  assert report["settled_by"] == "platform" and report["option_chosen"] is False
  assert "no longer holds the work claim" in report["reason"]


def test_an_agents_own_resolve_and_withdrawal_are_attributed_to_it(client, chat, run_auth):
  inputs.add(client, chat.id, run_auth, "tone", **QUESTION)
  inputs.add(client, chat.id, run_auth, "target", prompt="Staging or production?")
  inputs.edit(client, chat.id, run_auth, op="resolve", key="tone", answer="Casual",
              source="owner_message", reason="Owner wrote: keep it casual.")
  inputs.edit(client, chat.id, run_auth, op="withdraw", key="target",
              reason="The release was cancelled.")

  settled = inputs.edit(client, chat.id, run_auth, op="list").json()["settled"]

  assert [(row["key"], row["status"], row["settled_by"], row.get("answer"), row["reason"])
          for row in settled] == [
    ("tone", "answered", "agent:owner_message", {"text": "Casual"},
     "Owner wrote: keep it casual."),
    ("target", "withdrawn", "agent", None, "The release was cancelled."),
  ]


def test_the_settled_rows_stay_within_their_share_of_the_block(chat, db):
  for index in range(requests.SETTLED_SHOWN + 2):
    requests.add_request(db, chat_id=chat.id, run_id=None, goal_id=None,
                         key=f"q-{index}", spec={"prompt": f"Question {index}?"})
    requests.withdraw_request(db, chat_id=chat.id, ref=f"q-{index}", reason="x" * 450)

  context = requests.build_open_requests_context(db, chat.id)

  rows = _settled_block(context)
  body = context.rsplit("<settled_owner_requests>", 1)[1].split("</settled_owner_requests>")[0]
  assert len(body) <= requests.SETTLED_CONTEXT_CHARS
  # Newest last; the oldest are left out first, and the cut is said.
  assert rows[-1]["key"] == f"q-{requests.SETTLED_SHOWN + 1}"
  assert "owner_input with op list shows them whole" in context
  with SessionLocal() as session:
    assert len(requests.settled_since_last_turn(session, chat.id)) == requests.SETTLED_SHOWN


@pytest.mark.asyncio
async def test_a_message_written_before_the_card_existed_is_not_framed_as_writing_past_it(
  chat, db, run_auth, monkeypatch,
):
  requests.add_request(db, chat_id=chat.id, run_id=None, goal_id=None,
                       key="tone", spec=QUESTION)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat), db=db)
  inputs.start_turn(chat.id, "owner-turn", "Use staging")
  with SessionLocal() as session:
    published = session.get(models.ChatRun, inputs.asking_run_id(chat)).ended_at
    assert requests.owner_wrote_past_open_card(session, chat.id, "owner-turn") is True
    # The same message, accepted while the asking turn still ran (queued).
    session.get(models.ChatRun, "owner-turn").owner_input_at = published - timedelta(seconds=1)
    session.commit()
    assert requests.owner_wrote_past_open_card(session, chat.id, "owner-turn") is False
