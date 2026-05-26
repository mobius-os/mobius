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


# ---------------------------------------------------------------------
# Send/stream pairing — closes the multi-tab race where /stream would
# read "latest user message" instead of the specific one paired with
# the /send that returned the turn_id.
# ---------------------------------------------------------------------

def test_send_returns_turn_id(client, auth_cookie, monkeypatch, tmp_path):
  """/recover/chat/send returns a turn_id the client passes back to
  /stream so the response pairs with this specific message, not
  'latest' (which races under multi-tab use)."""
  log_path = tmp_path / "pair.jsonl"
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH", log_path,
  )
  r = client.post(
    "/recover/chat/send",
    json={"message": "first"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200
  body = r.json()
  assert "turn_id" in body
  assert body["turn_id"] == 0

  r2 = client.post(
    "/recover/chat/send",
    json={"message": "second"},
    cookies=auth_cookie,
  )
  assert r2.json()["turn_id"] == 1


def test_user_message_by_id_pairs_correctly(monkeypatch, tmp_path):
  """The runner helper returns the EXACT message at a turn_id, not
  the latest. This is what closes the send/stream pairing race."""
  log_path = tmp_path / "byid.jsonl"
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH", log_path,
  )
  from app import recover_chat_runner as rcr
  id1 = rcr.append_log("user", "first")
  id2 = rcr.append_log("assistant", "reply1")
  id3 = rcr.append_log("user", "second")

  assert rcr.user_message_by_id(id1) == "first"
  assert rcr.user_message_by_id(id2) is None  # assistant, not user
  assert rcr.user_message_by_id(id3) == "second"
  # Out-of-range ids return None cleanly.
  assert rcr.user_message_by_id(999) is None
  assert rcr.user_message_by_id(-1) is None


def test_stream_uses_provided_turn_id_not_latest(
  client, auth_cookie, monkeypatch, tmp_path,
):
  """If the client passes turn_id=N, the stream endpoint should
  resolve to message N — even if a NEWER user message has landed
  in the log since. This is the multi-tab race fix."""
  log_path = tmp_path / "race.jsonl"
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH", log_path,
  )
  # Two sends in quick succession (simulating two tabs racing).
  r1 = client.post(
    "/recover/chat/send",
    json={"message": "tab1 message"},
    cookies=auth_cookie,
  )
  tab1_turn = r1.json()["turn_id"]
  r2 = client.post(
    "/recover/chat/send",
    json={"message": "tab2 message"},
    cookies=auth_cookie,
  )
  tab2_turn = r2.json()["turn_id"]
  assert tab1_turn != tab2_turn

  # Verify the runner's user_message_by_id (what the stream endpoint
  # uses internally) returns the right message for each turn_id.
  from app import recover_chat_runner as rcr
  assert rcr.user_message_by_id(tab1_turn) == "tab1 message"
  assert rcr.user_message_by_id(tab2_turn) == "tab2 message"
  # latest_user_message would return tab2 (this is the race scenario
  # we are explicitly NOT relying on anymore).
  assert rcr.latest_user_message() == "tab2 message"


# ---------------------------------------------------------------------
# Owner-existence check on every cookie consumption
# ---------------------------------------------------------------------

def test_stale_cookie_after_owner_deletion_rejected(
  client, auth_cookie, monkeypatch, tmp_path,
):
  """A valid HMAC cookie issued before a factory reset must NOT
  retain elevated access after the Owner row is gone. Without the
  per-request owner-existence check, the cookie would stay valid
  for the remainder of its 1h TTL — a second tab, stolen cookie, or
  another browser profile would keep elevated write access on a
  wiped instance."""
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "recovery_chat.jsonl",
  )

  # Sanity: send works while the owner exists (the auth_cookie
  # fixture created the `tester` row).
  r = client.post(
    "/recover/chat/send",
    json={"message": "before reset"},
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
    json={"message": "after reset"},
    cookies=auth_cookie,
  )
  assert r.status_code == 401, "stale cookie must not be accepted on /send"

  r = client.post(
    "/recover/chat/stream",
    json={"turn_id": 0},
    cookies=auth_cookie,
  )
  assert r.status_code == 401, "stale cookie must not be accepted on /stream"

  r = client.post("/recover/chat/reset", cookies=auth_cookie)
  assert r.status_code == 401, "stale cookie must not be accepted on /reset"

  # The HTML page must redirect (its no-cookie behavior), not render.
  r = client.get("/recover/chat", cookies=auth_cookie, follow_redirects=False)
  assert r.status_code == 302


# ---------------------------------------------------------------------
# turn_id replay/reuse guard
# ---------------------------------------------------------------------

def test_turn_id_replay_returns_409(
  client, auth_cookie, monkeypatch, tmp_path,
):
  """The same turn_id POSTed to /stream twice must return 409 on
  the second attempt. Without this guard, a double-click or a
  network retry would spawn a second Claude CLI subprocess against
  the same historical message — doubled token cost + a duplicate
  assistant entry in the recovery log."""
  log_path = tmp_path / "replay.jsonl"
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH", log_path,
  )
  # Reset the module-level dedup set so the test is order-independent.
  from app import recover_chat as rc
  rc._streamed_turn_ids.clear()

  # Step 1: a user send creates turn_id=0.
  r = client.post(
    "/recover/chat/send",
    json={"message": "fix the thing"},
    cookies=auth_cookie,
  )
  assert r.status_code == 200
  turn_id = r.json()["turn_id"]

  # Step 2: stub stream_turn so the test doesn't actually spawn
  # the Claude CLI subprocess. The dedup check happens BEFORE the
  # stream begins, so even a no-op streamer suffices to verify the
  # 409 behavior on the second POST.
  from app import recover_chat_runner as rcr

  async def _empty_stream(_message):
    if False:
      yield ""  # generator that yields nothing

  monkeypatch.setattr(rcr, "stream_turn", _empty_stream)

  r1 = client.post(
    "/recover/chat/stream",
    json={"turn_id": turn_id},
    cookies=auth_cookie,
  )
  assert r1.status_code == 200, (
    f"first stream should succeed, got {r1.status_code}: {r1.text}"
  )

  # Step 3: replay the same turn_id — must 409.
  r2 = client.post(
    "/recover/chat/stream",
    json={"turn_id": turn_id},
    cookies=auth_cookie,
  )
  assert r2.status_code == 409, (
    f"replayed turn_id must return 409, got {r2.status_code}: {r2.text}"
  )
  # The body should explain why so a client UI can show something
  # actionable instead of a generic conflict.
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
    rc._mark_turn_id_streamed(i)
  assert len(rc._streamed_turn_ids) == cap
  # FIFO eviction: the oldest ids were dropped, the newest are kept.
  assert 0 not in rc._streamed_turn_ids
  assert (cap * 2 - 1) in rc._streamed_turn_ids


# ---------------------------------------------------------------------
# _defer_restore mode validation
# ---------------------------------------------------------------------

def test_defer_restore_rejects_invalid_mode():
  """Calling _defer_restore with an unknown mode must raise rather
  than write a typo to /data/.recover-pending. Otherwise the
  entrypoint silently falls through to restore_status='unknown-mode'
  and the container reboots into the same broken state."""
  from app.routes import recover as recover_routes

  import pytest as _pytest
  with _pytest.raises(ValueError):
    recover_routes._defer_restore("shell")  # missing -dist or -src
  with _pytest.raises(ValueError):
    recover_routes._defer_restore("")  # empty
  with _pytest.raises(ValueError):
    recover_routes._defer_restore("backend ")  # trailing whitespace


def test_defer_restore_valid_modes_listed():
  """All four expected modes are in the allow-list. If this fails,
  either a mode was renamed in entrypoint without updating the
  Python guardrail, or vice versa — both cases are bugs."""
  from app.routes import recover as recover_routes
  assert recover_routes._VALID_MODES == frozenset({
    "backend", "scripts", "shell-dist", "shell-src",
  })


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
  """Fire N=10 concurrent append_log calls; each must return a
  distinct id and the file must end with exactly 10 lines.

  Without the lock, two callers could both read the post-append
  line count and both return the same id (the TOCTOU race that
  re-opens the multi-tab pairing bug)."""
  import asyncio as _asyncio
  from app import recover_chat_runner as rcr
  monkeypatch.setattr(
    "app.recover_chat_runner.RECOVERY_LOG_PATH",
    tmp_path / "concurrent_append.jsonl",
  )

  N = 10
  results = await _asyncio.gather(
    *[_asyncio.to_thread(rcr.append_log, "user", f"msg-{i}")
      for i in range(N)]
  )
  # All ids distinct, covering 0..N-1 exactly.
  assert sorted(results) == list(range(N))
  # File has exactly N non-empty lines.
  log_path = tmp_path / "concurrent_append.jsonl"
  lines = [
    line for line in log_path.read_text().splitlines() if line.strip()
  ]
  assert len(lines) == N
  # Every line is a valid JSON object — no torn writes.
  for line in lines:
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
