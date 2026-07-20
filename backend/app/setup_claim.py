"""First-boot claim gate: the one-time secret that authorizes owner setup.

A freshly deployed public instance is owner-claimable by the first caller of
POST /api/auth/setup; the only prior gate was "no Owner row exists", which any
stranger could satisfy. This module adds a possession proof: while the instance
is unconfigured the app lifespan publishes a single claim token to
`<data_dir>/.setup-claim` (0600) and logs it where a deployer already looks
(uvicorn stdout == compose logs). Setup then requires that exact token.

Invariants (all load-bearing):
  - Exactly one published claim: the token file is created atomically (temp
    file + atomic os.link winner, the same shape as
    `backend/recovery/recovery_auth.py`) and never regenerated while it exists.
  - Exactly one unclaimed->owner transition: `SETUP_LOCK` serializes the whole
    owner-recheck -> claim-verify -> owner-create -> claim-consume sequence in
    the setup route, so concurrent setups yield exactly one owner.
  - No silent re-arming after Owner-row loss: consuming the claim writes a
    durable non-secret marker. If the owner row later vanishes (DB wipe or
    corruption) WITHOUT a deliberate factory reset, the marker (or the recovery
    seed) forces setup fail-closed rather than minting a fresh claim a stranger
    could use. Only an explicit factory reset clears the marker.
  - No claim exposure via /data git or the filesystem API: the token file is
    gitignored + untracked (entrypoint) and denied by routes/fs.py.
  - Fail-closed init: if the claim cannot be published while unconfigured,
    `verify` has no valid file to match, so setup stays unavailable rather than
    allowing an unauthenticated takeover — the failure never degrades to "open".
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("moebius.setup_claim")

# The one-time claim token file; the marker records that setup was consumed
# once (non-secret, but still kept out of git + the fs API as runtime state).
_CLAIM_NAME = ".setup-claim"
_MARKER_NAME = ".setup-consumed"

# The recovery seed (owner bcrypt mirror written at setup) doubles as a
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

# Per-process key for a length-blind constant-time compare: HMAC both sides to
# a fixed 32-byte digest before compare_digest so neither the published token's
# length nor the candidate's leaks through timing. The key never leaves the
# process and is not the claim.
_COMPARE_KEY = secrets.token_bytes(32)


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
  """Return the published claim token, or None.

  Strict on read so a tampered or malformed file can never authorize setup: a
  symlink, a non-regular file, any group/other permission bit, an empty file,
  or oversize/non-ASCII content all read as "no claim".
  """
  path = _claim_path(data_dir)
  try:
    st = os.lstat(path)
  except OSError:
    return None
  if not stat.S_ISREG(st.st_mode):
    return None  # a symlink or special file is never the claim we published
  if stat.S_IMODE(st.st_mode) & 0o077:
    return None  # any group/other bit set — not the 0600 we published
  try:
    with open(path, "r", encoding="ascii") as fh:
      data = fh.read(_MAX_CLAIM_LEN + 1)
  except (OSError, ValueError):
    return None  # ValueError covers a non-ASCII (UnicodeDecodeError) body
  token = data.strip()
  if not token or len(token) > _MAX_CLAIM_LEN:
    return None
  return token


def _link_claim_atomic(data_dir, token: str) -> str:
  """Publish `token` via temp file + atomic os.link, winner-takes-all.

  Copies the shape of `backend/recovery/recovery_auth.py`: write a UNIQUE temp
  file at 0600, then os.link it onto the target, which fails atomically if the
  target already exists. Concurrent generators each mint a candidate; exactly
  one wins the link and the losers re-read the winner's token, so every caller
  agrees on one claim. The finally-unlink drops the temp name (the linked file
  keeps the same inode). If the existing target is present but unreadable, it is
  a corrupt claim that would wedge setup forever, so we supersede it atomically.
  """
  path = _claim_path(data_dir)
  os.makedirs(str(path.parent), exist_ok=True)
  fd, tmp = tempfile.mkstemp(prefix=_CLAIM_NAME + ".", dir=str(path.parent))
  replaced = False
  try:
    with os.fdopen(fd, "w") as fh:
      fh.write(token)
    os.chmod(tmp, 0o600)
    try:
      os.link(tmp, path)
      return token
    except FileExistsError:
      winner = _read_claim_file(data_dir)
      if winner:
        return winner
      # target exists but is invalid — replace it so setup isn't wedged.
      os.replace(tmp, path)
      replaced = True
      return token
  finally:
    if not replaced:
      try:
        os.unlink(tmp)
      except OSError:
        pass


def _replace_claim_atomic(data_dir, token: str) -> str:
  """Publish `token` by atomic replace — used for an explicit preset, which is
  authoritative and must overwrite any stale generated value."""
  path = _claim_path(data_dir)
  os.makedirs(str(path.parent), exist_ok=True)
  fd, tmp = tempfile.mkstemp(prefix=_CLAIM_NAME + ".", dir=str(path.parent))
  try:
    with os.fdopen(fd, "w") as fh:
      fh.write(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)  # atomic; the preset value wins
    return token
  except Exception:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def _purge_claim(data_dir) -> None:
  """Delete the claim file if present. Best-effort but logged."""
  try:
    os.unlink(str(_claim_path(data_dir)))
  except FileNotFoundError:
    pass
  except OSError as exc:
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
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
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
  to one uniform 403 so there is no oracle for WHY a claim failed. The compare
  is length-blind: both sides are HMAC'd to a fixed-width digest before
  compare_digest so neither the published token nor the candidate leaks its
  length via timing, and an oversize candidate is rejected before any work.
  """
  published = _read_claim_file(data_dir)
  if not published:
    return False
  if not isinstance(candidate, str) or not candidate:
    return False
  if len(candidate) > _MAX_CLAIM_LEN:
    return False
  a = hmac.new(
    _COMPARE_KEY, candidate.encode("utf-8", "ignore"), hashlib.sha256,
  ).digest()
  b = hmac.new(_COMPARE_KEY, published.encode("utf-8"), hashlib.sha256).digest()
  return hmac.compare_digest(a, b)


def consume(data_dir) -> None:
  """Consume the claim on successful setup: marker first, then delete the file.

  Marker-before-delete is the safe crash order: a crash between the two leaves
  the marker (the instance is fail-closed on the next boot, never re-claimable),
  while a crash before the marker leaves the claim to be re-published. The owner
  row is already committed by the time this runs, so either crash resolves
  correctly on reboot.
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


def _publish_or_read(data_dir) -> str:
  """Resolve the published claim for an unconfigured, not-fail-closed instance.

  Precedence: an explicit MOBIUS_SETUP_CLAIM preset is authoritative and
  overwrites any stale generated value; otherwise a token is generated once and
  reused for the life of the file (never regenerated while it exists).
  """
  preset = _validate_preset(os.environ.get(_ENV_VAR))
  existing = _read_claim_file(data_dir)
  if preset is not None:
    if existing == preset:
      return preset
    return _replace_claim_atomic(data_dir, preset)
  if existing is not None:
    return existing
  return _link_claim_atomic(
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

  Not best-effort in spirit: on a publish failure this raises, and the lifespan
  logs it and serves with NO claim file, so `verify` fails closed — setup stays
  unavailable rather than silently open.
  """
  if owner_exists:
    _purge_claim(data_dir)
    return None
  if is_fail_closed(data_dir):
    _purge_claim(data_dir)  # never leave a stale secret in a locked state
    return None
  return _publish_or_read(data_dir)


def _reset_for_tests(data_dir) -> None:
  """Test-only: drop the claim file + consumed marker so a fresh claim can be
  ensured. Does not touch the recovery seed — conftest owns that removal."""
  for path in (_claim_path(data_dir), _marker_path(data_dir)):
    try:
      os.unlink(str(path))
    except OSError:
      pass
