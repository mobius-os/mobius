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
