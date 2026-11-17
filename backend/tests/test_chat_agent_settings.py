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
from app import transcript_rows
from sqlalchemy.orm import object_session

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
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


def _start_provider_turn(chat, provider_id: str = "claude") -> str:
  """Admit a provider-dispatch test through the durable turn boundary."""
  from app.chat_writer import StartTurn, get_writer

  run_token = f"rt-agent-settings-{chat.id}"
  get_writer().submit(StartTurn(
    chat_id=chat.id,
    run_token=run_token,
    user_msg={
      "role": "user", "content": "hi", "ts": 1,
      "cid": f"message-{chat.id}",
    },
    title_source="hi",
    default_provider=provider_id,
  )).result(timeout=5)
  return run_token


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
  """clear_agent_settings=true snapshots the current global choice.

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
  assert body["agent_settings_json"]["model"] == "fallback-model"
  assert body["effective"]["model"] == "fallback-model"


def test_clear_picker_settings_keeps_resolver_lifecycle(client, auth, chat, db):
  batch = {"12": {"upstream_commit": "abc", "completed": None}}
  chat.agent_settings_json = {
    "model": "old-model", "_app_update_resolver_batch": batch,
    "archive_on_goal_complete": True,
  }
  db.commit()
  _write_global_settings({"model": "fallback-model"})
  result = client.patch(
    f"/api/chats/{chat.id}", headers=auth,
    json={"clear_agent_settings": True},
  )
  assert result.status_code == 200, result.text
  settings = result.json()["agent_settings_json"]
  assert settings["model"] == "fallback-model"
  assert settings["_app_update_resolver_batch"] == batch
  assert settings["archive_on_goal_complete"] is True


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
  assert cleared["agent_settings_json"]["model"] == "current-model"

  detail = client.get(f"/api/chats/{chat.id}", headers=auth).json()
  assert detail["auto_resume_on_limit"] is True

  disabled = client.patch(
    f"/api/chats/{chat.id}",
    headers=auth,
    json={"auto_resume_on_limit": False},
  ).json()
  assert disabled["auto_resume_on_limit"] is False
  assert disabled["agent_settings_json"]["model"] == "current-model"


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


def test_agent_change_to_one_chat_does_not_become_the_owner_preference(
  client, auth, chat, db,
):
  """Only the owner's own choice carries forward to new chats."""
  from datetime import timedelta
  from app import auth as auth_mod

  client.patch(
    f"/api/chats/{chat.id}", headers=auth,
    json={"auto_resume_on_limit": True},
  )
  run_id = _start_provider_turn(chat)
  owner = db.query(models.Owner).first()
  agent_token = auth_mod.create_agent_token(
    chat.id, owner.username, owner.token_epoch,
    run_id=run_id, expires_delta=timedelta(minutes=5),
  )
  changed = client.patch(
    f"/api/chats/{chat.id}",
    headers={"Authorization": f"Bearer {agent_token}"},
    json={"auto_resume_on_limit": False},
  )
  assert changed.status_code == 200, changed.text
  assert changed.json()["auto_resume_on_limit"] is False

  later = client.post(
    "/api/chats", headers=auth, json={"title": "later"},
  ).json()
  assert client.get(
    f"/api/chats/{later['id']}", headers=auth,
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
  transcript_rows.replace_all(object_session(chat), chat, [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
  ])
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
  assert r.status_code == 200, r.json()
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

  transcript_rows.replace_all(object_session(chat), chat, [{"role": "user", "content": "first request"}])
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
      model="claude-opus-4-7",
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
  client, auth, chat, db, monkeypatch,
):
  """Sending provider + agent_settings_json in one PATCH applies both
  — the slash picker uses this when switching providers (it clears
  the stale per-chat model override at the same time)."""
  from app import providers
  monkeypatch.setattr(
    providers.CodexProvider, "check_auth", lambda self, d: None,
  )
  owner = db.query(models.Owner).first()
  owner.model_prefs_json = {"recent_ids": ["claude-opus-4-7"]}
  db.commit()

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
  prefs = client.get("/api/owner/model-prefs", headers=auth).json()
  assert prefs["recent_ids"] == ["claude-opus-4-7"]


def test_picker_recency_ignores_unsent_model_choices(client, auth, chat):
  """Model, effort and rejected provider changes cannot reorder before send."""
  url = f"/api/chats/{chat.id}"
  first = client.patch(url, headers=auth, json={
    "agent_settings_json": {"model": "claude-sonnet-4-6"},
  })
  assert first.status_code == 200, first.text
  second = client.patch(url, headers=auth, json={
    "agent_settings_json": {"model": "claude-opus-4-7"},
  })
  assert second.status_code == 200, second.text
  effort = client.patch(url, headers=auth, json={
    "agent_settings_json": {"effort": "high"},
  })
  assert effort.status_code == 200, effort.text
  rejected = client.patch(url, headers=auth, json={
    "provider": "codex", "agent_settings_json": {"model": "claude-sonnet-4-6"},
  })
  assert rejected.status_code == 422
  prefs = client.get("/api/owner/model-prefs", headers=auth).json()
  assert prefs["recent_ids"] == []


def test_queued_owner_messages_do_not_claim_unfrozen_model(
  client, auth, chat, db, monkeypatch,
):
  """A queued row may later execute under another choice; do not claim one."""
  from app.routes import chats_stream

  monkeypatch.setattr(chats_stream, "_input_must_queue", lambda *a, **k: True)
  url = f"/api/chats/{chat.id}"
  prefs_url = "/api/owner/model-prefs"
  owner = db.query(models.Owner).first()
  owner.model_prefs_json = {"recent_ids": ["claude-opus-4-7"]}
  db.commit()

  picked = client.patch(url, headers=auth, json={
    "agent_settings_json": {"model": "claude-sonnet-4-6"},
  })
  assert picked.status_code == 200, picked.text
  assert client.get(prefs_url, headers=auth).json()["recent_ids"] == ["claude-opus-4-7"]

  sent = client.post(f"{url}/messages", headers=auth, json={
    "content": "hello", "cid": "recent-send",
  })
  assert sent.status_code == 202, sent.text
  assert sent.json()["status"] == "queued"
  assert client.get(prefs_url, headers=auth).json()["recent_ids"] == ["claude-opus-4-7"]

  from fastapi import HTTPException

  async def refuse_append(*_args, **_kwargs):
    raise HTTPException(503, detail="store unavailable")

  with monkeypatch.context() as failed_store:
    failed_store.setattr(chats_stream, "_append_to_pending", refuse_append)
    refused = client.post(f"{url}/messages", headers=auth, json={
      "content": "not accepted", "cid": "recent-refused",
    })
  assert refused.status_code == 503
  assert client.get(prefs_url, headers=auth).json()["recent_ids"] == ["claude-opus-4-7"]

  # Selection changes the default independently but cannot change ordering.
  repicked = client.patch(url, headers=auth, json={
    "agent_settings_json": {"model": "claude-opus-4-7"},
  })
  assert repicked.status_code == 200, repicked.text
  retry = client.post(f"{url}/messages", headers=auth, json={
    "content": "hello", "cid": "recent-send",
  })
  assert retry.status_code in (200, 202), retry.text
  hidden = client.post(f"{url}/messages", headers=auth, json={
    "content": "internal", "cid": "recent-hidden", "hidden": True,
  })
  assert hidden.status_code == 202, hidden.text
  assert client.get(prefs_url, headers=auth).json()["recent_ids"] == ["claude-opus-4-7"]


def test_promoted_owner_queue_records_actual_model_not_waking_post(
  client, auth, chat, db, monkeypatch,
):
  """An owner POST may wake an older non-owner head; only its own turn counts."""
  from app.chat_writer import AppendPending, PromotePending, get_writer
  from app.routes import chats_stream

  monkeypatch.setattr(chats_stream, "_schedule_continuation", lambda **_kwargs: True)
  monkeypatch.setattr(chats_stream, "_input_must_queue", lambda *_a, **_k: False)
  monkeypatch.setattr(chats_stream, "is_chat_running", lambda _id: False)
  monkeypatch.setattr(chats_stream, "mark_starting", lambda _id: True)
  owner = db.query(models.Owner).first()
  owner.model_prefs_json = {"recent_ids": ["claude-sonnet-4-6", "claude-opus-4-7"]}
  chat.agent_settings_json = {"model": "claude-opus-4-7"}
  db.commit()
  get_writer().submit(AppendPending(
    chat_id=chat.id, run_token="",
    user_msg={"role": "user", "content": "older agent input", "hidden": True, "ts": 1,
              "cid": "older-agent"},
  )).result(timeout=5)

  woke = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "owner follow-up", "cid": "queued-owner",
  })
  assert woke.status_code == 202, woke.text
  assert woke.json()["status"] == "queued"
  assert woke.json()["started"] is True
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == [
    "claude-sonnet-4-6", "claude-opus-4-7",
  ]

  # The queued owner row had no model promise; promotion uses the then-current
  # chat selection, not the model when it entered the queue.
  switched = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "agent_settings_json": {"model": "claude-opus-4-8"},
  })
  assert switched.status_code == 200, switched.text

  promoted = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="promoted-owner-run",
  )).result(timeout=5)
  assert promoted["promoted"] is not None
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == [
    "claude-opus-4-8", "claude-sonnet-4-6", "claude-opus-4-7",
  ]


def test_direct_owner_send_recency_excludes_embed_guest_app_and_helper(db):
  """Immediate admission uses direct owner authority, not embed's owner-input stamp."""
  from app.deps import Principal
  from app.routes.chats_stream import _owner_model_use_send
  from app.schemas import SendMessage

  owner = db.query(models.Owner).first()
  body = SendMessage(content="visible human input")
  assert _owner_model_use_send(Principal(owner=owner, app_id=None), body)
  assert not _owner_model_use_send(Principal(
    owner=owner, app_id=42, scope="chat_embed", chat_id="embedded-chat",
  ), body)
  assert not _owner_model_use_send(Principal(
    owner=owner, app_id=None, browser=object(),
  ), body)
  assert not _owner_model_use_send(Principal(
    owner=owner, app_id=42, scope="app",
  ), body)
  assert not _owner_model_use_send(Principal(
    owner=owner, app_id=None, delegation_id="helper-delegation",
  ), body)


@pytest.mark.parametrize("app_id,owner_input,expect_promoted", [
  (42, True, False),   # chat_embed has owner-input provenance, but is app-origin
  (42, False, False),  # app token
  (None, False, False),  # agent/helper origin
  (None, True, True),  # ordinary owner (app-created chat is tested separately)
])
def test_queued_model_recency_uses_promoted_group_provenance(
  client, auth, chat, db, app_id, owner_input, expect_promoted,
):
  """A queue's own admitted row, never its waking POST, decides recency."""
  from app.chat_writer import AppendPending, PromotePending, get_writer

  owner = db.query(models.Owner).first()
  owner.model_prefs_json = {"recent_ids": ["claude-sonnet-4-6", "claude-opus-4-7"]}
  chat.agent_settings_json = {"model": "claude-opus-4-7"}
  if app_id is not None:
    app = models.App(
      slug="queued-recency-app", source_dir="/tmp/queued-recency-app",
      name="Queued recency app", description="test",
      jsx_source="export default () => null",
    )
    db.add(app)
    db.flush()
    app_id = app.id
  db.commit()
  appended = get_writer().submit(AppendPending(
    chat_id=chat.id, run_token="",
    user_msg={"role": "user", "content": "queued input", "ts": 1,
              "cid": "queued-provenance"},
    initiated_by_app_id=app_id, owner_input=owner_input,
  )).result(timeout=5)
  row = appended["stored"]
  assert bool(row.get("_owner_input_at")) is owner_input
  assert row.get("_initiated_by_app_id") == app_id
  before = client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"]
  assert before == ["claude-sonnet-4-6", "claude-opus-4-7"]

  promoted = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="provenance-promoted-run",
  )).result(timeout=5)
  assert promoted["promoted"] is not None
  after = client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"]
  assert after == (["claude-opus-4-7", "claude-sonnet-4-6"] if expect_promoted else before)


def test_direct_steer_uses_active_model_not_new_same_provider_pick(
  client, auth, chat, db, monkeypatch,
):
  """A picker change mid-turn does not change the live steer target."""
  from app.chat_writer import AppendSteeredUserMessage, get_writer
  from app.routes import chats_stream

  owner = db.query(models.Owner).first()
  owner.model_prefs_json = {"recent_ids": ["claude-sonnet-4-6", "claude-opus-4-7"]}
  chat.agent_settings_json = {"model": "claude-sonnet-4-6"}
  db.commit()
  monkeypatch.setattr(chats_stream, "is_chat_running", lambda _id: True)
  monkeypatch.setattr(chats_stream, "has_live_steerable_turn", lambda *_a: True)
  monkeypatch.setattr(chats_stream, "_browser_may_steer_run", lambda *_a: True)

  async def admit_steer(*_args):
    return True

  monkeypatch.setattr(chats_stream, "steer_into_active_turn", admit_steer)
  sent = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "course correction", "cid": "active-model-steer", "direct_steer": True,
  })
  assert sent.status_code == 202, sent.text
  assert sent.json()["status"] == "steered"
  # The HTTP acknowledgement alone does not prove provider delivery.
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == [
    "claude-sonnet-4-6", "claude-opus-4-7",
  ]
  db.refresh(chat)
  row = next(m for m in chat.pending_messages if m.get("cid") == "active-model-steer")
  get_writer().submit(AppendSteeredUserMessage(
    chat_id=chat.id, run_token="", user_msgs=[row],
    consume_pending_cids=["active-model-steer"],
    active_model="claude-opus-4-7",
  )).result(timeout=5)
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == [
    "claude-opus-4-7", "claude-sonnet-4-6",
  ]


@pytest.mark.parametrize("origin,owner_input,expected_use", [
  ("owner", True, True),
  ("embed", True, False),
  ("app", False, False),
  ("guest", True, False),
  ("helper", False, False),
])
@pytest.mark.parametrize("removed_before_cut", [False, True])
def test_steer_cut_recency_uses_original_pending_provenance(
  client, auth, chat, db, origin, owner_input, expected_use, removed_before_cut,
):
  """Only a newly delivered direct-owner row may reorder at a live cut."""
  from app.browser_access import BrowserAccessGrant
  from app.chat_writer import (
    AppendPending, AppendSteeredUserMessage, CancelPending, get_writer,
  )

  owner = db.query(models.Owner).first()
  owner.model_prefs_json = {"recent_ids": ["claude-sonnet-4-6", "claude-opus-4-7"]}
  chat.agent_settings_json = {"model": "claude-sonnet-4-6"}
  app_id = None
  grant_id = None
  if origin in ("embed", "app"):
    app = models.App(
      slug="steer-provenance-app", source_dir="/tmp/steer-provenance-app",
      name="Steer provenance app", description="test",
      jsx_source="export default () => null",
    )
    db.add(app)
    db.flush()
    app_id = app.id
  if origin == "guest":
    grant_id = "steer-guest-grant-1234567890"
    db.add(BrowserAccessGrant(
      id=grant_id, owner_id=owner.id, label="Steer guest", kind="invitation",
    ))
  db.commit()
  cid = f"steered-{origin}"
  row = get_writer().submit(AppendPending(
    chat_id=chat.id, run_token="", owner_input=owner_input,
    initiated_by_app_id=app_id, browser_grant_id=grant_id,
    user_msg={"role": "user", "content": "course correction", "ts": 10,
              "cid": cid},
  )).result(timeout=5)["stored"]
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == [
    "claude-sonnet-4-6", "claude-opus-4-7",
  ]
  if removed_before_cut:
    removed = get_writer().submit(CancelPending(
      chat_id=chat.id, run_token="", cid=cid,
    )).result(timeout=5)
    assert removed["pending"] == []

  def cut():
    return get_writer().submit(AppendSteeredUserMessage(
      chat_id=chat.id, run_token="", user_msgs=[row],
      consume_pending_cids=[cid], active_model="claude-opus-4-7",
    )).result(timeout=5)

  first = cut()
  assert len(first["stored_messages"]) == 1
  expected = (["claude-opus-4-7", "claude-sonnet-4-6"] if expected_use else
              ["claude-sonnet-4-6", "claude-opus-4-7"])
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == expected
  # The writer's cid gate discards a repeated delivery and never promotes it.
  second = cut()
  assert second["stored_messages"] == []
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == expected


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("removed_before_cut", [False, True])
def test_steered_saved_question_answer_never_promotes_model_use(
  client, auth, chat, db, hidden, removed_before_cut,
):
  """A card answer stays a continuation even at a deferred provider cut."""
  from app.chat_writer import (
    AppendPending, AppendSteeredUserMessage, CancelPending, get_writer,
  )

  owner = db.query(models.Owner).first()
  owner.model_prefs_json = {"recent_ids": ["claude-sonnet-4-6", "claude-opus-4-7"]}
  db.commit()
  cid = f"steered-answer-{hidden}-{removed_before_cut}"
  row = get_writer().submit(AppendPending(
    chat_id=chat.id, run_token="", owner_input=True,
    user_msg={"role": "user", "content": "saved answer", "cid": cid,
              "hidden": hidden, "kind": "continuation",
              "continuation_reason": "question_answer"},
  )).result(timeout=5)["stored"]
  assert row.get("_owner_input_at")
  if removed_before_cut:
    get_writer().submit(CancelPending(
      chat_id=chat.id, run_token="", cid=cid,
    )).result(timeout=5)

  def cut():
    return get_writer().submit(AppendSteeredUserMessage(
      chat_id=chat.id, run_token="", user_msgs=[row],
      consume_pending_cids=[cid], active_model="claude-opus-4-7",
    )).result(timeout=5)

  assert len(cut()["stored_messages"]) == 1
  assert cut()["stored_messages"] == []
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == [
    "claude-sonnet-4-6", "claude-opus-4-7",
  ]


def test_first_send_freezes_model_before_later_global_picker_change(
  client, auth, chat, db, monkeypatch,
):
  """The runner and MRU agree even if another chat changes global defaults."""
  from app import providers
  from app.routes import chats_stream

  chat.agent_settings_json = None
  db.commit()
  providers.update_agent_settings(os.environ["DATA_DIR"], lambda current: {
    **current, "provider": "claude", "model": "claude-sonnet-4-6",
  })
  original_ack = chats_stream.await_ack

  async def change_global_after_admission(ack):
    result = await original_ack(ack)
    providers.update_agent_settings(os.environ["DATA_DIR"], lambda current: {
      **current, "provider": "claude", "model": "claude-opus-4-7",
    })
    return result

  async def no_paid_turn(*_args, **_kwargs):
    return None

  monkeypatch.setattr(chats_stream, "await_ack", change_global_after_admission)
  monkeypatch.setattr(chats_stream, "run_chat", no_paid_turn)
  sent = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "first", "cid": "frozen-first",
  })
  assert sent.status_code == 202, sent.text
  db.refresh(chat)
  assert chat.agent_settings_json["model"] == "claude-sonnet-4-6"
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"][0] == "claude-sonnet-4-6"


def test_recency_write_failure_rejects_uncommitted_fresh_send(
  client, auth, chat, db, monkeypatch,
):
  from app import chat_writer

  def unavailable(*_args):
    raise RuntimeError("owner preferences unavailable")

  monkeypatch.setattr(
    chat_writer.ChatWriterActor, "_remember_admitted_owner_model_use",
    staticmethod(unavailable),
  )
  sent = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "accepted", "cid": "prefs-unavailable",
  })
  assert sent.status_code == 503, sent.text
  db.refresh(chat)
  assert not any(row.get("cid") == "prefs-unavailable" for row in transcript_rows.history(chat))
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == []


@pytest.mark.parametrize("selected,offline,accepted", [
  ("new-vision", False, True),
  ("removed-vision", False, False),
  ("new-vision", True, False),
])
def test_cold_mobius_catalogue_validates_before_owner_send_admission(
  client, auth, chat, db, monkeypatch, selected, offline, accepted,
):
  """Cold discovery admits only listed models, without a speculative write."""
  from app import providers
  from app.mobius_catalogue import parse_catalogue
  from app.routes import chats_stream
  from tests.test_mobius_catalogue import contract

  provider = providers.MobiusProvider()
  monkeypatch.setitem(providers.PROVIDERS, "mobius", provider)
  monkeypatch.setattr(provider, "check_auth", lambda _data_dir: None)
  providers.invalidate_model_cache()
  catalogue = contract()
  discovered = []

  async def discover(_data_dir):
    discovered.append(True)
    if offline:
      raise OSError("catalogue unavailable")
    provider.declaration = parse_catalogue(catalogue)
    return provider.declaration["models"]

  monkeypatch.setattr(provider, "fetch_models", discover)

  async def no_paid_turn(*_args, **_kwargs):
    return None

  monkeypatch.setattr(chats_stream, "run_chat", no_paid_turn)
  chat.provider = "mobius"
  chat.agent_settings_json = {"model": selected, "effort": "low"}
  db.commit()

  sent = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "use my selected model", "cid": "cold-mobius-send",
  })
  assert discovered
  assert sent.status_code == (202 if accepted else 409), sent.text
  db.refresh(chat)
  assert chat.agent_settings_json == {"model": selected, "effort": "low"}
  history = transcript_rows.history(chat)
  assert any(row.get("cid") == "cold-mobius-send" for row in history) is accepted
  assert chat.pending_messages in (None, [])
  assert db.query(models.ChatRun).filter(models.ChatRun.chat_id == chat.id).count() == int(accepted)
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == (
    [selected] if accepted else []
  )


def test_cold_removed_mobius_model_cannot_enter_owner_queue(
  client, auth, chat, db, monkeypatch,
):
  from app import providers
  from app.mobius_catalogue import parse_catalogue
  from app.routes import chats_stream
  from tests.test_mobius_catalogue import contract

  provider = providers.MobiusProvider()
  monkeypatch.setitem(providers.PROVIDERS, "mobius", provider)
  monkeypatch.setattr(provider, "check_auth", lambda _data_dir: None)
  providers.invalidate_model_cache()
  catalogue = contract()

  async def discover(_data_dir):
    provider.declaration = parse_catalogue(catalogue)
    return provider.declaration["models"]

  monkeypatch.setattr(provider, "fetch_models", discover)
  monkeypatch.setattr(chats_stream, "_input_must_queue", lambda *_a, **_k: True)
  chat.provider = "mobius"
  chat.agent_settings_json = {"model": "removed-vision"}
  db.commit()

  sent = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "queue this", "cid": "removed-model-queue",
  })
  assert sent.status_code == 409, sent.text
  db.refresh(chat)
  assert chat.agent_settings_json == {"model": "removed-vision"}
  assert not transcript_rows.history(chat)
  assert not chat.pending_messages
  assert db.query(models.ChatRun).filter(models.ChatRun.chat_id == chat.id).count() == 0
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == []


def test_human_send_in_app_created_chat_counts_model_use(
  client, auth, chat, db, monkeypatch,
):
  from app.routes import chats_stream

  app = models.App(
    slug="human-app-chat", source_dir="/tmp/human-app-chat",
    name="Human app chat", description="test", jsx_source="export default () => null",
  )
  db.add(app)
  db.flush()
  chat.created_by_app_id = app.id
  db.commit()

  async def no_paid_turn(*_args, **_kwargs):
    return None

  monkeypatch.setattr(chats_stream, "run_chat", no_paid_turn)
  expected_model = chats_stream._selected_model_for_chat(chat)
  sent = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "human message", "cid": "app-chat-human",
  })
  assert sent.status_code == 202, sent.text
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"][0] == expected_model


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
    run_token = _start_provider_turn(chat)
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="hi")],
      chat_id=chat.id,
      session_id=None,
      provider_id="claude",
      run_gen=chat_mod.current_run_generation(chat.id),
      run_token=run_token,
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
  # spawn_agent defaults helpers to this provider, so it must be the id the
  # Subagents app knows ("claude"), never the display name ("Claude Code").
  assert captured["base_env"]["MOBIUS_AGENT_PROVIDER"] == "claude"
  assert captured["base_env"]["MOBIUS_AGENT_MODEL"] == settings["model"]
  assert captured["base_env"]["MOBIUS_AGENT_EFFORT"] == settings["effort"]
  assert "MOBIUS_SUBAGENT_HELPER" not in captured["base_env"]


def test_claude_receives_an_owner_goal_command_as_a_plain_request(
  client, auth, chat, db,
):
  """Claude's CLI has its own /goal. Möbius owns the command, so the agent's
  copy must never start with it: the CLI would echo the hidden context Möbius
  appends as a visible "Goal set:" reply and arm a second goal loop."""
  from app import chat as chat_mod, schemas

  captured = {}

  async def fake_runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "fake-session-id", "cost_usd": 0.0, "error": None}

  async def _scenario():
    from app.broadcast import create_broadcast

    create_broadcast(chat.id)
    run_token = _start_provider_turn(chat)
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="/goal ship the fix")],
      chat_id=chat.id,
      session_id=None,
      provider_id="claude",
      run_gen=chat_mod.current_run_generation(chat.id),
      run_token=run_token,
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

  prompt = captured["user_message"]
  # Per-run delivery instructions may precede the time header; neither may
  # turn the owner's request back into Claude's native slash command.
  assert any(line.startswith("[Context — current time:") for line in prompt.splitlines())
  assert prompt.endswith("\n\nGoal: ship the fix")
  assert not any(line.startswith("/goal") for line in prompt.splitlines())


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
    json={"agent_settings_json": {"model": "claude-sonnet-4-5-20250929"}},
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
  transcript_rows.replace_all(object_session(chat), chat, [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
  ])
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
    json={"agent_settings_json": {"model": "claude-opus-4-7"}},
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
    run_token = _start_provider_turn(chat)
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="hi")],
      chat_id=chat.id,
      session_id=None,
      provider_id="claude",
      run_gen=chat_mod.current_run_generation(chat.id),
      run_token=run_token,
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


def test_new_chat_composes_app_prompt_fragments_off_the_event_loop(
  client, auth, chat, db, monkeypatch,
):
  """Fragment reads run Git per app, so they must not block the server's loop."""
  from app import chat as chat_mod, schemas, system_prompts

  monkeypatch.setattr(chat_mod, "_read_skill_text", lambda: "DEPLOYED-SKILL")
  db.add(models.App(
    slug="memory", name="Memory", source_dir="/nonexistent/memory",
    source_commit="0" * 40, system_prompt_file="memory-core.md",
  ))
  db.commit()
  on_loop = []

  def read_fragment(source_dir, source_commit, basename):
    try:
      asyncio.get_running_loop()
      on_loop.append(True)
    except RuntimeError:
      on_loop.append(False)
    return "APP FRAGMENT"

  monkeypatch.setattr(system_prompts, "_read_safe_fragment", read_fragment)
  captured = {}

  async def fake_runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "s", "cost_usd": 0.0, "error": None}

  async def _scenario():
    from app.broadcast import create_broadcast
    create_broadcast(chat.id)
    run_token = _start_provider_turn(chat)
    await chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="hi")],
      chat_id=chat.id,
      session_id=None,
      provider_id="claude",
      run_gen=chat_mod.current_run_generation(chat.id),
      run_token=run_token,
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

  assert on_loop == [False]
  assert "APP FRAGMENT" in captured["skill_text"]


def test_owner_send_with_json_text_settings_records_model_use(
  client, auth, chat, db, monkeypatch,
):
  """Supported string-mode settings must not make an ordinary send fail."""
  from app.routes import chats_stream
  chat.agent_settings_json = json.dumps({"model": "claude-sonnet-4-6"})
  db.commit()

  async def no_paid_turn(*_args, **_kwargs):
    return None
  monkeypatch.setattr(chats_stream, "run_chat", no_paid_turn)
  sent = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "hello", "cid": "string-settings-owner-send",
  })
  assert sent.status_code == 202, sent.text
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"][0] == "claude-sonnet-4-6"
