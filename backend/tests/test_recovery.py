"""Lock-in tests for the recovery surface.

Two categories:
  1. Pure unit tests of recover_auth (signed cookie roundtrip).
  2. End-to-end tests of the recovery chat HTTP surface via the
     FastAPI TestClient.

Filesystem permission tests live in test_recovery_filesystem.py and
need a real container — they're not testable from pytest's process
because Docker layer perms aren't reproducible without docker build.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import recover_auth


# ---------------------------------------------------------------------
# recover_auth — signed cookie roundtrip
# ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
  """recover_auth reads SECRET_KEY at call time; provide a stable one."""
  monkeypatch.setenv("SECRET_KEY", "a" * 64)


def test_recover_auth_roundtrip():
  tok = recover_auth.create_session_token("alice")
  assert recover_auth.decode_session_token(tok) == "alice"


def test_recover_auth_rejects_tampered_payload():
  tok = recover_auth.create_session_token("alice")
  payload_b64, sig_b64 = tok.split(".")
  # Substitute the payload with a forged one; signature won't match.
  import base64
  forged = base64.urlsafe_b64encode(
    b'{"sub":"attacker","exp":99999999999}'
  ).rstrip(b"=").decode("ascii")
  bad = f"{forged}.{sig_b64}"
  assert recover_auth.decode_session_token(bad) is None


def test_recover_auth_rejects_garbage():
  assert recover_auth.decode_session_token(None) is None
  assert recover_auth.decode_session_token("") is None
  assert recover_auth.decode_session_token("no-dot") is None
  assert recover_auth.decode_session_token("bad.token") is None


def test_recover_auth_rejects_expired(monkeypatch):
  """An exp in the past must reject."""
  tok = recover_auth.create_session_token("alice")
  # Fast-forward time past the TTL.
  import time as _time
  original = _time.time
  monkeypatch.setattr(
    "time.time",
    lambda: original() + recover_auth.SESSION_TTL_SECONDS + 10,
  )
  assert recover_auth.decode_session_token(tok) is None


def test_recover_auth_rejects_signed_with_different_key(monkeypatch):
  """Key rotation invalidates old cookies (no in-memory cache)."""
  tok = recover_auth.create_session_token("alice")
  monkeypatch.setenv("SECRET_KEY", "b" * 64)
  assert recover_auth.decode_session_token(tok) is None


def test_recover_auth_password_verify():
  import bcrypt
  hashed = bcrypt.hashpw(b"correct horse", bcrypt.gensalt()).decode()
  assert recover_auth.verify_password("correct horse", hashed)
  assert not recover_auth.verify_password("wrong", hashed)
  assert not recover_auth.verify_password("", hashed)


# ---------------------------------------------------------------------
# Recovery chat HTTP surface — end-to-end via TestClient
# ---------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
  """A fresh TestClient with isolated /data/recovery_chat.jsonl."""
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "recovery_chat.jsonl",
  )
  from app.main import app
  return TestClient(app)


@pytest.fixture
def auth_cookie(client, monkeypatch, tmp_path):
  """Sets up admin/admin and returns a cookies dict for authed requests."""
  from app.database import SessionLocal
  from app import models
  import bcrypt

  db = SessionLocal()
  # Idempotent setup — the previous test may have left an owner row.
  existing = db.query(models.Owner).filter(
    models.Owner.username == "tester"
  ).first()
  if not existing:
    owner = models.Owner(
      username="tester",
      hashed_password=bcrypt.hashpw(b"correct horse", bcrypt.gensalt()).decode(),
      provider="claude",
    )
    db.add(owner)
    db.commit()
  db.close()

  token = recover_auth.create_session_token("tester")
  return {recover_auth.COOKIE_NAME: token}


def test_recover_chat_page_redirects_without_cookie(client):
  r = client.get("/recover/chat", follow_redirects=False)
  assert r.status_code == 302
  assert "/recover" in r.headers.get("location", "") or "url=/recover" in r.text


def test_recover_chat_page_renders_with_cookie(client, auth_cookie):
  r = client.get("/recover/chat", cookies=auth_cookie)
  assert r.status_code == 200
  assert "Mobius recovery chat" in r.text
  assert "Recovery mode" in r.text


def test_recover_chat_send_persists_to_jsonl(client, auth_cookie, monkeypatch, tmp_path):
  log_path = tmp_path / "recovery_chat.jsonl"
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH", log_path,
  )
  r = client.post(
    "/recover/chat/send",
    json={"message": "fix the broken thing"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200
  assert r.json()["status"] == "queued"
  assert log_path.is_file()
  lines = log_path.read_text().strip().splitlines()
  assert len(lines) == 1
  entry = json.loads(lines[0])
  assert entry["role"] == "user"
  assert entry["content"] == "fix the broken thing"


def test_recover_chat_send_rejects_empty(client, auth_cookie):
  r = client.post(
    "/recover/chat/send",
    json={"message": "   "},
    cookies=auth_cookie,
  )
  assert r.status_code == 400


def test_recover_chat_send_requires_cookie(client):
  r = client.post(
    "/recover/chat/send",
    json={"message": "hi"},
  )
  assert r.status_code == 401


def test_recover_chat_reset_wipes_log(client, auth_cookie, monkeypatch, tmp_path):
  log_path = tmp_path / "recovery_chat.jsonl"
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH", log_path,
  )
  log_path.write_text(
    '{"role":"user","content":"x","ts":1.0}\n'
    '{"role":"assistant","content":"y","ts":2.0}\n'
  )
  assert log_path.is_file()
  r = client.post("/recover/chat/reset", cookies=auth_cookie)
  assert r.status_code == 200
  assert not log_path.is_file()


def test_recover_chat_reset_requires_cookie(client):
  r = client.post("/recover/chat/reset")
  assert r.status_code == 401


def test_recover_chat_stream_requires_cookie(client):
  # POST (not GET) so message never lands in access logs / browser
  # history. Body is empty — runner reads the latest user line from
  # /data/recovery_chat.jsonl.
  r = client.post("/recover/chat/stream")
  assert r.status_code == 401


def test_recover_chat_stream_rejects_when_no_message_in_log(
  client, auth_cookie, monkeypatch, tmp_path,
):
  # No log file yet, so latest_user_message() returns None.
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "empty.jsonl",
  )
  r = client.post("/recover/chat/stream", cookies=auth_cookie)
  assert r.status_code == 400


def test_recover_chat_latest_user_message(monkeypatch, tmp_path):
  """The stream endpoint reads the most-recent user line; runner
  must return it correctly across mixed roles."""
  log_path = tmp_path / "mixed.jsonl"
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH", log_path,
  )
  from app import recover_chat_runner as rcr
  rcr.append_log("user", "first")
  rcr.append_log("assistant", "reply1")
  rcr.append_log("user", "second")
  rcr.append_log("assistant", "reply2")
  assert rcr.latest_user_message() == "second"


def test_recover_chat_runner_log_helpers(monkeypatch, tmp_path):
  """append_log + load_log + reset_log work as documented."""
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "recovery.jsonl",
  )
  from app import recover_chat_runner as rcr

  assert rcr.load_log() == []
  rcr.append_log("user", "hello")
  rcr.append_log("assistant", "world")
  logged = rcr.load_log()
  assert len(logged) == 2
  assert logged[0]["content"] == "hello"
  assert logged[1]["content"] == "world"

  rcr.reset_log()
  assert rcr.load_log() == []


def test_recover_chat_page_escapes_role_field(client, auth_cookie, monkeypatch, tmp_path):
  """Poisoned role values (e.g. from a compromised agent writing to
  /data/recovery_chat.jsonl) must be HTML-escaped when rendered,
  otherwise the recovery page becomes XSS-vulnerable on the only
  trusted surface left when production chat is broken."""
  log_path = tmp_path / "poisoned.jsonl"
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH", log_path,
  )
  log_path.write_text(
    '{"role":"<script>alert(1)</script>","content":"hi","ts":1.0}\n'
  )
  r = client.get("/recover/chat", cookies=auth_cookie)
  assert r.status_code == 200
  # The raw payload must not appear as live HTML.
  assert "<script>alert(1)</script>" not in r.text
  # The escaped form should appear.
  assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_recover_auth_empty_secret_key():
  """SECRET_KEY missing or empty: create_session_token raises (the
  caller / route is responsible for surfacing this), decode returns
  None (graceful degradation for inbound requests)."""
  import os
  saved = os.environ.get("SECRET_KEY")
  os.environ["SECRET_KEY"] = ""
  try:
    import pytest
    with pytest.raises(RuntimeError):
      recover_auth.create_session_token("alice")
    # decode is the inbound path — must NOT raise; returns None.
    assert recover_auth.decode_session_token("any.token") is None
  finally:
    if saved is None:
      os.environ.pop("SECRET_KEY", None)
    else:
      os.environ["SECRET_KEY"] = saved
