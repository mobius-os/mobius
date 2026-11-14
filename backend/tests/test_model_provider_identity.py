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


def test_new_chat_choice_excludes_a_concurrent_picker_write(tmp_path, monkeypatch):
  path = tmp_path / "shared" / "agent-settings.json"
  path.parent.mkdir()
  path.write_text(json.dumps({"provider": "codex", "model": "new-undiscovered-id"}))
  original = providers.owner_default_provider
  lock_results = []

  def concurrent_writer():
    acquired = providers._AGENT_SETTINGS_LOCK.acquire(blocking=False)
    lock_results.append(acquired)
    if acquired:
      providers._AGENT_SETTINGS_LOCK.release()

  def resolve_and_attempt_write(*args):
    provider = original(*args)
    # The exact window between provider resolution and model snapshot.
    with ThreadPoolExecutor(max_workers=1) as pool:
      pool.submit(concurrent_writer).result()
    return provider

  monkeypatch.setattr(providers, "owner_default_provider", resolve_and_attempt_write)
  provider, settings = providers.snapshot_owner_chat_choice(str(tmp_path), "claude")
  assert lock_results == [False]
  assert (provider, settings["model"]) == ("codex", "new-undiscovered-id")


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
  })])
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
