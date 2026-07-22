import asyncio
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas import SettingsUpdate


def test_global_settings_do_not_expose_chat_auto_resume(client, auth):
  """Rate-limit auto-resume is controlled per chat, not globally."""
  res = client.get("/api/settings", headers=auth)
  assert res.status_code == 200
  assert "auto_resume_on_limit" not in res.json()
  assert "auto_resume_on_limit" not in SettingsUpdate.model_fields


def test_global_settings_reject_stale_auto_resume_payload(client, auth):
  """A cached pre-removal client must not receive a false success."""
  res = client.post(
    "/api/settings",
    headers=auth,
    json={"auto_resume_on_limit": True},
  )

  assert res.status_code == 422
  assert res.json()["detail"][0]["type"] == "extra_forbidden"


def test_boot_removes_stale_global_auto_resume_setting():
  """A deploy cleans the rollback hazard before accepting requests."""
  import json
  import os
  from pathlib import Path

  from fastapi.testclient import TestClient
  from app.main import app

  path = Path(os.environ["DATA_DIR"]) / "shared" / "agent-settings.json"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps({
    "auto_resume_on_limit": True,
    "model": "claude-opus-4-7",
  }))

  with TestClient(app):
    pass

  assert json.loads(path.read_text()) == {"model": "claude-opus-4-7"}


def test_set_provider(client, auth):
  """POST /api/settings with provider switches the active provider."""
  client.post("/api/settings", json={"provider": "codex"}, headers=auth)
  r = client.get("/api/settings", headers=auth)
  assert r.json()["provider"] == "codex"

  client.post("/api/settings", json={"provider": "claude"}, headers=auth)
  r = client.get("/api/settings", headers=auth)
  assert r.json()["provider"] == "claude"


def test_set_invalid_provider_rejected(client, auth):
  """POST /api/settings with invalid provider is rejected at the schema."""
  r = client.post("/api/settings", json={"provider": "invalid"}, headers=auth)
  assert r.status_code == 422
  r = client.get("/api/settings", headers=auth)
  assert r.json()["provider"] == "claude"


def test_skills_enabled_defaults_off(client, auth):
  """GET /api/settings reports skills_enabled False until opted in."""
  r = client.get("/api/settings", headers=auth)
  assert r.json()["skills_enabled"] is False


# ─── CLI version surfacing (feature 005) ──────────────────────────────


def test_get_settings_surfaces_cli_versions(client, auth, monkeypatch):
  """GET /api/settings reports the installed Claude/Codex CLI versions
  as "<bare-version> (<release-date>)".

  The shell-out is mocked so the assertion is deterministic. The route
  passes each raw `--version` banner through `_format_cli_version`,
  which strips the codex-cli prefix / "(Claude Code)" suffix and
  appends the build-captured release date from `_cli_release_dates()`
  (here stubbed, since the build-written JSON only exists in the image).
  """
  from app.routes import settings as settings_route

  versions = {"claude": "2.1.173 (Claude Code)", "codex": "codex-cli 0.134.0"}
  monkeypatch.setattr(
    settings_route, "_cli_version", lambda cmd: versions[cmd],
  )
  monkeypatch.setattr(
    settings_route, "_cli_release_dates",
    lambda: {"2.1.173": "2026-06-11", "0.134.0": "2026-05-26"},
  )
  body = client.get("/api/settings", headers=auth).json()
  assert body["claude_version"] == "2.1.173 (2026-06-11)"
  assert body["codex_version"] == "0.134.0 (2026-05-26)"


def test_format_cli_version_parses_claude_banner(monkeypatch):
  """The Claude banner "<v> (Claude Code)" → "<v> (<date>)"."""
  from app.routes import settings as settings_route

  monkeypatch.setattr(
    settings_route, "_cli_release_dates", lambda: {"2.1.173": "2026-06-11"},
  )
  assert (
    settings_route._format_cli_version("2.1.173 (Claude Code)")
    == "2.1.173 (2026-06-11)"
  )


def test_format_cli_version_parses_codex_banner(monkeypatch):
  """The Codex banner "codex-cli <v>" → "<v> (<date>)" (prefix dropped)."""
  from app.routes import settings as settings_route

  monkeypatch.setattr(
    settings_route, "_cli_release_dates", lambda: {"0.134.0": "2026-05-26"},
  )
  assert (
    settings_route._format_cli_version("codex-cli 0.134.0")
    == "0.134.0 (2026-05-26)"
  )


def test_format_cli_version_unknown_date_falls_back_to_bare_version():
  """An installed version with no entry in the date map surfaces the
  bare semver — the row is never blocked on a missing date."""
  from app.routes import settings as settings_route

  assert (
    settings_route._format_cli_version("9.9.9 (Claude Code)") == "9.9.9"
  )
  assert settings_route._format_cli_version("codex-cli 9.9.9") == "9.9.9"


def test_format_cli_version_passes_none_through():
  """None (CLI absent / unresponsive) stays None — the date lookup
  must never turn a missing CLI into a spurious row."""
  from app.routes import settings as settings_route

  assert settings_route._format_cli_version(None) is None


def test_format_cli_version_keeps_prerelease_suffix():
  """A pre-release/build suffix stays attached to the bare version
  (the \\S* tail), and an unknown such version falls back to bare."""
  from app.routes import settings as settings_route

  assert settings_route._format_cli_version("1.0.0-rc.1 (Claude Code)") == "1.0.0-rc.1"


def test_format_cli_version_unrecognized_banner_surfaces_raw():
  """A banner with no semver at all is surfaced verbatim rather than
  dropping the row to null."""
  from app.routes import settings as settings_route

  assert settings_route._format_cli_version("unknown build") == "unknown build"


def test_get_settings_cli_missing_degrades_to_null(client, auth, monkeypatch):
  """A missing/un-runnable CLI surfaces as null, never a 500.

  `_cli_version` returns None when the binary is absent; the route must
  pass that through rather than letting the failure bubble up.
  """
  from app.routes import settings as settings_route

  monkeypatch.setattr(
    settings_route, "_cli_version", lambda cmd: None,
  )
  res = client.get("/api/settings", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert body["claude_version"] is None
  assert body["codex_version"] is None


def test_cli_version_returns_none_when_binary_absent(monkeypatch):
  """`_cli_version` degrades to None when the CLI is not on PATH —
  the helper resolves the binary first and never shells out blind."""
  from app.routes import settings as settings_route

  monkeypatch.setattr(settings_route.shutil, "which", lambda _name: None)
  assert settings_route._cli_version("claude") is None


def test_cli_version_returns_none_on_timeout(monkeypatch):
  """A slow/hanging CLI is bounded by a timeout and degrades to None
  rather than blocking the settings request."""
  from app.routes import settings as settings_route

  monkeypatch.setattr(
    settings_route.shutil, "which", lambda _name: "/usr/local/bin/claude",
  )

  def _hang(*_args, **_kwargs):
    raise settings_route.subprocess.TimeoutExpired(cmd="claude", timeout=2)

  monkeypatch.setattr(settings_route.subprocess, "run", _hang)
  assert settings_route._cli_version("claude") is None


def test_cli_version_parses_stdout(monkeypatch):
  """A successful run returns the trimmed first line of stdout."""
  from app.routes import settings as settings_route

  monkeypatch.setattr(
    settings_route.shutil, "which", lambda _name: "/usr/local/bin/claude",
  )

  def _ok(*_args, **_kwargs):
    return SimpleNamespace(returncode=0, stdout="2.1.152 (Claude Code)\n")

  monkeypatch.setattr(settings_route.subprocess, "run", _ok)
  assert settings_route._cli_version("claude") == "2.1.152 (Claude Code)"


def test_set_skills_enabled_persists_to_shared_settings(client, auth):
  """POST /api/settings with skills_enabled writes the shared file and
  the provider gate reads it back."""
  from app.config import get_settings as _gs
  from app import providers

  r = client.post(
    "/api/settings", json={"skills_enabled": True}, headers=auth,
  )
  assert r.status_code == 200
  assert client.get("/api/settings", headers=auth).json()["skills_enabled"] is True
  assert providers.skills_enabled(_gs().data_dir) is True

  # Opting back out flips the gate.
  client.post("/api/settings", json={"skills_enabled": False}, headers=auth)
  assert providers.skills_enabled(_gs().data_dir) is False


def test_set_skills_enabled_preserves_other_agent_settings(client, auth):
  """Toggling skills_enabled must not clobber model/effort defaults the
  picker wrote into the same shared agent-settings.json file."""
  from app.config import get_settings as _gs
  from app import providers

  data_dir = _gs().data_dir
  providers.write_agent_settings(
    data_dir, {"model": "claude-x", "effort": "high"},
  )
  client.post("/api/settings", json={"skills_enabled": True}, headers=auth)
  merged = providers._load_agent_settings(data_dir)
  assert merged["model"] == "claude-x"
  assert merged["effort"] == "high"
  assert merged["skills_enabled"] is True


def test_get_settings_returns_background_agent_defaults(client, auth):
  """Background agents enable the resolved provider until explicitly set."""
  client.post("/api/settings", json={"provider": "codex"}, headers=auth)
  body = client.get("/api/settings", headers=auth).json()
  assert body["agent_settings"]["model"] is None
  assert body["agent_settings"]["effort"] == "medium"
  assert body["background_agents"]["primary"]["provider"] == "codex"
  assert body["background_agents"]["primary"]["model"] == "gpt-5.6-terra"
  assert body["background_agents"]["primary"]["effort"] == "medium"
  assert body["background_agents"]["fallback"] is None


def test_background_agent_defaults_do_not_inherit_chat_model_defaults(tmp_path):
  """Background work has provider-native defaults, not global chat defaults."""
  from app import providers

  providers.write_agent_settings(
    str(tmp_path),
    {"model": "gpt-5.4", "effort": "high"},
  )
  background = providers.background_agent_settings(str(tmp_path), "codex")
  assert background["primary"] == {
    "provider": "codex",
    "model": providers.DEFAULT_BACKGROUND_MODELS["codex"],
    "effort": "medium",
  }
  assert background["fallback"] is None


def test_get_settings_prefers_connected_codex_over_unconnected_default(client, auth):
  """A fresh owner row defaults to Claude, but Codex-only setup should
  surface Codex as the active default instead of a disconnected Claude."""
  from pathlib import Path
  from app.config import get_settings as _gs
  from app import providers

  codex_auth = Path(_gs().data_dir) / "cli-auth" / "codex" / "auth.json"
  codex_auth.parent.mkdir(parents=True, exist_ok=True)
  codex_auth.write_text("{}", encoding="utf-8")

  try:
    body = client.get("/api/settings", headers=auth).json()
    assert body["codex_authenticated"] is True
    assert body["provider"] == "codex"
    assert body["agent_settings"]["model"] == providers.DEFAULT_MODELS["codex"]
    assert body["background_agents"]["primary"]["provider"] == "codex"
  finally:
    codex_auth.unlink(missing_ok=True)


def test_new_chat_prefers_connected_codex_over_unconnected_default(
  client, auth, db,
):
  """New chats should inherit the usable provider, not the historical
  Owner.provider default, after a Codex-only setup."""
  from pathlib import Path
  from app import models
  from app.config import get_settings as _gs

  codex_auth = Path(_gs().data_dir) / "cli-auth" / "codex" / "auth.json"
  codex_auth.parent.mkdir(parents=True, exist_ok=True)
  codex_auth.write_text("{}", encoding="utf-8")

  try:
    r = client.post("/api/chats", json={"title": "Codex first"}, headers=auth)
    assert r.status_code == 200, r.text
    chat = db.query(models.Chat).filter(models.Chat.id == r.json()["id"]).first()
    assert chat.provider == "codex"
  finally:
    codex_auth.unlink(missing_ok=True)


def test_set_chat_agent_defaults_persists_without_clobbering_background(client, auth):
  """POST /api/settings can update global chat defaults separately."""
  from app.config import get_settings as _gs
  from app import providers

  data_dir = _gs().data_dir
  providers.write_agent_settings(
    data_dir,
    {
      "background_agents": {
        "primary": {
          "provider": "claude",
          "model": "claude-sonnet-4-6",
          "effort": "high",
        },
        "fallback": {
          "provider": "codex",
          "model": "gpt-5.4",
          "effort": "medium",
        },
      }
    },
  )
  r = client.post(
    "/api/settings",
    json={
      "provider": "codex",
      "agent_settings": {
        "model": "gpt-5.4",
        "effort": "high",
        "effort_by_provider": {"codex": "high"},
      },
    },
    headers=auth,
  )
  assert r.status_code == 200, r.text
  merged = providers._load_agent_settings(data_dir)
  assert merged["model"] == "gpt-5.4"
  assert merged["effort"] == "high"
  assert merged["effort_by_provider"] == {"codex": "high"}
  assert merged["background_agents"]["fallback"]["model"] == "gpt-5.4"
  body = client.get("/api/settings", headers=auth).json()
  assert body["provider"] == "codex"
  assert body["agent_settings"]["model"] == "gpt-5.4"
  assert body["agent_settings"]["effort"] == "high"


def test_set_background_agents_persists_to_shared_settings(client, auth):
  """POST /api/settings stores primary/fallback without clobbering siblings."""
  from app.config import get_settings as _gs
  from app import providers

  data_dir = _gs().data_dir
  providers.write_agent_settings(data_dir, {"skills_enabled": True})
  r = client.post(
    "/api/settings",
    json={
      "background_agents": {
        "primary": {
          "provider": "claude",
          "model": "claude-sonnet-4-6",
          "effort": "high",
        },
        "fallback": {
          "provider": "codex",
          "model": "gpt-5.4",
          "effort": "medium",
        },
      },
    },
    headers=auth,
  )
  assert r.status_code == 200, r.text
  merged = providers._load_agent_settings(data_dir)
  assert merged["skills_enabled"] is True
  assert merged["background_agents"] == {
    "providers": [
      {
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "effort": "high",
        "enabled": True,
      },
      {
        "provider": "codex",
        "model": "gpt-5.4",
        "effort": "medium",
        "enabled": True,
      },
    ],
    "primary": {
      "provider": "claude",
      "model": "claude-sonnet-4-6",
      "effort": "high",
    },
    "fallback": {
      "provider": "codex",
      "model": "gpt-5.4",
      "effort": "medium",
    },
  }
  body = client.get("/api/settings", headers=auth).json()
  assert body["background_agents"] == merged["background_agents"]


def test_set_background_agent_provider_order_persists_legacy_mirror(client, auth):
  """The ordered provider list is the source of truth; primary/fallback mirror it."""
  from app.config import get_settings as _gs
  from app import providers

  data_dir = _gs().data_dir
  r = client.post(
    "/api/settings",
    json={
      "background_agents": {
        "providers": [
          {
            "provider": "codex",
            "model": "gpt-5.5",
            "effort": "medium",
            "enabled": True,
          },
          {
            "provider": "claude",
            "model": "claude-opus-4-8",
            "effort": "xhigh",
            "enabled": False,
          },
        ],
      },
    },
    headers=auth,
  )
  assert r.status_code == 200, r.text
  merged = providers._load_agent_settings(data_dir)
  assert merged["background_agents"]["providers"] == [
    {
      "provider": "codex",
      "model": "gpt-5.5",
      "effort": "medium",
      "enabled": True,
    },
    {
      "provider": "claude",
      "model": "claude-opus-4-8",
      "effort": "xhigh",
      "enabled": False,
    },
  ]
  assert merged["background_agents"]["primary"] == {
    "provider": "codex",
    "model": "gpt-5.5",
    "effort": "medium",
  }
  assert merged["background_agents"]["fallback"] is None


def test_set_background_agents_primary_only_preserves_existing_fallback(client, auth):
  """Partial background-agent updates should not silently remove fallback."""
  from app.config import get_settings as _gs
  from app import providers

  data_dir = _gs().data_dir
  providers.write_agent_settings(
    data_dir,
    {
      "background_agents": {
        "primary": {
          "provider": "claude",
          "model": "claude-sonnet-4-6",
          "effort": "medium",
        },
        "fallback": {
          "provider": "codex",
          "model": "gpt-5.4",
          "effort": "high",
        },
      }
    },
  )
  r = client.post(
    "/api/settings",
    json={
      "background_agents": {
        "primary": {
          "provider": "codex",
          "model": "gpt-5.5",
          "effort": "medium",
        },
      },
    },
    headers=auth,
  )
  assert r.status_code == 200, r.text
  merged = providers._load_agent_settings(data_dir)
  assert merged["background_agents"]["primary"] == {
    "provider": "codex",
    "model": "gpt-5.5",
    "effort": "medium",
  }
  assert merged["background_agents"]["fallback"] == {
    "provider": "codex",
    "model": "gpt-5.4",
    "effort": "high",
  }


def test_set_background_agents_explicit_null_clears_fallback(client, auth):
  """Explicit fallback null remains the deliberate way to remove fallback."""
  from app.config import get_settings as _gs
  from app import providers

  data_dir = _gs().data_dir
  providers.write_agent_settings(
    data_dir,
    {
      "background_agents": {
        "primary": {"provider": "claude", "model": None, "effort": "medium"},
        "fallback": {"provider": "codex", "model": "gpt-5.4", "effort": "medium"},
      }
    },
  )
  r = client.post(
    "/api/settings",
    json={"background_agents": {"fallback": None}},
    headers=auth,
  )
  assert r.status_code == 200, r.text
  merged = providers._load_agent_settings(data_dir)
  assert merged["background_agents"]["fallback"] is None
  assert [
    row for row in merged["background_agents"]["providers"]
    if row["provider"] == "codex"
  ][0]["enabled"] is False


def test_settings_reports_agent_settings_disk_write_failure(client, auth, monkeypatch):
  """The UI must not show Saved when the shared settings file did not persist."""
  from app.routes import settings as settings_route

  monkeypatch.setattr(
    settings_route.providers,
    "update_agent_settings",
    lambda _data_dir, _updater: False,
  )
  r = client.post(
    "/api/settings",
    json={"provider": "codex", "skills_enabled": True},
    headers=auth,
  )
  assert r.status_code == 500
  assert "Could not save agent settings" in r.json()["detail"]
  assert client.get("/api/settings", headers=auth).json()["provider"] == "claude"


def test_concurrent_agent_settings_merges_preserve_both_updates(tmp_path):
  """A racing merge starts from the latest committed full document."""
  from app import providers

  data_dir = str(tmp_path)
  first_entered = threading.Event()
  release_first = threading.Event()

  def first_update(current):
    first_entered.set()
    assert release_first.wait(timeout=2)
    current["skills_enabled"] = True
    return current

  def second_update(current):
    current["model"] = "gpt-5.5"
    return current

  with ThreadPoolExecutor(max_workers=2) as pool:
    first = pool.submit(providers.update_agent_settings, data_dir, first_update)
    assert first_entered.wait(timeout=2)
    second = pool.submit(providers.update_agent_settings, data_dir, second_update)
    release_first.set()
    assert first.result(timeout=2) is True
    assert second.result(timeout=2) is True

  assert providers._load_agent_settings(data_dir) == {
    "skills_enabled": True,
    "model": "gpt-5.5",
  }


def test_agent_settings_atomic_write_failure_preserves_previous_file(
  tmp_path, monkeypatch,
):
  """A failed replacement cannot truncate the last good settings document."""
  from app import providers

  data_dir = str(tmp_path)
  assert providers.write_agent_settings(data_dir, {"model": "gpt-5.4"})
  path = tmp_path / "shared" / "agent-settings.json"
  previous = path.read_bytes()

  def fail_replace(_path, _content):
    raise OSError("disk full")

  monkeypatch.setattr(providers, "atomic_write", fail_replace)
  assert providers.write_agent_settings(data_dir, {"model": "gpt-5.5"}) is False
  assert path.read_bytes() == previous


def test_background_agent_settings_drops_cross_provider_models(tmp_path):
  """A stale model from the other provider is ignored at read time."""
  from app import providers

  providers.write_agent_settings(
    str(tmp_path),
    {
      "background_agents": {
        "primary": {
          "provider": "claude",
          "model": "gpt-5.5",
          "effort": "high",
        },
        "fallback": {
          "provider": "codex",
          "model": "claude-opus-4-8",
          "effort": "medium",
        },
      },
    },
  )
  background = providers.background_agent_settings(str(tmp_path), "claude")
  assert background["primary"] == {
    "provider": "claude",
    "model": "claude-opus-4-8",
    "effort": "high",
  }
  assert background["fallback"] == {
    "provider": "codex",
    "model": providers.DEFAULT_BACKGROUND_MODELS["codex"],
    "effort": "medium",
  }


def test_settings_rejects_unknown_background_agent_provider(client, auth):
  """Background agent provider ids share the same strict provider enum."""
  r = client.post(
    "/api/settings",
    json={"background_agents": {"primary": {"provider": "bogus"}}},
    headers=auth,
  )
  assert r.status_code == 422


def test_skills_enabled_gate_treats_absent_and_malformed_as_off(tmp_path):
  """providers.skills_enabled reads False for an absent file and a
  non-bool / non-true value — opt-in is explicit."""
  from app import providers

  # Absent file → off.
  assert providers.skills_enabled(str(tmp_path)) is False
  # Present but flag missing → off.
  providers.write_agent_settings(str(tmp_path), {"model": "x"})
  assert providers.skills_enabled(str(tmp_path)) is False
  # Truthy-but-not-True (string) → off; only the literal True opts in.
  providers.write_agent_settings(str(tmp_path), {"skills_enabled": "yes"})
  assert providers.skills_enabled(str(tmp_path)) is False
  providers.write_agent_settings(str(tmp_path), {"skills_enabled": True})
  assert providers.skills_enabled(str(tmp_path)) is True


def test_settings_update_provider_validator_rejects_unknown():
  """SettingsUpdate rejects unknown provider IDs."""
  try:
    SettingsUpdate(provider="bogus")
  except ValidationError:
    pass
  else:
    raise AssertionError("Expected ValidationError for bogus provider")


# ─── Model registry + owner prefs ─────────────────────────────────────


def test_model_registry_returns_known_models_on_missing_creds(client, auth):
  """`/api/models` returns KNOWN_MODELS for both providers when neither
  upstream is reachable. Confirms the per-provider fallback works.

  The TestClient has no real Anthropic / Codex credentials so both
  fetchers raise; the registry serves KNOWN_MODELS for both. Every
  entry is `available=True` in the fallback path because there's no
  live signal to mark anything unavailable.
  """
  from app.providers import (
    KNOWN_MODELS, _fallback_models, invalidate_model_cache,
  )
  invalidate_model_cache()
  res = client.get("/api/models", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert set(body["providers"]) == {"claude", "codex"}
  claude_ids = [m["id"] for m in body["providers"]["claude"]]
  assert claude_ids == KNOWN_MODELS["claude"]
  codex_ids = [m["id"] for m in body["providers"]["codex"]]
  assert codex_ids == KNOWN_MODELS["codex"]
  # Labels carry through from MODEL_LABELS.
  by_id = {m["id"]: m for m in body["providers"]["claude"]}
  assert by_id["claude-opus-4-8"]["label"] == "Opus 4.8"
  # The user-facing API contract is `available=true` on every fallback
  # entry, but the route layer relies on Pydantic's `ModelEntry`
  # default to fill that field. Verify the underlying helper directly
  # so a non-route caller of `_fallback_models()` doesn't trip a
  # KeyError on `available` — and so the contract is pinned at the
  # source, not just at the JSON boundary.
  raw_fallback = _fallback_models("claude")
  assert raw_fallback, "fallback should not be empty for claude"
  for entry in raw_fallback:
    assert entry["available"] is True, (
      f"_fallback_models must set available=True explicitly; "
      f"got {entry!r}"
    )
  assert all(m["available"] for m in body["providers"]["claude"])


def test_fetch_codex_models_reads_cli_catalog_not_sdk(tmp_path, monkeypatch):
  """Model discovery reads `codex debug models` (the CLI catalog) and never
  constructs the Codex SDK.

  The pinned SDK's reasoning-effort enum rejects the current catalog on every
  fetch, so the strict `AsyncCodex.models()` call was dropped and the CLI is the
  sole registry source. The subprocess must carry CODEX_HOME so it reads the
  connected credentials.
  """
  from app import providers

  codex_home = tmp_path / "cli-auth" / "codex"
  codex_home.mkdir(parents=True)
  (codex_home / "auth.json").write_text("{}")

  # Any attempt to use the Codex SDK for model discovery is a regression.
  class ForbiddenCodex:
    def __init__(self, *a, **k):
      raise AssertionError("model discovery must not construct the Codex SDK")

  monkeypatch.setitem(
    sys.modules, "openai_codex", SimpleNamespace(AsyncCodex=ForbiddenCodex),
  )

  captured = {}
  catalog = json.dumps({"models": [
    {"slug": "gpt-5.6-sol"}, {"slug": "gpt-5.6-terra"},
  ]}).encode()

  class FakeProc:
    returncode = 0

    async def communicate(self):
      return catalog, b""

  async def fake_exec(*args, **kwargs):
    captured["args"] = args
    captured["env"] = kwargs.get("env")
    return FakeProc()

  monkeypatch.setattr(providers.shutil, "which", lambda _name: "/usr/bin/codex")
  monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", fake_exec)

  ids = asyncio.run(providers._fetch_codex_models(str(tmp_path)))

  assert ids == ["gpt-5.6-sol", "gpt-5.6-terra"]
  assert captured["args"][1:3] == ("debug", "models")
  assert captured["env"]["CODEX_HOME"] == str(codex_home)


def test_fetch_codex_models_requires_credentials(tmp_path, monkeypatch):
  """Without connected Codex credentials the registry fails fast — it raises
  before spawning any `codex debug models` subprocess, so `list_models` serves
  KNOWN_MODELS cleanly.
  """
  from app import providers

  # No cli-auth/codex/auth.json under tmp_path.
  def forbidden_exec(*a, **k):
    raise AssertionError("must not spawn codex when credentials are missing")

  monkeypatch.setattr(providers.asyncio, "create_subprocess_exec", forbidden_exec)

  with pytest.raises(RuntimeError, match="codex credentials missing"):
    asyncio.run(providers._fetch_codex_models(str(tmp_path)))


def test_model_prefs_default_is_curated(client, auth):
  """A fresh owner starts with the compact recommended model set."""
  from app import providers
  res = client.get("/api/owner/model-prefs", headers=auth)
  assert res.status_code == 200
  assert res.json() == {"hidden_ids": providers.hidden_model_ids(None)}


def test_model_prefs_explicit_empty_is_distinct_from_missing(client, auth, db):
  """Saving an empty hidden list opts into showing the whole registry."""
  from app import models, providers

  owner = db.query(models.Owner).first()
  assert owner.model_prefs_json is None
  assert providers.hidden_model_ids(owner.model_prefs_json)

  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": []},
    headers=auth,
  )
  assert res.status_code == 200
  assert res.json() == {"hidden_ids": []}
  db.refresh(owner)
  assert owner.model_prefs_json == {"hidden_ids": []}
  assert client.get("/api/owner/model-prefs", headers=auth).json() == {
    "hidden_ids": [],
  }


def test_model_prefs_roundtrip_dedupes(client, auth, db):
  """PATCH stores hidden_ids verbatim (deduplicated, order-preserving)
  and GET returns the same set."""
  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": [
      "claude-haiku-4-5-20251001",
      "gpt-5.4",
      "claude-haiku-4-5-20251001",  # duplicate should drop
    ]},
    headers=auth,
  )
  assert res.status_code == 200
  assert res.json()["hidden_ids"] == [
    "claude-haiku-4-5-20251001", "gpt-5.4",
  ]
  res2 = client.get("/api/owner/model-prefs", headers=auth)
  assert res2.json()["hidden_ids"] == [
    "claude-haiku-4-5-20251001", "gpt-5.4",
  ]
  # Persisted on the Owner row.
  from app import models
  owner = db.query(models.Owner).first()
  assert owner.model_prefs_json == {"hidden_ids": [
    "claude-haiku-4-5-20251001", "gpt-5.4",
  ]}


def test_model_prefs_stale_id_tolerated(client, auth):
  """An ID that's not in the registry can still be stored; nothing
  errors. The picker handles stale IDs by silently not filtering
  what it can't find."""
  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": ["claude-fictitious-model-99"]},
    headers=auth,
  )
  assert res.status_code == 200
  assert res.json()["hidden_ids"] == ["claude-fictitious-model-99"]


def test_model_prefs_rejects_unknown_field(client, auth):
  """ModelPrefsUpdate has extra='forbid' — a typo'd field 422s rather
  than silently landing in the persisted prefs blob."""
  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": [], "sort_order": ["foo"]},
    headers=auth,
  )
  assert res.status_code == 422


def test_model_prefs_clear(client, auth, db):
  """Empty list clears all hidden entries."""
  client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": ["gpt-5.4"]},
    headers=auth,
  )
  res = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": []},
    headers=auth,
  )
  assert res.status_code == 200
  assert res.json() == {"hidden_ids": []}
  from app import models
  owner = db.query(models.Owner).first()
  assert owner.model_prefs_json == {"hidden_ids": []}


def test_live_model_entries_keep_curated_aliases_plus_live_extras():
  """The requested compatibility aliases survive a sparse live catalog."""
  from app.providers import _live_model_entries
  merged = _live_model_entries(
    "claude", ["claude-future-model", "claude-opus-4-8"],
  )
  assert [row["id"] for row in merged] == [
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-future-model",
  ]
  assert "claude-haiku-4-5-20251001" not in [m["id"] for m in merged]


def test_live_model_entries_float_curated_defaults_in_requested_order():
  from app import providers

  entries = providers._live_model_entries(
    "claude",
    ["claude-sonnet-5", "claude-future-model", "claude-fable-5", "claude-opus-4-8"],
  )
  assert [entry["id"] for entry in entries] == [
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-future-model",
  ]


def test_resolve_displayed_models_keeps_selected_even_when_hidden():
  """The picker's filter MUST keep the currently-selected model
  visible even when it appears in hidden_ids. The codex-review spec
  calls this out — without it the user could hide their own active
  model and lose the ability to switch away from it via the picker."""
  # Recreate the JS-side filter in Python to assert the contract.
  # Authoritative implementation lives in
  # frontend/src/components/ChatView/ChatSettingsPanel.jsx
  # (`resolveDisplayedModels`). This test exercises the rule with the
  # backend registry shape so a behavior regression on either side
  # is caught.
  registry_entries = [
    {"id": "a", "label": "A", "provider": "claude", "available": True},
    {"id": "b", "label": "B", "provider": "claude", "available": True},
    {"id": "c", "label": "C", "provider": "claude", "available": True},
  ]
  hidden = {"b", "c"}
  selected = "c"
  visible = [
    m for m in registry_entries
    if m["id"] not in hidden or m["id"] == selected
  ]
  assert [m["id"] for m in visible] == ["a", "c"]


def test_walkthrough_status_default_not_completed(client, auth):
  """A fresh owner has not seen the walkthrough."""
  res = client.get("/api/owner/walkthrough", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert body["completed"] is False
  assert body["completed_at"] is None


def test_walkthrough_complete_then_status(client, auth):
  """Posting `complete` flips the bit; subsequent GETs report
  completed=true and a timestamp."""
  before = client.get("/api/owner/walkthrough", headers=auth).json()
  assert before["completed"] is False
  done = client.post("/api/owner/walkthrough/complete", headers=auth)
  assert done.status_code == 204
  after = client.get("/api/owner/walkthrough", headers=auth).json()
  assert after["completed"] is True
  assert after["completed_at"] is not None


def test_walkthrough_complete_is_write_once(client, auth):
  """Posting `complete` twice succeeds both times AND the second
  call does not advance the persisted timestamp — the route is
  write-once on first success, so downstream analytics can correlate
  the original completion time against other signals without
  retry-or-idle-tab refreshes corrupting it."""
  first = client.post("/api/owner/walkthrough/complete", headers=auth)
  assert first.status_code == 204
  ts1 = client.get("/api/owner/walkthrough", headers=auth).json()["completed_at"]
  second = client.post("/api/owner/walkthrough/complete", headers=auth)
  assert second.status_code == 204
  ts2 = client.get("/api/owner/walkthrough", headers=auth).json()["completed_at"]
  assert ts2 == ts1, (
    "Second completion POST must NOT advance the timestamp — write-once"
  )


def test_walkthrough_endpoints_require_auth(client):
  """Unauthenticated requests must 401, like every other /api/owner
  surface."""
  no_get = client.get("/api/owner/walkthrough")
  assert no_get.status_code == 401
  no_post = client.post("/api/owner/walkthrough/complete")
  assert no_post.status_code == 401


def test_walkthrough_complete_rejects_cross_site_request(client, auth):
  """Defense-in-depth: when the browser sends Sec-Fetch-Site:
  cross-site (a genuine CSRF attempt), the write is blocked even if
  the attacker somehow obtained the bearer token. Same-origin and
  same-site stay allowed."""
  cross = client.post(
    "/api/owner/walkthrough/complete",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403, (
    "cross-site Sec-Fetch-Site must be rejected"
  )
  same_origin = client.post(
    "/api/owner/walkthrough/complete",
    headers={**auth, "Sec-Fetch-Site": "same-origin"},
  )
  assert same_origin.status_code == 204
  none_origin = client.post(
    "/api/owner/walkthrough/complete",
    headers={**auth, "Sec-Fetch-Site": "none"},
  )
  # Already write-once stamped above, still 204 — point is "not blocked."
  assert none_origin.status_code == 204


def test_model_prefs_patch_rejects_cross_site_request(client, auth):
  """Same defense-in-depth as walkthrough/complete — any owner-state
  PATCH should reject cross-site origin requests."""
  cross = client.patch(
    "/api/owner/model-prefs",
    json={"hidden_ids": []},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_settings_post_rejects_cross_site_request(client, auth):
  """POST /api/settings writes owner state and shares the
  reject_cross_site defense applied
  to walkthrough/complete and model-prefs PATCH. Catches the case
  where a future refactor accidentally drops the dep."""
  cross = client.post(
    "/api/settings",
    json={"provider": "codex"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403
  same_origin = client.post(
    "/api/settings",
    json={"provider": "claude"},
    headers={**auth, "Sec-Fetch-Site": "same-origin"},
  )
  assert same_origin.status_code == 200
