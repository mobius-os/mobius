"""Legacy Gauntlet work becomes inert history before generic boot recovery."""

from datetime import datetime, timedelta
import hashlib

from app import models
from app.chat_writer import (
  RetireLegacyGauntletExecution,
  get_writer,
  wait_ack,
)
from app.delegations import limit_resume_app_id


def _app(db) -> models.App:
  row = models.App(
    name="Legacy workflow owner",
    slug="legacy-workflow-owner",
    description="",
    source_dir="/tmp/legacy-workflow-owner",
    jsx_source="export default function App() { return null }",
    compiled_path="",
  )
  db.add(row)
  db.commit()
  return row


def _delegation(
  *, row_id: str, app_id: int, parent_chat_id: str, child_chat_id: str,
  task_key: str, prompt: str = "inspect", notify: bool = False,
) -> models.Delegation:
  return models.Delegation(
    id=row_id,
    app_id=app_id,
    parent_chat_id=parent_chat_id,
    parent_root_run_id=f"root-{parent_chat_id}",
    task_key=task_key,
    child_chat_id=child_chat_id,
    provider="claude",
    model="claude-opus-4-8",
    effort="low",
    scope="read",
    cwd="/data",
    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
    startup_prompt=prompt,
    notify_parent_on_complete=notify,
  )


def test_retirement_closes_complete_legacy_graph_without_touching_ordinary_work(
  db,
):
  app = _app(db)
  base = datetime(2026, 9, 8, 12, 0, 0)
  legacy_pending = {
    "role": "user",
    "content": "apply the Gauntlet evidence",
    "ts": 1,
    "cid": "gauntlet-g-active-integrate-1",
    "kind": "continuation",
    "continuation_reason": "gauntlet",
  }
  ordinary_pending = {
    "role": "user", "content": "unrelated owner follow-up", "ts": 2,
    "cid": "ordinary-follow-up",
  }
  partial = {
    "id": "writer-resume",
    "role": "assistant",
    "blocks": [{"type": "text", "content": "Partial work is preserved."}],
    "ts": 3,
  }
  child_partial = {
    "id": "critic-park",
    "role": "assistant",
    "blocks": [{"type": "text", "content": "Partial review is preserved."}],
    "ts": 3,
  }
  db.add_all((
    models.Chat(
      id="controller", title="Controller",
      messages=[{"role": "user", "content": "Run the workflow", "ts": 0}],
      pending_messages=[legacy_pending, ordinary_pending],
      live_assistant=partial,
      active_assistant_message_id="writer-resume",
      auto_resume_on_limit=True,
    ),
    models.Chat(
      id="critic", title="Critic", messages=[
        {"role": "user", "content": "inspect", "ts": 0},
      ],
      pending_messages=[{
        "role": "user", "content": "nested result", "ts": 4,
        "cid": "delegation-result",
      }],
      live_assistant=child_partial,
      active_assistant_message_id="critic-park",
      created_by_app_id=app.id,
      auto_resume_on_limit=True,
    ),
    models.Chat(
      id="nested", title="Nested", messages=[],
      pending_messages=[{
        "role": "user", "content": "queued branch work", "ts": 5,
        "cid": "nested-queued",
      }],
      created_by_app_id=app.id,
      auto_resume_on_limit=True,
    ),
    models.Chat(id="ordinary-parent", title="Ordinary parent", messages=[]),
    models.Chat(
      id="ordinary-child", title="Ordinary child", messages=[],
      created_by_app_id=app.id,
    ),
    models.Chat(
      id="completed-controller", title="History",
      messages=[{"role": "user", "content": "ordinary later work", "ts": 0}],
      live_assistant={
        "id": "ordinary-later", "role": "assistant",
        "blocks": [{"type": "text", "content": "Still ordinary."}], "ts": 1,
      },
      active_assistant_message_id="ordinary-later",
    ),
    models.Chat(
      id="completed-child", title="Completed critic", messages=[],
      created_by_app_id=app.id,
    ),
  ))
  db.flush()

  critic = _delegation(
    row_id="critic-delegation", app_id=app.id,
    parent_chat_id="controller", child_chat_id="critic", task_key="critic",
  )
  nested = _delegation(
    row_id="nested-delegation", app_id=app.id,
    parent_chat_id="critic", child_chat_id="nested", task_key="nested",
    notify=True,
  )
  ordinary = _delegation(
    row_id="ordinary-delegation", app_id=app.id,
    parent_chat_id="ordinary-parent", child_chat_id="ordinary-child",
    task_key="ordinary", prompt="keep me", notify=True,
  )
  completed = _delegation(
    row_id="completed-delegation", app_id=app.id,
    parent_chat_id="completed-controller", child_chat_id="completed-child",
    task_key="completed", notify=True,
  )
  completed.startup_prompt = None
  db.add_all((critic, nested, ordinary, completed))
  db.flush()

  db.add_all((
    models.ChatRun(
      id="controller-root", root_run_id="controller-root",
      chat_id="controller", status="running", provider="claude",
      goal_id="legacy-goal", goal_objective="Run the old Gauntlet",
      started_at=base,
    ),
    models.ChatRun(
      id="writer-seed", root_run_id="controller-root",
      chat_id="controller", status="completed", provider="claude",
      started_at=base + timedelta(seconds=10),
    ),
    models.ChatRun(
      id="writer-resume", root_run_id="controller-root",
      chat_id="controller", status="resume_pending", provider="claude",
      started_at=base + timedelta(seconds=20),
      park_reason="usage_limit", parked_until=base - timedelta(seconds=1),
    ),
    models.ChatRun(
      id="critic-park", root_run_id="critic-park", chat_id="critic",
      status="parked", provider="claude", initiated_by_app_id=app.id,
      park_reason="usage_limit", parked_until=base - timedelta(seconds=1),
      started_at=base + timedelta(seconds=5),
    ),
    models.ChatRun(
      id="nested-run", root_run_id="nested-run", chat_id="nested",
      status="running", provider="claude", initiated_by_app_id=app.id,
      started_at=base + timedelta(seconds=6),
    ),
    models.ChatRun(
      id="ordinary-run", root_run_id="ordinary-run", chat_id="ordinary-child",
      status="running", provider="claude", initiated_by_app_id=app.id,
      goal_id="ordinary-goal", goal_objective="Keep ordinary work running",
      started_at=base + timedelta(seconds=7),
    ),
    models.ChatRun(
      id="completed-run", root_run_id="completed-run",
      chat_id="completed-child", status="completed", provider="claude",
      initiated_by_app_id=app.id, started_at=base,
    ),
    models.ChatRun(
      id="history-root", root_run_id="history-root",
      chat_id="completed-controller", status="completed", provider="claude",
      started_at=base,
    ),
    models.ChatRun(
      id="history-writer", root_run_id="history-root",
      chat_id="completed-controller", status="completed", provider="claude",
      started_at=base + timedelta(seconds=10),
    ),
    models.ChatRun(
      id="ordinary-later", root_run_id="history-root",
      chat_id="completed-controller", status="running", provider="claude",
      goal_id="later-goal", goal_objective="Ordinary later work",
      started_at=base + timedelta(days=1),
    ),
  ))
  active = models.GauntletRun(
    id="g-active", app_id=app.id, parent_chat_id="controller",
    parent_root_run_id="controller-root", target_path="/data/platform",
    active_target_key="a" * 64,
    contract_json={"goal": "legacy"}, contract_sha256="b" * 64,
    provider="claude", status="running", phase="integrate",
    current_round=1, max_rounds=2, created_at=base, updated_at=base,
  )
  historical = models.GauntletRun(
    id="g-completed", app_id=app.id,
    parent_chat_id="completed-controller", parent_root_run_id="history-root",
    target_path="/data/platform/old", active_target_key=None,
    contract_json={"goal": "done"}, contract_sha256="c" * 64,
    provider="claude", status="completed", phase="terminal",
    current_round=1, max_rounds=1, created_at=base, updated_at=base,
    ended_at=base,
  )
  db.add_all((active, historical))
  db.flush()
  db.add_all((
    models.GauntletTask(
      id="g-active-critic", gauntlet_run_id=active.id, phase="baseline",
      round=0, ordinal=0, role="critic", scope="read",
      delegation_id=critic.id, prompt_sha256="d" * 64, created_at=base,
    ),
    models.GauntletTask(
      id="g-active-writer", gauntlet_run_id=active.id, phase="integrate",
      round=1, ordinal=0, role="integrator", scope="write",
      chat_run_id="writer-seed", prompt_sha256="e" * 64,
      created_at=base + timedelta(seconds=9),
    ),
    models.GauntletTask(
      id="g-completed-critic", gauntlet_run_id=historical.id,
      phase="baseline", round=0, ordinal=0, role="critic", scope="read",
      delegation_id=completed.id, prompt_sha256="f" * 64, created_at=base,
    ),
    models.GauntletTask(
      id="g-completed-writer", gauntlet_run_id=historical.id,
      phase="integrate", round=1, ordinal=0, role="integrator", scope="write",
      chat_run_id="history-writer", prompt_sha256="1" * 64,
      created_at=base + timedelta(seconds=9),
    ),
    models.ChatWait(
      id="ordinary-wait", chat_id="ordinary-parent",
      created_by_run_id=None, description="Keep waiting", kind="timer",
      due_at=base + timedelta(days=1), interval_secs=300,
      deadline_at=base + timedelta(days=2), status="armed",
      next_check_at=base + timedelta(days=1),
    ),
  ))
  db.commit()

  result = wait_ack(get_writer().submit(RetireLegacyGauntletExecution()))
  assert result == {
    "gauntlets": 1,
    "delegations": 2,
    "chat_runs": 4,
    "pending_messages": 3,
    "assistant_snapshots": 2,
  }

  db.expire_all()
  retired = db.get(models.GauntletRun, "g-active")
  assert (retired.status, retired.phase, retired.active_target_key) == (
    "stopped", "terminal", None,
  )
  assert retired.terminal_reason == (
    "Legacy Gauntlet execution was retired during the platform upgrade."
  )
  unchanged_history = db.get(models.GauntletRun, "g-completed")
  assert unchanged_history.status == "completed"
  assert unchanged_history.ended_at == base
  assert db.get(models.ChatRun, "ordinary-later").status == "running"
  assert db.get(models.Chat, "completed-controller").live_assistant[
    "id"
  ] == "ordinary-later"

  assert db.get(models.ChatRun, "controller-root").status == "stopped"
  assert db.get(models.ChatRun, "writer-seed").status == "completed"
  assert db.get(models.ChatRun, "writer-resume").status == "stopped"
  assert db.get(models.ChatRun, "critic-park").status == "stopped"
  assert db.get(models.ChatRun, "nested-run").status == "stopped"
  ordinary_run = db.get(models.ChatRun, "ordinary-run")
  assert (ordinary_run.status, ordinary_run.goal_id) == (
    "running", "ordinary-goal",
  )

  controller = db.get(models.Chat, "controller")
  assert [row["cid"] for row in controller.pending_messages] == [
    "ordinary-follow-up",
  ]
  assert controller.live_assistant is None
  assert controller.messages[-1]["blocks"][0]["content"] == (
    "Partial work is preserved."
  )
  assert controller.auto_resume_on_limit is True
  critic_chat = db.get(models.Chat, "critic")
  assert critic_chat.pending_messages == []
  assert critic_chat.live_assistant is None
  assert critic_chat.messages[-1]["blocks"][0]["content"] == (
    "Partial review is preserved."
  )
  assert db.get(models.Chat, "nested").pending_messages == []

  for delegation_id in ("critic-delegation", "nested-delegation"):
    row = db.get(models.Delegation, delegation_id)
    assert row.cancelled_at is not None
    assert row.startup_prompt is None
    assert row.notify_parent_on_complete is False
    child = db.get(models.Chat, row.child_chat_id)
    assert child.auto_resume_on_restart is False
    assert child.auto_resume_on_limit is False
  assert limit_resume_app_id(
    db, child_chat_id="critic", run_token="critic-park",
    initiated_by_app_id=app.id,
  ) is None

  ordinary_row = db.get(models.Delegation, "ordinary-delegation")
  assert ordinary_row.cancelled_at is None
  assert ordinary_row.startup_prompt == "keep me"
  assert ordinary_row.notify_parent_on_complete is True
  assert db.get(models.ChatWait, "ordinary-wait").status == "armed"

  completed_row = db.get(models.Delegation, "completed-delegation")
  assert completed_row.cancelled_at is None
  assert db.get(models.ChatRun, "completed-run").status == "completed"

  # Durable state is the cutover marker: a second boot pass is a no-op.
  second = wait_ack(get_writer().submit(RetireLegacyGauntletExecution()))
  assert second == {
    "gauntlets": 0,
    "delegations": 0,
    "chat_runs": 0,
    "pending_messages": 0,
    "assistant_snapshots": 0,
  }


def test_retirement_is_a_fresh_install_noop(db):
  assert wait_ack(get_writer().submit(RetireLegacyGauntletExecution())) == {
    "gauntlets": 0,
    "delegations": 0,
    "chat_runs": 0,
    "pending_messages": 0,
    "assistant_snapshots": 0,
  }
