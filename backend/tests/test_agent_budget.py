"""Tests for the weekly-allowance budget meter (app/agent_budget.py).

Exercises the two accrual paths (utilization deltas + token fallback), the
autopilot-chat attribution boundary, window rollover, the headroom ceiling, the
defer decision /respond consumes, and the chat_runs.cost_usd producer.
"""

import os

import pytest

from app import agent_budget, models
from app.database import SessionLocal
from app.timeutil import now_naive_utc


DATA_DIR = os.environ["DATA_DIR"]


@pytest.fixture
def db(fresh_db):
  s = SessionLocal()
  try:
    yield s
  finally:
    s.close()


def _register_autopilot_chat(db, chat_id, *, app_id=1, record_id="rec"):
  row = models.ContributionAutopilot(
    app_id=app_id, record_id=record_id, followup_chat_id=chat_id, enabled=True,
  )
  db.add(row)
  db.commit()


def _clear_budget_setting():
  path = os.path.join(DATA_DIR, "shared", "agent-settings.json")
  if os.path.exists(path):
    os.remove(path)


def _write_budget_setting(percent=None, weekly_tokens=None):
  import json
  os.makedirs(os.path.join(DATA_DIR, "shared"), exist_ok=True)
  block = {}
  if percent is not None:
    block["percent"] = percent
  if weekly_tokens is not None:
    block["weekly_tokens"] = weekly_tokens
  path = os.path.join(DATA_DIR, "shared", "agent-settings.json")
  with open(path, "w") as handle:
    json.dump({"autopilot_budget": block}, handle)


@pytest.fixture(autouse=True)
def _reset_setting():
  _clear_budget_setting()
  yield
  _clear_budget_setting()


def test_default_setting_and_empty_status(db):
  setting = agent_budget.read_budget_setting(DATA_DIR)
  assert setting == {"percent": 10.0, "weekly_tokens": 2_000_000}
  status = agent_budget.budget_status(db, DATA_DIR, "claude")
  assert status["paused"] is False
  assert status["points_spent"] == 0.0


def test_percent_zero_disables(db):
  _write_budget_setting(percent=0)
  status = agent_budget.budget_status(db, DATA_DIR, "claude")
  assert status["paused"] is True
  assert status["reason"] == "disabled"


def test_headroom_ceiling_defers_regardless_of_share(db):
  # 95% of the weekly allowance is gone (foreground included) — defer even
  # though autopilot itself has spent nothing.
  agent_budget.record_observation(db, "claude", 95.0, "2026-08-01T00:00:00Z")
  status = agent_budget.budget_status(db, DATA_DIR, "claude")
  assert status["paused"] is True
  assert status["reason"] == "headroom"


def test_utilization_delta_accrues_only_for_autopilot_chat(db):
  _register_autopilot_chat(db, "chatA")
  # Autopilot run: utilization 5 -> 8 = 3 points.
  agent_budget.accrue_run(
    db, chat_id="chatA", provider="claude",
    util_first=5.0, util_last=8.0, resets_at="2026-08-08T00:00:00Z",
  )
  assert agent_budget.budget_status(db, DATA_DIR, "claude")["points_spent"] == 3.0
  # Foreground run on a normal chat must NOT accrue.
  agent_budget.accrue_run(
    db, chat_id="foreground", provider="claude",
    util_first=8.0, util_last=50.0, resets_at="2026-08-08T00:00:00Z",
  )
  assert agent_budget.budget_status(db, DATA_DIR, "claude")["points_spent"] == 3.0


def test_share_cap_trips_at_percent(db):
  _register_autopilot_chat(db, "chatA")
  agent_budget.record_observation(db, "claude", 11.0, "2026-08-08T00:00:00Z")
  agent_budget.accrue_run(
    db, chat_id="chatA", provider="claude",
    util_first=0.0, util_last=11.0, resets_at="2026-08-08T00:00:00Z",
  )
  status = agent_budget.budget_status(db, DATA_DIR, "claude")
  assert status["paused"] is True
  assert status["reason"] == "share"
  assert status["resume_at"] == "2026-08-08T00:00:00Z"


def test_window_rolls_over_on_new_resets_at(db):
  _register_autopilot_chat(db, "chatA")
  agent_budget.record_observation(db, "claude", 11.0, "2026-08-08T00:00:00Z")
  agent_budget.accrue_run(
    db, chat_id="chatA", provider="claude",
    util_first=0.0, util_last=11.0, resets_at="2026-08-08T00:00:00Z",
  )
  assert agent_budget.budget_status(db, DATA_DIR, "claude")["paused"] is True
  # New week: the observation's resets_at advances, so the current window is a
  # fresh key with zero spend.
  agent_budget.record_observation(db, "claude", 1.0, "2026-08-15T00:00:00Z")
  status = agent_budget.budget_status(db, DATA_DIR, "claude")
  assert status["paused"] is False
  assert status["points_spent"] == 0.0


def test_token_fallback_accrues_and_caps(db):
  _write_budget_setting(percent=10, weekly_tokens=1000)
  _register_autopilot_chat(db, "chatA")
  # No resets_at => token fallback path (ISO-week bucket).
  agent_budget.accrue_run(
    db, chat_id="chatA", provider="codex",
    usage={"input_tokens": 400, "output_tokens": 300,
           "cache_creation_input_tokens": 100},
  )
  status = agent_budget.budget_status(db, DATA_DIR, "codex")
  assert status["tokens_spent"] == 800
  assert status["paused"] is False
  agent_budget.accrue_run(
    db, chat_id="chatA", provider="codex",
    usage={"input_tokens": 300, "output_tokens": 0},
  )
  status = agent_budget.budget_status(db, DATA_DIR, "codex")
  assert status["tokens_spent"] == 1100
  assert status["paused"] is True
  assert status["reason"] == "share"


def test_cost_producer_writes_chat_runs(db):
  run = models.ChatRun(id="run-1", chat_id="chatX", status="completed")
  db.add(run)
  db.commit()
  agent_budget.accrue_run(
    db, chat_id="chatX", provider="claude", run_token="run-1", cost_usd=0.42,
  )
  db.expire_all()
  refreshed = db.query(models.ChatRun).filter(models.ChatRun.id == "run-1").first()
  assert refreshed.cost_usd == pytest.approx(0.42)


def test_negative_delta_never_reduces_spend(db):
  _register_autopilot_chat(db, "chatA")
  # A window reset mid-run could make util_last < util_first; clamp to 0.
  agent_budget.accrue_run(
    db, chat_id="chatA", provider="claude",
    util_first=40.0, util_last=2.0, resets_at="2026-09-01T00:00:00Z",
  )
  agent_budget.record_observation(db, "claude", 2.0, "2026-09-01T00:00:00Z")
  assert agent_budget.budget_status(db, DATA_DIR, "claude")["points_spent"] == 0.0
