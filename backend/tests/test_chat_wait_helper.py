"""The one command-line fallback carries a wait's check command literally."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

from app import platform_tools


def _control(monkeypatch):
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  path = Path(__file__).parents[1] / "scripts" / "mobius_control_mcp.py"
  spec = importlib.util.spec_from_file_location("mobius_control_cli", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_stdin_arguments_save_a_quoted_command_without_shell_nesting(monkeypatch):
  control = _control(monkeypatch)
  command = "set -o pipefail\nvalue='a \"quoted\" value'\n[[ $value == *quoted* ]]\n"
  captured = []
  monkeypatch.setattr(
    control._WAITS, "_call",
    lambda *args: captured.append(args) or {"kind": "command"},
  )
  monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
    "description": "Ready", "command": command,
    "condition_owner": "test executor", "deadline_secs": 600,
  })))

  assert control._cli_call(["call", "declare_wait", "--args-json", "-"]) == 0
  assert captured[0][2]["command"] == command


def test_a_wait_with_both_a_command_and_a_timer_is_refused_before_any_request(
  monkeypatch, capsys,
):
  control = _control(monkeypatch)
  monkeypatch.setattr(
    control._WAITS, "_call", lambda *args: pytest.fail("must not connect"),
  )

  status = control._cli_call(["call", "declare_wait", "--args-json", json.dumps({
    "description": "Ready", "command": "true", "delay_secs": 60,
    "condition_owner": "x", "deadline_secs": 600,
  })])

  assert status == 1
  assert "exactly one of command, delay_secs, or github_checks" in capsys.readouterr().out


def test_an_empty_command_beside_a_timer_still_arms_the_timer(monkeypatch):
  control = _control(monkeypatch)
  captured = []
  monkeypatch.setattr(control._WAITS, "_call", lambda *args: captured.append(args) or {"kind": "timer"})

  assert control._cli_call(["call", "declare_wait", "--args-json", json.dumps({
    "description": "Later", "command": "", "delay_secs": 120,
  })]) == 0
  payload = captured[0][2]
  assert payload["kind"] == "timer"
  assert payload["command"] is None and payload["delay_secs"] == 120


def test_malformed_stdin_arguments_are_a_usage_error(monkeypatch, capsys):
  control = _control(monkeypatch)
  monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))

  assert control._cli_call(["call", "declare_wait", "--args-json", "-"]) == 2
  assert "invalid --args-json" in capsys.readouterr().err


def test_standard_check_details_cross_the_tool_boundary_without_shell_scripting(monkeypatch):
  control = _control(monkeypatch)
  captured = []
  monkeypatch.setattr(control._WAITS, "_call", lambda *args: captured.append(args) or {"kind": "github_checks"})
  args = {"description": "Checks finish", "github_checks": {
    "repository": "owner/repo", "pull_request": 7, "head_sha": "a" * 40},
    "deadline_secs": 600}
  assert control._cli_call(["call", "declare_wait", "--args-json", json.dumps(args)]) == 0
  payload = captured[0][2]
  assert payload["kind"] == "github_checks"
  assert payload["github_checks"] == args["github_checks"]
  assert payload["command"] is None and payload["delay_secs"] is None


def test_wait_for_decision_is_exact_top_level_operation_not_an_external_poll(monkeypatch):
  control = _control(monkeypatch)
  captured = []
  monkeypatch.setattr(control._WAITS, "_call", lambda *args: captured.append(args) or {"status": "waiting"})
  assert control.WAIT_FOR_DECISION_TOOL in platform_tools.OWNER_CONTROL_TOOL_NAMES
  assert control.WAIT_FOR_DECISION_TOOL not in platform_tools.DELEGATED_CONTROL_TOOL_NAMES
  assert control._call_wait_for_decision({"chat_id": "source", "question_id": "card"}) == {"status": "waiting"}
  assert captured == [("POST", "/api/chat-waits/decision", {"chat_id": "source", "question_id": "card"})]
  assert "wait_for_decision" in control.DECLARE_WAIT_DESCRIPTION
  for bad in ({}, {"chat_id": "source", "question_id": "card", "deadline_secs": 1},
              {"chat_id": "source", "question_id": ""}):
    with pytest.raises(ValueError):
      control._call_wait_for_decision(bad)
