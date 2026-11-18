"""A rejected secret card says why, and the tool schema promises only what passes."""

import importlib.util
from pathlib import Path

import pytest

from app import secure_inputs

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(filename: str, name: str):
  spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.fixture
def helper(monkeypatch):
  monkeypatch.setenv("API_BASE_URL", "http://local.test")
  monkeypatch.setenv("AGENT_TOKEN", "test-token")
  monkeypatch.setenv("CHAT_ID", "chat-1")
  return _load("secure-input.py", "secure_input_clarity")


def _request(helper):
  spec = {"title": "t", "fields": [{"name": "k", "type": "password", "label": "Key"}]}
  return helper._request_saved(spec, ["consumer"], "run")


def test_rejected_card_surfaces_the_servers_reason(helper, monkeypatch):
  detail = "Secure input title must be 1–80 characters."
  monkeypatch.setattr(helper, "_post", lambda *a, **k: (400, {"detail": detail}))
  with pytest.raises(RuntimeError) as error:
    _request(helper)
  assert detail in str(error.value)
  assert "no values were requested" in str(error.value)
  assert "Retry the identical request" not in str(error.value)


def test_server_fault_keeps_the_retry_advice(helper, monkeypatch):
  monkeypatch.setattr(helper, "_post", lambda *a, **k: (503, {}))
  with pytest.raises(RuntimeError, match="Retry the identical request"):
    _request(helper)


def test_request_secret_schema_limits_match_the_servers_validator():
  control = _load("mobius_control_mcp.py", "mobius_control_clarity")
  properties = control._TOOL_DEFINITIONS["request_secret"]["inputSchema"]["properties"]
  label = properties["fields"]["items"]["properties"]["label"]
  field = {"name": "k", "type": "password", "label": "x" * label["maxLength"]}
  title_max = properties["title"]["maxLength"]
  description_max = properties["description"]["maxLength"]

  secure_inputs.validate_request_spec(
    title="t" * title_max, description="d" * description_max, mode="sealed", fields=[field],
  )
  with pytest.raises(ValueError):
    secure_inputs.validate_request_spec(
      title="t" * (title_max + 1), description="", mode="sealed", fields=[field],
    )
  with pytest.raises(ValueError):
    secure_inputs.validate_request_spec(
      title="t", description="d" * (description_max + 1), mode="sealed", fields=[field],
    )
  with pytest.raises(ValueError):
    secure_inputs.validate_request_spec(
      title="t", description="", mode="sealed",
      fields=[{**field, "label": "x" * (label["maxLength"] + 1)}],
    )
