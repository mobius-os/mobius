from types import SimpleNamespace

from app.claude_sdk_contract import transport_process_pid


def test_transport_process_pid_owns_the_private_child_chain():
  client = SimpleNamespace(
    _transport=SimpleNamespace(_process=SimpleNamespace(pid=42)),
  )
  assert transport_process_pid(client) == 42
  assert transport_process_pid(SimpleNamespace()) is None
  assert transport_process_pid(SimpleNamespace(
    _transport=SimpleNamespace(_process=SimpleNamespace(pid=1)),
  )) is None
