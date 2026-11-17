"""Model discovery and chat routing must agree about provider identity."""

import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from app import providers, transcript_rows


@pytest.fixture
def discovered_models(monkeypatch):
  # Opaque ids prove ownership comes from discovery, not naming heuristics.
  models = {"claude": "new-discovered-a", "codex": "new-discovered-b"}
  monkeypatch.setattr(providers, "_model_registry_cache", {
    provider: (time.monotonic() - 10_000, [{"id": model, "available": False}])
    for provider, model in models.items()
  })
  return models


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_discovered_model_identity_survives_catalog_expiry_and_unavailability(
  discovered_models, provider,
):
  model = discovered_models[provider]
  other = "codex" if provider == "claude" else "claude"
  assert providers.provider_of_model(model) == provider
  assert not providers._model_belongs_to_other_provider(model, provider)
  assert providers._model_belongs_to_other_provider(model, other)
  # A partial live catalog must not erase the offline identities.
  assert providers.provider_of_model(providers.KNOWN_MODELS[provider][0]) == provider


def test_unknown_models_still_belong_to_the_explicit_provider(tmp_path, monkeypatch):
  monkeypatch.setattr(providers, "_model_registry_cache", {})
  model = "future-model-not-yet-discovered"
  assert providers.provider_of_model(model) is None
  assert providers.snapshot_chat_agent_settings(
    str(tmp_path), "codex", model=model,
  )["model"] == model


def test_new_chat_choice_never_splits_a_concurrent_picker_write(tmp_path, monkeypatch):
  data_dir = str(tmp_path)
  path = tmp_path / "shared" / "agent-settings.json"
  path.parent.mkdir()
  path.write_text(json.dumps({"provider": "claude", "model": "old-undiscovered-id"}))
  original = providers.owner_default_provider
  calls = []

  def resolve_then_pick(*args):
    provider = original(*args)
    calls.append(provider)
    if len(calls) == 1:
      # A real pick from another thread lands between provider resolution and
      # the model snapshot; the first read pair is torn and must be retried.
      with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(providers.write_agent_settings, data_dir, {
          "provider": "codex", "model": "new-undiscovered-id",
        }).result()
    return provider

  monkeypatch.setattr(providers, "owner_default_provider", resolve_then_pick)
  provider, settings = providers.snapshot_owner_chat_choice(data_dir, "claude")
  assert calls == ["claude", "codex"]
  assert (provider, settings["model"]) == ("codex", "new-undiscovered-id")


def test_new_chat_choice_refreshes_apps_outside_the_settings_lock(tmp_path, monkeypatch):
  held = []

  def record_sync(*_args, **_kwargs):
    acquired = providers._AGENT_SETTINGS_LOCK.acquire(blocking=False)
    if acquired:
      providers._AGENT_SETTINGS_LOCK.release()
    # Acquired from another thread proves no thread holds the lock here.
    held.append(not acquired)

  def sync_from_other_thread(*args, **kwargs):
    with ThreadPoolExecutor(max_workers=1) as pool:
      pool.submit(record_sync, *args, **kwargs).result()

  monkeypatch.setattr(providers, "sync_app_model_providers", sync_from_other_thread)
  providers.snapshot_owner_chat_choice(str(tmp_path), "claude")
  assert held and not any(held)


def test_same_provider_pick_on_a_disconnected_provider_is_refused(
  client, auth, chat, db, monkeypatch,
):
  # The picker now sends the provider with every pick, so a same-provider pick
  # gets the same sign-in check as a switch instead of saving a dead choice.
  monkeypatch.setattr(
    providers.ClaudeProvider, "check_auth", lambda self, d: "not signed in",
  )
  before = dict(chat.agent_settings_json or {})
  response = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "provider": "claude",
    "agent_settings_json": {"model": providers.KNOWN_MODELS["claude"][0]},
  })
  assert response.status_code == 409, response.text
  assert "not connected" in response.json()["detail"]
  db.refresh(chat)
  assert chat.provider == "claude"
  assert dict(chat.agent_settings_json or {}) == before


def test_explicit_picker_pair_routes_a_new_model_with_a_cold_catalog(
  client, auth, chat, monkeypatch,
):
  monkeypatch.setattr(providers, "_model_registry_cache", {})
  monkeypatch.setattr(providers.CodexProvider, "check_auth", lambda self, d: None)
  response = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "provider": "codex",
    "agent_settings_json": {"model": "new-undiscovered-id"},
  })
  assert response.status_code == 200, response.text
  assert response.json()["provider"] == "codex"
  assert response.json()["effective"]["model"] == "new-undiscovered-id"


def test_explicit_picker_pair_preserves_handoff_with_a_cold_catalog(
  client, auth, chat, db, monkeypatch,
):
  monkeypatch.setattr(providers, "_model_registry_cache", {})
  transcript_rows.replace_all(db, chat, [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hello"},
  ])
  db.commit()
  response = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "provider": "codex",
    "agent_settings_json": {"model": "new-undiscovered-id"},
  })
  assert response.status_code == 409, response.text
  db.refresh(chat)
  assert chat.provider == "claude"


def test_discovered_claude_model_cannot_be_admitted_as_codex(
  tmp_path, discovered_models,
):
  model = discovered_models["claude"]
  assert providers.effective_agent_settings(
    str(tmp_path), {"model": model}, provider="codex",
  )["model"] is None
  with pytest.raises(ValueError, match="does not belong"):
    providers.snapshot_chat_agent_settings(str(tmp_path), "codex", model=model)


def test_app_cannot_claim_a_discovered_native_model(discovered_models, monkeypatch):
  monkeypatch.setattr(providers, "PROVIDERS", dict(providers.PROVIDERS))
  monkeypatch.setattr(providers, "_app_provider_ids", set())
  monkeypatch.setattr(providers, "_app_provider_sync_at", 0.0)
  model = discovered_models["claude"]
  providers._apply_app_model_declarations([(987654, {
    "models": [{"id": model}], "default_model": model,
  }, "")])
  assert "app-987654" not in providers.PROVIDERS


def test_new_chat_repairs_stale_provider_for_discovered_model(
  client, auth, discovered_models,
):
  path = Path(os.environ["DATA_DIR"]) / "shared" / "agent-settings.json"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps({"provider": "codex", "model": discovered_models["claude"]}))
  response = client.post("/api/chats", headers=auth, json={"title": "New"})
  assert response.status_code == 200, response.text
  detail = client.get(f"/api/chats/{response.json()['id']}", headers=auth).json()
  assert detail["provider"] == "claude"
  assert detail["agent_settings_json"]["model"] == discovered_models["claude"]


def test_model_only_pick_switches_discovered_provider_atomically(
  client, auth, chat, db, discovered_models, monkeypatch,
):
  monkeypatch.setattr(providers.CodexProvider, "check_auth", lambda self, d: None)
  response = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "agent_settings_json": {"model": discovered_models["codex"]},
  })
  assert response.status_code == 200, response.text
  assert response.json()["provider"] == "codex"
  assert response.json()["effective"]["model"] == discovered_models["codex"]
  db.refresh(chat)
  assert chat.provider == "codex"
  saved = json.loads((Path(os.environ["DATA_DIR"]) / "shared" / "agent-settings.json").read_text())
  assert (saved["provider"], saved["model"]) == ("codex", discovered_models["codex"])


def test_discovered_model_pick_cannot_bypass_existing_handoff(
  client, auth, chat, db, discovered_models,
):
  transcript_rows.replace_all(db, chat, [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hello"},
  ])
  db.commit()
  response = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "agent_settings_json": {"model": discovered_models["codex"]},
  })
  assert response.status_code == 409, response.text
  db.refresh(chat)
  assert chat.provider == "claude"
  assert (chat.agent_settings_json or {}).get("model") != discovered_models["codex"]


def test_accepted_app_keeps_its_models_when_discovery_later_lists_them(
  client, auth, chat, db, tmp_path, monkeypatch,
):
  from app import models
  from app.app_capabilities import contract_from_manifest
  from app.config import get_settings

  for name in ("PROVIDERS", "DEFAULT_MODELS", "DEFAULT_BACKGROUND_MODELS",
               "_model_registry_locks"):
    monkeypatch.setattr(providers, name, dict(getattr(providers, name)))
  monkeypatch.setattr(providers, "_app_provider_ids", set())
  monkeypatch.setattr(providers, "_model_registry_cache", {})
  shared = "shared-opaque-model"
  app = models.App(
    name="Example Models", slug="example-models", description="",
    source_dir=str(tmp_path), capability_contract=contract_from_manifest({
      "id": "example-models", "name": "Example Models", "version": "1.0.0",
      "description": "Example", "entry": "index.jsx",
      "model_provider": {
        "name": "Example", "base_url": "https://models.example.invalid",
        "secret_name": "api_key", "default_model": shared,
        "models": [{"id": shared, "label": "Shared", "effort_levels": ["low"]}],
      },
    }),
  )
  db.add(app)
  db.commit()
  provider_id = f"app-{app.id}"
  data_dir = get_settings().data_dir
  providers.sync_app_model_providers(data_dir, force=True)
  assert provider_id in providers.PROVIDERS
  chat.provider = provider_id
  chat.agent_settings_json = {"model": shared}
  db.commit()

  # Codex discovery later lists the same id. The accepted app keeps it, and
  # the id is shared by (provider, model) rather than moved to Codex.
  providers._model_registry_cache["codex"] = (
    time.monotonic(), [{"id": shared, "available": True}],
  )
  providers.sync_app_model_providers(data_dir, force=True)
  assert provider_id in providers.PROVIDERS
  assert not providers._model_belongs_to_other_provider(shared, provider_id)
  assert not providers._model_belongs_to_other_provider(shared, "codex")
  assert providers.provider_of_model(shared, preferred=provider_id) == provider_id
  assert providers.effective_agent_settings(
    data_dir, {"model": shared}, provider=provider_id,
  )["model"] == shared

  response = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "agent_settings_json": {"model": shared},
  })
  assert response.status_code == 200, response.text
  assert response.json()["provider"] == provider_id
  detail = client.get(f"/api/chats/{chat.id}", headers=auth).json()
  assert detail["provider"] == provider_id
  db.refresh(chat)
  assert chat.provider == provider_id

  # A different app accepted only now still cannot claim the discovered id.
  providers._apply_app_model_declarations([
    *providers._read_app_model_declarations(),
    (987654, {"models": [{"id": shared}], "default_model": shared}, ""),
  ])
  assert provider_id in providers.PROVIDERS
  assert "app-987654" not in providers.PROVIDERS


def test_mobius_catalogue_is_not_rejected_over_a_discovered_shared_id(monkeypatch):
  from app.mobius_catalogue import parse_catalogue

  monkeypatch.setattr(providers, "_model_registry_cache", {
    "codex": (time.monotonic(), [{"id": "flow", "available": True}]),
  })
  catalogue = _mobius_catalogue_payload()
  declaration = providers.MobiusProvider._validate_catalogue(catalogue)
  assert declaration == parse_catalogue(catalogue)
  with pytest.raises(ValueError, match="collides"):
    providers.MobiusProvider._validate_catalogue(
      _mobius_catalogue_payload(model_id=providers.KNOWN_MODELS["codex"][0]),
    )


def _mobius_catalogue_payload(model_id="flow"):
  return {"contract_version": 1, "default_model": model_id, "data": [{
    "id": model_id, "name": "Flow", "owned_by": "mobius", "context_window": 1000,
    "effort_levels": ["low"], "input_modalities": ["text"], "default_effort": "low",
  }]}
