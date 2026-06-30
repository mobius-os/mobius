#!/usr/bin/env python3
"""recoveryd — the frozen Tier-1 recovery floor.

A self-contained HTTP server that survives a fully-broken Mobius
platform. It runs in its OWN container (same image, different command,
own `restart: unless-stopped`), so a platform crash-loop, OOM, or
SIGTERM cannot take it down — different cgroup, different pid1.

Design constraints (owner-signed plan _148):
  * stdlib `http.server.ThreadingHTTPServer` only — no FastAPI, no
    uvicorn, no web framework.
  * imports ZERO `app.*` and nothing from `/data/platform` (the
    agent-editable tree). The only third-party import is `bcrypt`,
    lazily inside the auth path, from root-owned site-packages.
  * its own source is baked root-owned + `chmod a-w` at
    `/app/recovery/`; a startup self-integrity check refuses to boot if
    any of its files were tampered with.
  * launched `python3 -P /app/recovery/recoveryd.py`; `-P` drops the
    script directory from sys.path[0]. The startup scrub additionally
    strips `/app`, `/data/platform*`, and cwd so `import app` can never
    resolve onto the broken platform tree.

Tier-1 floor (this MVP): deterministic, agent-free, network-free
restore. POST /recover/restore writes `/data/.recover-pending=<mode>`
(reusing the entrypoint's existing boot-time handler) then the restart
sentinel `/data/.platform-restart-requested` (acted on by the platform
container's poller -> kill pid1 -> Docker recreate -> fix loads).

Deferred (NOT in this MVP, noted for the follow-on):
  * Tier-2 SSE AI-rescue chat (lifts recover_chat_runner).
  * DB-independent `owner.json` auth (survives a wiped DB — O2). This
    floor uses the DB owner row, so it survives a broken PLATFORM but
    not a wiped DB.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Import isolation — run BEFORE importing anything that could resolve onto the
# agent-editable platform tree. `python3 -P` already drops the script dir from
# sys.path[0]; this strips the other dangerous roots (`/app` where WORKDIR puts
# the symlinked platform package, `/data/platform*`, and cwd) while KEEPING
# site-packages (where bcrypt lives). After the scrub we assert `app` is not
# importable, so a future careless edit can't silently re-couple us.
# ---------------------------------------------------------------------------

def _scrub_sys_path() -> None:
  """Removes platform-tree roots from sys.path, keeps site-packages."""
  recovery_dir = str(Path(__file__).resolve().parent)
  dangerous_prefixes = ("/app", "/data/platform")
  cwd = os.getcwd()
  kept: list[str] = []
  for entry in sys.path:
    resolved = os.path.abspath(entry) if entry else cwd
    # Keep our own frozen bundle; drop anything under the platform roots
    # or the working directory (which is /app per the Dockerfile WORKDIR).
    if resolved == recovery_dir:
      kept.append(entry)
      continue
    if resolved == cwd or resolved == "" or entry == "":
      continue
    if any(
      resolved == p or resolved.startswith(p + os.sep)
      for p in dangerous_prefixes
    ):
      continue
    kept.append(entry)
  # Ensure our own dir is importable (recovery_auth / recovery_db /
  # recovery_pages live beside this file).
  if recovery_dir not in kept:
    kept.insert(0, recovery_dir)
  sys.path[:] = kept


def _assert_platform_not_importable() -> None:
  """Hard invariant: the broken platform must not be reachable from here."""
  import importlib.util

  spec = importlib.util.find_spec("app")
  if spec is not None and getattr(spec, "origin", None):
    origin = str(spec.origin)
    # The ONLY acceptable `app` would be something unrelated in
    # site-packages; the dangerous one resolves under /app or
    # /data/platform. Refuse if it points at the platform tree.
    if "/data/platform" in origin or origin.startswith("/app/app"):
      raise SystemExit(
        f"FATAL: recoveryd import isolation breached — `app` resolves to "
        f"{origin}. Refusing to start."
      )


_scrub_sys_path()
_assert_platform_not_importable()


# Now safe to import the rest of stdlib + our frozen siblings.
import html  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
from http import HTTPStatus  # noqa: E402
from http.cookies import SimpleCookie  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import recovery_auth  # noqa: E402
import recovery_db  # noqa: E402
import recovery_pages  # noqa: E402


logging.basicConfig(
  level=logging.INFO,
  format="%(asctime)s recoveryd %(levelname)s %(message)s",
)
log = logging.getLogger("recoveryd")


# ---------------------------------------------------------------------------
# Configuration (env-driven, no app.config import).
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
RECOVERY_PORT = int(os.environ.get("RECOVERY_PORT", "8001"))
# Where to probe for platform health. On the compose network the platform
# service is reachable by its service name; override per environment.
PLATFORM_HEALTH_URL = os.environ.get(
  "RECOVERY_PLATFORM_HEALTH_URL", "http://app:8000/api/health"
)
# Host(s) the recovery surface is served under, for the Origin check. A
# comma-separated allowlist; empty disables the Origin host comparison
# (the Sec-Fetch-Site check still applies). Set RECOVERY_ALLOWED_HOSTS to
# the public DOMAIN in prod.
_ALLOWED_HOSTS = {
  h.strip().lower()
  for h in os.environ.get("RECOVERY_ALLOWED_HOSTS", "").split(",")
  if h.strip()
}

RECOVER_PENDING = DATA_DIR / ".recover-pending"
RESTART_SENTINEL = DATA_DIR / ".platform-restart-requested"
LAST_BOOT = DATA_DIR / ".last-successful-boot"
CLI_CREDS = DATA_DIR / "cli-auth" / "claude"

# The frozen-bundle files whose integrity is asserted at startup.
_FROZEN_FILES = [
  "recoveryd.py",
  "recovery_auth.py",
  "recovery_db.py",
  "recovery_pages.py",
]

# Valid Tier-1 restore modes. Kept in lockstep with the entrypoint's
# `.recover-pending` handler (entrypoint.sh ~909) and recovery_restore.sh.
_RESTORE_MODES = {"platform", "platform-baked"}


# ---------------------------------------------------------------------------
# Startup self-integrity — refuse to run from a tampered bundle.
# ---------------------------------------------------------------------------

def _assert_self_integrity() -> None:
  """Asserts every frozen source file is root-owned and not writable by
  group/other. A tampered (agent-writable) module is a breached floor;
  refuse to start loudly rather than serve compromised recovery code.

  The check is skipped only when RECOVERY_SKIP_INTEGRITY=1 is set
  explicitly (unit tests + local non-root dev), never in the image.
  """
  if os.environ.get("RECOVERY_SKIP_INTEGRITY") == "1":
    log.warning("self-integrity check SKIPPED (RECOVERY_SKIP_INTEGRITY=1)")
    return
  here = Path(__file__).resolve().parent
  problems: list[str] = []
  for name in _FROZEN_FILES:
    p = here / name
    try:
      st = p.stat()
    except OSError as exc:
      problems.append(f"{name}: cannot stat ({exc})")
      continue
    if st.st_uid != 0:
      problems.append(f"{name}: not root-owned (uid={st.st_uid})")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
      problems.append(
        f"{name}: group/other-writable (mode={oct(st.st_mode & 0o777)})"
      )
  if problems:
    for pr in problems:
      log.error("INTEGRITY FAIL: %s", pr)
    raise SystemExit(
      "FATAL: recoveryd self-integrity check failed — refusing to start. "
      "The frozen bundle must be root-owned and non-writable."
    )
  log.info("self-integrity OK: %d frozen files root-owned + read-only",
           len(_FROZEN_FILES))


# ---------------------------------------------------------------------------
# Platform health + status.
# ---------------------------------------------------------------------------

def _probe_platform_health(timeout: float = 2.0) -> bool | None:
  """Returns True if the platform answers /api/health 200, False if it
  refuses/errors, None if unknown (DNS not resolvable etc).

  Bounded timeout so a hung platform never hangs the recovery request
  thread.
  """
  try:
    req = urllib.request.Request(PLATFORM_HEALTH_URL, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      return 200 <= resp.status < 300
  except urllib.error.HTTPError as exc:
    # The platform answered, just not 200 — it's up but unhealthy.
    return 200 <= exc.code < 300
  except (urllib.error.URLError, socket.timeout, OSError):
    return False
  except Exception:
    return None


def build_status() -> dict:
  """Assembles the status dict the dashboard + status.json both use."""
  try:
    last_boot = LAST_BOOT.read_text(encoding="utf-8").strip() or None
  except OSError:
    last_boot = None
  return {
    "platform": {"healthy": _probe_platform_health()},
    "last_successful_boot": last_boot,
    "cli_creds_present": CLI_CREDS.is_dir(),
    "recovery": "ok",
    "owner_configured": recovery_db.owner_exists(),
  }


# ---------------------------------------------------------------------------
# Request handler.
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
  """Routes recovery requests. All response bodies are built by pure
  functions in recovery_pages; this class owns only HTTP framing,
  cookie/form parsing, and the cross-site guard."""

  server_version = "recoveryd/1.0"
  protocol_version = "HTTP/1.1"

  # -- low-level helpers --------------------------------------------------

  def _send(self, code: int, body: str, *, content_type: str = "text/html",
            extra_headers: dict | None = None) -> None:
    raw = body.encode("utf-8")
    self.send_response(code)
    self.send_header("Content-Type", f"{content_type}; charset=utf-8")
    self.send_header("Content-Length", str(len(raw)))
    # Recovery pages are never cached — always reflect live state.
    self.send_header("Cache-Control", "no-store")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("Referrer-Policy", "same-origin")
    for k, v in (extra_headers or {}).items():
      self.send_header(k, v)
    self.end_headers()
    if self.command != "HEAD":
      self.wfile.write(raw)

  def _cookie(self, name: str) -> str | None:
    raw = self.headers.get("Cookie")
    if not raw:
      return None
    try:
      jar = SimpleCookie()
      jar.load(raw)
      morsel = jar.get(name)
      return morsel.value if morsel else None
    except Exception:
      return None

  def _set_cookie_header(self, token: str) -> str:
    """Builds the literal Set-Cookie header for the recovery session.

    `Secure` is set UNCONDITIONALLY — recoveryd only ever serves behind
    the TLS-terminating reverse proxy. `SameSite=Strict` + `HttpOnly` +
    `Path=/recover` complete the CSRF-resistant cookie. A unit test
    asserts this exact shape.
    """
    return (
      f"{recovery_auth.COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; "
      f"Secure; Path=/recover; Max-Age={recovery_auth.SESSION_TTL_SECONDS}"
    )

  def _clear_cookie_header(self) -> str:
    return (
      f"{recovery_auth.COOKIE_NAME}=; HttpOnly; SameSite=Strict; Secure; "
      f"Path=/recover; Max-Age=0"
    )

  def _read_form(self) -> dict[str, str]:
    try:
      length = int(self.headers.get("Content-Length", "0"))
    except (TypeError, ValueError):
      length = 0
    # Bound the body so a malicious client can't exhaust memory; recovery
    # forms are tiny (username/password/mode).
    if length <= 0 or length > 64 * 1024:
      return {}
    raw = self.rfile.read(length).decode("utf-8", "replace")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}

  def _authed_username(self) -> str | None:
    """Returns the owner username if the session cookie is valid AND the
    owner row still exists (a factory-reset deletes the row but the
    HMAC stays valid until expiry), else None."""
    token = self._cookie(recovery_auth.COOKIE_NAME)
    username = recovery_auth.decode_session_token(token)
    if not username or not recovery_db.owner_exists_for(username):
      return None
    return username

  def _reject_cross_site(self) -> bool:
    """Returns True if the request must be rejected as cross-site.

    Mirrors the platform's deps.reject_cross_site: block
    `Sec-Fetch-Site: cross-site`; when the header is absent (ancient
    client), require the Origin/Referer host to match the allowlist if
    one is configured. Applied to EVERY state-changing POST.
    """
    sfs = self.headers.get("Sec-Fetch-Site")
    if sfs is not None:
      return sfs.lower() == "cross-site"
    # No Sec-Fetch-Site: fall back to an Origin/Referer host check, but
    # only if an allowlist is configured (else we can't know our host).
    if not _ALLOWED_HOSTS:
      return False
    ref = self.headers.get("Origin") or self.headers.get("Referer")
    if not ref:
      # A missing Origin on a same-origin POST is common; not by itself
      # evidence of an attack.
      return False
    try:
      host = urllib.parse.urlparse(ref).hostname or ""
    except Exception:
      return True
    return host.lower() not in _ALLOWED_HOSTS

  # -- routing ------------------------------------------------------------

  def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
    path = urllib.parse.urlparse(self.path).path
    if path == "/recover/health":
      self._send(HTTPStatus.OK, "ok", content_type="text/plain")
      return
    if path == "/recover/status.json":
      self._send(
        HTTPStatus.OK, json.dumps(build_status()),
        content_type="application/json",
      )
      return
    if path == "/recover" or path == "/recover/":
      self._render_recover_page()
      return
    self._send(HTTPStatus.NOT_FOUND, "not found", content_type="text/plain")

  def do_HEAD(self) -> None:  # noqa: N802
    self.do_GET()

  def do_POST(self) -> None:  # noqa: N802
    # Read (drain) the request body FIRST, unconditionally. With HTTP/1.1
    # keep-alive, a handler that replies before consuming the body leaves
    # those bytes in the socket buffer, where they get mis-parsed as the
    # next request ("Bad request syntax"). Draining up front makes every
    # early-return path connection-safe.
    form = self._read_form()
    path = urllib.parse.urlparse(self.path).path
    if path not in ("/recover/auth", "/recover/restore", "/recover/logout"):
      self._send(HTTPStatus.NOT_FOUND, "not found", content_type="text/plain")
      return
    # Cross-site guard on every state-changing POST.
    if self._reject_cross_site():
      self._send(
        HTTPStatus.FORBIDDEN, "Cross-site request blocked.",
        content_type="text/plain",
      )
      return
    if path == "/recover/auth":
      self._handle_auth(form)
    elif path == "/recover/restore":
      self._handle_restore(form)
    elif path == "/recover/logout":
      self._handle_logout()

  # -- handlers -----------------------------------------------------------

  def _render_recover_page(self) -> None:
    # First-boot-takeover guard: no owner row -> read-only page.
    if not recovery_db.owner_exists():
      self._send(HTTPStatus.OK, recovery_pages.not_configured_html())
      return
    if self._authed_username():
      self._send(HTTPStatus.OK, recovery_pages.dashboard_html(build_status()))
    else:
      self._send(HTTPStatus.OK, recovery_pages.login_html())

  def _handle_auth(self, form: dict[str, str]) -> None:
    # Refuse auth entirely until an owner exists (first-boot guard).
    if not recovery_db.owner_exists():
      self._send(HTTPStatus.OK, recovery_pages.not_configured_html())
      return
    username = form.get("username", "")
    password = form.get("password", "")
    pw_hash = recovery_db.owner_password_hash(username)
    # Constant-time-ish: always run bcrypt against a hash so a missing
    # user and a wrong password take the same time. recovery_auth's
    # verify_password handles a malformed hash by returning False.
    candidate = pw_hash if pw_hash else _DUMMY_HASH
    if not recovery_auth.verify_password(password, candidate) or not pw_hash:
      self._send(
        HTTPStatus.OK,
        recovery_pages.login_html(error="Incorrect username or password."),
      )
      return
    token = recovery_auth.create_session_token(username)
    self._send(
      HTTPStatus.OK,
      recovery_pages.dashboard_html(build_status()),
      extra_headers={"Set-Cookie": self._set_cookie_header(token)},
    )

  def _handle_logout(self) -> None:
    self._send(
      HTTPStatus.OK,
      recovery_pages.login_html(),
      extra_headers={"Set-Cookie": self._clear_cookie_header()},
    )

  def _handle_restore(self, form: dict[str, str]) -> None:
    # Destructive route: require BOTH a valid session AND a live owner.
    if not recovery_db.owner_exists():
      self._send(
        HTTPStatus.FORBIDDEN, "Not configured.", content_type="text/plain",
      )
      return
    if not self._authed_username():
      self._send(
        HTTPStatus.UNAUTHORIZED, "Not signed in.", content_type="text/plain",
      )
      return
    mode = form.get("mode", "")
    if mode not in _RESTORE_MODES:
      self._send(
        HTTPStatus.OK,
        recovery_pages.dashboard_html(
          build_status(), msg="Unknown restore mode."),
      )
      return
    ok, detail = schedule_restore(mode)
    if not ok:
      self._send(
        HTTPStatus.OK,
        recovery_pages.dashboard_html(
          build_status(), msg=f"Restore could not be scheduled: {detail}"),
      )
      return
    msg = (
      f"Restore scheduled ({html.escape(mode)}). The platform is restarting "
      "now — reload this page in ~30 seconds, then check the app."
    )
    self._send(HTTPStatus.OK, recovery_pages.dashboard_html(build_status(), msg=msg))

  # -- logging ------------------------------------------------------------

  def log_message(self, fmt: str, *args) -> None:  # noqa: A003
    # Route stdlib access logs through our logger (and never log bodies).
    log.info("%s - %s", self.address_string(), fmt % args)


# ---------------------------------------------------------------------------
# Restore scheduling — write the two flag files the platform acts on.
# ---------------------------------------------------------------------------

def schedule_restore(mode: str) -> tuple[bool, str]:
  """Writes `.recover-pending=<mode>` then the restart sentinel.

  Ordering is load-bearing: `.recover-pending` is written and flushed
  FIRST so that by the time the platform's poller sees the restart
  sentinel and cycles pid1, the mode flag is already on disk for the
  next boot's root-context handler to act on. The poller only watches
  the sentinel, so writing the pending file first closes the race.

  Returns (ok, detail). Never raises — a recovery action that 500s is
  the worst failure mode.
  """
  if mode not in _RESTORE_MODES:
    return False, f"invalid mode {mode!r}"
  try:
    _atomic_write(RECOVER_PENDING, mode)
  except OSError as exc:
    return False, f"could not write recover-pending: {exc}"
  try:
    _atomic_write(RESTART_SENTINEL, "1")
  except OSError as exc:
    # Roll back the pending flag so a stale half-written restore doesn't
    # fire on the NEXT unrelated restart.
    try:
      RECOVER_PENDING.unlink(missing_ok=True)
    except OSError:
      pass
    return False, f"could not write restart sentinel: {exc}"
  log.info("restore scheduled: mode=%s (sentinel written)", mode)
  return True, "ok"


def _atomic_write(path: Path, content: str) -> None:
  """Writes `content` to `path` via temp-file + atomic rename + fsync, so a
  partial flag is never observed by the boot-time handler."""
  tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
  fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
  try:
    os.write(fd, content.encode("utf-8"))
    os.fsync(fd)
  finally:
    os.close(fd)
  os.replace(str(tmp), str(path))


# A pre-computed bcrypt hash so login timing for a missing user matches a
# wrong-password attempt (defeats username enumeration via timing). Built
# lazily at startup after bcrypt is importable.
_DUMMY_HASH = ""


def _init_dummy_hash() -> None:
  global _DUMMY_HASH
  import bcrypt
  _DUMMY_HASH = bcrypt.hashpw(b"__dummy_password__", bcrypt.gensalt()).decode()


def main() -> None:
  _assert_self_integrity()
  _init_dummy_hash()
  server = ThreadingHTTPServer(("0.0.0.0", RECOVERY_PORT), _Handler)
  # Daemonic worker threads so a hung handler can't block shutdown.
  server.daemon_threads = True
  log.info(
    "recoveryd listening on 0.0.0.0:%d (platform health: %s)",
    RECOVERY_PORT, PLATFORM_HEALTH_URL,
  )
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    server.server_close()


if __name__ == "__main__":
  main()
