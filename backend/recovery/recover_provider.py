"""Provider (re)connect for the recovery agent — stdlib only.

Lifted from app/recover_oauth.py and stripped of FastAPI so it runs inside
recoveryd's stdlib http.server. Lets the owner connect Claude (PKCE OAuth) or
Codex (device-auth) from within recovery so the recovery agent has credentials
to run. Writes the same CLI credential files the agent runner reads
(/data/cli-auth/{claude,codex}). The recoveryd handler gates every call on the
recovery session cookie before calling anything here.

The Claude constants are duplicated public Anthropic values (same as
app/recover_oauth.py + routes/auth.py) so recovery keeps working even if the
platform's auth is broken. If Anthropic rotates the client id, update all three.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from base64 import urlsafe_b64encode
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_SCOPES = (
  "org:create_api_key user:profile user:inference "
  "user:sessions:claude_code user:mcp_servers user:file_upload"
)
_PKCE_TIMEOUT = 300  # 5 minutes.

_DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
_CLAUDE_CONFIG_PATH = _DATA_DIR / "cli-auth" / "claude"
_CODEX_CONFIG_PATH = _DATA_DIR / "cli-auth" / "codex"

# In-flight PKCE state for the Claude flow. Single-owner, so one flow at a
# time; a second /start replaces the prior state. The lock serializes the
# read-modify-write so two rapid starts can't strand the second exchange.
_active_pkce: dict | None = None
_pkce_lock = threading.Lock()

# In-flight Codex device-auth subprocess.
_codex_proc: subprocess.Popen | None = None
_codex_lock = threading.Lock()
_CODEX_LOGIN_MAX_LIFETIME = 600  # 10 minutes.


# --- Claude PKCE ---------------------------------------------------------

def _generate_pkce() -> tuple[str, str]:
  """Returns (code_verifier, code_challenge) for PKCE S256."""
  verifier = secrets.token_urlsafe(43)
  digest = hashlib.sha256(verifier.encode()).digest()
  challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
  return verifier, challenge


def _extract_code_and_state(raw_code: str) -> tuple[str, str | None]:
  """Extracts the provider code + echoed state from pasted input.

  Accepts a bare code, a `code=...&state=...` querystring, or a full
  redirect URL with the code in the query or fragment.
  """
  raw = raw_code.strip()
  parsed = urlparse(raw)
  values: dict = {}
  if parsed.scheme and (parsed.query or parsed.fragment):
    values = {**parse_qs(parsed.query), **parse_qs(parsed.fragment)}
  elif "=" in raw and ("&" in raw or raw.startswith("code=")):
    values = parse_qs(raw)
  if values:
    code = (values.get("code") or [raw])[0]
    state = (values.get("state") or [None])[0]
    return code, state
  code, _, fragment = raw.partition("#")
  state = None
  if fragment:
    state = (parse_qs(fragment).get("state") or [None])[0]
  return code or raw, state


def _write_claude_credentials(token_data: dict) -> None:
  """Writes the token response into the CLI's credential format (0600)."""
  for field in ("access_token", "refresh_token", "expires_in"):
    if field not in token_data:
      raise ValueError(f"Token response missing '{field}'")
  _CLAUDE_CONFIG_PATH.mkdir(parents=True, exist_ok=True)
  account = token_data.get("account") or {}
  creds = {
    "claudeAiOauth": {
      "accessToken": token_data["access_token"],
      "refreshToken": token_data["refresh_token"],
      "expiresAt": int(time.time() * 1000) + token_data["expires_in"] * 1000,
      "scopes": token_data.get("scope", "").split(),
      "email": account.get("email_address", ""),
    }
  }
  path = _CLAUDE_CONFIG_PATH / ".credentials.json"
  fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w") as f:
    json.dump(creds, f)


def claude_start() -> str:
  """Generates PKCE params and returns the Claude OAuth authorize URL."""
  global _active_pkce
  verifier, challenge = _generate_pkce()
  state = secrets.token_urlsafe(32)
  with _pkce_lock:
    _active_pkce = {"verifier": verifier, "state": state, "ts": time.time()}
  redirect = _REDIRECT_URI.replace(":", "%3A").replace("/", "%2F")
  scope = _SCOPES.replace(":", "%3A").replace(" ", "+")
  return (
    f"{_AUTHORIZE_URL}?code=true&client_id={_CLAUDE_CLIENT_ID}"
    f"&response_type=code&redirect_uri={redirect}&scope={scope}"
    f"&code_challenge={challenge}&code_challenge_method=S256&state={state}"
  )


def claude_exchange(raw_code: str) -> tuple[bool, str]:
  """Exchanges the pasted code for tokens and writes credentials.

  Returns (True, "") on success or (False, reason). Uses stdlib urllib
  (no httpx) so it runs in the frozen recovery bundle.
  """
  global _active_pkce
  if not raw_code.strip():
    return False, "code required"
  with _pkce_lock:
    if not _active_pkce:
      return False, "No auth flow in progress. Start one first."
    if time.time() - _active_pkce["ts"] > _PKCE_TIMEOUT:
      _active_pkce = None
      return False, "Auth flow expired. Please start again."
    pkce = _active_pkce
    _active_pkce = None
  code, returned_state = _extract_code_and_state(raw_code)
  if returned_state is not None and returned_state != pkce["state"]:
    return False, "OAuth state mismatch. Start the auth flow again."
  body = json.dumps({
    "grant_type": "authorization_code",
    "code": code,
    "client_id": _CLAUDE_CLIENT_ID,
    "redirect_uri": _REDIRECT_URI,
    "code_verifier": pkce["verifier"],
    "state": pkce["state"],
  }).encode()
  req = Request(
    _TOKEN_URL, data=body,
    headers={"Content-Type": "application/json"}, method="POST",
  )
  try:
    with urlopen(req, timeout=30) as resp:
      token_data = json.loads(resp.read().decode())
  except URLError as exc:
    return False, f"Token exchange failed ({exc}). Try starting again."
  except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
    return False, f"Token exchange error: {exc}"
  try:
    _write_claude_credentials(token_data)
  except (ValueError, OSError) as exc:
    return False, f"Could not save credentials: {exc}"
  return True, ""


# --- Codex device-auth ---------------------------------------------------

def codex_start() -> tuple[bool, str]:
  """Starts `codex login --device-auth`; returns (ok, url-or-instructions).

  Codex prints a verification URL + code to stdout; we surface the first
  meaningful line. The subprocess writes /data/cli-auth/codex/auth.json on
  success. A prior in-flight login is killed first.
  """
  global _codex_proc
  with _codex_lock:
    old = _codex_proc
    _codex_proc = None
  if old and old.poll() is None:
    old.kill()
  _CODEX_CONFIG_PATH.mkdir(parents=True, exist_ok=True)
  env = dict(os.environ)
  env["CODEX_HOME"] = str(_CODEX_CONFIG_PATH)
  try:
    proc = subprocess.Popen(
      ["codex", "login", "--device-auth"],
      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT, env=env, text=True,
    )
  except FileNotFoundError:
    return False, "codex CLI not found in the recovery image."
  with _codex_lock:
    _codex_proc = proc
  threading.Thread(target=_reap_codex, args=(proc,), daemon=True).start()
  # Read the first lines to surface the device URL/code to the user.
  deadline = time.time() + 15
  lines: list[str] = []
  while time.time() < deadline and proc.poll() is None:
    line = proc.stdout.readline() if proc.stdout else ""
    if not line:
      break
    lines.append(line.strip())
    if "http" in line.lower() or "code" in line.lower():
      break
  msg = " | ".join(l for l in lines if l) or "Codex login started."
  return True, msg


def _reap_codex(proc: subprocess.Popen) -> None:
  """Kills an abandoned codex login after its max lifetime."""
  try:
    proc.wait(timeout=_CODEX_LOGIN_MAX_LIFETIME)
  except subprocess.TimeoutExpired:
    proc.kill()
  finally:
    global _codex_proc
    with _codex_lock:
      if _codex_proc is proc:
        _codex_proc = None


def codex_connected() -> bool:
  """True once the codex login has written auth.json."""
  return (_CODEX_CONFIG_PATH / "auth.json").is_file()
