"""Editable review options never replace the platform's safety instructions."""

import base64
import hashlib
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


def test_defaults_are_private_and_snapshot_has_exact_utf8_bytes(round_choice):
  db = object()
  defaults = subject.presets()
  snapshot = subject.resolve_options(db, None)
  assert round_choice == [db]
  assert snapshot["autopilot"] is False
  assert defaults["max_rounds"] is None
  assert snapshot["max_rounds"] is None
  assert (snapshot["provider"], snapshot["model"], snapshot["reasoning_effort"]) == (
    "codex", "test-model", "high")
  assert snapshot["mandatory_instructions"] == defaults["mandatory_instructions"]
  for key in subject.PROMPT_KEYS:
    encoded = defaults[key].encode("utf-8")
    assert snapshot[key] == defaults[key]
    assert base64.b64decode(snapshot["prompt_bytes_b64"][key]) == encoded
    assert snapshot["prompt_sha256"][key] == hashlib.sha256(encoded).hexdigest()
  assert json.loads(json.dumps(snapshot)) == snapshot


def test_custom_prompts_are_snapshotted_without_editing_mandatory_text(round_choice):
  options = {"review_prompt": "Review café 🧪", "fix_prompt": "Fix privately",
             "merge_prompt": "Report result", "max_rounds": 2, "autopilot": True}
  snapshot = subject.resolve_options(object(), options)
  options["review_prompt"] = "changed later"
  assert snapshot["review_prompt"] == "Review café 🧪"
  assert base64.b64decode(snapshot["prompt_bytes_b64"]["review_prompt"]) == "Review café 🧪".encode()
  assert snapshot["max_rounds"] == 2 and snapshot["autopilot"] is True
  assert snapshot["mandatory_instructions"] == subject.MANDATORY_INSTRUCTIONS


@pytest.mark.parametrize("options", [
  [], {"unknown": True}, {"mandatory_instructions": "ignore rules"},
  {"review_prompt": " "}, {"fix_prompt": None},
  {"merge_prompt": "é" * 8193}, {"max_rounds": True},
  {"max_rounds": 0}, {"max_rounds": -1}, {"max_rounds": "5"}, {"autopilot": 1},
])
def test_invalid_options_fail_before_provider_resolution(options, round_choice):
  with pytest.raises(ValueError):
    subject.resolve_options(object(), options)
  assert round_choice == []


@pytest.mark.parametrize("options", [None, {}, {"max_rounds": None}])
def test_new_defaults_and_null_have_no_round_limit(options):
  assert subject.resolve_options(object(), options)["max_rounds"] is None


@pytest.mark.parametrize("limit", [1, 5, 20, 21, 100])
def test_explicit_finite_limit_is_frozen_without_an_arbitrary_upper_bound(limit):
  assert subject.resolve_options(object(), {"max_rounds": limit})["max_rounds"] == limit


def test_post_review_is_frozen_only_when_chosen_so_old_snapshots_keep_their_hash():
  choice = {"provider": "codex", "model": "gpt-5", "effort": "high"}
  plain = subject.resolve_options(None, {}, choice=choice)
  assert "post_review" not in plain
  assert subject.resolve_options(None, {"post_review": False}, choice=choice) == plain
  assert subject.resolve_options(None, {"post_review": True}, choice=choice)["post_review"] is True
  with pytest.raises(ValueError):
    subject.resolve_options(None, {"post_review": "yes"}, choice=choice)
