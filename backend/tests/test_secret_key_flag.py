# backend/tests/test_secret_key_flag.py
"""Tests for the SECRET_KEY drift detection flag in /api/debug/status.

The entrypoint.sh bash logic is not directly testable from Python, but the
Python side — reading the flag file and surfacing it in /api/debug/status —
is independently testable. These tests cover that contract:

 1. When the flag file is absent, secret_key_changed is absent from the response.
 2. When the flag file exists with a timestamp, secret_key_changed is present
    with that timestamp.
 3. The field is absent on a clean boot (flag cleared by entrypoint on key match).
"""
import os
import pathlib

import pytest


def _flag_path():
  return pathlib.Path(os.environ.get("DATA_DIR", "/tmp")) / ".secret-key-changed"


def test_debug_status_no_flag(client, auth):
  """secret_key_changed is absent when the flag file does not exist."""
  flag = _flag_path()
  flag.unlink(missing_ok=True)
  r = client.get("/api/debug/status", headers=auth)
  assert r.status_code == 200
  assert "secret_key_changed" not in r.json()


def test_debug_status_with_flag(client, auth):
  """secret_key_changed is present (with the flag file content) when the file exists."""
  flag = _flag_path()
  flag.parent.mkdir(parents=True, exist_ok=True)
  flag.write_text("2026-06-10T12:00:00Z")
  try:
    r = client.get("/api/debug/status", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body.get("secret_key_changed") == "2026-06-10T12:00:00Z"
  finally:
    flag.unlink(missing_ok=True)


def test_debug_status_flag_unreadable_returns_unknown(client, auth, monkeypatch):
  """When the flag file exists but can't be read, the value is 'unknown'."""
  flag = _flag_path()
  flag.parent.mkdir(parents=True, exist_ok=True)
  flag.write_text("2026-06-10T12:00:00Z")
  # Monkeypatch pathlib.Path.read_text at the class level to simulate an
  # unreadable file. We restore it via a try/finally since monkeypatch
  # setattr on PosixPath is read-only on some Pythons.
  import app.routes.debug as debug_mod
  import pathlib
  _orig = pathlib.Path.read_text
  def _fail_read_text(self, *a, **kw):
    if self == debug_mod._SECRET_KEY_CHANGED_FLAG:
      raise OSError("permission denied")
    return _orig(self, *a, **kw)
  monkeypatch.setattr(pathlib.Path, "read_text", _fail_read_text)
  try:
    r = client.get("/api/debug/status", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body.get("secret_key_changed") == "unknown"
  finally:
    flag.unlink(missing_ok=True)


def test_debug_status_requires_auth(client):
  """The debug/status endpoint is not public."""
  r = client.get("/api/debug/status")
  assert r.status_code == 401
