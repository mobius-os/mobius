"""Automatic Goal promotion reserves one verified hidden native activation."""

import importlib.util
from pathlib import Path

import pytest


def _helper():
  path = Path(__file__).resolve().parents[1] / "scripts" / "goal_promote.py"
  spec = importlib.util.spec_from_file_location("goal_promote_script", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_promotion_queues_hidden_activation_with_turn_scoped_identity(
  monkeypatch,
):
  helper = _helper()
  monkeypatch.setenv("API_BASE_URL", "http://mobius.test")
  monkeypatch.setenv("AGENT_TOKEN", "owner-token")
  monkeypatch.setenv("CHAT_ID", "chat-1")
  calls = []
  pending = []

  def request(method, path, body=None):
    calls.append((method, path, body))
    if method == "GET" and path.endswith("/runtime"):
      return {
        "running": True,
        "active_goal_objective": None,
        "pending_messages": list(pending),
      }
    if method == "GET":
      return {"messages": [
        {"role": "user", "content": "Do the work", "cid": "owner-turn"},
      ]}
    pending.append(dict(body))
    return {"status": "queued"}

  monkeypatch.setattr(helper, "_request", request)
  objective = "Finish every stage and verify the result"

  assert helper.promote_goal(objective) == "queued"
  post = next(call for call in calls if call[0] == "POST")
  assert post[2]["content"] == f"/goal {objective}"
  assert post[2]["hidden"] is True
  assert post[2]["cid"] == helper._activation_cid(
    "chat-1", "owner-turn", objective,
  )


def test_retry_identity_changes_only_with_the_owner_turn():
  helper = _helper()
  objective = "Ship and verify"
  first = helper._activation_cid("chat", "turn-a", objective)

  assert helper._activation_cid("chat", "turn-a", objective) == first
  assert helper._activation_cid("chat", "turn-b", objective) != first


def test_promotion_refuses_to_queue_outside_the_request_it_promotes(
  monkeypatch,
):
  helper = _helper()
  monkeypatch.setenv("API_BASE_URL", "http://mobius.test")
  monkeypatch.setenv("AGENT_TOKEN", "owner-token")
  monkeypatch.setenv("CHAT_ID", "chat-1")
  monkeypatch.setattr(
    helper, "_request",
    lambda *_args, **_kwargs: {
      "running": False,
      "active_goal_objective": None,
      "pending_messages": [],
    },
  )

  with pytest.raises(SystemExit, match="must run during the owner request"):
    helper.promote_goal("Ship and verify")
