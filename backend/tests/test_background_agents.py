"""Tests for the canonical background-agent resolver (app.background_agents).

One resolver now serves every nightly agent (Reflection, Memory, News); these
lock in the system-vs-app layering and the two owner toggles
(primary_agent_mode, secondary_agent_mode), including the behavior-preserving
legacy-default heuristic that keeps prod's live config (app pins provider=claude
with no model) inheriting the system model rather than dropping it.
"""

import json

from app import background_agents as bg


def _write_global(tmp_path, background):
  d = tmp_path / "shared"
  d.mkdir(parents=True, exist_ok=True)
  (d / "agent-settings.json").write_text(json.dumps({"background_agents": background}))


PROVIDERS_LIST = {
  "providers": [
    {"provider": "claude", "model": "claude-opus-4-8", "effort": "medium", "enabled": True},
    {"provider": "codex", "model": "gpt-5.5", "effort": "medium", "enabled": True},
  ],
  "primary": {"provider": "claude", "model": "claude-opus-4-8", "effort": "medium"},
  "fallback": {"provider": "codex", "model": "gpt-5.5", "effort": "medium"},
}


def test_system_only_uses_providers_list(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(str(tmp_path), None)
  assert out["primary"] == {"provider": "claude", "model": "claude-opus-4-8", "effort": "medium"}
  assert out["fallback"] == {"provider": "codex", "model": "gpt-5.5", "effort": "medium"}


def test_legacy_claude_no_model_inherits_system(tmp_path):
  # This is prod's LIVE app-56 shape: provider pinned to claude, model null,
  # no mode. It must NOT override (which would drop opus-4-8 to the SDK default).
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(str(tmp_path), {"provider": "claude", "model": None})
  assert out["primary"]["model"] == "claude-opus-4-8"  # inherited, not dropped
  assert out["fallback"]["provider"] == "codex"


def test_app_primary_override_switches_provider(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(str(tmp_path), {"provider": "codex", "model": "gpt-5.5"})
  assert out["primary"] == {"provider": "codex", "model": "gpt-5.5", "effort": None}


def test_primary_agent_mode_system_ignores_app_pin(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"primary_agent_mode": "system", "provider": "codex", "model": "gpt-5.5"})
  assert out["primary"]["provider"] == "claude"  # system wins


def test_primary_agent_mode_app_forces_app_even_default(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"primary_agent_mode": "app", "provider": "claude", "model": None})
  # Forced app mode: claude with null model (not the system's opus-4-8).
  assert out["primary"] == {"provider": "claude", "model": None, "effort": None}


def test_secondary_mode_app_uses_app_fallback(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"secondary_agent_mode": "app",
                    "fallback_provider": "codex", "fallback_model": "gpt-5.5"})
  assert out["fallback"] == {"provider": "codex", "model": "gpt-5.5", "effort": None}


def test_secondary_mode_system_ignores_app_fallback(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"secondary_agent_mode": "system",
                    "fallback_provider": "claude", "fallback_model": "claude-opus-4-8"})
  assert out["fallback"] == {"provider": "codex", "model": "gpt-5.5", "effort": "medium"}  # system


def test_secondary_mode_unset_presence_heuristic(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"fallback_provider": "claude", "fallback_model": "claude-opus-4-8"})
  assert out["fallback"] == {"provider": "claude", "model": "claude-opus-4-8", "effort": None}


def test_dedup_identical_primary_fallback_nulls_fallback(tmp_path):
  _write_global(tmp_path, {"providers": [
    {"provider": "claude", "model": "claude-opus-4-8", "effort": "medium", "enabled": True},
  ]})
  out = bg.resolve_background_agents(
    str(tmp_path), {"secondary_agent_mode": "app",
                    "fallback_provider": "claude", "fallback_model": "claude-opus-4-8",
                    "fallback_effort": "medium"})
  assert out["primary"]["provider"] == "claude"
  assert out["fallback"] is None


def test_no_settings_file_falls_back_to_provider_default(tmp_path):
  # No agent-settings.json at all → system default (claude, SDK-default model).
  out = bg.resolve_background_agents(str(tmp_path), None)
  assert out["primary"]["provider"] == "claude"
