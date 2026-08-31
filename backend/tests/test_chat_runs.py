"""Durable per-run state tests.

`chat_runs` is the sole durable state: one row per turn keyed by run_token,
closed terminal on a clean turn end and marked interrupted by boot
reconciliation when a process died mid-turn.

These drive the REAL `get_writer()` actor (the conftest `fresh_db` fixture
starts one bound to the test DB) and the real `reconcile_interrupted_chats`, so
they cover the wired lifecycle + reconciliation maintenance, not a mock.
"""

import asyncio
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta

from app import chat as chat_mod
from app import models
from app.chat_writer import (
  AppendPending, Barrier, FinishRun, PromotePending, StartTurn,
  RecordRunMetrics, alloc_run_token, get_writer,
)
from app.database import SessionLocal


def _seed_chat(chat_id, messages=None, pending=None):
  db = SessionLocal()
  try:
    chat = models.Chat(
      id=chat_id, title="t",
      messages=messages if messages is not None else [],
      pending_messages=pending if pending is not None else [],
      session_id="sess", provider="claude",
    )
    db.add(chat)
    db.commit()
  finally:
    db.close()
  return chat_id


def _seed_run(run_id, chat_id, status="running"):
  from datetime import UTC, datetime

  db = SessionLocal()
  try:
    db.add(models.ChatRun(
      id=run_id, chat_id=chat_id, status=status, provider="claude",
      started_at=datetime.now(UTC),
    ))
    db.commit()
  finally:
    db.close()


def _runs(chat_id):
  """Return {run_id: (status, ended_is_set)} for a chat's run records."""
  db = SessionLocal()
  try:
    rows = (
      db.query(models.ChatRun)
      .filter(models.ChatRun.chat_id == chat_id)
      .all()
    )
    return {r.id: (r.status, r.ended_at is not None) for r in rows}
  finally:
    db.close()


def _active_status(chat_id):
  db = SessionLocal()
  try:
    from app.run_state import latest_run
    run = latest_run(db, chat_id)
    return (
      run.status
      if run is not None and run.status in models.NONTERMINAL_RUN_STATUSES
      else None
    )
  finally:
    db.close()


def _goal_objective(chat_id):
  db = SessionLocal()
  try:
    from app.run_state import latest_run
    run = latest_run(db, chat_id)
    return run.goal_objective if run is not None else None
  finally:
    db.close()


def _drain():
  get_writer().submit(Barrier()).result(timeout=5)


def _start(chat_id, run_token):
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_token,
    user_msg={"role": "user", "content": "hi", "ts": 1},
    title_source="hi", default_provider="claude",
  )).result(timeout=5)


# -- durable start --------------------------------------------------------
def test_start_turn_opens_a_running_run_record():
  _seed_chat("r1")
  _start("r1", "rt-1")
  _drain()
  runs = _runs("r1")
  assert runs == {"rt-1": ("running", False)}
  assert _active_status("r1") == "running"


def test_only_semantic_continuation_inherits_goal_identity():
  _seed_chat("r-goal")
  get_writer().submit(StartTurn(
    chat_id="r-goal", run_token="rt-goal",
    user_msg={
      "role": "user", "content": "/goal finish the migration", "ts": 1,
    },
    title_source="goal", default_provider="codex",
  )).result(timeout=5)
  assert _goal_objective("r-goal") == "finish the migration"

  get_writer().submit(FinishRun(
    chat_id="r-goal",
    run_token="rt-goal",
    terminal_status="interrupted",
  )).result(timeout=5)
  get_writer().submit(StartTurn(
    chat_id="r-goal", run_token="rt-resume",
    user_msg={
      "role": "user", "content": "continue", "ts": 2,
      "kind": "continuation", "continuation_reason": "manual",
    },
    title_source="continue", default_provider="codex",
  )).result(timeout=5)
  assert _goal_objective("r-goal") == "finish the migration"

  get_writer().submit(FinishRun(
    chat_id="r-goal",
    run_token="rt-resume",
    terminal_status="interrupted",
  )).result(timeout=5)
  get_writer().submit(StartTurn(
    chat_id="r-goal", run_token="rt-plain-continue",
    user_msg={"role": "user", "content": "continue", "ts": 3},
    title_source="continue", default_provider="codex",
  )).result(timeout=5)
  assert _goal_objective("r-goal") is None


def test_completed_goal_does_not_leak_into_an_ordinary_later_run():
  _seed_chat("r-complete-goal")
  get_writer().submit(StartTurn(
    chat_id="r-complete-goal", run_token="rt-old-goal",
    user_msg={"role": "user", "content": "/goal old work", "ts": 1},
    title_source="goal", default_provider="codex",
  )).result(timeout=5)
  get_writer().submit(FinishRun(
    chat_id="r-complete-goal", run_token="rt-old-goal",
  )).result(timeout=5)
  get_writer().submit(StartTurn(
    chat_id="r-complete-goal", run_token="rt-ordinary",
    user_msg={"role": "user", "content": "what happened?", "ts": 2},
    title_source="ordinary", default_provider="codex",
  )).result(timeout=5)
  assert _goal_objective("r-complete-goal") is None


def test_stopped_goal_does_not_leak_into_a_later_question_answer():
  _seed_chat("r-stopped-goal")
  get_writer().submit(StartTurn(
    chat_id="r-stopped-goal", run_token="rt-stopped-goal",
    user_msg={
      "role": "user", "content": "/goal old work", "ts": 1,
    },
    title_source="goal", default_provider="codex",
  )).result(timeout=5)
  get_writer().submit(FinishRun(
    chat_id="r-stopped-goal",
    run_token="rt-stopped-goal",
    terminal_status="stopped",
  )).result(timeout=5)
  get_writer().submit(StartTurn(
    chat_id="r-stopped-goal", run_token="rt-later-answer",
    user_msg={
      "role": "user",
      "content": "- Pick one: b",
      "kind": "continuation",
      "continuation_reason": "question_answer",
      "hidden": True,
      "ts": 2,
    },
    title_source="answer", default_provider="codex",
  )).result(timeout=5)
  assert _goal_objective("r-stopped-goal") is None


# -- clean close ----------------------------------------------------------
def test_finish_run_completes_the_run_record():
  _seed_chat("r2")
  _start("r2", "rt-2")
  get_writer().submit(
    FinishRun(chat_id="r2", run_token="rt-2")
  ).result(timeout=5)
  _drain()
  runs = _runs("r2")
  assert runs["rt-2"] == ("completed", True)
  assert _active_status("r2") is None


def test_project_agent_completion_advances_recents_and_reconnect_cursor():
  old_activity = datetime.now(UTC) - timedelta(days=2)
  db = SessionLocal()
  try:
    db.add(models.Project(
      id="project-recency", name="Project recency", project_type="blank",
      root_path="projects/project-recency", template_snapshot_json={},
    ))
    db.add(models.Chat(
      id="project-agent", title="Builder", messages=[], pending_messages=[],
      session_id="sess", provider="claude", project_id="project-recency",
      activity_at=old_activity,
    ))
    db.add(models.ProjectWorkClaim(
      id="agent-claim", project_id="project-recency",
      actor_key="agent:project-agent", actor_kind="agent",
      display_name="Builder", chat_id="project-agent", path="index.html",
      summary="Editing index.html", updated_at=datetime.now(UTC),
      expires_at=datetime.now(UTC) + timedelta(minutes=30),
    ))
    db.add(models.ChatRun(
      id="project-run", chat_id="project-agent", status="running",
      provider="claude", started_at=datetime.now(UTC),
    ))
    db.commit()
  finally:
    db.close()

  get_writer().submit(FinishRun(
    chat_id="project-agent", run_token="project-run",
  )).result(timeout=5)
  _drain()

  db = SessionLocal()
  try:
    chat = db.get(models.Chat, "project-agent")
    assert chat.activity_at.replace(tzinfo=UTC) > old_activity
    change = db.query(models.ProjectChange).filter(
      models.ProjectChange.project_id == "project-recency",
    ).one()
    assert change.kind == "agent_run_completed"
    assert change.actor_key == "agent:project-agent"
    assert db.query(models.ProjectWorkClaim).filter(
      models.ProjectWorkClaim.project_id == "project-recency",
    ).count() == 0
  finally:
    db.close()


def test_finish_run_preserves_failed_outcome():
  """A provider-error turn closes durably as failed."""
  _seed_chat("r2-failed")
  _start("r2-failed", "rt-2-failed")
  get_writer().submit(FinishRun(
    chat_id="r2-failed",
    run_token="rt-2-failed",
    terminal_status="failed",
  )).result(timeout=5)
  _drain()
  assert _runs("r2-failed")["rt-2-failed"] == ("failed", True)
  assert _active_status("r2-failed") is None


def test_record_run_metrics_updates_exact_run_without_touching_transcript():
  _seed_chat("r-metrics", messages=[{
    "role": "user", "content": "keep me", "ts": 1,
  }])
  _seed_run("rt-metrics", "r-metrics")

  get_writer().submit(RecordRunMetrics(
    chat_id="r-metrics",
    run_token="rt-metrics",
    provider_session_id="provider-thread",
    cost_usd=0.125,
    usage={
      "provider": "codex",
      "input_tokens": 900,
      "output_tokens": 200,
      "cache_read_input_tokens": 500,
      "cache_creation_input_tokens": 0,
      "reasoning_output_tokens": 100,
      "total_tokens": 1_100,
      "model_context_window": 200_000,
    },
  )).result(timeout=5)

  db = SessionLocal()
  try:
    run = db.query(models.ChatRun).filter(
      models.ChatRun.id == "rt-metrics",
    ).one()
    chat = db.query(models.Chat).filter(models.Chat.id == "r-metrics").one()
    assert run.provider_session_id == "provider-thread"
    assert run.cost_usd == 0.125
    assert run.input_tokens == 900
    assert run.output_tokens == 200
    assert run.cache_read_input_tokens == 500
    assert run.reasoning_output_tokens == 100
    assert run.total_tokens == 1_100
    assert run.model_context_window == 200_000
    assert run.usage_json["provider"] == "codex"
    assert chat.messages == [{"role": "user", "content": "keep me", "ts": 1}]
  finally:
    db.close()


def test_record_run_metrics_keeps_session_identity_and_explicit_zero_cost(
  monkeypatch,
):
  commands = []

  class Writer:
    def submit(self, command):
      commands.append(command)
      ack = Future()
      ack.set_result(True)
      return ack

  monkeypatch.setattr(chat_mod, "get_writer", lambda: Writer())

  asyncio.run(chat_mod._record_run_metrics(
    chat_id="r-metrics",
    run_token="rt-session-only",
    provider_session_id="provider-thread",
    cost_usd=0,
    usage=None,
  ))
  asyncio.run(chat_mod._record_run_metrics(
    chat_id="r-metrics",
    run_token="rt-empty",
    provider_session_id=None,
    cost_usd=None,
    usage=None,
  ))

  assert len(commands) == 1
  assert commands[0].provider_session_id == "provider-thread"
  assert commands[0].cost_usd == 0
  assert commands[0].usage is None


# -- continuation handoff -------------------------------------------------
def test_promote_closes_prior_run_and_opens_the_continuation():
  _seed_chat("r3")
  _start("r3", "rt-3a")
  # Queue a follow-up, then promote it as the continuation under a new token.
  get_writer().submit(AppendPending(
    chat_id="r3", run_token="rt-3a",
    user_msg={"role": "user", "content": "next", "ts": 2},
  )).result(timeout=5)
  get_writer().submit(
    PromotePending(chat_id="r3", run_token="rt-3b")
  ).result(timeout=5)
  _drain()
  runs = _runs("r3")
  # The prior run is closed completed; the continuation is the live record.
  assert runs["rt-3a"] == ("completed", True)
  assert runs["rt-3b"] == ("running", False)
  assert _active_status("r3") == "running"


def test_error_handoff_marks_prior_run_failed_before_continuation():
  """Queued work may continue after a provider error, but that continuation
  must not rewrite the failed turn's observability row as successful."""
  _seed_chat("r3-failed")
  _start("r3-failed", "rt-3-failed-a")
  get_writer().submit(AppendPending(
    chat_id="r3-failed", run_token="rt-3-failed-a",
    user_msg={"role": "user", "content": "next", "ts": 2},
  )).result(timeout=5)
  get_writer().submit(PromotePending(
    chat_id="r3-failed",
    run_token="rt-3-failed-b",
    ending_status="failed",
  )).result(timeout=5)
  _drain()
  runs = _runs("r3-failed")
  assert runs["rt-3-failed-a"] == ("failed", True)
  assert runs["rt-3-failed-b"] == ("running", False)
  assert _active_status("r3-failed") == "running"


# -- identity-keyed dying-run clear ---------------------------------------
def test_dying_run_finish_closes_own_record_but_keeps_successor():
  """A dying run's late finish must not touch its durable successor."""
  _seed_chat("r4")
  _start("r4", "rt-4a")
  _start("r4", "rt-4b")          # fresh turn supersedes: rt-4a → interrupted
  # The dying rt-4a now issues its late finish.
  get_writer().submit(
    FinishRun(chat_id="r4", run_token="rt-4a")
  ).result(timeout=5)
  _drain()
  runs = _runs("r4")
  assert runs["rt-4a"][0] == "interrupted"  # superseded by the fresh start
  assert runs["rt-4b"] == ("running", False)  # successor untouched
  assert _active_status("r4") == "running"


# -- tokenless clear closes everything still running ----------------------
def test_tokenless_clear_closes_all_running_records():
  _seed_chat("r5")
  _start("r5", "rt-5")
  # A tokenless clear (Stop with no live handle / reconciliation handoff)
  # takes the chat idle and closes every still-running record.
  get_writer().submit(
    FinishRun(chat_id="r5", run_token="")
  ).result(timeout=5)
  _drain()
  assert _runs("r5")["rt-5"] == ("completed", True)
  assert _active_status("r5") is None


# -- reconciliation maintains the record ----------------------------------
def test_reconcile_marks_interrupted_run_record():
  """A running durable row with no live registry entry is reconciled."""
  _seed_chat(
    "r6", messages=[{"role": "user", "content": "hi", "ts": 1}],
  )
  _seed_run("rt-6", "r6", status="running")
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "r6" in reconciled
  assert _runs("r6")["rt-6"][0] == "interrupted"
  assert _active_status("r6") is None


def test_reconcile_uses_running_row_as_the_recovery_authority():
  """A running ChatRun is sufficient durable evidence for transcript repair."""
  _seed_chat(
    "r7", messages=[{"role": "user", "content": "hi", "ts": 1}],
  )
  _seed_run("rt-7", "r7", status="running")
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "r7" in reconciled
  assert _runs("r7")["rt-7"][0] == "interrupted"
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == "r7").first()
    assert [message["role"] for message in chat.messages] == [
      "user", "assistant",
    ]
  finally:
    db.close()


# -- restart-stable run-token identity (PK-reuse regression) --------------
def test_run_token_is_restart_stable_and_unique():
  """The run_token IS the chat_runs PK, so it must never be reissued — a
  process-local counter resets to rt-1 on restart and collides with surviving
  terminal rows. Random hex tokens are unique and not a small reusable int."""
  tokens = {alloc_run_token() for _ in range(2000)}
  assert len(tokens) == 2000, "tokens must be unique (no reuse)"
  sample = next(iter(tokens))
  assert sample.startswith("rt-")
  assert sample not in {"rt-1", "rt-2", "rt-3"}, "must not be a small counter"


def test_start_turn_coexists_with_surviving_terminal_run_records():
  """Post-restart realism: a chat carries terminal run records from a prior
  process incarnation. A fresh turn's restart-stable token lets StartTurn open
  a NEW running record alongside them with no chat_runs PK collision — the
  regression a per-process counter caused (reissued rt-1 → IntegrityError →
  the turn silently failed to start)."""
  _seed_chat("rr")
  _seed_run("rt-1", "rr", status="completed")    # a surviving prior-process PK
  _seed_run("rt-2", "rr", status="interrupted")
  token = alloc_run_token()
  get_writer().submit(StartTurn(
    chat_id="rr", run_token=token,
    user_msg={"role": "user", "content": "hi", "ts": 1},
    title_source="hi", default_provider="claude",
  )).result(timeout=5)
  _drain()
  runs = _runs("rr")
  assert runs[token] == ("running", False), "fresh turn opened, no PK collision"
  assert _active_status("rr") == "running"


# -- orphan sweep must not touch a live chat ------------------------------
def test_orphan_sweep_skips_a_live_chat():
  """The boot orphan sweep closes a dead chat's lingering running record but
  must NOT touch a chat the registry reports alive (the is_alive `continue`)."""
  _seed_chat("dead")
  _seed_run("rt-dead", "dead", status="running")
  _seed_chat("live")
  _seed_run("rt-live", "live", status="running")
  chat_mod.registry.mark_starting("live")  # live chat is mid-spawn
  db = SessionLocal()
  try:
    chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  try:
    assert _runs("dead")["rt-dead"][0] == "interrupted"
    assert _runs("live")["rt-live"][0] == "running", "live chat untouched"
  finally:
    chat_mod.registry.discard_starting("live")
