"""Stdlib provider (re)connect flow for the frozen recovery container.

Ported from `backend/app/recover_oauth.py`, which is a FastAPI + httpx
module. The recovery bundle is stdlib-only (plus bcrypt), so this copy
keeps the SAME OAuth mechanics — Claude PKCE code exchange and the Codex
device-auth banner flow — but reimplements the I/O with `urllib.request`
(for the Claude token POST) and blocking `subprocess.Popen` + reader
threads (for the Codex login), so it runs inside recoveryd's synchronous
`ThreadingHTTPServer` handlers without asyncio or a web framework.

What it does NOT do here: session auth. The FastAPI original gated every
endpoint on the recovery cookie; in recoveryd the request handler
performs that gate centrally (the same `_authed_username()` +
`owner_exists()` checks the restore/update routes use) before calling
into these functions, so this module stays focused on the OAuth
mechanics alone.

Credential destinations are taken from `recovery_chat_runner`'s
`CLAUDE_CONFIG_PATH` / `CODEX_CONFIG_PATH` constants so the write target
(here) and the read target (the runner's `provider_status` + spawn paths)
can never drift apart.

FROZEN: this file is baked root-owned + chmod a-w in the `/app/recovery/`
bundle. To change OAuth client ids or URLs, edit on the host repo and
rebuild.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pwd
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlparse

import recovery_chat_runner
from codex_login_parse import banner_has_code, parse_login_banner

log = logging.getLogger("recoveryd.oauth")


def _agent_ids() -> "tuple[int, int] | None":
  """Returns (uid, gid) for the mobius agent user, or None if unknown."""
  try:
    pw = pwd.getpwnam(recovery_chat_runner.RECOVERY_AGENT_USER)
  except KeyError:
    return None
  return pw.pw_uid, pw.pw_gid


def _chown_to_agent(path) -> None:
  """Best-effort chown of a credential path to the mobius agent user.

  Credentials are written here by recoveryd (root), but the rescue agent
  runs AS mobius and the provider CLI must READ and REFRESH them — a
  root-owned 0600 file would be unreadable to mobius, so the very
  reconnect the owner just performed would leave the agent unable to
  authenticate. Only meaningful as root; a no-op otherwise. Never raises.
  """
  if os.geteuid() != 0:
    return
  ids = _agent_ids()
  if ids is None:
    return
  try:
    os.chown(str(path), ids[0], ids[1])
  except OSError:
    pass


class OAuthError(Exception):
  """Carries an HTTP status + a user-facing detail for the request handler.

  The recoveryd handler catches this and maps `status`/`detail` onto the
  response, so the OAuth functions can signal a specific failure (bad
  code, expired flow, upstream error) without knowing about HTTP framing.
  """

  def __init__(self, status: int, detail: str) -> None:
    super().__init__(detail)
    self.status = status
    self.detail = detail


# --- Claude OAuth constants -----------------------------------------
# Duplicated from routes/auth.py (and recover_oauth.py) on purpose. These
# are public Anthropic constants; duplicating them keeps the recovery flow
# working even if the agent rewrites routes/auth.py. If Anthropic ever
# rotates the client id, update all copies.
_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_SCOPES = (
  "org:create_api_key user:profile user:inference "
  "user:sessions:claude_code user:mcp_servers user:file_upload"
)
_PKCE_TIMEOUT = 300  # 5 minutes


# Credential destinations, bound to the runner's constants so the connect
# target and the read/spawn target are one source of truth.
_CLAUDE_CONFIG_PATH = recovery_chat_runner.CLAUDE_CONFIG_PATH
_CODEX_CONFIG_PATH = recovery_chat_runner.CODEX_CONFIG_PATH


# In-flight PKCE state for the Claude flow. Single-owner surface, so one
# flow at a time is fine. A second call to claude_start replaces the prior
# state; a stale exchange after the timeout fails cleanly. The lock
# serializes read-modify-write so two rapid starts can't interleave and
# leave the second exchange stranded with the first start's verifier.
_active_pkce: dict | None = None
_pkce_lock = threading.Lock()


# --- Claude PKCE helpers --------------------------------------------

def _generate_pkce() -> tuple[str, str]:
  """Returns (code_verifier, code_challenge) for PKCE S256."""
  verifier = secrets.token_urlsafe(43)
  digest = hashlib.sha256(verifier.encode()).digest()
  challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
  return verifier, challenge


def _extract_provider_code_and_state(raw_code: str) -> tuple[str, str | None]:
  """Extract the provider code and echoed state from pasted input."""
  raw = raw_code.strip()
  parsed = urlparse(raw)
  values: dict = {}
  if parsed.scheme and (parsed.query or parsed.fragment):
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    values = {**query, **fragment}
  elif "=" in raw and ("&" in raw or raw.startswith("code=")):
    values = parse_qs(raw)

  if values:
    code = (values.get("code") or [raw])[0]
    state = (values.get("state") or [None])[0]
    return code, state

  code, _, fragment = raw.partition("#")
  state = None
  if fragment:
    fragment_values = parse_qs(fragment)
    state = (fragment_values.get("state") or [None])[0]
  return code or raw, state


def _write_claude_credentials(token_data: dict) -> None:
  """Transforms the token endpoint response into CLI credential format."""
  for field in ("access_token", "refresh_token", "expires_in"):
    if field not in token_data:
      raise OAuthError(502, f"Token response missing '{field}'")

  _CLAUDE_CONFIG_PATH.mkdir(parents=True, exist_ok=True)
  # The dir may have just been created root-owned; hand it to mobius so the
  # agent (running as mobius) can read the credential file written below.
  _chown_to_agent(_CLAUDE_CONFIG_PATH)

  account = token_data.get("account") or {}
  email = account.get("email_address", "")

  creds = {
    "claudeAiOauth": {
      "accessToken": token_data["access_token"],
      "refreshToken": token_data["refresh_token"],
      "expiresAt": int(time.time() * 1000) + token_data["expires_in"] * 1000,
      "scopes": token_data.get("scope", "").split(),
      "email": email,
    }
  }
  path = _CLAUDE_CONFIG_PATH / ".credentials.json"
  fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w") as f:
    json.dump(creds, f)
  # Written root-owned by recoveryd; the mobius agent must be able to read
  # and refresh it, so transfer ownership to mobius.
  _chown_to_agent(path)
  log.info("Recovery: Claude credentials written for %s", email or "(unknown)")


def claude_start() -> dict:
  """Generates PKCE params and returns `{auth_url}` for the Claude flow."""
  global _active_pkce
  verifier, challenge = _generate_pkce()
  state = secrets.token_urlsafe(32)
  with _pkce_lock:
    _active_pkce = {"verifier": verifier, "state": state, "ts": time.time()}

  auth_url = (
    f"{_AUTHORIZE_URL}?code=true"
    f"&client_id={_CLAUDE_CLIENT_ID}"
    f"&response_type=code"
    f"&redirect_uri={_REDIRECT_URI.replace(':', '%3A').replace('/', '%2F')}"
    f"&scope={_SCOPES.replace(':', '%3A').replace(' ', '+')}"
    f"&code_challenge={challenge}"
    f"&code_challenge_method=S256"
    f"&state={state}"
  )
  return {"auth_url": auth_url}


def claude_exchange(raw_code: str) -> None:
  """Exchanges the pasted authorization code for tokens and writes them.

  Raises OAuthError with an appropriate status on any failure (no flow in
  progress, expired, state mismatch, upstream non-200, timeout). Uses a
  blocking `urllib.request` POST — the recoveryd handler runs on a worker
  thread, so a synchronous call is correct here.
  """
  global _active_pkce
  raw_code = (raw_code or "").strip()
  if not raw_code:
    raise OAuthError(400, "code required")

  # Atomically claim the in-flight PKCE entry: check presence + expiry +
  # clear in one lock-held block so a concurrent start can't replace it
  # between our check and our read.
  with _pkce_lock:
    if not _active_pkce:
      raise OAuthError(400, "No auth flow in progress. Start one first.")
    if time.time() - _active_pkce["ts"] > _PKCE_TIMEOUT:
      _active_pkce = None
      raise OAuthError(400, "Auth flow expired. Please start again.")
    pkce = _active_pkce
    _active_pkce = None

  code, returned_state = _extract_provider_code_and_state(raw_code)
  if returned_state is not None and returned_state != pkce["state"]:
    raise OAuthError(403, "OAuth state mismatch. Start the auth flow again.")

  body = json.dumps({
    "grant_type": "authorization_code",
    "code": code,
    "client_id": _CLAUDE_CLIENT_ID,
    "redirect_uri": _REDIRECT_URI,
    "code_verifier": pkce["verifier"],
    "state": pkce["state"],
  }).encode("utf-8")
  req = urllib.request.Request(
    _TOKEN_URL,
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=30) as resp:
      data = json.loads(resp.read().decode("utf-8"))
  except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", "replace")[:500] if exc.fp else ""
    log.error(
      "Recovery: Claude token exchange failed (%s): %s", exc.code, detail,
    )
    raise OAuthError(
      502, "Token exchange failed. Try starting the auth flow again.",
    )
  except (urllib.error.URLError, socket.timeout) as exc:
    raise OAuthError(504, f"Token exchange timed out: {exc}")
  except OAuthError:
    raise
  except Exception as exc:  # noqa: BLE001 — surface as a clean 500
    log.error("Recovery: Claude token exchange error: %s", exc)
    raise OAuthError(500, str(exc))

  _write_claude_credentials(data)


# --- Codex device-auth flow -----------------------------------------

# Absolute cap on how long a codex device-auth subprocess may live waiting
# for the user to complete authentication. Mirrors recover_oauth.py's
# _CODEX_LOGIN_MAX_LIFETIME: an abandoned login must not outlive the
# recovery session and silently repopulate credentials later.
_CODEX_LOGIN_MAX_LIFETIME = 600  # 10 minutes

# Codex login state, guarded by _codex_lock. `proc` is the live Popen (or
# None); `result` is the terminal outcome ("complete" | "failed" | None);
# `output` accumulates the login banner text for the readiness probe +
# parse.
_codex_state: dict = {"proc": None, "result": None, "output": ""}
_codex_lock = threading.Lock()


def _codex_kill(proc) -> None:
  """Best-effort kill of a codex login subprocess. Never raises."""
  try:
    if proc is not None and proc.poll() is None:
      proc.kill()
  except (ProcessLookupError, OSError):
    pass


def terminate_active_codex_login() -> bool:
  """Kills any in-flight codex device-auth subprocess.

  The recoveryd equivalent of recover_oauth.terminate_active_codex_login —
  destructive admin actions call it so a login that completes AFTER a reset
  cannot recreate credentials the reset just wiped. Returns True iff a proc
  was active and got killed.
  """
  with _codex_lock:
    proc = _codex_state.get("proc")
    if proc is None or proc.poll() is not None:
      return False
    _codex_state["result"] = "failed"
  _codex_kill(proc)
  return True


def codex_start() -> dict:
  """Starts `codex login --device-auth` and returns `{url, code}`.

  Spawns the CLI, reads its banner on a daemon reader thread (which keeps
  draining until the process exits so credentials actually land), and
  waits up to 15s for the device code to appear. Raises OAuthError(500) if
  the banner never parses. A watchdog thread enforces the lifetime cap.
  """
  # Replace any prior in-flight login.
  with _codex_lock:
    old = _codex_state.get("proc")
  _codex_kill(old)

  _CODEX_CONFIG_PATH.mkdir(parents=True, exist_ok=True)
  # The device-auth subprocess runs as mobius (same guarantee as the rescue
  # agent), so CODEX_HOME must be mobius-writable — hand the (possibly
  # just-created root-owned) dir over before spawning, and the auth.json it
  # writes then lands mobius-owned and readable by the agent.
  _chown_to_agent(_CODEX_CONFIG_PATH)
  env = dict(os.environ)
  env["CODEX_HOME"] = str(_CODEX_CONFIG_PATH)

  try:
    proc = subprocess.Popen(
      ["codex", "login", "--device-auth"],
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      env=env,
      text=True,
      preexec_fn=recovery_chat_runner.agent_preexec(),
    )
  except (OSError, ValueError) as exc:
    raise OAuthError(500, f"could not start codex login: {exc}")

  with _codex_lock:
    _codex_state["proc"] = proc
    _codex_state["result"] = None
    _codex_state["output"] = ""

  def _reader() -> None:
    """Drains stdout into the shared buffer, then records the outcome.

    The device-auth process keeps running (and its stdout open) until the
    user authorizes, so this thread blocks on the buffer until then — that
    is exactly what keeps the process alive long enough to write auth.json.
    """
    try:
      assert proc.stdout is not None
      for line in proc.stdout:
        with _codex_lock:
          _codex_state["output"] += line
    except Exception:
      pass
    proc.wait()
    with _codex_lock:
      if _codex_state.get("proc") is proc and _codex_state.get("result") is None:
        _codex_state["result"] = (
          "complete" if proc.returncode == 0 else "failed"
        )

  def _watchdog() -> None:
    """Kills an abandoned login once the lifetime cap elapses."""
    time.sleep(_CODEX_LOGIN_MAX_LIFETIME)
    if proc.poll() is None:
      _codex_kill(proc)
      with _codex_lock:
        if _codex_state.get("proc") is proc and _codex_state.get("result") is None:
          _codex_state["result"] = "failed"

  threading.Thread(target=_reader, daemon=True).start()
  threading.Thread(target=_watchdog, daemon=True).start()

  # Wait for the banner (device code) to arrive, bounded at 15s.
  deadline = time.time() + 15
  while time.time() < deadline:
    with _codex_lock:
      out = _codex_state["output"]
    if banner_has_code(out):
      break
    if proc.poll() is not None:
      break
    time.sleep(0.2)

  with _codex_lock:
    out = _codex_state["output"]
  parsed = parse_login_banner(out)
  if parsed is None:
    _codex_kill(proc)
    with _codex_lock:
      if _codex_state.get("proc") is proc and _codex_state.get("result") is None:
        _codex_state["result"] = "failed"
    log.warning(
      "Recovery: could not parse codex device code from output: %s",
      out[:500],
    )
    raise OAuthError(500, "Could not parse device code")
  return parsed


def codex_status() -> dict:
  """Polls the codex login state: in_progress | complete | failed | idle.

  Reads the terminal `result` non-destructively (cleared only on the next
  codex_start) so two concurrent pollers both observe completion.
  """
  with _codex_lock:
    proc = _codex_state.get("proc")
    result = _codex_state.get("result")
  if result == "complete":
    return {"state": "complete"}
  if result == "failed":
    return {"state": "failed"}
  if proc is not None and proc.poll() is None:
    return {"state": "in_progress"}
  return {"state": "idle"}
