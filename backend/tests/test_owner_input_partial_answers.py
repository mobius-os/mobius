"""A partial answer on a request-list card leaves the entries that settle on
their own (a secret's sealed form, an unpressed restart) usable on it."""
from app import models, owner_input_requests as requests
from tests import owner_input_fixtures as inputs
from tests.owner_input_fixtures import isolated_restart_source  # noqa: F401


def test_a_partial_answer_keeps_the_secret_and_restart_entries_on_their_card(
  chat, db, tmp_path,
):
  rows = [
    requests.add_request(db, chat_id=chat.id, run_id=None, goal_id=None, key="tone",
                         spec={"prompt": "How formal should the changelog be?"}),
    requests.add_request(db, chat_id=chat.id, run_id=None, goal_id=None, key="mail-key",
                         spec=inputs.secret_body(tmp_path)),
    requests.add_request(db, chat_id=chat.id, run_id=None, goal_id=None, key="restart",
                         spec={"kind": "restart"}),
  ]
  card = requests.compose_card(rows, chat_id=chat.id, run_id="run", boot_id="boot")
  requests.stage_card_shown(db, chat.id, card)
  db.commit()
  ids = {row.key: row.id for row in rows}
  answers = {"How formal should the changelog be?": "Casual"}

  assert requests.self_settling_entries_left(card, answers) == {
    ids["mail-key"], ids["restart"]}
  # Written feedback settles a restart entry; a settled entry is not left.
  assert requests.self_settling_entries_left(
    {**card, "request_states": {ids["mail-key"]: {"status": "consumed"}}},
    {**answers, card["questions"][2]["question"]: "Not now, I am presenting."},
  ) == set()

  requests.stage_card_answers(db, chat.id, {**card, "answers": answers})
  db.commit()

  db.expire_all()
  stamps = {row.key: (row.status, row.published_question_id)
            for row in db.query(models.OwnerInputRequest).filter_by(chat_id=chat.id)}
  assert stamps == {
    "tone": ("answered", card["question_id"]),
    "mail-key": ("open", card["question_id"]),
    "restart": ("open", card["question_id"]),
  }
