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
import asyncio  # noqa: E402
import fcntl  # noqa: E402
import html  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
from collections import OrderedDict  # noqa: E402
from http import HTTPStatus  # noqa: E402
from http.cookies import SimpleCookie  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import recovery_auth  # noqa: E402
import recovery_chat_pages  # noqa: E402
import recovery_chat_runner  # noqa: E402
import recovery_db  # noqa: E402
import recovery_oauth  # noqa: E402
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
# after a launcher hand-off (see resolve_run_dir) -> the live copy under
# RECOVERY_LIVE_ROOT. Mirrors the local `recovery_dir` computed in
# _scrub_sys_path, which runs before this module constant is bound.
_SELF_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# The live-copy store — a DEDICATED, recoveryd-ONLY volume, NOT shared /data.
#
# SECURITY (root-escalation defense): recoveryd runs as ROOT and execs the
# live copy by PATH. If the live copy lived on /data (agent-writable), the
# agent — a non-root user with write access to /data — could win a TOCTOU
# race: between the trust check and the exec, it renames the trusted,
# root-owned live dir aside and renames an agent-owned dir into its place, so
# execv loads AGENT code as root (its module-level body runs before any
# self-integrity check). Owning the FILES root can't stop this, because
# directory-entry replacement is governed by write permission on the PARENT,
# and /data's parent must stay agent-writable by design.
#
# The fix is structural: put the entire live copy (code + its crash-loop
# counter + the pull's temp/clone dirs) on a volume the AGENT'S CONTAINER
# NEVER MOUNTS. With no filesystem path to it from the agent's side, the
# agent cannot rename, unlink, or swap it at any level, and the TOCTOU window
# closes completely. RECOVERY_LIVE_ROOT is env-overridable ONLY so tests can
# point it at a tmp dir; the prod default is the real recoveryd-only mount
# (see docker-compose.yml, where `recovery_live` is mounted into the recoveryd
# service alone and the `app` service does not mount it). Auth/db/sentinels
# stay on shared /data (recoveryd must exchange those with the platform);
# ONLY the live recovery CODE + its attempts counter move here.
# ---------------------------------------------------------------------------
RECOVERY_LIVE_ROOT = Path(
  os.environ.get("RECOVERY_LIVE_ROOT", "/recovery-live")
)

# The optional live copy on the recoveryd-only volume. It is preferred over
# the baked floor ONLY when it passes bundle_is_trusted (see resolve_run_dir);
# a fresh volume or an untrusted copy falls back to baked, the guaranteed
# floor.
LIVE_DIR = RECOVERY_LIVE_ROOT / "live"

# The previous live copy is moved aside to this path during an atomic swap
# (see _swap_into_live) and removed best-effort afterwards. Same filesystem
# as LIVE_DIR (both under RECOVERY_LIVE_ROOT) so the swap is a pure rename.
LIVE_DIR_OLD = RECOVERY_LIVE_ROOT / "live.old"

# Serializes concurrent pulls (see pull_latest_recovery) via fcntl.flock so a
# double-click can't race the swap. Lives on the recoveryd-only volume.
_PULL_LOCK = RECOVERY_LIVE_ROOT / ".pull.lock"

# Persistent crash-loop guard for the live copy. recoveryd (root) bumps this
# counter just before it execs into the live copy and resets it once the live
# copy reaches a bound, serving state. It lives on the recoveryd-only volume
# (which persists across a container restart) — unlike the in-process exec
# sentinel — so a trusted-but-crashing live copy that dies before serving
# cannot loop past the baked floor forever: after _MAX_LIVE_ATTEMPTS the
# launcher quarantines to baked. Written by recoveryd as root; kept off
# /data so the agent cannot forge a low count to defeat the quarantine.
ATTEMPTS_FILE = RECOVERY_LIVE_ROOT / ".attempts"
_MAX_LIVE_ATTEMPTS = 3

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


def _path_is_root_locked(path: str) -> bool:
  """Returns True iff `path` is owned by root (uid 0) and not
  group/other-writable.

  The atom of the trust rule, shared by the file and directory passes of
  bundle_is_trusted. root ownership is unforgeable by the agent (only root
  can chown to root); a clear group/other write bit means the agent cannot
  rewrite a root-owned entry it does not own. The user-write bit is
  deliberately NOT checked: only root owns these entries and root writes
  regardless of it, so it carries no security signal. Never raises: a stat
  error resolves to False (untrusted), so a vanished or inaccessible path
  can only cause a fallback to the baked floor, never a crash.
  """
  try:
    uid, mode = _uid_and_mode(path)
  except OSError:
    return False
  if uid != 0:
    return False
  if mode & (stat.S_IWGRP | stat.S_IWOTH):
    return False
  return True


def bundle_is_trusted(dir_path) -> bool:
  """Returns True iff `dir_path` is a directory whose every `*.py` file AND
  every directory (the bundle root plus all subdirectories, recursively) is
  owned by root (uid 0) and not group/other-writable.

  This is the load-bearing trust rule for a floor that runs as root: it may
  execute code from a directory ONLY when the agent — a non-root user with
  write access to /data — could not have forged or modified any Python in
  it. Checking the files is not enough: a root-owned file inside an
  agent-writable directory is still unsafe, because the agent can unlink it
  and drop a replacement (directory write permission governs the namespace,
  not file ownership), or win a TOCTOU race between this check and the exec.
  So every directory in the tree must itself be root-owned and not
  group/other-writable, closing that gap.

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
    subdirs = [p for p in d.rglob("*") if p.is_dir()]
  except OSError:
    return False
  if not py_files:
    return False
  # Files first: an untrusted *.py fails fast before the directory pass.
  for p in py_files:
    if not _path_is_root_locked(str(p)):
      return False
  # Then the bundle root and every subdirectory — a writable dir voids the
  # trust even when all files are root-owned and read-only.
  for dpath in (d, *subdirs):
    if not _path_is_root_locked(str(dpath)):
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
# Launcher — prefer a trusted live copy (on the recoveryd-only volume) over
# the baked floor.
# ---------------------------------------------------------------------------

# Set in the environment right before os.execv so the re-exec'd process
# knows it must run in place instead of execing again (loop guard).
_EXEC_SENTINEL_ENV = "MOBIUS_RECOVERY_EXECED"


def _live_attempts() -> int:
  """Returns the persisted live-copy start count, or 0 when unreadable.

  A missing, empty, or garbage counter reads as 0 rather than raising, so a
  corrupt attempts file can never wedge the launcher — the worst case is a
  reset budget, which only ever gives the live copy MORE chances, never
  fewer than the safe fallback would allow.
  """
  try:
    return int(ATTEMPTS_FILE.read_text(encoding="utf-8").strip())
  except (OSError, ValueError):
    return 0


def _bump_live_attempts() -> bool:
  """Increments the persisted live-copy start count. Returns whether it stuck.

  Ensures the recoveryd-only volume exists first so the very first bump
  (before any pull created it) still lands. Returns True when the higher
  count was durably written, False on any write failure. The caller uses a
  False return to fail TOWARD the baked floor: if the crash-loop counter
  cannot be persisted (e.g. ENOSPC), an untracked live-copy start could loop
  forever, so the launcher declines the live copy rather than run it blind.
  """
  try:
    os.makedirs(RECOVERY_LIVE_ROOT, exist_ok=True)
    _atomic_write(ATTEMPTS_FILE, str(_live_attempts() + 1))
    return True
  except OSError as exc:
    log.warning("could not bump live-attempts counter: %s", exc)
    return False


def _reset_live_attempts() -> None:
  """Clears the live-copy start count (best-effort).

  Ensures the recoveryd-only volume exists first; any write failure is
  swallowed so a counter-write problem can never crash the launcher.
  """
  try:
    os.makedirs(RECOVERY_LIVE_ROOT, exist_ok=True)
    _atomic_write(ATTEMPTS_FILE, "0")
  except OSError as exc:
    log.warning("could not reset live-attempts counter: %s", exc)


def _running_live_copy() -> bool:
  """True when this process is running from the live copy, not baked.

  After the launcher execs into LIVE_DIR, the successor's _SELF_DIR resolves
  to LIVE_DIR; a baked or quarantined run does not. main() uses this to reset
  the crash-loop counter only on a genuine live-copy start (never on the
  baked fallback that a quarantine put there).
  """
  return os.path.realpath(str(_SELF_DIR)) == os.path.realpath(str(LIVE_DIR))


def resolve_run_dir() -> str:
  """Returns the directory recoveryd should run from.

  Prefers the live copy at LIVE_DIR when it exists, contains a
  recoveryd.py entrypoint, AND passes bundle_is_trusted (root-owned +
  not group/other-writable — the SAME rule the baked floor uses).
  Otherwise returns the baked self dir, which is the guaranteed
  always-present fallback. Any reason to distrust the live copy resolves
  to baked, so no path can leave recovery unrunnable.

  Crash-loop quarantine: once the live copy has been started
  _MAX_LIVE_ATTEMPTS times without reaching a healthy serve loop (which
  resets the counter), it is presumed broken and the baked floor is returned
  even if the live copy still passes the trust check. This is what stops a
  trusted-but-crashing copy from looping forever across container restarts.
  """
  if _live_attempts() >= _MAX_LIVE_ATTEMPTS:
    return str(_SELF_DIR)
  if (LIVE_DIR / "recoveryd.py").is_file() and bundle_is_trusted(LIVE_DIR):
    return str(LIVE_DIR)
  return str(_SELF_DIR)


def _maybe_reexec_into_run_dir() -> None:
  """Re-execs into the trusted live copy when it differs from the running
  bundle, so a baked process hands off to a newer live copy at startup.

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
  # Count this live-copy start BEFORE handing off. execv never returns, and
  # if the live copy crashes before its serve loop resets the counter the
  # bump persists across the container restart, so after _MAX_LIVE_ATTEMPTS
  # resolve_run_dir quarantines to the baked floor. If the bump cannot be
  # persisted, decline the live copy and run baked in-place — an untracked
  # start could otherwise crash-loop forever with no way to reach the floor.
  if not _bump_live_attempts():
    log.warning("launcher: cannot persist attempts counter — running baked "
                "floor instead of the live copy")
    return
  os.environ[_EXEC_SENTINEL_ENV] = "1"
  log.info("launcher: re-exec into trusted live copy at %s", run_dir)
  os.execv(sys.executable, argv)


# ---------------------------------------------------------------------------
# Version + "update available" — the running bundle's version and whether
# the pinned upstream has published a newer release. Read-only surfacing:
# nothing here pulls or applies an update (that is Phase 3).
# ---------------------------------------------------------------------------

def running_version() -> str:
  """Returns the semver string of the currently-running recovery bundle.

  Reads the VERSION file from the running bundle dir (resolve_run_dir() —
  the trusted live copy after a launcher hand-off, else the baked
  floor). Returns its trimmed contents, or "0.0.0" when the file is
  missing, unreadable, or blank. A bundle with no version is treated as
  the lowest possible version so any tagged upstream release looks newer.
  """
  try:
    text = (Path(resolve_run_dir()) / "VERSION").read_text(encoding="utf-8")
  except OSError:
    return "0.0.0"
  return text.strip() or "0.0.0"


# The upstream recovery repo, pinned into the frozen bundle so it can never
# be repointed from /data or an agent-writable env in production. recoveryd
# clones it over GitHub TLS as root — the same trust root as the baked image
# itself (which is built from the org's own repos), so no separate signing
# layer is required for the baseline.
_RECOVERY_UPSTREAM_URL = "https://github.com/mobius-os/mobius-recovery.git"


def _upstream_url() -> str:
  """Returns the pinned upstream recovery repo URL.

  A test-only override via RECOVERY_UPSTREAM_URL_TEST lets the suite point
  at a local fixture repo, but it is honored ONLY when
  RECOVERY_SKIP_INTEGRITY=1 is ALSO set — the same bypass that gates the
  self-integrity check. Production never sets RECOVERY_SKIP_INTEGRITY, so
  the override is inert there and the frozen constant always wins: the
  upstream can never be repointed on a real instance.
  """
  if os.environ.get("RECOVERY_SKIP_INTEGRITY") == "1":
    override = os.environ.get("RECOVERY_UPSTREAM_URL_TEST")
    if override:
      return override
  return _RECOVERY_UPSTREAM_URL


def _parse_semver(text: str) -> tuple[int, int, int] | None:
  """Parses a semver-ish string into a (major, minor, patch) int tuple.

  Accepts an optional leading `v`/`V` and ignores any pre-release or build
  metadata (everything from the first `-` or `+`), which is enough for the
  release tags we compare today. Returns None when the core is not exactly
  three dot-separated integers, so a non-semver tag is simply skipped
  rather than mis-ranked.
  """
  s = text.strip()
  if s[:1] in ("v", "V"):
    s = s[1:]
  for sep in ("-", "+"):
    idx = s.find(sep)
    if idx != -1:
      s = s[:idx]
  parts = s.split(".")
  if len(parts) != 3:
    return None
  try:
    return int(parts[0]), int(parts[1]), int(parts[2])
  except ValueError:
    return None


def latest_upstream_version(timeout: float = 10) -> str | None:
  """Returns the highest semver release tag on the pinned upstream, or None.

  Runs `git ls-remote --tags --refs <url>` against _upstream_url() and
  parses the highest semver tag (accepting `vX.Y.Z` and `X.Y.Z`),
  returning it WITHOUT the leading `v`. Non-semver tags are ignored.

  Offline-safe by contract: any failure — network error, missing repo,
  non-zero git exit, or a timeout — returns None instead of raising or
  hanging. The subprocess `timeout` bounds the call so an unreachable
  upstream can never stall the recovery surface.
  """
  try:
    proc = subprocess.run(
      ["git", "ls-remote", "--tags", "--refs", _upstream_url()],
      capture_output=True,
      text=True,
      timeout=timeout,
      check=False,
    )
  except (subprocess.SubprocessError, OSError):
    return None
  if proc.returncode != 0:
    return None
  best: tuple[int, int, int] | None = None
  for line in proc.stdout.splitlines():
    # Each line is "<sha>\trefs/tags/<tag>"; take the tag after the last /.
    ref = line.split("\t")[-1].strip()
    if not ref.startswith("refs/tags/"):
      continue
    parsed = _parse_semver(ref.rsplit("/", 1)[-1])
    if parsed is None:
      continue
    if best is None or parsed > best:
      best = parsed
  if best is None:
    return None
  return f"{best[0]}.{best[1]}.{best[2]}"


def update_available() -> bool:
  """Returns True iff the pinned upstream has a release newer than running.

  True only when latest_upstream_version() is reachable (not None) AND
  semver-greater than running_version(). Any offline/error path makes the
  latest None, so this returns False — recovery never claims an update it
  cannot see.
  """
  latest = latest_upstream_version()
  if latest is None:
    return False
  latest_parsed = _parse_semver(latest)
  if latest_parsed is None:
    return False
  running_parsed = _parse_semver(running_version()) or (0, 0, 0)
  return latest_parsed > running_parsed


# ---------------------------------------------------------------------------
# Pull-and-run — recoveryd (as root) fetches the latest release into a
# root-owned live copy on the recoveryd-ONLY volume (RECOVERY_LIVE_ROOT, which
# the agent's container never mounts), validates it with the SAME trust check
# the baked floor uses, and atomically swaps it in. Because the agent has no
# path to that volume it cannot forge, swap, or delete the live copy, so it
# cannot get its own code executed as root.
# ---------------------------------------------------------------------------

def _clone_release(url: str, version: str, dest: Path,
                   timeout: float) -> bool:
  """Shallow-clones the release tag for `version` into `dest`.

  Tries the `v`-prefixed tag first, then the bare tag, so both `vX.Y.Z` and
  `X.Y.Z` release spellings work. `git clone` refuses a non-empty target, so
  a failed attempt's partial `dest` is removed before the next spelling is
  tried. On success the clone's `.git` directory is dropped — the live copy
  is a frozen runtime bundle that recoveryd never runs git against, so
  carrying history (and a root-owned gitconfig/hooks tree) is dead weight and
  needless trust-check surface. Returns True iff a clone succeeded.
  """
  for ref in (f"v{version}", version):
    try:
      proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--single-branch",
         "--branch", ref, url, str(dest)],
        capture_output=True, text=True, timeout=timeout, check=False,
      )
    except (subprocess.SubprocessError, OSError):
      shutil.rmtree(dest, ignore_errors=True)
      continue
    if proc.returncode == 0 and dest.is_dir():
      shutil.rmtree(dest / ".git", ignore_errors=True)
      return True
    shutil.rmtree(dest, ignore_errors=True)
  return False


def _harden_tree(path: Path) -> bool:
  """Makes `path` root-owned and not group/other-writable, recursively.

  Runs `chown -R root:root` then `chmod -R go-w` over the whole tree so the
  subsequent bundle_is_trusted check can pass and the agent can never rewrite
  the live copy. Only meaningful as root (the caller has already verified
  euid 0); returns False if either step fails so the caller aborts rather
  than swapping in a half-hardened copy.
  """
  for cmd in (
    ["chown", "-R", "root:root", str(path)],
    ["chmod", "-R", "go-w", str(path)],
  ):
    try:
      proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, check=False,
      )
    except (subprocess.SubprocessError, OSError):
      return False
    if proc.returncode != 0:
      return False
  return True


def _validate_pulled(path: Path) -> tuple[bool, str]:
  """Validates a hardened pull BEFORE it may replace LIVE_DIR.

  All three gates must hold or the copy is rejected and never swapped in:
  it carries a recoveryd.py entrypoint, it passes bundle_is_trusted
  (root-owned + not group/other-writable — the same rule the baked floor
  uses), and that entrypoint parses as Python. The parse runs in a separate
  `python3 -P` process that only `ast.parse`s the file — the pulled code is
  never imported or executed here, so validating an untrusted download never
  runs it as root. Operates entirely on `path`; LIVE_DIR is untouched until
  this returns True.
  """
  entry = path / "recoveryd.py"
  if not entry.is_file():
    return False, "pulled copy has no recoveryd.py entrypoint"
  if not bundle_is_trusted(path):
    return False, "pulled copy failed the root-ownership trust check"
  try:
    proc = subprocess.run(
      [sys.executable, "-P", "-c",
       "import ast, sys; ast.parse(open(sys.argv[1]).read())", str(entry)],
      capture_output=True, text=True, timeout=30, check=False,
    )
  except (subprocess.SubprocessError, OSError) as exc:
    return False, f"could not smoke-check the pulled copy: {exc}"
  if proc.returncode != 0:
    return False, "pulled recoveryd.py does not parse as Python"
  return True, "ok"


def _swap_into_live(tmp: Path) -> None:
  """Atomically moves the validated `tmp` tree into LIVE_DIR.

  os.rename is atomic within a filesystem (tmp and LIVE_DIR are both under
  RECOVERY_LIVE_ROOT by construction), so a concurrent reader (resolve_run_dir
  / bundle_is_trusted) never observes a half-written LIVE_DIR: it sees the old
  dir, then — for the brief window between the two renames — no LIVE_DIR at
  all (which resolve_run_dir treats as "run baked", the safe floor), then the
  new dir. There is never a partially-populated LIVE_DIR. The previous copy
  is moved aside first (rename cannot replace a non-empty dir) and removed
  best-effort afterwards.
  """
  if LIVE_DIR_OLD.exists():
    shutil.rmtree(LIVE_DIR_OLD, ignore_errors=True)
  if LIVE_DIR.exists():
    os.rename(str(LIVE_DIR), str(LIVE_DIR_OLD))
  os.rename(str(tmp), str(LIVE_DIR))
  if LIVE_DIR_OLD.exists():
    shutil.rmtree(LIVE_DIR_OLD, ignore_errors=True)


def pull_latest_recovery(timeout: float = 120) -> tuple[bool, str]:
  """Pulls the latest upstream recovery release into a trusted LIVE_DIR.

  The security-critical apply path. Refuses unless running as root (only root
  can produce the root-owned copy the trust check demands). Everything — the
  clone temp dir, the hardened copy, and the atomic swap target — lives under
  RECOVERY_LIVE_ROOT (the recoveryd-only volume the agent's container never
  mounts), so NOTHING here touches shared /data and the agent has no path to
  race the swap. An fcntl.flock serializes concurrent pulls so a double-click
  cannot race. Returns (True, version) on success, else (False, reason). Never
  raises — a recovery action that 500s is the worst failure mode.
  """
  if os.geteuid() != 0:
    return (
      False,
      "refusing to pull: recoveryd must run as root to write a root-owned "
      "live copy",
    )
  # Ensure the recoveryd-only volume exists (root-owned) before we clone or
  # lock under it. On a fresh volume this is the first thing to create it.
  try:
    os.makedirs(RECOVERY_LIVE_ROOT, exist_ok=True)
  except OSError as exc:
    return False, f"could not prepare the live-copy volume: {exc}"
  lock_fd = os.open(str(_PULL_LOCK), os.O_CREAT | os.O_WRONLY, 0o600)
  try:
    try:
      fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
      return False, "update already in progress"
    return _pull_locked(timeout)
  finally:
    # Closing the fd releases the flock (the lock is tied to this open file
    # description), so a crashed/returned pull never leaves it held.
    os.close(lock_fd)


def _pull_locked(timeout: float) -> tuple[bool, str]:
  """Runs the pull with the pull-lock held and root already verified.

  Resolves the latest release tag, clones it into a UNIQUE temp dir under
  RECOVERY_LIVE_ROOT (same filesystem as LIVE_DIR, so the swap is a pure
  rename), hardens it (root-owned + not group/other-writable), then VALIDATES
  the temp copy (trust check + entrypoint present + parses) BEFORE any swap.
  Only a fully validated copy is atomically swapped into LIVE_DIR; any failure
  cleans up the temp dir and leaves the existing LIVE_DIR (and the baked
  floor) untouched.
  """
  version = latest_upstream_version()
  if version is None:
    return False, "no upstream release reachable"
  url = _upstream_url()
  tmp = RECOVERY_LIVE_ROOT / f".recovery-pull-{os.getpid()}-{time.time_ns()}"
  try:
    if not _clone_release(url, version, tmp, timeout):
      return False, f"could not clone recovery release v{version}"
    if not _harden_tree(tmp):
      return False, "could not harden the pulled copy (chown/chmod failed)"
    ok, reason = _validate_pulled(tmp)
    if not ok:
      return False, reason
    _swap_into_live(tmp)
    # A fresh pull deserves a fresh crash-loop try budget: clear any attempts
    # accrued by a previous (now-replaced) live copy so the new one gets its
    # full _MAX_LIVE_ATTEMPTS before quarantine.
    _reset_live_attempts()
    log.info("pulled recovery v%s into %s", version, LIVE_DIR)
    return True, version
  except Exception as exc:  # noqa: BLE001 — never propagate into a request
    log.error("recovery pull failed: %s", exc)
    return False, f"pull failed: {exc}"
  finally:
    # If tmp still exists, the swap never consumed it (any early return or
    # error), so remove it — a failed pull must leave nothing behind.
    if tmp.exists():
      shutil.rmtree(tmp, ignore_errors=True)


def _restart_recoveryd() -> None:
  """Exits the process so the container restart policy re-runs the launcher.

  Called AFTER the update response has been written and flushed to the
  client (never mid-response — os.execv here would kill the reply in flight).
  A short-lived daemon thread gives the response bytes a moment to drain
  through the reverse proxy to the browser, then os._exit(0) drops the
  process; the recoveryd container's `restart: unless-stopped` policy
  immediately recreates it, and the fresh main() runs the launcher, which now
  execs into the just-pulled live copy. os._exit (not sys.exit) so no
  atexit/finally can intercept — this is a deliberate, unconditional restart.
  """
  def _die() -> None:
    time.sleep(1.0)
    os._exit(0)

  threading.Thread(target=_die, daemon=True).start()


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


def _dashboard_status() -> dict:
  """build_status() plus the version + update-available fields the dashboard
  shows.

  Kept OUT of build_status() so the frequently-polled status.json never
  triggers the upstream network check — only a human loading the dashboard
  pays for it. The upstream lookup is wrapped defensively: latest_upstream_
  version is offline-safe (timeout-bounded, returns None on any error), and
  the try/except means a slow or failing check can never block past its
  timeout or bubble into a 500 — it simply means "no update offered". A
  single ls-remote is done here (not update_available() plus a second lookup
  for the display version) so the dashboard never pays for two network round
  trips.
  """
  status = build_status()
  running = running_version()
  status["running_version"] = running
  latest = None
  try:
    latest = latest_upstream_version()
  except Exception:  # noqa: BLE001 — the dashboard must never 500
    latest = None
  status["available_version"] = latest
  latest_parsed = _parse_semver(latest) if latest else None
  running_parsed = _parse_semver(running) or (0, 0, 0)
  status["update_available"] = bool(
    latest_parsed is not None and latest_parsed > running_parsed
  )
  return status


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
    if path not in (
      "/recover/auth", "/recover/restore", "/recover/update", "/recover/logout"
    ):
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
    elif path == "/recover/update":
      self._handle_update(form)
    elif path == "/recover/logout":
      self._handle_logout()

  # -- handlers -----------------------------------------------------------

  def _render_recover_page(self) -> None:
    # First-boot-takeover guard: no owner row -> read-only page.
    if not recovery_db.owner_exists():
      self._send(HTTPStatus.OK, recovery_pages.not_configured_html())
      return
    if self._authed_username():
      self._send(
        HTTPStatus.OK, recovery_pages.dashboard_html(_dashboard_status()))
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

  def _handle_update(self, form: dict[str, str]) -> None:
    # Same gate as _handle_restore: this pulls + runs new root code, so it
    # requires BOTH a live owner AND a valid session. The cross-site guard in
    # do_POST already ran. Reuse the exact checks — do not weaken them.
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
    ok, detail = pull_latest_recovery()
    if not ok:
      self._send(
        HTTPStatus.OK,
        recovery_pages.dashboard_html(
          _dashboard_status(), msg=f"Recovery update failed: {detail}"),
      )
      return
    # Respond FIRST, then restart. dashboard_html escapes msg, so pass the
    # raw version through (a version string is safe, and double-escaping is
    # avoided). The restart runs only after the reply is written + flushed so
    # os.execv/_exit can never truncate the response mid-flight.
    msg = (
      f"Recovery updated to v{detail} — restarting now. Reload this page in "
      "a few seconds."
    )
    self._send(
      HTTPStatus.OK,
      recovery_pages.dashboard_html(_dashboard_status(), msg=msg),
    )
    try:
      self.wfile.flush()
    except OSError:
      pass
    _restart_recoveryd()

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
  # Launcher first: hand off to a trusted live copy when present so
  # a baked process spends no time on baked-specific setup when a newer,
  # trusted copy should run instead. A re-exec replaces this process; the
  # loop guard makes the successor fall through and run in place.
  _maybe_reexec_into_run_dir()
  _assert_self_integrity()
  _init_dummy_hash()
  server = ThreadingHTTPServer(("0.0.0.0", RECOVERY_PORT), _Handler)
  # Daemonic worker threads so a hung handler can't block shutdown.
  server.daemon_threads = True
  # Reaching a bound server from the live copy means it started cleanly, so
  # clear the crash-loop counter and give it a fresh budget. Guard on
  # running-as-the-live-copy so a baked run (including the quarantine
  # fallback) never resets the counter that put it on baked in the first
  # place. Done after the bind succeeds and before serve_forever so a bind
  # failure does not count as a healthy start.
  if _running_live_copy():
    _reset_live_attempts()
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
