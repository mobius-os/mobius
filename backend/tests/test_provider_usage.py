"""Provider-plan usage normalization and Settings endpoint contracts."""

import asyncio
import base64
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace

import httpx
import pytest


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


def test_a_window_past_its_limit_shows_full_instead_of_vanishing():
  """An over-limit reading is the one the owner most needs to see."""
  from app.provider_usage import normalize_claude_usage

  snapshot = normalize_claude_usage({
    "seven_day": {"utilization": 104.5, "resets_at": "2026-10-03T03:00:00Z"},
    "five_hour": {"utilization": -3, "resets_at": "2026-10-03T03:00:00Z"},
  })

  weekly = [w for w in snapshot["windows"] if w["kind"] == "weekly"]
  assert [w["used_percent"] for w in weekly] == [100]
  assert [w["id"] for w in snapshot["windows"]] == ["seven_day"]


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
    "5-hour", "7-day",
  ]
  assert [window["kind"] for window in snapshot["windows"]] == [
    "other", "weekly",
  ]
  assert snapshot["windows"][1]["used_percent"] == 54
  assert snapshot["credit_balance"] == "18.50 credits"
  assert snapshot["reset_credits"] is None


def test_normalize_codex_usage_surfaces_redeemable_reset_credits():
  from app.provider_usage import normalize_codex_usage

  snapshot = normalize_codex_usage(
    {
      "rate_limits": {"primary": {"used_percent": 90, "resets_at": 1785430800}},
      "rate_limit_reset_credits": {
        "available_count": 2,
        "credits": [
          {
            "id": "credit-a",
            "title": "Weekly reset",
            "status": "available",
            "granted_at": 1785000000,
            "expires_at": 1787592000,
          },
          # Already-redeemed rows must not become clickable offers.
          {"id": "credit-b", "status": "redeemed", "expires_at": 1787592000},
        ],
      },
    },
    plan_type="plus",
  )

  resets = snapshot["reset_credits"]
  assert resets["available_count"] == 2
  assert [row["id"] for row in resets["credits"]] == ["credit-a"]
  assert resets["credits"][0]["expires_at"] == "2026-08-24T17:20:00+00:00"


def test_normalize_codex_usage_reset_credits_count_only_without_detail_rows():
  from app.provider_usage import normalize_codex_usage

  snapshot = normalize_codex_usage(
    {
      "rate_limits": {"primary": {"used_percent": 10, "resets_at": 1785430800}},
      # availableCount known, detail rows not fetched (null) — still surfaced.
      "rate_limit_reset_credits": {"available_count": 1, "credits": None},
    },
    plan_type="plus",
  )

  assert snapshot["reset_credits"] == {"available_count": 1, "credits": []}


def test_normalize_codex_usage_preserves_an_explicit_zero_reset_count():
  from app.provider_usage import normalize_codex_usage

  snapshot = normalize_codex_usage({
    "rate_limits": {"primary": {"used_percent": 10, "resets_at": 1785430800}},
    "rate_limit_reset_credits": {"available_count": 0, "credits": []},
  }, plan_type="plus")

  assert snapshot["reset_credits"] == {"available_count": 0, "credits": []}


def test_normalize_mobius_usage_reads_api_credit_consumption():
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

  assert snapshot["state"] == "ready"
  assert snapshot["plan_label"] == "Trial"
  assert snapshot["windows"] == [{
    "id": "api_credits",
    "kind": "api_credits",
    "label": "API credits",
    "used_percent": 67.5,
    "resets_at": None,
    "remaining_percent": 32.5,
    "expires_at": "2026-09-07T19:40:34.682998+00:00",
  }]
  assert snapshot["credit_balance"] is None


def test_normalize_mobius_usage_keeps_an_exhausted_grant_measurable():
  from app.provider_usage import normalize_mobius_usage

  snapshot = normalize_mobius_usage({
    "plan": {"label": "Trial"},
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
  assert snapshot["windows"][0]["remaining_percent"] == 0
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
    "extra_usage": {
      "enabled": False,
      "available": False,
      "used_percent": None,
      "manageable": False,
    },
    "reset_credits": None,
  }
  assert codex == {
    "state": "unavailable",
    "plan_label": "Team plan",
    "windows": [],
    "credit_balance": None,
    "reset_credits": None,
  }
  assert normalize_mobius_usage({"balance": {"spendable_units": 500}}) == {
    "state": "unavailable",
    "plan_label": "Möbius",
    "windows": [],
    "credit_balance": None,
  }


def test_provider_usage_reads_only_requested_plan(monkeypatch, tmp_path):
  from app import provider_usage

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  seen = []

  async def fake_snapshot(provider_id, _data_dir):
    await asyncio.sleep(0)
    seen.append(provider_id)
    return {"state": "ready", "windows": [{"id": "primary"}]}

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  body = asyncio.run(provider_usage.read_provider_usage("codex", str(tmp_path)))

  assert body["state"] == "ready"
  assert body["stale"] is False
  assert seen == ["codex"]


@pytest.mark.asyncio
async def test_provider_usage_coalesces_concurrent_live_reads(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  started = asyncio.Event()
  release = asyncio.Event()
  calls = 0

  async def fake_snapshot(_provider_id, _data_dir):
    nonlocal calls
    calls += 1
    started.set()
    await release.wait()
    return {
      "state": "ready",
      "plan_label": "Max plan",
      "windows": [{
        "id": "seven_day",
        "kind": "weekly",
        "label": "Weekly",
        "used_percent": 24,
        "resets_at": "2099-09-05T03:00:00+00:00",
      }],
      "credit_balance": None,
    }

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

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  monkeypatch.setattr(provider_usage, "_CLAUDE_COLD_RETRY_DELAYS", ())
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


_READY_CLAUDE_PAYLOAD = {
  "seven_day": {"utilization": 24, "resets_at": "2099-09-05T03:00:00+00:00"},
}


def _claude_usage_http(monkeypatch, provider_usage, responses):
  """Serve each ``(status, json)`` in order to one Claude usage read attempt.

  An attempt is the ordinary read plus the reset-offer read; both get the
  attempt's response unless it is a ``{"usage": ..., "reset": ...}`` pair.
  """
  seen = []

  def handler(request):
    kind = "reset" if "cedar_ember" in str(request.url) else "usage"
    attempt = responses[sum(1 for prior in seen if prior == kind)]
    seen.append(kind)
    status, body = attempt[kind] if isinstance(attempt, dict) else attempt
    return httpx.Response(status, json=body, headers={"retry-after": "0"})

  real_client = httpx.AsyncClient

  def client(**kwargs):
    return real_client(transport=httpx.MockTransport(handler), **kwargs)

  async def token(_data_dir):
    return "secret-token"

  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers, "claude_subscription_type", lambda _d: "max",
  )
  return seen


@pytest.mark.asyncio
async def test_cold_claude_read_with_no_windows_is_retried(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  monkeypatch.setattr(provider_usage, "_CLAUDE_COLD_RETRY_DELAYS", (0,))
  seen = _claude_usage_http(monkeypatch, provider_usage, [
    (200, {}),
    (200, _READY_CLAUDE_PAYLOAD),
  ])

  result = await provider_usage._fetch_claude_usage(str(tmp_path))

  assert len(seen) == 4
  assert result["state"] == "ready"


@pytest.mark.asyncio
async def test_claude_usage_keeps_paid_extra_usage_beside_the_reset_offer(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  _claude_usage_http(monkeypatch, provider_usage, [{
    "usage": (200, {
      **_READY_CLAUDE_PAYLOAD,
      "extra_usage": {"is_enabled": True, "utilization": 25},
    }),
    "reset": (200, {
      **_READY_CLAUDE_PAYLOAD,
      "cedar_ember": {"eligible": False, "grants": []},
    }),
  }])

  snapshot = await provider_usage._fetch_claude_usage(str(tmp_path))

  assert snapshot["extra_usage"] == {
    "enabled": True,
    "available": True,
    "used_percent": 25.0,
    "manageable": True,
  }
  assert snapshot["reset_credits"]["eligible"] is False


@pytest.mark.asyncio
async def test_rate_limited_claude_read_is_refused_without_immediate_retry(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  monkeypatch.setattr(provider_usage, "_CLAUDE_COLD_RETRY_DELAYS", (0, 0))
  seen = _claude_usage_http(monkeypatch, provider_usage, [
    (429, {"error": {"type": "rate_limit_error"}}),
  ])

  with pytest.raises(provider_usage.ProviderUsageRefused) as refused:
    await provider_usage._fetch_claude_usage(str(tmp_path))

  assert len(seen) == 2
  # Claude sends "Retry-After: 0", which is not a usable backoff.
  assert refused.value.retry_after is None


def _scripted_claude_reads(monkeypatch, provider_usage, tmp_path, refusals):
  """A ready read, then ``refusals`` in order, on a controllable clock."""
  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  clock = [100.0]
  monkeypatch.setattr(provider_usage.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(
    provider_usage.providers, "claude_subscription_type", lambda _d: "max",
  )
  outcomes = [
    {
      "state": "ready",
      "plan_label": "Max plan",
      "windows": [{
        "id": "seven_day", "kind": "weekly", "label": "Weekly",
        "used_percent": 24, "resets_at": "2099-09-05T03:00:00+00:00",
      }],
      "credit_balance": None,
    },
    *refusals,
  ]
  probes = []

  async def fake_snapshot(_provider_id, _data_dir):
    probes.append(clock[0])
    outcome = outcomes.pop(0)
    if isinstance(outcome, Exception):
      raise outcome
    return outcome

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  read = lambda: provider_usage.read_provider_usage("claude", str(tmp_path))
  return clock, probes, read


@pytest.mark.asyncio
async def test_usage_refusal_holds_last_reading_and_backs_off_before_probing(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  refused = provider_usage.ProviderUsageRefused("claude", None)
  clock, probes, read = _scripted_claude_reads(
    monkeypatch, provider_usage, tmp_path, [refused, refused],
  )
  backoff = provider_usage._PROVIDER_USAGE_REFUSED_BACKOFF_SECONDS

  await read()
  clock[0] += provider_usage._PROVIDER_USAGE_FRESH_SECONDS
  first_refusal = await read()
  assert first_refusal["state"] == "ready" and first_refusal["stale"] is True
  assert len(probes) == 2

  # Many panes asking during the backoff get the held reading, no probe.
  clock[0] += backoff - 1
  held = await read()
  assert held["stale"] is True and held["windows"][0]["used_percent"] == 24
  assert len(probes) == 2

  # The next refusal doubles the wait.
  clock[0] += 1
  await read()
  assert len(probes) == 3
  clock[0] += 2 * backoff - 1
  await read()
  assert len(probes) == 3


@pytest.mark.asyncio
async def test_usage_refusal_backoff_never_serves_an_expired_reading(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  clock, probes, read = _scripted_claude_reads(
    monkeypatch, provider_usage, tmp_path,
    [provider_usage.ProviderUsageRefused("claude", 3600.0)],
  )

  await read()
  clock[0] += provider_usage._PROVIDER_USAGE_FRESH_SECONDS
  assert (await read())["stale"] is True
  clock[0] += provider_usage._PROVIDER_USAGE_STALE_SECONDS
  expired = await read()

  assert expired["state"] == "unavailable"
  assert expired["plan_label"] == "Max plan"
  assert len(probes) == 2


@pytest.mark.asyncio
async def test_provider_usage_keeps_recent_success_through_transient_failure(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  monkeypatch.setattr(provider_usage, "_CLAUDE_COLD_RETRY_DELAYS", ())
  clock = [100.0]
  monkeypatch.setattr(provider_usage.time, "monotonic", lambda: clock[0])
  live = [{
    "state": "ready",
    "plan_label": "Max plan",
    "windows": [{
      "id": "seven_day",
      "kind": "weekly",
      "label": "Weekly",
      "used_percent": 24,
      "resets_at": "2099-09-05T03:00:00+00:00",
    }],
    "credit_balance": None,
  }]

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
async def test_forced_provider_usage_read_never_falls_back_to_stale_success(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  monkeypatch.setattr(provider_usage, "_CLAUDE_COLD_RETRY_DELAYS", ())
  live = [{
    "state": "ready",
    "plan_label": "Max plan",
    "windows": [],
    "credit_balance": None,
  }]

  async def fake_snapshot(_provider_id, _data_dir):
    return live.pop(0) if live else provider_usage._unavailable("Max plan")

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  first = await provider_usage.read_provider_usage("claude", str(tmp_path))
  forced = await provider_usage.read_provider_usage(
    "claude", str(tmp_path), force_refresh=True,
  )

  assert first["state"] == "ready"
  assert forced["state"] == "unavailable"
  assert forced["stale"] is False


@pytest.mark.asyncio
async def test_provider_usage_never_reuses_a_snapshot_past_its_reset(
  monkeypatch, tmp_path,
):
  from app import provider_usage

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  monkeypatch.setattr(provider_usage, "_CLAUDE_COLD_RETRY_DELAYS", ())
  clock = [100.0]
  monkeypatch.setattr(provider_usage.time, "monotonic", lambda: clock[0])
  live = [{
    "state": "ready",
    "plan_label": "Max plan",
    "windows": [{
      "id": "seven_day",
      "kind": "weekly",
      "label": "Weekly",
      "used_percent": 99,
      "resets_at": "2020-01-01T00:00:00+00:00",
    }],
    "credit_balance": None,
  }]

  async def fake_snapshot(_provider_id, _data_dir):
    return live.pop(0) if live else provider_usage._unavailable("Max plan")

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  await provider_usage.read_provider_usage("claude", str(tmp_path))
  clock[0] += provider_usage._PROVIDER_USAGE_FRESH_SECONDS + 1
  result = await provider_usage.read_provider_usage("claude", str(tmp_path))

  assert result["state"] == "unavailable"


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


def test_normalize_claude_usage_surfaces_enabled_extra_usage():
  from app.provider_usage import normalize_claude_usage

  snapshot = normalize_claude_usage({
    "five_hour": {"utilization": 100, "resets_at": "2026-09-18T10:30:00Z"},
    "extra_usage": {
      "is_enabled": True,
      "monthly_limit": 50000,
      "used_credits": 12500,
      "utilization": 25,
      "currency": "USD",
    },
  })

  assert snapshot["extra_usage"] == {
    "enabled": True,
    "available": True,
    "used_percent": 25,
    "manageable": True,
  }


def test_normalize_claude_usage_does_not_invent_extra_usage_availability():
  from app.provider_usage import normalize_claude_usage

  unknown = normalize_claude_usage({
    "five_hour": {"utilization": 100},
    "extra_usage": {"is_enabled": True},
  })
  exhausted = normalize_claude_usage({
    "five_hour": {"utilization": 100},
    "extra_usage": {"is_enabled": True, "utilization": 100},
  })

  assert unknown["extra_usage"] == {
    "enabled": True,
    "available": None,
    "used_percent": None,
    "manageable": True,
  }
  assert exhausted["extra_usage"] == {
    "enabled": True,
    "available": False,
    "used_percent": 100.0,
    "manageable": True,
  }


def test_normalize_claude_usage_surfaces_only_provider_selected_reset():
  from app.provider_usage import normalize_claude_usage

  snapshot = normalize_claude_usage({
    "five_hour": {"utilization": 100},
    "cedar_ember": {
      "eligible": True,
      "at_limit": True,
      "next_grant_id": "grant-next",
      "grants": [
        {
          "id": "grant-next",
          "label": "Weekly reset",
          "resets_left": 2,
          "ends_at": "2099-10-01T12:00:00Z",
          "clears": ["five_hour", "seven_day"],
          "usable_now": True,
          "use_requires_limit": True,
          "paused": False,
        },
        {
          "id": "grant-later",
          "resets_left": 1,
          "usable_now": False,
        },
      ],
    },
  })

  resets = snapshot["reset_credits"]
  assert resets["available_count"] == 3
  assert resets["next_credit_id"] == "grant-next"
  assert resets["redeemable"] is True
  assert resets["credits"][0]["expires_at"] == "2099-10-01T12:00:00+00:00"


def test_normalize_claude_usage_rejects_expired_selected_reset():
  from app.provider_usage import normalize_claude_usage

  resets = normalize_claude_usage({
    "five_hour": {"utilization": 100},
    "cedar_ember": {
      "eligible": True,
      "at_limit": True,
      "next_grant_id": "grant-expired",
      "grants": [{
        "id": "grant-expired",
        "resets_left": 1,
        "ends_at": "2000-01-01T00:00:00Z",
        "usable_now": True,
        "paused": False,
      }],
    },
  })["reset_credits"]

  assert resets["next_credit_id"] == "grant-expired"
  assert resets["redeemable"] is False


def test_normalize_claude_usage_keeps_ineligible_offer_non_redeemable():
  from app.provider_usage import normalize_claude_usage

  resets = normalize_claude_usage({
    "five_hour": {"utilization": 10},
    "cedar_ember": {
      "eligible": False,
      "ineligible_reason": "surface",
      "next_grant_id": None,
      "grants": [],
    },
  })["reset_credits"]

  assert resets == {
    "available_count": 0,
    "credits": [],
    "credits_complete": True,
    "eligible": False,
    "ineligible_reason": "surface",
    "at_limit": False,
    "next_credit_id": None,
    "redeemable": False,
    "weekly_resets_at": None,
    "cooldown_until": None,
  }


@pytest.mark.parametrize(
  "grants",
  [None, ["invalid-row"], [{"id": "grant-next", "resets_left": "2"}]],
)
def test_normalize_claude_usage_marks_malformed_grant_catalogue_incomplete(
  grants,
):
  from app.provider_usage import normalize_claude_usage

  resets = normalize_claude_usage({
    "five_hour": {"utilization": 100},
    "cedar_ember": {
      "eligible": True,
      "next_grant_id": "grant-next",
      "grants": grants,
    },
  })["reset_credits"]

  assert resets["credits"] == []
  assert resets["credits_complete"] is False
  assert resets["redeemable"] is False


@pytest.mark.asyncio
async def test_set_claude_extra_usage_uses_bounded_existing_plan_toggle(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  calls = []

  class Response:
    def raise_for_status(self):
      return None

  class Client:
    def __init__(self, **kwargs):
      calls.append(("client", kwargs))

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def put(self, url, *, headers, json):
      calls.append(("put", url, headers, json))
      return Response()

  async def token(_data_dir):
    return "secret-token"

  async def refreshed(provider_id, data_dir):
    calls.append(("refresh", provider_id, data_dir))
    return {"extra_usage": {"enabled": True}}

  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", Client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers,
    "claude_organization_uuid",
    lambda _data_dir: "00000000-0000-4000-8000-000000000001",
  )
  monkeypatch.setattr(provider_usage, "read_provider_usage", refreshed)

  result = await provider_usage.set_claude_extra_usage(
    str(tmp_path), enabled=True,
  )

  put = next(call for call in calls if call[0] == "put")
  assert put[1].endswith(
    "/api/oauth/organizations/00000000-0000-4000-8000-000000000001/"
    "overage_spend_limit"
  )
  assert put[2]["Authorization"] == "Bearer secret-token"
  assert put[3] == {"is_enabled": True}
  assert not any("setup_overage_billing" in str(call) for call in calls)
  assert result == {"extra_usage": {"enabled": True}}


@pytest.mark.asyncio
async def test_redeem_claude_reset_uses_private_guarded_claim_once(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  calls = []

  class Response:
    def raise_for_status(self):
      return None

    def json(self):
      return {
        "result": "reset",
        "resets_left": 1,
        "cleared": ["five_hour"],
        "weekly_resets_at": "2026-09-26T03:00:00Z",
      }

  class Client:
    def __init__(self, **kwargs):
      calls.append(("client", kwargs))

    async def __aenter__(self):
      return self

    async def __aexit__(self, *args):
      return None

    async def post(self, url, *, headers, json):
      calls.append(("post", url, headers, json))
      return Response()

  async def token(_data_dir):
    return "secret-token"

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "state": "ready",
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 2}],
      },
    }

  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", Client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers,
    "claude_organization_uuid",
    lambda _data_dir: "00000000-0000-4000-8000-000000000001",
  )
  monkeypatch.setattr(provider_usage, "read_provider_usage", current)

  result = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  posts = [call for call in calls if call[0] == "post"]
  assert len(posts) == 1
  post = posts[0]
  assert post[1].endswith(
    "/api/organizations/00000000-0000-4000-8000-000000000001/"
    "reset_rate_limits"
  )
  assert post[2]["Authorization"] == "Bearer secret-token"
  assert post[3]["program"] == "cedar_ember"
  assert post[3]["grant_id"] == "grant-next"
  assert isinstance(post[3]["request_id"], str)
  assert result == {
    "outcome": "reset",
    "reason": None,
    "resets_left": 1,
    "cleared": ["five_hour"],
    "weekly_resets_at": "2026-09-26T03:00:00+00:00",
  }
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), "00000000-0000-4000-8000-000000000001",
  )
  assert provider_usage._load_claude_reset_intent(intent_path)["last_result"] == result


@pytest.mark.asyncio
async def test_claude_reset_serializes_concurrent_double_submit(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  resets_left = 2
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "state": "ready",
      "reset_credits": {
        "redeemable": resets_left > 0,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": resets_left}],
      },
    }

  class Response:
    def raise_for_status(self):
      return None

    def json(self):
      return {"result": "reset", "resets_left": 1}

  class Client:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def post(self, _url, *, headers, json):
      nonlocal resets_left
      posts.append(json)
      await asyncio.sleep(0)
      resets_left -= 1
      return Response()

  async def token(_data_dir):
    return "secret-token"

  monkeypatch.setattr(provider_usage, "read_provider_usage", current)
  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", Client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers,
    "claude_organization_uuid",
    lambda _data_dir: "00000000-0000-4000-8000-000000000001",
  )

  outcomes = await asyncio.gather(
    provider_usage.redeem_claude_reset(
      str(tmp_path), credit_id="grant-next", expected_resets_left=2,
    ),
    provider_usage.redeem_claude_reset(
      str(tmp_path), credit_id="grant-next", expected_resets_left=2,
    ),
    return_exceptions=True,
  )

  assert len(posts) == 1
  assert [item["outcome"] for item in outcomes] == ["reset", "reset"]


@pytest.mark.asyncio
async def test_claude_reset_success_keeps_receipt_until_usage_catches_up(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  resets_left = 2
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": resets_left}],
      },
    }

  class Response:
    def raise_for_status(self):
      return None

    def json(self):
      return {"result": "reset", "resets_left": 1}

  class Client:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def post(self, _url, *, headers, json):
      posts.append(json)
      return Response()

  async def token(_data_dir):
    return "secret-token"

  organization_uuid = "00000000-0000-4000-8000-000000000001"
  monkeypatch.setattr(provider_usage, "read_provider_usage", current)
  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", Client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers, "claude_organization_uuid",
    lambda _data_dir: organization_uuid,
  )

  first = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  provider_usage._claude_reset_locks.clear()  # the receipt survives a server restart
  repeated = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), organization_uuid,
  )
  assert first == repeated
  assert first["outcome"] == "reset"
  assert len(posts) == 1
  assert provider_usage._load_claude_reset_intent(intent_path)["last_result"] == first

  resets_left = 1
  reconciled = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  assert reconciled["outcome"] == "reset"
  assert reconciled["reconciled"] is True
  assert not intent_path.exists()

  with pytest.raises(provider_usage.ClaudeResetOfferChanged):
    await provider_usage.redeem_claude_reset(
      str(tmp_path), credit_id="grant-next", expected_resets_left=2,
    )
  assert len(posts) == 1

  await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=1,
  )
  assert len(posts) == 2
  assert posts[0]["request_id"] != posts[1]["request_id"]


@pytest.mark.asyncio
async def test_claude_reset_reconciles_provider_commit_after_response_loss(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  resets_left = 2
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "state": "ready",
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": resets_left}],
      },
    }

  class Client:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def post(self, _url, *, headers, json):
      nonlocal resets_left
      posts.append(json)
      resets_left = 1
      raise httpx.ReadTimeout("response lost after provider commit")

  async def token(_data_dir):
    return "secret-token"

  monkeypatch.setattr(provider_usage, "read_provider_usage", current)
  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", Client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers,
    "claude_organization_uuid",
    lambda _data_dir: "00000000-0000-4000-8000-000000000001",
  )

  uncertain = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  reconciled = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert uncertain["outcome"] == "unknown"
  assert uncertain["pending"] is True
  assert reconciled == {
    "outcome": "reset",
    "reason": "reconciled_after_interruption",
    "resets_left": 1,
    "cleared": [],
    "weekly_resets_at": None,
    "reconciled": True,
  }
  assert len(posts) == 1


@pytest.mark.asyncio
async def test_claude_reset_reuses_persisted_request_id_after_transport_loss(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "state": "ready",
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 2}],
      },
    }

  class Response:
    def raise_for_status(self):
      return None

    def json(self):
      return {"result": "reset", "resets_left": 1}

  class Client:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def post(self, _url, *, headers, json):
      posts.append(json)
      if len(posts) == 1:
        raise httpx.ReadTimeout("response lost before provider commit")
      return Response()

  async def token(_data_dir):
    return "secret-token"

  monkeypatch.setattr(provider_usage, "read_provider_usage", current)
  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", Client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers,
    "claude_organization_uuid",
    lambda _data_dir: "00000000-0000-4000-8000-000000000001",
  )

  first = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), "00000000-0000-4000-8000-000000000001",
  )
  persisted = provider_usage._load_claude_reset_intent(intent_path)
  assert persisted is not None
  assert persisted.get("invalid") is not True
  provider_usage._claude_reset_locks.clear()  # simulate a restarted process
  second = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert first["outcome"] == "unknown"
  assert second["outcome"] == "reset"
  assert len(posts) == 2
  assert posts[0]["request_id"] == persisted["request_id"]
  assert posts[0]["request_id"] == posts[1]["request_id"]


def _install_claude_reset_mocks(
  monkeypatch, provider_usage, *, current, post,
) -> str:
  class Client:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def post(self, url, *, headers, json):
      return await post(url, headers=headers, json=json)

  async def token(_data_dir):
    return "secret-token"

  organization_uuid = "00000000-0000-4000-8000-000000000001"
  monkeypatch.setattr(provider_usage, "read_provider_usage", current)
  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", Client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers, "claude_organization_uuid",
    lambda _data_dir: organization_uuid,
  )
  return organization_uuid


@pytest.mark.asyncio
async def test_claude_reset_replay_409_keeps_intent_while_usage_is_stale(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 2}],
      },
    }

  class Client:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def post(self, url, *, headers, json):
      posts.append(json)
      if len(posts) == 1:
        raise httpx.ReadTimeout("response lost after provider commit")
      return httpx.Response(409, request=httpx.Request("POST", url))

  async def token(_data_dir):
    return "secret-token"

  organization_uuid = "00000000-0000-4000-8000-000000000001"
  monkeypatch.setattr(provider_usage, "read_provider_usage", current)
  monkeypatch.setattr(provider_usage.httpx, "AsyncClient", Client)
  monkeypatch.setattr(provider_usage.providers, "claude_access_token", token)
  monkeypatch.setattr(
    provider_usage.providers, "claude_organization_uuid",
    lambda _data_dir: organization_uuid,
  )

  first = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  provider_usage._claude_reset_locks.clear()  # restart before the replay
  second = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  third = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert [first["outcome"], second["outcome"], third["outcome"]] == [
    "unknown", "unknown", "unknown",
  ]
  assert len(posts) == 3
  assert len({post["request_id"] for post in posts}) == 1
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), organization_uuid,
  )
  assert provider_usage._load_claude_reset_intent(intent_path)["request_id"] == (
    posts[0]["request_id"]
  )


@pytest.mark.asyncio
async def test_claude_reset_pending_intent_yields_to_authoritative_successor(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "state": "ready",
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-new",
        "credits": [
          {"id": "grant-pending", "resets_left": 2},
          {"id": "grant-new", "resets_left": 1},
        ],
      },
    }

  async def post(_url, *, headers, json):
    posts.append(json)
    return SimpleNamespace(
      raise_for_status=lambda: None,
      json=lambda: {"result": "reset", "resets_left": 0},
    )

  organization_uuid = _install_claude_reset_mocks(
    monkeypatch, provider_usage, current=current, post=post,
  )
  provider_usage._write_claude_reset_intent(
    provider_usage._claude_reset_intent_path(str(tmp_path), organization_uuid),
    request_id="stable-pending-request",
    credit_id="grant-pending",
    resets_left_before=2,
  )
  result = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-new", expected_resets_left=1,
  )

  assert result["outcome"] == "reset"
  assert len(posts) == 1
  assert posts[0]["grant_id"] == "grant-new"
  assert posts[0]["request_id"] != "stable-pending-request"
  assert provider_usage._load_claude_reset_intent(
    provider_usage._claude_reset_intent_path(str(tmp_path), organization_uuid),
  )["request_id"] == posts[0]["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  "outcome", ["already_used", "cooldown", "not_limited", "ineligible"],
)
async def test_claude_reset_fresh_non_consuming_result_retires_intent(
  tmp_path, monkeypatch, outcome,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 2}],
      },
    }

  async def post(_url, *, headers, json):
    posts.append(json)
    return SimpleNamespace(
      raise_for_status=lambda: None,
      json=lambda: {"result": outcome},
    )

  _install_claude_reset_mocks(
    monkeypatch, provider_usage, current=current, post=post,
  )

  first = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  second = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert first["outcome"] == outcome
  assert second["outcome"] == outcome
  assert len(posts) == 2
  assert posts[0]["request_id"] != posts[1]["request_id"]


@pytest.mark.asyncio
async def test_claude_reset_ambiguous_results_keep_one_request_id(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 2}],
      },
    }

  async def post(_url, *, headers, json):
    posts.append(json)
    if len(posts) == 2:
      raise httpx.ReadTimeout("response lost")
    if len(posts) == 3:
      response = httpx.Response(
        401, request=httpx.Request("POST", "https://api.anthropic.com"),
      )
      response.raise_for_status()
    outcome = "unavailable" if len(posts) == 1 else "reset"
    return SimpleNamespace(
      raise_for_status=lambda: None,
      json=lambda: {"result": outcome, "resets_left": 1},
    )

  _install_claude_reset_mocks(
    monkeypatch, provider_usage, current=current, post=post,
  )

  results = [
    await provider_usage.redeem_claude_reset(
      str(tmp_path), credit_id="grant-next", expected_resets_left=2,
    )
    for _ in range(4)
  ]

  assert [item["outcome"] for item in results] == [
    "unknown", "unknown", "unknown", "reset",
  ]
  assert len({post["request_id"] for post in posts}) == 1


@pytest.mark.asyncio
async def test_claude_reset_fresh_http_error_keeps_request_id(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 2}],
      },
    }

  async def post(_url, *, headers, json):
    posts.append(json)
    if len(posts) == 1:
      response = httpx.Response(
        401, request=httpx.Request("POST", "https://api.anthropic.com"),
      )
      response.raise_for_status()
    return SimpleNamespace(
      raise_for_status=lambda: None,
      json=lambda: {"result": "reset", "resets_left": 1},
    )

  _install_claude_reset_mocks(
    monkeypatch, provider_usage, current=current, post=post,
  )

  first = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  second = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert first["outcome"] == "unknown"
  assert second["outcome"] == "reset"
  assert len({post["request_id"] for post in posts}) == 1


@pytest.mark.asyncio
async def test_claude_reset_expired_offer_cannot_be_reclaimed(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{
          "id": "grant-next",
          "resets_left": 2,
          "expires_at": "2000-01-01T00:00:00Z",
        }],
      },
    }

  async def post(_url, *, headers, json):
    posts.append(json)
    raise AssertionError("expired grants must not reach Claude")

  organization_uuid = _install_claude_reset_mocks(
    monkeypatch, provider_usage, current=current, post=post,
  )
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), organization_uuid,
  )
  provider_usage._write_claude_reset_intent(
    intent_path,
    request_id="expired-grant-request",
    credit_id="grant-next",
    resets_left_before=2,
    last_result={
      "outcome": "reset",
      "reason": None,
      "resets_left": 1,
      "cleared": [],
      "weekly_resets_at": None,
    },
  )

  with pytest.raises(provider_usage.ClaudeResetOfferChanged):
    await provider_usage.redeem_claude_reset(
      str(tmp_path), credit_id="grant-next", expected_resets_left=2,
    )

  assert posts == []
  assert not intent_path.exists()


@pytest.mark.asyncio
async def test_claude_reset_receipt_never_reclaims_unchanged_offer(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 2}],
      },
    }

  async def post(_url, *, headers, json):
    posts.append(json)
    return SimpleNamespace(
      raise_for_status=lambda: None,
      json=lambda: {"result": "reset", "resets_left": 1},
    )

  organization_uuid = _install_claude_reset_mocks(
    monkeypatch, provider_usage, current=current, post=post,
  )
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), organization_uuid,
  )
  provider_usage._write_claude_reset_intent(
    intent_path,
    request_id="confirmed-request",
    credit_id="grant-next",
    resets_left_before=2,
    last_result={
      "outcome": "reset",
      "reason": None,
      "resets_left": 1,
      "cleared": [],
      "weekly_resets_at": None,
    },
  )
  result = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert result["outcome"] == "reset"
  assert posts == []
  assert provider_usage._load_claude_reset_intent(intent_path)[
    "request_id"
  ] == "confirmed-request"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["not_limited", "ineligible"])
async def test_claude_reset_replayed_definitive_noop_retires_intent(
  tmp_path, monkeypatch, outcome,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 2}],
      },
    }

  async def post(_url, *, headers, json):
    posts.append(json)
    return SimpleNamespace(
      raise_for_status=lambda: None,
      json=lambda: {"result": outcome},
    )

  organization_uuid = _install_claude_reset_mocks(
    monkeypatch, provider_usage, current=current, post=post,
  )
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), organization_uuid,
  )
  provider_usage._write_claude_reset_intent(
    intent_path,
    request_id="stable-request",
    credit_id="grant-next",
    resets_left_before=2,
  )

  result = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert result["outcome"] == outcome
  assert posts[0]["request_id"] == "stable-request"
  assert not intent_path.exists()


@pytest.mark.asyncio
async def test_claude_reset_incomplete_snapshot_keeps_request_id_across_restart(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  reads = 0
  posts = []

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    nonlocal reads
    assert force_refresh is True
    reads += 1
    if reads == 1:
      return provider_usage.normalize_claude_usage({
        "five_hour": {"utilization": 100},
        "cedar_ember": {
          "eligible": True,
          "next_grant_id": "grant-next",
          "grants": [{
            "id": "grant-next",
            "resets_left": "2",
            "usable_now": True,
          }],
        },
      })
    return provider_usage.normalize_claude_usage({
      "five_hour": {"utilization": 100},
      "cedar_ember": {
        "eligible": True,
        "next_grant_id": "grant-next",
        "grants": [{
          "id": "grant-next",
          "resets_left": 2,
          "usable_now": True,
          "paused": False,
        }],
      },
    })

  async def post(_url, *, headers, json):
    posts.append(json)
    return SimpleNamespace(
      raise_for_status=lambda: None,
      json=lambda: {"result": "reset", "resets_left": 1},
    )

  organization_uuid = _install_claude_reset_mocks(
    monkeypatch, provider_usage, current=current, post=post,
  )
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), organization_uuid,
  )
  provider_usage._write_claude_reset_intent(
    intent_path,
    request_id="stable-across-restart",
    credit_id="grant-next",
    resets_left_before=2,
  )

  first = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )
  provider_usage._claude_reset_locks.clear()
  second = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert first["outcome"] == "unknown"
  assert second["outcome"] == "reset"
  assert [post["request_id"] for post in posts] == ["stable-across-restart"]


@pytest.mark.asyncio
async def test_claude_reset_missing_grant_retires_unresolved_intent(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  organization_uuid = "00000000-0000-4000-8000-000000000001"
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), organization_uuid,
  )
  provider_usage._write_claude_reset_intent(
    intent_path,
    request_id="vanished-request",
    credit_id="grant-vanished",
    resets_left_before=1,
  )

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": False,
        "next_credit_id": None,
        "credits": [],
      },
    }

  monkeypatch.setattr(provider_usage, "read_provider_usage", current)
  monkeypatch.setattr(
    provider_usage.providers, "claude_organization_uuid",
    lambda _data_dir: organization_uuid,
  )

  with pytest.raises(provider_usage.ClaudeResetOfferChanged):
    await provider_usage.redeem_claude_reset(
      str(tmp_path), credit_id="grant-vanished", expected_resets_left=1,
    )

  assert not intent_path.exists()


@pytest.mark.asyncio
async def test_claude_reset_receipt_outcome_precedes_account_count_change(
  tmp_path, monkeypatch,
):
  from app import provider_usage

  provider_usage._claude_reset_locks.clear()
  organization_uuid = "00000000-0000-4000-8000-000000000001"
  intent_path = provider_usage._claude_reset_intent_path(
    str(tmp_path), organization_uuid,
  )
  receipt = {
    "outcome": "already_used",
    "reason": None,
    "resets_left": 2,
    "cleared": [],
    "weekly_resets_at": None,
  }
  provider_usage._write_claude_reset_intent(
    intent_path,
    request_id="stable-request",
    credit_id="grant-next",
    resets_left_before=2,
    last_result=receipt,
  )

  async def current(_provider_id, _data_dir, *, force_refresh=False):
    assert force_refresh is True
    return {
      "reset_credits": {
        "redeemable": True,
        "next_credit_id": "grant-next",
        "credits": [{"id": "grant-next", "resets_left": 1}],
      },
    }

  monkeypatch.setattr(provider_usage, "read_provider_usage", current)
  monkeypatch.setattr(
    provider_usage.providers, "claude_organization_uuid",
    lambda _data_dir: organization_uuid,
  )

  result = await provider_usage.redeem_claude_reset(
    str(tmp_path), credit_id="grant-next", expected_resets_left=2,
  )

  assert result == {**receipt, "reconciled": True}
  assert not intent_path.exists()


WEEKLY_READY = {
  "state": "ready",
  "plan_label": "Max plan",
  "windows": [{
    "id": "seven_day", "kind": "weekly", "label": "Weekly",
    "used_percent": 42, "resets_at": "2099-09-05T03:00:00+00:00",
  }],
  "credit_balance": None,
}


def test_the_last_reading_survives_a_restart_while_the_provider_refuses(
  monkeypatch, tmp_path,
):
  """A restart empties memory, and the first reads after it are often refused.

  The last good reading bridges that gap as a stale gauge instead of the
  weekly figure vanishing until the provider answers again.
  """
  from app import provider_usage

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  answers = [WEEKLY_READY]

  async def fake_snapshot(_provider_id, _data_dir):
    answer = answers.pop(0)
    if isinstance(answer, Exception):
      raise answer
    return answer

  monkeypatch.setattr(provider_usage, "_provider_snapshot", fake_snapshot)
  first = asyncio.run(provider_usage.read_provider_usage("claude", str(tmp_path)))
  assert first["state"] == "ready"

  provider_usage._provider_usage_cache.clear()  # the restart
  answers.append(provider_usage.ProviderUsageRefused("claude", None))
  after_restart = asyncio.run(
    provider_usage.read_provider_usage("claude", str(tmp_path))
  )

  assert after_restart["state"] == "ready"
  assert after_restart["stale"] is True
  assert after_restart["windows"][0]["used_percent"] == 42
  # The refusal backoff still holds: a second request does not probe again.
  assert asyncio.run(
    provider_usage.read_provider_usage("claude", str(tmp_path))
  )["stale"] is True


def test_a_saved_reading_past_the_stale_bound_is_not_served(monkeypatch, tmp_path):
  import json
  import time

  from app import provider_usage

  provider_usage._provider_usage_cache.clear()
  provider_usage._provider_usage_locks.clear()
  path = provider_usage._last_reading_path(str(tmp_path), "claude")
  path.parent.mkdir(parents=True)
  path.write_text(json.dumps({
    "observed_at": time.time() - provider_usage._PROVIDER_USAGE_STALE_SECONDS - 5,
    "snapshot": WEEKLY_READY,
  }))

  async def refused(_provider_id, _data_dir):
    raise provider_usage.ProviderUsageRefused("claude", None)

  monkeypatch.setattr(provider_usage, "_provider_snapshot", refused)
  body = asyncio.run(provider_usage.read_provider_usage("claude", str(tmp_path)))

  assert body["state"] == "unavailable"
