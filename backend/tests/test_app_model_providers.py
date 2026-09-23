"""Accepted app model providers join the same pickers and agent registry."""

from datetime import UTC, datetime
from pathlib import Path
import json

import pytest

from app import background_agents, models, providers
from app.app_capabilities import contract_from_manifest
from app.config import get_settings
from app.manifest_contract import ManifestContractError, validate_manifest_contract
from app.routes.secrets import _write_secret


def _manifest():
  return {
    "id": "deepseek-connect", "name": "DeepSeek Connect", "version": "1.0.0",
    "description": "Connect DeepSeek models", "entry": "index.jsx",
    "model_provider": {
      "name": "DeepSeek", "base_url": "https://api.deepseek.com",
      "secret_name": "api_key", "default_model": "deepseek-flash",
      "models": [
        {"id": "deepseek-flash", "label": "DeepSeek Flash",
         "effort_levels": ["low", "medium", "high", "max"],
         "context_window": 128000},
        {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro",
         "effort_levels": ["low", "high", "max"]},
      ],
    },
  }


def test_manifest_freezes_reviewed_model_connection():
  manifest = _manifest()
  validate_manifest_contract(manifest)
  accepted = contract_from_manifest(manifest)
  assert accepted["model_provider"] == manifest["model_provider"]
  manifest["model_provider"]["base_url"] = "https://changed.example"
  assert accepted["model_provider"]["base_url"] == "https://api.deepseek.com"


def test_broker_transport_requires_the_identity_app_grant():
  manifest = _manifest()
  manifest["model_provider"].pop("secret_name")
  manifest["model_provider"].update({
    "transport": "identity_broker", "base_url": "http://127.0.0.1:8765/v1",
  })
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(manifest)
  manifest["id"] = "identity"
  manifest["permissions"] = {"identity_manage": True}
  validate_manifest_contract(manifest)
  manifest["model_provider"]["base_url"] = "http://127.0.0.1:9999/v1"
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(manifest)


@pytest.mark.asyncio
async def test_identity_app_declaration_owns_native_models_and_background_choice(db, tmp_path):
  manifest = _manifest()
  manifest["id"] = "identity"
  manifest["permissions"] = {"identity_manage": True}
  manifest["model_provider"].pop("secret_name")
  manifest["model_provider"].update({
    "transport": "identity_broker", "base_url": "http://127.0.0.1:8765/v1",
  })
  app = models.App(
    name="Möbius · You", slug="identity", description="",
    source_dir=str(tmp_path), capability_contract=contract_from_manifest(manifest),
  )
  db.add(app)
  db.commit()
  data_dir = get_settings().data_dir
  try:
    providers.sync_app_model_providers(data_dir, force=True)
    assert providers.PROVIDERS["mobius"].app_id == app.id
    assert providers.DEFAULT_MODELS["mobius"] == "deepseek-flash"
    assert providers.DEFAULT_BACKGROUND_MODELS["mobius"] == "deepseek-flash"
    assert providers.provider_of_model("deepseek-flash") == "mobius"
    listed = await providers.list_models(data_dir)
    assert [row["id"] for row in listed["mobius"]] == ["deepseek-flash", "deepseek-v4-pro"]
    assert any(row["provider"] == "mobius" for row in providers.background_agent_settings(data_dir)["providers"])

    assert providers.update_agent_settings(data_dir, lambda current: {
      **current, "model_providers_enabled": {"mobius": False},
    })
    assert "mobius" not in await providers.list_models(data_dir)
    assert all(row["provider"] != "mobius" for row in providers.background_agent_settings(data_dir)["providers"])
    assert providers.update_agent_settings(data_dir, lambda current: {
      **current, "model_providers_enabled": {"mobius": True},
      "provider": "mobius", "model": "deepseek-flash",
    })
    assert providers.owner_default_provider(data_dir, "mobius") == "mobius"

    app.deleted_at = datetime.now(UTC)
    db.commit()
    providers.sync_app_model_providers(data_dir, force=True)
    assert providers.PROVIDERS["mobius"].declaration is None
    assert "mobius" not in providers.DEFAULT_MODELS
    assert "mobius" not in providers.DEFAULT_BACKGROUND_MODELS
    assert providers.provider_of_model("deepseek-flash") is None
    assert providers.owner_default_provider(data_dir, "mobius") != "mobius"
    assert all(row["provider"] != "mobius" for row in providers.background_agent_settings(data_dir)["providers"])
  finally:
    providers.PROVIDERS["mobius"].set_declaration(None, None)
    providers.invalidate_model_cache()


@pytest.mark.parametrize("change", [
  {"base_url": "http://api.deepseek.com"},
  {"base_url": "https://someone:password@api.deepseek.com"},
  {"default_model": "not-declared"},
  {"models": [{"id": "spark", "label": "Collision"},
              {"id": "spark", "label": "Duplicate"}]},
])
def test_bad_app_model_declarations_fail_before_acceptance(change):
  manifest = _manifest()
  manifest["model_provider"].update(change)
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(manifest)


@pytest.mark.asyncio
async def test_installed_app_models_join_chat_and_background_then_revoke(db, tmp_path):
  manifest = _manifest()
  app = models.App(
    name="DeepSeek Connect", slug="deepseek-connect", description="",
    source_dir=str(tmp_path), capability_contract=contract_from_manifest(manifest),
  )
  db.add(app)
  db.commit()
  app_id = app.id
  provider_id = f"app-{app_id}"
  data_dir = get_settings().data_dir
  providers.sync_app_model_providers(data_dir, force=True)
  assert providers.provider_of_model("deepseek-flash") == provider_id
  assert "deepseek-flash" not in providers.KNOWN_MODELS.get(provider_id, [])
  assert providers.PROVIDERS[provider_id].check_auth(data_dir) is not None
  listed = await providers.list_models(data_dir)
  assert [row["id"] for row in listed[provider_id]] == [
    "deepseek-flash", "deepseek-v4-pro",
  ]
  background = providers.background_agent_settings(data_dir)
  assert next(row for row in background["providers"] if row["provider"] == provider_id) == {
    "provider": provider_id, "model": "deepseek-flash",
    "effort": providers.DEFAULT_EFFORT, "enabled": False,
  }
  resolved = background_agents.resolve_background_agents(data_dir, {
    "primary": {"provider": provider_id, "model": "deepseek-flash", "effort": "high"},
  })
  assert resolved["primary"] == {
    "provider": provider_id, "model": "deepseek-flash", "effort": "high",
  }
  _write_secret(Path(data_dir) / "app-secrets" / str(app_id) / "api_key", "test-only-placeholder-key")
  adapter = providers.PROVIDERS[provider_id]
  assert providers.provider_runtime_kind(provider_id) == "codex_sdk"
  assert adapter.check_auth(data_dir) is None
  assert providers.update_agent_settings(data_dir, lambda current: {
    **current, "model_providers_enabled": {provider_id: False},
  })
  assert adapter.check_auth(data_dir) == "DeepSeek is turned off in its app."
  assert provider_id not in await providers.list_models(data_dir)
  assert providers.update_agent_settings(data_dir, lambda current: {
    **current, "model_providers_enabled": {provider_id: True},
  })
  env = adapter.build_env({}, data_dir, "chat-test")
  assert env[f"MOBIUS_APP_MODEL_KEY_{app_id}"] == "test-only-placeholder-key"
  assert "test-only-placeholder-key" not in "\n".join(adapter.codex_config_overrides())
  catalog = json.loads((Path(data_dir) / "apps" / str(app_id) / "model-runtime" / "catalog.json").read_text())
  assert [row["slug"] for row in catalog["models"]] == [
    "deepseek-flash", "deepseek-v4-pro",
  ]
  assert catalog["models"][0]["context_window"] == 128000
  assert 'model_catalog_json=' in "\n".join(adapter.codex_config_overrides())

  updated = _manifest()
  updated["model_provider"]["models"] = [
    {"id": "deepseek-next", "label": "DeepSeek Next", "effort_levels": ["low"]},
  ]
  updated["model_provider"]["default_model"] = "deepseek-next"
  app.capability_contract = contract_from_manifest(updated)
  db.commit()
  providers.sync_app_model_providers(data_dir, force=True)
  assert providers.provider_of_model("deepseek-flash") is None
  assert providers.provider_of_model("deepseek-next") == provider_id
  assert [row["id"] for row in (await providers.list_models(data_dir))[provider_id]] == [
    "deepseek-next",
  ]
  assert providers.DEFAULT_BACKGROUND_MODELS[provider_id] == "deepseek-next"

  app.deleted_at = datetime.now(UTC)
  db.commit()
  providers.sync_app_model_providers(data_dir, force=True)
  assert provider_id not in providers.PROVIDERS
  assert providers.provider_of_model("deepseek-next") is None
  with pytest.raises(ValueError, match="not installed"):
    providers.get_provider(provider_id)
