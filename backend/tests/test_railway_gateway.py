import importlib.util
from pathlib import Path

import pytest


_GATEWAY_PATH = (
  Path(__file__).resolve().parents[1] / "scripts" / "railway_gateway.py"
)


def _load_gateway():
  spec = importlib.util.spec_from_file_location("railway_gateway", _GATEWAY_PATH)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)
  return module


def test_recover_paths_route_to_recoveryd():
  gateway = _load_gateway()
  assert gateway.is_recovery_path("/recover") is True
  assert gateway.is_recovery_path("/recover/") is True
  assert gateway.is_recovery_path("/recover/chat") is True
  assert gateway.is_recovery_path("/api/health") is False
  assert gateway.is_recovery_path("/recovering") is False


def test_parse_upstream_accepts_http_urls_and_host_ports():
  gateway = _load_gateway()
  assert gateway.parse_upstream("http://127.0.0.1:18000") == (
    "127.0.0.1", 18000)
  assert gateway.parse_upstream("localhost:18001") == ("localhost", 18001)
  assert gateway.parse_upstream("http://mobius.internal") == (
    "mobius.internal", 80)


def test_parse_upstream_rejects_non_http_urls():
  gateway = _load_gateway()
  with pytest.raises(ValueError):
    gateway.parse_upstream("https://example.test")


def test_gateway_has_no_app_imports():
  src = _GATEWAY_PATH.read_text()
  for line in src.splitlines():
    stripped = line.strip()
    assert not stripped.startswith("import app"), stripped
    assert not stripped.startswith("from app "), stripped
    assert not stripped.startswith("from app."), stripped
