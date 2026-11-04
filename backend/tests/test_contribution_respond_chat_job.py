"""A live "Respond to feedback" chat is its PR's one agent job, though no review run tracks it."""
import pytest

import app.chat as chat_module
from app import models
from app.chat_writer import create_chat
from app.routes import contribution_reviews as routes
from app.timeutil import now_naive_utc
from tests.test_contribution_review_admission import owner, refusal, start  # noqa: F401  (fixture)
from tests.test_contribution_review_runs import settle_fixture_review, setup  # noqa: F401  (fixture)

BUSY = (409, "An agent is already working on this PR.")


@pytest.fixture
def working(monkeypatch):
  """The chat ids whose turn is running right now."""
  running = set()
  monkeypatch.setattr(chat_module, "is_chat_running", lambda chat_id: chat_id in running)
  return running


def respond_chat(db, chat_id="respond-7", *, scope="contribute-address:example/project#7", app_id=1, **fields):
  chat = create_chat(id=chat_id, title="Respond", created_by_app_id=app_id,
                     agent_settings_json={"chat_scope": scope}, **fields)
  db.add(chat)
  db.commit()
  return chat


def listed(db, principal):
  return routes.list_reviews(1, db, principal)["respond_chats"]


def test_a_running_respond_chat_refuses_a_second_agent_on_its_pr(owner, working):
  db, fixture_run, principal, started = owner
  settle_fixture_review(db, fixture_run)
  working.add(respond_chat(db).id)
  assert refusal(db, principal) == BUSY
  assert refusal(db, principal, mode="review_fix_merge", takeover_reason="The author is away.") == BUSY
  assert started == []


def test_a_respond_chat_holds_its_pr_whatever_case_its_repository_is_written_in(owner, working):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  working.add(respond_chat(db, scope="contribute-address:Example/Project#7").id)
  assert refusal(db, principal) == BUSY


def test_a_respond_chat_waiting_for_the_owner_still_holds_its_pr(owner, working):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  respond_chat(db, pending_question_id="question-1")
  assert refusal(db, principal) == BUSY


def test_a_respond_chat_with_no_next_move_holds_nothing(owner, working):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  respond_chat(db)
  assert start(db, principal)["run"]["mode"] == "review"


def test_a_deleted_respond_chat_holds_nothing(owner, working):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  chat = respond_chat(db)
  working.add(chat.id)
  chat.deleted_at = now_naive_utc()
  db.commit()
  assert start(db, principal)["run"]["mode"] == "review"


def test_a_respond_chat_on_another_pr_holds_nothing_here(owner, working):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  working.add(respond_chat(db, scope="contribute-address:example/project#8").id)
  working.add(respond_chat(db, "respond-other-repo", scope="contribute-address:example/other#7").id)
  assert start(db, principal)["run"]["mode"] == "review"


def test_only_this_apps_respond_chats_count(owner, working):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  db.add(models.App(id=2, name="Other", slug="other", source_dir="test-other"))
  db.commit()
  working.add(respond_chat(db, "other-app", app_id=2).id)
  working.add(respond_chat(db, "owner-chat", app_id=None).id)
  working.add(respond_chat(db, "unrelated", scope="workout:7").id)
  assert listed(db, principal) == []
  assert start(db, principal)["run"]["mode"] == "review"


def test_the_list_names_each_live_respond_chat_and_whether_it_needs_an_answer(owner, working):
  db, _, principal, _ = owner
  working.add(respond_chat(db).id)
  respond_chat(db, "respond-8", scope="contribute-address:example/project#8", pending_question_id="question-1")
  respond_chat(db, "respond-9", scope="contribute-address:example/project#9")  # nothing will move it
  assert sorted(listed(db, principal), key=lambda chat: chat["number"]) == [
    {"repo": "example/project", "number": 7, "chat_id": "respond-7", "state": "running"},
    {"repo": "example/project", "number": 8, "chat_id": "respond-8", "state": "needs_answer"}]


def test_the_job_is_a_respond_job_only_for_an_app(owner, working):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  working.add(respond_chat(db).id)
  job = routes._pr_job(db, principal.owner.id, "Example/Project", 7, app_id=1)
  assert job == {"kind": "respond", "state": "running", "chat_id": "respond-7"}
  assert routes._pr_job(db, principal.owner.id, "example/project", 7) is None
