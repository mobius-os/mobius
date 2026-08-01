from types import SimpleNamespace

from app.claude_sdk_contract import transport_exit_error, transport_process_pid


def test_transport_process_pid_owns_the_private_child_chain():
  client = SimpleNamespace(
    _transport=SimpleNamespace(_process=SimpleNamespace(pid=42)),
  )
  assert transport_process_pid(client) == 42
  assert transport_process_pid(SimpleNamespace()) is None
  assert transport_process_pid(SimpleNamespace(
    _transport=SimpleNamespace(_process=SimpleNamespace(pid=1)),
  )) is None


def test_transport_exit_error_preserves_the_sdk_outcome_object():
  outcome = object()
  client = SimpleNamespace(_transport=SimpleNamespace(_exit_error=outcome))
  assert transport_exit_error(client) is outcome
  assert transport_exit_error(SimpleNamespace()) is None
