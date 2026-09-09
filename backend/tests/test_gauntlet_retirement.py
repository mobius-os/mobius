"""Legacy Gauntlet execution is retired once without claiming reused chats."""

from datetime import datetime, timedelta
import hashlib

import pytest
from sqlalchemy import text

from app import models
from app.chat_writer import (
  RetireLegacyGauntletExecution,
  get_writer,
  wait_ack,
)
from app.delegations import limit_resume_app_id


_EMPTY_RESULT = {
  "gauntlets": 0,
  "delegations": 0,
  "chat_runs": 0,
  "pending_messages": 0,
  "assistant_snapshots": 0,
}


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
  parent_root_run_id: str | None = None,
) -> models.Delegation:
  return models.Delegation(
    id=row_id,
    app_id=app_id,
    parent_chat_id=parent_chat_id,
    parent_root_run_id=(
      parent_root_run_id or f"root-{parent_chat_id}"
    ),
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


def _gauntlet(
  *, row_id: str, app_id: int, chat_id: str, root_id: str,
  status: str, base: datetime,
) -> models.GauntletRun:
  active = status in {"running", "stopping"}
  return models.GauntletRun(
    id=row_id,
    app_id=app_id,
    parent_chat_id=chat_id,
    parent_root_run_id=root_id,
    target_path=f"/data/{row_id}",
    active_target_key=(hashlib.sha256(row_id.encode()).hexdigest() if active else None),
    contract_json={"goal": row_id},
    contract_sha256=hashlib.sha256(f"contract:{row_id}".encode()).hexdigest(),
    provider="claude",
    status=status,
    phase="baseline" if active else "terminal",
    current_round=0,
    max_rounds=2,
    created_at=base,
    updated_at=base,
    ended_at=None if active else base,
  )


@pytest.mark.parametrize("status", ["running", "parked", "resume_pending", "parked_notified"])
def test_active_controller_before_first_writer_cannot_resume(db, status):
  app = _app(db)
  base = datetime(2026, 9, 8, 12)
  db.add(models.Chat(id="launch-controller", title="Launch", messages=[]))
  db.add(models.ChatRun(
    id="launch-root", root_run_id="launch-root", chat_id="launch-controller",
    status=status, started_at=base, park_reason="usage_limit",
  ))
  db.add(_gauntlet(
    row_id="launch-gauntlet", app_id=app.id, chat_id="launch-controller",
    root_id="launch-root", status="running", base=base,
  ))
  db.commit()
  wait_ack(get_writer().submit(RetireLegacyGauntletExecution()))
  db.expire_all()
  assert db.get(models.ChatRun, "launch-root").status == "stopped"


def test_notified_legacy_child_park_loses_resume_authority(db):
  app = _app(db)
  base = datetime(2026, 9, 8, 12)
  db.add_all([
    models.Chat(id="notified-parent", title="Parent", messages=[]),
    models.Chat(id="notified-child", title="Child", messages=[]),
  ])
  db.add(_gauntlet(
    row_id="notified-gauntlet", app_id=app.id, chat_id="notified-parent",
    root_id="parent-root", status="running", base=base,
  ))
  db.add(_delegation(
    row_id="notified-delegation", app_id=app.id, parent_chat_id="notified-parent",
    child_chat_id="notified-child", task_key="notified-review",
  ))
  db.add(models.GauntletTask(
    id="notified-task", gauntlet_run_id="notified-gauntlet", phase="baseline",
    round=0, ordinal=0, role="critic", scope="read",
    delegation_id="notified-delegation", prompt_sha256="1" * 64,
  ))
  db.add(models.ChatRun(
    id="notified-run", root_run_id="notified-run", chat_id="notified-child",
    status="parked_notified", started_at=base, park_reason="usage_limit",
    initiated_by_app_id=app.id,
  ))
  db.commit()
  assert limit_resume_app_id(
    db, child_chat_id="notified-child", run_token="notified-run",
    initiated_by_app_id=app.id,
  ) == app.id
  wait_ack(get_writer().submit(RetireLegacyGauntletExecution()))
  db.expire_all()
  assert db.get(models.ChatRun, "notified-run").status == "stopped"
  assert db.get(models.Delegation, "notified-delegation").cancelled_at is not None
  assert limit_resume_app_id(
    db, child_chat_id="notified-child", run_token="notified-run",
    initiated_by_app_id=app.id,
  ) is None


def test_first_cutover_retires_task_lineages_without_claiming_reused_chats(db):
  app = _app(db)
  base = datetime(2026, 9, 8, 12, 0, 0)
  writer_prompt = "Apply the exact legacy Gauntlet evidence."
  synthetic_writer = {
    "role": "user",
    "content": writer_prompt,
    "ts": 2,
    "cid": "gauntlet-g-active-integrate-1",
    "kind": "continuation",
    "continuation_reason": "gauntlet",
  }
  owner_controller_pending = {
    "role": "user", "content": "ordinary controller follow-up", "ts": 3,
    "cid": "owner-controller-pending",
  }
  owner_child_pending = {
    "role": "user", "content": "ordinary child follow-up", "ts": 3,
    "cid": "owner-child-pending",
  }
  owner_nested_pending = {
    "role": "user", "content": "ordinary nested follow-up", "ts": 3,
    "cid": "owner-nested-pending",
  }
  writer_partial = {
    "id": "writer-recovery",
    "role": "assistant",
    "blocks": [{"type": "text", "content": "Writer partial is retained."}],
    "ts": 4,
  }
  critic_partial = {
    "id": "critic-park",
    "role": "assistant",
    "blocks": [{"type": "text", "content": "Critic partial is retained."}],
    "ts": 4,
  }
  db.add_all((
    models.Chat(
      id="controller", title="Controller",
      messages=[
        {"role": "user", "content": "launch", "ts": 0},
        {
          "role": "user", "content": "resume writer", "ts": 1,
          "cid": "writer-restart", "kind": "continuation",
          "continuation_reason": "restart",
          "_continuation_supersedes_run_token": "writer-seed",
          "_continuation_run_token": "writer-recovery",
        },
      ],
      pending_messages=[synthetic_writer, owner_controller_pending],
      live_assistant=writer_partial,
      active_assistant_message_id="writer-recovery",
      auto_resume_on_restart=True,
      auto_resume_on_limit=True,
    ),
    models.Chat(
      id="critic", title="Critic",
      messages=[{"role": "user", "content": "inspect", "ts": 0}],
      pending_messages=[owner_child_pending],
      live_assistant=critic_partial,
      active_assistant_message_id="critic-park",
      created_by_app_id=app.id,
      auto_resume_on_restart=True,
      auto_resume_on_limit=True,
    ),
    models.Chat(
      id="nested", title="Nested", messages=[],
      pending_messages=[owner_nested_pending],
      created_by_app_id=app.id,
      auto_resume_on_restart=True,
      auto_resume_on_limit=True,
    ),
    models.Chat(
      id="queued", title="Queued", messages=[],
      pending_messages=[{
        "role": "user", "content": "owner input while queued", "ts": 1,
        "cid": "owner-queued-pending",
      }],
      created_by_app_id=app.id,
      auto_resume_on_restart=True,
      auto_resume_on_limit=True,
    ),
    models.Chat(id="completed-controller", title="History", messages=[]),
    models.Chat(
      id="completed-child", title="Reused completed child", messages=[],
      pending_messages=[{
        "role": "user", "content": "new ordinary owner input", "ts": 8,
        "cid": "owner-before-cutover",
      }],
      live_assistant={
        "id": "ordinary-new-root", "role": "assistant",
        "blocks": [{"type": "text", "content": "New work stays live."}],
        "ts": 9,
      },
      active_assistant_message_id="ordinary-new-root",
      created_by_app_id=app.id,
      auto_resume_on_restart=True,
      auto_resume_on_limit=True,
    ),
    models.Chat(
      id="later-child", title="Later unrelated delegation", messages=[],
      created_by_app_id=app.id,
      auto_resume_on_restart=True,
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
    parent_root_run_id="critic-root", notify=True,
  )
  queued = _delegation(
    row_id="queued-delegation", app_id=app.id,
    parent_chat_id="controller", child_chat_id="queued", task_key="queued",
    notify=True,
  )
  completed = _delegation(
    row_id="completed-delegation", app_id=app.id,
    parent_chat_id="completed-controller", child_chat_id="completed-child",
    task_key="completed", notify=True,
  )
  completed.startup_prompt = None
  later = _delegation(
    row_id="later-delegation", app_id=app.id,
    parent_chat_id="completed-child", child_chat_id="later-child",
    parent_root_run_id="ordinary-goal", task_key="later", prompt="keep me",
    notify=True,
  )
  db.add_all((critic, nested, queued, completed, later))
  db.flush()

  db.add_all((
    models.ChatRun(
      id="controller-root", root_run_id="controller-root",
      chat_id="controller", status="completed", provider="claude",
      started_at=base,
    ),
    models.ChatRun(
      id="writer-seed", root_run_id="controller-root",
      chat_id="controller", status="completed", provider="claude",
      started_at=base + timedelta(seconds=10),
    ),
    models.ChatRun(
      id="writer-recovery", root_run_id="controller-root",
      chat_id="controller", status="running", provider="claude",
      restart_nonce="legacy-restart",
      started_at=base + timedelta(seconds=20),
    ),
    models.ChatRun(
      id="critic-root", root_run_id="critic-root", chat_id="critic",
      status="completed", provider="claude", initiated_by_app_id=app.id,
      started_at=base + timedelta(seconds=5),
    ),
    models.ChatRun(
      id="critic-park", root_run_id="critic-root", chat_id="critic",
      status="parked", provider="claude", initiated_by_app_id=app.id,
      park_reason="usage_limit", parked_until=base - timedelta(seconds=1),
      started_at=base + timedelta(seconds=6),
    ),
    models.ChatRun(
      id="nested-run", root_run_id="nested-run", chat_id="nested",
      status="running", provider="claude", initiated_by_app_id=app.id,
      started_at=base + timedelta(seconds=7),
    ),
    models.ChatRun(
      id="legacy-completed", root_run_id="legacy-completed",
      chat_id="completed-child", status="completed", provider="claude",
      initiated_by_app_id=app.id, started_at=base,
    ),
    models.ChatRun(
      id="ordinary-new-root", root_run_id="ordinary-new-root",
      chat_id="completed-child", status="running", provider="claude",
      goal_id="ordinary-goal", goal_objective="Ordinary reused-child work",
      started_at=base + timedelta(days=1),
    ),
    models.ChatRun(
      id="later-run", root_run_id="later-run", chat_id="later-child",
      status="running", provider="claude", initiated_by_app_id=app.id,
      started_at=base + timedelta(days=1, seconds=1),
    ),
  ))

  active = _gauntlet(
    row_id="g-active", app_id=app.id, chat_id="controller",
    root_id="controller-root", status="running", base=base,
  )
  historical = _gauntlet(
    row_id="g-completed", app_id=app.id, chat_id="completed-controller",
    root_id="completed-controller-root", status="completed", base=base,
  )
  db.add_all((active, historical))
  db.flush()
  db.add_all((
    models.GauntletTask(
      id="g-active-critic", gauntlet_run_id=active.id, phase="baseline",
      round=0, ordinal=0, role="critic", scope="read",
      delegation_id=critic.id, prompt_sha256="a" * 64, created_at=base,
    ),
    models.GauntletTask(
      id="g-active-queued", gauntlet_run_id=active.id, phase="baseline",
      round=0, ordinal=1, role="queued", scope="read",
      delegation_id=queued.id, prompt_sha256="b" * 64,
      created_at=base + timedelta(seconds=1),
    ),
    models.GauntletTask(
      id="g-active-writer", gauntlet_run_id=active.id, phase="integrate",
      round=1, ordinal=0, role="integrator", scope="write",
      chat_run_id="writer-seed",
      prompt_sha256=hashlib.sha256(writer_prompt.encode()).hexdigest(),
      created_at=base + timedelta(seconds=9),
    ),
    models.GauntletTask(
      id="g-completed-critic", gauntlet_run_id=historical.id,
      phase="baseline", round=0, ordinal=0, role="critic", scope="read",
      delegation_id=completed.id, prompt_sha256="c" * 64, created_at=base,
    ),
    models.ChatWait(
      id="ordinary-wait", chat_id="completed-child",
      description="Keep ordinary reused-child wait", kind="timer",
      due_at=base + timedelta(days=2), interval_secs=300,
      deadline_at=base + timedelta(days=3), status="armed",
      next_check_at=base + timedelta(days=2),
    ),
  ))
  db.commit()

  result = wait_ack(get_writer().submit(RetireLegacyGauntletExecution()))
  assert result == {
    "gauntlets": 1,
    "delegations": 3,
    "chat_runs": 3,
    "pending_messages": 1,
    "assistant_snapshots": 2,
  }

  db.expire_all()
  retired = db.get(models.GauntletRun, "g-active")
  assert (retired.status, retired.phase, retired.active_target_key) == (
    "stopped", "terminal", None,
  )
  assert db.get(models.GauntletRun, "g-completed").status == "completed"
  assert db.get(models.ChatRun, "controller-root").status == "completed"
  assert db.get(models.ChatRun, "writer-seed").status == "completed"
  assert db.get(models.ChatRun, "writer-recovery").status == "stopped"
  assert db.get(models.ChatRun, "critic-root").status == "completed"
  assert db.get(models.ChatRun, "critic-park").status == "stopped"
  assert db.get(models.ChatRun, "nested-run").status == "stopped"

  controller = db.get(models.Chat, "controller")
  assert [row["cid"] for row in controller.pending_messages] == [
    "owner-controller-pending",
  ]
  assert controller.live_assistant is None
  assert controller.messages[-1]["blocks"][0]["content"] == (
    "Writer partial is retained."
  )
  critic_chat = db.get(models.Chat, "critic")
  assert [row["cid"] for row in critic_chat.pending_messages] == [
    "owner-child-pending",
  ]
  assert critic_chat.live_assistant is None
  assert critic_chat.messages[-1]["blocks"][0]["content"] == (
    "Critic partial is retained."
  )
  assert [row["cid"] for row in db.get(
    models.Chat, "nested",
  ).pending_messages] == ["owner-nested-pending"]
  assert [row["cid"] for row in db.get(
    models.Chat, "queued",
  ).pending_messages] == ["owner-queued-pending"]

  for delegation_id in (
    "critic-delegation", "nested-delegation", "queued-delegation",
  ):
    row = db.get(models.Delegation, delegation_id)
    assert row.cancelled_at is not None
    assert row.startup_prompt is None
    assert row.notify_parent_on_complete is False
    child = db.get(models.Chat, row.child_chat_id)
    assert child.auto_resume_on_restart is True
  assert db.get(models.Chat, "critic").auto_resume_on_limit is True
  assert limit_resume_app_id(
    db, child_chat_id="critic", run_token="critic-park",
    initiated_by_app_id=app.id,
  ) is None

  completed_row = db.get(models.Delegation, "completed-delegation")
  assert completed_row.cancelled_at is None
  assert completed_row.notify_parent_on_complete is False
  reused = db.get(models.Chat, "completed-child")
  assert db.get(models.ChatRun, "ordinary-new-root").status == "running"
  assert reused.live_assistant["id"] == "ordinary-new-root"
  assert [row["cid"] for row in reused.pending_messages] == [
    "owner-before-cutover",
  ]
  assert reused.auto_resume_on_restart is True
  assert reused.auto_resume_on_limit is True
  assert db.get(models.ChatWait, "ordinary-wait").status == "armed"

  later_row = db.get(models.Delegation, "later-delegation")
  assert later_row.cancelled_at is None
  assert later_row.startup_prompt == "keep me"
  assert later_row.notify_parent_on_complete is True
  assert db.get(models.ChatRun, "later-run").status == "running"
  marker_id = models.LEGACY_GAUNTLET_RETIREMENT_MARKER_ID
  assert db.get(models.GauntletTargetMutex, marker_id).revision == 1

  # New work created after the successful cutover is outside the one-time
  # snapshot even when it reuses a selected legacy child chat.
  critic_chat.live_assistant = {
    "id": "critic-new-root", "role": "assistant", "blocks": [], "ts": 20,
  }
  critic_chat.active_assistant_message_id = "critic-new-root"
  critic_chat.pending_messages = list(critic_chat.pending_messages) + [{
    "role": "user", "content": "arrived after cutover", "ts": 21,
    "cid": "owner-after-cutover",
  }]
  db.add(models.ChatRun(
    id="critic-new-root", root_run_id="critic-new-root", chat_id="critic",
    status="running", provider="claude", goal_id="after-goal",
    goal_objective="New work after retirement", started_at=base + timedelta(days=2),
  ))
  db.commit()

  assert wait_ack(
    get_writer().submit(RetireLegacyGauntletExecution())
  ) == _EMPTY_RESULT
  db.expire_all()
  assert db.get(models.ChatRun, "critic-new-root").status == "running"
  assert db.get(models.Chat, "critic").live_assistant["id"] == "critic-new-root"
  assert [row["cid"] for row in db.get(
    models.Chat, "critic",
  ).pending_messages] == ["owner-child-pending", "owner-after-cutover"]


def test_completion_marker_rolls_back_with_failed_cutover_and_retry(db):
  app = _app(db)
  base = datetime(2026, 9, 8, 12, 0, 0)
  db.add(models.Chat(
    id="controller", title="Controller", messages=[],
    pending_messages=[], live_assistant={
      "id": "writer", "role": "assistant", "blocks": [], "ts": 1,
    }, active_assistant_message_id="writer",
  ))
  db.flush()
  gauntlet = _gauntlet(
    row_id="g-active", app_id=app.id, chat_id="controller",
    root_id="controller-root", status="running", base=base,
  )
  db.add(gauntlet)
  db.flush()
  db.add_all((
    models.ChatRun(
      id="controller-root", root_run_id="controller-root",
      chat_id="controller", status="completed", provider="claude",
      started_at=base,
    ),
    models.ChatRun(
      id="writer", root_run_id="controller-root", chat_id="controller",
      status="running", provider="claude", started_at=base + timedelta(seconds=1),
    ),
    models.GauntletTask(
      id="writer-task", gauntlet_run_id=gauntlet.id, phase="integrate",
      round=1, ordinal=0, role="integrator", scope="write",
      chat_run_id="writer", prompt_sha256="d" * 64, created_at=base,
    ),
  ))
  db.commit()
  db.execute(text(
    "CREATE TRIGGER refuse_gauntlet_retirement_marker "
    "BEFORE INSERT ON gauntlet_target_mutex WHEN NEW.id = -1 "
    "BEGIN SELECT RAISE(ABORT, 'injected marker failure'); END"
  ))
  db.commit()

  with pytest.raises(Exception, match="injected marker failure"):
    wait_ack(get_writer().submit(RetireLegacyGauntletExecution()))

  db.expire_all()
  marker_id = models.LEGACY_GAUNTLET_RETIREMENT_MARKER_ID
  assert db.get(models.GauntletTargetMutex, marker_id) is None
  assert db.get(models.GauntletRun, "g-active").status == "running"
  assert db.get(models.ChatRun, "writer").status == "running"
  assert db.get(models.Chat, "controller").live_assistant["id"] == "writer"

  db.execute(text("DROP TRIGGER refuse_gauntlet_retirement_marker"))
  db.commit()
  assert wait_ack(
    get_writer().submit(RetireLegacyGauntletExecution())
  ) == {
    "gauntlets": 1,
    "delegations": 0,
    "chat_runs": 1,
    "pending_messages": 0,
    "assistant_snapshots": 0,
  }
  db.expire_all()
  assert db.get(models.GauntletTargetMutex, marker_id).revision == 1
  assert db.get(models.GauntletRun, "g-active").status == "stopped"
  assert db.get(models.ChatRun, "writer").status == "stopped"


def test_fresh_install_records_one_time_completion_without_history_scan(db):
  assert wait_ack(
    get_writer().submit(RetireLegacyGauntletExecution())
  ) == _EMPTY_RESULT
  db.expire_all()
  marker_id = models.LEGACY_GAUNTLET_RETIREMENT_MARKER_ID
  assert db.get(models.GauntletTargetMutex, marker_id).revision == 1
  assert wait_ack(
    get_writer().submit(RetireLegacyGauntletExecution())
  ) == _EMPTY_RESULT
