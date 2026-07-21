"""First-boot claim gate: the one-time secret that authorizes owner setup.

A freshly deployed public instance is owner-claimable by the first caller of
POST /api/auth/setup; the only prior gate was "no Owner row exists", which any
stranger could satisfy. This module adds a possession proof: while the instance
is unconfigured the app lifespan publishes a single claim token to
`<data_dir>/.setup-claim` (0600) and logs it where a deployer already looks
(uvicorn stdout == compose logs). Setup then requires that exact token.

Invariants (all load-bearing):
  - Exactly one published claim: the token file is written atomically under
    `SETUP_LOCK` and never regenerated while it exists.
  - Exactly one unclaimed->owner transition: `SETUP_LOCK` serializes threads,
    which is sufficient for the single-worker deployment.
  - No silent re-arming after Owner-row loss: consuming the claim writes a
    durable non-secret marker. If the owner row later vanishes (DB wipe or
    corruption) WITHOUT a deliberate factory reset, the marker (or the recovery
    seed) forces setup fail-closed rather than minting a fresh claim a stranger
    could use. Only an explicit factory reset clears the marker.
  - No claim exposure via /data git or the filesystem API: the token file is
    gitignored + untracked (entrypoint) and denied by routes/fs.py.
  - Fail-closed init: verification is disabled until this boot successfully
    reconciles the claim. A failed init also purges any old claim best-effort,
    so the failure never degrades to "use the stale secret".
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("moebius.setup_claim")

# The one-time claim token file; the marker records that setup was consumed
# once (non-secret, but still kept out of git + the fs API as runtime state).
_CLAIM_NAME = ".setup-claim"
_MARKER_NAME = ".setup-consumed"

# The recovery seed (owner password-hash mirror written at setup) doubles as a
# "setup already happened here" signal for the fail-closed check. Kept in
# lockstep with app.recovery_seed.OWNER_SEED_PATH's basename.
_RECOVERY_SEED_NAME = ".recovery-owner.json"

_ENV_VAR = "MOBIUS_SETUP_CLAIM"

# Preset hardening. A deployer/test preset must be base64url ASCII; outside a
# test runtime it must also be strong (a weak, fixed, or public value would
# defeat the gate). token_urlsafe(24) generates 32 base64url chars.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MIN_PRESET_LEN = 24
_MAX_CLAIM_LEN = 512
_GENERATED_ENTROPY_BYTES = 24

# The whole unclaimed->owner transition runs under this process lock; the
# setup route acquires it around recheck/verify/create/consume so concurrent
# first-boot setups can produce only one owner. Module-level so every worker
# thread of the one process shares it.
SETUP_LOCK = threading.Lock()

# Per-process boot gate. Lifespan clears this before reconciliation, and only a
# successful ensure_claim() sets it. An old claim file is therefore unusable if
# this boot could not validate/publish the configured claim.
_INIT_SUCCEEDED = threading.Event()

def _claim_path(data_dir) -> Path:
  return Path(data_dir) / _CLAIM_NAME


def _marker_path(data_dir) -> Path:
  return Path(data_dir) / _MARKER_NAME


def _test_runtime() -> bool:
  """Whether the process runs in the dedicated test runtime, which relaxes the
  preset-strength check so CI/harnesses can pin a fixed, known claim value."""
  return os.environ.get("MOBIUS_TEST_RUNTIME") == "1"


def _validate_preset(raw: Optional[str]) -> Optional[str]:
  """Validate the MOBIUS_SETUP_CLAIM preset. Returns the token, or None when
  unset/blank (blank means "generate a random claim").

  Raises ValueError for a set-but-invalid preset. Failing loud is deliberate:
  a bad preset must NOT silently fall back to a generated token the deployer
  can never see — that would leave setup unavailable with no indication why. A
  raise surfaces the misconfiguration in the logs while still leaving no
  weak/unknown claim published (fail-closed).
  """
  if raw is None:
    return None
  value = raw.strip()
  if not value:
    return None
  if not _B64URL_RE.match(value):
    raise ValueError(
      f"{_ENV_VAR} must be base64url (A-Z a-z 0-9 - _)."
    )
  if len(value) > _MAX_CLAIM_LEN:
    raise ValueError(f"{_ENV_VAR} is too long (max {_MAX_CLAIM_LEN} chars).")
  if not _test_runtime() and len(value) < _MIN_PRESET_LEN:
    raise ValueError(
      f"{_ENV_VAR} must be at least {_MIN_PRESET_LEN} chars; weak or fixed "
      "values are allowed only under MOBIUS_TEST_RUNTIME=1."
    )
  return value


def _read_claim_file(data_dir) -> Optional[str]:
  """Return the published claim token, or None for a missing/invalid file."""
  try:
    data = _claim_path(data_dir).read_text(encoding="ascii")
  except (OSError, ValueError):
    return None
  token = data.strip()
  if not token or len(token) > _MAX_CLAIM_LEN:
    return None
  return token


def _fsync_dir(path: Path) -> None:
  """Persist a directory-entry change before returning."""
  flags = os.O_RDONLY
  if hasattr(os, "O_DIRECTORY"):
    flags |= os.O_DIRECTORY
  fd = os.open(str(path), flags)
  try:
    os.fsync(fd)
  finally:
    os.close(fd)


def _write_claim_atomic(data_dir, token: str) -> str:
  """Publish `token` with one temp-file + replace under `SETUP_LOCK`."""
  path = _claim_path(data_dir)
  os.makedirs(str(path.parent), exist_ok=True)
  fd, tmp = tempfile.mkstemp(prefix=_CLAIM_NAME + ".", dir=str(path.parent))
  try:
    with os.fdopen(fd, "w", encoding="ascii") as fh:
      fh.write(token)
      os.fchmod(fh.fileno(), 0o600)
    os.replace(tmp, path)
    return token
  except Exception:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def _purge_claim(data_dir, *, strict: bool = False) -> None:
  """Delete the claim.

  Reconciliation can request strict failure reporting. Consumption is already
  fail-closed once its marker is durable, so it logs a deletion failure.
  """
  path = _claim_path(data_dir)
  try:
    os.unlink(str(path))
  except FileNotFoundError:
    pass
  except OSError as exc:
    if strict:
      raise
    log.warning("could not purge setup claim: %s", exc)


def _write_marker(data_dir) -> None:
  """Write the durable setup-consumed marker via atomic replace.

  The value carries no secret (it only records THAT setup happened), but it is
  written 0600 and kept out of git + the fs API as platform runtime state.
  """
  path = _marker_path(data_dir)
  os.makedirs(str(path.parent), exist_ok=True)
  fd, tmp = tempfile.mkstemp(prefix=_MARKER_NAME + ".", dir=str(path.parent))
  try:
    with os.fdopen(fd, "w") as fh:
      fh.write("setup-consumed\n")
      fh.flush()
      os.fchmod(fh.fileno(), 0o600)
      os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)
  except Exception:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def is_consumed(data_dir) -> bool:
  """True when the durable setup-consumed marker is present."""
  return _marker_path(data_dir).exists()


def _recovery_seed_present(data_dir) -> bool:
  """True when the DB-independent recovery owner seed exists — another durable
  "setup already happened here" signal (best-effort written at setup)."""
  return (Path(data_dir) / _RECOVERY_SEED_NAME).exists()


def is_fail_closed(data_dir) -> bool:
  """Whether an owner-less instance must refuse setup and require recovery.

  Meaningful only when NO owner row exists (the caller checks owner first). An
  instance that already completed setup once (marker) or still carries a
  recovery seed has had an owner; if that owner is gone without a deliberate
  factory reset, re-claiming it would be a takeover, so setup fails closed.
  """
  return is_consumed(data_dir) or _recovery_seed_present(data_dir)


def verify(data_dir, candidate) -> bool:
  """Constant-time check of `candidate` against the published claim.

  Missing/empty/malformed/wrong all return False — the route maps every False
  to one uniform 403 so there is no oracle for WHY a claim failed.
  """
  if not _INIT_SUCCEEDED.is_set():
    return False
  published = _read_claim_file(data_dir)
  if not published:
    return False
  if not isinstance(candidate, str) or not candidate:
    return False
  if len(candidate) > _MAX_CLAIM_LEN:
    return False
  return secrets.compare_digest(
    candidate.encode("utf-8"), published.encode("utf-8"),
  )


def consume(data_dir) -> None:
  """Durably consume the verified claim before committing the owner.

  Marker-before-delete is the safe crash order: a crash between the two leaves
  the marker (the instance is fail-closed on the next boot, never re-claimable),
  while a crash before the marker has not begun the owner write. The route calls
  this before db.commit(), so every later crash is fail-closed even if the owner
  transaction never becomes durable.
  """
  _write_marker(data_dir)
  _purge_claim(data_dir)


def clear_consumed_marker(data_dir) -> None:
  """Clear the setup-consumed marker so the instance can be claimed afresh.

  ONLY a deliberate factory reset should call this — it re-arms first-boot
  setup. Provided here so the marker lifecycle has a single owner; wiring it
  into the (frozen, out-of-scope) recovery factory-reset path is card 263.
  """
  try:
    os.unlink(str(_marker_path(data_dir)))
  except FileNotFoundError:
    pass
  except OSError as exc:
    log.warning("could not clear setup-consumed marker: %s", exc)


def _publish_or_read(data_dir, preset: Optional[str]) -> str:
  """Resolve the published claim for an unconfigured, not-fail-closed instance.

  Precedence: an explicit MOBIUS_SETUP_CLAIM preset is authoritative and
  overwrites any stale generated value; otherwise a token is generated once and
  reused for the life of the file (never regenerated while it exists).
  """
  existing = _read_claim_file(data_dir)
  if preset is not None:
    if existing == preset:
      return preset
    return _write_claim_atomic(data_dir, preset)
  if existing is not None:
    return existing
  return _write_claim_atomic(
    data_dir, secrets.token_urlsafe(_GENERATED_ENTROPY_BYTES)
  )


def ensure_claim(data_dir, *, owner_exists: bool) -> Optional[str]:
  """Reconcile the claim file for the instance's current state; return the
  published token, or None when no claim should exist.

  Called once from the app lifespan after DB init and before serving. The
  caller supplies `owner_exists` (it holds the DB session) so this module stays
  DB-independent.

    - owner present: purge any stale claim; return None (setup is closed).
    - no owner but fail-closed (marker or recovery seed): do NOT advertise a
      claim; return None (setup requires factory reset).
    - no owner, not fail-closed: publish/reuse exactly one claim; return it.

  Any failure leaves this boot's verification gate disabled and attempts to
  remove an old claim before raising. The in-memory gate is authoritative even
  when a filesystem failure prevents that cleanup.
  """
  with SETUP_LOCK:
    begin_initialization()
    try:
      if owner_exists:
        _purge_claim(data_dir, strict=True)
        result = None
      elif is_fail_closed(data_dir):
        _purge_claim(data_dir, strict=True)
        result = None
      else:
        # Validate the preset before reading, replacing, or publishing a claim.
        preset = _validate_preset(os.environ.get(_ENV_VAR))
        result = _publish_or_read(data_dir, preset)
    except Exception:
      _purge_claim(data_dir)
      raise
    _INIT_SUCCEEDED.set()
    return result


def begin_initialization() -> None:
  """Disable verification until claim reconciliation succeeds this boot."""
  _INIT_SUCCEEDED.clear()


def _reset_for_tests(data_dir) -> None:
  """Test-only: drop the claim file + consumed marker so a fresh claim can be
  ensured. Does not touch the recovery seed — conftest owns that removal."""
  begin_initialization()
  for path in (_claim_path(data_dir), _marker_path(data_dir)):
    try:
      os.unlink(str(path))
    except OSError:
      pass
