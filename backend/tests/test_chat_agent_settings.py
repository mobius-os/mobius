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

from app import models
from app.providers import effective_agent_settings
from app.schemas import AgentSettingsOverride, ChatPatch


def _write_global_settings(payload: dict) -> None:
  """Writes /data/shared/agent-settings.json under the test DATA_DIR."""
  data_dir = Path(os.environ["DATA_DIR"])
  shared = data_dir / "shared"
  shared.mkdir(parents=True, exist_ok=True)
  (shared / "agent-settings.json").write_text(json.dumps(payload))


def _read_global_settings() -> dict:
  path = Path(os.environ["DATA_DIR"]) / "shared" / "agent-settings.json"
  return json.loads(path.read_text())


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
  """An explicit None masks the last pick so admission can request a choice."""
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(
    json.dumps({"model": "claude-opus-4-5"})
  )
  result = effective_agent_settings(
    str(tmp_path), {"model": None, "effort": "high"},
  )
  assert result == {"model": None, "effort": "high"}


def test_effective_settings_has_no_model_until_manual_pick(tmp_path):
  """No global/chat model remains unresolved for the send-time guard."""
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(json.dumps({}))
  result = effective_agent_settings(str(tmp_path), None)
  assert result["model"] is None
  assert result["effort"] == "medium"


def test_new_chat_provider_follows_last_selected_model(tmp_path):
  """A new chat's provider follows the remembered model's family, so it can't
  be born on a provider whose model belongs to the OTHER family. The stored
  provider (which can drift when many chats run at once) is ignored while a
  model is remembered."""
  from app.providers import owner_default_provider

  shared = tmp_path / "shared"
  shared.mkdir()
  # Last-selected model is a Claude one; the drifted stored provider is codex.
  (shared / "agent-settings.json").write_text(
    json.dumps({"model": "claude-opus-4-8"})
  )
  assert owner_default_provider(str(tmp_path), "codex") == "claude"

  # And the reverse: a remembered Codex model wins over a stale claude provider.
  (shared / "agent-settings.json").write_text(
    json.dumps({"model": "gpt-5.6-sol"})
  )
  assert owner_default_provider(str(tmp_path), "claude") == "codex"


def test_new_chat_provider_follows_live_model_picker_pair(tmp_path):
  """Live discovery is broader than KNOWN_MODELS. The provider persisted in
  the same picker write keeps a newly released model usable without teaching
  the backend its naming convention first."""
  from app.providers import owner_default_provider

  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(json.dumps({
    "model": "future-catalog-id-with-no-family-prefix",
    "provider": "codex",
  }))
  assert owner_default_provider(str(tmp_path), "claude") == "codex"


def test_known_model_repairs_contradictory_picker_provider(tmp_path):
  """A known model remains self-identifying if an old writer or manual edit
  leaves a contradictory provider beside it."""
  from app.providers import owner_default_provider

  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(json.dumps({
    "model": "claude-opus-4-8",
    "provider": "codex",
  }))
  assert owner_default_provider(str(tmp_path), "codex") == "claude"


def test_new_chat_provider_falls_back_to_stored_on_first_run(tmp_path):
  """With no model ever selected, provider comes from the stored value and the
  picker prompts on first send (the one legitimate no-model case)."""
  from app.providers import owner_default_provider

  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "agent-settings.json").write_text(json.dumps({}))
  assert owner_default_provider(str(tmp_path), "codex") == "codex"
  # And nothing resolves a model, so admission still asks for a pick.
  assert effective_agent_settings(
    str(tmp_path), None, provider="codex",
  )["model"] is None


def test_created_chat_provider_follows_remembered_model_over_drift(
  client, auth, db,
):
  """End to end: a drifted owner.provider must not birth a mismatched chat.
  With a Codex model remembered but owner.provider stuck on claude, a new chat
  starts on codex so it resolves to that remembered model instead of nothing."""
  from app import models

  _write_global_settings({"model": "gpt-5.6-sol", "effort": "high"})
  owner = db.query(models.Owner).first()
  owner.provider = "claude"
  db.commit()

  created = client.post(
    "/api/chats", headers=auth, json={"title": "fresh"},
  ).json()
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == created["id"]).first()
  assert row.provider == "codex"


def test_settings_view_provider_follows_remembered_model_over_drift(
  client, auth, db,
):
  """GET /settings reports the provider the last-selected model implies, not a
  drifted owner.provider, so the picker shows a consistent model + provider."""
  from app import models

  _write_global_settings({"model": "gpt-5.6-sol", "effort": "high"})
  owner = db.query(models.Owner).first()
  owner.provider = "claude"
  db.commit()

  body = client.get("/api/settings", headers=auth).json()
  assert body["provider"] == "codex"
  assert body["agent_settings"]["model"] == "gpt-5.6-sol"


def test_chat_detail_pristine_provider_follows_global_model_over_drift(
  client, auth, db,
):
  """A pristine chat's picker shows the model it would actually use. With a
  drifted stored provider and no per-chat model, the detail derives the provider
  from the live global model instead of surfacing no model at all."""
  from app import models

  _write_global_settings({"model": "gpt-5.6-sol", "effort": "high"})
  cid = client.post(
    "/api/chats", headers=auth, json={"title": "drifted"},
  ).json()["id"]
  # Simulate a chat created before the global model's family changed: stored
  # provider frozen on claude, no per-chat model, no assistant turns.
  row = db.query(models.Chat).filter(models.Chat.id == cid).first()
  row.provider = "claude"
  row.agent_settings_json = None
  db.commit()

  body = client.get(f"/api/chats/{cid}", headers=auth).json()
  assert body["provider"] == "codex"
  assert body["effective_agent_settings"]["model"] == "gpt-5.6-sol"


def test_chat_detail_user_only_chat_keeps_its_committed_provider(
  client, auth, db,
):
  """A failed/unfinished first turn is no longer pristine: its next send keeps
  the provider committed when that turn began, even before an assistant row
  exists."""
  from app import models

  _write_global_settings({"model": "gpt-5.6-sol", "effort": "high"})
  cid = client.post(
    "/api/chats",
    headers=auth,
    json={
      "title": "started",
      "messages": [{"role": "user", "content": "hello"}],
    },
  ).json()["id"]
  row = db.query(models.Chat).filter(models.Chat.id == cid).first()
  row.provider = "claude"
  row.agent_settings_json = None
  db.commit()

  body = client.get(f"/api/chats/{cid}", headers=auth).json()
  assert body["has_assistant_turns"] is False
  assert body["provider"] == "claude"
  assert body["effective_agent_settings"]["model"] is None


def test_chat_detail_per_chat_model_outranks_stored_provider(
  client, auth, db,
):
  """An explicit per-chat model decides the picker's provider. Stored provider
  and global default both say claude here, so only the model-priority branch can
  report the codex model's provider — and without it the response would filter
  that per-chat model away to no model at all."""
  from app import models

  _write_global_settings({"model": "claude-sonnet-5", "effort": "high"})
  cid = client.post(
    "/api/chats", headers=auth, json={"title": "per-chat model"},
  ).json()["id"]
  row = db.query(models.Chat).filter(models.Chat.id == cid).first()
  row.provider = "claude"
  row.agent_settings_json = {"model": "gpt-5.6-sol"}
  db.commit()

  body = client.get(f"/api/chats/{cid}", headers=auth).json()
  assert body["provider"] == "codex"
  assert body["effective_agent_settings"]["model"] == "gpt-5.6-sol"


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


def test_auto_resume_is_per_chat_and_survives_runtime_clear(
  client, auth, chat, db,
):
  """The continuation preference is chat-local and independent of models."""
  from app import models

  chat.agent_settings_json = {"model": "historical-model", "effort": "high"}
  chat.provider = "codex"
  db.commit()
  owner = db.query(models.Owner).first()
  owner.provider = "claude"
  db.commit()
  _write_global_settings({"model": "current-model", "effort": "low"})
  other = client.post(
    "/api/chats", headers=auth, json={"title": "other"},
  ).json()

  enabled = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"auto_resume_on_limit": True},
  )
  assert enabled.status_code == 200
  assert enabled.json()["auto_resume_on_limit"] is True
  assert enabled.json()["agent_settings_json"] == {
    "model": "historical-model", "effort": "high",
  }
  assert "auto_resume_on_limit" not in enabled.json()["effective"]
  assert _read_global_settings() == {
    "model": "current-model", "effort": "low",
  }
  db.expire_all()
  assert db.query(models.Owner).first().provider == "claude"

  sibling = client.get(f"/api/chats/{other['id']}", headers=auth).json()
  assert sibling["auto_resume_on_limit"] is False

  cleared = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"clear_agent_settings": True},
  ).json()
  assert cleared["auto_resume_on_limit"] is True
  assert cleared["agent_settings_json"] is None

  detail = client.get(f"/api/chats/{chat.id}", headers=auth).json()
  assert detail["auto_resume_on_limit"] is True

  disabled = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"auto_resume_on_limit": False},
  ).json()
  assert disabled["auto_resume_on_limit"] is False
  assert disabled["agent_settings_json"] is None


def test_new_chat_inherits_last_auto_resume_selection(client, auth, chat):
  """The last explicit choice seeds new chats without rewriting old ones."""
  initial = client.post(
    "/api/chats", headers=auth, json={"title": "initial"},
  ).json()
  assert client.get(
    f"/api/chats/{initial['id']}", headers=auth,
  ).json()["auto_resume_on_limit"] is False

  client.patch(
    f"/api/chats/{chat.id}", headers=auth,
    json={"auto_resume_on_limit": False},
  )
  inherited_off = client.post(
    "/api/chats", headers=auth, json={"title": "inherits off"},
  ).json()
  assert client.get(
    f"/api/chats/{inherited_off['id']}", headers=auth,
  ).json()["auto_resume_on_limit"] is False

  client.patch(
    f"/api/chats/{chat.id}", headers=auth,
    json={"auto_resume_on_limit": True},
  )
  inherited_on = client.post(
    "/api/chats", headers=auth, json={"title": "inherits on"},
  ).json()
  assert client.get(
    f"/api/chats/{inherited_on['id']}", headers=auth,
  ).json()["auto_resume_on_limit"] is True

  assert client.get(
    f"/api/chats/{inherited_off['id']}", headers=auth,
  ).json()["auto_resume_on_limit"] is False
  assert client.get(
    f"/api/chats/{inherited_on['id']}", headers=auth,
  ).json()["auto_resume_on_limit"] is True


def test_restart_continuation_is_always_on_and_not_owner_configurable(
  client, auth, chat, db,
):
  """Planned-restart continuation is always on and has no owner toggle.

  A Möbius-initiated restart should always continue interrupted work, so the
  per-chat column is on for every real chat and is neither exposed nor settable
  through the chat API. It is only ever cleared internally (delegation
  cancellation, covered in test_delegations)."""
  from app import models

  # The runtime setting is no longer part of any chat payload.
  initial = client.get(f"/api/chats/{chat.id}", headers=auth).json()
  assert "auto_resume_on_restart" not in initial

  # Every freshly created chat continues after a restart at the storage layer.
  created = client.post(
    "/api/chats", headers=auth, json={"title": "fresh"},
  ).json()
  assert "auto_resume_on_restart" not in created
  db.expire_all()
  assert db.get(models.Chat, created["id"]).auto_resume_on_restart is True

  # An attempt to turn it off through the API is ignored, not honored.
  patched = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"auto_resume_on_restart": False},
  )
  assert patched.status_code == 200
  assert "auto_resume_on_restart" not in patched.json()
  db.expire_all()
  assert db.get(models.Chat, chat.id).auto_resume_on_restart is True


def test_stale_global_auto_resume_setting_is_not_a_chat_default(
  client, auth, chat,
):
  _write_global_settings({
    "auto_resume_on_limit": False,
    "model": "claude-opus-4-7",
  })
  detail = client.get(f"/api/chats/{chat.id}", headers=auth).json()
  assert detail["auto_resume_on_limit"] is False
  # Reads ignore the removed owner-global key. The one-way file cleanup is a
  # boot migration (covered in test_settings), not a racy write from GET.
  assert _read_global_settings() == {
    "auto_resume_on_limit": False,
    "model": "claude-opus-4-7",
  }


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

  monkeypatch.setattr(
    providers.CodexProvider, "check_auth", lambda self, d: None,
  )

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
    json={"provider": "unsupported-provider"},
  )
  assert r.status_code == 422
  db.expire_all()
  owner = db.query(models.Owner).first()
  assert owner.provider == "claude"  # untouched


def test_first_live_turn_cannot_switch_provider_via_patch(
  client, auth, chat, db,
):
  """The provider is immutable once the first run has claimed the chat."""
  from app import models

  chat.messages = [{"role": "user", "content": "first request"}]
  db.add(models.ChatRun(
    id="first-live-turn",
    chat_id=chat.id,
    status="running",
    provider="claude",
  ))
  db.commit()

  response = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={
      "provider": "codex",
      "agent_settings_json": {"model": "gpt-5.4"},
    },
  )
  assert response.status_code == 409
  db.refresh(chat)
  assert chat.provider == "claude"


def test_chat_patch_provider_validator_rejects_unknown():
  """ChatPatch rejects unknown provider IDs."""
  try:
    ChatPatch(provider="bogus")
  except ValidationError:
    pass
  else:
    raise AssertionError("Expected ValidationError for bogus provider")


def test_agent_settings_override_rejects_unknown_keys():
  """Unknown fields are rejected (extra='forbid'). Round-2 security
  finding H2 — the previous 'allow' policy silently persisted any
  key into chat.agent_settings_json + every GET response."""
  from pydantic import ValidationError
  try:
    AgentSettingsOverride(
      model="claude-opus-4-7-20251215",
      sandbox_mode="workspace-write",
    )
  except ValidationError:
    return
  raise AssertionError("Expected ValidationError for unknown key")


def test_agent_settings_override_accepts_codex_catalog_ultra_effort():
  """Sol/Terra advertise `ultra`; the picker payload must round-trip."""
  settings = AgentSettingsOverride(
    model="gpt-5.6-sol",
    effort="ultra",
    effort_by_provider={"codex": "ultra"},
  )

  assert settings.model_dump(exclude_unset=True) == {
    "model": "gpt-5.6-sol",
    "effort": "ultra",
    "effort_by_provider": {"codex": "ultra"},
  }


def test_patch_chat_provider_and_model_in_same_request(
  client, auth, chat, monkeypatch,
):
  """Sending provider + agent_settings_json in one PATCH applies both
  — the slash picker uses this when switching providers (it clears
  the stale per-chat model override at the same time)."""
  from app import providers
  monkeypatch.setattr(
    providers.CodexProvider, "check_auth", lambda self, d: None,
  )

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
  assert _read_global_settings()["provider"] == "codex"


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

  with patch(
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


def test_patch_cannot_bypass_handoff_after_assistant_turn(
  client, auth, chat, db, monkeypatch,
):
  """A populated chat must use the atomic incoming-provider handoff route."""
  from app import models, providers

  monkeypatch.setattr(
    providers.CodexProvider, "check_auth", lambda self, d: None,
  )
  chat.session_id = "claude-session"
  chat.agent_settings_json = {"model": "claude-sonnet-4-6"}
  chat.messages = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
  ]
  db.commit()

  response = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={
      "provider": "codex",
      "agent_settings_json": {"model": "gpt-5.4"},
    },
  )
  assert response.status_code == 409
  assert "handoff" in response.json()["detail"].lower()
  db.expire_all()
  row = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  assert row.provider == "claude"
  assert row.session_id == "claude-session"
  assert row.agent_settings_json == {"model": "claude-sonnet-4-6"}


def test_patch_rejects_explicit_provider_model_mismatch(client, auth, chat):
  response = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={
      "provider": "claude",
      "agent_settings_json": {"model": "gpt-5.4"},
    },
  )
  assert response.status_code == 422
  assert "does not belong" in response.json()["detail"].lower()


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


def test_run_chat_passes_deployed_skill_and_picker_settings(
  client, auth, chat, db, monkeypatch,
):
  """A turn passes the deployed skill text plus the picker-chosen
  settings into the Claude runner. This is the only path now that the
  named-agent override has been removed."""
  from app import chat as chat_mod, schemas

  _write_global_settings({"model": "global-default", "effort": "medium"})
  chat.agent_settings_json = {"model": "claude-opus-4-5"}
  db.commit()

  monkeypatch.setattr(chat_mod, "_read_skill_text", lambda: "DEPLOYED-SKILL")

  captured = {}

  async def fake_runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "s", "cost_usd": 0.0, "error": None}

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

  with patch(
         "app.claude_sdk_runner.run_claude_sdk_turn",
         side_effect=fake_runner,
       ), \
       patch(
         "app.providers.ClaudeProvider.check_auth",
         return_value=None,
       ):
    asyncio.run(_scenario())

  assert captured["skill_text"].startswith("DEPLOYED-SKILL\n\n")
  assert "<agent_experience>" in captured["skill_text"]
  assert captured["agent_settings"]["model"] == "claude-opus-4-5"
  db.expire_all()
  persisted = db.query(models.Chat).filter(models.Chat.id == chat.id).one()
  assert persisted.system_prompt_snapshot_id
  snapshot = db.get(
    models.SystemPromptSnapshot, persisted.system_prompt_snapshot_id,
  )
  assert snapshot is not None
  assert snapshot.content == "DEPLOYED-SKILL"
