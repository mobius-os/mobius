"""Stage-1 integration test: the whole autopilot loop through real HTTP.

Unit coverage lives in test_contribution_autopilot.py (state machine + trust).
This drives the loop END TO END across the real endpoints — submit stamps the
grant, /respond claims + creates the dedicated chat + writes the ledger mirror,
/update runs the submit push path, /reply, /complete advances the cursor, and a
second /respond proves the self-re-trigger guard.

git/gh are stubbed at the command boundary exactly like the existing submit tests
(_submit_preflight_response + a fake _git/_gh), and the background turn spawn is
stubbed so no real agent runs. The REAL git push + review detection are proven
separately by the live Stage-2 harness (scripts/autopilot-live-check.sh).
"""

import hashlib
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from app import contribution_autopilot as autopilot
from app import github_auth, models
from app.config import get_settings
from app.database import SessionLocal
from app.timeutil import now_naive_utc

from app.routes import github as github_routes
from app.routes.github import _limiter as _github_limiter

# Reuse the proven submit-flow fakes + fixtures-worth of helpers.
from tests.test_github_routes import (
  _allow_synthetic_source_provenance,
  _app_token,
  _commit_metadata,
  _cp,
  _submit_preflight_response,
  _write_contribution,
  _write_token,
)

_github_limiter.enabled = False


@pytest.fixture(autouse=True)
def _github_state():
  """Mirror test_github_routes' credential-dir reset (its fixture is module-scoped)."""
  github_auth.set_device_flow(None)
  shutil.rmtree(github_auth.GH_AUTH_DIR, ignore_errors=True)
  get_settings.cache_clear()
  yield
  github_auth.set_device_flow(None)
  shutil.rmtree(github_auth.GH_AUTH_DIR, ignore_errors=True)
  get_settings.cache_clear()


_BASE = "b" * 40
_HEAD1 = "a" * 40
_HEAD2 = "c" * 40
_BRANCH = "fix/demo-polish"
_DIFF1 = (
  "diff --git a/index.jsx b/index.jsx\n"
  "--- a/index.jsx\n+++ b/index.jsx\n@@ -1 +1 @@\n-old\n+one\n"
)
_DIFF2 = (
  "diff --git a/index.jsx b/index.jsx\n"
  "--- a/index.jsx\n+++ b/index.jsx\n@@ -1 +1 @@\n-old\n+two\n"
)


def _record(record_id, repo_path):
  return {
    "id": record_id, "type": "pr", "repo": "mobius-os/app-demo",
    "status": "prepared", "title": "Polish demo", "branch": _BRANCH,
    "created_at": "2026-07-09T00:00:00Z", "updated_at": "2026-07-09T00:00:00Z",
    "plan": {
      "action": "pr", "repo": "mobius-os/app-demo", "title": "Polish demo",
      "body_draft": "## What\n\nPolishes the demo.", "branch": _BRANCH,
      "repo_path": str(repo_path), "base_sha": _BASE, "head_sha": _HEAD1,
      "diff_sha256": hashlib.sha256(_DIFF1.encode()).hexdigest(),
      "labels": ["bug"],
    },
    "quality_review": {
      "state": "all_clear",
      "reviewed_head_sha": _HEAD1,
      "reviewed_at": "2026-07-09T00:00:00Z",
    },
  }


def _mark_reviewed_update(record):
  """Model the exact public text witness produced by a refreshed PR review."""
  plan = record["plan"]
  plan["action"] = "pr_update"
  plan["pr_metadata"] = {
    "old_title": plan["title"],
    "old_body": plan["body_draft"],
  }
  return record


def _make_fakes(state):
  """A head-agnostic fake _git/_gh keyed on the mutable `state` (head/diff), so
  the same fakes serve the initial submit (head1/diff1) and the /update
  (head2/diff2)."""
  def fake_git(repo_path, *args, check=True):
    state["git_calls"].append(args)
    pre = _submit_preflight_response(args)
    if pre is not None:
      return pre
    head, diff_text = state["head"], state["diff_text"]
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("develop\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", _BRANCH):
      return _cp(head + "\n")
    if args == ("rev-parse", "--verify", f"{_BASE}^{{commit}}"):
      return _cp(_BASE + "\n")
    if args == ("rev-parse", "--verify", f"{head}^{{commit}}"):
      return _cp(head + "\n")
    if args == (
      "-c", "core.quotePath=false", "diff", "--no-ext-diff", "--no-color",
      "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/",
      f"{_BASE}..{head}",
    ):
      return _cp(diff_text)
    if args == (
      "-c", "core.quotePath=false", "diff", "--name-only", "-z",
      f"{_BASE}..{head}",
    ):
      return _cp("index.jsx\0")
    if args == ("log", "-1", "--format=%B", _BRANCH):
      return _cp(
        "Polish demo\n\n"
        "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n"
      )
    if args[:3] == ("show", "-s", "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI"):
      return _commit_metadata(head)
    if args == ("remote", "get-url", "origin"):
      return _cp("https://github.com/mobius-os/app-demo.git\n")
    if args == ("remote", "get-url", "fork"):
      return (
        _cp("https://github.com/octocat/app-demo-1.git\n")
        if state["fork_ready"] else _cp(returncode=1)
      )
    return _cp("")

  def fake_gh(repo_path, *args, check=True):
    state["gh_calls"].append(args)
    if args[:2] == ("api", "repos/mobius-os/app-demo/pulls/42"):
      return _cp(json.dumps({
        "state": "open",
        "draft": False,
        "title": "Polish demo",
        "body": "## What\n\nPolishes the demo.",
        "html_url": "https://github.com/mobius-os/app-demo/pull/42",
        "head": {
          "repo": {"full_name": "octocat/app-demo-1"},
          "ref": _BRANCH,
          "sha": state["head"],
        },
        "base": {"ref": "main"},
      }))
    if args[:2] == ("repo", "fork"):
      state["fork_ready"] = True
      return _cp("")
    if args[:2] == ("pr", "list"):
      if state.get("pr_open"):
        return _cp(json.dumps([{
          "url": "https://github.com/mobius-os/app-demo/pull/42",
          "headRefName": _BRANCH,
          "headRefOid": state["head"],
          "headRepositoryOwner": {"login": "octocat"},
        }]))
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      state["pr_open"] = True
      return _cp("https://github.com/mobius-os/app-demo/pull/42\n")
    return _cp("")

  return fake_git, fake_gh


def _install_fakes(monkeypatch, state):
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
  fake_git, fake_gh = _make_fakes(state)
  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  # Autopilot's reviewed changed-path read remains at the route trust boundary.
  monkeypatch.setattr("app.routes.github._git", fake_git)


async def _fake_spawn(*args, **kwargs):
  return True


def _record_path(app_id, record_id):
  return (
    Path(get_settings().data_dir) / "apps" / str(app_id) /
    "contributions" / f"{record_id}.json"
  )


def _read(app_id, record_id):
  return json.loads(_record_path(app_id, record_id).read_text())


def test_full_autopilot_loop_end_to_end(client, owner_token, monkeypatch):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  headers = {"Authorization": f"Bearer {app_token}"}
  agent_headers = {"Authorization": f"Bearer {owner_token}"}
  record_id = "rec-loop"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  _write_contribution(app_id, record_id, _record(record_id, repo), _DIFF1)

  state = {
    "head": _HEAD1, "diff_text": _DIFF1, "fork_ready": False,
    "git_calls": [], "gh_calls": [],
  }
  _install_fakes(monkeypatch, state)
  # This end-to-end state-machine test uses synthetic commit ids and fake Git
  # transport. Real installed-source rejection is covered by the route test.
  _allow_synthetic_source_provenance(monkeypatch)
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: state["head"],
  )
  # No real agent turn — assert only claim/chat/mirror wiring.
  monkeypatch.setattr(autopilot, "spawn_round_turn", _fake_spawn)
  # /reply shells out to `gh` directly (not the monkeypatched _gh); stub the
  # server-side post so no real GitHub call happens. The live Stage-2 harness
  # exercises the real reply.
  monkeypatch.setattr(
    github_routes, "_autopilot_post_reply",
    lambda *a, **k: {"ok": True},
  )
  monkeypatch.setattr(
    github_routes, "_autopilot_live_target",
    lambda *a, **k: {
      "error": None,
      "head_sha": state["head"],
      "base_branch": "main",
      "base_sha": _BASE,
      "title": "Polish demo",
      "body": "## What\n\nPolishes the demo.",
    },
  )

  # 1. Submit with autopilot → PR opened + grant stamped + mirror written.
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    # The end-to-end loop exercises a review-ready PR. Draft is now the safe
    # compatibility default, so this test must opt into the public ready stage
    # instead of relying on the retired implicit-open behavior.
    json={"autopilot": True, "publication_stage": "ready"}, headers=headers,
  )
  assert r.status_code == 200, r.text
  assert r.json()["record"]["status"] == "open"
  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    assert row is not None and row.enabled and row.state == "idle"
  finally:
    db.close()
  assert _read(app_id, record_id).get("autopilot", {}).get("enabled") is True

  # 2. Respond to a review → claim + dedicated chat + mirror responding.
  attention = {"key": "changes_requested:1", "type": "changes_requested",
               "event_at": "2026-07-10T00:00:00Z"}
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/respond",
    json={"attention": attention}, headers=headers,
  )
  assert r.status_code == 200, r.text
  assert r.json()["status"] == "responding"
  run_id = r.json()["run_id"]
  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    assert row.state == "responding" and row.run_id == run_id
    assert row.followup_chat_id
    chat = db.query(models.Chat).filter(
      models.Chat.id == row.followup_chat_id).first()
    assert chat is not None and chat.title.startswith("Autopilot:")
  finally:
    db.close()
  assert _read(app_id, record_id)["autopilot"]["state"] == "responding"

  # 3. Agent pushes a fix: advance the branch + rewrite the reviewed diff, then
  # /update runs the submit push path (stubbed) and updates the record.
  rec = _read(app_id, record_id)
  rec["plan"]["head_sha"] = _HEAD2
  rec["plan"]["diff_sha256"] = hashlib.sha256(_DIFF2.encode()).hexdigest()
  _mark_reviewed_update(rec)
  rec["needs_attention"] = True
  rec["attention"] = attention
  _write_contribution(app_id, record_id, rec, _DIFF2)
  state["head"] = _HEAD2
  state["diff_text"] = _DIFF2
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update",
    json={"run_id": run_id, "head_sha": _HEAD2,
          "diff_sha256": hashlib.sha256(_DIFF2.encode()).hexdigest()},
    headers=agent_headers,
  )
  assert r.status_code == 200, r.text

  # 4. Reply + complete → idle, one round, cursor advanced.
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/reply",
    json={"run_id": run_id, "body": "Addressed the review."},
    headers=agent_headers,
  )
  assert r.status_code == 200, r.text
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/complete",
    json={"run_id": run_id, "outcome": "pushed",
          "summary": "Fixed the reordering."}, headers=agent_headers,
  )
  assert r.status_code == 200, r.text
  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    assert row.state == "idle" and row.rounds_used == 1
    assert row.last_handled_event_at == "2026-07-10T00:00:00.000000Z"
  finally:
    db.close()
  mirror = _read(app_id, record_id)["autopilot"]
  assert mirror["state"] == "idle" and mirror["rounds_used"] == 1
  assert mirror["last_round"]["outcome"] == "pushed"
  assert _read(app_id, record_id)["needs_attention"] is False
  assert _read(app_id, record_id)["attention"] is None

  # 5. The same event must NOT re-trigger (cursor guards the self-reply loop).
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/respond",
    json={"attention": attention}, headers=headers,
  )
  assert r.status_code == 409  # duplicate


@pytest.mark.parametrize(
  "death_phase", ["armed", "normalizing", "push_pending", "branch_published"],
)
def test_expired_new_round_resumes_exact_action_after_each_pre_settlement_death(
  client, owner_token, monkeypatch, death_phase,
):
  """Lease expiry cannot strand an exact signed action at any durable phase."""
  class HardDeath(BaseException):
    pass

  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  app_headers = {"Authorization": f"Bearer {app_token}"}
  agent_headers = {"Authorization": f"Bearer {owner_token}"}
  record_id = f"rec-hard-death-{death_phase}"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  record = _record(record_id, repo)
  record.update({
    "status": "open",
    "number": 42,
    "url": "https://github.com/mobius-os/app-demo/pull/42",
    "head_repository": "octocat/app-demo-1",
  })
  record["plan"]["head_sha"] = _HEAD2
  record["plan"]["diff_sha256"] = hashlib.sha256(_DIFF2.encode()).hexdigest()
  _mark_reviewed_update(record)
  _write_contribution(app_id, record_id, record, _DIFF2)
  state = {
    "head": _HEAD2,
    "diff_text": _DIFF2,
    "fork_ready": True,
    "git_calls": [],
    "gh_calls": [],
    "public_head": _HEAD1,
  }
  _install_fakes(monkeypatch, state)
  _allow_synthetic_source_provenance(monkeypatch)
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": state["public_head"],
      "base_branch": "main",
      "base_sha": _BASE,
      "title": record["plan"]["title"],
      "body": record["plan"]["body_draft"],
    },
  )

  db = SessionLocal()
  try:
    autopilot.stamp_grant(
      db,
      app_id,
      record_id,
      head_sha=_HEAD1,
      target_repo=record["repo"],
      target_pr_number=42,
      target_head_repository=record["head_repository"],
      target_branch=record["branch"],
      target_repo_path=str(repo.resolve()),
    )
    first = autopilot.claim_for_round(
      db,
      app_id,
      record_id,
      attention_key="hard-death:first",
      event_at="2026-07-10T00:00:00Z",
    )
    first_run = first["run_id"]
  finally:
    db.close()

  attempts = []

  original_arm_claim = github_routes._PersonalAttemptOwner.arm_claim
  die_during_arm = {"now": death_phase == "armed"}

  def arm_then_die(self, *args, **kwargs):
    original_arm_claim(self, *args, **kwargs)
    if die_during_arm["now"]:
      die_during_arm["now"] = False
      raise HardDeath()

  monkeypatch.setattr(
    github_routes._PersonalAttemptOwner, "arm_claim", arm_then_die,
  )

  def die_after_publication(_record, _diff_path, **kwargs):
    attempts.append("attempted")
    patch = ({
      "last_submit_stage": (
        "push_pending" if death_phase == "push_pending" else "pushed"
      ),
      "last_submit_push_sha": _HEAD2,
      "head_repository": record["head_repository"],
    } if death_phase in {"push_pending", "branch_published"} else {})
    kwargs["attempt_event"](
      death_phase,
      {
        "action": death_phase,
        "repo": record["repo"],
        "head_repository": record["head_repository"],
        "branch": record["branch"],
        "head_sha": _HEAD2,
        "base_branch": "main",
        "expected_remote_sha": _HEAD1,
      },
      patch,
    )
    if death_phase == "branch_published":
      state["public_head"] = _HEAD2
    raise HardDeath()

  if death_phase != "armed":
    monkeypatch.setattr(
      github_routes, "_submit_prepared_pr", die_after_publication,
    )
  request_body = {
    "run_id": first_run,
    "head_sha": _HEAD2,
    "diff_sha256": record["plan"]["diff_sha256"],
  }
  with pytest.raises(HardDeath):
    client.post(
      f"/api/github/contributions/{app_id}/{record_id}/update",
      json=request_body,
      headers=agent_headers,
    )
  receipt = github_routes._read_personal_attempt(
    _record_path(app_id, record_id), app_id=app_id, record_id=record_id,
  )
  assert receipt is not None and receipt["phase"] == death_phase

  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    row.lease_expires_at = now_naive_utc() - timedelta(minutes=1)
    db.commit()
  finally:
    db.close()
  second = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/respond",
    json={
      "attention": {
        "key": "hard-death:second",
        "event_at": "2026-07-11T00:00:00Z",
      },
    },
    headers=app_headers,
  )
  assert second.status_code == 200, second.text
  second_run = second.json()["run_id"]
  assert second_run != first_run
  def resume_exact_action(_record, _diff_path, **kwargs):
    attempts.append("resumed")
    assert kwargs["prior_attempt_phase"] == death_phase
    assert kwargs["expected_existing_head_sha"] == state["public_head"]
    return (
      record["url"],
      record["number"],
      {
        "last_submit_stage": "pushed",
        "last_submit_push_sha": _HEAD2,
        "head_repository": record["head_repository"],
        "publication_stage": "draft",
      },
    )

  monkeypatch.setattr(
    github_routes, "_submit_prepared_pr", resume_exact_action,
  )

  recovered = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update",
    json={**request_body, "run_id": second_run},
    headers=agent_headers,
  )

  assert recovered.status_code == 200, recovered.text
  assert recovered.json()["number"] == 42
  assert attempts == (
    ["resumed"] if death_phase == "armed" else ["attempted", "resumed"]
  )
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record_id,
  ).exists()
  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    assert row.run_id == second_run
    assert row.round_action == "pushed"
    assert row.round_head_sha == _HEAD2
  finally:
    db.close()


@pytest.mark.parametrize(
  ("record_failure", "remote_after_failure"),
  [
    ("false", "exact"),
    ("exception", "exact"),
    ("hard_death", "exact"),
    ("hard_death", "reset"),
  ],
)
def test_complete_update_receipt_survives_db_failure_and_new_round(
  client, owner_token, monkeypatch, record_failure, remote_after_failure,
):
  """A terminal public receipt outlives DB failure and prevents a second push."""
  class HardDeath(BaseException):
    pass

  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  app_headers = {"Authorization": f"Bearer {app_token}"}
  agent_headers = {"Authorization": f"Bearer {owner_token}"}
  record_id = f"rec-complete-db-{record_failure}-{remote_after_failure}"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  record = _record(record_id, repo)
  record.update({
    "status": "open",
    "number": 42,
    "url": "https://github.com/mobius-os/app-demo/pull/42",
    "head_repository": "octocat/app-demo-1",
  })
  record["plan"]["head_sha"] = _HEAD2
  record["plan"]["diff_sha256"] = hashlib.sha256(_DIFF2.encode()).hexdigest()
  _mark_reviewed_update(record)
  _write_contribution(app_id, record_id, record, _DIFF2)
  state = {
    "head": _HEAD2,
    "diff_text": _DIFF2,
    "fork_ready": True,
    "git_calls": [],
    "gh_calls": [],
    "public_head": _HEAD1,
  }
  _install_fakes(monkeypatch, state)
  monkeypatch.setattr(autopilot, "spawn_round_turn", _fake_spawn)
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": state["public_head"],
      "base_branch": "main",
      "base_sha": _BASE,
      "title": record["plan"]["title"],
      "body": record["plan"]["body_draft"],
    },
  )
  source_checks = []
  monkeypatch.setattr(
    github_routes,
    "_assert_personal_publication_source",
    lambda *_args: source_checks.append("checked"),
  )

  async def record_equivalence(*_args, **_kwargs):
    return None

  monkeypatch.setattr(
    github_routes, "_record_pending_equivalence_locked", record_equivalence,
  )
  publication_calls = []

  def complete_publication(_record, _diff_path, **kwargs):
    publication_calls.append("pushed")
    state["public_head"] = _HEAD2
    patch = {
      "last_submit_stage": "pushed",
      "last_submit_push_sha": _HEAD2,
      "head_repository": record["head_repository"],
      "publication_stage": "draft",
    }
    kwargs["attempt_event"]("complete", {
      "action": "update_pr",
      "repo": record["repo"],
      "number": record["number"],
      "url": record["url"],
      "head_repository": record["head_repository"],
      "branch": record["branch"],
      "head_sha": _HEAD2,
      "base_branch": "main",
    }, patch)
    return record["url"], record["number"], patch

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", complete_publication)
  real_record_action = autopilot.record_action
  first_record = {"pending": True}

  def fail_record_action_once(*args, **kwargs):
    if first_record.pop("pending", False):
      if record_failure == "false":
        return False
      if record_failure == "exception":
        raise RuntimeError("DB action write failed")
      raise HardDeath()
    return real_record_action(*args, **kwargs)

  monkeypatch.setattr(autopilot, "record_action", fail_record_action_once)
  db = SessionLocal()
  try:
    autopilot.stamp_grant(
      db,
      app_id,
      record_id,
      head_sha=_HEAD1,
      target_repo=record["repo"],
      target_pr_number=record["number"],
      target_head_repository=record["head_repository"],
      target_branch=record["branch"],
      target_repo_path=str(repo.resolve()),
    )
    first = autopilot.claim_for_round(
      db,
      app_id,
      record_id,
      attention_key="complete-db:first",
      event_at="2026-07-10T00:00:00Z",
    )
    first_run = first["run_id"]
  finally:
    db.close()

  request_body = {
    "run_id": first_run,
    "head_sha": _HEAD2,
    "diff_sha256": record["plan"]["diff_sha256"],
  }
  if record_failure == "false":
    failed = client.post(
      f"/api/github/contributions/{app_id}/{record_id}/update",
      json=request_body,
      headers=agent_headers,
    )
    assert failed.status_code == 409, failed.text
  else:
    failure = RuntimeError if record_failure == "exception" else HardDeath
    with pytest.raises(failure):
      client.post(
        f"/api/github/contributions/{app_id}/{record_id}/update",
        json=request_body,
        headers=agent_headers,
      )

  receipt = github_routes._read_personal_attempt(
    _record_path(app_id, record_id), app_id=app_id, record_id=record_id,
  )
  assert receipt is not None and receipt["phase"] == "complete"
  assert _read(app_id, record_id)["last_submit_push_sha"] == _HEAD2
  if remote_after_failure == "exact":
    monkeypatch.setattr(
      github_routes,
      "_assert_personal_publication_source",
      lambda *_args: pytest.fail(
        "an exact complete receipt must not depend on the now-reverted source"
      ),
    )
  else:
    state["public_head"] = _HEAD1

    def reject_reverted_source(*_args):
      source_checks.append("rechecked")
      raise github_routes.ContributionSubmitError(
        "The installed source no longer proves this reset public head.",
        status_code=409,
      )

    monkeypatch.setattr(
      github_routes,
      "_assert_personal_publication_source",
      reject_reverted_source,
    )

  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    row.lease_expires_at = now_naive_utc() - timedelta(minutes=1)
    db.commit()
  finally:
    db.close()
  second = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/respond",
    json={
      "attention": {
        "key": "complete-db:second",
        "event_at": "2026-07-11T00:00:00Z",
      },
    },
    headers=app_headers,
  )
  assert second.status_code == 200, second.text
  second_run = second.json()["run_id"]
  assert second_run != first_run

  recovered = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update",
    json={**request_body, "run_id": second_run},
    headers=agent_headers,
  )
  receipt_path = github_routes.contribution_runtime.personal_attempt_path(
    app_id, record_id,
  )
  if remote_after_failure == "reset":
    assert recovered.status_code == 409, recovered.text
    assert publication_calls == ["pushed"]
    assert source_checks == ["checked", "rechecked"]
    assert receipt_path.exists()
  else:
    assert recovered.status_code == 200, recovered.text
    assert publication_calls == ["pushed"]
    assert source_checks == ["checked"]
    assert not receipt_path.exists()
  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    assert row.run_id == second_run
    if remote_after_failure == "reset":
      assert row.round_action is None
      assert row.round_head_sha is None
    else:
      assert row.round_action == "pushed"
      assert row.round_head_sha == _HEAD2
  finally:
    db.close()


def test_injection_diff_outside_allowlist_is_rejected(
  client, owner_token, monkeypatch,
):
  """An /update whose diff touches a non-source path is refused (Hard stop #2)."""
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  headers = {"Authorization": f"Bearer {app_token}"}
  agent_headers = {"Authorization": f"Bearer {owner_token}"}
  record_id = "rec-injection"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  rec = _record(record_id, repo)
  rec["status"] = "open"  # already shipped
  rec["number"] = 42
  rec["url"] = "https://github.com/mobius-os/app-demo/pull/42"
  rec["head_repository"] = "octocat/app-demo-1"
  evil_diff = (
    "diff --git a/data/shared/memory/secret b/data/shared/memory/secret\n"
    "--- a/data/shared/memory/secret\n+++ b/data/shared/memory/secret\n"
    "@@ -1 +1 @@\n-x\n+leak\n"
  )
  rec["plan"]["head_sha"] = _HEAD2
  rec["plan"]["diff_sha256"] = hashlib.sha256(evil_diff.encode()).hexdigest()
  _mark_reviewed_update(rec)
  _write_contribution(app_id, record_id, rec, evil_diff)
  monkeypatch.setattr(
    github_routes, "_autopilot_changed_paths",
    lambda *args: ["data/shared/memory/secret"],
  )
  monkeypatch.setattr(
    github_routes, "_resolve_reviewed_commit",
    lambda repo_path, value, label: str(value),
  )
  monkeypatch.setattr(
    github_routes, "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": _HEAD1,
      "base_branch": "main",
      "base_sha": _BASE,
      "title": rec["plan"]["title"],
      "body": rec["plan"]["body_draft"],
    },
  )

  db = SessionLocal()
  try:
    autopilot.stamp_grant(
      db, app_id, record_id, head_sha=_HEAD1,
      target_repo="mobius-os/app-demo", target_pr_number=42,
      target_head_repository="octocat/app-demo-1",
      target_branch=_BRANCH, target_repo_path=str(repo.resolve()),
    )
    verdict = autopilot.claim_for_round(
      db, app_id, record_id, attention_key="k", event_at="2026-07-10T00:00:00Z",
    )
    run_id = verdict["run_id"]
  finally:
    db.close()

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update",
    json={"run_id": run_id, "head_sha": _HEAD2,
          "diff_sha256": hashlib.sha256(evil_diff.encode()).hexdigest()},
    headers=agent_headers,
  )
  assert r.status_code == 422
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record_id,
  ).exists()


def test_stale_lease_then_second_failure_escalates(
  client, owner_token, monkeypatch,
):
  """A crashed round (expired lease) becomes stale; the second escalates with a
  human_required attention + owner notification."""
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  headers = {"Authorization": f"Bearer {app_token}"}
  record_id = "rec-stale"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  rec = _record(record_id, repo)
  rec["status"] = "open"
  _write_contribution(app_id, record_id, rec, _DIFF1)
  monkeypatch.setattr(autopilot, "spawn_round_turn", _fake_spawn)
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")

  db = SessionLocal()
  try:
    autopilot.stamp_grant(db, app_id, record_id, head_sha=_HEAD1)
  finally:
    db.close()

  # First round claims, then "crashes" (force the lease into the past).
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/respond",
    json={"attention": {"key": "k1", "event_at": "2026-07-10T00:00:00Z"}},
    headers=headers,
  )
  assert r.json()["status"] == "responding"
  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    row.lease_expires_at = now_naive_utc() - timedelta(minutes=1)
    db.commit()
  finally:
    db.close()

  # Second event reclaims (stale round #1) and starts round #2.
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/respond",
    json={"attention": {"key": "k2", "event_at": "2026-07-11T00:00:00Z"}},
    headers=headers,
  )
  assert r.json()["status"] == "responding"
  db = SessionLocal()
  try:
    row = autopilot.get_row(db, app_id, record_id)
    row.lease_expires_at = now_naive_utc() - timedelta(minutes=1)
    db.commit()
  finally:
    db.close()

  # Third event: second consecutive stale → escalate.
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/respond",
    json={"attention": {"key": "k3", "event_at": "2026-07-12T00:00:00Z"}},
    headers=headers,
  )
  assert r.status_code == 200
  assert r.json()["status"] == "escalated"
  updated = _read(app_id, record_id)
  assert updated["needs_attention"] is True
  assert updated["attention"]["type"] == "human_required"
  db = SessionLocal()
  try:
    notes = db.query(models.Notification).all()
    assert any("needs you" in (n.title or "").lower() for n in notes)
  finally:
    db.close()
