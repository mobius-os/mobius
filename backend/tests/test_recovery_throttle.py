"""Tests for the recovery-auth resource bounds (feature 263).

The frozen recovery floor (recoveryd) is public and DB-independent, so its
bcrypt login path must not be a cheap remote-DoS lever. These exercise the
three bounds added to `_handle_auth`: a per-client fixed-window throttle, a
global bcrypt semaphore, and the client-IP trust boundary that decides when an
`X-Forwarded-For` header may be trusted.

Like the rest of the recoveryd suite these load the frozen bundle directly off
`backend/recovery/` (it imports ZERO `app.*`) and call the handler functions
without standing up a socket server.
"""

import importlib
import sqlite3
import sys
import threading
from http import HTTPStatus
from pathlib import Path

import bcrypt
import pytest

# The frozen bundle ships at backend/recovery/. Put it on sys.path so the
# stdlib-only modules import the same way they do inside /app/recovery.
_RECOVERY_DIR = Path(__file__).resolve().parents[1] / "recovery"
if str(_RECOVERY_DIR) not in sys.path:
  sys.path.insert(0, str(_RECOVERY_DIR))


@pytest.fixture()
def recovery_env(monkeypatch, tmp_path):
  """Isolated DATA_DIR + DB with the recovery modules freshly imported."""
  data_dir = tmp_path / "data"
  data_dir.mkdir()
  (data_dir / "db").mkdir()
  db_path = data_dir / "db" / "ultimate.db"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  monkeypatch.setenv("RECOVERY_LIVE_ROOT", str(tmp_path / "recovery-live"))
  monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
  monkeypatch.setenv("RECOVERY_SKIP_INTEGRITY", "1")
  for mod in ("recovery_auth", "recovery_db", "recovery_pages", "recoveryd"):
    sys.modules.pop(mod, None)
  recovery_auth = importlib.import_module("recovery_auth")
  recovery_db = importlib.import_module("recovery_db")
  recovery_pages = importlib.import_module("recovery_pages")
  recoveryd = importlib.import_module("recoveryd")
  return {
    "data_dir": data_dir,
    "db_path": db_path,
    "auth": recovery_auth,
    "db": recovery_db,
    "pages": recovery_pages,
    "recoveryd": recoveryd,
  }


def _create_owner(db_path: Path, username: str, password: str) -> None:
  con = sqlite3.connect(str(db_path))
  con.execute(
    "CREATE TABLE IF NOT EXISTS owner "
    "(id INTEGER PRIMARY KEY, username TEXT, hashed_password TEXT)"
  )
  pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
  con.execute(
    "INSERT INTO owner (username, hashed_password) VALUES (?, ?)",
    (username, pw_hash),
  )
  con.commit()
  con.close()


def _auth_handler(recoveryd, *, peer=("172.18.0.9", 5000), xff=None):
  """Builds a bare _Handler (no __init__) whose _send captures the response."""
  handler = recoveryd._Handler.__new__(recoveryd._Handler)
  handler.client_address = peer
  headers = {}
  if xff is not None:
    headers["X-Forwarded-For"] = xff
  handler.headers = headers
  captured: dict = {}

  def _send(code, body="", *, content_type="text/html", extra_headers=None):
    captured["code"] = int(code)
    captured["body"] = body
    captured["headers"] = extra_headers or {}

  handler._send = _send
  return handler, captured


# -- client-IP trust boundary (_resolve_client_key) -------------------------

def test_resolve_client_key_trusts_forwarded_from_private_peer(recovery_env):
  """A private peer (our own proxy) means the forwarded client IP is trusted."""
  recoveryd = recovery_env["recoveryd"]
  key = recoveryd._resolve_client_key("172.18.0.9", "203.0.113.7")
  assert key == "203.0.113.7"


def test_resolve_client_key_takes_rightmost_forwarded_token(recovery_env):
  """The rightmost XFF element is the one our proxy appended — a client-spoofed
  prefix sits to its LEFT and must NOT become the throttle key."""
  recoveryd = recovery_env["recoveryd"]
  # Attacker sends `X-Forwarded-For: 1.1.1.1` hoping for a fresh bucket; Caddy
  # appends the real observed client (203.0.113.7) as the last hop.
  key = recoveryd._resolve_client_key(
    "10.0.0.2", "1.1.1.1, 9.9.9.9, 203.0.113.7")
  assert key == "203.0.113.7"


def test_resolve_client_key_ignores_forwarded_from_public_peer(recovery_env):
  """A NON-private direct peer did not pass through our proxy, so its XFF is
  untrusted and we key on the peer itself — over-throttle, never a bypass."""
  recoveryd = recovery_env["recoveryd"]
  key = recoveryd._resolve_client_key("8.8.8.8", "1.2.3.4")
  assert key == "8.8.8.8"


def test_resolve_client_key_falls_back_to_peer_without_forwarded(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  rck = recoveryd._resolve_client_key
  assert rck("172.18.0.9", None) == "172.18.0.9"
  assert rck("172.18.0.9", "") == "172.18.0.9"
  # A whitespace-only / empty last token falls back to the peer, never "".
  assert rck("172.18.0.9", "10.0.0.1, ") == "172.18.0.9"


def test_resolve_client_key_trusts_loopback_peer(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  assert recoveryd._resolve_client_key("127.0.0.1", "198.51.100.9") == (
    "198.51.100.9")


def test_resolve_client_key_unparseable_peer_ignores_forwarded(recovery_env):
  """A garbage peer address can't be classified as private, so XFF is not
  trusted — the key falls back to the raw peer string."""
  recoveryd = recovery_env["recoveryd"]
  key = recoveryd._resolve_client_key("not-an-ip", "203.0.113.7")
  assert key == "not-an-ip"


# -- fixed-window throttle --------------------------------------------------

def test_throttle_allows_up_to_limit_then_blocks(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=3, window=60, max_keys=16)
  # Three attempts inside one window are allowed; the fourth is blocked.
  assert t.allow("k", now=100.0) is True
  assert t.allow("k", now=100.1) is True
  assert t.allow("k", now=100.2) is True
  assert t.allow("k", now=100.3) is False
  assert t.allow("k", now=159.9) is False  # still inside the 60s window


def test_throttle_resets_after_window(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=2, window=60, max_keys=16)
  assert t.allow("k", now=100.0) is True
  assert t.allow("k", now=100.1) is True
  assert t.allow("k", now=120.0) is False
  # Once the window elapses a fresh window opens and attempts flow again.
  assert t.allow("k", now=160.0) is True


def test_throttle_keys_are_independent(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=1, window=60, max_keys=16)
  assert t.allow("a", now=100.0) is True
  assert t.allow("a", now=100.1) is False
  # A different client key has its own budget.
  assert t.allow("b", now=100.2) is True


def test_throttle_bounded_size_evicts_oldest(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=5, window=60, max_keys=4)
  for i in range(20):
    t.allow(f"key-{i}", now=100.0 + i)
  # The dict never grows past max_keys, so a flood of distinct keys can't
  # exhaust memory.
  assert len(t._hits) <= 4
  # The most recent keys survive; the oldest were evicted.
  assert "key-19" in t._hits
  assert "key-0" not in t._hits


# -- _handle_auth integration ------------------------------------------------

def test_auth_success_still_sets_cookie(recovery_env, monkeypatch):
  """Regression: the throttle + semaphore must not break a correct login."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  # Keep the success page render off the network (build_status probes health).
  monkeypatch.setattr(recoveryd, "build_status", lambda: {})
  monkeypatch.setattr(
    recovery_env["pages"], "dashboard_html", lambda *a, **k: "<ok>")
  handler, captured = _auth_handler(recoveryd)
  handler._handle_auth({"username": "admin", "password": "hunter2"})
  assert captured["code"] == int(HTTPStatus.OK)
  cookie = captured["headers"].get("Set-Cookie", "")
  assert cookie.startswith("moebius_recover=")
  assert "HttpOnly" in cookie


def test_auth_wrong_password_returns_login_error(recovery_env, monkeypatch):
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  handler, captured = _auth_handler(recoveryd)
  handler._handle_auth({"username": "admin", "password": "wrong"})
  assert captured["code"] == int(HTTPStatus.OK)
  assert "Set-Cookie" not in captured["headers"]
  assert "Incorrect" in captured["body"]


def test_auth_throttle_returns_429_after_limit(recovery_env, monkeypatch):
  """After `limit` attempts from one client, the next is rejected with 429
  BEFORE any bcrypt runs."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  monkeypatch.setattr(
    recoveryd, "_AUTH_THROTTLE",
    recoveryd._FixedWindowThrottle(limit=3, window=60, max_keys=16))
  # Three throttle-permitted attempts (all wrong-password 200s from one peer).
  for _ in range(3):
    handler, captured = _auth_handler(recoveryd, peer=("172.18.0.9", 5000))
    handler._handle_auth({"username": "admin", "password": "wrong"})
    assert captured["code"] == int(HTTPStatus.OK)
  # The fourth from the same client is throttled.
  handler, captured = _auth_handler(recoveryd, peer=("172.18.0.9", 5000))
  handler._handle_auth({"username": "admin", "password": "hunter2"})
  assert captured["code"] == int(HTTPStatus.TOO_MANY_REQUESTS)
  assert "Retry-After" in captured["headers"]


def test_auth_throttle_is_per_client(recovery_env, monkeypatch):
  """A throttled client must not throttle a different real client. The key is
  the forwarded IP, so two distinct forwarded IPs get independent budgets even
  though they share the same proxy peer."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  monkeypatch.setattr(
    recoveryd, "_AUTH_THROTTLE",
    recoveryd._FixedWindowThrottle(limit=1, window=60, max_keys=16))
  # Client A (behind the proxy) burns its single attempt and is then blocked.
  for expected in (HTTPStatus.OK, HTTPStatus.TOO_MANY_REQUESTS):
    handler, captured = _auth_handler(
      recoveryd, peer=("172.18.0.9", 5000), xff="203.0.113.10")
    handler._handle_auth({"username": "admin", "password": "wrong"})
    assert captured["code"] == int(expected)
  # Client B, same proxy peer but a different forwarded IP, is unaffected.
  handler, captured = _auth_handler(
    recoveryd, peer=("172.18.0.9", 5000), xff="203.0.113.20")
  handler._handle_auth({"username": "admin", "password": "wrong"})
  assert captured["code"] == int(HTTPStatus.OK)


def test_auth_bcrypt_gate_returns_503_when_saturated(recovery_env, monkeypatch):
  """When every bcrypt slot is taken, a new attempt returns 503 promptly
  instead of parking a thread on the verify."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  monkeypatch.setattr(recoveryd, "_BCRYPT_WAIT_SECS", 0.05)
  gate = threading.BoundedSemaphore(1)
  monkeypatch.setattr(recoveryd, "_BCRYPT_GATE", gate)
  # Simulate the single slot being held by another in-flight verify.
  assert gate.acquire(timeout=1) is True
  try:
    handler, captured = _auth_handler(recoveryd)
    handler._handle_auth({"username": "admin", "password": "hunter2"})
  finally:
    gate.release()
  assert captured["code"] == int(HTTPStatus.SERVICE_UNAVAILABLE)
  assert captured["headers"].get("Retry-After") == "2"
