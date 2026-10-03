"""The one command-line fallback carries a wait's check command literally."""

import importlib.util
import io
import json
from pathlib import Path

import pytest


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
    "deadline_secs": 600, "on_ready": "Review the results", "check_description": "Published checks", "owner_chat_id": "owner-chat"}
  assert control._cli_call(["call", "declare_wait", "--args-json", json.dumps(args)]) == 0
  payload = captured[0][2]
  assert payload["kind"] == "github_checks"
  for name in ("github_checks", "on_ready", "check_description", "owner_chat_id"):
    assert payload[name] == args[name]
