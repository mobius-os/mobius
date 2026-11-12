"""Following switched on after a PR was sent, and the ask-before-push mode.

The grant stays the platform's own row: a first grant or another push mode
needs the owner's confirmation, an ask-mode round can prepare an update but
never push it, and ``prepared`` is recorded only when the record proves it.
"""
import json
from pathlib import Path

import pytest

from app import auth as auth_mod
from app import contribution_autopilot as autopilot
from app import models, owner_grants
from app.config import get_settings
from app.routes import github as publication
from app.routes.github import _limiter as _github_limiter
from app.storage_io import atomic_write
from tests.test_contribution_direct_merge import NAME, NUMBER, desk  # noqa: F401  (fixture and constants)
from tests.test_owner_grants import confirm, declare_owner_confirm

_github_limiter.enabled = False

HEAD = "a" * 40
NEXT_HEAD = "c" * 40
FOLLOW = {"enabled": True, "repo": NAME, "number": NUMBER}


def _record_path(app_id, record_id="rec1"):
  return Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions" / f"{record_id}.json"


def _write(app_id, record, record_id="rec1"):
  path = _record_path(app_id, record_id)
  path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(path, json.dumps(record))


def _sent_record(record_id="rec1"):
  """An open PR that was sent without following."""
  # Its own folder name: the data directory outlives one test.
  repo_path = Path(get_settings().data_dir) / "contributions" / f"follow-{record_id}" / "repo"
  (repo_path / ".git").mkdir(parents=True, exist_ok=True)
  return {
    "id": record_id, "type": "pr", "status": "open", "repo": NAME, "title": "Reviewed fix",
    "url": f"https://github.com/{NAME}/pull/{NUMBER}", "number": NUMBER,
    "head_repository": "octocat/project", "last_submit_push_sha": HEAD,
    "plan": {"action": "pr", "repo": NAME, "branch": "fix/x", "head_sha": HEAD,
             "diff_sha256": "b" * 64, "repo_path": str(repo_path)},
  }


def _toggle(desk, body, headers=None, record_id="rec1"):
  return desk.client.post(f"/api/github/contributions/{desk.app_id}/{record_id}/autopilot",
                          json=body, headers=headers or desk.owner)


def _app_headers(db, slug="contribute"):
  owner = db.query(models.Owner).first()
  app = db.query(models.App).filter_by(slug=slug).one()
  return {"Authorization": "Bearer " + auth_mod.create_app_token(
    app.id, owner.username, owner.token_epoch, app_nonce=app.token_nonce)}


def _agent_headers(db):
  owner = db.query(models.Owner).first()
  return {"Authorization": "Bearer " + auth_mod.create_agent_token(
    "desk-chat", owner.username, owner.token_epoch, run_id="desk-run")}


def _row(db, app_id, record_id="rec1"):
  db.expire_all()
  return autopilot.get_row(db, app_id, record_id)


def _grant(db, app_id, record, push_mode=None, record_id="rec1"):
  """The grant Send (or a confirmed follow) leaves for this record."""
  return autopilot.stamp_grant(db, app_id, record_id, push_mode=push_mode,
                               **publication._autopilot_grant_fields(record))


def _claim(db, app_id, record_id="rec1"):
  return autopilot.claim_for_round(db, app_id, record_id, attention_key="k1",
                                   event_at="2026-07-01T00:00:00Z")["run_id"]


# The grant row and its mode

def test_send_grants_auto_and_a_resend_cannot_widen_ask_before_push(fresh_db, db):
  assert autopilot.stamp_grant(db, 1, "rec", head_sha=HEAD).push_mode == "auto"
  autopilot.set_enabled(db, 1, "rec", True, push_mode="ask")
  assert autopilot.stamp_grant(db, 1, "rec", head_sha=NEXT_HEAD).push_mode == "ask"
  with pytest.raises(ValueError):
    autopilot.stamp_grant(db, 1, "rec", head_sha=HEAD, push_mode="silent")


def test_the_mirror_carries_the_push_mode(fresh_db, db):
  row = autopilot.stamp_grant(db, 1, "rec", head_sha=HEAD, push_mode="ask")
  assert autopilot.mirror_block(row)["push_mode"] == "ask"
  assert autopilot.mirror_block(autopilot.stamp_grant(db, 1, "other", head_sha=HEAD))["push_mode"] == "auto"


def test_changing_the_mode_revokes_the_round_started_under_the_old_one(fresh_db, db):
  autopilot.stamp_grant(db, 1, "rec", head_sha=HEAD, push_mode="ask")
  run_id = _claim(db, 1, "rec")
  autopilot.set_enabled(db, 1, "rec", True, push_mode="auto")
  assert not autopilot.verify_claim(autopilot.get_row(db, 1, "rec"), run_id)


# The prepared outcome

def _settle(db, outcome, *, mode, prepared_update, reply=False):
  record_id = f"{mode}-{outcome}-{prepared_update}-{reply}"
  autopilot.stamp_grant(db, 1, record_id, head_sha=HEAD, push_mode=mode)
  run_id = _claim(db, 1, record_id)
  if reply:
    assert autopilot.record_action(db, 1, record_id, run_id=run_id, action="replied")
  result = autopilot.complete_round(db, 1, record_id, run_id=run_id, outcome=outcome,
                                    summary="s", prepared_update=prepared_update)
  db.expire_all()
  return result, autopilot.get_row(db, 1, record_id)


def test_prepared_settles_an_ask_mode_round_only_with_proof(fresh_db, db):
  result, row = _settle(db, "prepared", mode="ask", prepared_update=True)
  assert result["productive"] is True and row.consecutive_failures == 0
  assert autopilot.mirror_block(row)["last_round"]["outcome"] == "prepared"
  assert row.last_handled_attention_key == "k1"
  assert row.granted_head_sha == HEAD  # nothing was published, so the public head did not move

  result, row = _settle(db, "prepared", mode="ask", prepared_update=False)
  assert result["productive"] is False and row.rounds_json[-1]["outcome"] == "failed"
  assert row.consecutive_failures == 1


def test_prepared_is_never_recorded_for_a_grant_that_pushes_by_itself(fresh_db, db):
  result, row = _settle(db, "prepared", mode="auto", prepared_update=True)
  assert result["productive"] is False and row.rounds_json[-1]["outcome"] == "failed"


def test_a_proven_prepared_update_outranks_a_reply_and_an_unproven_one_keeps_the_reply(fresh_db, db):
  _result, row = _settle(db, "prepared", mode="ask", prepared_update=True, reply=True)
  assert row.rounds_json[-1]["outcome"] == "prepared"
  _result, row = _settle(db, "prepared", mode="ask", prepared_update=False, reply=True)
  assert row.rounds_json[-1]["outcome"] == "replied"


def test_the_existing_outcomes_keep_their_rules_in_ask_mode(fresh_db, db):
  assert _settle(db, "handled", mode="ask", prepared_update=False)[1].rounds_json[-1]["outcome"] == "handled"
  assert _settle(db, "pushed", mode="ask", prepared_update=False)[1].rounds_json[-1]["outcome"] == "failed"


def test_complete_records_prepared_only_while_the_record_waits_as_a_prepared_update(desk, db):
  record = _sent_record()
  _write(desk.app_id, record)
  _grant(db, desk.app_id, record, "ask")
  complete = lambda run_id: desk.client.post(
    f"/api/github/contributions/{desk.app_id}/rec1/complete",
    json={"run_id": run_id, "outcome": "prepared", "summary": "Fix is ready."}, headers=desk.owner)

  # Still public: the agent only says it prepared something.
  assert complete(_claim(db, desk.app_id)).status_code == 200
  assert _row(db, desk.app_id).rounds_json[-1]["outcome"] == "failed"

  # Restaged at the head that is already public: nothing new waits for the owner.
  autopilot.set_enabled(db, desk.app_id, "rec1", True)
  staged = {**record, "status": "prepared", "plan": {**record["plan"], "action": "pr_update"}}
  _write(desk.app_id, staged)
  assert complete(_claim(db, desk.app_id)).status_code == 200
  assert _row(db, desk.app_id).rounds_json[-1]["outcome"] == "failed"

  autopilot.set_enabled(db, desk.app_id, "rec1", True)
  staged["plan"]["head_sha"] = NEXT_HEAD
  _write(desk.app_id, staged)
  assert complete(_claim(db, desk.app_id)).status_code == 200
  mirrored = json.loads(_record_path(desk.app_id).read_text())["autopilot"]
  assert mirrored["last_round"]["outcome"] == "prepared" and mirrored["push_mode"] == "ask"
  assert mirrored["state"] == "idle"


def test_a_prepared_update_of_another_pull_request_is_no_proof(desk, db):
  record = _sent_record()
  _grant(db, desk.app_id, record, "ask")
  moved = {**record, "status": "prepared", "number": NUMBER + 1, "url": "",
           "plan": {**record["plan"], "action": "pr_update", "head_sha": NEXT_HEAD}}
  _write(desk.app_id, moved)
  run_id = _claim(db, desk.app_id)
  response = desk.client.post(f"/api/github/contributions/{desk.app_id}/rec1/complete",
                              json={"run_id": run_id, "outcome": "prepared"}, headers=desk.owner)
  assert response.status_code == 200
  assert _row(db, desk.app_id).rounds_json[-1]["outcome"] == "failed"


# An ask-mode round cannot push

def test_update_is_refused_in_ask_mode_before_any_push_work(desk, db, monkeypatch):
  record = _sent_record()
  _write(desk.app_id, record)
  _grant(db, desk.app_id, record, "ask")
  run_id = _claim(db, desk.app_id)
  monkeypatch.setattr(publication, "_autopilot_update_action_input",
                      lambda *_args: pytest.fail("push work started in ask mode"))
  refused = desk.client.post(f"/api/github/contributions/{desk.app_id}/rec1/update",
                             json={"run_id": run_id, "head_sha": HEAD, "diff_sha256": "b" * 64},
                             headers=desk.owner)
  assert refused.status_code == 409 and refused.json()["detail"] == autopilot.ASK_MODE_REFUSAL
  assert _row(db, desk.app_id).round_action is None


def test_update_is_unchanged_for_a_grant_that_pushes_by_itself(desk, db):
  record = _sent_record()
  record["plan"]["base_sha"] = "e" * 40
  _write(desk.app_id, record)
  _grant(db, desk.app_id, record)
  run_id = _claim(db, desk.app_id)
  response = desk.client.post(f"/api/github/contributions/{desk.app_id}/rec1/update",
                              json={"run_id": run_id, "head_sha": NEXT_HEAD, "diff_sha256": "d" * 64},
                              headers=desk.owner)
  # It reached the reviewed-state check of the push path, as before.
  assert response.status_code == 409 and response.json()["detail"] != autopilot.ASK_MODE_REFUSAL


def test_an_ask_mode_round_cannot_restage_in_place(desk, db):
  record = _sent_record()
  _write(desk.app_id, record)
  _grant(db, desk.app_id, record, "ask")
  run_id = _claim(db, desk.app_id)
  refused = desk.client.post(f"/api/github/contributions/{desk.app_id}/rec1/stage",
                             json={"autopilot_run_id": run_id}, headers=_agent_headers(db))
  assert refused.status_code == 409 and refused.json()["detail"] == autopilot.ASK_MODE_REFUSAL
  assert json.loads(_record_path(desk.app_id).read_text()) == record


# The round's brief

def _brief(db, mode):
  row = autopilot.stamp_grant(db, 1, f"brief-{mode}", head_sha=HEAD, target_repo=NAME,
                              target_pr_number=NUMBER, push_mode=mode)
  return publication._autopilot_round_brief(1, row.record_id, row, {"key": "k1"}, "run-1")


def test_the_ask_mode_brief_prepares_an_update_and_never_pushes(fresh_db, db):
  brief = _brief(db, "ask")
  base = "/api/github/contributions/1/brief-ask"
  assert f"do not call POST {base}/update" in brief
  assert f"POST {base}/update   " not in brief  # not offered as an action
  assert f"POST {base}/stage with an empty JSON body (no `autopilot_run_id`)" in brief
  assert "Update PR press" in brief
  assert f"POST {base}/review" in brief
  assert 'outcome: "prepared"' in brief
  assert "reply only to a point that needs no code change" in brief
  assert "Run id (present this on every autopilot call): run-1" in brief


def test_the_auto_brief_still_offers_the_push(fresh_db, db):
  brief = _brief(db, "auto")
  assert "POST /api/github/contributions/1/brief-auto/update   — push a validated fix" in brief
  assert "prepared" not in brief and "ask-before-push" not in brief


# Following switched on after Send

def test_the_owner_switches_following_on_for_a_sent_pull_request(desk, db):
  record = _sent_record()
  _write(desk.app_id, record)
  response = _toggle(desk, {**FOLLOW, "push_mode": "ask"})
  assert response.status_code == 200, response.text
  assert response.json() == {"status": "ok", "enabled": True, "push_mode": "ask"}
  row = _row(db, desk.app_id)
  assert (row.enabled, row.state, row.push_mode, row.granted_head_sha) == (True, "idle", "ask", HEAD)
  assert (row.target_repo, row.target_pr_number, row.target_head_repository, row.target_branch) == (
    NAME, NUMBER, "octocat/project", "fix/x")
  assert row.target_repo_path == str(Path(record["plan"]["repo_path"]).resolve())
  # The grant is exactly the one the agent routes later hold the record to.
  publication._autopilot_assert_bound_target(row, record)
  assert json.loads(_record_path(desk.app_id).read_text())["autopilot"]["push_mode"] == "ask"


def test_following_without_a_mode_pushes_by_itself_as_send_does(desk, db):
  _write(desk.app_id, _sent_record())
  assert _toggle(desk, FOLLOW).json()["push_mode"] == "auto"
  assert _row(db, desk.app_id).push_mode == "auto"


def _changed(change):
  record = _sent_record()
  change(record)
  return record


@pytest.mark.parametrize("change,status,words", [
  (lambda r: r.update(type="issue"), 409, "only for an open pull request"),
  (lambda r: r.update(status="prepared"), 409, "only for an open pull request"),
  (lambda r: r.update(status="merged"), 409, "only for an open pull request"),
  (lambda r: r["plan"].update(stack={"parent_record_id": "p"}), 409, "part of a stack"),
  (lambda r: r.update(number=None, url=""), 409, "does not record its pull request number"),
  (lambda r: r.pop("head_repository"), 409, "does not record its pull request number"),
  (lambda r: r["plan"].pop("branch"), 422, ""),
  (lambda r: r["plan"].update(repo_path="/etc"), 422, ""),
  (lambda r: r["plan"].pop("repo_path"), 422, ""),
])
def test_a_record_that_cannot_be_followed_is_refused_and_gets_no_grant(desk, db, change, status, words):
  _write(desk.app_id, _changed(change))
  refused = _toggle(desk, FOLLOW)
  assert refused.status_code == status, refused.text
  assert words in str(refused.json()["detail"])
  assert _row(db, desk.app_id) is None


@pytest.mark.parametrize("named", [{"number": NUMBER + 1}, {"repo": "example/other"}, {"repo": None, "number": None}])
def test_the_grant_goes_only_to_the_pull_request_the_request_names(desk, db, named):
  _write(desk.app_id, _sent_record())
  refused = _toggle(desk, {**FOLLOW, **named})
  assert refused.status_code == 409 and "pull request named" in refused.json()["detail"]
  assert _row(db, desk.app_id) is None


def test_a_missing_record_gets_no_grant(desk, db):
  assert _toggle(desk, FOLLOW).status_code == 404
  assert _row(db, desk.app_id) is None


# The owner's confirmation

def test_an_app_that_declares_owner_confirm_starts_following_only_with_the_owners_grant(desk, db):
  _write(desk.app_id, _sent_record())
  declare_owner_confirm(db, "contribute")
  app = _app_headers(db)
  body = {**FOLLOW, "push_mode": "ask"}
  refused = _toggle(desk, body, app)
  assert refused.status_code == 403 and refused.json()["detail"] == owner_grants.OWNER_GRANT_REQUIRED
  assert _row(db, desk.app_id) is None

  assert confirm(desk, "follow", body, record_id="rec1").status_code == 200
  assert _toggle(desk, {**body, "push_mode": "auto"}, app).status_code == 403  # another mode is not covered
  assert _toggle(desk, body, app, record_id="rec2").status_code == 403  # nor another record
  assert _row(db, desk.app_id) is None
  assert _toggle(desk, body, app).status_code == 200
  assert _row(db, desk.app_id).push_mode == "ask"
  assert db.query(models.OwnerGrant).filter(models.OwnerGrant.consumed_at.is_(None)).count() == 0


def _agent_minted_app_headers(desk, db):
  """An app token an agent minted: it carries the agent's chat, so it is not the owner's tap."""
  minted = desk.client.post("/api/auth/app-token", json={"app_id": desk.app_id}, headers=_agent_headers(db))
  return {"Authorization": f"Bearer {minted.json()['token']}"}


@pytest.mark.parametrize("who", ["agent", "app_token_an_agent_minted"])
def test_an_agent_can_neither_start_following_nor_change_the_mode(desk, db, who):
  # Contribute has not declared owner_confirm here: the route's own guard must refuse an agent.
  record = _sent_record()
  _write(desk.app_id, record)
  agent = _agent_headers(db) if who == "agent" else _agent_minted_app_headers(desk, db)
  refused = _toggle(desk, FOLLOW, agent)
  assert refused.status_code == 403 and "An agent cannot" in refused.json()["detail"]
  assert _row(db, desk.app_id) is None
  _grant(db, desk.app_id, record, "ask")
  assert _toggle(desk, {**FOLLOW, "push_mode": "auto"}, agent).status_code == 403
  assert _row(db, desk.app_id).push_mode == "ask"
  # Pause and Resume keep today's rule for the same callers.
  assert _toggle(desk, {"enabled": False}, agent).status_code == 200
  assert _toggle(desk, {"enabled": True}, agent).json()["push_mode"] == "ask"


def test_starting_to_follow_leaves_the_records_attention_alone_and_resume_clears_it(desk, db):
  record = {**_sent_record(), "needs_attention": True, "attention": {"key": "review:1"}}
  _write(desk.app_id, record)
  assert _toggle(desk, FOLLOW).status_code == 200
  assert json.loads(_record_path(desk.app_id).read_text())["attention"] == {"key": "review:1"}
  assert _toggle(desk, {"enabled": True}).status_code == 200
  assert json.loads(_record_path(desk.app_id).read_text())["needs_attention"] is False


def test_pause_and_resume_of_an_existing_grant_need_no_confirmation(desk, db):
  record = _sent_record()
  _write(desk.app_id, record)
  declare_owner_confirm(db, "contribute")
  app = _app_headers(db)
  assert _toggle(desk, {"enabled": False}, app).status_code == 404  # no grant yet, as before
  _grant(db, desk.app_id, record, "ask")
  paused = _toggle(desk, {"enabled": False}, app)
  assert paused.status_code == 200 and paused.json()["enabled"] is False
  for resume in ({"enabled": True}, {"enabled": True, "push_mode": "ask"}):
    resumed = _toggle(desk, resume, app)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json() == {"status": "ok", "enabled": True, "push_mode": "ask"}
  assert db.query(models.OwnerGrant).count() == 0


def test_changing_the_mode_of_an_existing_grant_needs_the_owners_grant(desk, db):
  record = _sent_record()
  _write(desk.app_id, record)
  declare_owner_confirm(db, "contribute")
  app = _app_headers(db)
  _grant(db, desk.app_id, record, "ask")
  body = {**FOLLOW, "push_mode": "auto"}
  assert _toggle(desk, body, app).status_code == 403
  assert _row(db, desk.app_id).push_mode == "ask"
  assert confirm(desk, "follow", body, record_id="rec1").status_code == 200
  assert _toggle(desk, body, app).json() == {"status": "ok", "enabled": True, "push_mode": "auto"}
  assert _row(db, desk.app_id).push_mode == "auto"


def test_a_mode_change_names_the_grants_own_pull_request_not_the_ledgers(desk, db):
  record = _sent_record()
  _grant(db, desk.app_id, record, "ask")
  _write(desk.app_id, {**record, "number": NUMBER + 1, "url": ""})  # the ledger moved; the grant did not
  assert _toggle(desk, {**FOLLOW, "number": NUMBER + 1, "push_mode": "auto"}).status_code == 409
  assert _toggle(desk, {**FOLLOW, "push_mode": "auto"}).status_code == 200
  assert _row(db, desk.app_id).target_pr_number == NUMBER


def test_a_grant_that_records_no_pull_request_cannot_change_its_mode(desk, db):
  autopilot.stamp_grant(db, desk.app_id, "rec1", head_sha=HEAD)
  refused = _toggle(desk, {"enabled": True, "push_mode": "ask"})
  assert refused.status_code == 409 and _row(db, desk.app_id).push_mode == "auto"


# The sheet's words

def _preview(desk, body, record_id="rec1"):
  return desk.client.post("/api/owner-grants/preview", headers=desk.owner, json={
    "app_id": desk.app_id, "action": "follow", "scope": {"record_id": record_id}, "body": body})


def test_the_sheet_names_the_pull_request_and_what_each_mode_pushes(desk, db):
  from app.routes import contribution_owner_grants as grants
  record = _sent_record()
  _write(desk.app_id, record)
  desk.github.pull["user"] = {"login": "octocat"}
  sheet = _preview(desk, {**FOLLOW, "push_mode": "ask"}).json()
  facts = {fact["label"]: fact["value"] for fact in sheet["facts"]}
  assert (sheet["headline"], sheet["verb"], sheet["tone"]) == (f"Follow {NAME}#{NUMBER}", "Follow", "caution")
  assert facts["Pull request"] == f"{NAME}#{NUMBER}: A change"  # GitHub's title, not the ledger's
  assert (facts["Author"], facts["Branch"]) == ("octocat", "fix/x")
  assert facts["Pushes"] == grants.FOLLOW_MODES["ask"] and "Update PR" in facts["Pushes"]
  assert _preview(desk, FOLLOW).json()["facts"][3]["value"] == grants.FOLLOW_MODES["auto"]

  _grant(db, desk.app_id, record, "ask")
  change = _preview(desk, {**FOLLOW, "push_mode": "auto"}).json()
  assert (change["headline"], change["verb"]) == (f"Change how {NAME}#{NUMBER} is followed", "Change")
  assert _row(db, desk.app_id).push_mode == "ask"  # a preview changes nothing


def test_nothing_is_confirmed_for_a_change_that_needs_no_sheet_or_a_closed_pull_request(desk, db):
  record = _sent_record()
  _write(desk.app_id, record)
  desk.github.pull["state"] = "closed"
  assert _preview(desk, FOLLOW).status_code == 409
  desk.github.pull["state"] = "open"
  assert _preview(desk, {**FOLLOW, "number": NUMBER + 1}).status_code == 409
  assert _preview(desk, {"enabled": False}).status_code == 409
  _grant(db, desk.app_id, record, "ask")
  assert _preview(desk, {**FOLLOW, "push_mode": "ask"}).status_code == 409  # a plain Resume
  assert _preview(desk, FOLLOW, record_id="rec1").status_code == 409
  assert db.query(models.OwnerGrant).count() == 0


# What the app is told

def test_both_capabilities_are_advertised(desk):
  for path in (f"/api/github/contributions/{desk.app_id}/review-presets", "/api/github/review-presets"):
    capabilities = desk.client.get(path, headers=desk.owner).json()["capabilities"]
    assert capabilities["follow_after_send"] is True and capabilities["follow_ask_mode"] is True
