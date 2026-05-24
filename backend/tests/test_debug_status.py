"""`/api/debug/status` must reflect EVERY runtime registry.

Three concurrent runtime registries exist: `_active_procs`
(subprocess-backed turns), `_active_clients` (Claude SDK clients),
`_active_sessions` (Codex SDK threads). The monitoring scripts in
the runbook poll `/api/debug/status` to detect chat completion — if
any registry is missing from the response, an in-flight chat appears
idle and the monitor reports false-done.

Added after the blast-radius review caught that the original status
endpoint was blind to SDK turns. See
`_003-tech-debt-and-test-gaps.md` TG-6 for context.
"""

from unittest.mock import MagicMock


def test_status_reports_subprocess_runtimes(client, auth):
  """Subprocess-backed chats appear in `active_procs`."""
  from app import chat as chat_mod

  proc = MagicMock()
  proc.pid = 4242
  proc.returncode = None
  chat_mod._active_procs["chat-subproc"] = proc
  try:
    r = client.get("/api/debug/status", headers=auth)
    assert r.status_code == 200
    body = r.json()
    chat_ids = {entry["chat_id"] for entry in body["active_procs"]}
    assert "chat-subproc" in chat_ids
  finally:
    chat_mod._active_procs.pop("chat-subproc", None)


def test_status_reports_sdk_clients(client, auth):
  """Claude SDK-backed chats appear in `active_sdk_clients`."""
  from app import chat as chat_mod

  chat_mod._active_clients["chat-sdk-claude"] = object()
  try:
    r = client.get("/api/debug/status", headers=auth)
    assert r.status_code == 200
    body = r.json()
    chat_ids = {entry["chat_id"] for entry in body["active_sdk_clients"]}
    assert "chat-sdk-claude" in chat_ids
  finally:
    chat_mod._active_clients.pop("chat-sdk-claude", None)


def test_status_reports_sdk_sessions(client, auth):
  """Codex SDK-backed chats appear in `active_sdk_sessions`."""
  from app import chat as chat_mod

  chat_mod._active_sessions["chat-sdk-codex"] = (object(), object())
  try:
    r = client.get("/api/debug/status", headers=auth)
    assert r.status_code == 200
    body = r.json()
    chat_ids = {entry["chat_id"] for entry in body["active_sdk_sessions"]}
    assert "chat-sdk-codex" in chat_ids
  finally:
    chat_mod._active_sessions.pop("chat-sdk-codex", None)


def test_status_response_has_all_runtime_keys(client, auth):
  """Every runtime registry field is always present (even when empty),
  so monitors can rely on a stable schema."""
  r = client.get("/api/debug/status", headers=auth)
  assert r.status_code == 200
  body = r.json()
  for key in (
    "active_procs",
    "active_sdk_clients",
    "active_sdk_sessions",
    "starting",
    "broadcasts",
  ):
    assert key in body, f"missing key in /api/debug/status: {key}"
