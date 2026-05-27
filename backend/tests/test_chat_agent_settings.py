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

from pydantic import ValidationError

from app.providers import effective_agent_settings
from app.schemas import AgentSettingsOverride, ChatPatch


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
  """clear_agent_settings=true drops the per-chat override entirely.

  Under PATCH-immediate mirror semantics: picking "override-model" in
  the picker writes it to the global default. Clearing this chat's
  override falls back to whatever's now in global — which IS
  "override-model" (the last picked value). To test "clear" against
  a different fallback, reset the global between the PATCH and the
  clear so the global isn't the same value.
  """
  _write_global_settings({"model": "fallback-model"})
  client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "override-model"}},
  )
  # PATCH-immediate mirror just wrote override-model to global.
  # Reset global so we can verify clear falls back to it.
  _write_global_settings({"model": "fallback-model"})
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
  """GET /chats/{id} surfaces both raw override and merged effective.

  Under PATCH-immediate mirror: the per-chat PATCH also writes model
  to global. The global's existing `effort: low` is preserved because
  the mirror is ADDITIVE (it only overwrites keys actually set).
  """
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
  # effort still comes from global, which kept its "low" because the
  # mirror only writes keys present in the chat's settings.
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


def test_patch_chat_provider_mirrors_to_owner_immediately(
  client, auth, chat, db, monkeypatch,
):
  """PATCH /chats/{id} with `provider` mirrors to owner.provider
  immediately so the NEXT new chat inherits the picked provider.

  Earlier revisions of this contract gated the mirror on send, but
  the picker UX broke for the common case: pick a model, open a new
  chat, find it still on the old provider. PATCH-immediate matches
  the "default = last selected" mental model.
  """
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

  # Owner.provider mirrors immediately under PATCH-immediate.
  db.expire_all()
  owner_after = db.query(models.Owner).first()
  assert owner_after.provider == "codex"


def test_patch_chat_provider_rejects_unknown_value(client, auth, chat, db):
  """Bogus provider strings are rejected before the handler runs."""
  from app import models

  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"provider": "gemini-pro"},
  )
  assert r.status_code == 422
  db.expire_all()
  owner = db.query(models.Owner).first()
  assert owner.provider == "claude"  # untouched


def test_chat_patch_provider_validator_rejects_unknown():
  """ChatPatch rejects unknown provider IDs."""
  try:
    ChatPatch(provider="bogus")
  except ValidationError:
    pass
  else:
    raise AssertionError("Expected ValidationError for bogus provider")


def test_agent_settings_override_allows_unknown_keys():
  override = AgentSettingsOverride(
    model="claude-opus-4-7-20251215",
    sandbox_mode="workspace-write",
  )
  assert override.model == "claude-opus-4-7-20251215"
  assert override.model_dump()["sandbox_mode"] == "workspace-write"


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


def test_patch_model_only_with_cross_provider_model_switches_provider(
  client, auth, chat, db, monkeypatch,
):
  """A model-only PATCH whose model belongs to a different provider
  than the chat is currently on must infer the target provider and
  switch atomically — never leave `chat.provider=codex` paired with
  `chat.agent_settings_json.model=claude-sonnet-X`.

  Observed in prod: the picker's same-provider branch sends only
  `{agent_settings_json: {model}}` when it thinks the chat is
  already on that provider. When local picker state diverges from
  the server (TanStack Query refetch landing mid-pick, stale prop,
  etc.) the model field gets persisted but the provider stays
  whatever the DB had. The runner's silent cross-provider fallback
  (codex_sdk_runner / claude_sdk_runner) then re-normalizes at turn
  time, masking the bug AND running the wrong model.

  Backend-level defense: infer the target provider from the model
  when the body didn't state one. Subject to the existing 409-on-
  disconnected-provider guard.
  """
  from app import providers, models
  monkeypatch.setattr(providers.CodexProvider, "check_auth", lambda self, d: None)

  # Chat starts on claude with a claude model.
  assert chat.provider == "claude"
  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "claude-sonnet-4-5-20251001"}},
  )
  assert r.status_code == 200

  # Now PATCH model-only with a Codex model — provider must auto-flip.
  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "gpt-5.4"}},
  )
  assert r.status_code == 200, r.json()
  body = r.json()
  assert body["provider"] == "codex", (
    "provider must auto-switch to codex because gpt-5.4 is a Codex "
    "model and the body didn't explicitly state a provider"
  )
  assert body["agent_settings_json"]["model"] == "gpt-5.4"

  # And the session_id must be cleared so the next turn starts a
  # fresh codex session (the prior session_id was for claude).
  db.expire_all()
  refreshed = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
  assert refreshed.session_id is None
  assert refreshed.provider == "codex"


def test_patch_model_only_cross_provider_409s_if_target_disconnected(
  client, auth, chat, db, monkeypatch,
):
  """Same auto-inference, but if the inferred target provider isn't
  connected, the PATCH must 409 instead of partially committing.
  Without this, a model-only PATCH could leave the chat in a state
  where the next send fails auth — the exact UX the explicit-
  provider 409 was added to prevent.
  """
  from app import providers, models
  # Codex is NOT mocked as connected → check_auth returns an error.
  monkeypatch.setattr(
    providers.CodexProvider,
    "check_auth",
    lambda self, d: "Codex not authenticated",
  )

  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "gpt-5.4"}},
  )
  assert r.status_code == 409
  assert "not connected" in r.json()["detail"].lower()

  # Atomic: chat row unchanged.
  db.expire_all()
  refreshed = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
  assert refreshed.provider == "claude"
  # The model write must also have been rolled back — partial commit
  # would leave model=gpt-5.4 on a claude chat (the original bug).
  assert (refreshed.agent_settings_json or {}).get("model") != "gpt-5.4"


def test_patch_model_only_same_provider_does_not_change_provider(
  client, auth, chat, db, monkeypatch,
):
  """Sanity guard for the auto-inference logic: a model-only PATCH
  whose model belongs to the SAME provider as the chat must NOT
  trigger any provider-switch side-effects (session wipe, auth
  check). This is the happy path the picker's same-provider branch
  uses every time the user changes Sonnet → Opus etc.
  """
  from app import models, providers

  # Hold session_id so we can prove it wasn't wiped.
  chat.session_id = "session-must-survive"
  db.commit()

  # Don't mock codex auth at all — if the handler erroneously triggers
  # a switch, the check_auth on real CodexProvider would 409 (codex not
  # connected in test env). The test passes only if we DON'T hit it.

  r = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"agent_settings_json": {"model": "claude-opus-4-7-20251215"}},
  )
  assert r.status_code == 200, r.json()
  assert r.json()["provider"] == "claude"

  db.expire_all()
  refreshed = db.query(models.Chat).filter(models.Chat.id == chat.id).first()
  assert refreshed.session_id == "session-must-survive", (
    "same-provider model swap must preserve session_id"
  )
