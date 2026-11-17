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


def _example_app_contract(model_ids):
  from app.app_capabilities import contract_from_manifest
  return contract_from_manifest({
    "id": "example-models", "name": "Example Models", "version": "1.0.0",
    "description": "Example", "entry": "index.jsx",
    "model_provider": {
      "name": "Example", "base_url": "https://models.example.invalid",
      "secret_name": "api_key", "default_model": model_ids[0],
      "models": [{"id": mid, "label": mid, "effort_levels": ["low"]} for mid in model_ids],
    },
  })


def _example_declaration(model_ids):
  return _example_app_contract(model_ids)["model_provider"]


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


def test_a_pick_after_the_single_settings_read_does_not_tear_the_pair(
  tmp_path, monkeypatch,
):
  data_dir = str(tmp_path)
  path = tmp_path / "shared" / "agent-settings.json"
  path.parent.mkdir()
  path.write_text(json.dumps({"provider": "codex", "model": "old-undiscovered-id"}))
  original = providers.owner_default_provider

  def resolve_then_pick(*args, **kwargs):
    provider = original(*args, **kwargs)
    # A real pick from another thread lands between provider resolution and
    # the model snapshot. Both steps use the one read taken before it.
    with ThreadPoolExecutor(max_workers=1) as pool:
      assert pool.submit(providers.write_agent_settings, data_dir, {
        "provider": "claude", "model": "new-undiscovered-id",
      }).result()
    return provider

  monkeypatch.setattr(providers, "owner_default_provider", resolve_then_pick)
  provider, settings = providers.snapshot_owner_chat_choice(data_dir, "claude")
  assert (provider, settings["model"]) == ("codex", "old-undiscovered-id")


def test_same_provider_pick_saves_without_a_sign_in_check(
  client, auth, chat, db, monkeypatch,
):
  # The picker sends the provider with every pick. Only a real switch checks
  # sign-in, so a same-provider pick never waits on (or fails with) one.
  def no_check(self, data_dir):
    raise AssertionError("a same-provider pick must not check sign-in")

  monkeypatch.setattr(providers.ClaudeProvider, "check_auth", no_check)
  model = providers.KNOWN_MODELS["claude"][0]
  response = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "provider": "claude", "agent_settings_json": {"model": model},
  })
  assert response.status_code == 200, response.text
  db.refresh(chat)
  assert chat.provider == "claude"
  assert chat.agent_settings_json["model"] == model


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


def test_app_may_declare_an_id_discovery_also_lists(discovered_models, monkeypatch):
  for name in ("PROVIDERS", "DEFAULT_MODELS", "DEFAULT_BACKGROUND_MODELS",
               "_model_registry_locks"):
    monkeypatch.setattr(providers, name, dict(getattr(providers, name)))
  monkeypatch.setattr(providers, "_app_provider_ids", set())
  monkeypatch.setattr(providers, "_app_provider_sync_at", 0.0)
  model = discovered_models["claude"]
  providers._apply_app_model_declarations([
    (987654, _example_declaration([model]), ""),
  ])
  assert "app-987654" in providers.PROVIDERS
  # Each provider keeps its own pair; the saved provider resolves the tie.
  assert not providers._model_belongs_to_other_provider(model, "app-987654")
  assert not providers._model_belongs_to_other_provider(model, "claude")
  assert providers.provider_of_model(model, preferred="app-987654") == "app-987654"
  assert providers.provider_of_model(model, preferred="claude") == "claude"
  # Built-in ids are still never shareable.
  providers._apply_app_model_declarations([
    (987654, _example_declaration([providers.KNOWN_MODELS["codex"][0]]), ""),
  ])
  assert "app-987654" not in providers.PROVIDERS


def test_saved_chat_model_keeps_the_saved_provider_on_the_send_path(
  db, chat, discovered_models, monkeypatch,
):
  from app.routes import chats_stream

  for name in ("PROVIDERS", "DEFAULT_MODELS", "DEFAULT_BACKGROUND_MODELS",
               "_model_registry_locks"):
    monkeypatch.setattr(providers, name, dict(getattr(providers, name)))
  monkeypatch.setattr(providers, "_app_provider_ids", set())
  monkeypatch.setattr(providers, "_read_app_model_declarations", lambda: [
    (987654, _example_declaration([discovered_models["codex"]]), ""),
  ])
  providers.sync_app_model_providers("", force=True)
  # A shared id resolves to the chat's own provider, not the first lister.
  chat.provider = "app-987654"
  chat.agent_settings_json = {"model": discovered_models["codex"]}
  db.commit()
  assert chats_stream._next_execution_provider(db, chat) == "app-987654"
  chat.provider = "codex"
  db.commit()
  assert chats_stream._next_execution_provider(db, chat) == "codex"
  # After a restart the discovered id is unknown until the models are fetched
  # again; the saved provider still carries the pair.
  providers._model_registry_cache.clear()
  chat.provider = "claude"
  chat.agent_settings_json = {"model": discovered_models["claude"]}
  db.commit()
  assert chats_stream._next_execution_provider(db, chat) == "claude"


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


def test_app_update_adding_a_discovered_id_keeps_the_provider_and_its_chats(
  client, auth, chat, db, tmp_path, monkeypatch,
):
  from app import models
  from app.config import get_settings

  for name in ("PROVIDERS", "DEFAULT_MODELS", "DEFAULT_BACKGROUND_MODELS",
               "_model_registry_locks"):
    monkeypatch.setattr(providers, name, dict(getattr(providers, name)))
  monkeypatch.setattr(providers, "_app_provider_ids", set())
  monkeypatch.setattr(providers, "_model_registry_cache", {})
  app = models.App(
    name="Example Models", slug="example-models", description="",
    source_dir=str(tmp_path), capability_contract=_example_app_contract(["app-only"]),
  )
  db.add(app)
  db.commit()
  provider_id = f"app-{app.id}"
  data_dir = get_settings().data_dir
  providers.sync_app_model_providers(data_dir, force=True)
  assert provider_id in providers.PROVIDERS

  # Codex discovers an id, then the app updates to declare it as well.
  shared = "gpt-new"
  providers._model_registry_cache["codex"] = (
    time.monotonic(), [{"id": shared, "available": True}],
  )
  app.capability_contract = _example_app_contract(["app-only", shared])
  db.commit()
  providers.sync_app_model_providers(data_dir, force=True)
  assert provider_id in providers.PROVIDERS
  assert not providers._model_belongs_to_other_provider(shared, provider_id)
  assert not providers._model_belongs_to_other_provider(shared, "codex")
  assert providers.effective_agent_settings(
    data_dir, {"model": shared}, provider=provider_id,
  )["model"] == shared

  chat.provider = provider_id
  chat.agent_settings_json = {"model": "app-only"}
  db.commit()
  response = client.patch(f"/api/chats/{chat.id}", headers=auth, json={
    "agent_settings_json": {"model": shared},
  })
  assert response.status_code == 200, response.text
  assert response.json()["provider"] == provider_id
  detail = client.get(f"/api/chats/{chat.id}", headers=auth).json()
  assert detail["provider"] == provider_id
  db.refresh(chat)
  assert chat.provider == provider_id
  assert chat.agent_settings_json["model"] == shared


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
