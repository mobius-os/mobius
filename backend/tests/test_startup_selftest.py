"""The candidate's smoke uses real local configuration, never provider work."""

import json
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import models, providers, startup_selftest


@pytest.mark.parametrize("configured", [False, True])
def test_smoke_resolves_real_provider_configuration_offline(db, tmp_path, monkeypatch, configured):
  data_dir = str(tmp_path)
  monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(data_dir=data_dir))
  monkeypatch.setattr(providers, "_app_provider_sync_at", 0)

  def forbidden(*args, **kwargs):
    raise AssertionError("startup smoke must not authenticate, spawn or use network")

  monkeypatch.setattr(socket.socket, "connect", forbidden)
  monkeypatch.setattr(subprocess, "Popen", forbidden)
  for cls in (providers.ClaudeProvider, providers.CodexProvider,
              providers.MobiusProvider, providers.AppModelProvider):
    for method in ("check_auth", "ensure_auth", "fetch_models", "build_env"):
      monkeypatch.setattr(cls, method, forbidden)

  # Gemini is built in, like Claude, Codex and Möbius.
  expected = {"claude", "codex", "gemini", "mobius"}
  if configured:
    app = models.App(
      name="Smoke provider", slug="smoke-provider", description="",
      source_dir=str(tmp_path / "source"), capability_contract={"model_provider": {
        "name": "Smoke", "base_url": "https://invalid.example/v1",
        "secret_name": "unused", "default_model": "smoke-model",
        "models": [{"id": "smoke-model", "label": "Smoke"}],
      }},
    )
    db.add(app)
    db.commit()
    provider_id = f"app-{app.id}"
    expected.add(provider_id)
    settings_path = tmp_path / "shared" / "agent-settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({
      "provider": provider_id, "model": "smoke-model",
      "background_agents": {"providers": [{
        "provider": provider_id, "model": "smoke-model", "enabled": True,
      }]},
    }))
    before = settings_path.read_bytes()

  resolve = Mock(wraps=providers.get_provider)
  effective = Mock(wraps=providers.effective_agent_settings)
  background = Mock(wraps=providers.background_agent_settings)
  monkeypatch.setattr(providers, "get_provider", resolve)
  monkeypatch.setattr(providers, "effective_agent_settings", effective)
  monkeypatch.setattr(providers, "background_agent_settings", background)
  try:
    startup_selftest.main()
    assert {call.args[0] for call in resolve.call_args_list} == expected
    assert all(call.kwargs == {"data_dir": data_dir} for call in resolve.call_args_list)
    assert {call.kwargs["provider"] for call in effective.call_args_list} == expected
    background.assert_called_once_with(data_dir)
    if configured:
      assert effective(data_dir, provider=provider_id)["model"] == "smoke-model"
      assert background(data_dir)["primary"]["provider"] == provider_id
      assert settings_path.read_bytes() == before
    assert not (tmp_path / "cli-auth").exists()
  finally:
    if configured:
      db.delete(app)
      db.commit()
    providers.sync_app_model_providers(data_dir, force=True)
