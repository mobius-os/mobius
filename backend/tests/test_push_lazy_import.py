"""The Web Push delivery stack stays outside ordinary backend startup."""

import json
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import push


BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module_name", ["app.push", "app.main"])
def test_backend_import_does_not_load_pywebpush(module_name: str):
  code = (
    "import sys\n"
    "assert 'pywebpush' not in sys.modules\n"
    f"__import__({module_name!r})\n"
    "assert 'pywebpush' not in sys.modules\n"
  )
  env = os.environ.copy()
  existing_pythonpath = env.get("PYTHONPATH")
  env["PYTHONPATH"] = (
    str(BACKEND_ROOT)
    if not existing_pythonpath
    else f"{BACKEND_ROOT}{os.pathsep}{existing_pythonpath}"
  )

  result = subprocess.run(
    [sys.executable, "-c", code],
    cwd=BACKEND_ROOT,
    env=env,
    capture_output=True,
    text=True,
    timeout=20,
    check=False,
  )

  assert result.returncode == 0, result.stderr


class FakeWebPushException(Exception):
  def __init__(self, message: str, *, response=None):
    super().__init__(message)
    self.response = response


def install_fake_pywebpush(monkeypatch, webpush):
  fake_module = types.ModuleType("pywebpush")
  fake_module.webpush = webpush
  fake_module.WebPushException = FakeWebPushException
  monkeypatch.setitem(sys.modules, "pywebpush", fake_module)


def test_send_push_loads_delivery_dependency_and_preserves_arguments(monkeypatch):
  calls = []
  fake_vapid = object()
  claims = {"sub": "mailto:test@example.com"}
  subscription = {"endpoint": "https://push.example/subscription"}
  payload = {"title": "Ready", "body": "The job finished."}

  def fake_webpush(**kwargs):
    calls.append(kwargs)

  install_fake_pywebpush(monkeypatch, fake_webpush)
  monkeypatch.setattr(push, "_vapid", fake_vapid)
  monkeypatch.setattr(push, "get_vapid_claims", lambda: claims)

  assert push.send_push(subscription, payload) is True
  assert calls == [
    {
      "subscription_info": subscription,
      "data": json.dumps(payload),
      "vapid_private_key": fake_vapid,
      "vapid_claims": claims,
      "content_encoding": "aes128gcm",
    }
  ]


def test_send_push_returns_false_when_subscription_is_gone(monkeypatch):
  error = FakeWebPushException(
    "subscription is gone",
    response=SimpleNamespace(status_code=410),
  )

  def fake_webpush(**_kwargs):
    raise error

  install_fake_pywebpush(monkeypatch, fake_webpush)
  monkeypatch.setattr(push, "_vapid", object())

  assert push.send_push({}, {}) is False


def test_send_push_reraises_other_delivery_failures(monkeypatch):
  error = FakeWebPushException(
    "delivery failed",
    response=SimpleNamespace(status_code=503),
  )

  def fake_webpush(**_kwargs):
    raise error

  install_fake_pywebpush(monkeypatch, fake_webpush)
  monkeypatch.setattr(push, "_vapid", object())

  with pytest.raises(FakeWebPushException) as raised:
    push.send_push({}, {})

  assert raised.value is error


def test_uninitialized_vapid_fails_before_loading_delivery_dependency(monkeypatch):
  monkeypatch.setattr(push, "_vapid", None)
  monkeypatch.delitem(sys.modules, "pywebpush", raising=False)

  with pytest.raises(RuntimeError, match="VAPID not initialized"):
    push.send_push({}, {})

  assert "pywebpush" not in sys.modules
