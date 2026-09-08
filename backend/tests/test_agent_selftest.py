"""Behavioral coverage for the bounded production agent self-test."""

import importlib.util
from pathlib import Path
import urllib.error


_PATH = Path(__file__).parents[1] / "scripts" / "agent_selftest.py"
_SPEC = importlib.util.spec_from_file_location("agent_selftest", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
agent_selftest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent_selftest)


def _created_chat(*, provider="codex", model="gpt-test"):
  return {
    "id": "chat-1",
    "detail": {
      "provider": provider,
      "effective_agent_settings": {"model": model},
    },
  }


def test_selftest_uses_the_current_picker_without_mutating_owner_defaults(monkeypatch):
  calls = []

  def request(base, method, path, token, body=None, *, timeout=30):
    assert 0 < timeout <= 5
    calls.append((method, path, body))
    if path == "/api/chats":
      return _created_chat()
    if method == "GET":
      return {
        "running": False,
        "messages": [{"role": "assistant", "content": "OK"}],
      }
    return {}

  monkeypatch.setattr(agent_selftest, "_request", request)
  monkeypatch.setattr(
    agent_selftest,
    "_load_classifier",
    lambda: lambda chat: "passed" if not chat.get("running") else "pending",
  )

  result = agent_selftest.run_selftest(
    base="http://localhost:8000",
    token="not-printed",
    expected_provider="codex",
    expected_model="gpt-test",
    prompt="Reply with exactly: OK",
    timeout=5,
    keep=False,
  )

  assert result["result"] == "passed"
  assert result["provider"] == "codex"
  assert result["model"] == "gpt-test"
  assert calls == [
    ("POST", "/api/chats", {"title": "Deploy self-test", "messages": []}),
    (
      "POST",
      "/api/chats/chat-1/messages",
      {"content": "Reply with exactly: OK", "hidden": True},
    ),
    ("GET", "/api/chats/chat-1?limit=10", None),
    ("DELETE", "/api/chats/chat-1", None),
  ]


def test_picker_expectation_mismatch_is_unavailable_and_still_cleans_up(monkeypatch):
  calls = []

  def request(base, method, path, token, body=None, *, timeout=30):
    assert 0 < timeout <= 5
    calls.append((method, path))
    if path == "/api/chats":
      return _created_chat(provider="claude", model="claude-test")
    return {}

  monkeypatch.setattr(agent_selftest, "_request", request)

  result = agent_selftest.run_selftest(
    base="http://localhost:8000",
    token="not-printed",
    expected_provider="codex",
    expected_model=None,
    prompt="OK",
    timeout=5,
    keep=False,
  )

  assert result == {
    "result": "unavailable",
    "reason": "active provider does not match expectation",
    "provider": "claude",
    "model": "claude-test",
  }
  assert calls == [
    ("POST", "/api/chats"),
    ("DELETE", "/api/chats/chat-1"),
  ]


def test_rate_limit_and_transport_failures_are_failed_not_unavailable(monkeypatch):
  for exc in (
    urllib.error.HTTPError("url", 429, "limited", {}, None),
    urllib.error.URLError("connection refused"),
  ):
    monkeypatch.setattr(
      agent_selftest,
      "_request",
      lambda *args, _exc=exc, **kwargs: (_ for _ in ()).throw(_exc),
    )
    result = agent_selftest.run_selftest(
      base="http://localhost:8000",
      token="not-printed",
      expected_provider=None,
      expected_model=None,
      prompt="OK",
      timeout=5,
      keep=False,
    )
    assert result["result"] == "failed"


def test_cleanup_failure_is_reported_without_erasing_a_passing_turn(monkeypatch):
  def request(base, method, path, token, body=None, *, timeout=30):
    if path == "/api/chats":
      return _created_chat()
    if method == "GET":
      return {"running": False, "messages": [{"role": "assistant"}]}
    if method == "DELETE":
      raise OSError("cleanup failed")
    return {}

  monkeypatch.setattr(agent_selftest, "_request", request)
  monkeypatch.setattr(agent_selftest, "_load_classifier", lambda: lambda chat: "passed")

  result = agent_selftest.run_selftest(
    base="http://localhost:8000",
    token="not-printed",
    expected_provider=None,
    expected_model=None,
    prompt="OK",
    timeout=5,
    keep=False,
  )

  assert result["result"] == "passed"
  assert result["cleanup"] == "failed"
  assert result["cleanup_chat_id"] == "chat-1"


def test_keep_returns_the_exact_chat_identity_for_operator_inspection(monkeypatch):
  calls = []

  def request(base, method, path, token, body=None, *, timeout=30):
    calls.append((method, path))
    if path == "/api/chats":
      return _created_chat()
    if method == "GET":
      return {"running": False, "messages": [{"role": "assistant"}]}
    return {}

  monkeypatch.setattr(agent_selftest, "_request", request)
  monkeypatch.setattr(agent_selftest, "_load_classifier", lambda: lambda chat: "passed")

  result = agent_selftest.run_selftest(
    base="http://localhost:8000",
    token="not-printed",
    expected_provider=None,
    expected_model=None,
    prompt="OK",
    timeout=5,
    keep=True,
  )

  assert result["result"] == "passed"
  assert result["chat_id"] == "chat-1"
  assert all(method != "DELETE" for method, _path in calls)


def test_request_timeout_is_capped_by_the_remaining_probe_budget(monkeypatch):
  observed = []
  now = iter((10.0, 10.0, 10.4, 10.7, 10.8, 10.85))

  def request(base, method, path, token, body=None, *, timeout=30):
    observed.append((method, timeout))
    if path == "/api/chats":
      return _created_chat()
    if method == "GET":
      return {"running": False, "messages": [{"role": "assistant"}]}
    return {}

  monkeypatch.setattr(agent_selftest, "_request", request)
  monkeypatch.setattr(agent_selftest, "_load_classifier", lambda: lambda chat: "passed")

  result = agent_selftest.run_selftest(
    base="http://localhost:8000",
    token="not-printed",
    expected_provider=None,
    expected_model=None,
    prompt="OK",
    timeout=1,
    keep=False,
    clock=lambda: next(now),
    sleep=lambda _seconds: None,
  )

  assert result["result"] == "passed"
  assert observed[0] == ("POST", 1.0)
  assert observed[1][0] == "POST" and 0 < observed[1][1] <= 0.6
  assert observed[2][0] == "GET" and 0 < observed[2][1] <= 0.3
  assert observed[3] == ("DELETE", 1.0)
