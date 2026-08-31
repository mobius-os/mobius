"""Provider-plan usage normalization and Settings endpoint contracts."""

import asyncio
import base64
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest


def _ready_usage_snapshot(**window_updates):
  window = {
    "id": "seven_day",
    "kind": "weekly",
    "label": "Weekly",
    "used_percent": 24,
    "resets_at": "2099-09-05T03:00:00+00:00",
  }
  window.update(window_updates)
  return {
    "state": "ready",
    "plan_label": "Max plan",
    "windows": [window],
    "credit_balance": None,
  }


@pytest.fixture(autouse=True)
def _reset_provider_usage_state():
  from app import provider_usage

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  yield
  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()


def test_normalize_claude_usage_keeps_current_and_model_windows():
  from app.provider_usage import normalize_claude_usage

  snapshot = normalize_claude_usage(
    {
      "five_hour": {
        "utilization": 34.2,
        "resets_at": "2026-07-30T17:00:00Z",
      },
      "seven_day": {
        "utilization": 61,
        "resets_at": "2026-08-03T00:00:00+00:00",
      },
      "seven_day_opus": {
        "utilization": 12,
        "resets_at": "2026-08-03T00:00:00Z",
      },
      "extra_usage": {"is_enabled": False},
    },
    subscription_type="max",
  )

  assert snapshot["state"] == "ready"
  assert snapshot["plan_label"] == "Max plan"
  assert [window["label"] for window in snapshot["windows"]] == [
    "5-hour", "Weekly", "Opus weekly",
  ]
  assert [window["kind"] for window in snapshot["windows"]] == [
    "other", "weekly", "other",
  ]
  assert snapshot["windows"][0]["used_percent"] == 34.2
  assert snapshot["windows"][0]["resets_at"] == "2026-07-30T17:00:00+00:00"


def test_normalize_codex_usage_reads_primary_secondary_and_credits():
  from app.provider_usage import normalize_codex_usage

  snapshot = normalize_codex_usage(
    {
      "rate_limits": {
        "primary": {
          "used_percent": 21,
          "window_duration_mins": 300,
          "resets_at": 1785430800,
        },
        "secondary": {
          "used_percent": 54,
          "window_duration_mins": 10080,
          "resets_at": 1785715200,
        },
        "credits": {
          "has_credits": True,
          "unlimited": False,
          "balance": "18.50",
        },
      },
    },
    plan_type="plus",
  )

  assert snapshot["state"] == "ready"
  assert snapshot["plan_label"] == "Plus plan"
  assert [window["label"] for window in snapshot["windows"]] == [
    "5-hour", "Weekly",
  ]
  assert [window["kind"] for window in snapshot["windows"]] == [
    "other", "weekly",
  ]
  assert snapshot["windows"][1]["used_percent"] == 54
  assert snapshot["credit_balance"] == "18.50 credits"


def test_normalize_mobius_usage_reads_consumed_credit_and_active_expiry():
  from app.provider_usage import normalize_mobius_usage

  snapshot = normalize_mobius_usage({
    "plan": {"label": "Trial"},
    "balance": {
      "spendable_units": 650_000,
      "grants": [{
        "amount_units": 2_000_000,
        "available_units": 650_000,
        "revoked": False,
        "expires_at": "2026-09-07T19:40:34.682998Z",
      }],
    },
  })

  assert snapshot == {
    "state": "ready",
    "plan_label": "Trial",
    "windows": [{
      "id": "api_credits",
      "kind": "api_credits",
      "label": "API credits",
      "used_percent": 67.5,
      "resets_at": None,
      "expires_at": "2026-09-07T19:40:34.682998+00:00",
    }],
    "credit_balance": None,
  }


def test_normalize_mobius_usage_keeps_an_exhausted_grant_measurable():
  from app.provider_usage import normalize_mobius_usage

  snapshot = normalize_mobius_usage({
    "balance": {
      "spendable_units": 0,
      "grants": [{
        "amount_units": 2_000_000,
        "available_units": 0,
        "revoked": False,
      }],
    },
  })

  assert snapshot["windows"][0]["used_percent"] == 100
  assert snapshot["credit_balance"] is None


def test_normalizers_report_unavailable_without_inventing_limits():
  from app.provider_usage import (
    normalize_claude_usage,
    normalize_codex_usage,
    normalize_mobius_usage,
  )

  claude = normalize_claude_usage({}, subscription_type="pro")
  codex = normalize_codex_usage({"rate_limits": {}}, plan_type="team")

  assert claude == {
    "state": "unavailable",
    "plan_label": "Pro plan",
    "windows": [],
    "credit_balance": None,
  }
  assert codex == {
    "state": "unavailable",
    "plan_label": "Team plan",
    "windows": [],
    "credit_balance": None,
  }
  assert normalize_mobius_usage({"balance": {"spendable_units": 500}}) == {
    "state": "unavailable",
    "plan_label": "Möbius subscription",
    "windows": [],
    "credit_balance": None,
  }


def test_provider_usage_reads_only_requested_plan(monkeypatch):
  from app import provider_usage

  seen = []

  async def fake_snapshot(provider_id, _data_dir):
    await asyncio.sleep(0)
    seen.append(provider_id)
    return {"state": "ready", "windows": [{"id": "primary"}]}

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  body = asyncio.run(provider_usage.read_provider_usage("codex", "/data"))

  assert body["state"] == "ready"
  assert body["stale"] is False
  assert seen == ["codex"]


@pytest.mark.asyncio
async def test_provider_usage_coalesces_concurrent_live_reads(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  started = asyncio.Event()
  release = asyncio.Event()
  calls = 0

  async def fake_snapshot(_provider_id, _data_dir):
    nonlocal calls
    calls += 1
    started.set()
    await release.wait()
    return _ready_usage_snapshot()

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  first = asyncio.create_task(
    provider_usage.read_provider_usage("claude", str(tmp_path))
  )
  await started.wait()
  second = asyncio.create_task(
    provider_usage.read_provider_usage("claude", str(tmp_path))
  )
  await asyncio.sleep(0)
  release.set()
  first_result, second_result = await asyncio.gather(first, second)

  assert calls == 1
  assert first_result == second_result
  assert first_result["stale"] is False


@pytest.mark.asyncio
async def test_provider_usage_coalesces_concurrent_failed_cold_reads(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  started = asyncio.Event()
  release = asyncio.Event()
  calls = 0

  async def fake_snapshot(_provider_id, _data_dir):
    nonlocal calls
    calls += 1
    started.set()
    await release.wait()
    return provider_usage._unavailable("Max plan")

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  first = asyncio.create_task(
    provider_usage.read_provider_usage("claude", str(tmp_path))
  )
  await started.wait()
  second = asyncio.create_task(
    provider_usage.read_provider_usage("claude", str(tmp_path))
  )
  await asyncio.sleep(0)
  release.set()
  first_result, second_result = await asyncio.gather(first, second)

  assert calls == 1
  assert first_result == second_result
  assert first_result["state"] == "unavailable"


@pytest.mark.asyncio
async def test_claude_usage_retries_a_successful_cold_read(
  monkeypatch, tmp_path,
):
  from app import provider_usage, providers

  monkeypatch.setattr(provider_usage, "_CLAUDE_COLD_RETRY_DELAYS", (0,))
  monkeypatch.setattr(
    providers, "claude_subscription_type", lambda _data_dir: "max",
  )

  async def fake_access_token(_data_dir):
    return "token"

  monkeypatch.setattr(providers, "claude_access_token", fake_access_token)
  payloads = [{}, {
    "seven_day": {
      "utilization": 24,
      "resets_at": "2099-09-05T03:00:00+00:00",
    },
  }]

  class FakeResponse:
    def raise_for_status(self):
      pass

    def json(self):
      return payloads.pop(0)

  class FakeClient:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      pass

    async def get(self, *_args, **_kwargs):
      return FakeResponse()

  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", FakeClient)
  result = await provider_usage._fetch_claude_usage(str(tmp_path))

  assert result["state"] == "ready"
  assert result["windows"][0]["used_percent"] == 24
  assert payloads == []


@pytest.mark.asyncio
async def test_claude_usage_does_not_retry_transport_failures(
  monkeypatch, tmp_path,
):
  from app import provider_usage, providers

  monkeypatch.setattr(provider_usage, "_CLAUDE_COLD_RETRY_DELAYS", (0, 0))
  monkeypatch.setattr(
    providers, "claude_subscription_type", lambda _data_dir: "max",
  )

  async def fake_access_token(_data_dir):
    return "token"

  monkeypatch.setattr(providers, "claude_access_token", fake_access_token)
  calls = 0

  class FailingClient:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      pass

    async def get(self, *_args, **_kwargs):
      nonlocal calls
      calls += 1
      raise RuntimeError("offline")

  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", FailingClient)

  with pytest.raises(RuntimeError, match="offline"):
    await provider_usage._fetch_claude_usage(str(tmp_path))
  assert calls == 1


@pytest.mark.asyncio
async def test_provider_usage_keeps_recent_success_through_transient_failure(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  clock = [100.0]
  monkeypatch.setattr(provider_usage.time, "monotonic", lambda: clock[0])
  live = [_ready_usage_snapshot()]

  async def fake_snapshot(_provider_id, _data_dir):
    return live.pop(0) if live else provider_usage._unavailable("Max plan")

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  first = await provider_usage.read_provider_usage("claude", str(tmp_path))
  clock[0] += provider_usage._PROVIDER_USAGE_FRESH_SECONDS + 1
  fallback = await provider_usage.read_provider_usage("claude", str(tmp_path))

  assert first["stale"] is False
  assert fallback["state"] == "ready"
  assert fallback["stale"] is True
  assert fallback["windows"][0]["used_percent"] == 24


@pytest.mark.asyncio
async def test_provider_usage_stops_reusing_a_success_after_the_stale_limit(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  clock = [100.0]
  monkeypatch.setattr(provider_usage.time, "monotonic", lambda: clock[0])
  live = [_ready_usage_snapshot()]

  async def fake_snapshot(_provider_id, _data_dir):
    return live.pop(0) if live else provider_usage._unavailable("Max plan")

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  await provider_usage.read_provider_usage("claude", str(tmp_path))
  clock[0] += provider_usage._PROVIDER_USAGE_STALE_SECONDS + 1
  result = await provider_usage.read_provider_usage("claude", str(tmp_path))

  assert result["state"] == "unavailable"
  assert result["stale"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["resets_at", "expires_at"])
async def test_provider_usage_never_reuses_a_snapshot_past_its_boundary(
  monkeypatch, tmp_path, boundary,
):
  from app import provider_usage

  clock = [100.0]
  monkeypatch.setattr(provider_usage.time, "monotonic", lambda: clock[0])
  live = [_ready_usage_snapshot(
    used_percent=99,
    **{boundary: "2020-01-01T00:00:00+00:00"},
  )]

  async def fake_snapshot(_provider_id, _data_dir):
    return live.pop(0) if live else provider_usage._unavailable("Max plan")

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  await provider_usage.read_provider_usage("claude", str(tmp_path))
  clock[0] += provider_usage._PROVIDER_USAGE_FRESH_SECONDS + 1
  result = await provider_usage.read_provider_usage("claude", str(tmp_path))

  assert result["state"] == "unavailable"


@pytest.mark.asyncio
async def test_provider_usage_disconnect_discards_a_prior_observation(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  clock = [100.0]
  monkeypatch.setattr(provider_usage.time, "monotonic", lambda: clock[0])
  live = [_ready_usage_snapshot(), {
    "state": "disconnected",
    "plan_label": None,
    "windows": [],
    "credit_balance": None,
  }]

  async def fake_snapshot(_provider_id, _data_dir):
    return live.pop(0)

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  await provider_usage.read_provider_usage("claude", str(tmp_path))
  clock[0] += provider_usage._PROVIDER_USAGE_FRESH_SECONDS + 1
  result = await provider_usage.read_provider_usage("claude", str(tmp_path))

  assert result["state"] == "disconnected"
  assert provider_usage._provider_usage_cache == {}


def test_settings_provider_usage_endpoint(client, auth, monkeypatch):
  from app import provider_usage

  expected = {"state": "ready", "windows": []}
  seen = []

  async def fake_read(provider_id, _data_dir):
    seen.append(provider_id)
    return expected

  monkeypatch.setattr(provider_usage, "read_provider_usage", fake_read)
  response = client.get("/api/settings/provider-usage/codex", headers=auth)

  assert response.status_code == 200
  assert response.json() == expected
  assert seen == ["codex"]


def test_settings_provider_usage_rejects_unknown_provider(client, auth):
  response = client.get("/api/settings/provider-usage/other", headers=auth)

  assert response.status_code == 404


def test_codex_subscription_type_reads_display_claim(tmp_path):
  from app import providers

  payload = {
    "https://api.openai.com/auth": {
      "chatgpt_plan_type": "plus",
    },
  }
  encoded = base64.urlsafe_b64encode(
    json.dumps(payload).encode(),
  ).decode().rstrip("=")
  auth_path = tmp_path / "cli-auth" / "codex" / "auth.json"
  auth_path.parent.mkdir(parents=True)
  auth_path.write_text(json.dumps({
    "tokens": {"id_token": f"header.{encoded}.signature"},
  }))

  assert providers.codex_subscription_type(str(tmp_path)) == "plus"


def test_configured_plan_labels_never_fetch_usage(monkeypatch):
  from app import provider_usage, providers

  monkeypatch.setattr(
    providers,
    "claude_subscription_type",
    lambda _data_dir: "max",
  )
  monkeypatch.setattr(
    providers,
    "codex_subscription_type",
    lambda _data_dir: "plus",
  )

  assert provider_usage.configured_plan_labels("/data") == {
    "claude": "Max plan",
    "codex": "Plus plan",
  }


@pytest.mark.asyncio
async def test_codex_usage_ignores_saturated_default_executor(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  calls = []

  class FakeLimits:
    def model_dump(self, **_kwargs):
      return {"rate_limits": {}}

  class FakeClient:
    def __init__(self, _config):
      pass

    def _record(self, name):
      calls.append((name, threading.current_thread().name))

    def start(self):
      self._record("start")

    def initialize(self):
      self._record("initialize")

    def account_read(self):
      self._record("account_read")
      return SimpleNamespace(
        account=SimpleNamespace(root=SimpleNamespace(plan_type="plus")),
      )

    def request(self, *_args, **_kwargs):
      self._record("request")
      return FakeLimits()

    def close(self):
      self._record("close")

  codex_package = ModuleType("openai_codex")
  codex_package.__path__ = []
  codex_client = ModuleType("openai_codex.client")
  codex_client.CodexClient = FakeClient
  codex_client.CodexConfig = lambda **kwargs: kwargs
  generated_package = ModuleType("openai_codex.generated")
  generated_package.__path__ = []
  generated_v2 = ModuleType("openai_codex.generated.v2_all")
  generated_v2.GetAccountRateLimitsResponse = object
  monkeypatch.setitem(sys.modules, "openai_codex", codex_package)
  monkeypatch.setitem(sys.modules, "openai_codex.client", codex_client)
  monkeypatch.setitem(sys.modules, "openai_codex.generated", generated_package)
  monkeypatch.setitem(sys.modules, "openai_codex.generated.v2_all", generated_v2)
  monkeypatch.setattr(provider_usage.shutil, "which", lambda _name: "/codex")

  loop = asyncio.get_running_loop()
  release = threading.Event()
  occupied = threading.Event()
  saturated = ThreadPoolExecutor(max_workers=1)
  replacement = ThreadPoolExecutor(max_workers=1)
  loop.set_default_executor(saturated)

  def occupy_default_worker():
    occupied.set()
    release.wait()

  blocker = loop.run_in_executor(None, occupy_default_worker)
  while not occupied.is_set():
    await asyncio.sleep(0)

  try:
    result = await asyncio.wait_for(
      provider_usage._fetch_codex_usage(str(tmp_path)), timeout=1,
    )
    assert result["plan_label"] == "Plus plan"
    assert {name for name, _thread in calls} >= {
      "start", "initialize", "account_read", "request", "close",
    }
    assert all(
      thread.startswith("mobius-codex-usage") for _name, thread in calls
    )
  finally:
    release.set()
    await blocker
    loop.set_default_executor(replacement)
    saturated.shutdown(wait=True)
