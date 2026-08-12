"""Provider-neutral execution boundary for inline native helpers."""

from app.delegation_runtime import native_subagents_enabled


def test_native_subagents_default_on_for_both_providers():
  assert native_subagents_enabled("claude", environ={}) is True
  assert native_subagents_enabled("codex", environ={}) is True


def test_shared_native_subagent_switch_disables_both_providers():
  environ = {"MOEBIUS_NATIVE_SUBAGENTS": "off"}
  assert native_subagents_enabled("claude", environ=environ) is False
  assert native_subagents_enabled("codex", environ=environ) is False


def test_legacy_codex_switch_remains_a_narrow_override():
  environ = {"MOEBIUS_CODEX_MULTI_AGENT": "0"}
  assert native_subagents_enabled("claude", environ=environ) is True
  assert native_subagents_enabled("codex", environ=environ) is False
