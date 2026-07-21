"""Tests for the recovery-auth resource bounds (feature 263).

The frozen recovery floor (recoveryd) is public and DB-independent, so its
bcrypt login path must not be a cheap remote-DoS lever. These exercise the
bounds added to the /recover/auth admission path — a per-client fixed-window
throttle, a non-blocking cap on concurrent auth handlers, a global bcrypt
semaphore, fail-safe env parsing, and the client-IP trust boundary that decides
when an X-Forwarded-For header may be trusted.

Like the rest of the recoveryd suite these load the frozen bundle directly off
`backend/recovery/` (it imports ZERO `app.*`) and call the handler functions
without standing up a socket server.
"""

import email.message
import importlib
import math
import sqlite3
import sys
import threading
import time
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


def _auth_handler(recoveryd, *, peer=("172.18.0.9", 5000), xff=None, form=None):
  """Builds a bare _Handler (no __init__) whose _send captures the response.

  `_read_form` and `_reject_cross_site` are stubbed so _route_auth_post runs
  without a socket; tests that assert ordering re-stub _read_form as a spy.
  """
  handler = recoveryd._Handler.__new__(recoveryd._Handler)
  handler.client_address = peer
  headers = {}
  if xff is not None:
    headers["X-Forwarded-For"] = xff
  handler.headers = headers
  handler.close_connection = False
  captured: dict = {}

  def _send(code, body="", *, content_type="text/html", extra_headers=None):
    captured["code"] = int(code)
    captured["body"] = body
    captured["headers"] = extra_headers or {}

  handler._send = _send
  handler._read_form = lambda: dict(form or {})
  handler._reject_cross_site = lambda: False
  return handler, captured


# -- client-IP trust boundary (_resolve_client_key) -------------------------

def test_resolve_client_key_trusts_single_forwarded_from_private_peer(
    recovery_env):
  recoveryd = recovery_env["recoveryd"]
  key = recoveryd._resolve_client_key("172.18.0.9", ["203.0.113.7"])
  assert key == "203.0.113.7"


def test_resolve_client_key_canonicalizes_ipv6(recovery_env):
  """Equivalent IPv6 spellings must collapse onto ONE bucket, so an attacker
  can't rotate spellings of one address into distinct keys."""
  recoveryd = recovery_env["recoveryd"]
  a = recoveryd._resolve_client_key("172.18.0.9", ["2001:db8::1"])
  b = recoveryd._resolve_client_key(
    "172.18.0.9", ["2001:0db8:0000:0000:0000:0000:0000:0001"])
  assert a == b == "2001:db8::1"


def test_resolve_client_key_rejects_ported_or_bracketed_token(recovery_env):
  """A port suffix or brackets is not a bare IP, so it must NOT become a raw
  key — fall back to the proxy peer instead."""
  recoveryd = recovery_env["recoveryd"]
  assert recoveryd._resolve_client_key(
    "172.18.0.9", ["203.0.113.7:8080"]) == "172.18.0.9"
  assert recoveryd._resolve_client_key(
    "172.18.0.9", ["[2001:db8::1]"]) == "172.18.0.9"


def test_resolve_client_key_rejects_unicode_or_garbage_token(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  assert recoveryd._resolve_client_key("172.18.0.9", ["ⓧ"]) == "172.18.0.9"
  assert recoveryd._resolve_client_key(
    "172.18.0.9", ["x" * 500]) == "172.18.0.9"


def test_resolve_client_key_rejects_multitoken_forwarded(recovery_env):
  """A comma means an unexpected extra proxy hop under our single-hop Caddy
  contract; fall back to the peer rather than guess which token is the
  client."""
  recoveryd = recovery_env["recoveryd"]
  assert recoveryd._resolve_client_key(
    "10.0.0.2", ["1.1.1.1, 203.0.113.7"]) == "10.0.0.2"


def test_resolve_client_key_rejects_duplicate_headers(recovery_env):
  """Two X-Forwarded-For headers are ambiguous -> key on the peer."""
  recoveryd = recovery_env["recoveryd"]
  assert recoveryd._resolve_client_key(
    "172.18.0.9", ["203.0.113.7", "9.9.9.9"]) == "172.18.0.9"


def test_resolve_client_key_ignores_forwarded_from_public_peer(recovery_env):
  """A NON-private direct peer did not pass through our proxy, so its XFF is
  untrusted and we key on the peer itself — over-throttle, never a bypass.

  8.8.8.8 is genuinely globally-routable; the TEST-NET documentation ranges
  read as is_private=True under Python 3.12, so a real public address is
  required to exercise the untrusted branch."""
  recoveryd = recovery_env["recoveryd"]
  assert recoveryd._resolve_client_key("8.8.8.8", ["1.2.3.4"]) == "8.8.8.8"


def test_resolve_client_key_falls_back_to_peer_without_forwarded(recovery_env):
  rck = recovery_env["recoveryd"]._resolve_client_key
  assert rck("172.18.0.9", None) == "172.18.0.9"
  assert rck("172.18.0.9", []) == "172.18.0.9"


def test_resolve_client_key_trusts_loopback_peer(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  assert recoveryd._resolve_client_key(
    "127.0.0.1", ["198.51.100.9"]) == "198.51.100.9"


def test_resolve_client_key_unparseable_peer_ignores_forwarded(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  key = recoveryd._resolve_client_key("not-an-ip", ["203.0.113.7"])
  assert key == "not-an-ip"


def test_client_key_reads_duplicate_headers_via_get_all(recovery_env):
  """The handler must see BOTH X-Forwarded-For headers (get_all), so a duplicate
  header is treated as ambiguous rather than silently reduced to one."""
  recoveryd = recovery_env["recoveryd"]
  headers = email.message.Message()
  headers["X-Forwarded-For"] = "203.0.113.7"
  headers["X-Forwarded-For"] = "9.9.9.9"
  handler = recoveryd._Handler.__new__(recoveryd._Handler)
  handler.client_address = ("172.18.0.9", 5000)
  handler.headers = headers
  assert handler._client_key() == "172.18.0.9"


# -- fixed-window throttle --------------------------------------------------

def test_throttle_allows_up_to_limit_then_blocks(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=3, window=60, max_keys=16)
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
  assert t.allow("k", now=160.0) is True  # window elapsed -> fresh allowance


def test_throttle_keys_are_independent(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=1, window=60, max_keys=16)
  assert t.allow("a", now=100.0) is True
  assert t.allow("a", now=100.1) is False
  assert t.allow("b", now=100.2) is True


def test_throttle_bounded_size_evicts_oldest_unblocked(recovery_env):
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=5, window=60, max_keys=4)
  for i in range(20):
    t.allow(f"key-{i}", now=100.0 + i)
  # A flood of distinct under-limit keys can't grow the table past max_keys;
  # the oldest are evicted, the most recent survive.
  assert len(t._hits) <= 4
  assert "key-19" in t._hits
  assert "key-0" not in t._hits


def test_throttle_over_limit_key_survives_eviction_pressure(recovery_env):
  """A currently-blocked key must NOT be evicted+reset by other keys churning
  through a full table (the finding-3 rate-reset bug)."""
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=2, window=60, max_keys=3)
  assert t.allow("victim", now=100.0) is True
  assert t.allow("victim", now=100.1) is True   # victim now at its limit
  assert t.allow("victim", now=100.2) is False  # blocked
  # Churn under-limit keys to force eviction; the blocked victim is skipped.
  assert t.allow("b", now=100.3) is True
  assert t.allow("c", now=100.4) is True        # table now full
  assert t.allow("d", now=100.5) is True        # evicts oldest UNBLOCKED (b)
  assert t.allow("victim", now=100.6) is False  # STILL blocked (survived)
  assert t.allow("b", now=100.7) is True        # b was the evicted one


def test_throttle_blocks_newcomer_when_all_keys_blocked(recovery_env):
  """When the table is full of actively-blocked keys, a new identity is thrown
  (fail-safe) rather than displacing a blocked key — and an expired window frees
  room again."""
  recoveryd = recovery_env["recoveryd"]
  t = recoveryd._FixedWindowThrottle(limit=1, window=60, max_keys=2)
  assert t.allow("a", now=100.0) is True
  assert t.allow("a", now=100.1) is False       # a blocked
  assert t.allow("b", now=100.2) is True
  assert t.allow("b", now=100.3) is False       # b blocked; table full+blocked
  assert t.allow("c", now=100.4) is False       # newcomer thrown, not admitted
  assert t.allow("a", now=100.5) is False       # a still tracked+blocked
  assert t.allow("c", now=200.0) is True        # a/b windows expired -> room


# -- fail-safe env parsing --------------------------------------------------

def test_env_int_min_fail_safe(recovery_env, monkeypatch):
  f = recovery_env["recoveryd"]._env_int_min
  monkeypatch.delenv("RECOVERY_X", raising=False)
  assert f("RECOVERY_X", 10, 1) == 10          # unset -> default
  for bad in ("", "  ", "abc", "0", "-5", "nan"):
    monkeypatch.setenv("RECOVERY_X", bad)
    assert f("RECOVERY_X", 10, 1) == 10, bad   # bad/below-min -> default
  monkeypatch.setenv("RECOVERY_X", "7")
  assert f("RECOVERY_X", 10, 1) == 7           # valid -> parsed


def test_env_float_min_fail_safe(recovery_env, monkeypatch):
  f = recovery_env["recoveryd"]._env_float_min
  # A window must be > 0 (inclusive=False) or throttling would disable itself.
  for bad in ("", "abc", "nan", "inf", "-inf", "0", "-1"):
    monkeypatch.setenv("RECOVERY_X", bad)
    assert f("RECOVERY_X", 60.0, 0.0, inclusive=False) == 60.0, bad
  monkeypatch.setenv("RECOVERY_X", "30")
  assert f("RECOVERY_X", 60.0, 0.0, inclusive=False) == 30.0
  # inclusive=True honors the minimum (0 explicitly disables the fail delay).
  monkeypatch.setenv("RECOVERY_X", "0")
  assert f("RECOVERY_X", 0.5, 0.0, inclusive=True) == 0.0
  monkeypatch.setenv("RECOVERY_X", "nan")
  assert f("RECOVERY_X", 0.5, 0.0, inclusive=True) == 0.5


def test_module_tunables_are_valid(recovery_env):
  """The live module singletons must be sane regardless of env, so the server
  always binds."""
  recoveryd = recovery_env["recoveryd"]
  assert recoveryd._AUTH_RATE_WINDOW > 0
  assert recoveryd._AUTH_RATE_LIMIT >= 1
  assert recoveryd._BCRYPT_CONCURRENCY >= 1
  assert recoveryd._AUTH_MAX_CONCURRENCY >= 1
  assert math.isfinite(recoveryd._BCRYPT_WAIT_SECS)
  assert recoveryd._AUTH_FAIL_DELAY >= 0


# -- /recover/auth admission ordering ---------------------------------------

def test_cross_site_auth_consumes_zero_throttle_budget(
  recovery_env, monkeypatch,
):
  recoveryd = recovery_env["recoveryd"]

  class _TrackingThrottle:
    def __init__(self):
      self.calls = 0
      self.throttle = recoveryd._FixedWindowThrottle(
        limit=1, window=60, max_keys=16)

    def allow(self, key, now):
      self.calls += 1
      return self.throttle.allow(key, now)

  class _TrackingInflight:
    def __init__(self):
      self.acquire_calls = 0
      self.release_calls = 0

    def acquire(self, *, blocking):
      self.acquire_calls += 1
      return True

    def release(self):
      self.release_calls += 1

  throttle = _TrackingThrottle()
  inflight = _TrackingInflight()
  monkeypatch.setattr(recoveryd, "_AUTH_THROTTLE", throttle)
  monkeypatch.setattr(recoveryd, "_AUTH_INFLIGHT", inflight)

  body_reads = {"n": 0}
  cross_site, captured = _auth_handler(recoveryd)
  del cross_site._reject_cross_site
  cross_site.headers["Sec-Fetch-Site"] = "cross-site"
  cross_site._read_form = (
    lambda: body_reads.__setitem__("n", body_reads["n"] + 1) or {})
  cross_site._route_auth_post()

  assert captured["code"] == int(HTTPStatus.FORBIDDEN)
  assert cross_site.close_connection is True
  assert throttle.calls == 0
  assert inflight.acquire_calls == 0
  assert inflight.release_calls == 0
  assert body_reads["n"] == 0

  same_site, captured = _auth_handler(recoveryd)
  same_site._handle_auth = lambda form: same_site._send(HTTPStatus.OK, "ok")
  same_site._route_auth_post()
  assert captured["code"] == int(HTTPStatus.OK)

  exhausted, captured = _auth_handler(recoveryd)
  exhausted._route_auth_post()
  assert captured["code"] == int(HTTPStatus.TOO_MANY_REQUESTS)


def test_auth_route_throttle_precedes_body_and_db(recovery_env, monkeypatch):
  """A throttled request must be rejected from headers alone — no body read, no
  owner_exists()/DB touch, no bcrypt."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")

  class _AlwaysBlock:
    def allow(self, key, now):
      return False

  monkeypatch.setattr(recoveryd, "_AUTH_THROTTLE", _AlwaysBlock())
  calls = {"read_form": 0, "owner_exists": 0, "verify": 0}
  monkeypatch.setattr(
    recovery_env["db"], "owner_exists",
    lambda: calls.__setitem__("owner_exists", calls["owner_exists"] + 1)
    or True)
  monkeypatch.setattr(
    recovery_env["auth"], "verify_password",
    lambda *a: calls.__setitem__("verify", calls["verify"] + 1) or True)
  handler, captured = _auth_handler(recoveryd)
  handler._read_form = (
    lambda: calls.__setitem__("read_form", calls["read_form"] + 1) or {})
  handler._route_auth_post()
  assert captured["code"] == int(HTTPStatus.TOO_MANY_REQUESTS)
  assert handler.close_connection is True
  assert calls == {"read_form": 0, "owner_exists": 0, "verify": 0}


def test_auth_route_inflight_cap_returns_503(recovery_env, monkeypatch):
  """When the concurrency cap is saturated, a new auth request is refused
  immediately (non-blocking) before the body is read."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  sem = threading.BoundedSemaphore(1)
  monkeypatch.setattr(recoveryd, "_AUTH_INFLIGHT", sem)
  read = {"n": 0}
  assert sem.acquire(timeout=1) is True
  try:
    handler, captured = _auth_handler(recoveryd)
    handler._read_form = lambda: read.__setitem__("n", read["n"] + 1) or {}
    handler._route_auth_post()
  finally:
    sem.release()
  assert captured["code"] == int(HTTPStatus.SERVICE_UNAVAILABLE)
  assert handler.close_connection is True
  assert read["n"] == 0


# -- /recover/auth outcomes -------------------------------------------------

def test_auth_success_still_sets_cookie(recovery_env, monkeypatch):
  """Regression: the bounds must not break a correct login."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "build_status", lambda: {})
  monkeypatch.setattr(
    recovery_env["pages"], "dashboard_html", lambda *a, **k: "<ok>")
  handler, captured = _auth_handler(
    recoveryd, form={"username": "admin", "password": "hunter2"})
  handler._route_auth_post()
  assert captured["code"] == int(HTTPStatus.OK)
  cookie = captured["headers"].get("Set-Cookie", "")
  assert cookie.startswith("moebius_recover=")
  assert "HttpOnly" in cookie


def test_auth_wrong_password_returns_login_error(recovery_env, monkeypatch):
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  handler, captured = _auth_handler(
    recoveryd, form={"username": "admin", "password": "wrong"})
  handler._route_auth_post()
  assert captured["code"] == int(HTTPStatus.OK)
  assert "Set-Cookie" not in captured["headers"]
  assert "Incorrect" in captured["body"]


def test_auth_throttle_returns_429_after_limit(recovery_env, monkeypatch):
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  monkeypatch.setattr(
    recoveryd, "_AUTH_THROTTLE",
    recoveryd._FixedWindowThrottle(limit=3, window=60, max_keys=16))
  for _ in range(3):
    handler, captured = _auth_handler(
      recoveryd, peer=("172.18.0.9", 5000),
      form={"username": "admin", "password": "wrong"})
    handler._route_auth_post()
    assert captured["code"] == int(HTTPStatus.OK)
  handler, captured = _auth_handler(
    recoveryd, peer=("172.18.0.9", 5000),
    form={"username": "admin", "password": "hunter2"})
  handler._route_auth_post()
  assert captured["code"] == int(HTTPStatus.TOO_MANY_REQUESTS)
  assert "Retry-After" in captured["headers"]
  assert handler.close_connection is True


def test_auth_throttle_is_per_client(recovery_env, monkeypatch):
  """Two distinct forwarded client IPs behind the same proxy peer get
  independent budgets, so one client can't lock out another."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  monkeypatch.setattr(
    recoveryd, "_AUTH_THROTTLE",
    recoveryd._FixedWindowThrottle(limit=1, window=60, max_keys=16))
  for expected in (HTTPStatus.OK, HTTPStatus.TOO_MANY_REQUESTS):
    handler, captured = _auth_handler(
      recoveryd, peer=("172.18.0.9", 5000), xff="203.0.113.10",
      form={"username": "admin", "password": "wrong"})
    handler._route_auth_post()
    assert captured["code"] == int(expected)
  handler, captured = _auth_handler(
    recoveryd, peer=("172.18.0.9", 5000), xff="203.0.113.20",
    form={"username": "admin", "password": "wrong"})
  handler._route_auth_post()
  assert captured["code"] == int(HTTPStatus.OK)


# -- bcrypt semaphore: cap + owner progress ---------------------------------

def test_bcrypt_concurrency_never_exceeds_cap(recovery_env, monkeypatch):
  """No more than _BCRYPT_CONCURRENCY verifies run at once, no matter how many
  requests arrive together."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  monkeypatch.setattr(recoveryd, "_BCRYPT_WAIT_SECS", 5.0)
  cap = 2
  monkeypatch.setattr(
    recoveryd, "_BCRYPT_GATE", threading.BoundedSemaphore(cap))
  state = {"inside": 0, "peak": 0}
  lock = threading.Lock()
  release = threading.Event()
  entered = threading.Semaphore(0)

  def _slow_verify(pw, h):
    with lock:
      state["inside"] += 1
      state["peak"] = max(state["peak"], state["inside"])
    entered.release()
    release.wait(5)
    with lock:
      state["inside"] -= 1
    return False

  monkeypatch.setattr(recovery_env["auth"], "verify_password", _slow_verify)
  threads = []
  for _ in range(6):
    def run():
      h, _c = _auth_handler(
        recoveryd, form={"username": "admin", "password": "x"})
      h._handle_auth({"username": "admin", "password": "x"})
    t = threading.Thread(target=run)
    t.start()
    threads.append(t)
  try:
    for _ in range(cap):
      assert entered.acquire(timeout=5)
    time.sleep(0.2)  # give any (incorrectly) extra verify a chance to enter
    peak = state["peak"]
  finally:
    release.set()
    for t in threads:
      t.join(5)
  assert peak == cap


def test_owner_gets_bounded_503_when_gate_saturated(recovery_env, monkeypatch):
  """With every bcrypt slot held, a login returns 503 within the bounded wait
  instead of hanging — the owner makes bounded progress under a flood."""
  recoveryd = recovery_env["recoveryd"]
  _create_owner(recovery_env["db_path"], "admin", "hunter2")
  monkeypatch.setattr(recoveryd, "_AUTH_FAIL_DELAY", 0.0)
  monkeypatch.setattr(recoveryd, "_BCRYPT_WAIT_SECS", 0.1)
  gate = threading.BoundedSemaphore(1)
  monkeypatch.setattr(recoveryd, "_BCRYPT_GATE", gate)
  assert gate.acquire(timeout=1) is True
  try:
    handler, captured = _auth_handler(recoveryd)
    started = time.monotonic()
    handler._handle_auth({"username": "admin", "password": "hunter2"})
    elapsed = time.monotonic() - started
  finally:
    gate.release()
  assert captured["code"] == int(HTTPStatus.SERVICE_UNAVAILABLE)
  assert captured["headers"].get("Retry-After") == "2"
  assert elapsed < 2.0  # bounded by _BCRYPT_WAIT_SECS, not a hang
