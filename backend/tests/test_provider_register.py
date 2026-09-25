"""The Claude runner's provider-authored concise register.

The shared per-chat snapshot (`system_prompts.py`) is identical for every
provider. The Claude runner appends its own concise register on top; the Codex
runner declares none. These tests pin that seam: the register is appended (never
substituted), the shared base is preserved verbatim, an empty register is
behavior-neutral, and the register never leaks into the other provider.
"""

from __future__ import annotations

from app import claude_sdk_runner


def test_register_is_appended_after_base():
  out = claude_sdk_runner._system_prompt_with_register("SHARED CONSTITUTION")
  assert out.startswith("SHARED CONSTITUTION")
  assert "# Concise register" in out
  # Register comes AFTER the shared constitution, never in front of it.
  assert out.index("SHARED CONSTITUTION") < out.index("# Concise register")


def test_register_protects_required_behaviors():
  reg = claude_sdk_runner._CONCISE_REGISTER
  assert reg.strip()
  # Concision must not be allowed to suppress any of these.
  for required in (
    "lead with the result",
    "the turn closeout",
    "clarifying-question cards",
    "making non-obvious findings explicit",
    "citation",
  ):
    assert required in reg, f"register dropped protection for: {required!r}"


def test_register_does_not_duplicate_waiting_policy():
  reg = claude_sdk_runner._CONCISE_REGISTER
  assert "Monitor" not in reg
  assert "declare_wait" not in reg


def test_empty_register_is_behavior_neutral(monkeypatch):
  monkeypatch.setattr(claude_sdk_runner, "_CONCISE_REGISTER", "   ")
  assert claude_sdk_runner._system_prompt_with_register("BASE") == "BASE"


def test_codex_runner_declares_no_register():
  # The register lives only in the Claude runner; the other provider must not
  # gain a behavioral addendum from this change.
  from app import codex_sdk_runner

  assert not hasattr(codex_sdk_runner, "_CONCISE_REGISTER")


def test_register_states_the_harness_facts_the_replaced_default_gave():
  """Möbius replaces Claude Code's default prompt, whose harness section tells
  the model that visible text is the reply. Without it, updates meant for the
  partner ended up in folded thinking."""
  reg = claude_sdk_runner._CONCISE_REGISTER
  assert reg.index("# Harness") < reg.index("# Concise register")
  for required in (
    "Text you write outside tool calls is your reply",
    "thinking is folded away and is not a reply",
    "A denied tool call",
    "Report outcomes faithfully",
  ):
    assert required in reg, f"register dropped harness fact: {required!r}"
