from __future__ import annotations

import json

from app import providers
from app.schemas import AgentSettingsOverride, ChatProviderSwitch


def _provider():
  provider = providers.MobiusProvider()
  provider.set_declaration(6, {
    "name": "Möbius", "transport": "identity_broker",
    "base_url": "http://127.0.0.1:8765/v1", "default_model": "inkling",
    "models": [
      {"id": "spark", "label": "Spark", "effort_levels": ["minimal", "low", "medium", "high", "max"], "context_window": 235930},
      {"id": "inkling", "label": "Evolve", "effort_levels": ["minimal", "low", "medium", "high", "max"], "context_window": 900000},
    ],
  })
  return provider


def test_trial_provider_requires_linked_broker(monkeypatch, tmp_path):
  provider = _provider()
  monkeypatch.setattr(provider, "_identity", lambda: {"linked": False})
  assert "Möbius · You" in provider.check_auth(str(tmp_path))
  monkeypatch.setattr(provider, "_identity", lambda: {"linked": True})
  assert provider.check_auth(str(tmp_path)) is None


def test_trial_provider_config_uses_only_local_broker_marker(tmp_path):
  provider = _provider()
  env = provider.build_env(
    {
      "OPENAI_API_KEY": "must-not-leak",
      "PRIVATE_PROVIDER_API_KEY": "must-not-leak",
    },
    str(tmp_path),
    "chat-1",
  )
  config = (tmp_path / "cli-auth" / "mobius" / "config.toml").read_text()

  assert env["OPENAI_API_KEY"] == ""
  assert env["PRIVATE_PROVIDER_API_KEY"] == ""
  assert env["MOBIUS_LOCAL_BROKER_KEY"] == "local-broker"
  assert env["CODEX_HOME"] == str(tmp_path / "cli-auth" / "mobius")
  assert 'model="inkling"' in config
  assert "http://127.0.0.1:8765/v1" in config
  assert "MOBIUS_LOCAL_BROKER_KEY" in config
  assert "PRIVATE_PROVIDER" not in config
  assert "secret" not in config.lower()


def test_subscription_route_reconnects_a_stalled_stream():
  """The subscription route must reconnect through an upstream stall.

  The gateway enforces a 60s no-token ceiling, so one silence can otherwise
  lose a healthy long turn. Turning retries off here is what makes that
  failure terminal.
  """
  overrides = providers.MobiusProvider().codex_config_overrides()

  assert "model_providers.mobius_trial.stream_max_retries=2" in overrides
  assert "model_providers.mobius_trial.request_max_retries=2" in overrides


def test_subscription_search_uses_codex_native_provider_endpoint():
  overrides = providers.MobiusProvider().codex_config_overrides()
  assert "model_providers.mobius_trial.supports_standalone_web_search=true" in overrides
  assert "features.standalone_web_search=true" in overrides
  assert "suppress_unstable_features_warning=true" in overrides
  assert 'web_search="live"' in overrides
  assert 'web_search="disabled"' not in overrides


def test_subscription_catalog_comes_from_app_declaration(tmp_path, monkeypatch):
  provider = _provider()
  monkeypatch.setitem(providers.PROVIDERS, "mobius", provider)
  env = provider.build_env({}, str(tmp_path))
  payload = json.loads((tmp_path / "cli-auth" / "mobius" / "catalog.json").read_text())
  assert [row["slug"] for row in payload["models"]] == ["spark", "inkling"]
  assert [row["display_name"] for row in payload["models"]] == ["Spark", "Evolve"]
  assert all(row["supports_parallel_tool_calls"] is False for row in payload["models"])
  assert all(row["support_verbosity"] is False for row in payload["models"])
  assert providers.known_model_ids("mobius") == ["spark", "inkling"]
  assert providers.provider_runtime_kind("mobius") == "codex_sdk"
  assert [row["label"] for row in providers._fallback_models("mobius")] == ["Spark", "Evolve"]
  assert env["CODEX_HOME"] == str(tmp_path / "cli-auth" / "mobius")


def test_saved_evolve_selection_keeps_inkling_id_for_atomic_provider_handoff():
  switch = ChatProviderSwitch(
    provider="mobius",
    agent_settings_json=AgentSettingsOverride(model="inkling", effort="high"),
    switch_id="switch-to-evolve",
  )
  assert switch.provider == "mobius"


def test_subscription_never_becomes_an_implicit_connected_default(monkeypatch):
  monkeypatch.setattr(
    providers.MobiusProvider, "check_auth", lambda self, _data_dir: None,
  )
  monkeypatch.setattr(
    providers.CodexProvider, "check_auth", lambda self, _data_dir: "missing",
  )
  monkeypatch.setattr(
    providers.ClaudeProvider, "check_auth", lambda self, _data_dir: "missing",
  )

  assert providers.authenticated_provider_ids("/data") == []
  assert providers.resolve_default_provider("/data", "claude") == "claude"
