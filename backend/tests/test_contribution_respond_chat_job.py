"""A live "Respond to feedback" chat is its PR's one agent job, though no review run tracks it."""
import asyncio
from contextlib import asynccontextmanager
from functools import partial

import pytest
from fastapi import HTTPException

import app.chat as chat_module
from app import (chat_queue, contribution_direct_merge as direct_merge, contribution_pull_status as pull_status,
                 contribution_review_merge_actions as merge_actions, contribution_review_runs as domain, models)
from app.chat_writer import create_chat
from app.database import SessionLocal
from app.deps import Principal
from app.routes import contribution_reviews as routes
from app.timeutil import now_naive_utc
from tests.test_contribution_direct_merge import FakeGitHub
from tests.test_contribution_pull_status import StatusGitHub
from tests.test_contribution_review_admission import owner, refusal, start, start_body  # noqa: F401  (fixture)
from tests.test_contribution_review_runs import ITEM, report, settle_fixture_review, setup  # noqa: F401  (fixture)

BUSY = (409, "An agent is already working on this PR.")
THE_PULL = {key: ITEM[key] for key in ("repo", "number", "head_sha")}


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


@pytest.fixture
def plain(fresh_db, monkeypatch):
  """The owner's own session with Contribute installed and nothing else faked: GitHub is each test's fake."""
  db = SessionLocal()
  owner = models.Owner(username="status-owner", hashed_password="unused")
  db.add(owner)
  db.add(models.App(id=1, name="Contribute", slug="contribute", source_dir="test-contribute",
                    github_access=True, token_nonce="nonce"))
  db.commit()
  monkeypatch.setattr(routes, "_validate_submit_app", lambda *args: "nonce")
  yield db, Principal(owner=owner, app_id=None)
  db.close()


def respond_started_while_waiting_for_the_connection(monkeypatch, module, db, working):
  """The owner starts Respond for the PR while the request waits for the GitHub connection."""
  real = module._github_connection_transaction

  @asynccontextmanager
  async def waiting():
    async with real():
      working.add(respond_chat(db).id)
      yield

  monkeypatch.setattr(module, "_github_connection_transaction", waiting)


def test_a_status_change_is_refused_when_a_respond_chat_started_during_its_wait_for_the_connection(
    plain, working, monkeypatch):
  db, principal = plain
  github = StatusGitHub()
  monkeypatch.setattr(routes, "_gh", github)
  respond_started_while_waiting_for_the_connection(monkeypatch, pull_status, db, working)
  request = routes.PullStatusChange(**THE_PULL)
  with pytest.raises(HTTPException) as refused:
    asyncio.run(routes.close_pull_now(1, request, db, principal))
  assert (refused.value.status_code, refused.value.detail) == BUSY
  assert github.writes_seen == []


def test_merge_now_is_refused_when_a_respond_chat_started_during_its_wait_for_the_connection(
    plain, working, monkeypatch):
  db, principal = plain
  github = FakeGitHub()
  monkeypatch.setattr(routes, "_gh", github)
  respond_started_while_waiting_for_the_connection(monkeypatch, direct_merge, db, working)
  with pytest.raises(HTTPException) as refused:
    asyncio.run(routes.merge_pull_now(1, routes.DirectMerge(**THE_PULL), db, principal))
  assert (refused.value.status_code, refused.value.detail) == BUSY
  assert github.merges == [] and github.enqueues == []


def test_a_review_merge_is_refused_when_a_respond_chat_started_during_its_wait_for_the_connection(
    setup, working, monkeypatch):
  respond_started_while_waiting_for_the_connection(monkeypatch, merge_actions, setup[0], working)
  monkeypatch.setattr(domain, "perform_merge", lambda *a: pytest.fail("merged under a Respond chat"))
  item = report(setup)["run"]["items"][0]
  assert (item["state"], item["summary"]) == ("needs_you", BUSY[1])


def test_the_held_job_lookup_reads_the_chats_when_asked_and_keeps_review_admission_out(owner, working):
  db, fixture_run, principal, _ = owner
  settle_fixture_review(db, fixture_run)
  hold = partial(routes._pr_job_held, db, principal.owner.id, "Example/Project", 7, app_id=1)
  admission = chat_queue.get_transition_lock(f"review-pr-admission:{principal.owner.id}")  # kept: the registry is weak

  async def scenario():
    async with hold() as job_now:
      assert admission._lock.locked() and job_now() is None
      working.add(respond_chat(db).id)
      assert job_now() == {"kind": "respond", "state": "running", "chat_id": "respond-7"}
      # A review run being admitted waits for the write this hold covers.
      other = asyncio.create_task(routes.start_reviews(1, start_body(db, principal, request_id="entering"), db, principal))
      await asyncio.sleep(.05)
      assert not other.done()
    assert not admission._lock.locked()
    with pytest.raises(HTTPException) as refused:
      await other  # and once admitted, it meets the Respond chat that started meanwhile
    assert (refused.value.status_code, refused.value.detail) == BUSY

  asyncio.run(scenario())
