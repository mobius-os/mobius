"""Behavioral coverage for deterministic production deploy policy."""

import importlib.util
from pathlib import Path

import pytest


_PATH = Path(__file__).parents[2] / "scripts" / "deploy_support.py"
_SPEC = importlib.util.spec_from_file_location("deploy_support", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
deploy_support = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deploy_support)


@pytest.mark.parametrize(
  ("raw", "expected"),
  [
    ("https://Services.Example.test/", "https://services.example.test"),
    ("https://services.example.test:443", "https://services.example.test"),
    ("https://services.example.test:8443", "https://services.example.test:8443"),
  ],
)
def test_gateway_origin_is_canonicalized(raw, expected):
  assert deploy_support.normalize_gateway_origin(raw, "mobius.example.test") == expected


@pytest.mark.parametrize(
  "raw",
  [
    "http://services.example.test",
    "https://user@services.example.test",
    "https://services.example.test/path",
    "https://services.example.test?query=yes",
    "https://services.example.test#fragment",
    "https://services.example.test:70000",
    "https://mobius.example.test",
  ],
)
def test_gateway_origin_rejects_unsafe_or_same_origin_values(raw):
  with pytest.raises(deploy_support.DeployInputError):
    deploy_support.normalize_gateway_origin(raw, "mobius.example.test")


@pytest.mark.parametrize(
  ("image", "expected"),
  [
    ("mobius-app", "mobius-app:rollback-prev"),
    ("mobius-app:latest", "mobius-app:rollback-prev"),
    ("registry.test:5000/mobius/app:v2", "registry.test:5000/mobius/app:rollback-prev"),
    ("registry.test/mobius/app@sha256:abc", "registry.test/mobius/app:rollback-prev"),
  ],
)
def test_rollback_tag_preserves_registry_and_repository(image, expected):
  assert deploy_support.rollback_tag_for_image(image) == expected


def test_cli_validation_requires_an_already_canonical_origin(capsys):
  assert deploy_support.main([
    "validate-gateway-origin", "https://services.example.test",
  ]) == 0
  assert deploy_support.main([
    "validate-gateway-origin", "https://Services.example.test/",
  ]) == 1
  assert capsys.readouterr().out == ""


# --- Synthetic-turn oracle (#2) ---------------------------------------------


def test_synthetic_turn_still_running_is_pending():
  assert deploy_support.classify_synthetic_turn({"running": True}) == "pending"


def test_synthetic_turn_no_assistant_is_failed():
  # An empty assistant response is exactly the incident's non-responsive chat.
  chat = {"running": False, "messages": [{"role": "user", "content": "hi"}]}
  assert deploy_support.classify_synthetic_turn(chat) == "failed"


def test_synthetic_turn_error_or_resumable_block_is_failed():
  # The incident: admission-deferred turns emit an error/resumable block. The
  # old canary scored these as passing (any assistant block) — reject them.
  resumable = {
    "running": False,
    "messages": [{"role": "assistant", "content": "", "resumable": True,
                  "message": "waiting for storage headroom"}],
  }
  assert deploy_support.classify_synthetic_turn(resumable) == "failed"
  error_block = {
    "running": False,
    "messages": [{"role": "assistant", "blocks": [{"type": "error"}]}],
  }
  assert deploy_support.classify_synthetic_turn(error_block) == "failed"


def test_synthetic_turn_empty_text_is_failed():
  chat = {"running": False, "messages": [{"role": "assistant", "content": "  "}]}
  assert deploy_support.classify_synthetic_turn(chat) == "failed"


def test_synthetic_turn_non_text_block_content_is_not_a_reply():
  chat = {
    "running": False,
    "messages": [{
      "role": "assistant",
      "content": None,
      "blocks": [{"type": "tool_result", "content": [{"type": "image"}]}],
    }],
  }
  assert deploy_support.classify_synthetic_turn(chat) == "failed"


def test_synthetic_turn_real_reply_passes():
  by_content = {
    "running": False,
    "messages": [{"role": "assistant", "content": "OK"}],
  }
  assert deploy_support.classify_synthetic_turn(by_content) == "passed"
  by_block = {
    "running": False,
    "messages": [{"role": "assistant", "blocks": [{"type": "text", "text": "OK"}]}],
  }
  assert deploy_support.classify_synthetic_turn(by_block) == "passed"


def test_cli_classify_turn_failed_returns_nonzero(capsys):
  import json
  code = deploy_support.main([
    "classify-turn",
    json.dumps({"running": False, "messages": [
      {"role": "assistant", "resumable": True, "content": ""}]}),
  ])
  assert code == 2
  assert capsys.readouterr().out.strip() == "failed"


# --- Deployment receipt (#8) ------------------------------------------------


def test_receipt_requires_valid_outcome():
  with pytest.raises(deploy_support.DeployInputError):
    deploy_support.build_deploy_receipt({"outcome": "maybe"})


def test_failed_receipt_must_record_rollback_target():
  with pytest.raises(deploy_support.DeployInputError):
    deploy_support.build_deploy_receipt({"outcome": "failed"})
  ok = deploy_support.build_deploy_receipt({
    "outcome": "failed", "rollback_target": "mobius-app:rollback-prev",
  })
  assert ok["outcome"] == "failed"
  assert ok["rollback_target"] == "mobius-app:rollback-prev"


def test_receipt_is_canonical_and_drops_unknown_keys():
  receipt = deploy_support.build_deploy_receipt({
    "outcome": "success",
    "build_sha": "abc123",
    "rollback_target": "mobius-app:rollback-prev",
    "synthetic_turn": {"claude": {"result": "passed"}},
    "unexpected_field": "SHOULD-NOT-APPEAR",
  })
  assert receipt["schema_version"] == deploy_support.RECEIPT_SCHEMA_VERSION
  assert receipt["build_sha"] == "abc123"
  assert "unexpected_field" not in receipt
  # Every canonical field is present (as None when unset) for a stable shape.
  for field in deploy_support.RECEIPT_FIELDS:
    assert field in receipt
