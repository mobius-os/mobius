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


def _record(run_id, total, usage=None, *, session="session", start=None, chat="cost-chat"):
  return get_writer().submit(RecordRunMetrics(
    chat_id=chat, run_token=run_id, provider_session_id=session,
    cost_usd=total, cost_scope="session", usage=usage,
    session_start=start or {"kind": "resumed", "session_id": session},
  )).result(timeout=5)["cost_usd"]


FRESH = {"kind": "fresh"}


def test_writer_subtracts_snapshot_once_and_preserves_source():
  _seed_chat("cost-chat")
  _seed_run("cost-1", "cost-chat")
  assert _record("cost-1", 10, _usage(10, 10, 10), start=FRESH) == 10
  _seed_run("cost-2", "cost-chat")
  usage = _usage(13, 18, 8)
  assert _record("cost-2", 13, usage) == 3
  assert _record("cost-2", 13, usage) == 3  # Duplicate acknowledgement.
  assert _record("cost-2", 13) == 3  # Missing usage must not erase earlier tokens.
  _seed_run("cost-3", "cost-chat")
  assert _record("cost-3", 13, _usage(13, 18, 0)) == 0
  _seed_run("cost-4", "cost-chat")
  assert _record("cost-4", 2, _usage(2, 2, 2), session="new-session", start=FRESH) == 2
  # Resuming an earlier session must not use the latest *different* session.
  _seed_run("cost-5", "cost-chat")
  assert _record("cost-5", 14, _usage(14, 20, 2)) == 1
  # A delayed repeat of an old result must not subtract a successor.
  assert _record("cost-2", 13, usage) == 3
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
  assert _record("unknown-1", 10, _usage(10, 10, 10)) is None
  # Its raw snapshot is still a valid starting point for the next result.
  _seed_run("known-2", "cost-chat")
  assert _record("known-2", 13, _usage(13, 18, 8)) == 3
  _seed_run("failed-3", "cost-chat")
  with SessionLocal() as db:
    row = db.get(models.ChatRun, "failed-3")
    row.provider_execution_admitted = True
    db.commit()  # Provider died without a cost result or even a session id.
  _seed_run("unknown-4", "cost-chat")
  assert _record("unknown-4", 15, _usage(15, 20, 2)) is None
  _seed_run("known-5", "cost-chat")
  assert _record("known-5", 16, _usage(16, 21, 1)) == 1


def test_writer_unknown_session_start_does_not_guess():
  _seed_chat("cost-chat")
  _seed_run("no-start", "cost-chat")
  assert get_writer().submit(RecordRunMetrics(
    chat_id="cost-chat", run_token="no-start", provider_session_id="session",
    cost_usd=10, cost_scope="session", usage=_usage(10, 10, 10),
  )).result(timeout=5)["cost_usd"] is None


def test_writer_can_use_verified_pre_repair_snapshot():
  _seed_chat("cost-chat")
  _seed_run("legacy-1", "cost-chat")
  get_writer().submit(RecordRunMetrics(
    chat_id="cost-chat", run_token="legacy-1", provider_session_id="session",
    cost_usd=10, usage=_usage(10, 10, 10),
  )).result(timeout=5)
  _seed_run("current-2", "cost-chat")
  assert _record("current-2", 13, _usage(13, 18, 8)) == 3


def test_writer_does_not_use_a_crash_zero_as_a_later_baseline():
  _seed_chat("cost-chat")
  _seed_run("before-crash", "cost-chat")
  assert _record("before-crash", 10, _usage(10, 10, 10), start=FRESH) == 10
  _seed_run("crash-zero", "cost-chat")
  assert _record("crash-zero", 0, _usage(0, 0, 0)) is None
  _seed_run("after-crash", "cost-chat")
  assert _record("after-crash", 13, _usage(13, 14, 4)) is None
  _seed_run("measured-again", "cost-chat")
  assert _record("measured-again", 14, _usage(14, 16, 2)) == 1


def test_writer_leaves_a_restore_older_than_the_last_result_unknown():
  # The CLI saves session totals only on a normal exit. If it was killed after
  # reporting 13, the next resume restores 10 and reports 10 + 5 = 15.
  _seed_chat("cost-chat")
  _seed_run("saved", "cost-chat")
  assert _record("saved", 10, _usage(10, 10, 10), start=FRESH) == 10
  _seed_run("killed", "cost-chat")
  assert _record("killed", 13, _usage(13, 18, 8)) == 3
  _seed_run("restored-older", "cost-chat")
  assert _record("restored-older", 15, _usage(15, 15, 5)) is None  # Not 2.
  # The reported total is still what a normal exit saves for the next resume.
  _seed_run("after", "cost-chat")
  assert _record("after", 16, _usage(16, 17, 2)) == 1


def test_writer_accepts_subagent_and_background_spend_beyond_turn_usage():
  _seed_chat("cost-chat")
  _seed_run("first", "cost-chat")
  assert _record("first", 10, _usage(10, 10, 10), start=FRESH) == 10
  _seed_run("with-task", "cost-chat")
  usage = _usage(14, 30, 8)  # A subagent's requests are in model_usage only.
  usage["provider_model_usage"]["haiku"] = _usage(1, 4, 4)["provider_model_usage"]["opus"]
  usage["provider_model_usage"]["opus"]["costUSD"] = 13
  assert _record("with-task", 14, usage) == 4


def test_branch_first_turn_subtracts_its_source_session_snapshot():
  # A branch forks the source session, and Claude restores that session's
  # totals on the branch's first turn, which has no session of its own yet.
  _seed_chat("cost-chat")
  _seed_run("parent-1", "cost-chat")
  assert _record("parent-1", 10, _usage(10, 10, 10), start=FRESH) == 10
  _seed_run("parent-2", "cost-chat")
  assert _record("parent-2", 13, _usage(13, 18, 8)) == 3
  _seed_chat("branch-chat")
  _seed_run("branch-1", "branch-chat")
  fork = {"kind": "forked", "session_id": "session"}
  assert _record(
    "branch-1", 14, _usage(14, 20, 2), session="branch-session",
    start=fork, chat="branch-chat",
  ) == 1
  _seed_run("branch-2", "branch-chat")
  assert _record(
    "branch-2", 16, _usage(16, 23, 3), session="branch-session",
    chat="branch-chat",
  ) == 2
  # A fork whose source has no recorded snapshot stays unknown.
  _seed_chat("orphan-chat")
  _seed_run("orphan-1", "orphan-chat")
  assert _record(
    "orphan-1", 14, _usage(14, 20, 2), session="orphan-session",
    start={"kind": "forked", "session_id": "missing"}, chat="orphan-chat",
  ) is None


def test_chat_usage_sums_corrected_turns_not_session_snapshots(client, auth, chat):
  for run_id, total, start in (
    ("usage-1", 10, FRESH),
    ("usage-2", 13, {"kind": "resumed", "session_id": "session"}),
  ):
    _seed_run(run_id, chat.id)
    get_writer().submit(RecordRunMetrics(
      chat_id=chat.id, run_token=run_id, provider_session_id="session",
      cost_usd=total, cost_scope="session", session_start=start,
    )).result(timeout=5)
  response = client.get(f"/api/chats/{chat.id}/usage", headers=auth)
  assert response.status_code == 200
  payload = response.json()
  assert payload["totals"]["cost_usd"] == 10  # The resume has no counters to verify.
  assert payload["coverage"]["runs_with_usage"] == 0  # Cost metadata isn't token usage.
  assert payload["coverage"]["runs_by_quality"]["missing"] == 2
  assert [run["usage_quality"] for run in payload["runs"]] == ["missing", "missing"]
  assert [run["cost_usd"] for run in payload["runs"]] == [10, None]
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
  first["provider_model_usage"]["haiku"] = copy.deepcopy(second["provider_model_usage"]["haiku"])
  third = _usage(15, 22, 4)
  third["provider_model_usage"]["haiku"] = copy.deepcopy(second["provider_model_usage"]["haiku"])
  records = [
    ("cumulative", 10, first), ("cumulative", 13, second),
    ("cumulative", 15, third),
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


@pytest.mark.parametrize("auxiliary", ["missing", "reset", "unchanged"])
def test_historical_repair_preserves_ambiguous_multi_model_per_call(tmp_path, auxiliary):
  first = _usage(0, 0, 0)
  first["provider_model_usage"]["haiku"] = _usage(10, 10, 10)["provider_model_usage"]["opus"]
  second = _usage(13, 13, 13)
  if auxiliary != "missing":
    second["provider_model_usage"]["haiku"] = _usage(
      10 if auxiliary == "unchanged" else 0,
      10 if auxiliary == "unchanged" else 0, 0,
    )["provider_model_usage"]["opus"]
  cost = sum(model["costUSD"] for model in second["provider_model_usage"].values())
  eng = _migration_database(tmp_path, [("session", 10, first), ("session", cost, second)])
  _repair_claude_session_costs(eng)
  with eng.connect() as conn:
    row = conn.execute(text("SELECT cost_usd, usage_json FROM chat_runs WHERE id = '1'")).one()
  assert row.cost_usd == cost
  assert json.loads(row.usage_json) == second


@pytest.mark.parametrize("auxiliary", ["unchanged", "missing", "reset", "advanced"])
def test_historical_repair_requires_consistent_other_model_evidence(tmp_path, auxiliary):
  first = _usage(2, 10, 10)
  first["provider_model_usage"]["haiku"] = _usage(8, 8, 8)["provider_model_usage"]["opus"]
  second = _usage(5, 13, 3)
  if auxiliary != "missing":
    second["provider_model_usage"]["haiku"] = _usage(
      {"unchanged": 8, "reset": 0, "advanced": 9}[auxiliary],
      {"unchanged": 8, "reset": 0, "advanced": 9}[auxiliary], 0,
    )["provider_model_usage"]["opus"]
  cost = sum(model["costUSD"] for model in second["provider_model_usage"].values())
  eng = _migration_database(tmp_path, [("session", 10, first), ("session", cost, second)])
  _repair_claude_session_costs(eng)
  with eng.connect() as conn:
    rows = conn.execute(text("SELECT cost_usd, usage_json FROM chat_runs ORDER BY started_at")).all()
  assert rows[1].cost_usd == (3 if auxiliary == "unchanged" else cost)
  if auxiliary != "unchanged":
    assert json.loads(rows[1].usage_json) == second
  _repair_claude_session_costs(eng)
  with eng.connect() as conn:
    assert conn.execute(text("SELECT cost_usd, usage_json FROM chat_runs ORDER BY started_at")).all() == rows


def _with_haiku(usage, tokens, cost=0):
  usage["provider_model_usage"]["haiku"] = _usage(cost, tokens, 0)["provider_model_usage"]["opus"]
  usage["provider_model_usage"]["opus"]["costUSD"] -= cost
  return usage


def test_historical_repair_follows_a_proven_cumulative_session(tmp_path):
  # Once a row proves the session cumulative, later rows need only consistent
  # counters: subagent requests add to model_usage beyond the turn's own usage,
  # and Claude Code's background Haiku calls move another model's counters.
  records = [
    ("proven", 10, _with_haiku(_usage(10, 10, 10), 2)),
    ("proven", 13, _with_haiku(_usage(13, 18, 8), 2)),  # Exact proof.
    ("proven", 17, _with_haiku(_usage(17, 30, 6), 2)),  # Subagent spend.
    ("proven", 19, _with_haiku(_usage(19, 34, 4), 5, cost=1)),  # Haiku moved.
    ("proven", 20, _with_haiku(_usage(20, 34, 9), 5, cost=1)),  # Does not cover.
    ("proven", 22, _with_haiku(_usage(22, 30, 2), 5, cost=1)),  # Counter regressed.
    # The same later rows without an earlier proof keep their totals.
    ("unproven", 10, _with_haiku(_usage(10, 10, 10), 2)),
    ("unproven", 17, _with_haiku(_usage(17, 30, 6), 2)),
    ("unproven", 19, _with_haiku(_usage(19, 34, 4), 5, cost=1)),
  ]
  eng = _migration_database(tmp_path, records)
  _repair_claude_session_costs(eng)
  with eng.connect() as conn:
    rows = conn.execute(text("SELECT cost_usd FROM chat_runs ORDER BY started_at")).all()
  assert [row.cost_usd for row in rows] == [10, 3, 4, 2, 20, 22, 10, 17, 19]
  _repair_claude_session_costs(eng)
  with eng.connect() as conn:
    again = conn.execute(text("SELECT cost_usd FROM chat_runs ORDER BY started_at")).all()
  assert again == rows
