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
  }


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
    if args[:2] == ("repo", "fork"):
      state["fork_ready"] = True
      return _cp("")
    if args[:2] == ("pr", "list"):
      if state.get("pr_open"):
        return _cp(json.dumps([{
          "url": "https://github.com/mobius-os/app-demo/pull/42",
          "headRefOid": state["head"],
        }]))
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      state["pr_open"] = True
      return _cp("https://github.com/mobius-os/app-demo/pull/42\n")
    return _cp("")

  return fake_git, fake_gh


def _install_fakes(monkeypatch, state):
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")
  fake_git, fake_gh = _make_fakes(state)
  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", fake_gh)


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
    github_routes, "_autopilot_live_target_error",
    lambda *a, **k: None,
  )

  # 1. Submit with autopilot → PR opened + grant stamped + mirror written.
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    json={"autopilot": True}, headers=headers,
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
  _write_contribution(app_id, record_id, rec, evil_diff)
  monkeypatch.setattr(
    github_routes, "_autopilot_changed_paths",
    lambda *args: ["data/shared/memory/secret"],
  )
  monkeypatch.setattr(
    github_routes, "_resolve_reviewed_commit",
    lambda repo_path, value, label: str(value),
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
