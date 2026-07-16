"""Fail-closed contracts for the shared-edge production deploy path."""

from pathlib import Path
import re
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-prod.sh"


def _gateway_validator() -> str:
  text = SCRIPT.read_text(encoding="utf-8")
  match = re.search(r"valid_gateway_origin\(\) \{.*?^\}", text, re.MULTILINE | re.DOTALL)
  assert match, "deploy script must keep gateway validation in one testable helper"
  return match.group(0)


@pytest.mark.parametrize("origin", [
  "https://services.example.test",
  "https://services.example.test:8443",
  "https://127.0.0.1:443",
])
def test_gateway_renderer_accepts_bare_https_origins(origin):
  result = subprocess.run(
    ["bash", "-c", f'{_gateway_validator()}\nvalid_gateway_origin "$1"', "_", origin],
    check=False,
  )
  assert result.returncode == 0


@pytest.mark.parametrize("origin", [
  "http://services.example.test",
  "https://services.example.test/path",
  "https://user@services.example.test",
  "https://services.example.test|d",
  "https://services.example.test:0",
  "https://services.example.test:65536",
])
def test_gateway_renderer_rejects_non_origins_and_render_metacharacters(origin):
  result = subprocess.run(
    ["bash", "-c", f'{_gateway_validator()}\nvalid_gateway_origin "$1"', "_", origin],
    check=False,
  )
  assert result.returncode != 0


def test_stopped_shared_edge_never_falls_back_to_bundled_caddy():
  text = SCRIPT.read_text(encoding="utf-8")
  assert "EDGE_CONTAINER_STATE" in text
  assert "{{.State.Status}}" in text
  assert "refusing to fall back to bundled Caddy" in text
  assert "Restore the shared edge proxy" in text


def test_unknown_csp_mode_fails_instead_of_silently_enforcing():
  text = SCRIPT.read_text(encoding="utf-8")
  assert "EDGE_CSP_MODE must be 'enforce' or 'report-only'." in text
