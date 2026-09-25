"""Möbius helper tools: spawn/message/stop/list, identity, and result delivery."""

from tests.goal_fixtures import goal_run as make_goal_run

import asyncio
import importlib.util
import types
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


def _fake_subagents(control, monkeypatch, *, enabled=True):
  app = types.SimpleNamespace(
    snapshot=lambda: {"app_id": 7, "providers": {
      "claude": {"connected": True, "enabled": enabled, "default_effort": "medium"},
      "codex": {"connected": True, "enabled": True, "default_effort": "low"},
    }},
    _resolve_model=lambda provider, requested, state: requested or f"{provider}-default",
  )
  monkeypatch.setattr(control, "_subagents_app", lambda: app)


def _capture_api(control, monkeypatch, responses):
  calls = []

  def fake(method, path, payload=None):
    calls.append((method, path, payload))
    for prefix, value in responses.items():
      if path.startswith(prefix):
        return value(payload) if callable(value) else value
    return {}

  monkeypatch.setattr(control, "_agent_api_call", fake)
  return calls


def _row(**overrides):
  row = {
    "id": "del-1", "task_key": "review", "provider": "claude", "model": "m",
    "scope": "read", "status": "running",
  }
  row.update(overrides)
  return row


def test_spawn_starts_a_background_helper_with_subagents_defaults(monkeypatch):
  control = _control(monkeypatch, CHAT_ID="parent-1", MOBIUS_AGENT_PROVIDER="claude")
  _fake_subagents(control, monkeypatch)
  calls = _capture_api(control, monkeypatch, {"/api/delegations": lambda body: _row(
    task_key=body["task_key"], provider=body["provider"], model=body["model"],
  )})

  result = control._call_spawn_agent({
    "name": "review", "task": "  Review the diff.  ", "access": "read",
  })

  method, path, body = calls[0]
  assert (method, path) == ("POST", "/api/delegations")
  assert body == {
    "app_id": 7, "parent_chat_id": "parent-1", "task_key": "review",
    "prompt": "Review the diff.", "provider": "claude", "model": "claude-default",
    "effort": "medium", "scope": "read", "notify_parent_on_complete": True,
  }
  assert result["helper"] == "review" and "do not poll" in result["note"]


def test_spawn_honours_a_paused_provider_only_when_named(monkeypatch):
  control = _control(monkeypatch, CHAT_ID="parent-1", MOBIUS_AGENT_PROVIDER="claude")
  _fake_subagents(control, monkeypatch, enabled=False)
  _capture_api(control, monkeypatch, {"/api/delegations": _row()})

  with pytest.raises(RuntimeError, match="paused"):
    control._call_spawn_agent({"name": "a", "task": "t", "access": "read"})
  assert control._call_spawn_agent({
    "name": "a", "task": "t", "access": "read", "provider": "claude",
  })["helper_id"] == "del-1"


def test_spawn_can_use_mobius_models_on_the_codex_harness(monkeypatch):
  control = _control(monkeypatch, CHAT_ID="parent-1")
  _fake_subagents(control, monkeypatch)
  calls = _capture_api(control, monkeypatch, {"/api/delegations": _row()})
  control._call_spawn_agent({
    "name": "m", "task": "t", "access": "write", "provider": "mobius", "model": "spark",
  })
  assert calls[0][2]["provider"] == "mobius" and calls[0][2]["model"] == "spark"


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
  with pytest.raises(ValueError, match="No helper"):
    control._call_stop_agent({"helper": "missing"})


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


def test_follow_up_restarts_a_settled_helper_and_rearms_its_wake(
  client, owner_token, db, monkeypatch,
):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="follow-up",
    result_blocks=[{"type": "text", "content": "First answer."}],
  )
  row = db.get(models.Delegation, delegation_id)
  row.parent_woken_at = now_naive_utc()
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
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None


def test_a_working_or_stopped_helper_cannot_be_messaged(client, owner_token, db):
  _, _, running_id = _seed_delegation(db, suffix="busy", child_status="running")
  _, _, stopped_id = _seed_delegation(db, suffix="gone", cancelled=True)
  headers = {"Authorization": f"Bearer {owner_token}"}
  busy = client.post(f"/api/delegations/{running_id}/messages",
                     json={"message": "x"}, headers=headers)
  gone = client.post(f"/api/delegations/{stopped_id}/messages",
                     json={"message": "x"}, headers=headers)
  assert busy.status_code == 409 and "still working" in busy.text
  assert gone.status_code == 409 and "stopped" in gone.text


# ----------------------------------------------------------------- live delivery


def _running_parent(db, suffix):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix=suffix, result_blocks=[{"type": "text", "content": "Done while you worked."}],
  )
  db.add(make_goal_run(db,
    id=f"root-{suffix}", root_run_id=f"root-{suffix}",
    chat_id=parent_id, status="running", provider="claude",
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


def test_a_result_is_steered_into_the_running_parent_turn_once(db, monkeypatch):
  parent_id, child_id, delegation_id = _running_parent(db, "steer-yes")
  steered = _live_parent(monkeypatch, accepted=True)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  (chat_id, content, user_msgs, consume), = steered
  assert chat_id == parent_id and "Done while you worked." in content
  assert user_msgs[0]["kind"] == "delegation_result" and consume is None
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None
  # Delivered: the next turn's context does not repeat it.
  assert delegations_mod.available_delegation_results(db, parent_id) == []


def test_a_refused_steer_leaves_the_result_for_after_the_turn(db, monkeypatch):
  parent_id, child_id, delegation_id = _running_parent(db, "steer-no")
  _live_parent(monkeypatch, accepted=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  assert db.get(models.Chat, parent_id).pending_messages == []
  assert [row.id for row in delegations_mod.available_delegation_results(db, parent_id)] == [
    delegation_id,
  ]


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
  child.messages = [
    *child.messages,
    {"role": "user", "content": "carrier", "hidden": True, "kind": "delegation_result"},
  ]
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
