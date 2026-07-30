"""Provider-plan usage normalization and Settings endpoint contracts."""

import asyncio
import base64
import json


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
  assert snapshot["windows"][1]["used_percent"] == 54
  assert snapshot["credit_balance"] == "18.50 credits"


def test_normalizers_report_unavailable_without_inventing_limits():
  from app.provider_usage import normalize_claude_usage, normalize_codex_usage

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
  assert seen == ["codex"]


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
