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
      "source_dir": "/data/apps/decision-spinner",
      "chat_id": "chat-7",
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


def test_source_dir_is_available_only_when_explicitly_requested(
  monkeypatch, capsys,
):
  module = _load()
  monkeypatch.setattr(
    sys,
    "argv",
    ["list_apps.py", "--slug", "decision-spinner", "--with-source-dir"],
  )
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setenv("API_BASE_URL", "http://mobius.test/")
  monkeypatch.setattr(
    module.urllib.request,
    "urlopen",
    lambda request, timeout: _Response([{
      "id": 7,
      "name": "Decision Spinner",
      "slug": "decision-spinner",
      "source_dir": "/data/apps/decision-spinner",
      "permissions": {"large": "payload"},
    }]),
  )

  module.main()

  assert json.loads(capsys.readouterr().out) == [{
    "id": 7,
    "name": "Decision Spinner",
    "slug": "decision-spinner",
    "source_dir": "/data/apps/decision-spinner",
  }]


def test_exact_name_filter_returns_every_duplicate_name(
  monkeypatch, capsys,
):
  module = _load()
  monkeypatch.setattr(
    sys, "argv", ["list_apps.py", "--name", "Shared name"],
  )
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setattr(
    module.urllib.request,
    "urlopen",
    lambda request, timeout: _Response([
      {
        "id": 2, "name": "Shared name", "slug": "first",
        "source_dir": "/data/apps/first", "chat_id": "chat-a",
      },
      {
        "id": 9, "name": "Shared name", "slug": "second",
        "source_dir": "/data/apps/second", "chat_id": "chat-b",
      },
      {
        "id": 10, "name": "Other", "slug": "other",
        "source_dir": "/data/apps/other", "chat_id": "chat-a",
      },
    ]),
  )

  module.main()

  result = json.loads(capsys.readouterr().out)
  assert [app["id"] for app in result] == [2, 9]


@pytest.mark.parametrize(
  ("argv", "expected_id"),
  [
    (["--id", "9"], 9),
    (["--slug", "second"], 9),
    (["--source-dir", "/data/apps/second"], 9),
  ],
)
def test_unique_identity_filters_narrow_without_name_resolution(
  monkeypatch, capsys, argv, expected_id,
):
  module = _load()
  monkeypatch.setattr(sys, "argv", ["list_apps.py", *argv])
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setattr(
    module.urllib.request,
    "urlopen",
    lambda request, timeout: _Response([
      {
        "id": 2, "name": "Same", "slug": "first",
        "source_dir": "/data/apps/first", "chat_id": "chat-a",
      },
      {
        "id": 9, "name": "Same", "slug": "second",
        "source_dir": "/data/apps/second", "chat_id": "chat-b",
      },
    ]),
  )

  module.main()

  result = json.loads(capsys.readouterr().out)
  assert [app["id"] for app in result] == [expected_id]


def test_chat_filter_preserves_all_apps_owned_by_chat(monkeypatch, capsys):
  module = _load()
  monkeypatch.setattr(
    sys, "argv", ["list_apps.py", "--chat-id", "chat-a"],
  )
  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setattr(
    module.urllib.request,
    "urlopen",
    lambda request, timeout: _Response([
      {
        "id": 2, "name": "One", "slug": "one",
        "source_dir": "/data/apps/one", "chat_id": "chat-a",
      },
      {
        "id": 3, "name": "Two", "slug": "two",
        "source_dir": "/data/apps/two", "chat_id": "chat-a",
      },
      {
        "id": 4, "name": "Three", "slug": "three",
        "source_dir": "/data/apps/three", "chat_id": "chat-b",
      },
    ]),
  )

  module.main()

  result = json.loads(capsys.readouterr().out)
  assert [app["id"] for app in result] == [2, 3]


def test_list_apps_requires_agent_token(monkeypatch, capsys):
  module = _load()
  monkeypatch.setattr(sys, "argv", ["list_apps.py"])
  monkeypatch.delenv("AGENT_TOKEN", raising=False)

  with pytest.raises(SystemExit) as exc:
    module.main()

  assert exc.value.code == 1
  assert "AGENT_TOKEN" in capsys.readouterr().err
