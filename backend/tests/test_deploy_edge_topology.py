"""Fail-closed contracts for the shared-edge production deploy path."""

from pathlib import Path
import importlib.util

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-prod.sh"
SUPPORT = SCRIPT.with_name("deploy_support.py")
_SPEC = importlib.util.spec_from_file_location("deploy_support_edge", SUPPORT)
assert _SPEC is not None and _SPEC.loader is not None
deploy_support = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deploy_support)

@pytest.mark.parametrize("origin", [
  "https://services.example.test",
  "https://services.example.test:8443",
  "https://127.0.0.1:443",
])
def test_gateway_renderer_accepts_bare_https_origins(origin):
  assert deploy_support.normalize_gateway_origin(origin) == (
    origin.removesuffix(":443")
  )


@pytest.mark.parametrize("origin", [
  "http://services.example.test",
  "https://services.example.test/path",
  "https://user@services.example.test",
  "https://services.example.test|d",
  "https://services.example.test:0",
  "https://services.example.test:65536",
])
def test_gateway_renderer_rejects_non_origins_and_render_metacharacters(origin):
  with pytest.raises(deploy_support.DeployInputError):
    deploy_support.normalize_gateway_origin(origin)


def test_stopped_shared_edge_never_falls_back_to_bundled_caddy():
  text = SCRIPT.read_text(encoding="utf-8")
  assert "EDGE_CONTAINER_STATE" in text
  assert "{{.State.Status}}" in text
  assert "refusing to fall back to bundled Caddy" in text
  assert "Restore the shared edge proxy" in text
