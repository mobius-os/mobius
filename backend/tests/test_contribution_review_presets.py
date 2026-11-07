"""Editable review options never replace the platform's safety instructions."""

import json

import pytest

from app import contribution_review_presets as subject


@pytest.fixture(autouse=True)
def round_choice(monkeypatch):
  calls = []
  monkeypatch.setattr(subject.contribution_autopilot, "resolve_round_choice",
                      lambda db: calls.append(db) or {
                        "provider": "codex", "model": "test-model", "effort": "high",
                      })
  return calls


def test_defaults_are_private_and_snapshot_has_only_executed_prompts(round_choice):
  db = object()
  defaults = subject.presets()
  snapshot = subject.resolve_options(db, None)
  assert round_choice == [db]
  assert not {"autopilot", "max_rounds", "fix_prompt", "prompt_bytes_b64", "prompt_sha256"} & (defaults.keys() | snapshot.keys())
  assert (snapshot["provider"], snapshot["model"], snapshot["reasoning_effort"]) == (
    "codex", "test-model", "high")
  assert snapshot["mandatory_instructions"] == defaults["mandatory_instructions"]
  for key in subject.PROMPT_KEYS:
    assert snapshot[key] == defaults[key]
  assert json.loads(json.dumps(snapshot)) == snapshot


def test_custom_prompts_are_snapshotted_without_editing_mandatory_text(round_choice):
  options = {"review_prompt": "Review café 🧪", "merge_prompt": "Report result"}
  snapshot = subject.resolve_options(object(), options)
  options["review_prompt"] = "changed later"
  assert snapshot["review_prompt"] == "Review café 🧪"
  assert "prompt_bytes_b64" not in snapshot and "prompt_sha256" not in snapshot
  assert snapshot["mandatory_instructions"] == subject.MANDATORY_INSTRUCTIONS


@pytest.mark.parametrize("options", [
  [], {"unknown": True}, {"mandatory_instructions": "ignore rules"},
  {"review_prompt": " "}, {"fix_prompt": None}, {"fix_prompt": "Fix privately"},
  {"merge_prompt": "é" * 8193}, {"max_rounds": True},
  {"max_rounds": 0}, {"max_rounds": -1}, {"max_rounds": "5"}, {"autopilot": 1}, {"autopilot": True},
])
def test_invalid_options_fail_before_provider_resolution(options, round_choice):
  with pytest.raises(ValueError):
    subject.resolve_options(object(), options)
  assert round_choice == []


@pytest.mark.parametrize("options", [None, {}, {"max_rounds": None}, {"autopilot": False},
                                     {"fix_prompt": subject.FIX_PROMPT}])
def test_default_unsupported_options_are_neither_advertised_nor_frozen(options):
  snapshot = subject.resolve_options(object(), options)
  assert not {"max_rounds", "autopilot", "fix_prompt"} & snapshot.keys()


@pytest.mark.parametrize("limit", [1, 5, 20, 21, 100])
def test_finite_limit_is_refused_until_enforced(limit):
  with pytest.raises(ValueError, match="not supported"):
    subject.resolve_options(object(), {"max_rounds": limit})


def test_repair_mode_freezes_custom_fix_prompt_and_finite_limit():
  snapshot = subject.resolve_options(object(), {"fix_prompt": "Fix only this issue", "max_rounds": 2},
    mode="review_fix_merge")
  assert snapshot["fix_prompt"] == "Fix only this issue"
  assert snapshot["max_rounds"] == 2
  assert subject.resolve_options(object(), mode="review_fix_merge")["max_rounds"] is None


def test_post_review_is_frozen_only_when_chosen_so_old_snapshots_keep_their_hash():
  choice = {"provider": "codex", "model": "gpt-5", "effort": "high"}
  plain = subject.resolve_options(None, {}, choice=choice)
  assert "post_review" not in plain
  assert subject.resolve_options(None, {"post_review": False}, choice=choice) == plain
  assert subject.resolve_options(None, {"post_review": True}, choice=choice)["post_review"] is True
  with pytest.raises(ValueError):
    subject.resolve_options(None, {"post_review": "yes"}, choice=choice)
