"""Restored Claude session spend is counted once, including across restarts."""

import copy
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app import models
from app.claude_costs import session_cost_snapshot, turn_cost
from app.chat_writer import RecordRunMetrics, get_writer
from app.database import SessionLocal
from app.schema_migrations import _repair_claude_session_costs
from tests.test_chat_runs import _seed_chat, _seed_run


def _usage(total, tokens, turn_tokens):
  return {
    "provider": "claude", "input_tokens": turn_tokens,
    "output_tokens": turn_tokens, "total_tokens": 2 * turn_tokens,
    "provider_usage": {
      "input_tokens": turn_tokens, "output_tokens": turn_tokens,
      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    },
    "provider_model_usage": {"opus": {
      "costUSD": total, "inputTokens": tokens, "outputTokens": tokens,
      "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
    }},
  }


@pytest.mark.parametrize("total,baseline,expected", [
  (13, 10, 3), (13, 13, 0), (13, None, None),
  (0, 13, None), (float("nan"), 0, None),
  (float("inf"), 0, None), (True, 0, None), (-1, 0, None),
])
def test_turn_cost_preserves_zero_and_unknown(total, baseline, expected):
  assert turn_cost(total, baseline) == expected


def test_snapshot_requires_raw_evidence_and_rejects_crash_regression():
  usage = _usage(13, 20, 10)
  assert session_cost_snapshot(usage, 13) == 13
  assert session_cost_snapshot(usage, 3) is None
  usage["provider_cost"] = {"scope": "session", "total_usd": 13, "baseline_usd": 10}
  assert session_cost_snapshot(usage, 3) == 13
  usage["provider_cost"]["total_usd"] = 10 - 1e-12
  assert session_cost_snapshot(usage, 0) == 10 - 1e-12
  usage["provider_cost"]["total_usd"] = 0
  assert session_cost_snapshot(usage, 0) is None


def _record(run_id, total, *, session="session", resumed="session", usage=None):
  return get_writer().submit(RecordRunMetrics(
    chat_id="cost-chat", run_token=run_id, provider_session_id=session,
    cost_usd=total, cost_scope="session", resumed_session_id=resumed, usage=usage,
  )).result(timeout=5)["cost_usd"]


def test_writer_subtracts_snapshot_once_and_preserves_source():
  _seed_chat("cost-chat")
  _seed_run("cost-1", "cost-chat")
  assert _record("cost-1", 10, resumed=None, usage=_usage(10, 10, 10)) == 10
  _seed_run("cost-2", "cost-chat")
  usage = _usage(13, 18, 8)
  assert _record("cost-2", 13, usage=usage) == 3
  assert _record("cost-2", 13, usage=usage) == 3  # Duplicate acknowledgement.
  assert _record("cost-2", 13) == 3  # Missing usage must not erase earlier tokens.
  _seed_run("cost-3", "cost-chat")
  assert _record("cost-3", 13, usage=_usage(13, 18, 0)) == 0
  _seed_run("cost-4", "cost-chat")
  assert _record("cost-4", 2, session="new-session") == 2
  # Resuming an earlier session must not use the latest *different* session.
  _seed_run("cost-5", "cost-chat")
  assert _record("cost-5", 14) == 1
  # A delayed repeat of an old result must not subtract a successor.
  assert _record("cost-2", 13, usage=usage) == 3
  with SessionLocal() as db:
    row = db.get(models.ChatRun, "cost-2")
    assert row.cost_usd == 3
    assert row.input_tokens == 8
    assert row.usage_json["provider_model_usage"] == usage["provider_model_usage"]
    assert row.usage_json["provider_cost"] == {
      "scope": "session", "total_usd": 13, "baseline_usd": 10,
    }
  assert "provider_cost" not in usage  # Caller snapshot was not mutated.


def test_writer_unknown_resume_and_failed_gap_do_not_guess():
  _seed_chat("cost-chat")
  _seed_run("unknown-1", "cost-chat")
  assert _record("unknown-1", 10) is None
  # Its raw snapshot is still a valid starting point for the next result.
  _seed_run("known-2", "cost-chat")
  assert _record("known-2", 13) == 3
  _seed_run("failed-3", "cost-chat")
  with SessionLocal() as db:
    row = db.get(models.ChatRun, "failed-3")
    row.provider_execution_admitted = True
    db.commit()  # Provider died without a cost result or even a session id.
  _seed_run("unknown-4", "cost-chat")
  assert _record("unknown-4", 15) is None
  _seed_run("known-5", "cost-chat")
  assert _record("known-5", 16) == 1


def test_writer_can_use_verified_pre_repair_snapshot():
  _seed_chat("cost-chat")
  _seed_run("legacy-1", "cost-chat")
  get_writer().submit(RecordRunMetrics(
    chat_id="cost-chat", run_token="legacy-1", provider_session_id="session",
    cost_usd=10, usage=_usage(10, 10, 10),
  )).result(timeout=5)
  _seed_run("current-2", "cost-chat")
  assert _record("current-2", 13) == 3


def test_writer_does_not_use_a_crash_zero_as_a_later_baseline():
  _seed_chat("cost-chat")
  _seed_run("before-crash", "cost-chat")
  assert _record("before-crash", 10, resumed=None) == 10
  _seed_run("crash-zero", "cost-chat")
  assert _record("crash-zero", 0) is None
  _seed_run("after-crash", "cost-chat")
  assert _record("after-crash", 13) is None
  _seed_run("measured-again", "cost-chat")
  assert _record("measured-again", 14) == 1


def test_chat_usage_sums_corrected_turns_not_session_snapshots(client, auth, chat):
  for run_id, total, resumed in (("usage-1", 10, None), ("usage-2", 13, "session")):
    _seed_run(run_id, chat.id)
    get_writer().submit(RecordRunMetrics(
      chat_id=chat.id, run_token=run_id, provider_session_id="session",
      cost_usd=total, cost_scope="session", resumed_session_id=resumed,
    )).result(timeout=5)
  response = client.get(f"/api/chats/{chat.id}/usage", headers=auth)
  assert response.status_code == 200
  payload = response.json()
  assert payload["totals"]["cost_usd"] == 13
  assert payload["coverage"]["runs_with_usage"] == 0  # Cost metadata isn't token usage.
  assert payload["coverage"]["runs_by_quality"]["missing"] == 2
  assert [run["usage_quality"] for run in payload["runs"]] == ["missing", "missing"]
  assert [run["cost_usd"] for run in payload["runs"]] == [10, 3]
  assert [run["usage"]["provider_cost"]["total_usd"]
          for run in payload["runs"]] == [10, 13]


def _migration_database(tmp_path, records):
  eng = create_engine(f"sqlite:///{tmp_path / 'costs.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE chat_runs (id TEXT PRIMARY KEY, chat_id TEXT, provider TEXT, "
      "provider_session_id TEXT, started_at TIMESTAMP, cost_usd FLOAT, usage_json JSON)"
    ))
    for index, (session, cost, usage) in enumerate(records):
      conn.execute(text(
        "INSERT INTO chat_runs VALUES (:id, 'chat', 'claude', :session, :started, :cost, :usage)"
      ), {"id": str(index), "session": session, "cost": cost,
          "started": datetime(2026, 10, 27) + timedelta(seconds=index),
          "usage": json.dumps(usage)})
  return eng


def test_historical_migration_proves_deltas_and_is_idempotent(tmp_path):
  first = _usage(10, 10, 10)
  second = _usage(13, 18, 8)
  second["provider_model_usage"]["haiku"] = {
    "costUSD": 0, "inputTokens": 3, "outputTokens": 1,
    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
  }
  records = [
    ("cumulative", 10, first), ("cumulative", 13, second),
    ("cumulative", 15, _usage(15, 22, 4)),
    ("per-call", 2, _usage(2, 10, 10)),
    ("per-call", 3, _usage(3, 20, 20)),
    # Unexplained counters must not be repaired by dollar monotonicity alone.
    ("ambiguous", 10, _usage(10, 10, 10)),
    ("ambiguous", 13, _usage(13, 99, 8)),
    # A counter reset is a new call, never a negative/clamped spend.
    ("reset", 10, _usage(10, 10, 10)),
    ("reset", 1, _usage(1, 2, 2)),
    # Missing prior telemetry cannot establish a cumulative baseline.
    ("unknown", 10, None), ("unknown", 13, _usage(13, 30, 8)),
  ]
  originals = copy.deepcopy(records)
  eng = _migration_database(tmp_path, records)
  _repair_claude_session_costs(eng)
  with eng.connect() as conn:
    rows = conn.execute(text("SELECT * FROM chat_runs ORDER BY started_at")).mappings().all()
  assert [r["cost_usd"] for r in rows] == [10, 3, 2, 2, 3, 10, 13, 10, 1, 10, 13]
  repaired = json.loads(rows[1]["usage_json"])
  assert repaired.pop("provider_cost") == {
    "scope": "session", "total_usd": 13,
    "baseline_usd": 10, "historical_repair": True,
  }
  assert repaired == originals[1][2]
  assert json.loads(rows[6]["usage_json"]) == originals[6][2]
  _repair_claude_session_costs(eng)
  with eng.connect() as conn:
    again = conn.execute(text("SELECT * FROM chat_runs ORDER BY started_at")).mappings().all()
  assert again == rows


def test_historical_repair_does_not_change_pre_telemetry_tables(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'pre-telemetry.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE chat_runs (id TEXT PRIMARY KEY, chat_id TEXT)"))
    conn.execute(text("INSERT INTO chat_runs VALUES ('old-run', 'old-chat')"))
  _repair_claude_session_costs(eng)
  with eng.connect() as conn:
    assert list(conn.execute(text("SELECT * FROM chat_runs"))) == [("old-run", "old-chat")]
