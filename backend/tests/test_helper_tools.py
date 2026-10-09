"""Möbius helper tools: spawn/message/stop/list, identity, and result delivery."""
from sqlalchemy.orm import object_session
from app import transcript_rows

from tests.goal_fixtures import goal_run as make_goal_run

import asyncio
import importlib.util
from pathlib import Path

import pytest

import app.chat as chat_mod
import app.chat_start as chat_start_mod
import app.delegations as delegations_mod
from app import models
from app.timeutil import now_naive_utc
from tests.test_delegations import _seed_delegation


def _control(monkeypatch, **env):
  # Start from a clean baseline: the suite must not depend on the runner's own
  # helper-host identity, which is present whenever it runs inside a host.
  for name in ("MOBIUS_HELPER_HOST", "MOBIUS_CALLER_ENV_FILE"):
    monkeypatch.delenv(name, raising=False)
  for name, value in env.items():
    monkeypatch.setenv(name, value)
  path = Path(__file__).resolve().parents[1] / "scripts" / "mobius_control_mcp.py"
  spec = importlib.util.spec_from_file_location("mobius_control_helpers_test", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _fake_capabilities(control, *, enabled=True):
  control._test_capabilities = {
    "connections": {name: {"configured": True} for name in ("claude", "codex", "mobius")},
    "config": {"providers": {
      "claude": {"enabled": enabled, "default_effort": "medium",
                 "default_model": "claude-default"},
      "codex": {"enabled": True, "default_effort": "low"},
    }},
    "models": {"claude": [{"id": "claude-default"}],
               "mobius": [{"id": "spark"}]},
  }


def _capture_api(control, monkeypatch, responses):
  calls = []

  def fake(method, path, payload=None):
    calls.append((method, path, payload))
    if path == "/api/delegations/capabilities":
      return getattr(control, "_test_capabilities", {})
    for prefix, value in responses.items():
      if path.startswith(prefix):
        return value(payload) if callable(value) else value
    return {}

  monkeypatch.setattr(control, "_agent_api_call", fake)
  return calls


def _row(**overrides):
  row = {
    "id": "del-1", "task_key": "review", "provider": "claude", "model": "m",
    "scope": "write", "status": "running",
  }
  row.update(overrides)
  return row


def test_spawn_starts_a_background_helper_with_subagents_defaults(monkeypatch):
  control = _control(monkeypatch, CHAT_ID="parent-1", MOBIUS_AGENT_PROVIDER="claude")
  _fake_capabilities(control)
  calls = _capture_api(control, monkeypatch, {"/api/delegations": lambda body: _row(
    task_key=body["task_key"], provider=body["provider"], model=body["model"],
  )})

  result = control._call_spawn_agent({
    "name": "review", "task": "  Review the diff.  ",
  })

  method, path, body = calls[-1]
  assert (method, path) == ("POST", "/api/delegations")
  assert body == {
    "app_id": None, "parent_chat_id": "parent-1", "task_key": "review",
    "prompt": "Review the diff.", "provider": "claude", "model": "claude-default",
    "effort": "medium", "scope": "write", "notify_parent_on_complete": True,
  }
  assert result["helper"] == "review" and "do not poll" in result["note"]

  with pytest.raises(ValueError, match="no longer takes access"):
    control._call_spawn_agent({
      "name": "old-call", "task": "Inspect.", "access": "read",
    })


def test_spawn_honours_a_paused_provider_only_when_named(monkeypatch):
  control = _control(monkeypatch, CHAT_ID="parent-1", MOBIUS_AGENT_PROVIDER="claude")
  _fake_capabilities(control, enabled=False)
  _capture_api(control, monkeypatch, {"/api/delegations": _row()})

  with pytest.raises(RuntimeError, match="paused"):
    control._call_spawn_agent({"name": "a", "task": "t"})
  assert control._call_spawn_agent({
    "name": "a", "task": "t", "provider": "claude",
  })["helper_id"] == "del-1"


def test_spawn_can_use_mobius_models_on_the_codex_harness(monkeypatch):
  control = _control(monkeypatch, CHAT_ID="parent-1")
  _fake_capabilities(control)
  calls = _capture_api(control, monkeypatch, {"/api/delegations": _row()})
  control._call_spawn_agent({
    "name": "m", "task": "t", "provider": "mobius", "model": "spark",
  })
  assert calls[-1][2]["provider"] == "mobius" and calls[-1][2]["model"] == "spark"


def test_message_stop_and_list_address_helpers_by_name(monkeypatch):
  control = _control(monkeypatch, CHAT_ID="parent-1")
  listed = {"items": [_row(id="del-new", task_key="review"), _row(id="del-2", task_key="scan")]}
  calls = _capture_api(control, monkeypatch, {
    "/api/delegations?parent_chat_id=parent-1": listed,
    "/api/delegations/del-new/messages": _row(id="del-new", status="starting"),
    "/api/delegations/del-2/cancel": _row(id="del-2", status="cancelled"),
    "/api/delegations/del-2": _row(id="del-2", status="completed", result="All good."),
  })

  assert control._call_message_agent({"helper": "review", "message": "Also check tests."})[
    "status"] == "starting"
  assert ("POST", "/api/delegations/del-new/messages", {"message": "Also check tests."}) in calls
  assert control._call_stop_agent({"helper": "del-2"})["status"] == "cancelled"
  assert [h["helper"] for h in control._call_list_agents({})["helpers"]] == ["review", "scan"]
  assert control._call_list_agents({"helper": "scan"})["result"] == "All good."
  # Reading one helper's result goes through the agent-side read, which
  # records receipt; the bare listing stays a plain read.
  assert ("POST", "/api/delegations/del-2/result-read", {}) in calls
  with pytest.raises(ValueError, match="No helper"):
    control._call_stop_agent({"helper": "missing"})


def test_message_agent_reports_wrong_fields_and_peer_chat_target_without_echoing_values(monkeypatch):
  control = _control(monkeypatch, CHAT_ID="parent-1")
  calls = _capture_api(control, monkeypatch, {
    "/api/delegations?parent_chat_id=parent-1": {"items": []},
  })
  with pytest.raises(ValueError) as wrong:
    control._call_message_agent({"recipient": "secret-target", "body": "secret-body"})
  assert "invalid keys: body, recipient" in str(wrong.value)
  assert "missing: helper, message" in str(wrong.value)
  assert "send_agent_message(recipients, body" in str(wrong.value)
  assert "secret-target" not in str(wrong.value)
  assert "secret-body" not in str(wrong.value)
  assert calls == []

  with pytest.raises(ValueError) as target:
    control._call_message_agent({"helper": "peer-chat-secret", "message": "Follow up"})
  assert "list_agents" in str(target.value)
  assert "send_agent_message(recipients, body)" in str(target.value)
  assert "peer-chat-secret" not in str(target.value)
  assert calls == [("GET", "/api/delegations?parent_chat_id=parent-1&limit=200", None)]


def test_every_agent_level_offers_the_helper_tools(monkeypatch):
  from app import platform_tools
  control = _control(monkeypatch, MOBIUS_RUN_TOKEN="run")
  assert set(platform_tools.HELPER_TOOL_NAMES) <= set(control._available_tool_names())
  monkeypatch.delenv("MOBIUS_RUN_TOKEN")
  assert set(platform_tools.HELPER_TOOL_NAMES) <= set(control._available_tool_names())


def test_caller_identity_is_honoured_only_inside_a_helper_host(monkeypatch, tmp_path):
  env_file = tmp_path / "turn.env"
  env_file.write_text("export CHAT_ID=helper-chat\nexport AGENT_TOKEN=helper-token\n")
  control = _control(monkeypatch, CHAT_ID="host-chat", AGENT_TOKEN="host-token")
  seen = []
  monkeypatch.setitem(control._TOOL_HANDLERS, control.LIST_AGENTS_TOOL,
                      lambda args: seen.append(control.os.environ["CHAT_ID"]) or {})

  control._call_tool({"name": control.LIST_AGENTS_TOOL,
                      "arguments": {control.CALLER_ENV_ARGUMENT: str(env_file)}})
  monkeypatch.setenv(control.HELPER_HOST_ENV, "host-digest")
  control._call_tool({"name": control.LIST_AGENTS_TOOL,
                      "arguments": {control.CALLER_ENV_ARGUMENT: str(env_file)}})

  assert seen == ["host-chat", "helper-chat"]
  assert control.os.environ["CHAT_ID"] == "host-chat"  # restored after the call


def test_codex_host_tool_server_adopts_its_turn_identity_file(monkeypatch, tmp_path):
  env_file = tmp_path / "turn.env"
  env_file.write_text("export AGENT_TOKEN=turn-token\n")
  monkeypatch.delenv("AGENT_TOKEN", raising=False)
  control = _control(monkeypatch, MOBIUS_CALLER_ENV_FILE=str(env_file))
  assert control.os.environ["AGENT_TOKEN"] == "turn-token"


# ----------------------------------------------------------------- follow-ups


def test_follow_up_restarts_a_settled_helper_and_owes_its_next_result(
  client, owner_token, db, monkeypatch,
):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="follow-up",
    result_blocks=[{"type": "text", "content": "First answer."}],
  )
  row = db.get(models.Delegation, delegation_id)
  row.delivered_run_id = "child-run-follow-up"
  db.commit()
  started = []

  async def fake_start(**kwargs):
    started.append(kwargs)
    return True

  monkeypatch.setattr(chat_start_mod, "start_programmatic_chat_turn", fake_start)
  response = client.post(
    f"/api/delegations/{delegation_id}/messages",
    json={"message": "Now check the tests too."},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 202, response.text
  assert started[0]["chat_id"] == child_id
  assert started[0]["content"] == "Now check the tests too."
  db.expire_all()
  # The follow-up's result is a new child run, so it is owed without erasing
  # the record that the first result was delivered.
  row = db.get(models.Delegation, delegation_id)
  assert row.notify_parent_on_complete is True
  assert row.delivered_run_id == "child-run-follow-up"


def test_a_working_or_stopped_helper_cannot_be_messaged(client, owner_token, db):
  _, _, running_id = _seed_delegation(db, suffix="busy", child_status="running")
  _, _, stopped_id = _seed_delegation(db, suffix="gone", cancelled=True)
  headers = {"Authorization": f"Bearer {owner_token}"}
  busy = client.post(f"/api/delegations/{running_id}/messages",
                     json={"message": "x"}, headers=headers)
  gone = client.post(f"/api/delegations/{stopped_id}/messages",
                     json={"message": "x"}, headers=headers)
  assert busy.status_code == 409 and "still working" in busy.text
  assert "send_agent_message(recipients, body)" in busy.text
  assert "list_agent_peers" in busy.text
  assert gone.status_code == 409 and "stopped" in gone.text


def test_an_agent_reading_a_settled_result_is_not_woken_to_receive_it_again(
  client, owner_token, db,
):
  """A helper read its sub-helpers' results itself, finished, and was woken 2 s
  later to 'receive' them, so it repeated its whole report to its parent."""
  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="read-in-turn",
    result_blocks=[{"type": "text", "content": "Sub-helper findings."}],
  )
  row = db.get(models.Delegation, delegation_id)
  assert delegations_mod.available_delegation_results(db, parent_id)

  response = client.post(
    f"/api/delegations/{delegation_id}/result-read", json={},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 200, response.text
  assert "Sub-helper findings." in response.json()["result"]
  db.expire_all()
  row = db.get(models.Delegation, delegation_id)
  assert row.delivered_run_id == row.incorporated_run_id is not None
  assert delegations_mod.available_delegation_results(db, parent_id) == []
  assert delegations_mod._wake_eligible_rows_for_parent(
    db, parent_id, row.parent_root_run_id,
  ) == []


def test_failed_setup_recovery_makes_helper_result_eligible_for_parent_wake(db):
  """Interrupted attempts are not deliverable; failed setup is terminal."""
  from app.chat_writer import RecoverWedgedRun, get_writer

  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="setup-recovery-wake", child_status="running",
  )
  source = db.get(models.Delegation, delegation_id).parent_root_run_id
  assert delegations_mod._wake_eligible_rows_for_parent(db, parent_id, source) == []

  get_writer().submit(RecoverWedgedRun(
    chat_id=child_id,
    run_token="child-run-setup-recovery-wake",
    terminal_status="failed",
    interruption_block={
      "type": "error", "message": "Setup failed.", "resumable": True,
    },
  )).result(timeout=5)

  db.expire_all()
  assert db.get(models.ChatRun, "child-run-setup-recovery-wake").status == "failed"
  assert [row.id for row in delegations_mod._wake_eligible_rows_for_parent(
    db, parent_id, source,
  )] == [delegation_id]


def test_viewing_a_helper_does_not_count_as_its_parent_receiving_the_result(
  client, owner_token, db,
):
  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="view-only",
    result_blocks=[{"type": "text", "content": "Still owed."}],
  )
  response = client.get(
    f"/api/delegations/{delegation_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).delivered_run_id is None
  assert delegations_mod.available_delegation_results(db, parent_id)


def test_reading_a_helper_that_is_still_working_leaves_its_result_owed(
  client, owner_token, db,
):
  _parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="read-running", child_status="running",
  )
  response = client.post(
    f"/api/delegations/{delegation_id}/result-read", json={},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  db.expire_all()
  row = db.get(models.Delegation, delegation_id)
  assert row.delivered_run_id is None and row.incorporated_run_id is None


# ----------------------------------------------------------------- live delivery


def _running_parent(db, suffix, *, provider="claude"):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix=suffix, result_blocks=[{"type": "text", "content": "Done while you worked."}],
  )
  db.get(models.Chat, parent_id).provider = provider
  db.add(make_goal_run(db,
    id=f"root-{suffix}", root_run_id=f"root-{suffix}",
    chat_id=parent_id, status="running", provider=provider,
    started_at=now_naive_utc(),
  ))
  db.commit()
  return parent_id, child_id, delegation_id


def _live_parent(monkeypatch, accepted):
  import app.chat_steering as steering
  steered = []

  async def fake_steer(provider, chat_id, content, user_msgs, consume):
    steered.append((chat_id, content, user_msgs, consume))
    return accepted

  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: True)
  monkeypatch.setattr(steering, "has_live_steerable_turn", lambda *_a: True)
  monkeypatch.setattr(steering, "steer_into_active_turn", fake_steer)
  return steered


def _steer_cut(parent_id, user_msgs, consume):
  """The live turn lands its steer: the queued carrier moves into the transcript."""
  from app.chat_writer import AppendSteeredUserMessage, get_writer

  get_writer().submit(AppendSteeredUserMessage(
    chat_id=parent_id, user_msgs=user_msgs, consume_pending_cids=consume,
  )).result(timeout=5)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_steered_result_is_delivered_at_the_steer_cut_once(db, monkeypatch, provider):
  """A steered result travels as a queued carrier, like a peer note.

  Codex consumes that carrier at the steer cut. Until then the result
  is owed; after it, the result is delivered and the after-turn wake has
  nothing left to start.
  """
  parent_id, child_id, delegation_id = _running_parent(
    db, "steer-yes", provider=provider,
  )
  steered = _live_parent(monkeypatch, accepted=True)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  (chat_id, content, user_msgs, consume), = steered
  assert chat_id == parent_id and "Done while you worked." in content
  assert user_msgs[0]["kind"] == "delegation_result"
  assert consume == [user_msgs[0]["cid"]]
  db.expire_all()
  assert db.get(models.Chat, parent_id).pending_messages == user_msgs
  assert db.get(models.Delegation, delegation_id).delivered_run_id is None

  _steer_cut(parent_id, user_msgs, consume)

  db.expire_all()
  assert db.get(models.Chat, parent_id).pending_messages == []
  assert db.get(models.Delegation, delegation_id).delivered_run_id is not None
  # Delivered: the next turn's context does not repeat it.
  assert delegations_mod.available_delegation_results(db, parent_id) == []
  db.get(models.ChatRun, "root-steer-yes").status = "completed"
  db.commit()
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: False)
  starts = []

  async def record_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr(
    chat_start_mod, "start_programmatic_activity_continuation", record_start,
  )
  asyncio.run(delegations_mod.deliver_results_after_parent_settled(parent_id))
  assert starts == []


def test_claude_stop_does_not_wake_an_unread_helper_result(
  db,
):
  """Stop, not the helper, cuts Claude and leaves the result for the owner."""
  from app.claude_sdk_runner import ActiveClaudeClient
  from app.runner_registry import registry

  parent_id, child_id, delegation_id = _running_parent(db, "steer-stopped")
  interrupts = []

  class _Client:
    async def interrupt(self):
      interrupts.append("interrupt")

  async def settle_then_stop():
    handle = ActiveClaudeClient(_Client(), chat_id=parent_id)
    registry.register(handle)
    try:
      await delegations_mod.wake_parent_after_child_settled(child_id)
      assert interrupts == []
      handle.mark_finished()  # Simulate the runner's completed SDK drain.
      stopped, cleared = await chat_mod.stop_chat_for(parent_id)
      assert stopped is True and cleared == []
      assert handle.interrupt_requested is True
    finally:
      registry.unregister(parent_id, handle.kind)

  asyncio.run(settle_then_stop())
  assert interrupts == ["interrupt"]
  db.expire_all()
  assert db.get(models.Chat, parent_id).pending_messages == []
  assert db.get(models.Delegation, delegation_id).delivered_run_id is None
  # The synthetic runner's final status follows the real Stop above.
  db.get(models.ChatRun, "root-steer-stopped").status = "stopped"
  db.commit()
  before = {row.id for row in db.query(models.ChatRun.id).filter(
    models.ChatRun.chat_id == parent_id,
  )}
  asyncio.run(delegations_mod.deliver_results_after_parent_settled(parent_id))
  db.expire_all()
  assert {row.id for row in db.query(models.ChatRun.id).filter(
    models.ChatRun.chat_id == parent_id,
  )} == before
  assert delegations_mod.parent_wake_blocker(
    db, parent_id, "root-steer-stopped", "root-steer-stopped",
  )[0] == "parent_not_waiting"
  assert delegations_mod.build_delegation_result_context(
    db, parent_id,
  ).delegation_ids == (delegation_id,)


def test_claude_helper_result_does_not_block_owner_steer_admission(db):
  """Both inputs enter the native queue without cutting work or claiming delivery."""
  from app.claude_sdk_runner import ActiveClaudeClient
  from app.chat_steering import steer_into_active_turn
  from app.runner_registry import registry

  parent_id, child_id, delegation_id = _running_parent(db, "owner-steer")
  interrupts = []
  inputs = []

  class _Client:
    async def query(self, prompt):
      inputs.extend([item async for item in prompt])

    async def interrupt(self):
      interrupts.append("interrupt")

  async def settle_then_steer():
    handle = ActiveClaudeClient(_Client(), chat_id=parent_id)
    handle.mark_ready()
    registry.register(handle)
    try:
      await delegations_mod.wake_parent_after_child_settled(child_id)
      assert await steer_into_active_turn(
        "claude", parent_id, "The owner changed course.",
      ) is True
      await asyncio.gather(*handle._send_tasks)
      assert interrupts == []
      assert [item["priority"] for item in inputs] == ["next", "next"]
      assert "delegation_results" in inputs[0]["message"]["content"]
      assert "The owner changed course." in inputs[1]["message"]["content"]
      assert inputs[0]["uuid"] != inputs[1]["uuid"]
    finally:
      handle.mark_finished()
      registry.unregister(parent_id, handle.kind)

  asyncio.run(settle_then_steer())
  db.expire_all()
  pending = db.get(models.Chat, parent_id).pending_messages
  assert len(pending) == 1 and pending[0]["hidden"] is True
  assert delegation_id in delegations_mod.carrier_results(db, pending[0])
  assert db.get(models.Delegation, delegation_id).delivered_run_id is None


def test_a_result_the_live_turn_already_admitted_is_not_steered_again(
  db, monkeypatch,
):
  """A result admitted into the running turn's opening context latches only at
  Finalize; the helper's delayed settle hook must not steer it in twice."""
  parent_id, child_id, delegation_id = _running_parent(
    db, "steer-admitted", provider="codex",
  )
  db.get(models.ChatRun, "root-steer-admitted").activity_delivery_json = {
    "delegation_ids": [delegation_id],
    "result_run_ids": {delegation_id: "child-run-steer-admitted"},
    "delivery_contract": delegations_mod.ACTIVITY_DELIVERY_FINALIZE_ATOMIC,
  }
  db.commit()
  steered = _live_parent(monkeypatch, accepted=True)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert steered == []
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).delivered_run_id is None


def test_a_reopened_helper_result_is_steered_again_not_deduplicated(
  db, monkeypatch,
):
  """Steer identity is the result, not the helper.

  The first result's carrier stays in the parent transcript. After a
  follow-up (message_agent) settles as a new child run, that carrier must
  neither mark the new result delivered nor share its steer identity.
  """
  parent_id, child_id, delegation_id = _running_parent(
    db, "steer-again", provider="codex",
  )
  steered = _live_parent(monkeypatch, accepted=True)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))
  (_chat, _content, first_msgs, first_consume), = steered
  _steer_cut(parent_id, first_msgs, first_consume)
  db.add(make_goal_run(db,
    id="child-run-steer-again-2", root_run_id="child-run-steer-again-2",
    chat_id=child_id, status="completed", provider="claude",
    started_at=now_naive_utc(),
  ))
  db.commit()

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert len(steered) == 2
  _chat, _content, second_msgs, second_consume = steered[1]
  assert second_msgs[0]["cid"] != first_msgs[0]["cid"]
  assert '"run_id":"child-run-steer-again-2"' in second_msgs[0]["content"]
  db.expire_all()
  assert db.get(
    models.Delegation, delegation_id,
  ).delivered_run_id == "child-run-steer-again"
  _steer_cut(parent_id, second_msgs, second_consume)
  db.expire_all()
  assert db.get(
    models.Delegation, delegation_id,
  ).delivered_run_id == "child-run-steer-again-2"
  assert delegations_mod.available_delegation_results(db, parent_id) == []


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_refused_steer_leaves_the_result_queued_for_after_the_turn(
  db, monkeypatch, provider,
):
  """A turn that ends before taking the steer (Codex refuses a second
  simultaneous steer; a closing turn refuses any) leaves the carrier queued,
  exactly like a peer note, and the result owed until a turn carries it."""
  parent_id, child_id, delegation_id = _running_parent(
    db, "steer-no", provider=provider,
  )
  _live_parent(monkeypatch, accepted=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  db.expire_all()
  assert db.get(models.Delegation, delegation_id).delivered_run_id is None
  [carrier] = db.get(models.Chat, parent_id).pending_messages
  assert carrier["kind"] == "delegation_result"
  assert delegation_id in delegations_mod.carrier_results(db, carrier)


def test_a_helper_finishing_before_claude_is_ready_wakes_after_it(db, monkeypatch):
  """Startup has no steerable handle yet; its result must remain durably owed."""
  parent_id, child_id, delegation_id = _running_parent(db, "tool-safe")
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: True)
  steered = _live_parent(monkeypatch, accepted=True)
  import app.chat_steering as steering
  monkeypatch.setattr(steering, "has_live_steerable_turn", lambda *_a: False)
  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))
  assert steered == []
  db.expire_all()
  assert db.get(models.Chat, parent_id).pending_messages == []
  assert db.get(models.Delegation, delegation_id).delivered_run_id is None
  db.get(models.ChatRun, "root-tool-safe").status = "completed"
  db.commit()
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: False)
  starts = []

  async def record_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr(
    chat_start_mod, "start_programmatic_activity_continuation", record_start,
  )
  asyncio.run(delegations_mod.deliver_results_after_parent_settled(parent_id))
  assert len(starts) == 1
  assert starts[0]["chat_id"] == parent_id
  assert starts[0]["activity_id"] == delegation_id
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).delivered_run_id is None


# ----------------------------------------------------------------- display


def test_helper_rows_carry_engine_timing_and_current_step(db, monkeypatch):
  from app import chat_activity
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="rows-live", child_status="running",
  )
  delegations_mod._HELPER_PARENT[child_id] = parent_id
  published = []
  monkeypatch.setattr(delegations_mod, "publish_chat_activity_changed", published.append)
  monkeypatch.setattr(delegations_mod, "_PARENT_ACTIVITY_PUBLISHED", {})

  delegations_mod.note_helper_activity(child_id, "Bash", "npm test")
  delegations_mod.note_helper_activity(child_id, "Read", "a.py")  # throttled

  running = next(
    e for e in chat_activity.chat_activity_page(db, parent_id)["events"]
    if e["delegation_id"] == delegation_id
  )
  assert running["provider"] == "claude" and running["model"] == "claude-sonnet-4-6"
  assert running["started_at"]
  assert running["activity"] == {"tool": "Read", "summary": "a.py"}
  assert published == [parent_id]


def test_a_finished_helper_row_reports_its_duration(db):
  from datetime import timedelta
  from app import chat_activity
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="rows-done", result_blocks=[{"type": "text", "content": "ok"}],
  )
  run = db.get(models.ChatRun, "child-run-rows-done")
  run.ended_at = run.started_at + timedelta(seconds=42)
  db.commit()
  finished = next(
    e for e in chat_activity.chat_activity_page(db, parent_id)["events"]
    if e["delegation_id"] == delegation_id
  )
  assert finished["duration_ms"] == 42_000 and finished["model"] == "claude-sonnet-4-6"


def test_the_conversation_panel_shows_a_helpers_own_steps(client, owner_token, db):
  _parent, child_id, delegation_id = _seed_delegation(
    db, suffix="panel", result_blocks=[
      {"type": "tool", "tool": "Bash", "input": "echo hi", "output": "hi", "status": "done"},
      {"type": "text", "content": "All checks passed."},
    ],
  )
  child = db.get(models.Chat, child_id)
  transcript_rows.replace_all(object_session(child), child, [
    *list(transcript_rows.history(child)),
    {"role": "user", "content": "carrier", "hidden": True, "kind": "delegation_result"},
  ])
  db.commit()
  response = client.get(
    f"/api/chats/{_parent}/helpers/{delegation_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["provider"] == "claude" and body["child_chat_id"] == child_id
  assert [b.get("role") or b.get("type") for b in body["blocks"]] == ["user", "tool", "assistant"]
  assert body["blocks"][0]["content"] == "Do the bounded task."
  # Only the parent that spawned the helper can open it.
  assert client.get(
    f"/api/chats/{child_id}/helpers/{delegation_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  ).status_code == 404


def test_claudes_step_text_arriving_after_its_start_is_shown(db, monkeypatch):
  parent_id, child_id, _ = _seed_delegation(db, suffix="rows-claude", child_status="running")
  delegations_mod._HELPER_PARENT[child_id] = parent_id
  published = []
  monkeypatch.setattr(delegations_mod, "publish_chat_activity_changed", published.append)
  monkeypatch.setattr(delegations_mod, "_PARENT_ACTIVITY_PUBLISHED", {})

  delegations_mod.note_helper_activity(child_id, "Bash", "")
  delegations_mod.note_helper_activity(child_id, "Bash", "npm test")

  assert delegations_mod.helper_current_activity(child_id) == {"tool": "Bash", "summary": "npm test"}
  assert published == [parent_id, parent_id]
