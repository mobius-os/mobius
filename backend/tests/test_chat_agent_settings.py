"""Tests for per-chat agent settings — the `/` slash picker.

Locks in three contracts:
  1. `effective_agent_settings` merges per-chat overrides on top of
     the global default JSON, last-write-wins per key.
  2. `PATCH /api/chats/{id}` merges (not replaces) the override, and
     `clear_agent_settings=true` reverts the chat to the global default.
  3. `_run_chat_impl` (the SDK dispatch) passes the merged settings
     into the runner — verified by mocking the SDK runner and
     asserting the `agent_settings` kwarg.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from app.providers import effective_agent_settings


def _write_global_settings(payload: dict) -> None:
  """Writes /data/shared/agent-settings.json under the test DATA_DIR."""
  data_dir = Path(os.environ["DATA_DIR"])
  shared = data_dir / "shared"
  shared.mkdir(parents=True, exist_ok=True)
  (shared / "agent-settings.json").write_text(json.dumps(payload))


def test_effective_settings_falls_back_to_global(tmp_path):
  """No chat override → returns the global default unchanged."""
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(
    json.dumps({"model": "claude-opus-4-5", "effort": "high"})
  )
  result = effective_agent_settings(str(tmp_path), None)
  assert result == {"model": "claude-opus-4-5", "effort": "high"}


def test_effective_settings_chat_override_wins(tmp_path):
  """Chat override per-key replaces the global value; missing keys
  fall through to the default."""
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(
    json.dumps({"model": "claude-opus-4-5", "effort": "medium"})
  )
  result = effective_agent_settings(
    str(tmp_path), {"model": "claude-sonnet-4-5"},
  )
  assert result["model"] == "claude-sonnet-4-5"
  assert result["effort"] == "medium"  # fell through


def test_effective_settings_ignores_none_values(tmp_path):
  """A None in the override means "no opinion" — fall through to default."""
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(
    json.dumps({"model": "claude-opus-4-5"})
  )
  result = effective_agent_settings(
    str(tmp_path), {"model": None, "effort": "high"},
  )
  assert result == {"model": "claude-opus-4-5", "effort": "high"}


def test_patch_chat_writes_override(client, auth, chat):
  """PATCH /chats/{id} sets agent_settings_json and returns effective."""
  _write_global_settings({"model": "default-model"})
  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "claude-opus-4-7"}},
  )
  assert r.status_code == 200
  body = r.json()
  assert body["ok"] is True
  assert body["agent_settings_json"] == {"model": "claude-opus-4-7"}
  assert body["effective"]["model"] == "claude-opus-4-7"


def test_patch_chat_merges_partial_updates(client, auth, chat):
  """Sending only `effort` must NOT clear a previously-set `model`."""
  _write_global_settings({})
  client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "claude-opus-4-7"}},
  )
  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"effort": "high"}},
  )
  assert r.status_code == 200
  assert r.json()["agent_settings_json"] == {
    "model": "claude-opus-4-7",
    "effort": "high",
  }


def test_patch_chat_clear_reverts_to_default(client, auth, chat):
  """clear_agent_settings=true drops the override entirely."""
  _write_global_settings({"model": "fallback-model"})
  client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "override-model"}},
  )
  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"clear_agent_settings": True},
  )
  assert r.status_code == 200
  body = r.json()
  assert body["agent_settings_json"] is None
  assert body["effective"]["model"] == "fallback-model"


def test_get_chat_includes_effective_settings(client, auth, chat):
  """GET /chats/{id} surfaces both raw override and merged effective."""
  _write_global_settings({"model": "global", "effort": "low"})
  client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "per-chat"}},
  )
  r = client.get(f"/api/chats/{chat.id}", headers=auth)
  body = r.json()
  assert body["agent_settings_json"] == {"model": "per-chat"}
  assert body["effective_agent_settings"]["model"] == "per-chat"
  assert body["effective_agent_settings"]["effort"] == "low"
  assert body["has_assistant_turns"] is False


def test_get_chat_has_assistant_turns_reflects_history(
  client, auth, chat, db,
):
  """The flag flips to True once an assistant message exists."""
  chat.messages = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
  ]
  db.commit()
  r = client.get(f"/api/chats/{chat.id}", headers=auth)
  assert r.json()["has_assistant_turns"] is True


def test_patch_chat_switches_provider_but_does_not_mirror_yet(
  client, auth, chat, db, monkeypatch,
):
  """PATCH /chats/{id} with `provider` switches the chat but does NOT
  mirror to owner.provider — the global default only shifts when the
  user actually SENDS a message with the new provider (see
  `_settings_dirty` + the send path in chats_stream.py). This
  matches the "only manual sent changes update the default" contract
  the user asked for: picking-and-closing the picker without sending
  shouldn't change what new chats inherit.

  Pair with `test_send_after_patch_mirrors_to_owner` for the
  on-send mirror behavior."""
  from app import models, providers

  monkeypatch.setattr(providers.CodexProvider, "check_auth", lambda self, d: None)

  owner_before = db.query(models.Owner).first()
  assert owner_before.provider == "claude"

  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"provider": "codex"},
  )
  assert r.status_code == 200
  body = r.json()
  assert body["provider"] == "codex"

  # Owner.provider stays "claude" — no mirror until send.
  db.expire_all()
  owner_after = db.query(models.Owner).first()
  assert owner_after.provider == "claude"


def test_patch_chat_provider_ignores_unknown_value(client, auth, chat, db):
  """Bogus provider strings are silently ignored — no mirror, no
  crash, existing chat.provider untouched."""
  from app import models

  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"provider": "gemini-pro"},
  )
  assert r.status_code == 200
  db.expire_all()
  owner = db.query(models.Owner).first()
  assert owner.provider == "claude"  # untouched


def test_patch_chat_provider_and_model_in_same_request(
  client, auth, chat, monkeypatch,
):
  """Sending provider + agent_settings_json in one PATCH applies both
  — the slash picker uses this when switching providers (it clears
  the stale per-chat model override at the same time)."""
  from app import providers
  monkeypatch.setattr(providers.CodexProvider, "check_auth", lambda self, d: None)

  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={
      "provider": "codex",
      "agent_settings_json": {"model": "gpt-5.4-codex"},
    },
  )
  assert r.status_code == 200
  body = r.json()
  assert body["provider"] == "codex"
  assert body["agent_settings_json"] == {"model": "gpt-5.4-codex"}


def test_run_chat_passes_merged_settings_into_claude_sdk(
  client, auth, chat, db,
):
  """The smoke contract — when a chat has agent_settings_json,
  `_run_chat_impl` passes the merged dict into run_claude_sdk_turn
  via the `agent_settings` kwarg.

  Mocks the SDK runner so no real LLM call happens; asserts the
  kwarg shape only. Driven via asyncio.run to match the pattern in
  test_codex_sdk_runner.py (the repo doesn't depend on pytest-asyncio).
  """
  from app import chat as chat_mod, schemas

  _write_global_settings({"model": "global-default", "effort": "medium"})
  chat.agent_settings_json = {"model": "claude-opus-4-5"}
  db.commit()

  captured = {}

  async def fake_runner(**kwargs):
    captured.update(kwargs)
    return {
      "session_id": "fake-session-id",
      "cost_usd": 0.0,
      "error": None,
    }

  async def _scenario():
    from app.broadcast import create_broadcast
    create_broadcast(chat.id)
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="hi")],
      chat_id=chat.id,
      session_id=None,
      provider_id="claude",
      run_gen=chat_mod.current_run_generation(chat.id),
    )

  with patch.dict(os.environ, {"MOBIUS_USE_SDK": "1"}), \
       patch(
         "app.claude_sdk_runner.run_claude_sdk_turn",
         side_effect=fake_runner,
       ), \
       patch(
         "app.providers.ClaudeProvider.check_auth",
         return_value=None,
       ):
    asyncio.run(_scenario())

  assert "agent_settings" in captured, (
    "run_claude_sdk_turn must receive agent_settings"
  )
  settings = captured["agent_settings"]
  assert settings["model"] == "claude-opus-4-5"
  assert settings["effort"] == "medium"


