"""Hot paths caught stalling the event loop keep their database work off it.

The stall recorder caught the event loop frozen for seconds inside these
paths' database reads while the disk was saturated. On the loop, one slow read
freezes every request and stream; in a worker thread it only slows its own.
"""

import asyncio

import pytest

from app import delegations
from app.routes import apps as apps_routes


def _running_on_event_loop() -> bool:
  try:
    asyncio.get_running_loop()
  except RuntimeError:
    return False
  return True


def test_dependency_sweep_selects_off_loop_after_reconciliation(monkeypatch):
  from concurrent.futures import Future
  from app import goal_dependencies, chat_writer

  seen = []

  def pending():
    seen.append(("pending", _running_on_event_loop()))
    return True

  def submit(command):
    assert isinstance(command, chat_writer.ReconcileClaimSources)
    seen.append(("reconcile", _running_on_event_loop()))
    result = Future()
    result.set_result(None)
    return result

  def select():
    seen.append(("select", _running_on_event_loop()))
    return ["dependency"], {"chat"}

  async def deliver(row_id):
    assert row_id == "dependency"
    seen.append(("deliver", _running_on_event_loop()))
    return True

  async def cleanup(chat_id):
    assert chat_id == "chat"
    seen.append(("cleanup", _running_on_event_loop()))

  monkeypatch.setattr(goal_dependencies, "_has_pending_claim_dependencies", pending)
  monkeypatch.setattr(goal_dependencies, "_select_dependency_deliveries", select)
  monkeypatch.setattr(goal_dependencies, "deliver_dependency", deliver)
  monkeypatch.setattr(goal_dependencies, "withdraw_disposed_dependency_notices", cleanup)
  monkeypatch.setattr(chat_writer, "get_writer",
    lambda: type("Writer", (), {"submit": staticmethod(submit)}))

  assert asyncio.run(goal_dependencies.deliver_pending_dependencies()) == 1
  assert seen == [("pending", False), ("reconcile", True), ("select", False),
    ("deliver", True), ("cleanup", True)]


def test_stop_following_reads_in_a_worker_owned_session(db, chat, client, auth, monkeypatch):
  from app import database
  from tests.test_decision_dependencies import _setup, _follow

  _setup(db, chat)
  row = _follow(db, chat)
  seen = []
  real_session = database.SessionLocal

  def session():
    seen.append(_running_on_event_loop())
    return real_session()

  monkeypatch.setattr(database, "SessionLocal", session)
  response = client.post(f"/api/chat-waits/dependencies/{row.id}/withdraw", headers=auth)

  assert response.status_code == 200, response.text
  assert seen and not any(seen)


def test_parent_wake_reads_the_parent_off_the_event_loop(monkeypatch):
  seen = []

  def plan(parent_chat_id, source_work_id):
    seen.append(_running_on_event_loop())
    return None

  monkeypatch.setattr(delegations, "_plan_parent_wake", plan)

  assert asyncio.run(
    delegations._deliver_parent_wake_once("parent-chat", "source-run"),
  ) is False
  assert seen == [False]


def test_parent_wake_starts_the_planned_continuation_on_the_loop(monkeypatch):
  import app.chat as chat_mod
  import app.chat_start as chat_start

  plan = delegations._ParentWakePlan(
    root_run_id="root", run_token="token", source_work_id="source-run",
    activity_id="activity",
  )
  monkeypatch.setattr(delegations, "_plan_parent_wake", lambda *_a: plan)
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: False)
  started = []

  async def start(**kwargs):
    started.append((_running_on_event_loop(), kwargs))
    return True

  monkeypatch.setattr(chat_start, "start_programmatic_activity_continuation", start)

  assert asyncio.run(
    delegations._deliver_parent_wake_once("parent-chat", "source-run"),
  ) is True
  assert started == [(True, {
    "chat_id": "parent-chat", "root_run_id": "root", "run_token": "token",
    "source_work_id": "source-run", "activity_id": "activity",
    "_transition_lock_held": True,
  })]


def test_parent_wake_skips_the_history_scan_without_a_running_run(
  chat, db, monkeypatch,
):
  # The pre-upgrade carrier it looks for always owns a running ChatRun in the
  # parent; without one, rereading the whole transcript every recovery pass
  # is pure cost that grows with the chat.
  from app import models

  read = []
  real_history = delegations.transcript_rows.history

  def history(parent):
    read.append(parent.id)
    return real_history(parent)

  monkeypatch.setattr(delegations.transcript_rows, "history", history)

  assert delegations._plan_parent_wake(chat.id, "source-run") is None
  assert read == []

  db.add(models.ChatRun(
    id="orphan-run", root_run_id="orphan-run", chat_id=chat.id,
    status="running", provider="claude", provider_execution_admitted=False,
  ))
  db.commit()

  assert delegations._plan_parent_wake(chat.id, "source-run") is None
  assert read == [chat.id]


def test_app_list_reads_run_off_the_event_loop(client, auth, monkeypatch):
  seen = []
  real_list = apps_routes._live_apps_for_drawer
  real_tombstones = apps_routes._has_expired_app_tombstone

  def live_apps(db):
    seen.append(("list", _running_on_event_loop()))
    return real_list(db)

  def tombstones(db, cutoff):
    seen.append(("tombstones", _running_on_event_loop()))
    return real_tombstones(db, cutoff)

  monkeypatch.setattr(apps_routes, "_live_apps_for_drawer", live_apps)
  monkeypatch.setattr(apps_routes, "_has_expired_app_tombstone", tombstones)

  assert client.get("/api/apps/", headers=auth).status_code == 200
  assert seen == [("tombstones", False), ("list", False)]


def _freeze_provider_clock(monkeypatch, start=1_000_000.0):
  """A controllable monotonic clock for the app-provider throttle only."""
  import time as real_time
  from app import providers

  clock = [start]

  class Clock:
    def monotonic(self):
      return clock[0]

    def __getattr__(self, name):
      return getattr(real_time, name)

  monkeypatch.setattr(providers, "time", Clock())
  return clock


def test_turn_start_reads_off_the_loop_and_keeps_a_concurrent_pick(monkeypatch):
  # Each of these froze the loop at turn start in the stall recorder: the
  # app-provider declarations, the peer roster and inbox, the helper status
  # hint, the recency transcript scan and the app activity manifests. The
  # four context reads share one worker session, and a model pick that lands
  # while they run wins over the first-send settings snapshot.
  from app import agent_activity_provider, agent_coordination, models, providers
  from app.database import SessionLocal
  import app.chat as chat_mod
  from tests.test_terminal_completion import (
    _patch_claude_runner, _run_real_chat, _seed_chat, _seed_owner_and_creds,
  )

  chat_id = "turn-start-off-loop"
  _seed_owner_and_creds()
  _seed_chat(
    chat_id, messages=[{"role": "user", "content": "hi", "ts": 1}],
    pending=[], running="running", run_token="rt-turn-start-off-loop",
  )
  with SessionLocal() as db:
    db.get(models.Chat, chat_id).agent_settings_json = None
    db.commit()
  picked = {"model": "claude-opus-4-8", "effort": "low"}

  context = []

  def record(module, name):
    real = getattr(module, name)

    def wrapper(db, *args, **kwargs):
      context.append((name, _running_on_event_loop(), id(db)))
      return real(db, *args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)

  record(agent_coordination, "build_coordination_context_delivery")
  record(delegations, "active_parent_context")
  record(chat_mod, "_last_user_message_elapsed")
  record(agent_activity_provider, "resolve_agent_activity_binding")
  real_read_context = chat_mod._read_turn_context

  def read_context_with_concurrent_pick(*args):
    with SessionLocal() as db:
      db.get(models.Chat, chat_id).agent_settings_json = picked
      db.commit()
    return real_read_context(*args)

  monkeypatch.setattr(
    chat_mod, "_read_turn_context", read_context_with_concurrent_pick,
  )

  # The declaration TTL lapses during a slow preflight: provider resolution
  # must still never read declarations on the loop.
  clock = _freeze_provider_clock(monkeypatch)
  declaration_reads = []
  real_declarations = providers._read_app_model_declarations

  def read_declarations():
    declaration_reads.append(_running_on_event_loop())
    return real_declarations()

  monkeypatch.setattr(
    providers, "_read_app_model_declarations", read_declarations,
  )
  real_policy = delegations.policy_for_chat

  def slow_policy(*args, **kwargs):
    clock[0] += 5.0
    return real_policy(*args, **kwargs)

  monkeypatch.setattr(delegations, "policy_for_chat", slow_policy)

  _patch_claude_runner(monkeypatch)
  chat_mod.mark_starting(chat_id)
  _run_real_chat(
    chat_id, run_token="rt-turn-start-off-loop",
    run_gen=chat_mod.current_run_generation(chat_id),
  )

  assert declaration_reads == [False]
  assert {name for name, _, _ in context} == {
    "build_coordination_context_delivery",
    "active_parent_context",
    "_last_user_message_elapsed",
    "resolve_agent_activity_binding",
  }
  assert {on_loop for _, on_loop, _ in context} == {False}
  assert len({session for _, _, session in context}) == 1
  with SessionLocal() as db:
    assert db.get(models.Chat, chat_id).agent_settings_json == picked


def test_turn_context_uses_liveness_captured_on_the_loop(db, monkeypatch):
  # The runner registry belongs to the loop; the worker gets a snapshot.
  from app import agent_coordination
  import app.chat as chat_mod
  from app.runner_registry import registry
  from tests.test_agent_coordination import _network_fixture

  chats, _ = _network_fixture(db)
  alive = frozenset(registry.all_alive_chat_ids())
  expected = agent_coordination.build_coordination_context_delivery(
    db, chats["scout"].id, "scout-run",
  )

  def forbidden(_chat_id):
    raise AssertionError("worker read the loop-owned runner registry")

  monkeypatch.setattr(registry, "is_alive", forbidden)
  reads = asyncio.run(asyncio.to_thread(
    chat_mod._read_turn_context, chats["scout"].id, "scout-run", alive,
  ))

  assert reads.coordination == expected
  assert chats["builder"].id in reads.coordination.text


def test_slow_app_provider_load_is_fresh_from_its_completion(monkeypatch):
  # Stamping the throttle when a load started let a load slower than the
  # window send the next synchronous get_provider back to the database.
  from app import providers

  clock = _freeze_provider_clock(monkeypatch)
  reads = []

  def slow_read():
    reads.append(_running_on_event_loop())
    clock[0] += 5.0
    return []

  monkeypatch.setattr(providers, "_read_app_model_declarations", slow_read)

  asyncio.run(providers.refresh_app_model_providers("/data"))
  providers.sync_app_model_providers("/data")
  providers.get_provider("claude")

  assert reads == [False]


def test_concurrent_app_provider_refreshes_await_one_load(monkeypatch):
  # A second refresh used to return at once, before the first load applied.
  import threading
  from app import providers

  release = threading.Event()
  reads = []
  applied = []
  real_apply = providers._apply_app_model_declarations

  def blocking_read():
    reads.append(1)
    release.wait(5)
    return []

  def apply(declarations):
    applied.append(declarations)
    real_apply(declarations)

  monkeypatch.setattr(providers, "_read_app_model_declarations", blocking_read)
  monkeypatch.setattr(providers, "_apply_app_model_declarations", apply)

  async def scenario():
    async def refresh():
      await providers.refresh_app_model_providers("/data")
      return len(applied)

    first = asyncio.ensure_future(refresh())
    await asyncio.sleep(0)
    second = asyncio.ensure_future(refresh())
    await asyncio.sleep(0.05)
    assert not second.done()
    release.set()
    return await asyncio.gather(first, second)

  assert asyncio.run(scenario()) == [1, 1]
  assert reads == [1]


def test_parent_settle_finds_owed_results_off_the_loop(monkeypatch):
  seen = []

  def sources(parent_chat_id):
    seen.append(("sources", _running_on_event_loop()))
    return ["source-run"]

  async def deliver(parent_chat_id, source_work_id):
    seen.append(("deliver", source_work_id, _running_on_event_loop()))
    return False

  monkeypatch.setattr(delegations, "_undelivered_result_sources", sources)
  monkeypatch.setattr(delegations, "_deliver_parent_wake", deliver)

  asyncio.run(delegations.deliver_results_after_parent_settled("parent-chat"))

  assert seen == [("sources", False), ("deliver", "source-run", True)]


@pytest.mark.parametrize("change", ["parent_settles", "drain_begins", None])
def test_result_steer_plans_off_the_loop_and_rechecks_liveness_after(
  monkeypatch, change,
):
  # The plan reads durable state in a worker; the runner and drain checks
  # run on the loop after it, so a change during planning submits nothing.
  import app.chat as chat_mod
  import app.chat_steering as chat_steering
  import app.chat_writer as chat_writer

  state = {"running": True, "draining": False}
  seen = []

  def plan_steer(parent_chat_id, source_work_id):
    seen.append(("plan", _running_on_event_loop()))
    if change == "parent_settles":
      state["running"] = False
    elif change == "drain_begins":
      state["draining"] = True
    return "claude", {"role": "user", "content": "results", "cid": "cid"}

  def submit(command):
    seen.append(("submit", _running_on_event_loop()))
    raise AssertionError("stop after the loop-side checks")

  monkeypatch.setattr(delegations, "_plan_result_steer", plan_steer)
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _id: state["running"])
  monkeypatch.setattr(chat_mod, "is_draining", lambda: state["draining"])
  monkeypatch.setattr(
    chat_steering, "has_live_steerable_turn", lambda _id, _provider: True,
  )
  monkeypatch.setattr(
    chat_writer, "get_writer", lambda: type("W", (), {"submit": staticmethod(submit)}),
  )

  if change is None:
    with pytest.raises(AssertionError, match="stop after"):
      asyncio.run(delegations.steer_results_into_running_parent("parent", "src"))
    assert seen == [("plan", False), ("submit", True)]
  else:
    assert asyncio.run(
      delegations.steer_results_into_running_parent("parent", "src"),
    ) is False
    assert seen == [("plan", False)]


def test_parent_wake_plan_never_reads_the_runner_registry(chat, db, monkeypatch):
  # _plan_parent_wake runs in a worker; the registry belongs to the loop,
  # which re-checks is_chat_running itself after planning.
  import app.chat as chat_mod
  from app import models

  def forbidden(_chat_id):
    raise AssertionError("worker read the loop-owned runner registry")

  monkeypatch.setattr(chat_mod, "is_chat_running", forbidden)
  physical = models.ChatRun(
    id="carrier-run", root_run_id="carrier-run", chat_id=chat.id,
    status="running", provider="claude", provider_execution_admitted=False,
  )
  db.add(physical)
  db.commit()
  monkeypatch.setattr(
    delegations, "_committed_parent_wake",
    lambda _db, _chat, _message: (physical, [], ()),
  )
  monkeypatch.setattr(
    delegations, "safe_parent_wake_startup_writer_orphan",
    lambda _db, _chat, _physical: True,
  )

  assert delegations._committed_parent_wake_is_unowned(db, chat, {}) is True


def test_activity_start_checks_its_blockers_off_the_loop(monkeypatch):
  import app.chat as chat_mod
  import app.chat_start as chat_start

  seen = []

  def blocker(db, chat_id, **_kwargs):
    seen.append(_running_on_event_loop())
    return "owner_input"

  monkeypatch.setattr(chat_mod, "programmatic_start_blocker", blocker)
  monkeypatch.setattr(chat_mod, "is_draining", lambda: False)

  assert asyncio.run(chat_start.start_programmatic_activity_continuation(
    chat_id="parent-chat", root_run_id="root", run_token="not-started",
    source_work_id="source", activity_id="activity",
  )) is False
  assert seen == [False]


def test_activity_start_rechecks_drain_after_its_off_loop_read(monkeypatch):
  # A drain that begins during the worker read owns its runs; the start
  # must stop before touching the database on the loop.
  import app.chat as chat_mod
  import app.chat_start as chat_start
  import app.database as database

  draining = {"value": False}
  sessions = []

  def precheck(chat_id, run_token):
    draining["value"] = True
    return True, False

  monkeypatch.setattr(chat_mod, "is_draining", lambda: draining["value"])
  monkeypatch.setattr(chat_start, "_activity_start_precheck", precheck)
  monkeypatch.setattr(
    database, "SessionLocal", lambda *a, **kw: sessions.append(1),
  )

  assert asyncio.run(chat_start.start_programmatic_activity_continuation(
    chat_id="parent-chat", root_run_id="root", run_token="existing-run",
    source_work_id="source", activity_id="activity",
  )) is False
  assert sessions == []
