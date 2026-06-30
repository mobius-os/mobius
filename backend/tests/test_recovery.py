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


def test_recover_auth_survives_secret_key_rotation(monkeypatch, tmp_path):
  """Rotating SECRET_KEY must NOT invalidate recovery cookies.

  The recovery cookie is now keyed on /data/.recovery-secret, not
  SECRET_KEY — so a SECRET_KEY drift (the documented JWT-invalidation
  outage) leaves the recovery surface reachable.  This is the inverse
  of the old behavior (which was the P0 bug we fixed).
  """
  monkeypatch.setattr(recover_auth, "_RECOVERY_SECRET_PATH", tmp_path / ".recovery-secret")
  tok = recover_auth.create_session_token("alice")
  # Rotate SECRET_KEY — old behavior would have invalidated the cookie.
  monkeypatch.setenv("SECRET_KEY", "b" * 64)
  # Cookie must STILL be valid after SECRET_KEY rotation.
  assert recover_auth.decode_session_token(tok) == "alice", (
    "recovery cookie must survive SECRET_KEY rotation"
  )


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
  """A fresh TestClient with an isolated recovery layout.

  Patches BOTH the legacy single-file path (RECOVERY_LOG_PATH) and
  the multi-chat directory (RECOVERY_CHATS_DIR) into tmp_path so a
  test can't pollute the developer's /data. Also clears the
  in-process dedup set so test order doesn't matter.
  """
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "recovery_chat.jsonl",
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_CHATS_DIR",
    tmp_path / "chats",
  )
  from app import recover_chat as rc
  rc._streamed_turn_ids.clear()
  from app.main import app
  return TestClient(app)


@pytest.fixture
def chat_id(client):
  """Creates a fresh Claude chat and yields its chat_id.

  Most HTTP tests need a chat to act on. The client fixture isolates
  the chats dir to tmp_path, so this is safe per-test.
  """
  from app import recover_chat_runner as rcr
  return rcr.create_chat("claude")


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


def test_recover_action_requires_password_reauth(client, auth_cookie, monkeypatch):
  """The recovery cookie ALONE must not authorize the secret-bearing or
  destructive actions.

  Threat: the recovery cookie is HttpOnly + SameSite=Strict + path-scoped, but
  a same-origin mini-app (sandboxed iframe with allow-same-origin) can still
  attach it to a credentialed fetch and replay it within the 1-hour window —
  silently exfiltrating the secrets backup or wiping the instance. download_
  backup and factory_reset therefore require a fresh owner-password re-entry
  (which a replaying app can't supply); reinstall_store stays cookie-only.
  """
  from app.routes import recover as rec
  from fastapi.responses import HTMLResponse

  # Stub the heavy side effects so the test exercises only the re-auth GATE.
  monkeypatch.setattr(rec, "_create_backup", lambda data_dir: HTMLResponse("BACKUP_OK"))
  monkeypatch.setattr(rec, "_action_reinstall_store", lambda: "reinstalled")

  # download_backup with the cookie ALONE (the replay vector) -> blocked.
  r = client.post("/recover/action", data={"action": "download_backup"}, cookies=auth_cookie)
  assert r.status_code == 401
  assert "BACKUP_OK" not in r.text
  # ...and with a WRONG password -> blocked.
  r = client.post(
    "/recover/action",
    data={"action": "download_backup", "password": "wrong"},
    cookies=auth_cookie,
  )
  assert r.status_code == 401
  # ...with the CORRECT password -> passes the gate, backup runs.
  r = client.post(
    "/recover/action",
    data={"action": "download_backup", "password": "correct horse"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200
  assert "BACKUP_OK" in r.text

  # factory_reset with the cookie alone -> blocked AND the owner survives
  # (proves the destructive action did not run behind the gate).
  r = client.post("/recover/action", data={"action": "factory_reset"}, cookies=auth_cookie)
  assert r.status_code == 401
  assert rec._owner_exists("tester"), "factory_reset executed without password re-auth"

  # reinstall_store is idempotent + non-secret -> NOT gated; cookie alone works.
  r = client.post("/recover/action", data={"action": "reinstall_store"}, cookies=auth_cookie)
  assert r.status_code == 200


def test_recover_action_empty_password_never_authorizes(client, monkeypatch):
  """Defense in depth: even if the owner configured an EMPTY password, the
  re-auth gate must not be satisfiable by the replayed POST's default empty
  `password` — verify_password("", hash_of_"") is True, so the gate rejects an
  empty submission outright before trusting bcrypt."""
  from app.database import SessionLocal
  from app import models
  from app.routes import recover as rec
  from fastapi.responses import HTMLResponse
  import bcrypt

  db = SessionLocal()
  if not db.query(models.Owner).filter(models.Owner.username == "emptypw").first():
    db.add(models.Owner(
      username="emptypw",
      hashed_password=bcrypt.hashpw(b"", bcrypt.gensalt()).decode(),
      provider="claude",
    ))
    db.commit()
  db.close()

  cookie = {recover_auth.COOKIE_NAME: recover_auth.create_session_token("emptypw")}
  monkeypatch.setattr(rec, "_create_backup", lambda data_dir: HTMLResponse("BACKUP_OK"))

  # The trap: verify_password DOES accept "" against a hash of "".
  assert recover_auth.verify_password("", rec._owner_password_hash("emptypw"))
  # ...but the gate still blocks the empty (default) submission a replay sends.
  r = client.post(
    "/recover/action",
    data={"action": "download_backup", "password": ""},
    cookies=cookie,
  )
  assert r.status_code == 401
  assert "BACKUP_OK" not in r.text


def test_recover_chat_page_redirects_without_cookie(client):
  r = client.get("/recover/chat", follow_redirects=False)
  assert r.status_code == 302
  assert "/recover" in r.headers.get("location", "") or "url=/recover" in r.text


def test_recover_chat_page_renders_with_cookie(client, auth_cookie):
  r = client.get("/recover/chat", cookies=auth_cookie)
  assert r.status_code == 200
  assert "Mobius recovery chat" in r.text
  assert "Recovery mode" in r.text


def test_recover_chat_send_persists_to_jsonl(client, auth_cookie, chat_id):
  """A successful /send appends a user entry to the chat's log file.

  Verifies the on-disk shape (jsonl with the user message) so a
  later /stream can replay against the file even if the in-process
  state is lost.
  """
  from app import recover_chat_runner as rcr
  log_path = rcr.chat_log_path(chat_id)
  # New chat: log file has only the _meta line at this point.
  assert log_path.is_file()
  initial_lines = log_path.read_text().strip().splitlines()
  assert len(initial_lines) == 1  # _meta only

  r = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "fix the broken thing"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200
  assert r.json()["status"] == "queued"
  lines = log_path.read_text().strip().splitlines()
  assert len(lines) == 2  # _meta + the new user entry
  user_entry = json.loads(lines[1])
  assert user_entry["role"] == "user"
  assert user_entry["content"] == "fix the broken thing"


def test_recover_chat_send_rejects_empty(client, auth_cookie, chat_id):
  r = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "   "},
    cookies=auth_cookie,
  )
  assert r.status_code == 400


def test_recover_chat_send_requires_cookie(client, chat_id):
  r = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "hi"},
  )
  assert r.status_code == 401


def test_recover_chat_reset_wipes_log(client, auth_cookie, chat_id):
  """/recover/chat/reset truncates the chat to just its _meta line.

  Reset KEEPS the chat slot (so the chat_id stays valid; user can
  keep chatting). For permanent deletion the user calls
  /recover/chat/delete instead.
  """
  from app import recover_chat_runner as rcr
  # Populate the chat with a couple of entries via the runner so
  # we have something to reset.
  rcr.append_log(chat_id, "user", "x")
  rcr.append_log(chat_id, "assistant", "y")
  log_path = rcr.chat_log_path(chat_id)
  pre = log_path.read_text().strip().splitlines()
  assert len(pre) == 3  # _meta + user + assistant

  r = client.post(
    "/recover/chat/reset",
    json={"chat_id": chat_id},
    cookies=auth_cookie,
  )
  assert r.status_code == 200
  post = log_path.read_text().strip().splitlines()
  assert len(post) == 1  # only _meta survives
  # Chat slot still valid.
  assert rcr.get_chat_provider(chat_id) == "claude"


def test_recover_chat_reset_requires_cookie(client, chat_id):
  # The 401 fires before body parsing, so the missing chat_id in
  # the body doesn't matter — but pass it anyway for clarity.
  r = client.post("/recover/chat/reset", json={"chat_id": chat_id})
  assert r.status_code == 401


def test_recover_chat_stream_requires_cookie(client):
  # POST (not GET) so message never lands in access logs / browser
  # history. Body is empty — runner reads the latest user line from
  # /data/recovery_chat.jsonl.
  r = client.post("/recover/chat/stream")
  assert r.status_code == 401


def test_recover_chat_latest_user_message(monkeypatch, tmp_path):
  """The stream endpoint reads the most-recent user line; runner
  must return it correctly across mixed roles."""
  log_path = tmp_path / "mixed.jsonl"
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_CHATS_DIR",
    tmp_path / "chats",
  )
  chat_id = rcr.create_chat("claude")
  rcr.append_log(chat_id, "user", "first")
  rcr.append_log(chat_id, "assistant", "reply1")
  rcr.append_log(chat_id, "user", "second")
  rcr.append_log(chat_id, "assistant", "reply2")
  assert rcr.latest_user_message(chat_id) == "second"


def test_recover_chat_runner_log_helpers(monkeypatch, tmp_path):
  """append_log + load_log + reset_log work as documented for a chat."""
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_CHATS_DIR",
    tmp_path / "chats",
  )
  from app import recover_chat_runner as rcr
  chat_id = rcr.create_chat("claude")

  # load_log skips the _meta line; freshly-created chat has zero
  # user/assistant messages.
  assert rcr.load_log(chat_id) == []
  rcr.append_log(chat_id, "user", "hello")
  rcr.append_log(chat_id, "assistant", "world")
  logged = rcr.load_log(chat_id)
  assert len(logged) == 2
  assert logged[0]["content"] == "hello"
  assert logged[1]["content"] == "world"

  # reset_log truncates the chat to its _meta line (keeps the slot).
  rcr.reset_log(chat_id)
  assert rcr.load_log(chat_id) == []
  # Chat still exists (provider preserved).
  assert rcr.get_chat_provider(chat_id) == "claude"


def test_recover_chat_page_escapes_role_field(
  client, auth_cookie, monkeypatch, tmp_path,
):
  """Poisoned role values (e.g. from a compromised agent writing to
  the chat log file) must be HTML-escaped when rendered, otherwise
  the recovery page becomes XSS-vulnerable on the only trusted
  surface left when production chat is broken.

  Under multi-chat, the page renders /recover/chat?id=<chat_id>, so
  we plant the malformed entry into a per-chat log and request that
  chat specifically.
  """
  from app import recover_chat_runner as rcr
  chat_id = rcr.create_chat("claude")
  # Manually append a poisoned entry to the chat's log file —
  # bypasses the runner's validation so we test the page's escape
  # behavior, not the writer's.
  log_path = rcr.chat_log_path(chat_id)
  with log_path.open("a") as f:
    f.write(
      '{"role":"<script>alert(1)</script>","content":"hi","ts":1.0}\n'
    )
  r = client.get(f"/recover/chat?id={chat_id}", cookies=auth_cookie)
  assert r.status_code == 200
  assert "<script>alert(1)</script>" not in r.text
  assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_recover_auth_empty_secret_key_is_ok(tmp_path, monkeypatch):
  """An empty SECRET_KEY must NOT break recovery auth.

  Recovery cookies are now keyed on /data/.recovery-secret, NOT on
  SECRET_KEY. An empty/missing SECRET_KEY is the documented JWT-
  invalidation outage mode — that is EXACTLY when recovery matters
  most, so recovery cookie creation and validation must still work.
  """
  monkeypatch.setattr(recover_auth, "_RECOVERY_SECRET_PATH", tmp_path / ".recovery-secret")
  monkeypatch.setenv("SECRET_KEY", "")
  # Must NOT raise, even with an empty SECRET_KEY.
  tok = recover_auth.create_session_token("alice")
  assert recover_auth.decode_session_token(tok) == "alice", (
    "recovery cookies must work even when SECRET_KEY is empty"
  )


def test_recover_auth_unreadable_recovery_secret_degrades_gracefully(
  tmp_path, monkeypatch,
):
  """If /data/.recovery-secret cannot be read OR created (permissions
  failure — genuinely unusual), decode_session_token must return None
  rather than raising, so the endpoint sees a 401 instead of a 500."""
  import stat
  secret_path = tmp_path / ".recovery-secret"
  # Make the directory unwritable so generation fails.
  tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x, no write
  monkeypatch.setattr(recover_auth, "_RECOVERY_SECRET_PATH", secret_path)
  try:
    # decode must not raise — graceful degradation.
    result = recover_auth.decode_session_token("any.token")
    assert result is None
  finally:
    # Restore write permission so tmp_path cleanup works.
    tmp_path.chmod(0o755)


# ---------------------------------------------------------------------
# Send/stream pairing — closes the multi-tab race where /stream would
# read "latest user message" instead of the specific one paired with
# the /send that returned the turn_id.
# ---------------------------------------------------------------------

def test_send_returns_turn_id(client, auth_cookie, chat_id):
  """/recover/chat/send returns a turn_id the client passes back to
  /stream so the response pairs with this specific message, not
  'latest' (which races under multi-tab use).

  Under multi-chat, turn_id=0 is the _meta line. The first user
  send gets turn_id=1, the second gets turn_id=2, etc.
  """
  r = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "first"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200
  body = r.json()
  assert "turn_id" in body
  assert body["turn_id"] == 1  # 0 is the _meta line

  r2 = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "second"},
    cookies=auth_cookie,
  )
  assert r2.json()["turn_id"] == 2


def test_user_message_by_id_pairs_correctly(monkeypatch, tmp_path):
  """The runner helper returns the EXACT message at a turn_id, not
  the latest. This is what closes the send/stream pairing race."""
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_CHATS_DIR",
    tmp_path / "chats",
  )
  from app import recover_chat_runner as rcr
  chat_id = rcr.create_chat("claude")
  id1 = rcr.append_log(chat_id, "user", "first")
  id2 = rcr.append_log(chat_id, "assistant", "reply1")
  id3 = rcr.append_log(chat_id, "user", "second")

  assert rcr.user_message_by_id(chat_id, id1) == "first"
  assert rcr.user_message_by_id(chat_id, id2) is None  # assistant
  assert rcr.user_message_by_id(chat_id, id3) == "second"
  # Out-of-range ids return None cleanly.
  assert rcr.user_message_by_id(chat_id, 999) is None
  assert rcr.user_message_by_id(chat_id, -1) is None
  # The _meta line at index 0 is not a user message.
  assert rcr.user_message_by_id(chat_id, 0) is None


def test_stream_uses_provided_turn_id_not_latest(
  client, auth_cookie, chat_id,
):
  """If the client passes turn_id=N, the stream endpoint should
  resolve to message N — even if a NEWER user message has landed
  in the log since. This is the multi-tab race fix."""
  # Two sends in quick succession (simulating two tabs racing).
  r1 = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "tab1 message"},
    cookies=auth_cookie,
  )
  tab1_turn = r1.json()["turn_id"]
  r2 = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "tab2 message"},
    cookies=auth_cookie,
  )
  tab2_turn = r2.json()["turn_id"]
  assert tab1_turn != tab2_turn

  # The runner's user_message_by_id (what the stream endpoint uses
  # internally) returns the right message for each turn_id.
  from app import recover_chat_runner as rcr
  assert rcr.user_message_by_id(chat_id, tab1_turn) == "tab1 message"
  assert rcr.user_message_by_id(chat_id, tab2_turn) == "tab2 message"
  # latest_user_message would return tab2 (the race scenario we
  # are explicitly NOT relying on anymore).
  assert rcr.latest_user_message(chat_id) == "tab2 message"


# ---------------------------------------------------------------------
# Owner-existence check on every cookie consumption
# ---------------------------------------------------------------------

def test_stale_cookie_after_owner_deletion_rejected(
  client, auth_cookie, chat_id,
):
  """A valid HMAC cookie issued before a factory reset must NOT
  retain elevated access after the Owner row is gone. Without the
  per-request owner-existence check, the cookie would stay valid
  for the remainder of its 1h TTL — a second tab, stolen cookie, or
  another browser profile would keep elevated write access on a
  wiped instance.
  """
  # Sanity: send works while the owner exists (the auth_cookie
  # fixture created the `tester` row).
  r = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "before reset"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200

  # Simulate factory reset: delete the Owner row. Cookie HMAC stays
  # valid; the structural check is whether the backend re-verifies
  # the owner on each call.
  from app.database import SessionLocal
  from app import models
  db = SessionLocal()
  db.query(models.Owner).filter(models.Owner.username == "tester").delete()
  db.commit()
  db.close()

  # Every endpoint that mutates state or returns elevated data must
  # reject the now-stale cookie.
  r = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "after reset"},
    cookies=auth_cookie,
  )
  assert r.status_code == 401, "stale cookie must not be accepted on /send"

  r = client.post(
    "/recover/chat/stream",
    json={"chat_id": chat_id, "turn_id": 1},
    cookies=auth_cookie,
  )
  assert r.status_code == 401, "stale cookie must not be accepted on /stream"

  r = client.post(
    "/recover/chat/reset",
    json={"chat_id": chat_id},
    cookies=auth_cookie,
  )
  assert r.status_code == 401, "stale cookie must not be accepted on /reset"

  # The HTML page must redirect (its no-cookie behavior), not render.
  r = client.get("/recover/chat", cookies=auth_cookie, follow_redirects=False)
  assert r.status_code == 302


# ---------------------------------------------------------------------
# turn_id replay/reuse guard
# ---------------------------------------------------------------------

def test_turn_id_replay_returns_409(client, auth_cookie, chat_id, monkeypatch):
  """The same (chat_id, turn_id) POSTed to /stream twice must return
  409 on the second attempt. Under multi-chat, the dedup key is the
  tuple — two different chats can each have turn_id=1 without conflict.
  """
  from app import recover_chat as rc
  rc._streamed_turn_ids.clear()

  r = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "fix the thing"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200
  turn_id = r.json()["turn_id"]

  # Stub stream_turn so the test doesn't actually spawn a CLI.
  from app import recover_chat_runner as rcr

  async def _empty_stream(_message, _provider=None, chat_id=None, model=None):
    if False:
      yield ""

  monkeypatch.setattr(rcr, "stream_turn", _empty_stream)

  r1 = client.post(
    "/recover/chat/stream",
    json={"chat_id": chat_id, "turn_id": turn_id},
    cookies=auth_cookie,
  )
  assert r1.status_code == 200, (
    f"first stream should succeed, got {r1.status_code}: {r1.text}"
  )

  # Replay: same (chat_id, turn_id) → 409.
  r2 = client.post(
    "/recover/chat/stream",
    json={"chat_id": chat_id, "turn_id": turn_id},
    cookies=auth_cookie,
  )
  assert r2.status_code == 409
  assert "turn_id" in r2.text.lower() or "already" in r2.text.lower()


def test_turn_id_streamed_set_is_bounded(monkeypatch):
  """The dedup set must not grow unbounded — at most N most-recent
  turn_ids are remembered. This guards against memory exhaustion
  via a long-running recovery session or adversarial id flooding."""
  from app import recover_chat as rc
  rc._streamed_turn_ids.clear()

  # Push 2x the cap; only the most-recent N entries should survive.
  cap = rc._STREAMED_TURN_IDS_MAX
  for i in range(cap * 2):
    rc._mark_turn_id_streamed("test-chat", i)
  assert len(rc._streamed_turn_ids) == cap
  # FIFO eviction: the oldest ids were dropped, the newest are kept.
  # Old assertion (int-keyed) replaced with tuple-keyed check below.
  assert all(k[1] != 0 for k in rc._streamed_turn_ids)
  assert any(k[1] == cap * 2 - 1 for k in rc._streamed_turn_ids)


# ---------------------------------------------------------------------
# Structural fix #1: stream_turn must release its run-claim on client
# disconnect so the next /stream call isn't blocked. The old design
# held an asyncio.Lock across the whole generator; FastAPI stopping
# consumption (client disconnect) didn't run the lock's __aexit__
# until generator GC, orphaning the lock and blocking every later
# /stream.
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_turn_releases_claim_on_client_disconnect(
  monkeypatch, tmp_path,
):
  """Abandon a stream mid-flight (aclose the generator before
  consuming "done"); a fresh stream_turn must run without blocking.

  Regression target: prior code held _STREAM_LOCK across the entire
  generator. A client disconnect left the lock held until generator
  GC ran __aexit__, which on a busy server was often "never until
  process restart" — blocking the only escape hatch the user has.

  We fake the subprocess so the test runs without a real claude
  binary: any spawn from _stream_turn_impl returns a stub Process
  whose stdout slowly yields a few lines and then EOFs. We start
  the first stream, pull one chunk, then aclose() it — simulating
  FastAPI stopping consumption on disconnect. Then we start a
  second stream and assert it produces events promptly (i.e. the
  claim was released).
  """
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "disconnect.jsonl",
  )

  class _StubStream:
    def __init__(self, lines):
      self._lines = list(lines)
      self._closed = False

    async def readline(self):
      if self._closed or not self._lines:
        return b""
      # Yield a small delay so the test can interleave aclose()
      # between reads — simulates a real streaming subprocess.
      await _asyncio.sleep(0.01)
      return self._lines.pop(0)

    async def read(self):
      return b""

  class _StubProc:
    instances: list = []

    def __init__(self, *lines):
      # Two text-delta events then EOF; enough to let the consumer
      # pull one chunk before aclose.
      payload = [
        (
          b'{"type":"stream_event","event":{"type":'
          b'"content_block_delta","delta":{"type":'
          b'"text_delta","text":"hi"}}}\n'
        ),
        (
          b'{"type":"stream_event","event":{"type":'
          b'"content_block_delta","delta":{"type":'
          b'"text_delta","text":" there"}}}\n'
        ),
      ]
      self.stdout = _StubStream(payload)
      self.stderr = _StubStream([])
      self.stdin = _StubStdin()
      self.returncode = None
      self._terminated = False
      self._waited = False
      _StubProc.instances.append(self)

    def terminate(self):
      self._terminated = True
      self.returncode = -15

    def kill(self):
      self.returncode = -9

    async def wait(self):
      self._waited = True
      if self.returncode is None:
        self.returncode = 0
      return self.returncode

  class _StubStdin:
    def __init__(self):
      self._closed = False

    def write(self, data):
      pass

    async def drain(self):
      pass

    def close(self):
      self._closed = True

  async def fake_spawn(*cmd, **kwargs):
    return _StubProc()

  monkeypatch.setattr(
    "app.recover_chat_runner.asyncio.create_subprocess_exec",
    fake_spawn,
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.shutil.which",
    lambda _: "/fake/claude",
  )

  # First stream — pull one chunk then abandon it.
  gen1 = rcr.stream_turn("first")
  first_chunk = await gen1.__anext__()
  assert "hi" in first_chunk or "data:" in first_chunk
  await gen1.aclose()

  # The run-claim must be released after aclose so the next stream
  # can proceed without blocking. Without the fix this hangs.
  gen2 = rcr.stream_turn("second")
  got_event = False
  async def _consume():
    nonlocal got_event
    async for chunk in gen2:
      got_event = True
      if "done" in chunk:
        break

  await _asyncio.wait_for(_consume(), timeout=2.0)
  assert got_event, "second stream never produced events"


@pytest.mark.asyncio
async def test_stream_turn_rejects_concurrent_second_request(
  monkeypatch, tmp_path,
):
  """A second stream_turn invoked while the first is in flight must
  receive an SSE error frame ('Another recovery turn is in
  progress.') and complete promptly — not queue, not block."""
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "concurrent.jsonl",
  )

  # Hold the first stream open: stub a subprocess whose stdout never
  # EOFs until we set a release event.
  release = _asyncio.Event()

  class _BlockingStream:
    async def readline(self):
      await release.wait()
      return b""
    async def read(self):
      return b""

  class _BlockingStdin:
    def write(self, data):
      pass
    async def drain(self):
      pass
    def close(self):
      pass

  class _BlockingProc:
    def __init__(self):
      self.stdout = _BlockingStream()
      self.stderr = _BlockingStream()
      self.stdin = _BlockingStdin()
      self.returncode = None

    def terminate(self):
      self.returncode = -15

    def kill(self):
      self.returncode = -9

    async def wait(self):
      if self.returncode is None:
        self.returncode = 0
      return self.returncode

  async def fake_spawn(*cmd, **kwargs):
    return _BlockingProc()

  monkeypatch.setattr(
    "app.recover_chat_runner.asyncio.create_subprocess_exec",
    fake_spawn,
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.shutil.which",
    lambda _: "/fake/claude",
  )

  gen1 = rcr.stream_turn("first")
  # Kick off gen1 so it claims the slot — pull one chunk via a task
  # but DON'T block waiting forever.
  task1 = _asyncio.create_task(gen1.__anext__())
  await _asyncio.sleep(0.05)  # yield so gen1 can claim

  # Second stream while first is live: should immediately yield an
  # error event mentioning the conflict, then done.
  gen2 = rcr.stream_turn("second")
  events = []
  async for chunk in gen2:
    events.append(chunk)
  joined = "".join(events)
  assert "Another recovery turn is in progress" in joined
  assert '"type":"done"' in joined

  # Release gen1 cleanly.
  release.set()
  task1.cancel()
  try:
    await task1
  except (_asyncio.CancelledError, StopAsyncIteration):
    pass
  await gen1.aclose()


# ---------------------------------------------------------------------
# Structural fix #2: append_log must be atomic across concurrent
# callers so each returns a distinct turn_id and the file ends with
# exactly N lines for N appends.
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_log_concurrent_callers_get_distinct_ids(
  monkeypatch, tmp_path,
):
  """Fire N=10 concurrent append_log calls into the same chat; each
  must return a distinct id and the file must end with exactly N+1
  lines (N appends + the _meta line).

  Without the lock, two callers could both read the post-append
  line count and both return the same id (the TOCTOU race that
  re-opens the multi-tab pairing bug).
  """
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_CHATS_DIR",
    tmp_path / "chats",
  )
  chat_id = rcr.create_chat("claude")

  N = 10
  results = await _asyncio.gather(
    *[_asyncio.to_thread(rcr.append_log, chat_id, "user", f"msg-{i}")
      for i in range(N)]
  )
  # All ids distinct, covering 1..N (turn_id=0 is the _meta line).
  assert sorted(results) == list(range(1, N + 1))
  # File has exactly N+1 non-empty lines (_meta + N appends).
  log_path = rcr.chat_log_path(chat_id)
  lines = [
    line for line in log_path.read_text().splitlines() if line.strip()
  ]
  assert len(lines) == N + 1
  # Every appended line is a valid JSON object — no torn writes.
  for line in lines[1:]:
    entry = json.loads(line)
    assert entry["role"] == "user"
    assert entry["content"].startswith("msg-")


# ---------------------------------------------------------------------
# Structural fix #3: long messages go via stdin, not argv, so they
# don't crash subprocess spawn at Linux's ~128KB argv cap.
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_turn_passes_long_message_via_stdin(
  monkeypatch, tmp_path,
):
  """A >200KB user message must spawn cleanly. We mock the subprocess
  to record the argv and the stdin write so we can assert (a) the
  message is NOT on argv (which would crash for real on Linux) and
  (b) the message IS written to stdin in full.

  Regression target: prior code passed `user_message` as a trailing
  positional argv item. Recovery is exactly the context where users
  paste big diffs and crash logs, so the argv cap was a real risk."""
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "long.jsonl",
  )

  spawn_args = {"argv": None, "stdin_writes": []}

  class _StubStream:
    async def readline(self):
      return b""  # immediate EOF — runner emits done and exits
    async def read(self):
      return b""

  class _StubStdin:
    def write(self, data):
      spawn_args["stdin_writes"].append(data)
    async def drain(self):
      pass
    def close(self):
      pass

  class _StubProc:
    def __init__(self):
      self.stdout = _StubStream()
      self.stderr = _StubStream()
      self.stdin = _StubStdin()
      self.returncode = 0
    def terminate(self): pass
    def kill(self): pass
    async def wait(self):
      return 0

  async def fake_spawn(*cmd, **kwargs):
    spawn_args["argv"] = list(cmd)
    spawn_args["kwargs"] = kwargs
    return _StubProc()

  monkeypatch.setattr(
    "app.recover_chat_runner.asyncio.create_subprocess_exec",
    fake_spawn,
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.shutil.which",
    lambda _: "/fake/claude",
  )

  big_message = "X" * 250_000  # 250 KB — well past the ~128KB argv cap
  gen = rcr.stream_turn(big_message)
  async for _ in gen:
    pass

  # The message must NOT appear anywhere on the argv list.
  assert spawn_args["argv"] is not None
  for arg in spawn_args["argv"]:
    assert big_message not in arg, (
      "user message leaked into argv — would crash for real on Linux"
    )
  # stdin must be wired up and the message must be written to it.
  assert spawn_args["kwargs"].get("stdin") is not None
  combined_stdin = b"".join(spawn_args["stdin_writes"])
  assert combined_stdin == big_message.encode("utf-8")


# ---------------------------------------------------------------------
# Round-5: reset must clear the turn_id replay-dedup set
#
# The reset endpoint wipes recovery_chat.jsonl, after which the next
# user send starts at turn_id=0 again. Without also clearing the
# in-memory _streamed_turn_ids set, the next /stream POST 409s
# immediately because the old generation's ids are still remembered
# — the reset button silently breaks the very next turn.
# ---------------------------------------------------------------------

def test_reset_clears_streamed_turn_ids(
  client, auth_cookie, chat_id, monkeypatch,
):
  """POST /recover/chat/reset must clear the (chat_id, turn_id)
  entries from the replay dedup set for that chat, or the next
  send (which restarts at turn_id=1 — the line just after _meta)
  will 409 against the prior generation's already-streamed ids.
  """
  from app import recover_chat as rc
  rc._streamed_turn_ids.clear()

  # Pre-populate the dedup set as if we'd streamed several turns
  # in THIS chat.
  for i in range(1, 6):
    rc._mark_turn_id_streamed(chat_id, i)
  assert len(rc._streamed_turn_ids) == 5

  # Also seed an entry for a DIFFERENT chat — reset shouldn't clear
  # other chats' ids.
  rc._mark_turn_id_streamed("other-chat", 1)
  assert len(rc._streamed_turn_ids) == 6

  # Reset this chat: clears just THIS chat's entries.
  r = client.post(
    "/recover/chat/reset",
    json={"chat_id": chat_id},
    cookies=auth_cookie,
  )
  assert r.status_code == 200, r.text
  remaining = list(rc._streamed_turn_ids.keys())
  assert remaining == [("other-chat", 1)], (
    f"reset must clear only this chat's ids; got {remaining}"
  )

  # End-to-end: after reset, a fresh send + stream cycle must work.
  from app import recover_chat_runner as rcr

  async def _empty_stream(_message, _provider=None, chat_id=None, model=None):
    if False:
      yield ""

  monkeypatch.setattr(rcr, "stream_turn", _empty_stream)

  send = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "hi"},
    cookies=auth_cookie,
  )
  assert send.status_code == 200, send.text
  fresh_turn_id = send.json()["turn_id"]
  # After reset, the chat has just the _meta line (turn_id=0); the
  # first user append goes to turn_id=1.
  assert fresh_turn_id == 1, (
    f"after reset the first user send must get turn_id=1, got {fresh_turn_id}"
  )

  stream = client.post(
    "/recover/chat/stream",
    json={"chat_id": chat_id, "turn_id": fresh_turn_id},
    cookies=auth_cookie,
  )
  assert stream.status_code == 200, (
    f"post-reset turn_id=1 was 409d as a replay: {stream.text}"
  )


# ---------------------------------------------------------------------
# Round-5: stream_turn cleanup must release the claim slot even if
# _terminate_proc raises an exception.
#
# Regression target: codex review flagged that an OSError other than
# ProcessLookupError, or a CancelledError during real ASGI client
# disconnect, would propagate out of _terminate_proc and abort the
# claim-release that follows. The slot would stay set, and every
# subsequent /stream would 409 with "Another recovery turn is in
# progress." until server restart. The structural fix released the
# slot synchronously BEFORE the await on _terminate_proc, and
# wrapped the await in try/except BaseException. This test exercises
# the contract: even if terminate raises, the next claim succeeds.
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_turn_releases_claim_even_when_terminate_raises(
  monkeypatch, tmp_path,
):
  """Make _terminate_proc raise unexpectedly. The next stream_turn
  must still be able to claim the slot."""
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "terminate_raises.jsonl",
  )

  class _StubStream:
    def __init__(self, lines):
      self._lines = list(lines)
    async def readline(self):
      if not self._lines:
        return b""
      await _asyncio.sleep(0.005)
      return self._lines.pop(0)
    async def read(self):
      return b""

  class _StubProc:
    def __init__(self, *_a, **_kw):
      self.stdout = _StubStream([
        b'{"type":"stream_event","event":{"type":'
        b'"content_block_delta","delta":{"type":'
        b'"text_delta","text":"x"}}}\n',
      ])
      self.stderr = _StubStream([])
      self.stdin = _StubStdin()
      self.returncode = None
    def terminate(self):
      self.returncode = -15
    def kill(self):
      self.returncode = -9
    async def wait(self):
      if self.returncode is None:
        self.returncode = 0
      return self.returncode

  class _StubStdin:
    def write(self, _): pass
    async def drain(self): pass
    def close(self): pass

  async def fake_spawn(*_a, **_kw):
    return _StubProc()

  # Inject a _terminate_proc that always raises. The structural fix
  # absorbs this and still clears the claim.
  async def exploding_terminate(_proc):
    raise OSError("simulated signal-delivery failure")

  monkeypatch.setattr(
    "app.recover_chat_runner.asyncio.create_subprocess_exec",
    fake_spawn,
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.shutil.which", lambda _: "/fake/claude",
  )
  monkeypatch.setattr(rcr, "_terminate_proc", exploding_terminate)

  # Sanity: slot starts empty.
  assert rcr._current_run is None

  # Run a stream to completion (or near it), abandoning it via
  # aclose to fire the finally with our exploding _terminate_proc.
  gen1 = rcr.stream_turn("first")
  await gen1.__anext__()
  await gen1.aclose()

  # Despite _terminate_proc raising, the slot MUST be released.
  assert rcr._current_run is None, (
    "claim slot was not released after _terminate_proc raised"
    " — recovery chat is wedged"
  )

  # End-to-end: a second stream can claim the slot promptly.
  gen2 = rcr.stream_turn("second")
  got_event = False
  async def _consume():
    nonlocal got_event
    async for chunk in gen2:
      got_event = True
      if "done" in chunk:
        break
  await _asyncio.wait_for(_consume(), timeout=2.0)
  assert got_event, "second stream blocked — slot release was incomplete"


# ---------------------------------------------------------------------
# Step 3: provider picker flows through send/stream into stream_turn.
#
# When the client picks `codex` (or `claude`), the runner must receive
# that exact value as its second argument. Lock this in so a future
# refactor that drops the provider param from /stream → stream_turn
# fails loudly.
# ---------------------------------------------------------------------

def test_provider_picker_flows_to_runner(
  client, auth_cookie, chat_id, monkeypatch,
):
  """Client explicitly picks `codex`; runner must receive that
  value as its `provider` argument.

  The chat was created with `claude` (see `chat_id` fixture), so
  this verifies the per-turn override path — the client can
  override even when the chat itself defaults to a different
  provider.
  """
  from app import recover_chat as rc
  from app import recover_chat_runner as rcr
  rc._streamed_turn_ids.clear()

  seen_provider: list = []

  async def _capturing_stream(_message, provider=None, chat_id=None, model=None):
    seen_provider.append(provider)
    if False:
      yield ""

  monkeypatch.setattr(rcr, "stream_turn", _capturing_stream)

  send = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "rescue this"},
    cookies=auth_cookie,
  )
  assert send.status_code == 200
  turn_id = send.json()["turn_id"]

  r = client.post(
    "/recover/chat/stream",
    json={"chat_id": chat_id, "turn_id": turn_id, "provider": "codex"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200, r.text
  assert seen_provider == ["codex"], (
    f"runner did not receive provider=codex, saw {seen_provider}"
  )


def test_provider_picker_unknown_falls_back_to_default(
  client, auth_cookie, chat_id, monkeypatch,
):
  """Client passes a bogus provider; the route normalizes the
  override to None so the runner falls back to the chat's stored
  provider (claude, per the fixture).
  """
  from app import recover_chat as rc
  from app import recover_chat_runner as rcr
  rc._streamed_turn_ids.clear()

  seen_provider: list = []

  async def _capturing_stream(_message, provider=None, chat_id=None, model=None):
    seen_provider.append(provider)
    if False:
      yield ""

  monkeypatch.setattr(rcr, "stream_turn", _capturing_stream)

  send = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "rescue"},
    cookies=auth_cookie,
  )
  turn_id = send.json()["turn_id"]

  r = client.post(
    "/recover/chat/stream",
    json={"chat_id": chat_id, "turn_id": turn_id, "provider": "not-a-real-provider"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200, r.text
  # When the override is invalid, the route uses the chat's stored
  # provider — claude in this fixture.
  assert seen_provider == ["claude"], (
    f"unknown provider should fall back to chat's stored provider, saw {seen_provider}"
  )


# ─── Recovery agent + model selection ─────────────────────────────


def _capture_spawn(monkeypatch):
  """Patches create_subprocess_exec + which() so a spawn records its
  argv into the returned dict and exits immediately. Shared scaffold
  for the model-on-argv tests."""
  from app import recover_chat_runner as rcr

  captured = {"argv": None, "stdin": []}

  class _StubStream:
    async def readline(self):
      return b""
    async def read(self):
      return b""

  class _StubStdin:
    def write(self, data):
      captured["stdin"].append(data)
    async def drain(self):
      pass
    def close(self):
      pass

  class _StubProc:
    def __init__(self):
      self.stdout = _StubStream()
      self.stderr = _StubStream()
      self.stdin = _StubStdin()
      self.returncode = 0
    def terminate(self): pass
    def kill(self): pass
    async def wait(self):
      return 0

  async def fake_spawn(*cmd, **kwargs):
    captured["argv"] = list(cmd)
    return _StubProc()

  monkeypatch.setattr(
    "app.recover_chat_runner.asyncio.create_subprocess_exec", fake_spawn,
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.shutil.which", lambda name: f"/fake/{name}",
  )
  return captured


@pytest.mark.asyncio
async def test_recovery_claude_argv_carries_model(monkeypatch):
  """A chosen model lands on the Claude spawn argv as `--model <id>`."""
  from app import recover_chat_runner as rcr
  captured = _capture_spawn(monkeypatch)

  async for _ in rcr.stream_turn(
    "fix it", provider="claude", model="claude-opus-4-8",
  ):
    pass

  argv = captured["argv"]
  assert argv is not None
  assert "--model" in argv
  assert argv[argv.index("--model") + 1] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_recovery_codex_argv_carries_model(monkeypatch):
  """A chosen model lands on the Codex spawn argv as `-m <id>`, before
  the trailing `-` stdin marker."""
  from app import recover_chat_runner as rcr
  captured = _capture_spawn(monkeypatch)

  async for _ in rcr.stream_turn(
    "fix it", provider="codex", model="gpt-5.5",
  ):
    pass

  argv = captured["argv"]
  assert argv is not None
  assert "-m" in argv
  assert argv[argv.index("-m") + 1] == "gpt-5.5"
  assert argv[-1] == "-"  # stdin marker stays last


@pytest.mark.asyncio
async def test_recovery_no_model_omits_model_flag(monkeypatch):
  """No model selected → no --model on argv (CLI default, the
  pre-model-selection behavior preserved exactly)."""
  from app import recover_chat_runner as rcr
  captured = _capture_spawn(monkeypatch)

  async for _ in rcr.stream_turn("fix it", provider="claude"):
    pass

  assert "--model" not in captured["argv"]


@pytest.mark.asyncio
async def test_recovery_cross_provider_model_ignored(monkeypatch):
  """A Codex model on a Claude turn is dropped (not passed to the CLI)
  — the runner falls back to the CLI default rather than spawning a
  bogus --model."""
  from app import recover_chat_runner as rcr
  captured = _capture_spawn(monkeypatch)

  async for _ in rcr.stream_turn(
    "fix it", provider="claude", model="gpt-5.5",
  ):
    pass

  assert "--model" not in captured["argv"]


def test_recovery_system_prompt_is_single_recovery_posture():
  """There is one recovery prompt with the conservative posture baked
  in — no selectable persona. The prompt names the recovery surface."""
  from app import recover_chat_runner as rcr

  prompt = rcr._system_prompt("abc123")
  assert prompt.startswith("You are running inside the Mobius recovery")
  assert "conservative" in prompt.lower()
  assert "frozen island" in prompt.lower() or "frozen-island" in prompt.lower()


def test_recovery_validation_helpers():
  """is_valid_recovery_model gates the frozen model literals."""
  from app import recover_chat_runner as rcr

  assert rcr.is_valid_recovery_model("claude", None) is True
  assert rcr.is_valid_recovery_model("claude", "") is True
  assert rcr.is_valid_recovery_model("claude", "claude-opus-4-8") is True
  assert rcr.is_valid_recovery_model("claude", "gpt-5.5") is False
  assert rcr.is_valid_recovery_model("codex", "gpt-5.5") is True


def test_recovery_stream_passes_model_to_runner(
  client, auth_cookie, chat_id, monkeypatch,
):
  """The /stream endpoint forwards a valid model into stream_turn; an
  unknown model 400s before the runner is touched."""
  from app import recover_chat as rc
  from app import recover_chat_runner as rcr
  rc._streamed_turn_ids.clear()

  seen = {}

  async def _capturing_stream(
    _message, provider=None, chat_id=None, model=None,
  ):
    seen["model"] = model
    if False:
      yield ""

  monkeypatch.setattr(rcr, "stream_turn", _capturing_stream)

  send = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "rescue"},
    cookies=auth_cookie,
  )
  turn_id = send.json()["turn_id"]

  r = client.post(
    "/recover/chat/stream",
    json={
      "chat_id": chat_id, "turn_id": turn_id,
      "model": "claude-opus-4-8",
    },
    cookies=auth_cookie,
  )
  assert r.status_code == 200, r.text
  assert seen == {"model": "claude-opus-4-8"}


def test_recovery_stream_rejects_unknown_model(
  client, auth_cookie, chat_id,
):
  """An unknown model on /stream 400s (chat is claude per fixture, so
  a Codex model is cross-provider-invalid)."""
  send = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "rescue"},
    cookies=auth_cookie,
  )
  turn_id = send.json()["turn_id"]
  r = client.post(
    "/recover/chat/stream",
    json={"chat_id": chat_id, "turn_id": turn_id, "model": "bogus-model"},
    cookies=auth_cookie,
  )
  assert r.status_code == 400
  assert "model" in r.json()["detail"].lower()


def test_recovery_new_chat_validates_model(
  client, auth_cookie,
):
  """/recover/chat/new 400s on an unknown model; accepts a valid one
  (without persisting it — it's per-turn)."""
  ok = client.post(
    "/recover/chat/new",
    json={"provider": "claude", "model": "claude-opus-4-8"},
    cookies=auth_cookie,
  )
  assert ok.status_code == 200

  bad_model = client.post(
    "/recover/chat/new",
    json={"provider": "claude", "model": "nope"},
    cookies=auth_cookie,
  )
  assert bad_model.status_code == 400


def test_recovery_page_renders_unified_model_select(
  client, auth_cookie,
):
  """The recovery HTML exposes ONE unified model dropdown (model implies
  provider) — no separate provider radio, no agent picker. Options encode
  provider+model as `provider:model`, with a CLI-default row per provider."""
  r = client.get("/recover/chat", cookies=auth_cookie)
  assert r.status_code == 200
  assert "rc-model-sel" in r.text
  # CLI-default entries per provider, encoded as `provider:` (empty model).
  assert 'value="claude:"' in r.text
  assert 'value="codex:"' in r.text
  assert "CLI default" in r.text
  # A specific model is encoded provider-qualified, not bare.
  assert 'value="claude:claude-opus-4-8"' in r.text
  # The redundant provider radio and the agent picker are gone.
  assert 'name="rc-prov"' not in r.text
  assert "rc-agent-sel" not in r.text


def test_provider_status_helpers(monkeypatch, tmp_path):
  """`provider_status` reflects which credential files exist;
  `default_provider` prefers claude when both are configured."""
  from app import recover_chat_runner as rcr

  fake_claude = tmp_path / "claude"
  fake_codex = tmp_path / "codex"
  fake_claude.mkdir()
  fake_codex.mkdir()

  monkeypatch.setattr(rcr, "CLAUDE_CONFIG_PATH", fake_claude)
  monkeypatch.setattr(rcr, "CODEX_CONFIG_PATH", fake_codex)

  # Nothing configured yet.
  assert rcr.provider_status() == {"claude": False, "codex": False}

  # Only codex configured.
  (fake_codex / "auth.json").write_text("{}")
  assert rcr.provider_status() == {"claude": False, "codex": True}
  assert rcr.default_provider() == "codex"

  # Both configured → claude wins by preference order.
  (fake_claude / ".credentials.json").write_text("{}")
  assert rcr.provider_status() == {"claude": True, "codex": True}
  assert rcr.default_provider() == "claude"


# ---------------------------------------------------------------------
# Post-review regression locks (codex caught these in the 186ff8a review)
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_codex_signature_accepts_chat_id(monkeypatch, tmp_path):
  """The dispatcher calls _spawn_codex(user_message, claim, chat_id),
  but an earlier draft only declared (user_message, claim). Codex
  caught the arity mismatch in review — every Codex recovery turn
  would have crashed silently with TypeError.

  This test exercises just the signature, not the full subprocess
  spawn — we stub `asyncio.create_subprocess_exec` so the test
  doesn't need a real codex binary."""
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_CHATS_DIR",
    tmp_path / "chats",
  )

  class _StubStdout:
    async def readline(self):
      return b""  # immediate EOF

  class _StubStdin:
    def write(self, _): pass
    async def drain(self): pass
    def close(self): pass

  class _StubProc:
    def __init__(self):
      self.stdout = _StubStdout()
      self.stderr = _StubStdout()
      self.stdin = _StubStdin()
      self.returncode = None
    def terminate(self): self.returncode = -15
    def kill(self): self.returncode = -9
    async def wait(self):
      if self.returncode is None:
        self.returncode = 0
      return self.returncode

  async def fake_spawn(*_a, **_kw):
    return _StubProc()

  monkeypatch.setattr(
    "app.recover_chat_runner.asyncio.create_subprocess_exec",
    fake_spawn,
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.shutil.which",
    lambda _: "/fake/codex",
  )

  chat_id = rcr.create_chat("codex")
  events = []
  async for chunk in rcr.stream_turn("hi", provider="codex", chat_id=chat_id):
    events.append(chunk)
  # The 3-arg call into _spawn_codex must have succeeded — we expect
  # at least a `done` event back.
  assert any('"done"' in e for e in events), (
    f"_spawn_codex(user_message, claim, chat_id) signature broken; events={events!r}"
  )


def test_recover_oauth_rejects_stale_cookie_post_factory_reset(
  client, auth_cookie,
):
  """Factory reset deletes the owner row but the recovery cookie's
  HMAC stays valid for ~1h. The OAuth endpoints must re-check the
  owner row, not just the HMAC, or a stolen cookie / second tab can
  keep rewriting /data/cli-auth/ post-reset.

  Codex review flagged: recover_oauth.py's _require_recovery_session
  was only checking the HMAC. Now it also checks _owner_exists.
  """
  # Sanity: with the owner present, OAuth start endpoint works.
  r = client.post("/recover/provider/claude/start", cookies=auth_cookie)
  assert r.status_code == 200

  # Simulate factory reset: delete the owner row.
  from app.database import SessionLocal
  from app import models
  db = SessionLocal()
  db.query(models.Owner).filter(models.Owner.username == "tester").delete()
  db.commit()
  db.close()

  # Same cookie, owner gone — must 401 on every OAuth surface.
  r = client.post("/recover/provider/claude/start", cookies=auth_cookie)
  assert r.status_code == 401, (
    f"stale cookie must not start Claude OAuth post-reset, got {r.status_code}"
  )
  r = client.post(
    "/recover/provider/claude/code",
    json={"code": "x"},
    cookies=auth_cookie,
  )
  assert r.status_code == 401
  r = client.post("/recover/provider/codex/start", cookies=auth_cookie)
  assert r.status_code == 401
  r = client.get("/recover/provider/codex/status", cookies=auth_cookie)
  assert r.status_code == 401


# ---------------------------------------------------------------------
# Post-review MEDIUM fixes (codex 186ff8a review)
# ---------------------------------------------------------------------

def test_send_surfaces_disk_failure_as_500(
  client, auth_cookie, chat_id, monkeypatch,
):
  """If append_log returns -1 (its 'disk error' sentinel), the route
  must 500 — not return `{"status":"queued","turn_id":-1}` and let
  the client think the message was persisted.

  Codex caught this: a swallowed -1 would propagate to /stream
  where user_message_by_id(-1) returns None, and the user just
  sees a generic "no message in log" without ever knowing the
  earlier write failed.
  """
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(rcr, "append_log", lambda *a, **kw: -1)
  r = client.post(
    "/recover/chat/send",
    json={"chat_id": chat_id, "message": "disk full"},
    cookies=auth_cookie,
  )
  assert r.status_code == 500
  assert "persist" in r.text.lower() or "failed" in r.text.lower()


def test_legacy_migration_atomic_under_partial_copy(monkeypatch, tmp_path):
  """If the legacy → multi-chat copy is interrupted mid-write,
  the next list_chats() call must re-attempt rather than treating
  the partial file as the migrated artifact.

  Codex caught this: the old code wrote directly to legacy.jsonl,
  so a copy that died halfway left a half-good file that future
  calls assumed was complete. The atomic-rename fix writes to
  .partial first; on failure the partial is cleaned up so the
  next list_chats() retries.
  """
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(rcr, "RECOVERY_CHATS_DIR", tmp_path / "chats")
  legacy = tmp_path / "legacy_log.jsonl"
  legacy.write_text(
    '{"role":"user","content":"first","ts":1.0}\n'
    '{"role":"assistant","content":"reply","ts":2.0}\n'
    '{"role":"user","content":"second","ts":3.0}\n'
  )
  monkeypatch.setattr(rcr, "RECOVERY_LOG_PATH", legacy)

  # Make shutil.copyfileobj fail mid-write to simulate disk full.
  original_copy = rcr.shutil.copyfileobj

  def boom(*_a, **_kw):
    raise OSError("simulated disk full")

  monkeypatch.setattr(rcr.shutil, "copyfileobj", boom)
  # First attempt — should silently fail (best-effort migration)
  # but NOT leave a partial legacy.jsonl behind.
  rcr.list_chats()
  partial = tmp_path / "chats" / "legacy.jsonl.partial"
  target = tmp_path / "chats" / "legacy.jsonl"
  assert not partial.exists(), "partial copy must be cleaned up on failure"
  assert not target.exists(), "failed copy must not produce a target file"
  assert legacy.is_file(), "legacy source must be preserved when copy fails"

  # Restore copyfileobj and retry: should succeed cleanly.
  monkeypatch.setattr(rcr.shutil, "copyfileobj", original_copy)
  chats = rcr.list_chats()
  assert any(c["chat_id"] == "legacy" for c in chats)
  # Source removed only after the atomic rename succeeded.
  assert not legacy.exists()
  # And the migrated file has all 3 original lines + the _meta line.
  migrated_lines = target.read_text().strip().splitlines()
  assert len(migrated_lines) == 4  # _meta + 3 entries


# ---------------------------------------------------------------------
# Post-review #3 (Claude reviewer): security contract locks
# ---------------------------------------------------------------------

@pytest.mark.parametrize("endpoint,payload_extra", [
  ("/recover/chat/send", {"message": "x"}),
  ("/recover/chat/delete", {}),
  ("/recover/chat/reset", {}),
])
def test_path_traversal_chat_id_rejected(
  client, auth_cookie, endpoint, payload_extra,
):
  """A chat_id like '../etc/passwd' must be rejected at every endpoint
  that takes one. The defense lives in _validate_chat_id (regex
  allowlist) inside chat_log_path; this test pins the contract so
  a future refactor that bypasses chat_log_path opens a real
  traversal that the test catches.

  Claude reviewer flagged: the chat_log_path validator is the
  load-bearing security check; without a regression test, a refactor
  could plug in raw path joins and the security failure would be
  invisible.
  """
  body = {"chat_id": "../etc/passwd", **payload_extra}
  r = client.post(endpoint, json=body, cookies=auth_cookie)
  # Any non-2xx is acceptable — current paths return 400 (delete,
  # reset) or 404 (send). The contract is "not 2xx", not the exact
  # status, since the helper that raises ValueError happens to be
  # caught with different statuses per endpoint.
  assert r.status_code >= 400, (
    f"{endpoint}: traversal chat_id must be rejected, got {r.status_code}"
  )
  assert r.status_code < 500, (
    f"{endpoint}: traversal must be a CLIENT error, not 5xx (server error)"
  )


def test_claude_oauth_rejects_state_mismatch(client, auth_cookie):
  """OAuth's state parameter is the CSRF guard for the Claude PKCE
  flow. A code payload that carries `state=WRONG` (i.e. forged by an
  attacker who somehow tricked the user into pasting a crafted
  callback URL) must be rejected.

  Claude reviewer flagged: the state-mismatch branch in
  recover_oauth.py was untested.
  """
  # Establish a PKCE flow so _active_pkce is set with a known state.
  start = client.post("/recover/provider/claude/start", cookies=auth_cookie)
  assert start.status_code == 200

  # Submit a code with an explicitly wrong state in the URL fragment.
  # _extract_provider_code_and_state will parse `state=WRONG` and the
  # downstream comparison must reject.
  r = client.post(
    "/recover/provider/claude/code",
    json={"code": "https://platform.claude.com/oauth/code/callback?code=abc&state=DEFINITELY-WRONG"},
    cookies=auth_cookie,
  )
  assert r.status_code == 403, (
    f"state mismatch must return 403, got {r.status_code}: {r.text}"
  )


@pytest.mark.asyncio
async def test_codex_login_times_out_if_device_code_never_appears(
  client, auth_cookie, monkeypatch,
):
  """If the codex subprocess hangs (never emits a device code), the
  /codex/start endpoint must return 500 within the configured 15s
  budget — not hang the request indefinitely.

  Claude reviewer flagged: the timeout branch in recover_oauth.py
  (lines ~361-368) was the only protection against a hung CLI and
  had zero test coverage.
  """
  # Stub create_subprocess_exec to return a proc whose stdout never
  # yields a line matching the device-code pattern.
  import asyncio as _asyncio

  class _HangingStdout:
    async def readline(self):
      # Sleep beyond the timeout deadline so the asyncio.timeout(15)
      # fires. We monkey-patch the timeout below to keep the test fast.
      await _asyncio.sleep(60)
      return b""

  class _HangingProc:
    def __init__(self):
      self.stdout = _HangingStdout()
      self.returncode = None
    def kill(self):
      self.returncode = -9
    async def wait(self):
      if self.returncode is None:
        self.returncode = 0
      return self.returncode

  async def fake_spawn(*_a, **_kw):
    return _HangingProc()

  from app import recover_oauth as roa
  monkeypatch.setattr(
    "app.recover_oauth.asyncio.create_subprocess_exec",
    fake_spawn,
  )
  # Shrink the timeout from 15s to 0.5s so the test runs fast.
  # The route uses `async with asyncio.timeout(15)` so we swap the
  # asyncio.timeout factory for one with a shorter deadline.
  original_timeout = _asyncio.timeout

  def fast_timeout(seconds):
    return original_timeout(0.5)

  monkeypatch.setattr("app.recover_oauth.asyncio.timeout", fast_timeout)

  # Call the endpoint. With the hanging proc + 0.5s timeout, the
  # route should kill the proc and 500.
  r = client.post("/recover/provider/codex/start", cookies=auth_cookie)
  assert r.status_code == 500
  assert "timed out" in r.text.lower() or "timeout" in r.text.lower()


def test_system_prompt_lists_all_frozen_island_files():
  """The recovery agent's system prompt enumerates the frozen-island
  paths so the agent doesn't waste turns trying to edit them. Codex
  reviewer flagged: the list was missing recover_oauth.py, config.py,
  and models.py even though all three are in protected-files.txt
  and chmod 444 root-owned.
  """
  from app import recover_chat_runner as rcr
  prompt = rcr._system_prompt("abc")
  expected = [
    "main.py",
    "routes/__init__.py",
    "auth.py",
    "database.py",
    "config.py",
    "models.py",
    "recover_chat.py",
    "recover_chat_runner.py",
    "recover_auth.py",
    "recover_oauth.py",
    "entrypoint.sh",
    "recovery_restore.sh",
  ]
  for path in expected:
    assert path in prompt, f"frozen-island list must include {path}"


def test_terminate_active_run_for_only_kills_matching_chat(monkeypatch):
  """delete_chat must kill the rescue subprocess if (and only if) the
  active run is for the chat being deleted. A run for a different
  chat must be left alone. Codex reviewer flagged: an in-flight
  rescue survived chat deletion and its final append_log silently
  failed."""
  from app import recover_chat_runner as rcr

  killed = {"flag": False}

  class _FakeProc:
    returncode = None
    def kill(self):
      killed["flag"] = True

  # Active run for chat "A". Deleting chat "B" should NOT kill it.
  rcr._current_run = {"proc": _FakeProc(), "chat_id": "A"}
  try:
    assert rcr.terminate_active_run_for("B") is False
    assert killed["flag"] is False, "deleting chat B must not kill chat A's run"
    assert rcr._current_run is not None, "claim for chat A must survive"

    # Now delete chat "A" — should kill.
    assert rcr.terminate_active_run_for("A") is True
    assert killed["flag"] is True
    assert rcr._current_run is None
  finally:
    rcr._current_run = None


def test_terminate_active_codex_login_kills_and_clears_state():
  """Factory reset must kill an in-flight `codex login --device-auth`
  subprocess. Otherwise a delayed auth completion can recreate
  /data/cli-auth/codex/auth.json on an instance that was just
  factory-reset. Codex reviewer caught this as HIGH severity."""
  from app import recover_oauth

  killed = {"flag": False}

  class _FakeProc:
    returncode = None
    def kill(self):
      killed["flag"] = True

  recover_oauth._codex_login_procs["active"] = _FakeProc()
  recover_oauth._codex_login_status.pop("result", None)
  try:
    result = recover_oauth.terminate_active_codex_login()
    assert result is True
    assert killed["flag"] is True
    assert "active" not in recover_oauth._codex_login_procs
    assert recover_oauth._codex_login_status.get("result") == "failed"

    # No-op when there's nothing active.
    assert recover_oauth.terminate_active_codex_login() is False
  finally:
    recover_oauth._codex_login_procs.pop("active", None)
    recover_oauth._codex_login_status.pop("result", None)


def test_recover_page_redirects_to_login_on_stale_cookie(
  client, auth_cookie,
):
  """GET /recover with a valid HMAC cookie but a deleted owner row
  must render the login form, not the dashboard. Codex reviewer
  caught the asymmetry: POST endpoints re-check owner-existence,
  GET /recover was only checking the HMAC.
  """
  # Sanity: dashboard renders while the owner exists.
  r = client.get("/recover", cookies=auth_cookie)
  assert r.status_code == 200
  assert "Restore" in r.text or "Backup" in r.text or "Recovery" in r.text

  # Delete the owner.
  from app.database import SessionLocal
  from app import models
  db = SessionLocal()
  db.query(models.Owner).filter(models.Owner.username == "tester").delete()
  db.commit()
  db.close()

  # Same cookie, no owner — must NOT render the dashboard.
  r = client.get("/recover", cookies=auth_cookie)
  assert r.status_code == 200
  # The login form has a password field; the dashboard does not.
  assert 'type="password"' in r.text.lower() or 'name="password"' in r.text.lower()


def test_system_prompt_references_per_chat_log_path():
  """Multi-chat moved the recovery log from /data/recovery_chat.jsonl
  (legacy single file) to /data/recovery/chats/<chat_id>.jsonl.
  The system prompt must point the agent at the CURRENT per-chat
  path so it can read prior turns; pointing at the legacy path
  silently breaks multi-turn context (Claude review caught this).
  """
  from app import recover_chat_runner as rcr
  prompt = rcr._system_prompt("abc123")
  assert "/data/recovery/chats/abc123.jsonl" in prompt, (
    "system prompt must include the per-chat log path so the agent "
    "knows which file to read for prior turns"
  )
  assert "/data/recovery_chat.jsonl" not in prompt, (
    "system prompt must not reference the LEGACY single-file path; "
    "that file is unlinked after migration"
  )

  # Defensive: no chat_id → fall back, but DON'T point at the legacy
  # singleton (we want the agent to discover the multi-chat layout
  # rather than read a deleted file).
  fallback = rcr._system_prompt(None)
  assert "/data/recovery/chats/" in fallback


def test_codex_spawn_prepends_system_prompt_to_user_message(
  monkeypatch, tmp_path,
):
  """Codex `exec --json` has no --system-prompt flag — the recovery
  agent needs its context prepended to the stdin payload. Without
  this, the Codex rescue agent has no knowledge of the recovery
  surface, write-surface layout, or per-chat log path.

  Claude review flagged: _spawn_codex was passing only the user
  message via stdin, dropping the system prompt entirely.
  """
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_CHATS_DIR",
    tmp_path / "chats",
  )

  written: list[bytes] = []

  class _RecordingStdin:
    def write(self, data):
      written.append(data)
    async def drain(self): pass
    def close(self): pass

  class _StubStdout:
    async def readline(self):
      return b""

  class _StubProc:
    def __init__(self):
      self.stdin = _RecordingStdin()
      self.stdout = _StubStdout()
      self.stderr = _StubStdout()
      self.returncode = None
    def kill(self): self.returncode = -9
    def terminate(self): self.returncode = -15
    async def wait(self):
      if self.returncode is None:
        self.returncode = 0
      return self.returncode

  async def fake_spawn(*_a, **_kw):
    return _StubProc()

  monkeypatch.setattr(
    "app.recover_chat_runner.asyncio.create_subprocess_exec",
    fake_spawn,
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.shutil.which", lambda _: "/fake/codex",
  )

  chat_id = rcr.create_chat("codex")
  events = []
  async def _drive():
    async for chunk in rcr.stream_turn(
      "diagnose the broken backend", provider="codex", chat_id=chat_id,
    ):
      events.append(chunk)
  _asyncio.run(_drive())

  combined = b"".join(written).decode()
  assert "diagnose the broken backend" in combined, (
    "user message must reach stdin"
  )
  assert "running inside the Mobius recovery chat" in combined, (
    "system prompt must be prepended so Codex has recovery context"
  )
  assert f"/data/recovery/chats/{chat_id}.jsonl" in combined, (
    "system prompt must include the per-chat log path"
  )


def test_factory_reset_terminates_active_recovery_run(monkeypatch):
  """Factory reset must kill an in-flight recovery rescue agent so
  it can't keep writing to /data/* after the reset wipes credentials.

  Codex review flagged: _require_session re-checks the owner row
  before each HTTP request, but a stream that's already running
  keeps its subprocess alive. terminate_active_run() bridges the gap.
  """
  from app import recover_chat_runner as rcr
  from app.routes import recover as rec_routes

  # Pre-populate the runner's claim slot with a fake "active" proc
  # so terminate_active_run has something to kill.
  killed = {"flag": False}

  class _FakeProc:
    def kill(self):
      killed["flag"] = True

  rcr._current_run = {"proc": _FakeProc()}
  try:
    # Stub out the destructive side-effects of factory_reset so the
    # test doesn't touch real files — we only care about the
    # terminate-active-run call.
    monkeypatch.setattr(rec_routes, "_db_delete_all_apps", lambda: None)
    monkeypatch.setattr(rec_routes, "_db_delete_all_owners", lambda: None)
    monkeypatch.setattr(rec_routes, "_db_delete_all_user_content", lambda: None)
    monkeypatch.setattr(rec_routes, "_rm_tree", lambda _p: None)

    from pathlib import Path as _Path
    rec_routes._action_factory_reset(_Path("/tmp/nonexistent-test-data"))

    assert killed["flag"], "factory_reset must kill the active rescue proc"
    assert rcr._current_run is None, "claim slot must be cleared post-reset"
  finally:
    rcr._current_run = None


def test_factory_reset_clears_user_content(monkeypatch):
  """Factory reset must wipe chats, notifications, and push subscriptions (chats
  are NOT owner-scoped, so a leftover would show the prior owner's history to the
  next one) and the /data/chats attachments dir — not just apps + owners."""
  from app.routes import recover as rec_routes
  from pathlib import Path as _Path
  called = {"user_content": False}
  monkeypatch.setattr(rec_routes, "_db_delete_all_apps", lambda: None)
  monkeypatch.setattr(rec_routes, "_db_delete_all_owners", lambda: None)
  monkeypatch.setattr(
    rec_routes, "_db_delete_all_user_content",
    lambda: called.__setitem__("user_content", True),
  )
  removed: list[str] = []
  monkeypatch.setattr(rec_routes, "_rm_tree", lambda p: removed.append(p.name))
  rec_routes._action_factory_reset(_Path("/tmp/nonexistent-test-data"))
  assert called["user_content"], "reset must delete chats/notifications/push rows"
  assert "chats" in removed, "reset must wipe the /data/chats attachments dir"
  assert "agent-browser-profiles" in removed, (
    "reset must wipe per-chat Chromium profiles (browser session data)"
  )


# ---------------------------------------------------------------------
# Review round #5 regressions
# ---------------------------------------------------------------------

def test_codex_status_is_idempotent_across_concurrent_polls(monkeypatch):
  """codex_status used to `pop("result")`, so the first poller after
  completion consumed the terminal state and every subsequent
  poller (or concurrent tab / EventSource reconnect) read `idle`
  instead of `complete`. The second tab would then hang on
  "Waiting…" forever. Codex review round #5 caught this.
  """
  from app import recover_oauth

  recover_oauth._codex_login_procs.pop("active", None)
  recover_oauth._codex_login_status["result"] = "complete"
  # Stub the auth wrapper — we're testing read semantics, not auth.
  monkeypatch.setattr(
    "app.recover_oauth._require_recovery_session", lambda _t: None,
  )
  # Disable slowapi rate-limiting: the test calls the route function
  # directly with a Mock, so the limiter's request inspection raises.
  # The rate limit is added per security review round 2; this test
  # is unit-level and bypasses it intentionally.
  monkeypatch.setattr(recover_oauth._limiter, "enabled", False)

  try:
    from unittest.mock import Mock
    req = Mock()
    s1 = recover_oauth.codex_status(req, moebius_recover="x")
    s2 = recover_oauth.codex_status(req, moebius_recover="x")
    s3 = recover_oauth.codex_status(req, moebius_recover="x")
    assert s1 == {"state": "complete"}, "first poll must see complete"
    assert s2 == {"state": "complete"}, (
      "second concurrent poll must STILL see complete — pop would race"
    )
    assert s3 == {"state": "complete"}, "third poll likewise"
    # And the underlying state must STILL be there (non-destructive read).
    assert recover_oauth._codex_login_status.get("result") == "complete"
  finally:
    recover_oauth._codex_login_status.pop("result", None)


def test_codex_status_cleared_only_by_next_start():
  """After codex_status is non-destructive, the `result` key must
  be cleared when /provider/codex/start kicks off a new flow —
  otherwise a stale `complete` from a prior run would leak into
  the next attempt's first poll. The clearing already lives in
  codex_start (line `_codex_login_status.pop("result", None)`);
  this test pins that behavior so it can't regress.
  """
  from app import recover_oauth
  # Inspect codex_start's source for the clearing call. We use
  # textual assertion rather than re-running the full subprocess
  # spawn (which would need codex on PATH + working network).
  import inspect
  src = inspect.getsource(recover_oauth.codex_start)
  assert '_codex_login_status.pop("result"' in src, (
    "codex_start must clear stale result so the next status poll "
    "doesn't see the previous run's terminal state"
  )


def test_codex_start_registers_proc_before_readline_loop():
  """The original codex_start spawned the subprocess at line 393
  but didn't publish it to _codex_login_procs until line 438,
  AFTER the 15s readline loop + parsing. A factory reset hitting
  that window called terminate_active_codex_login on an empty
  registry, killed nothing, and let the login complete + rewrite
  /data/cli-auth/codex/auth.json after the reset wiped it. Codex
  review round #5 caught this as the security gap the prior round
  was supposed to close.

  Source-level assertion: `_codex_login_procs["active"] = proc`
  must appear BEFORE the first `await proc.stdout.readline()` in
  the function body. Source inspection is the right tool here
  because the runtime path goes through slowapi's request-typed
  decorator and the cleanup branches we want to verify are timeout
  / parse-failure paths that are awkward to drive end-to-end.
  """
  import inspect
  from app import recover_oauth

  src = inspect.getsource(recover_oauth.codex_start)
  register_pos = src.find('_codex_login_procs["active"] = proc')
  readline_pos = src.find("proc.stdout.readline()")

  assert register_pos != -1, (
    "codex_start must publish the proc to _codex_login_procs so a "
    "concurrent factory reset can kill it"
  )
  assert readline_pos != -1, "expected a readline call in codex_start"
  assert register_pos < readline_pos, (
    "registration must happen BEFORE the readline loop — otherwise "
    "terminate_active_codex_login can't find the proc during the "
    "startup window and a delayed login completion can rewrite "
    "/data/cli-auth/codex/auth.json post-reset"
  )

  # And both timeout + parse-failure branches must clean up the
  # registry entry (since the watcher hasn't started yet).
  cleanup = '_codex_login_procs.pop("active", None)'
  # The clearing-on-start call counts as 1. The timeout-branch and
  # parse-failure-branch cleanups add 2 more, for a minimum of 3.
  assert src.count(cleanup) >= 3, (
    "both timeout and parse-failure branches must pop the registry "
    "entry so a failed startup doesn't leak a stale active proc"
  )


def test_terminate_active_run_for_cancels_during_spawn_startup(monkeypatch):
  """The original terminate_active_run_for only killed when
  claim["proc"] was already attached. _claim_run installs the
  claim with proc=None and the subprocess is attached LATER, after
  create_subprocess_exec returns. A delete_chat landing in that
  window cleared _current_run and reported success, but the not-
  yet-attached subprocess kept starting and ran against the now-
  deleted chat. Codex review round #5 caught this race.

  We simulate the window: install a claim with proc=None for chat
  "A", call terminate_active_run_for("A"), then verify the
  cancellation flag is set so the eventual proc-attach can no-op.
  """
  from app import recover_chat_runner as rcr

  claim = {"proc": None, "chat_id": "A"}
  rcr._current_run = claim
  try:
    result = rcr.terminate_active_run_for("A")
    assert result is True, (
      "terminate must succeed even when proc isn't attached yet"
    )
    assert claim.get("cancelled") is True, (
      "the spawn-task sentinel must be set so the in-flight spawn "
      "kills itself when it tries to publish the proc"
    )
    assert rcr._current_run is None, "claim slot must be released"
  finally:
    rcr._current_run = None


def test_spawn_kills_proc_when_cancelled_during_spawn(monkeypatch, tmp_path):
  """End-to-end: if a claim is cancelled while _spawn_claude is
  awaiting create_subprocess_exec, the spawn task must kill the
  just-spawned proc instead of publishing it and continuing to
  stream against a deleted chat. Codex review round #5 follow-up.
  """
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr

  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_CHATS_DIR",
    tmp_path / "chats",
  )

  killed = {"flag": False}

  class _RecordingStdin:
    def write(self, _): pass
    async def drain(self): pass
    def close(self): pass

  class _StubStdout:
    async def readline(self):
      return b""
    async def read(self):
      return b""

  class _StubProc:
    def __init__(self):
      self.stdin = _RecordingStdin()
      self.stdout = _StubStdout()
      self.stderr = _StubStdout()
      self.returncode = None
    def kill(self):
      killed["flag"] = True
      self.returncode = -9
    async def wait(self):
      if self.returncode is None:
        self.returncode = 0
      return self.returncode

  async def _fake_spawn(*_a, **_kw):
    # Simulate the race: the claim was just cancelled (e.g. by a
    # delete_chat that landed during the await) BEFORE the spawn
    # returns. By the time create_subprocess_exec resolves, the
    # cancellation flag is already set.
    claim["cancelled"] = True
    return _StubProc()

  monkeypatch.setattr(
    "app.recover_chat_runner.asyncio.create_subprocess_exec",
    _fake_spawn,
  )
  monkeypatch.setattr(
    "app.recover_chat_runner.shutil.which", lambda _: "/fake/claude",
  )

  chat_id = rcr.create_chat("claude")
  claim = rcr._claim_run(chat_id=chat_id)
  assert claim is not None

  try:
    events = []
    async def _drive():
      async for chunk in rcr._spawn_claude("hi", claim, chat_id):
        events.append(chunk)
    _asyncio.run(_drive())

    assert killed["flag"], (
      "spawn task must kill the just-spawned proc when it observes "
      "the cancellation flag — otherwise the not-yet-attached proc "
      "would run against the deleted chat"
    )
    # Only a `done` SSE should escape; no text events.
    assert any('"done"' in e for e in events), (
      "cancelled spawn must still emit done so the SSE client closes"
    )
  finally:
    rcr._current_run = None


def test_system_prompt_fallback_does_not_emit_read_glob():
  """The chat_id=None fallback path used to emit `Read /data/recovery/
  chats/*.jsonl  (multi-chat layout — list-chats first)` as a
  literal Read instruction. The Claude CLI's Read tool does not
  expand globs and would waste a tool call on a guaranteed file-
  not-found. The fallback must instead tell the agent to LIST the
  directory rather than READ a glob. Both Claude (round #5) and
  Codex flagged this; Codex confirmed it's unreachable from
  production HTTP but it remains a defensive nit for non-HTTP
  callers.
  """
  from app import recover_chat_runner as rcr
  fallback = rcr._system_prompt(None)

  # The defensive fallback must NOT direct the agent to Read a
  # glob path (which the Read tool would treat literally).
  assert "Read /data/recovery/chats/*.jsonl" not in fallback, (
    "fallback must not ask the agent to Read a glob path"
  )
  # And must mention the directory so the agent can find its way.
  assert "/data/recovery/chats/" in fallback


def test_system_prompt_with_chat_id_still_emits_read_instruction():
  """Conversely, the normal path (chat_id supplied) MUST still
  include the `Read <per-chat-path>` instruction — that's how the
  recovery agent picks up prior turns. Regression guard for the
  defensive-fallback edit so it doesn't accidentally drop the
  instruction from the happy path.
  """
  from app import recover_chat_runner as rcr
  prompt = rcr._system_prompt("xyz789")
  assert "Read /data/recovery/chats/xyz789.jsonl" in prompt, (
    "the chat_id path must still tell the agent to read its log"
  )


# =====================================================================
# P0 / P1 / P2 fixes — recovery subsystem hardening
# =====================================================================

# ---------------------------------------------------------------------
# P0 — recovery HMAC key independence from SECRET_KEY
#
# A SECRET_KEY rotation (the documented outage mode) must NOT
# invalidate recovery cookies. The recovery HMAC is keyed on
# /data/.recovery-secret, not SECRET_KEY.
# ---------------------------------------------------------------------

class TestRecoverySecretIndependence:
  """Recovery cookie validity must be decoupled from SECRET_KEY."""

  def test_recovery_cookie_survives_secret_key_rotation(self, tmp_path, monkeypatch):
    """Changing SECRET_KEY must NOT invalidate a recovery cookie.

    This is the core P0 guarantee: when SECRET_KEY drifts (the
    documented outage mode that invalidates all JWTs), the recovery
    surface — the only way to get back in — must remain reachable.
    """
    # Point the recovery-secret path at a known temp file.
    monkeypatch.setattr(recover_auth, "_RECOVERY_SECRET_PATH", tmp_path / ".recovery-secret")

    # Create a cookie under the initial key.
    tok = recover_auth.create_session_token("alice")
    assert recover_auth.decode_session_token(tok) == "alice"

    # Rotate SECRET_KEY (the JWT key — simulates the documented outage).
    monkeypatch.setenv("SECRET_KEY", "z" * 64)

    # The recovery cookie must still be valid — it uses the recovery
    # secret, not SECRET_KEY.
    assert recover_auth.decode_session_token(tok) == "alice", (
      "recovery cookie became invalid after SECRET_KEY rotation — "
      "the recovery surface is now unreachable during the very outage "
      "it exists to fix"
    )

  def test_different_recovery_secret_invalidates_cookie(self, tmp_path, monkeypatch):
    """Two instances with different recovery secrets must not accept
    each other's cookies — the secret IS the identity boundary."""
    secret_path = tmp_path / ".recovery-secret"
    monkeypatch.setattr(recover_auth, "_RECOVERY_SECRET_PATH", secret_path)

    tok = recover_auth.create_session_token("bob")

    # Overwrite the recovery secret — simulates a factory reset
    # followed by a fresh secret generation on next use.
    secret_path.write_text("0" * 64, encoding="ascii")
    secret_path.chmod(0o600)

    assert recover_auth.decode_session_token(tok) is None, (
      "old cookie must be invalid after the recovery secret changes"
    )

  def test_recovery_secret_generated_on_first_use(self, tmp_path, monkeypatch):
    """_recovery_secret_bytes must create /data/.recovery-secret when
    it doesn't exist, and the file must be mode 600."""
    secret_path = tmp_path / ".recovery-secret"
    assert not secret_path.exists()
    monkeypatch.setattr(recover_auth, "_RECOVERY_SECRET_PATH", secret_path)

    key = recover_auth._recovery_secret_bytes()
    assert secret_path.exists()
    assert len(key) >= 32, "generated key must be at least 32 bytes"
    mode = oct(secret_path.stat().st_mode & 0o777)
    assert mode == "0o600", f"recovery secret must be mode 600, got {mode}"

  def test_recovery_secret_stable_across_calls(self, tmp_path, monkeypatch):
    """_recovery_secret_bytes must return the same value on every call
    (reads the file each time; does NOT regenerate if present)."""
    secret_path = tmp_path / ".recovery-secret"
    monkeypatch.setattr(recover_auth, "_RECOVERY_SECRET_PATH", secret_path)

    k1 = recover_auth._recovery_secret_bytes()
    k2 = recover_auth._recovery_secret_bytes()
    assert k1 == k2

  def test_secret_key_env_empty_with_recovery_secret_present(self, tmp_path, monkeypatch):
    """When SECRET_KEY is empty but /data/.recovery-secret exists,
    cookie creation and validation must still work (the recovery secret
    is the only key that matters for recovery cookies)."""
    secret_path = tmp_path / ".recovery-secret"
    monkeypatch.setattr(recover_auth, "_RECOVERY_SECRET_PATH", secret_path)
    # Ensure the secret file exists first.
    recover_auth._recovery_secret_bytes()

    # Now clear SECRET_KEY. create_session_token uses _recovery_secret_bytes
    # (not SECRET_KEY), so it must succeed even if SECRET_KEY is empty.
    monkeypatch.setenv("SECRET_KEY", "")
    tok = recover_auth.create_session_token("carol")
    # Decode must also work — it uses the same recovery secret.
    assert recover_auth.decode_session_token(tok) == "carol"


# ---------------------------------------------------------------------
# P0 — backup zip completeness
# ---------------------------------------------------------------------

class TestBackupZipCompleteness:
  """Backup must include all identity secrets and key material."""

  def _make_data_dir(self, tmp_path):
    """Builds a minimal /data layout for backup tests."""
    data = tmp_path / "data"
    # Database
    (data / "db").mkdir(parents=True)
    import sqlite3
    conn = sqlite3.connect(str(data / "db" / "ultimate.db"))
    conn.execute("CREATE TABLE test (x INTEGER)")
    conn.commit()
    conn.close()
    # Identity secrets
    (data / ".secret-key").write_text("sk-" + "x" * 60, encoding="ascii")
    (data / ".secret-key").chmod(0o600)
    (data / ".recovery-secret").write_text("rs-" + "y" * 60, encoding="ascii")
    (data / ".recovery-secret").chmod(0o600)
    (data / "service-token.txt").write_text("svc-token-placeholder", encoding="ascii")
    (data / "service-token.txt").chmod(0o600)
    # VAPID keys
    (data / "push").mkdir()
    (data / "push" / "vapid-private.pem").write_text("---BEGIN---", encoding="ascii")
    (data / "push" / "vapid-public.pem").write_text("---BEGIN---", encoding="ascii")
    # Recovery chat history
    (data / "recovery" / "chats").mkdir(parents=True)
    (data / "recovery" / "chats" / "abc123.jsonl").write_text(
      '{"_meta":{"provider":"claude"}}\n', encoding="utf-8"
    )
    # App storage
    (data / "apps" / "notes").mkdir(parents=True)
    (data / "apps" / "notes" / "index.jsx").write_text("export default () => null;", encoding="utf-8")
    # CLI auth
    (data / "cli-auth" / "claude").mkdir(parents=True)
    (data / "cli-auth" / "claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    return data

  def _zip_names(self, data_dir):
    """Calls _create_backup and returns the zip entry names by running
    the async generator via asyncio.run."""
    import asyncio
    import io
    import zipfile
    from app.routes.recover import _create_backup

    resp = _create_backup(data_dir)
    # StreamingResponse wraps a BytesIO as an async iterable. Collect
    # all chunks by running the generator under a fresh event loop.
    async def _collect():
      chunks = []
      async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
      return b"".join(chunks)

    raw = asyncio.run(_collect())
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
      return zf.namelist()

  def test_backup_includes_secret_key(self, tmp_path, monkeypatch):
    """The backup zip must contain .secret-key."""
    data = self._make_data_dir(tmp_path)
    names = self._zip_names(data)
    assert ".secret-key" in names, (
      "backup is missing .secret-key — restoring would invalidate all sessions"
    )

  def test_backup_includes_recovery_secret(self, tmp_path):
    """The backup zip must contain .recovery-secret."""
    data = self._make_data_dir(tmp_path)
    names = self._zip_names(data)
    assert ".recovery-secret" in names, (
      "backup is missing .recovery-secret — restoring would break the recovery surface"
    )

  def test_backup_includes_service_token(self, tmp_path):
    """The backup zip must contain service-token.txt."""
    data = self._make_data_dir(tmp_path)
    names = self._zip_names(data)
    assert "service-token.txt" in names, (
      "backup is missing service-token.txt — restoring would break scheduled tasks"
    )

  def test_backup_includes_vapid_keys(self, tmp_path):
    """The backup zip must contain push/ (VAPID keys)."""
    data = self._make_data_dir(tmp_path)
    names = self._zip_names(data)
    push_files = [n for n in names if n.startswith("push/")]
    assert push_files, (
      "backup is missing push/ (VAPID keys) — restoring would permanently "
      "break Web Push for all subscribed devices"
    )

  def test_backup_includes_recovery_history(self, tmp_path):
    """The backup zip must contain recovery/ (prior rescue chat history)."""
    data = self._make_data_dir(tmp_path)
    names = self._zip_names(data)
    recovery_files = [n for n in names if n.startswith("recovery/")]
    assert recovery_files, (
      "backup is missing recovery/ — prior rescue chat history lost on restore"
    )

  def test_backup_skips_symlinks(self, tmp_path):
    """Symlinks inside /data must not be followed or archived.
    A symlink pointing outside /data could leak host files or produce
    a zip with a confusing entry."""
    data = self._make_data_dir(tmp_path)
    # Plant a symlink inside apps/
    link = data / "apps" / "notes" / "evil-link"
    target = tmp_path / "outside-data.txt"
    target.write_text("should not appear in backup", encoding="utf-8")
    link.symlink_to(target)
    names = self._zip_names(data)
    assert "apps/notes/evil-link" not in names, (
      "symlink was included in backup — could leak out-of-tree files"
    )


# ---------------------------------------------------------------------
# P1 — factory reset produces a clean slate
# ---------------------------------------------------------------------

class TestFactoryResetCleanSlate:
  """Factory reset must delete all identity secrets and prior-owner data."""

  def _setup_data_dir(self, tmp_path):
    """Creates a minimal populated /data layout."""
    data = tmp_path / "data"
    (data / "db").mkdir(parents=True)
    import sqlite3
    conn = sqlite3.connect(str(data / "db" / "ultimate.db"))
    conn.execute("CREATE TABLE owner (id INTEGER, username TEXT, hashed_password TEXT)")
    conn.execute("CREATE TABLE apps (id INTEGER)")
    conn.execute("CREATE TABLE chats (id TEXT, messages TEXT, deleted_at TEXT)")
    conn.execute("CREATE TABLE notifications (id INTEGER)")
    conn.execute("CREATE TABLE push_subscriptions (id INTEGER)")
    conn.execute(
      "INSERT INTO owner VALUES (1, 'oldowner', '$2b$12$placeholder')"
    )
    conn.commit()
    conn.close()
    (data / ".secret-key").write_text("old-sk", encoding="ascii")
    (data / ".recovery-secret").write_text("old-rs", encoding="ascii")
    (data / "service-token.txt").write_text("old-svc", encoding="ascii")
    (data / "recovery" / "chats").mkdir(parents=True)
    (data / "recovery" / "chats" / "old.jsonl").write_text(
      '{"role":"user","content":"prior owner private chat"}\n', encoding="utf-8"
    )
    # Standard subdirs
    for d in ["compiled", "apps", "shared", "logs", "cli-auth", "chats",
              "agent-browser-profiles"]:
      (data / d).mkdir(parents=True, exist_ok=True)
    return data

  def test_factory_reset_removes_secret_key(self, tmp_path):
    """After factory reset, .secret-key must be absent so the next
    boot regenerates it (new identity)."""
    from app.routes.recover import _action_factory_reset
    data = self._setup_data_dir(tmp_path)
    # We need a valid SQLite DB at the path recover.py reads from.
    import os
    monkeypatch_env = f"sqlite:////{data}/db/ultimate.db"
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = monkeypatch_env
    # Patch the module-level constant used by the route
    from app.routes import recover as recover_mod
    orig_db = recover_mod.RECOVERY_DB_PATH
    recover_mod.RECOVERY_DB_PATH = str(data / "db" / "ultimate.db")
    try:
      _action_factory_reset(data)
    finally:
      recover_mod.RECOVERY_DB_PATH = orig_db
      if old is None:
        os.environ.pop("DATABASE_URL", None)
      else:
        os.environ["DATABASE_URL"] = old

    assert not (data / ".secret-key").exists(), (
      ".secret-key must be deleted by factory reset so next boot generates a fresh one"
    )

  def test_factory_reset_removes_recovery_secret(self, tmp_path):
    """After factory reset, .recovery-secret must be absent."""
    from app.routes.recover import _action_factory_reset
    data = self._setup_data_dir(tmp_path)
    from app.routes import recover as recover_mod
    orig_db = recover_mod.RECOVERY_DB_PATH
    recover_mod.RECOVERY_DB_PATH = str(data / "db" / "ultimate.db")
    try:
      _action_factory_reset(data)
    finally:
      recover_mod.RECOVERY_DB_PATH = orig_db

    assert not (data / ".recovery-secret").exists(), (
      ".recovery-secret must be deleted by factory reset"
    )

  def test_factory_reset_removes_service_token(self, tmp_path):
    """After factory reset, service-token.txt must be absent."""
    from app.routes.recover import _action_factory_reset
    data = self._setup_data_dir(tmp_path)
    from app.routes import recover as recover_mod
    orig_db = recover_mod.RECOVERY_DB_PATH
    recover_mod.RECOVERY_DB_PATH = str(data / "db" / "ultimate.db")
    try:
      _action_factory_reset(data)
    finally:
      recover_mod.RECOVERY_DB_PATH = orig_db

    assert not (data / "service-token.txt").exists(), (
      "service-token.txt must be deleted by factory reset"
    )

  def test_factory_reset_removes_recovery_directory(self, tmp_path):
    """After factory reset, the recovery/ directory must be absent.
    It contains the prior owner's private rescue-chat history."""
    from app.routes.recover import _action_factory_reset
    data = self._setup_data_dir(tmp_path)
    from app.routes import recover as recover_mod
    orig_db = recover_mod.RECOVERY_DB_PATH
    recover_mod.RECOVERY_DB_PATH = str(data / "db" / "ultimate.db")
    try:
      _action_factory_reset(data)
    finally:
      recover_mod.RECOVERY_DB_PATH = orig_db

    assert not (data / "recovery").exists(), (
      "recovery/ must be deleted by factory reset — it is the prior owner's private data"
    )


# ---------------------------------------------------------------------
# P1 — debug status exposes reconciliation failure
# ---------------------------------------------------------------------

def test_debug_status_exposes_reconciliation_failure(client, auth_cookie, monkeypatch):
  """When startup reconciliation fails, /api/debug/status must include
  reconciliation_failed: true so operators and tests can see it.

  The test simulates the failure by setting app.state.reconciliation_failed
  directly (the same flag lifespan() sets on exception). The never-crash-
  boot contract is preserved: the endpoint must still return 200.
  """
  from app.main import app
  app.state.reconciliation_failed = True
  try:
    # /api/debug/status requires JWT auth; the auth_cookie fixture
    # creates an owner and returns a recovery-cookie dict, but we need
    # the API JWT. Use the existing `auth` fixture pattern instead.
    from app.database import SessionLocal
    from app import models, auth as auth_mod
    import bcrypt

    db = SessionLocal()
    existing = db.query(models.Owner).filter(
      models.Owner.username == "rec_test_owner"
    ).first()
    if not existing:
      owner = models.Owner(
        username="rec_test_owner",
        hashed_password=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
        provider="claude",
      )
      db.add(owner)
      db.commit()
    db.close()

    token = auth_mod.create_access_token({"sub": "rec_test_owner"})
    headers = {"Authorization": f"Bearer {token}"}

    r = client.get("/api/debug/status", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body.get("reconciliation_failed") is True, (
      "/api/debug/status must expose reconciliation_failed=true when "
      "startup reconciliation threw an exception"
    )
  finally:
    # Clean up so this flag doesn't leak into other tests.
    app.state.reconciliation_failed = False


def test_debug_status_no_reconciliation_flag_when_ok(client, monkeypatch):
  """When reconciliation succeeded (the normal case), the
  reconciliation_failed field must be absent from the response."""
  from app.main import app
  # Ensure the flag is not set.
  if hasattr(app.state, "reconciliation_failed"):
    del app.state.reconciliation_failed

  from app.database import SessionLocal
  from app import models, auth as auth_mod
  import bcrypt

  db = SessionLocal()
  existing = db.query(models.Owner).filter(
    models.Owner.username == "rec_ok_owner"
  ).first()
  if not existing:
    owner = models.Owner(
      username="rec_ok_owner",
      hashed_password=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
      provider="claude",
    )
    db.add(owner)
    db.commit()
  db.close()

  token = auth_mod.create_access_token({"sub": "rec_ok_owner"})
  headers = {"Authorization": f"Bearer {token}"}
  r = client.get("/api/debug/status", headers=headers)
  assert r.status_code == 200
  body = r.json()
  assert "reconciliation_failed" not in body, (
    "reconciliation_failed must be absent from debug/status when "
    "reconciliation succeeded"
  )
