from types import SimpleNamespace

import pytest

from app.codex_sdk_contract import (
  CodexSdkContractError,
  app_server_pid,
  control_client,
  install_approval_handler,
)


def test_control_client_localizes_the_async_sdk_private_seam():
  client = object()
  codex = SimpleNamespace(_client=client)
  assert control_client(codex) is client


def test_control_client_fails_loudly_when_sdk_shape_changes():
  with pytest.raises(CodexSdkContractError, match="AsyncCodex._client missing"):
    control_client(SimpleNamespace())


def test_approval_handler_installs_into_the_expected_sync_slot():
  sync = SimpleNamespace(_approval_handler=None)
  codex = SimpleNamespace(_client=SimpleNamespace(_sync=sync))
  handler = lambda _method, _params: {}

  assert install_approval_handler(codex, handler) is True
  assert sync._approval_handler is handler


def test_approval_handler_allows_lightweight_fake_but_rejects_partial_chain():
  assert install_approval_handler(SimpleNamespace(), lambda *_: {}) is False
  codex = SimpleNamespace(_client=SimpleNamespace(_sync=SimpleNamespace()))
  with pytest.raises(CodexSdkContractError, match="approval_handler missing"):
    install_approval_handler(codex, lambda *_: {})


def test_app_server_pid_owns_the_private_process_chain():
  codex = SimpleNamespace(
    _client=SimpleNamespace(_sync=SimpleNamespace(_proc=SimpleNamespace(pid=42))),
  )
  assert app_server_pid(codex) == 42
  assert app_server_pid(SimpleNamespace()) is None
  assert app_server_pid(SimpleNamespace(
    _client=SimpleNamespace(_sync=SimpleNamespace(_proc=SimpleNamespace(pid=1))),
  )) is None
