"""Automatic Goal promotion uses one typed, run-bound platform operation."""

import importlib.util
import json
from pathlib import Path

import pytest


def _helper():
  path = Path(__file__).resolve().parents[1] / "scripts" / "goal_promote.py"
  spec = importlib.util.spec_from_file_location("goal_promote_script", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class _Response:
  def __init__(self, payload):
    self.payload = payload

  def __enter__(self):
    return self

  def __exit__(self, *_args):
    return False

  def read(self):
    return json.dumps(self.payload).encode("utf-8")


def _env(monkeypatch):
  monkeypatch.setenv("API_BASE_URL", "http://mobius.test")
  monkeypatch.setenv("AGENT_TOKEN", "run-bound-token")
  monkeypatch.setenv("CHAT_ID", "chat-1")
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")


def test_promotion_posts_one_typed_request_without_message_text(monkeypatch):
  helper = _helper()
  _env(monkeypatch)
  calls = []

  def open_request(request, timeout):
    calls.append((request, timeout))
    return _Response({
      "state": "promoted",
      "objective": "Finish every stage and verify the result",
      "root_run_id": "run-1",
      "run_id": "run-1",
    })

  monkeypatch.setattr(helper, "urlopen", open_request)
  objective = "Finish every stage and verify the result"

  assert helper.promote_goal(objective)["state"] == "promoted"
  assert len(calls) == 1
  request, timeout = calls[0]
  assert request.full_url == "http://mobius.test/api/chats/chat-1/goal"
  assert request.method == "POST"
  assert timeout == 30
  assert json.loads(request.data) == {"objective": objective}
  assert b"/goal" not in request.data


def test_promotion_accepts_an_idempotent_active_response(monkeypatch):
  helper = _helper()
  _env(monkeypatch)
  monkeypatch.setattr(helper, "urlopen", lambda *_args, **_kwargs: _Response({
    "state": "active",
    "objective": "Ship and verify",
    "root_run_id": "run-1",
    "run_id": "run-1",
  }))

  assert helper.promote_goal("Ship and verify")["state"] == "active"


def test_promotion_rejects_a_response_for_another_run(monkeypatch):
  helper = _helper()
  _env(monkeypatch)
  monkeypatch.setattr(helper, "urlopen", lambda *_args, **_kwargs: _Response({
    "state": "promoted",
    "objective": "Ship and verify",
    "root_run_id": "run-1",
    "run_id": "newer-run",
  }))

  with pytest.raises(SystemExit, match="activation was not verified"):
    helper.promote_goal("Ship and verify")


def test_promotion_requires_the_current_physical_run(monkeypatch):
  helper = _helper()
  monkeypatch.setenv("API_BASE_URL", "http://mobius.test")
  monkeypatch.setenv("AGENT_TOKEN", "owner-token")
  monkeypatch.setenv("CHAT_ID", "chat-1")

  with pytest.raises(SystemExit, match="MOBIUS_RUN_TOKEN"):
    helper.promote_goal("Ship and verify")
