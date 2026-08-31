"""Compact apply receipt contract for the agent-facing helper."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "apply_app.py"


def _load():
  spec = importlib.util.spec_from_file_location("apply_app_script", SCRIPT)
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


def test_apply_prints_compact_reusable_identity_receipt(
  monkeypatch, capsys, tmp_path,
):
  module = _load()
  source = tmp_path / "same-name"
  source.mkdir()
  monkeypatch.setattr(sys, "argv", ["apply_app.py", str(source)])
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setenv("CHAT_ID", "building-chat")
  monkeypatch.setattr(
    module.urllib.request,
    "urlopen",
    lambda request, timeout: _Response({
      "mode": "created",
      "warnings": ["skill guide.md: left unchanged"],
      "app": {
        "id": 73,
        "name": "Same name",
        "slug": "same-name-2",
        "source_dir": str(source.resolve()),
        "chat_id": "building-chat",
        "capability_contract": {"large": "payload"},
        "compiled_path": "/private/runtime/path",
      },
    }),
  )

  module.main()

  assert json.loads(capsys.readouterr().out) == {
    "mode": "created",
    "app_id": 73,
    "name": "Same name",
    "slug": "same-name-2",
    "source_dir": str(source.resolve()),
    "chat_id": "building-chat",
    "preview_path": "/app/73",
    "open_path": "/shell/?app=73",
    "warnings": ["skill guide.md: left unchanged"],
  }


def test_apply_forwards_explicit_local_package_acceptance(
  monkeypatch, capsys, tmp_path,
):
  module = _load()
  source = tmp_path / "store-app"
  source.mkdir()
  monkeypatch.setattr(sys, "argv", [
    "apply_app.py", "--accept-local-package", str(source),
  ])
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  captured = {}

  def urlopen(request, timeout):
    captured["payload"] = json.loads(request.data)
    return _Response({
      "mode": "updated", "warnings": [],
      "app": {
        "id": 9, "name": "Store app", "slug": "store-app",
        "source_dir": str(source.resolve()), "chat_id": None,
      },
    })

  monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)

  module.main()

  assert captured["payload"]["accept_local_package"] is True
  assert json.loads(capsys.readouterr().out)["mode"] == "updated"


def test_apply_fails_loudly_when_response_loses_numeric_identity(
  monkeypatch, capsys, tmp_path,
):
  module = _load()
  source = tmp_path / "bad"
  source.mkdir()
  monkeypatch.setattr(sys, "argv", ["apply_app.py", str(source)])
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setattr(
    module.urllib.request,
    "urlopen",
    lambda request, timeout: _Response({
      "mode": "created", "app": {"name": "No id"},
    }),
  )

  with pytest.raises(SystemExit) as exc:
    module.main()

  assert exc.value.code == 1
  assert "numeric app id" in capsys.readouterr().err
