"""Tests for the contribution autopilot lifecycle + routes.

Two layers:
  - Module (app/contribution_autopilot.py): the claim/lease/round state machine,
    dedupe/cursor, run_id binding, escalation, round limit, pause/resume.
  - Routes (app/routes/github.py autopilot endpoints): the trust boundary — the
    DB row is the only authorization, a forged ledger block does nothing, agent
    tokens can't forge a claim, status advertises capability.
"""

import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from app import contribution_autopilot as autopilot
from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.storage_io import atomic_write
from app.timeutil import now_naive_utc
from test_app_fixtures import create_local_app

from app.routes.github import _limiter as _github_limiter
from app.routes.github import ContributionSubmitBody

_github_limiter.enabled = False


@pytest.fixture
def db(fresh_db):
  s = SessionLocal()
  try:
    yield s
  finally:
    s.close()


# ─────────────────────────── module: state machine ─────────────────────


def test_stamp_grant_idempotent_and_reenables(db):
  row = autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  assert row.enabled is True and row.state == "idle"
  autopilot.set_enabled(db, 1, "rec", False)
  assert autopilot.get_row(db, 1, "rec").enabled is False
  # Re-send re-enables and refreshes the head without wiping the log.
  autopilot.stamp_grant(db, 1, "rec", head_sha="def")
  again = autopilot.get_row(db, 1, "rec")
  assert again.enabled is True
  assert again.granted_head_sha == "def"


def test_claim_dedupe_busy_and_cursor(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k1", event_at="2026-07-01T00:00:00Z",
  )
  assert v["status"] == "granted"
  # Same key while in flight → duplicate.
  assert autopilot.claim_for_round(
    db, 1, "rec", attention_key="k1", event_at="2026-07-01T00:00:00Z",
  )["status"] == "duplicate"
  # Different key while a live round holds the claim → busy.
  assert autopilot.claim_for_round(
    db, 1, "rec", attention_key="k2", event_at="2026-07-02T00:00:00Z",
  )["status"] == "busy"
  run_id = v["run_id"]
  assert autopilot.record_action(
    db, 1, "rec", run_id=run_id, action="pushed",
  )
  autopilot.complete_round(
    db, 1, "rec", run_id=run_id, outcome="pushed", summary="ok",
    event_at="2099-07-01T12:00:00Z",
  )
  # Completion cannot choose a future cursor; the claim timestamp wins.
  assert autopilot.get_row(
    db, 1, "rec"
  ).last_handled_event_at == "2026-07-01T00:00:00.000000Z"
  # An event at/older than the cursor never re-triggers.
  assert autopilot.claim_for_round(
    db, 1, "rec", attention_key="k3", event_at="2026-07-01T00:00:00Z",
  )["status"] == "duplicate"


def test_run_id_binds_the_round(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k1", event_at="2026-07-01T00:00:00Z",
  )
  row = autopilot.get_row(db, 1, "rec")
  assert autopilot.verify_claim(row, v["run_id"]) is True
  assert autopilot.verify_claim(row, "wrong") is False
  # complete with the wrong run_id is a no-op stale.
  assert autopilot.complete_round(
    db, 1, "rec", run_id="wrong", outcome="pushed", summary="x",
  )["status"] == "stale"
  assert autopilot.release_for_retry(
    db, 1, "rec", run_id="wrong",
  ) is False
  assert autopilot.verify_claim(
    autopilot.get_row(db, 1, "rec"), v["run_id"],
  ) is True


def test_claim_is_atomic_across_sessions(db, monkeypatch):
  autopilot.stamp_grant(db, 1, "race", head_sha="abc")
  barrier = threading.Barrier(2)
  original = autopilot.get_row
  local = threading.local()

  def synchronized_get(session, app_id, record_id):
    row = original(session, app_id, record_id)
    if not getattr(local, "waited", False):
      local.waited = True
      barrier.wait(timeout=5)
    return row

  monkeypatch.setattr(autopilot, "get_row", synchronized_get)
  results = []

  def worker(key):
    session = SessionLocal()
    try:
      results.append(autopilot.claim_for_round(
        session, 1, "race", attention_key=key,
        event_at=f"2026-07-01T00:00:0{key}Z",
      ))
    finally:
      session.close()

  threads = [threading.Thread(target=worker, args=(key,)) for key in ("1", "2")]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(timeout=10)
  assert all(not thread.is_alive() for thread in threads)
  assert sorted(result["status"] for result in results) == ["busy", "granted"]


def test_failed_rounds_escalate_after_threshold(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k1", event_at="2026-07-01T00:00:00Z",
  )
  r1 = autopilot.complete_round(
    db, 1, "rec", run_id=v["run_id"], outcome="failed", summary="boom",
  )
  assert r1["escalate"] is False
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k2", event_at="2026-07-02T00:00:00Z",
  )
  r2 = autopilot.complete_round(
    db, 1, "rec", run_id=v["run_id"], outcome="failed", summary="boom2",
  )
  assert r2["escalate"] is True


def test_completion_cannot_claim_unrecorded_public_work(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k", event_at="2026-07-01T00:00:00Z",
  )
  result = autopilot.complete_round(
    db, 1, "rec", run_id=v["run_id"],
    outcome="pushed", summary="claimed without using /update",
    head_sha="f" * 40,
  )
  assert result == {
    "status": "ok", "escalate": False, "productive": False,
  }
  row = autopilot.get_row(db, 1, "rec")
  assert row.rounds_used == 0
  assert row.last_handled_event_at is None
  assert row.granted_head_sha == "abc"
  assert row.rounds_json[-1]["outcome"] == "failed"


def test_reply_action_mirrors_exact_event_urls(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k", event_at="2026-07-01T00:00:00Z",
  )
  url = "https://github.com/mobius-os/mobius/pull/1#issuecomment-2"
  assert autopilot.record_action(
    db, 1, "rec", run_id=v["run_id"], action="replied",
    public_event_url=url,
  )
  block = autopilot.mirror_block(autopilot.get_row(db, 1, "rec"))
  assert block["ignored_event_urls"] == [url]


def test_concurrent_actions_preserve_push_and_both_reply_urls(db, monkeypatch):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k", event_at="2026-07-01T00:00:00Z",
  )
  barrier = threading.Barrier(3)
  original = autopilot.get_row
  local = threading.local()

  def synchronized_get(session, app_id, record_id):
    row = original(session, app_id, record_id)
    if not getattr(local, "waited", False):
      local.waited = True
      barrier.wait(timeout=5)
    return row

  monkeypatch.setattr(autopilot, "get_row", synchronized_get)
  results = []

  def worker(action, url=None):
    session = SessionLocal()
    try:
      results.append(autopilot.record_action(
        session, 1, "rec", run_id=v["run_id"], action=action,
        head_sha="f" * 40 if action == "pushed" else None,
        public_event_url=url,
      ))
    finally:
      session.close()

  urls = [
    "https://github.com/mobius-os/mobius/pull/1#issuecomment-2",
    "https://github.com/mobius-os/mobius/pull/1#issuecomment-3",
  ]
  threads = [
    threading.Thread(target=worker, args=("pushed",)),
    *(threading.Thread(target=worker, args=("replied", url)) for url in urls),
  ]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(timeout=10)
  assert all(not thread.is_alive() for thread in threads)
  assert results == [True, True, True]
  monkeypatch.setattr(autopilot, "get_row", original)
  db.expire_all()
  row = autopilot.get_row(db, 1, "rec")
  assert row.round_action == "pushed"
  assert row.round_head_sha == "f" * 40
  assert set(row.ignored_event_urls_json) == set(urls)


def test_escalation_pauses_until_owner_resumes(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k", event_at="2026-07-01T00:00:00Z",
  )
  autopilot.escalate(db, 1, "rec")
  row = autopilot.get_row(db, 1, "rec")
  assert row.enabled is False
  assert autopilot.verify_claim(row, v["run_id"]) is False
  assert autopilot.claim_for_round(
    db, 1, "rec", attention_key="k2", event_at="2026-07-02T00:00:00Z",
  )["status"] == "not_granted"


def test_stale_lease_reclaim_and_double_stale_escalate(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  autopilot.claim_for_round(
    db, 1, "rec", attention_key="k1", event_at="2026-07-01T00:00:00Z",
  )
  row = autopilot.get_row(db, 1, "rec")
  row.lease_expires_at = now_naive_utc() - timedelta(minutes=1)
  db.commit()
  # The next retry reclaims stale round #1 and starts round #2.
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k2", event_at="2026-07-02T00:00:00Z",
  )
  assert v["status"] == "granted"
  row = autopilot.get_row(db, 1, "rec")
  row.lease_expires_at = now_naive_utc() - timedelta(minutes=1)
  db.commit()
  # A second stale claim crosses the threshold → escalate verdict.
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k3", event_at="2026-07-03T00:00:00Z",
  )
  assert v["status"] == "escalate"
  assert v["reason"] == "stale_rounds"


def test_round_limit_escalates(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  row = autopilot.get_row(db, 1, "rec")
  row.rounds_used = row.max_rounds
  db.commit()
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k", event_at="2026-08-01T00:00:00Z",
  )
  assert v["status"] == "escalate" and v["reason"] == "round_limit"


def test_resume_resets_round_limit_and_close_out(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  row = autopilot.get_row(db, 1, "rec")
  row.rounds_used = 5
  row.consecutive_failures = 2
  db.commit()
  autopilot.set_enabled(db, 1, "rec", True)
  row = autopilot.get_row(db, 1, "rec")
  assert row.rounds_used == 0 and row.consecutive_failures == 0
  autopilot.close_out(db, 1, "rec")
  assert autopilot.get_row(db, 1, "rec").enabled is False


def test_visible_in_owner_drawer_honors_explicit_flag():
  from app.routes.chats import _visible_in_owner_drawer

  # An ordinary owner chat (no creating app) is visible by default...
  owner_chat = models.Chat(
    id="c-owner", title="t", messages=[], pending_messages=[],
  )
  assert _visible_in_owner_drawer(owner_chat) is True
  # ...but an explicit drawer_hidden bit overrides in either direction.
  owner_chat.agent_settings_json = {"drawer_hidden": True}
  assert _visible_in_owner_drawer(owner_chat) is False
  owner_chat.agent_settings_json = {"drawer_hidden": False}
  assert _visible_in_owner_drawer(owner_chat) is True


def test_followup_chat_hidden_until_escalation(db):
  from app.routes.chats import _visible_in_owner_drawer

  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  chat_id = autopilot.ensure_followup_chat(
    db, 1, "rec", title="Autopilot: fix thing", provider="claude",
  )
  assert chat_id

  def _chat():
    db.expire_all()
    return db.query(models.Chat).filter(models.Chat.id == chat_id).first()

  # A routine round is kept out of the owner's chat list.
  assert _visible_in_owner_drawer(_chat()) is False

  # Escalation is the one moment it needs the owner → surface it.
  assert autopilot.escalate(db, 1, "rec") is True
  assert _visible_in_owner_drawer(_chat()) is True

  # Resuming hands control back to autopilot → tuck it away again.
  autopilot.set_enabled(db, 1, "rec", True)
  assert _visible_in_owner_drawer(_chat()) is False


def test_followup_chat_hidden_on_close_out(db):
  from app.routes.chats import _visible_in_owner_drawer

  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  chat_id = autopilot.ensure_followup_chat(
    db, 1, "rec", title="Autopilot: fix thing", provider="claude",
  )
  autopilot.escalate(db, 1, "rec")

  def _chat():
    db.expire_all()
    return db.query(models.Chat).filter(models.Chat.id == chat_id).first()

  assert _visible_in_owner_drawer(_chat()) is True
  # PR merged/closed: terminal cleanup hides the chat for good.
  autopilot.close_out(db, 1, "rec")
  assert _visible_in_owner_drawer(_chat()) is False


@pytest.mark.asyncio
async def test_round_turn_does_not_bypass_pending_owner_question(
  db, monkeypatch,
):
  """An escalated Autopilot chat stays parked until its owner answers."""
  from app import chat as chat_mod

  question = {
    "role": "assistant",
    "content": "Choose a direction.",
    "blocks": [{
      "type": "question",
      "question_id": "owner-decision",
      "questions": [{
        "id": "owner-decision", "question": "Which direction?",
      }],
    }],
  }
  chat = models.Chat(
    id="autopilot-question-blocked",
    title="Autopilot follow-up",
    messages=[question],
    pending_messages=[],
    provider="claude",
    pending_question_id="owner-decision",
  )
  db.add(chat)
  db.commit()
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: False)
  starts = []

  async def blocked_start(**kwargs):
    starts.append(kwargs)
    return False

  monkeypatch.setattr(autopilot, "start_programmatic_chat_turn", blocked_start)

  started = await autopilot.spawn_round_turn(
    db,
    chat.id,
    title="Autopilot follow-up",
    content="Continue automatically",
    provider="claude",
  )

  assert started is False
  assert [start["chat_id"] for start in starts] == [chat.id]
  db.expire_all()
  parked = db.get(models.Chat, chat.id)
  assert parked.messages == [question]
  assert parked.pending_question_id == "owner-decision"
  assert db.query(models.ChatRun).filter_by(chat_id=chat.id).count() == 0


def test_failed_surfacing_leaves_escalation_retryable(db):
  """A crash while surfacing must not consume the one-shot escalate latch.

  ``escalate`` wins exactly once (its UPDATE is gated on ``enabled``), and the
  caller only notifies the owner when it wins. If surfacing the chat committed
  separately, a failure between the two commits would leave the record
  escalated, the chat hidden, and the owner never told — with no retry able to
  repair it. Staging both in one transaction makes the failure recoverable.
  """
  from app.routes.chats import _visible_in_owner_drawer

  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  chat_id = autopilot.ensure_followup_chat(
    db, 1, "rec", title="Autopilot: fix thing", provider="claude",
  )

  def _chat():
    db.expire_all()
    return db.query(models.Chat).filter(models.Chat.id == chat_id).first()

  boom = RuntimeError("database is locked")
  original = autopilot.stage_followup_drawer_hidden
  autopilot.stage_followup_drawer_hidden = lambda *a, **k: (_ for _ in ()).throw(boom)
  try:
    with pytest.raises(RuntimeError):
      autopilot.escalate(db, 1, "rec")
    db.rollback()
  finally:
    autopilot.stage_followup_drawer_hidden = original

  # Nothing was consumed: the grant is still live and the chat still tucked away.
  db.expire_all()
  assert autopilot.get_row(db, 1, "rec").enabled is True
  assert _visible_in_owner_drawer(_chat()) is False

  # The retry that the cron pass makes next wins and surfaces the chat.
  assert autopilot.escalate(db, 1, "rec") is True
  assert _visible_in_owner_drawer(_chat()) is True


def test_failed_hiding_leaves_resume_retryable(db):
  """Resume and its drawer transition land together or remain retryable."""
  from app.routes.chats import _visible_in_owner_drawer

  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  chat_id = autopilot.ensure_followup_chat(
    db, 1, "rec", title="Autopilot: fix thing", provider="claude",
  )
  assert autopilot.escalate(db, 1, "rec") is True

  def _chat():
    db.expire_all()
    return db.query(models.Chat).filter(models.Chat.id == chat_id).first()

  boom = RuntimeError("database is locked")
  original = autopilot.stage_followup_drawer_hidden
  autopilot.stage_followup_drawer_hidden = lambda *a, **k: (_ for _ in ()).throw(boom)
  try:
    with pytest.raises(RuntimeError):
      autopilot.set_enabled(db, 1, "rec", True)
    db.rollback()
  finally:
    autopilot.stage_followup_drawer_hidden = original

  db.expire_all()
  assert autopilot.get_row(db, 1, "rec").enabled is False
  assert _visible_in_owner_drawer(_chat()) is True

  autopilot.set_enabled(db, 1, "rec", True)
  assert autopilot.get_row(db, 1, "rec").enabled is True
  assert _visible_in_owner_drawer(_chat()) is False


def test_round_log_capped(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  for i in range(40):
    v = autopilot.claim_for_round(
      db, 1, "rec", attention_key=f"k{i}", event_at=f"2026-07-{i%28+1:02d}T00:00:00Z",
    )
    if v["status"] == "granted":
      autopilot.record_action(
        db, 1, "rec", run_id=v["run_id"], action="pushed",
      )
      autopilot.complete_round(
        db, 1, "rec", run_id=v["run_id"], outcome="pushed", summary=f"r{i}",
        event_at=f"2026-07-{i%28+1:02d}T01:00:00Z",
      )
  block = autopilot.mirror_block(autopilot.get_row(db, 1, "rec"))
  assert len(block["rounds"]) <= autopilot.MAX_ROUND_LOG


# ─────────────────────────── routes: trust boundary ────────────────────


def _app_with_github_access(client, owner_token, *, connect=False):
  app_id = create_local_app(
    client, {"Authorization": f"Bearer {owner_token}"},
    name="contribute-test", description="t",
  )["id"]
  s = SessionLocal()
  try:
    app = s.query(models.App).filter(models.App.id == app_id).first()
    app.github_access = True
    app.github_connect = connect
    s.commit()
  finally:
    s.close()
  r = client.post("/api/auth/app-token", json={"app_id": app_id},
                  headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 200, r.text
  return app_id, r.json()["token"]


def _write_record(app_id, record_id, record):
  base = Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  base.mkdir(parents=True, exist_ok=True)
  atomic_write(base / f"{record_id}.json", json.dumps(record))


def _open_pr_record(record_id="rec1"):
  return {
    "id": record_id, "type": "pr", "status": "open",
    "repo": "mobius-os/app-demo", "title": "Reviewed fix",
    "url": "https://github.com/mobius-os/app-demo/pull/7", "number": 7,
    "plan": {"action": "pr", "repo": "mobius-os/app-demo",
             "branch": "fix/x", "head_sha": "a" * 40,
             "diff_sha256": "b" * 64},
  }


def test_status_advertises_autopilot(client, owner_token):
  app_id, app_token = _app_with_github_access(
    client, owner_token, connect=True,
  )
  r = client.get("/api/github/status",
                 headers={"Authorization": f"Bearer {app_token}"})
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["autopilot_available"] is True


def test_legacy_submit_body_does_not_grant_autopilot():
  assert ContributionSubmitBody().autopilot is False


def test_forged_ledger_block_is_ignored_no_db_row(client, owner_token):
  app_id, app_token = _app_with_github_access(client, owner_token)
  # A forged autopilot block in the agent-writable ledger must NOT authorize.
  rec = _open_pr_record()
  rec["autopilot"] = {"enabled": True, "state": "idle"}
  _write_record(app_id, "rec1", rec)
  r = client.post(
    f"/api/github/contributions/{app_id}/rec1/respond",
    json={"attention": {"key": "changes_requested:1", "type": "changes_requested"}},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200
  assert r.json()["status"] == "not_granted"


def test_app_token_cannot_perform_agent_actions(client, owner_token):
  app_id, app_token = _app_with_github_access(client, owner_token)
  _write_record(app_id, "rec1", _open_pr_record())
  # App JWTs may detect/respond, but cannot inherit the agent action surface.
  headers = {"Authorization": f"Bearer {app_token}"}
  for path, body in [
    ("complete", {"run_id": "ghost", "outcome": "pushed"}),
    ("reply", {"run_id": "ghost", "body": "hi"}),
    ("escalate", {"run_id": "ghost", "message": "x"}),
    ("update", {"run_id": "ghost", "head_sha": "a" * 40,
                "diff_sha256": "b" * 64}),
  ]:
    r = client.post(
      f"/api/github/contributions/{app_id}/rec1/{path}",
      json=body, headers=headers,
    )
    assert r.status_code == 403, (path, r.text)


def test_bound_target_rejects_agent_retargeting(client, owner_token):
  app_id, _ = _app_with_github_access(client, owner_token)
  repo_path = Path(get_settings().data_dir) / "contributions" / "bound" / "repo"
  (repo_path / ".git").mkdir(parents=True)
  rec = _open_pr_record("bound")
  rec["head_repository"] = "octocat/app-demo"
  rec["plan"]["repo_path"] = str(repo_path)
  _write_record(app_id, "bound", rec)
  s = SessionLocal()
  try:
    autopilot.stamp_grant(
      s, app_id, "bound", head_sha="a" * 40,
      target_repo="mobius-os/app-demo", target_pr_number=7,
      target_head_repository="octocat/app-demo", target_branch="fix/x",
      target_repo_path=str(repo_path.resolve()),
    )
    run_id = autopilot.claim_for_round(
      s, app_id, "bound", attention_key="k",
      event_at="2026-07-01T00:00:00Z",
    )["run_id"]
  finally:
    s.close()
  rec["repo"] = "mobius-os/mobius"
  rec["plan"]["repo"] = "mobius-os/mobius"
  _write_record(app_id, "bound", rec)
  r = client.post(
    f"/api/github/contributions/{app_id}/bound/reply",
    json={"run_id": run_id, "body": "retarget"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 409


def test_reply_mirrors_own_event_before_round_completion(
  client, owner_token, monkeypatch,
):
  app_id, _ = _app_with_github_access(client, owner_token)
  repo_path = Path(get_settings().data_dir) / "contributions" / "reply" / "repo"
  (repo_path / ".git").mkdir(parents=True)
  rec = _open_pr_record("reply")
  rec["head_repository"] = "octocat/app-demo"
  rec["plan"]["repo_path"] = str(repo_path)
  _write_record(app_id, "reply", rec)
  s = SessionLocal()
  try:
    autopilot.stamp_grant(
      s, app_id, "reply", head_sha="a" * 40,
      target_repo="mobius-os/app-demo", target_pr_number=7,
      target_head_repository="octocat/app-demo", target_branch="fix/x",
      target_repo_path=str(repo_path.resolve()),
    )
    run_id = autopilot.claim_for_round(
      s, app_id, "reply", attention_key="k",
      event_at="2026-07-01T00:00:00Z",
    )["run_id"]
  finally:
    s.close()
  url = "https://github.com/mobius-os/app-demo/pull/7#issuecomment-9"
  monkeypatch.setattr(
    "app.routes.github._autopilot_post_reply",
    lambda *_args, **_kwargs: {"ok": True, "url": url},
  )
  r = client.post(
    f"/api/github/contributions/{app_id}/reply/reply",
    json={"run_id": run_id, "body": "Handled."},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "contributions" / "reply.json"
  )
  mirrored = json.loads(record_path.read_text())
  assert mirrored["autopilot"]["ignored_event_urls"] == [url]


def test_pause_resume_requires_grant(client, owner_token):
  app_id, app_token = _app_with_github_access(client, owner_token)
  _write_record(app_id, "rec1", _open_pr_record())
  headers = {"Authorization": f"Bearer {app_token}"}
  # No grant row yet → 404.
  r = client.post(f"/api/github/contributions/{app_id}/rec1/autopilot",
                  json={"enabled": False}, headers=headers)
  assert r.status_code == 404
  # Stamp a grant (as submit would), then pause + resume succeed.
  s = SessionLocal()
  try:
    autopilot.stamp_grant(s, app_id, "rec1", head_sha="a" * 40)
  finally:
    s.close()
  r = client.post(f"/api/github/contributions/{app_id}/rec1/autopilot",
                  json={"enabled": False}, headers=headers)
  assert r.status_code == 200 and r.json()["enabled"] is False
  r = client.post(f"/api/github/contributions/{app_id}/rec1/autopilot",
                  json={"enabled": True}, headers=headers)
  assert r.status_code == 200 and r.json()["enabled"] is True


def test_respond_requires_attention_key(client, owner_token):
  app_id, app_token = _app_with_github_access(client, owner_token)
  _write_record(app_id, "rec1", _open_pr_record())
  s = SessionLocal()
  try:
    autopilot.stamp_grant(s, app_id, "rec1", head_sha="a" * 40)
  finally:
    s.close()
  r = client.post(
    f"/api/github/contributions/{app_id}/rec1/respond",
    json={"attention": {}},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 400


def test_update_rejects_mismatched_reviewed_state(client, owner_token):
  app_id, app_token = _app_with_github_access(client, owner_token)
  rec = _open_pr_record()
  repo = Path(get_settings().data_dir) / "contributions" / "rec1" / "repo"
  (repo / ".git").mkdir(parents=True)
  rec["head_repository"] = "octocat/app-demo"
  rec["plan"]["repo_path"] = str(repo)
  rec["plan"]["base_sha"] = "e" * 40
  _write_record(app_id, "rec1", rec)
  s = SessionLocal()
  try:
    autopilot.stamp_grant(
      s, app_id, "rec1", head_sha="a" * 40,
      target_repo="mobius-os/app-demo", target_pr_number=7,
      target_head_repository="octocat/app-demo", target_branch="fix/x",
      target_repo_path=str(repo.resolve()),
    )
    v = autopilot.claim_for_round(
      s, app_id, "rec1", attention_key="k1", event_at="2026-07-01T00:00:00Z",
    )
    run_id = v["run_id"]
  finally:
    s.close()
  # Wrong head/diff vs the record's plan → 409 (won't push unreviewed work).
  r = client.post(
    f"/api/github/contributions/{app_id}/rec1/update",
    json={"run_id": run_id, "head_sha": "c" * 40, "diff_sha256": "d" * 64},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 409
