"""Switching a chat onto a registered provider must not depend on a name list.

Adding the Möbius subscription used to leave the handoff synthesis dispatch
hard-coded to claude/codex, so selecting Möbius raised "Unknown incoming
provider" in the middle of an otherwise valid switch. These tests pin the
dispatch on the registry-declared runtime kind instead.
"""

from __future__ import annotations

import asyncio

import pytest

from app import compaction, providers


@pytest.mark.parametrize("provider_id", ["claude", "codex", "mobius"])
def test_dispatch_routes_by_runtime_kind(monkeypatch, provider_id):
  seen = {}

  async def _fake_claude_turn(prompt, **kwargs):
    seen.update(runtime="claude", kwargs=kwargs)
    return "a portable briefing"

  async def _fake_codex_turn(prompt, **kwargs):
    seen.update(runtime="codex", kwargs=kwargs)
    return "a portable briefing"

  monkeypatch.setattr(compaction, "_run_claude_summarize_turn", _fake_claude_turn)
  monkeypatch.setattr(compaction, "_run_codex_summarize_turn", _fake_codex_turn)
  out = asyncio.run(compaction._run_provider_summarize_turn(
    "synthesize",
    data_dir="/data",
    provider_id=provider_id,
    model=None,
    effort=None,
  ))
  assert out == "a portable briefing"
  expected_runtime = "claude" if provider_id == "claude" else "codex"
  assert seen["runtime"] == expected_runtime
  if expected_runtime == "codex":
    assert seen["kwargs"]["provider_id"] == provider_id
  else:
    assert "provider_id" not in seen["kwargs"]


def test_an_unregistered_provider_stays_unknown():
  with pytest.raises(compaction.CompactionError, match="Unknown incoming"):
    asyncio.run(compaction._run_provider_summarize_turn(
      "synthesize",
      data_dir="/data",
      provider_id="not-a-provider",
      model=None,
      effort=None,
    ))
