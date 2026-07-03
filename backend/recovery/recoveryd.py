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
import urllib.error  # noqa: E402
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

# The directory this running bundle lives in. Baked -> /app/recovery;
# after a launcher hand-off (see resolve_run_dir) -> /data/recovery-live.
# Mirrors the local `recovery_dir` computed in _scrub_sys_path, which runs
# before this module constant is bound.
_SELF_DIR = Path(__file__).resolve().parent

# The optional live copy on the persistent volume. It is preferred over
# the baked floor ONLY when it passes bundle_is_trusted (see
# resolve_run_dir); a fresh volume or an untrusted copy falls back to
# baked, which is the guaranteed floor.
LIVE_DIR = DATA_DIR / "recovery-live"

# Valid Tier-1 restore modes. Kept in lockstep with the entrypoint's
# `.recover-pending` handler (entrypoint.sh ~909) and recovery_restore.sh.
_RESTORE_MODES = {"platform", "platform-baked"}


# ---------------------------------------------------------------------------
# Bundle trust — the root-ownership integrity rule, reused by both the
# self check and the launcher's live-copy decision.
# ---------------------------------------------------------------------------

def _uid_and_mode(path: str) -> tuple[int, int]:
  """Returns (st_uid, st_mode) for `path` via os.stat.

  This is the single ownership/mode lookup, isolated so tests can
  simulate root-owned files (which a non-root test process cannot
  create) by monkeypatching this one function. Production always hits
  the real os.stat here, so the trust check is never env-bypassable.
  """
  st = os.stat(path)
  return st.st_uid, st.st_mode


def bundle_is_trusted(dir_path) -> bool:
  """Returns True iff `dir_path` is a directory whose every `*.py` file
  (recursively) is owned by root (uid 0) and not group/other-writable.

  This is the load-bearing trust rule for a floor that runs as root: it
  may execute code from a directory ONLY when the agent — a non-root
  user with write access to /data — could not have forged or modified
  any Python in it. root ownership is unforgeable by the agent (only
  root can chown to root); a not-group/other-writable mode means the
  agent cannot rewrite a root-owned file it does not own. The user-write
  bit is deliberately NOT checked: only root owns these files and root
  writes regardless of it, so it carries no security signal.

  A directory with no Python at all is untrusted — there is nothing to
  verify and nothing runnable. Never raises: a missing directory or any
  stat error resolves to False (untrusted), so a broken live copy can
  only ever cause a fallback to the baked floor, never a crash.
  """
  try:
    d = Path(dir_path)
    if not d.is_dir():
      return False
    py_files = sorted(d.rglob("*.py"))
  except OSError:
    return False
  if not py_files:
    return False
  for p in py_files:
    try:
      uid, mode = _uid_and_mode(str(p))
    except OSError:
      return False
    if uid != 0:
      return False
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
      return False
  return True


# ---------------------------------------------------------------------------
# Startup self-integrity — refuse to run from a tampered bundle.
# ---------------------------------------------------------------------------

def _assert_self_integrity() -> None:
  """Refuses to run from a tampered bundle.

  The running bundle (_SELF_DIR — baked /app/recovery, or the live copy
  after a launcher hand-off) must pass bundle_is_trusted: every source
  file root-owned and not group/other-writable. A tampered
  (agent-writable) module is a breached floor, so fail loudly rather
  than serve compromised recovery code.

  Skipped ONLY when RECOVERY_SKIP_INTEGRITY=1 is set explicitly (unit
  tests + local non-root dev), never in the image. The bypass applies to
  this SELF check alone; bundle_is_trusted itself is never
  env-bypassable, so the launcher's live-copy trust decision cannot be
  disabled.
  """
  if os.environ.get("RECOVERY_SKIP_INTEGRITY") == "1":
    log.warning("self-integrity check SKIPPED (RECOVERY_SKIP_INTEGRITY=1)")
    return
  if not bundle_is_trusted(_SELF_DIR):
    log.error(
      "INTEGRITY FAIL: %s is not a trusted bundle — every source file must "
      "be root-owned and not group/other-writable", _SELF_DIR,
    )
    raise SystemExit(
      "FATAL: recoveryd self-integrity check failed — refusing to start. "
      "The frozen bundle must be root-owned and non-writable."
    )
  log.info("self-integrity OK: %s is a trusted (root-owned) bundle", _SELF_DIR)


# ---------------------------------------------------------------------------
# Launcher — prefer a trusted /data live copy over the baked floor.
# ---------------------------------------------------------------------------

# Set in the environment right before os.execv so the re-exec'd process
# knows it must run in place instead of execing again (loop guard).
_EXEC_SENTINEL_ENV = "MOBIUS_RECOVERY_EXECED"


def resolve_run_dir() -> str:
  """Returns the directory recoveryd should run from.

  Prefers the live copy at LIVE_DIR when it exists, contains a
  recoveryd.py entrypoint, AND passes bundle_is_trusted (root-owned +
  not group/other-writable — the SAME rule the baked floor uses).
  Otherwise returns the baked self dir, which is the guaranteed
  always-present fallback. Any reason to distrust the live copy resolves
  to baked, so no path can leave recovery unrunnable.
  """
  if (LIVE_DIR / "recoveryd.py").is_file() and bundle_is_trusted(LIVE_DIR):
    return str(LIVE_DIR)
  return str(_SELF_DIR)


def _maybe_reexec_into_run_dir() -> None:
  """Re-execs into the trusted live copy when it differs from the running
  bundle, so a baked process hands off to a newer /data copy at startup.

  Guarded against an exec loop by the _EXEC_SENTINEL_ENV sentinel: it is
  set in the environment before os.execv, so the re-exec'd process sees
  it and runs in place instead of execing again. os.execv replaces the
  current process image, so this function returns only when NO hand-off
  happens — already re-exec'd, or the resolved run dir IS the running
  dir.
  """
  if os.environ.get(_EXEC_SENTINEL_ENV) == "1":
    return
  run_dir = resolve_run_dir()
  if os.path.realpath(run_dir) == os.path.realpath(str(_SELF_DIR)):
    return
  target = os.path.join(run_dir, "recoveryd.py")
  # Preserve the interpreter's `-P` hardening (drops the script dir from
  # sys.path[0]) and pass any extra argv through unchanged.
  argv = [sys.executable, "-P", target, *sys.argv[1:]]
  os.environ[_EXEC_SENTINEL_ENV] = "1"
  log.info("launcher: re-exec into trusted live copy at %s", run_dir)
  os.execv(sys.executable, argv)


# ---------------------------------------------------------------------------
# Version — the running bundle's own version, read from its VERSION file.
# ---------------------------------------------------------------------------

def running_version() -> str:
  """Returns the semver string of the currently-running recovery bundle.

  Reads the VERSION file from the running bundle dir (resolve_run_dir() —
  the trusted /data live copy after a launcher hand-off, else the baked
  floor). Returns its trimmed contents, or "0.0.0" when the file is
  missing, unreadable, or blank. A bundle with no version is treated as
  the lowest possible version so any tagged upstream release looks newer.
  """
  try:
    text = (Path(resolve_run_dir()) / "VERSION").read_text(encoding="utf-8")
  except OSError:
    return "0.0.0"
  return text.strip() or "0.0.0"


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
    if sfs is not None and sfs.lower() == "cross-site":
      return True
    # INDEPENDENTLY of Sec-Fetch-Site: if an allowlist is configured and an
    # Origin/Referer is present, its host MUST match. A same-site sibling
    # origin can still send a SameSite=Strict cookie, so "same-site" is not a
    # free pass for a destructive POST (Codex vector 4). A missing Origin on a
    # genuine same-origin POST is common and is not by itself an attack.
    if _ALLOWED_HOSTS:
      ref = self.headers.get("Origin") or self.headers.get("Referer")
      if ref:
        try:
          host = urllib.parse.urlparse(ref).hostname or ""
        except Exception:
          return True
        if host.lower() not in _ALLOWED_HOSTS:
          return True
    return False

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
  # Launcher first: hand off to a trusted /data live copy when present so
  # a baked process spends no time on baked-specific setup when a newer,
  # trusted copy should run instead. A re-exec replaces this process; the
  # loop guard makes the successor fall through and run in place.
  _maybe_reexec_into_run_dir()
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
