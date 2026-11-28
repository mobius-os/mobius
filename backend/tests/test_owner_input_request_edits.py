"""Editing a recorded owner request: what each edit returns, how a re-add
merges, and why no edit loses a concurrent change or strands a claim."""
from types import SimpleNamespace

import pytest

from app import models, owner_input_requests as requests, timeutil
from app.database import SessionLocal
from tests import owner_input_fixtures as inputs
from tests.owner_input_fixtures import isolated_restart_source  # noqa: F401


CHOICE = {
  "prompt": "Which environment should the release go to?",
  "context": "Staging is frozen until Friday.",
  "options": [{"label": "Staging"}, {"label": "Production", "description": "Live now."}],
}


@pytest.fixture
def run_auth(chat, db):
  return inputs.start_run(db, chat.id, f"edits-{chat.id}")


def _claim(work_key):
  with SessionLocal() as session:
    row = session.query(models.AgentWorkClaim).filter_by(work_key=work_key).first()
    if row is not None:
      session.expunge(row)
    return row


def _open_claim(work_key):
  row = _claim(work_key)
  return row is not None and row.released_at is None and row.completed_at is None


# ---- Results --------------------------------------------------------------

def test_each_edit_names_what_it_did_and_only_add_carries_a_next_step(client, chat, run_auth):
  created = inputs.add(client, chat.id, run_auth, "release-target", **CHOICE).json()
  same = inputs.add(client, chat.id, run_auth, "release-target", **CHOICE).json()
  revised = inputs.edit(client, chat.id, run_auth, op="update", key="release-target",
                        multiple=True).json()
  unchanged = inputs.edit(client, chat.id, run_auth, op="update", key="release-target",
                          multiple=True).json()
  listed = inputs.edit(client, chat.id, run_auth, op="list").json()

  assert (created["result"], created["revision"]) == ("created", 1)
  assert len(created["next_action"].split(". ")) == 1
  # An identical re-add is not a new revision.
  assert (same["result"], same["revision"]) == ("unchanged", 1)
  assert set(revised) == {"result", "key", "revision", "open"}
  assert (revised["result"], revised["revision"]) == ("revised", 2)
  assert (unchanged["result"], unchanged["revision"]) == ("unchanged", 2)
  assert set(listed) == {"open", "settled"}


def test_an_edit_refers_to_a_request_by_key_only(client, chat, run_auth):
  row = inputs.add(client, chat.id, run_auth, "release-target", **CHOICE).json()["open"][0]

  by_id = inputs.edit(client, chat.id, run_auth, op="withdraw", id=row["id"], reason="Moot.")

  assert by_id.status_code == 422
  assert [r["key"] for r in inputs.edit(client, chat.id, run_auth, op="list").json()["open"]] == [
    "release-target"]


def test_an_option_needs_no_description():
  spec = requests.normalize_spec(CHOICE)

  assert spec["fields"][0]["options"] == [
    {"id": "0", "label": "Staging"},
    {"id": "1", "label": "Production", "description": "Live now."},
  ]
  with pytest.raises(requests.RequestError, match="an approval request does not take header"):
    requests.normalize_spec({"kind": "approval", "header": "Go", "prompt": "Go?"})


# ---- add on an open key merges exactly like update ------------------------

def test_re_adding_an_open_key_keeps_what_it_does_not_restate(client, chat, run_auth):
  inputs.add(client, chat.id, run_auth, "release-target", **CHOICE)

  revised = inputs.add(client, chat.id, run_auth, "release-target",
                       prompt="Staging or production for this release?").json()

  assert (revised["result"], revised["revision"]) == ("revised", 2)
  stored = inputs.request_row("release-target").spec_json
  assert stored["prompt"] == "Staging or production for this release?"
  assert stored["context"] == CHOICE["context"]
  assert [o["label"] for o in stored["fields"][0]["options"]] == ["Staging", "Production"]


def test_re_adding_an_approval_key_as_a_question_is_refused(client, chat, run_auth):
  inputs.add(client, chat.id, run_auth, "publish", **inputs.APPROVAL)

  refused = inputs.add(client, chat.id, run_auth, "publish", prompt="Publish today?")

  assert refused.status_code == 422 and "open approval request" in refused.text


# ---- Approval claims --------------------------------------------------------

@pytest.mark.parametrize("blocks", [
  pytest.param({"tasks": "deploy"}, id="malformed"),
  pytest.param({"tasks": ["deploy"], "extra": 1}, id="unknown-field"),
])
def test_an_approval_refused_for_its_blocks_takes_no_claim(client, chat, run_auth, blocks):
  refused = inputs.add(client, chat.id, run_auth, "publish", **inputs.APPROVAL, blocks=blocks)

  assert refused.status_code == 422, refused.text
  assert _claim(inputs.APPROVAL["work_key"]) is None
  with SessionLocal() as session:
    assert requests.open_requests(session, chat.id) == []


def test_an_approval_losing_a_same_key_race_releases_the_claim_it_took(
  client, chat, run_auth, monkeypatch,
):
  """A concurrent add of the same key for another action wins the open-key
  index; this add is refused and must not keep its own action claimed."""
  real_require_can_add = requests.require_can_add
  calls = []

  def other_add_lands_first(session, chat_id, spec):
    real_require_can_add(session, chat_id, spec)
    calls.append(True)
    if len(calls) == 2:
      # After this add's claim, before its row.
      with SessionLocal() as competing:
        requests.add_request(competing, chat_id=chat_id, run_id=None, goal_id=None,
                             key="publish", spec={**inputs.APPROVAL,
                                                  "prompt": "Publish the other notes?",
                                                  "work_key": "site:other-notes:publish"})

  monkeypatch.setattr(requests, "require_can_add", other_add_lands_first)

  refused = inputs.add(client, chat.id, run_auth, "publish", **inputs.APPROVAL)

  assert refused.status_code == 422 and "work_key cannot change" in refused.text
  claim = _claim(inputs.APPROVAL["work_key"])
  assert claim is not None and claim.released_at is not None
  assert "not recorded" in claim.outcome


def test_withdrawing_an_approval_releases_its_claim(client, chat, run_auth):
  inputs.add(client, chat.id, run_auth, "publish", **inputs.APPROVAL)
  assert _open_claim(inputs.APPROVAL["work_key"])

  withdrawn = inputs.edit(client, chat.id, run_auth, op="withdraw", key="publish",
                          reason="The owner said not to publish.").json()

  assert (withdrawn["result"], withdrawn["released"]) == ("withdrawn", True)
  claim = _claim(inputs.APPROVAL["work_key"])
  assert claim.released_at is not None
  assert claim.outcome == "Approval request withdrawn: The owner said not to publish."


def test_withdrawing_one_of_two_approvals_for_one_action_keeps_its_claim(
  client, chat, run_auth,
):
  inputs.add(client, chat.id, run_auth, "publish", **inputs.APPROVAL)
  inputs.add(client, chat.id, run_auth, "publish-again", **{
    **inputs.APPROVAL, "prompt": "Publish the notes after review?"})

  withdrawn = inputs.edit(client, chat.id, run_auth, op="withdraw", key="publish",
                          reason="Merged into the other request.").json()

  assert withdrawn["released"] is False
  assert _open_claim(inputs.APPROVAL["work_key"])


def test_an_app_agents_work_key_copied_from_the_list_is_not_prefixed_again():
  from app.routes.owner_input_requests import _confined_spec

  principal = SimpleNamespace(app_agent=SimpleNamespace(app_id=7))
  once = _confined_spec(principal, "chat-1", {"kind": "approval", "work_key": "notes:publish"})
  again = _confined_spec(principal, "chat-1", dict(once))

  assert once["work_key"] == "app:7:chat:chat-1:notes:publish"
  assert again["work_key"] == once["work_key"]
  # Another chat's prefix is just text inside this chat's key.
  other = _confined_spec(principal, "chat-2", dict(once))
  assert other["work_key"] == "app:7:chat:chat-2:app:7:chat:chat-1:notes:publish"


# ---- No edit loses a concurrent change --------------------------------------

def _after_first_read(monkeypatch, competing_change):
  """Run ``competing_change`` in its own session right after the first read
  of the request an edit is about to write."""
  real_find_open = requests.find_open
  done = []

  def find_then_race(session, chat_id, ref):
    row = real_find_open(session, chat_id, ref)
    if not done:
      done.append(True)
      with SessionLocal() as competing:
        competing_change(competing, chat_id)
        competing.commit()
    return row

  monkeypatch.setattr(requests, "find_open", find_then_race)


def test_an_update_racing_another_update_keeps_both_changes(
  client, chat, run_auth, monkeypatch,
):
  inputs.add(client, chat.id, run_auth, "release-target", **CHOICE)
  _after_first_read(monkeypatch, lambda session, chat_id: requests.update_request(
    session, chat_id=chat_id, ref="release-target",
    spec={"prompt": "Staging or production, given the freeze?"}))

  updated = inputs.edit(client, chat.id, run_auth, op="update", key="release-target",
                        context="Production needs a sign-off.")

  assert updated.status_code == 200, updated.text
  stored = inputs.request_row("release-target")
  assert stored.spec_json["prompt"] == "Staging or production, given the freeze?"
  assert stored.spec_json["context"] == "Production needs a sign-off."
  assert (updated.json()["revision"], stored.revision) == (3, 3)


def _answer_on_the_card(session, chat_id):
  row = session.query(models.OwnerInputRequest).filter_by(
    chat_id=chat_id, key="release-target").one()
  row.status, row.answer_json, row.answered_by = "answered", {"text": "Staging"}, "card"
  row.answered_revision, row.updated_at = row.revision, timeutil.now_naive_utc()


@pytest.mark.parametrize("body", [
  pytest.param({"op": "update", "context": "Production needs a sign-off."}, id="update"),
  pytest.param({"op": "withdraw", "reason": "No longer needed."}, id="withdraw"),
  pytest.param({"op": "resolve", "answer": "Production", "source": "found",
                "reason": "deploy.yml targets production."}, id="resolve"),
  pytest.param({"op": "add", "prompt": "Staging or production now?"}, id="re-add"),
])
def test_an_edit_racing_an_owners_answer_never_overwrites_it(
  client, chat, run_auth, monkeypatch, body,
):
  """The owner's answer commits after the edit read the request and just
  before it writes (the edit stamps its time last)."""
  inputs.add(client, chat.id, run_auth, "release-target", **CHOICE)
  done = []

  def answer_lands_first():
    if not done:
      done.append(True)
      with SessionLocal() as competing:
        _answer_on_the_card(competing, chat.id)
        competing.commit()
    return timeutil.now_naive_utc()

  monkeypatch.setattr(requests, "now_naive_utc", answer_lands_first)

  refused = inputs.edit(client, chat.id, run_auth, key="release-target", **body)

  assert done and refused.status_code == 422, refused.text
  assert "no longer open: it was answered" in refused.text
  stored = inputs.request_row("release-target")
  assert (stored.status, stored.revision, stored.answer_json) == (
    "answered", 1, {"text": "Staging"})
  assert stored.spec_json["prompt"] == CHOICE["prompt"]


def test_an_edit_that_keeps_losing_its_race_is_refused_with_the_state_it_found(
  chat, db, monkeypatch,
):
  requests.add_request(db, chat_id=chat.id, run_id=None, goal_id=None,
                       key="release-target", spec=CHOICE)
  real_find_open = requests.find_open
  bumps = iter(range(2, 10))

  def find_then_bump(session, chat_id, ref):
    row = real_find_open(session, chat_id, ref)
    with SessionLocal() as competing:
      competing.query(models.OwnerInputRequest).filter_by(id=row.id).update(
        {"revision": next(bumps)})
      competing.commit()
    return row

  monkeypatch.setattr(requests, "find_open", find_then_bump)

  with pytest.raises(requests.RequestError, match="changed while this edit was applied"):
    requests.update_request(db, chat_id=chat.id, ref="release-target",
                            spec={"context": "Production needs a sign-off."})
  assert "sign-off" not in str(inputs.request_row("release-target").spec_json)
