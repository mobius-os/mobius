"""Provider runner boundaries reject cross-provider arguments."""

import ast
import inspect
from pathlib import Path

from app import claude_sdk_runner, codex_sdk_runner


def _parameter_shape(callable_):
  return {
    name: (parameter.kind, parameter.default)
    for name, parameter in inspect.signature(callable_).parameters.items()
  }


def _runner_calls(runner_name):
  app_root = Path(__file__).parents[1] / "app"
  for source_path in app_root.rglob("*.py"):
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    for node in ast.walk(tree):
      if not isinstance(node, ast.Call):
        continue
      called = node.func
      called_name = (
        called.id if isinstance(called, ast.Name)
        else called.attr if isinstance(called, ast.Attribute)
        else None
      )
      if called_name == runner_name:
        yield source_path, node


def test_codex_lock_wrapper_preserves_the_inner_runner_contract():
  assert _parameter_shape(codex_sdk_runner.run_codex_sdk_turn) == (
    _parameter_shape(codex_sdk_runner._run_codex_sdk_turn)
  )


def test_public_provider_runners_have_strict_explicit_signatures():
  codex = inspect.signature(codex_sdk_runner.run_codex_sdk_turn).parameters
  claude = inspect.signature(claude_sdk_runner.run_claude_sdk_turn).parameters

  for parameters in (codex, claude):
    assert all(
      parameter.kind is inspect.Parameter.KEYWORD_ONLY
      for parameter in parameters.values()
    )


def test_backend_runner_calls_use_only_declared_provider_arguments():
  runners = {
    "run_codex_sdk_turn": codex_sdk_runner.run_codex_sdk_turn,
    "run_claude_sdk_turn": claude_sdk_runner.run_claude_sdk_turn,
  }
  for runner_name, runner in runners.items():
    declared = set(inspect.signature(runner).parameters)
    calls = list(_runner_calls(runner_name))
    assert calls, f"no backend calls found for {runner_name}"
    for source_path, call in calls:
      assert not call.args, (
        f"{source_path}:{call.lineno} passes positional arguments to "
        f"{runner_name}; keep provider arguments named at the boundary"
      )
      assert all(keyword.arg is not None for keyword in call.keywords), (
        f"{source_path}:{call.lineno} expands arbitrary kwargs into "
        f"{runner_name}; keep the provider boundary explicit"
      )
      supplied = {keyword.arg for keyword in call.keywords}
      unknown = supplied - declared
      assert not unknown, (
        f"{source_path}:{call.lineno} passes unsupported arguments "
        f"to {runner_name}: {sorted(unknown)}"
      )
