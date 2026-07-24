"""Compact app discovery helper contract."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "list_apps.py"


def _load():
  spec = importlib.util.spec_from_file_location("list_apps_script", SCRIPT)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)
  return module


class _Response:
  def __init__(self, payload):
    self.payload = payload

  def __enter__(self):
    return self

  def __exit__(self, *_args):
    return False

  def read(self):
    return json.dumps(self.payload).encode()


def test_list_apps_prints_only_compact_identity_fields(
  monkeypatch, capsys,
):
  module = _load()
  monkeypatch.setattr(sys, "argv", ["list_apps.py"])
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setenv("API_BASE_URL", "http://mobius.test/")
  monkeypatch.setattr(
    module.urllib.request,
    "urlopen",
    lambda request, timeout: _Response([{
      "id": 7,
      "name": "Decision Spinner",
      "slug": "decision-spinner",
      "permissions": {"large": "payload"},
      "compiled_path": "/private/runtime/path",
    }]),
  )

  module.main()

  assert json.loads(capsys.readouterr().out) == [{
    "id": 7,
    "name": "Decision Spinner",
    "slug": "decision-spinner",
  }]


def test_list_apps_requires_agent_token(monkeypatch, capsys):
  module = _load()
  monkeypatch.setattr(sys, "argv", ["list_apps.py"])
  monkeypatch.delenv("AGENT_TOKEN", raising=False)

  with pytest.raises(SystemExit) as exc:
    module.main()

  assert exc.value.code == 1
  assert "AGENT_TOKEN" in capsys.readouterr().err
