from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
  "compatibility_bootstrap", ROOT / "scripts" / "compatibility_bootstrap.py"
)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)

CURRENT_SHA = "b" * 40
PREVIOUS = bootstrap.Identity("sha256:" + "a" * 64, "a" * 40)
CURRENT = bootstrap.Identity("sha256:" + "b" * 64, CURRENT_SHA)


def classify(main, external=None, daily=None):
  return bootstrap.classify_inventory(
    main=main,
    external=external,
    daily=daily,
    current_sha=CURRENT_SHA,
    previous=PREVIOUS,
  )


def test_inventory_accepts_only_fresh_partial_and_complete_states():
  assert classify(PREVIOUS) == ("build", "")
  assert classify(CURRENT) == ("build", "")
  assert classify(CURRENT, CURRENT) == ("reuse", CURRENT.digest)


def test_inventory_rejects_mismatched_same_revision_and_unexpected_channels():
  other = bootstrap.Identity("sha256:" + "c" * 64, CURRENT_SHA)
  with pytest.raises(bootstrap.StateError):
    classify(CURRENT, other)
  with pytest.raises(bootstrap.StateError):
    classify(PREVIOUS, CURRENT)
  with pytest.raises(bootstrap.StateError):
    classify(PREVIOUS, daily=PREVIOUS)


def test_prewrite_rechecks_detect_tags_appearing_after_inventory():
  unexpected = bootstrap.Identity("sha256:" + "c" * 64, "c" * 40)
  bootstrap.assert_prewrite_state(
    tag="main",
    main=PREVIOUS,
    external=None,
    daily=None,
    current=CURRENT,
    previous=PREVIOUS,
  )
  # A lone current-revision main is untrusted metadata from an incomplete
  # attempt. The guarded build may replace it, but it may not carry forward to
  # external-recovery until main owns the new selected digest.
  untrusted = bootstrap.Identity("sha256:" + "d" * 64, CURRENT_SHA)
  bootstrap.assert_prewrite_state(
    tag="main",
    main=untrusted,
    external=None,
    daily=None,
    current=CURRENT,
    previous=PREVIOUS,
  )
  with pytest.raises(bootstrap.StateError):
    bootstrap.assert_prewrite_state(
      tag="main",
      main=PREVIOUS,
      external=unexpected,
      daily=None,
      current=CURRENT,
      previous=PREVIOUS,
    )
  with pytest.raises(bootstrap.StateError):
    bootstrap.assert_prewrite_state(
      tag="external-recovery",
      main=PREVIOUS,
      external=None,
      daily=None,
      current=CURRENT,
      previous=PREVIOUS,
    )


def test_final_state_requires_both_exact_channels_and_no_daily():
  bootstrap.assert_final_state(
    main=CURRENT, external=CURRENT, daily=None, current=CURRENT
  )
  with pytest.raises(bootstrap.StateError):
    bootstrap.assert_final_state(
      main=CURRENT, external=None, daily=None, current=CURRENT
    )


def test_inspect_requires_reference_specific_manifest_absence():
  reference = "ghcr.io/mobius-os/mobius:daily"

  def absent_runner(*_args, **_kwargs):
    return subprocess.CompletedProcess(
      [], 1, "", f"ERROR: {reference}: not found"
    )

  assert bootstrap.inspect_tag(
    "ghcr.io/mobius-os/mobius",
    "daily",
    attempts=2,
    runner=absent_runner,
    sleeper=lambda _seconds: None,
  ) is None

  def ambiguous_runner(*_args, **_kwargs):
    return subprocess.CompletedProcess([], 1, "", "ERROR: docker builder not found")

  with pytest.raises(bootstrap.StateError, match="ambiguous registry failure"):
    bootstrap.inspect_tag(
      "ghcr.io/mobius-os/mobius",
      "daily",
      attempts=2,
      runner=ambiguous_runner,
      sleeper=lambda _seconds: None,
    )
