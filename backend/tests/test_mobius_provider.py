from __future__ import annotations

import json
import threading
import tomllib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from app import providers
from app.schemas import AgentSettingsOverride, ChatProviderSwitch


def _provider():
  return providers.MobiusProvider()


def _catalogue():
  return json.loads(
    (Path(providers.__file__).with_name("mobius_models.json")).read_text()
  )


def test_account_change_while_copying_broker_result_cannot_rehold_old_identity(
  monkeypatch,
):
  from contextlib import contextmanager
  from app import runtime_identity

  provider = _provider()
  copying = threading.Event()
  resume = threading.Event()
  original_copy = providers.copy.deepcopy

  class Client:
    def get(self, _route):
      return self

    def raise_for_status(self):
      pass

    def json(self):
      return {"linked": False}

  @contextmanager
  def broker_client(**_kwargs):
    yield Client()

  def paused_copy(value, *args, **kwargs):
    if isinstance(value, dict) and value == {"linked": False}:
      copying.set()
      assert resume.wait(3)
    return original_copy(value, *args, **kwargs)

  monkeypatch.setattr(runtime_identity, "broker_client", broker_client)
  monkeypatch.setattr(providers.copy, "deepcopy", paused_copy)
  with ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(provider._identity)
    assert copying.wait(3)
    provider.forget_account_reads()
    resume.set()
    assert future.result(timeout=3) == {"linked": False}
  assert "/identity" not in provider._held_reads


def test_trial_provider_requires_linked_broker(monkeypatch, tmp_path):
  provider = _provider()
  monkeypatch.setattr(provider, "_identity", lambda: {"linked": False})
  assert provider.check_auth(str(tmp_path)) == (
    "Sign in to your Möbius account in Settings to use Möbius models."
  )
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
  assert f'model="{_catalogue()["default_model"]}"' in config
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


def test_subscription_codex_home_config_keeps_native_sub_agents_off(tmp_path):
  _provider().build_env({}, str(tmp_path))
  config = tomllib.loads(
    (tmp_path / "cli-auth" / "mobius" / "config.toml").read_text()
  )
  assert config["agents"]["enabled"] is False
  assert config["features"]["multi_agent"] is False
  assert config["features"]["multi_agent_v2"]["enabled"] is False


def test_subscription_search_uses_codex_native_provider_endpoint():
  overrides = providers.MobiusProvider().codex_config_overrides()
  assert "model_providers.mobius_trial.supports_standalone_web_search=true" in overrides
  assert "features.standalone_web_search=true" in overrides
  assert "suppress_unstable_features_warning=true" in overrides
  assert 'web_search="live"' in overrides
  assert 'web_search="disabled"' not in overrides


def test_subscription_catalog_comes_from_platform_catalogue(tmp_path, monkeypatch):
  provider = _provider()
  monkeypatch.setitem(providers.PROVIDERS, "mobius", provider)
  catalogue = _catalogue()
  ids = [model["id"] for model in catalogue["models"]]
  labels = [model["label"] for model in catalogue["models"]]
  env = provider.build_env({}, str(tmp_path))
  payload = json.loads((tmp_path / "cli-auth" / "mobius" / "catalog.json").read_text())
  assert [row["slug"] for row in payload["models"]] == ids
  assert [row["display_name"] for row in payload["models"]] == labels
  assert all(row["supports_parallel_tool_calls"] is False for row in payload["models"])
  assert all(row["support_verbosity"] is False for row in payload["models"])
  assert providers.known_model_ids("mobius") == ids
  assert providers.provider_runtime_kind("mobius") == "codex_sdk"
  assert [row["label"] for row in providers._fallback_models("mobius")] == labels
  assert env["CODEX_HOME"] == str(tmp_path / "cli-auth" / "mobius")


def test_platform_catalogue_satisfies_the_published_model_provider_contract():
  """The catalogue keeps the manifest block's shape so one writer serves both."""
  from app.manifest_contract import validate_manifest_contract

  validate_manifest_contract({
    "id": "identity", "name": "Möbius · You", "description": "Catalogue shape",
    "version": "1.0.0", "entry": "index.jsx",
    "permissions": {"identity_manage": True},
    "model_provider": _catalogue(),
  })
  assert providers.DEFAULT_MODELS["mobius"] == _catalogue()["default_model"]
  assert providers.DEFAULT_BACKGROUND_MODELS["mobius"] == _catalogue()["default_model"]


def test_turned_off_subscription_points_to_settings(monkeypatch, tmp_path):
  provider = _provider()
  monkeypatch.setattr(providers, "provider_enabled", lambda _d, _p: False)
  assert provider.check_auth(str(tmp_path)) == "Möbius models are turned off in Settings."


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


class _FakeBroker:
  """Counts broker GETs; each response comes from `answers[route]`."""

  def __init__(self, answers):
    self.answers = answers
    self.calls: list[str] = []
    self.during_call = None

  def client(self, *, timeout):
    broker = self

    class _Response:
      def __init__(self, route):
        self.route = route

      def raise_for_status(self):
        value = broker.answers[self.route]
        if isinstance(value, Exception):
          raise value

      def json(self):
        return broker.answers[self.route]

    class _Client:
      def __enter__(self):
        return self

      def __exit__(self, *_exc):
        return False

      def get(self, route):
        broker.calls.append(route)
        if broker.during_call:
          broker.during_call()
        return _Response(route)

    return _Client()


def _broker(monkeypatch, answers):
  from app import runtime_identity
  broker = _FakeBroker(answers)
  monkeypatch.setattr(runtime_identity, "broker_client", broker.client)
  return broker


def test_trial_balance_is_held_between_status_reads_and_dropped_when_account_changes(monkeypatch):
  provider = _provider()
  monkeypatch.setitem(providers.PROVIDERS, "mobius", provider)
  broker = _broker(monkeypatch, {"/v1/balance": {"spendable_units": 0}})

  assert provider.trial_status() == {"spendable_units": 0}
  assert provider.trial_status() == {"spendable_units": 0}
  assert broker.calls == ["/v1/balance"]

  # Activating credit (or linking) must be visible on the very next read.
  broker.answers["/v1/balance"] = {"spendable_units": 500}
  providers.mobius_account_changed()
  assert provider.trial_status() == {"spendable_units": 500}
  assert broker.calls == ["/v1/balance", "/v1/balance"]


def test_balance_hold_expires(monkeypatch):
  provider = _provider()
  broker = _broker(monkeypatch, {"/v1/balance": {"spendable_units": 1}})
  provider.trial_status()
  held_at, value = provider._held_reads["/v1/balance"]
  provider._held_reads["/v1/balance"] = (
    held_at - provider.BALANCE_HOLD_SECONDS - 1, value,
  )
  provider.trial_status()
  assert broker.calls == ["/v1/balance", "/v1/balance"]


def test_identity_link_is_held_briefly_but_a_sign_in_is_seen_at_once(monkeypatch, tmp_path):
  provider = _provider()
  monkeypatch.setitem(providers.PROVIDERS, "mobius", provider)
  broker = _broker(monkeypatch, {"/identity": {"linked": False}})
  data_dir = str(tmp_path)

  assert provider.check_auth(data_dir) is not None
  assert provider.check_auth(data_dir) is not None
  assert broker.calls == ["/identity"]

  broker.answers["/identity"] = {"linked": True}
  providers.mobius_account_changed()
  assert provider.check_auth(data_dir) is None


def test_failed_identity_read_is_never_held(monkeypatch, tmp_path):
  """A transient broker error must not pin the account as unlinked."""
  provider = _provider()
  broker = _broker(monkeypatch, {"/identity": RuntimeError("broker down")})
  assert provider.check_auth(str(tmp_path)) is not None
  broker.answers["/identity"] = {"linked": True}
  assert provider.check_auth(str(tmp_path)) is None
  assert broker.calls == ["/identity", "/identity"]


def test_balance_read_in_flight_when_account_changes_is_not_held(monkeypatch):
  provider = _provider()
  broker = _broker(monkeypatch, {"/v1/balance": {"spendable_units": 0}})
  broker.during_call = provider.forget_account_reads
  assert provider.trial_status() == {"spendable_units": 0}
  assert "/v1/balance" not in provider._held_reads


def test_held_balance_cannot_be_mutated_by_a_caller(monkeypatch):
  provider = _provider()
  _broker(monkeypatch, {"/v1/balance": {"spendable_units": 3}})
  provider.trial_status()["spendable_units"] = 0
  assert provider.trial_status() == {"spendable_units": 3}
