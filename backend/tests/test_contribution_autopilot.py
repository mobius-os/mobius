"""Tests for the contribution autopilot lifecycle + routes.

Two layers:
  - Module (app/contribution_autopilot.py): the claim/lease/round state machine,
    dedupe/cursor, run_id binding, escalation, budget-exhaustion, pause/resume.
  - Routes (app/routes/github.py autopilot endpoints): the trust boundary — the
    DB row is the only authorization, a forged ledger block does nothing, agent
    tokens can't forge a claim, status advertises availability + budget.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from app import contribution_autopilot as autopilot
from app import models
from app.config import get_settings
from app.database import SessionLocal
from app.storage_io import atomic_write
from app.timeutil import now_naive_utc

from app.routes.github import _limiter as _github_limiter

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
  autopilot.complete_round(
    db, 1, "rec", run_id=run_id, outcome="pushed", summary="ok",
    event_at="2026-07-01T12:00:00Z",
  )
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


def test_stale_lease_reclaim_and_double_stale_escalate(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  autopilot.claim_for_round(
    db, 1, "rec", attention_key="k1", event_at="2026-07-01T00:00:00Z",
  )
  row = autopilot.get_row(db, 1, "rec")
  row.lease_expires_at = now_naive_utc() - timedelta(minutes=1)
  db.commit()
  swept = autopilot.sweep_stale(db)
  assert swept == [(1, "rec", False)]
  assert autopilot.get_row(db, 1, "rec").state == "idle"
  # A second stale claim crosses the threshold → escalate verdict.
  autopilot.claim_for_round(
    db, 1, "rec", attention_key="k2", event_at="2026-07-02T00:00:00Z",
  )
  row = autopilot.get_row(db, 1, "rec")
  row.lease_expires_at = now_naive_utc() - timedelta(minutes=1)
  db.commit()
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k3", event_at="2026-07-03T00:00:00Z",
  )
  assert v["status"] == "escalate"
  assert v["reason"] == "stale_rounds"


def test_budget_exhaustion_escalates(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  row = autopilot.get_row(db, 1, "rec")
  row.rounds_used = row.max_rounds
  db.commit()
  v = autopilot.claim_for_round(
    db, 1, "rec", attention_key="k", event_at="2026-08-01T00:00:00Z",
  )
  assert v["status"] == "escalate" and v["reason"] == "budget_exhausted"


def test_resume_resets_budget_and_close_out(db):
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


def test_round_log_capped(db):
  autopilot.stamp_grant(db, 1, "rec", head_sha="abc")
  for i in range(40):
    v = autopilot.claim_for_round(
      db, 1, "rec", attention_key=f"k{i}", event_at=f"2026-07-{i%28+1:02d}T00:00:00Z",
    )
    if v["status"] == "granted":
      autopilot.complete_round(
        db, 1, "rec", run_id=v["run_id"], outcome="pushed", summary=f"r{i}",
        event_at=f"2026-07-{i%28+1:02d}T01:00:00Z",
      )
  block = autopilot.mirror_block(autopilot.get_row(db, 1, "rec"))
  assert len(block["rounds"]) <= autopilot.MAX_ROUND_LOG


# ─────────────────────────── routes: trust boundary ────────────────────


def _app_with_github_access(client, owner_token):
  r = client.post("/api/apps/", json={
    "name": "contribute-test",
    "description": "t",
    "jsx_source": "export default function App(){ return <div/> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]
  s = SessionLocal()
  try:
    app = s.query(models.App).filter(models.App.id == app_id).first()
    app.github_access = True
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


def test_status_advertises_autopilot_and_budget(client, owner_token):
  app_id, app_token = _app_with_github_access(client, owner_token)
  r = client.get("/api/github/status",
                 headers={"Authorization": f"Bearer {app_token}"})
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["autopilot_available"] is True
  assert "autopilot_budget" in body


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


def test_agent_token_cannot_forge_a_claim(client, owner_token):
  app_id, app_token = _app_with_github_access(client, owner_token)
  _write_record(app_id, "rec1", _open_pr_record())
  # No live round → complete/reply/escalate/update all 409 on run_id.
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
    assert r.status_code == 409, (path, r.text)


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


def test_respond_defers_when_budget_exhausted(client, owner_token):
  app_id, app_token = _app_with_github_access(client, owner_token)
  _write_record(app_id, "rec1", _open_pr_record())
  # Grant + a budget setting of 0% (disables spend) → /respond defers.
  os.makedirs(os.path.join(get_settings().data_dir, "shared"), exist_ok=True)
  with open(os.path.join(get_settings().data_dir, "shared",
                         "agent-settings.json"), "w") as handle:
    json.dump({"autopilot_budget": {"percent": 0}}, handle)
  s = SessionLocal()
  try:
    autopilot.stamp_grant(s, app_id, "rec1", head_sha="a" * 40)
  finally:
    s.close()
  r = client.post(
    f"/api/github/contributions/{app_id}/rec1/respond",
    json={"attention": {"key": "changes_requested:1"}},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200
  assert r.json()["status"] == "deferred"
  os.remove(os.path.join(get_settings().data_dir, "shared",
                         "agent-settings.json"))


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
  _write_record(app_id, "rec1", _open_pr_record())
  s = SessionLocal()
  try:
    autopilot.stamp_grant(s, app_id, "rec1", head_sha="a" * 40)
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
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 409
