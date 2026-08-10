"""Tests for the canonical scheduled background-agent policy.

These lock in what the PLATFORM owns: the system ordering, the shared
normalization, and the uniform override contract every background agent speaks.
The contract is deliberately shape-agnostic — an absent key inherits the system
default, a present key owns that slot — so no app's settings format appears
here. Each app's translation from its own settings screen into this shape is
tested with that app, which is what keeps this module free of app knowledge.
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


def test_empty_override_inherits_system_untouched(tmp_path):
  # An app with no preference declares nothing; both slots stay the owner's.
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(str(tmp_path), {})
  assert out["primary"]["model"] == "claude-opus-4-8"
  assert out["fallback"]["provider"] == "codex"


def test_declared_primary_owns_the_slot(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"primary": {"provider": "codex", "model": "gpt-5.5"}})
  assert out["primary"] == {"provider": "codex", "model": "gpt-5.5", "effort": None}


def test_declared_primary_owns_the_slot_even_when_it_matches_the_default(tmp_path):
  # A declared choice is honored verbatim: the null model must NOT silently
  # inherit the system's model, or an app could never pin the SDK default.
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"primary": {"provider": "claude", "model": None}})
  assert out["primary"] == {"provider": "claude", "model": None, "effort": None}


def test_absent_primary_key_leaves_system_primary(tmp_path):
  # Declaring only a fallback must not disturb the system primary.
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"fallback": {"provider": "codex", "model": "gpt-5.5"}})
  assert out["primary"]["model"] == "claude-opus-4-8"


def test_declared_fallback_owns_the_slot(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"fallback": {"provider": "codex", "model": "gpt-5.5"}})
  assert out["fallback"] == {"provider": "codex", "model": "gpt-5.5", "effort": None}


def test_explicit_null_fallback_means_no_second_agent(tmp_path):
  # Present-but-None is the only way to say "run without a fallback", and must
  # not be confused with an absent key, which inherits one.
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(str(tmp_path), {"fallback": None})
  assert out["primary"]["provider"] == "claude"
  assert out["fallback"] is None


def test_unusable_declared_primary_keeps_system_rather_than_no_agent(tmp_path):
  # A malformed declaration must never leave the night with no agent at all.
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"primary": {"provider": "nonsense"}})
  assert out["primary"]["provider"] == "claude"


def test_declared_choice_is_normalized_like_every_other(tmp_path):
  # Normalization lives here precisely so no app has to repeat it: a model
  # belonging to the other provider is dropped rather than passed through.
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"primary": {"provider": "codex", "model": "claude-opus-4-8"}})
  assert out["primary"] == {"provider": "codex", "model": None, "effort": None}


def test_disabled_declared_choice_is_ignored(tmp_path):
  _write_global(tmp_path, PROVIDERS_LIST)
  out = bg.resolve_background_agents(
    str(tmp_path), {"fallback": {"provider": "codex", "enabled": False}})
  assert out["fallback"] is None


def test_dedup_identical_primary_fallback_nulls_fallback(tmp_path):
  _write_global(tmp_path, {"providers": [
    {"provider": "claude", "model": "claude-opus-4-8", "effort": "medium", "enabled": True},
  ]})
  out = bg.resolve_background_agents(
    str(tmp_path), {"fallback": {"provider": "claude", "model": "claude-opus-4-8",
                                 "effort": "medium"}})
  assert out["primary"]["provider"] == "claude"
  assert out["fallback"] is None


def test_resolver_carries_no_app_settings_vocabulary():
  # The regression this migration exists to prevent: app-specific setting names
  # creeping back into the shared resolver.
  import inspect
  source = inspect.getsource(bg)
  for token in ("primary_agent_mode", "secondary_agent_mode",
                "fallback_provider", "fallback_model", "fallback_effort"):
    assert token not in source, f"app settings key {token!r} leaked into the resolver"


def test_no_settings_file_falls_back_to_provider_default(tmp_path):
  # No agent-settings.json at all → system default (claude, SDK-default model).
  out = bg.resolve_background_agents(str(tmp_path), None)
  assert out["primary"]["provider"] == "claude"


def test_no_settings_file_uses_the_only_connected_provider(tmp_path):
  codex_home = tmp_path / "cli-auth" / "codex"
  codex_home.mkdir(parents=True)
  (codex_home / "auth.json").write_text("{}")

  out = bg.resolve_background_agents(str(tmp_path), None)

  assert out["primary"] == {
    "provider": "codex", "model": None, "effort": None,
  }
  assert out["fallback"] is None
