"""Container rebuild control for Settings.

The browser can request only fixed official-image operations. Managed Railway
discovers the newest completely published GHCR release, then pins its immutable
revision and digest; a reviewed image-owned update carries that same identity.
Self-hosting keeps rebuilding the applied upstream revision through its narrow
host helper. The root-owned restart ledger preserves chat continuation across
either cutover.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

import httpx

from app import platform_activation, platform_update
from app.config import get_settings
from app.runtime_identity import broker_client


log = logging.getLogger(__name__)


RebuildState = Literal[
  "idle", "queued", "preparing", "replacing", "verifying", "succeeded",
  "no_change", "failed", "rolled_back", "needs_recovery",
]


class RebuildStatus(TypedDict):
  supported: bool
  deployment: platform_activation.DeploymentKind
  operation_id: str | None
  state: RebuildState
  expected_sha: str | None
  code: str | None
  message: str | None
  error: str | None
  image_digest: str | None
  release_source: Literal["applied", "latest_ghcr"]
  updated_at: str | None
  # The app's nonce for the host request this status reports (self-hosted).
  request_nonce: str | None


class OfficialImageRelease(TypedDict):
  build_sha: str
  image_digest: str
  image_ref: str


class DeploymentControlError(RuntimeError):
  """Known owner-action failure with a stable UI code and HTTP status."""

  def __init__(
    self, code: str, message: str, *, status_code: int = 503,
  ) -> None:
    super().__init__(message)
    self.code = code
    self.message = message
    self.status_code = status_code


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_KNOWN_STATES = {
  "idle", "queued", "preparing", "replacing", "verifying", "succeeded",
  "no_change", "failed", "rolled_back", "needs_recovery",
}
_ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}
# Recovery is not running (the browser must stop polling), but the controller
# still owns its unresolved transaction. It is neither a retryable outcome nor
# proof that the exact bound replacement ended.
_UNRESOLVED_STATES = {"needs_recovery"}
# The host path unit (mobius-rebuild.path) claims request.json within seconds.
# Well past that with the host still idle, the helper is not picking up requests
# (for example the path unit is not installed or not running).
_UNCLAIMED_REQUEST_BOUND_S = 120
_HANDOFF_VERSION = "external-cutover-v1"
_HOST_REQUEST_VERSION = 2
_managed_recovery_tasks: set[asyncio.Task[None]] = set()
# The Railway handoff this process can still start or cancel. Its nonce lives
# only in that request, and the account service never expires a prepared
# handoff, so ``awaiting_handoff`` for any other operation is abandoned: nothing
# can ever advance it, and reporting it as running would block every retry.
_managed_handoff: str | None = None
_UPGRADE_MESSAGE = (
  "The Host replacement helper predates the current protected-runtime or "
  "safe external chat-handoff contract. Re-run "
  "scripts/install-rebuild-helper.sh from the current trusted checkout."
)


def _empty_status(
  deployment: platform_activation.DeploymentKind,
  *,
  supported: bool,
  message: str | None = None,
  code: str | None = None,
) -> RebuildStatus:
  return RebuildStatus(
    supported=supported,
    deployment=deployment,
    operation_id=None,
    state="idle",
    expected_sha=None,
    code=code,
    message=message,
    error=None,
    image_digest=None,
    release_source="applied",
    updated_at=None,
    request_nonce=None,
  )


def _schedule_managed_recovery(
  restart: Callable[[], Awaitable[None]],
) -> None:
  """Keep the sole post-rejection restart alive until it settles."""
  task = asyncio.create_task(restart())
  _managed_recovery_tasks.add(task)
  task.add_done_callback(_managed_recovery_tasks.discard)


async def _reconcile_ambiguous_managed_start(
  operation_id: str,
  handoff_nonce: str,
  recover: Callable[[], Awaitable[None]],
) -> None:
  """Recover only when the account atomically cancels an unaccepted Start.

  A timed-out POST may have reached Railway even though this worker saw no
  response. A read of the old state is not proof that the write will never
  commit, so the still-running drained worker asks the account service to race
  Start with one conditional Cancel update against the same handoff state. Only
  an affirmative Cancel result makes local recovery safe; if Start won, the
  account service owns the cutover.
  """

  while True:
    await asyncio.sleep(2)
    try:
      raw = await asyncio.to_thread(
        _managed_request,
        "POST",
        "cancel",
        {"operation_id": operation_id, "handoff_nonce": handoff_nonce},
      )
    except DeploymentControlError:
      continue
    if str(raw.get("operation_id") or "") != operation_id:
      continue
    cancelled = raw.get("cancelled")
    if cancelled is True:
      await _release_binding({"controller": "railway", "id": operation_id})
      await recover()
      return
    if cancelled is False:
      return


def _release_managed_handoff(operation_id: str) -> None:
  global _managed_handoff
  if _managed_handoff == operation_id:
    _managed_handoff = None


def _schedule_ambiguous_start_reconciliation(
  operation_id: str,
  handoff_nonce: str,
  recover: Callable[[], Awaitable[None]],
) -> None:
  """Keep ambiguous Start reconciliation alive after the request returns."""

  task = asyncio.create_task(
    _reconcile_ambiguous_managed_start(operation_id, handoff_nonce, recover),
  )
  _managed_recovery_tasks.add(task)
  task.add_done_callback(_managed_recovery_tasks.discard)
  # Only this task still holds the nonce, so it owns the handoff until it ends.
  task.add_done_callback(lambda _task: _release_managed_handoff(operation_id))


def _control_dir() -> Path:
  return Path(get_settings().data_dir) / "mobius-rebuild"


def _status_path() -> Path:
  return _control_dir() / "status.json"


def _inbox_dir() -> Path:
  return _control_dir() / "inbox"


def _configured() -> bool:
  control = _control_dir()
  inbox = _inbox_dir()
  return (
    control.is_dir()
    and inbox.is_dir()
    and os.access(inbox, os.W_OK | os.X_OK)
    and _status_path().is_file()
  )


def _normalize_status(
  raw: dict[str, Any], *, expected_sha: str | None = None,
) -> RebuildStatus:
  state = str(raw.get("state") or "idle").strip().lower()
  if state not in _KNOWN_STATES:
    raise DeploymentControlError(
      "controller_invalid_response",
      "The host controller returned an unknown replacement state.",
    )
  operation_id = str(raw.get("operation_id") or "").strip() or None
  code = str(raw.get("code") or "").strip() or None
  message = str(raw.get("message") or "").strip() or None
  reported_sha = str(raw.get("expected_sha") or expected_sha or "").strip()
  if reported_sha and not _SHA_RE.fullmatch(reported_sha):
    reported_sha = ""
  updated_at = str(raw.get("updated_at") or "").strip() or None
  nonce = str(raw.get("request_nonce") or "").strip()
  return RebuildStatus(
    supported=True,
    deployment="self_hosted",
    operation_id=operation_id,
    state=state,  # type: ignore[typeddict-item]
    expected_sha=reported_sha or None,
    code=code,
    message=message,
    error=str(raw.get("error") or "").strip() or None,
    image_digest=None,
    release_source="applied",
    updated_at=updated_at,
    request_nonce=nonce if _OPERATION_RE.fullmatch(nonce) else None,
  )


def managed_cutover_ready() -> bool:
  """Whether this exact boot owns the baked managed-cutover supervisor."""
  marker = Path(get_settings().data_dir) / "run" / "managed-cutover-ready"
  try:
    marker_boot_id = marker.read_text(encoding="utf-8").strip()
  except (FileNotFoundError, OSError, UnicodeError):
    return False
  boot_id = str(os.environ.get("MOBIUS_BOOT_ID") or "").strip()
  return bool(boot_id and secrets.compare_digest(marker_boot_id, boot_id))


def _managed_request(method: str, suffix: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
  if not get_settings().mobius_sso_enabled:
    raise DeploymentControlError(
      "not_configured",
      "This Railway deployment is not linked to its Möbius account service.",
      status_code=409,
    )
  request_kwargs: dict[str, Any] = {"json": payload} if payload is not None else {}
  try:
    raw = bytearray()
    with broker_client(timeout=15.0) as client:
      with client.stream(
        method,
        "/managed/api/instance/v1/container-replacement/" + suffix,
        **request_kwargs,
      ) as response:
        status = response.status_code
        for chunk in response.iter_bytes():
          raw.extend(chunk)
          if len(raw) > 64 * 1024:
            raise DeploymentControlError(
              "controller_invalid_response",
              "The account service returned too much data.",
            )
  except (httpx.HTTPError, OSError) as exc:
    raise DeploymentControlError(
      "controller_unavailable", "The Möbius account service is unavailable."
    ) from exc
  if status >= 400:
    try:
      error = json.loads(bytes(raw[:16 * 1024]).decode("utf-8"))
      detail = (
        str(error.get("detail") or error.get("message") or "")
        if isinstance(error, dict) else ""
      )
    except (ValueError, UnicodeError):
      detail = ""
    if status not in {400, 409} or not detail:
      raise DeploymentControlError(
        "controller_unavailable", "The Möbius account service is unavailable."
      )
    raise DeploymentControlError(
      "controller_rejected",
      detail[:360],
      status_code=409,
    )
  try:
    value = json.loads(raw.decode("utf-8"))
  except (ValueError, UnicodeError) as exc:
    raise DeploymentControlError(
      "controller_invalid_response", "The account service returned invalid status."
    ) from exc
  if not isinstance(value, dict):
    raise DeploymentControlError(
      "controller_invalid_response", "The account service returned invalid status."
    )
  return value


def _normalize_managed_status(
  raw: dict[str, Any], *,
  release_source: Literal["applied", "latest_ghcr"] = "latest_ghcr",
  image_digest: str | None = None,
) -> RebuildStatus:
  remote_state = str(raw.get("state") or "idle").lower()
  states: dict[str, RebuildState] = {
    "idle": "idle", "awaiting_handoff": "preparing", "queued": "queued",
    "updating": "preparing", "deploying": "replacing",
    "rolling_back": "verifying", "no_change": "no_change",
    "succeeded": "succeeded", "rolled_back": "rolled_back",
    "failed": "failed", "needs_recovery": "needs_recovery",
  }
  if remote_state not in states:
    raise DeploymentControlError(
      "controller_invalid_response", "The account service returned an unknown state."
    )
  expected = str(raw.get("expected_sha") or "").lower()
  digest = str(raw.get("image_digest") or image_digest or "").lower()
  return RebuildStatus(
    supported=True,
    deployment="railway",
    operation_id=str(raw.get("operation_id") or "") or None,
    state=states[remote_state],
    expected_sha=expected if _SHA_RE.fullmatch(expected) else None,
    code=None,
    message=str(raw.get("message") or "")[:360] or None,
    error=str(raw.get("error") or "")[:1000] or None,
    image_digest=digest if _DIGEST_RE.fullmatch(digest) else None,
    release_source=release_source,
    updated_at=str(raw.get("updated_at") or "") or None,
    request_nonce=None,
  )


async def latest_official_release() -> OfficialImageRelease:
  """Discover the newest fully published official Railway image from GHCR.

  The account service cross-checks mutable ``main`` against its immutable SHA
  tag. This method accepts only the resulting commit+digest identity; callers
  then pass both back to prepare, which prevents a moved tag from changing the
  reviewed bytes.
  """
  raw = await asyncio.to_thread(_managed_request, "GET", "release")
  build_sha = str(raw.get("build_sha") or "").strip().lower()
  image_digest = str(raw.get("image_digest") or "").strip().lower()
  image_ref = str(raw.get("image_ref") or "").strip()
  if not _SHA_RE.fullmatch(build_sha) or not _DIGEST_RE.fullmatch(image_digest):
    raise DeploymentControlError(
      "controller_invalid_response",
      "The account service returned an invalid official image identity.",
    )
  return OfficialImageRelease(
    build_sha=build_sha,
    image_digest=image_digest,
    image_ref=image_ref,
  )


def applied_release_sha() -> str:
  """Translate an unprovable Finish target into the deployment action error."""
  try:
    return platform_update.applied_release_sha()
  except platform_update.PlatformUpdateError as exc:
    raise DeploymentControlError(
      "target_unavailable",
      "The installed release could not be verified. Review the latest update instead.",
      status_code=409,
    ) from exc


async def applied_release_digest(target_sha: str) -> str:
  """Recover an applied release's immutable image identity without retargeting.

  The account service exposes latest-release discovery, not historical lookup.
  The update's own progress record keeps the image it reviewed; the durable
  operation receipt can identify a failed or completed exact target; otherwise
  discovery is useful only when it names the same applied revision.
  """
  progress = platform_update.platform_update_progress()
  digest = str(progress.get("image_digest") or "")
  if progress.get("target_sha") == target_sha and _DIGEST_RE.fullmatch(digest):
    return digest
  status = await read_rebuild_status()
  digest = str(status.get("image_digest") or "")
  if status.get("expected_sha") == target_sha and _DIGEST_RE.fullmatch(digest):
    return digest
  release = await latest_official_release()
  if release["build_sha"] == target_sha:
    return release["image_digest"]
  raise DeploymentControlError(
    "applied_image_unavailable",
    "This installed release no longer has a verified image here. "
    "Review the latest update instead.",
    status_code=409,
  )


def _read_host_status() -> dict[str, Any]:
  try:
    value = json.loads(_status_path().read_text(encoding="utf-8"))
  except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise DeploymentControlError(
      "controller_unavailable",
      "The host replacement status is unavailable.",
    ) from exc
  if not isinstance(value, dict):
    raise DeploymentControlError(
      "controller_invalid_response",
      "The host controller returned unreadable replacement status.",
    )
  request = _inbox_dir() / "request.json"
  if str(value.get("state") or "idle") not in (_ACTIVE_STATES | _UNRESOLVED_STATES) and request.is_file():
    try:
      pending = json.loads(request.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
      pending = {}
    expected = str(pending.get("expected_sha") or "") if isinstance(pending, dict) else ""
    if _SHA_RE.fullmatch(expected):
      # The host worker atomically renames request.json out of the inbox before
      # it acts, so a request still present here was never claimed. Its mtime is
      # when it was queued; a long-idle request means the path unit is not firing.
      try:
        queued_at = datetime.fromtimestamp(request.stat().st_mtime, tz=timezone.utc)
      except OSError:
        queued_at = datetime.now(timezone.utc)
      synthesized = {
        "expected_sha": expected,
        "updated_at": queued_at.isoformat(),
        "handoff": value.get("handoff"),
        "request_versions": value.get("request_versions"),
        "request_nonce": (
          str(pending.get("nonce") or "") or None
          if isinstance(pending, dict) else None
        ),
        # Helpers predating image-owned protected runtime advertised the same
        # handoff version. Preserve their retired capability through this
        # synthesized state so a pending request cannot make them look current.
        "runtime_overlay": value.get("runtime_overlay"),
      }
      unclaimed_for = (datetime.now(timezone.utc) - queued_at).total_seconds()
      if unclaimed_for >= _UNCLAIMED_REQUEST_BOUND_S:
        # Still queued: nothing may start beside it, but the owner learns why it
        # is not moving and can withdraw it.
        return {
          **synthesized,
          "state": "queued",
          "code": "host_helper_unclaimed",
          "message": (
            "The host update helper has not picked up this request. Check it "
            "with `systemctl status mobius-rebuild.path` and, if needed, rerun "
            "scripts/install-rebuild-helper.sh from the trusted Möbius checkout. "
            "You can withdraw this request and try again."
          ),
        }
      return {
        **synthesized,
        "state": "queued",
        "message": "Container rebuild queued.",
      }
  return value


def _current_host_controller(raw: dict[str, Any]) -> bool:
  # Request version 2 carries the app's nonce, which the helper echoes so only
  # that exact replacement can confirm an update (``reconcile_bound_operation``).
  versions = raw.get("request_versions")
  return (
    raw.get("handoff") == _HANDOFF_VERSION
    and not raw.get("runtime_overlay")
    and isinstance(versions, list) and _HOST_REQUEST_VERSION in versions
  )


def _write_request(expected_sha: str, nonce: str) -> None:
  inbox = _inbox_dir()
  request = inbox / "request.json"
  if request.exists():
    raise DeploymentControlError(
      "already_running",
      "A container rebuild request is already queued.",
      status_code=409,
    )
  temp = inbox / f".request-{secrets.token_hex(12)}.tmp"
  payload = json.dumps(
    {"version": _HOST_REQUEST_VERSION, "expected_sha": expected_sha, "nonce": nonce},
    separators=(",", ":"),
  )
  try:
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(payload)
      handle.flush()
      os.fsync(handle.fileno())
    # Publish without overwriting a request queued since the initial read.
    # Both names live in the same durable inbox, so link is an atomic claim.
    os.link(temp, request)
  except FileExistsError as exc:
    raise DeploymentControlError(
      "already_running",
      "A container rebuild request is already queued.",
      status_code=409,
    ) from exc
  except OSError as exc:
    raise DeploymentControlError(
      "controller_unavailable",
      "The host replacement request could not be queued.",
    ) from exc
  finally:
    temp.unlink(missing_ok=True)


def replacement_ready_path(operation_id: str) -> Path:
  """Validate the host's current cutover request and return its ready marker."""
  if not _OPERATION_RE.fullmatch(operation_id):
    raise DeploymentControlError(
      "invalid_operation", "The replacement operation is invalid.",
      status_code=409,
    )
  status = _read_host_status()
  if (
    status.get("operation_id") != operation_id
    or status.get("state") != "preparing"
  ):
    raise DeploymentControlError(
      "operation_mismatch",
      "The host is not waiting for this replacement operation.",
      status_code=409,
    )
  return _inbox_dir() / f"ready-{operation_id}"


async def withdraw_unclaimed_host_request() -> RebuildStatus:
  """Drop a queued host request the helper has not claimed, so a retry is possible.

  The host worker atomically renames request.json out of the inbox before it
  acts (see ``mobius-rebuild-host.run``), so a request still present in the inbox
  was never claimed and removing it cannot interrupt an in-flight replacement.
  This is the owner's escape from an unclaimed request that ``_write_request``
  would otherwise keep refusing as ``already_running``. Removing an absent
  request is a no-op, so the action is idempotent.
  """
  if platform_activation.deployment_kind() == "railway":
    raise DeploymentControlError(
      "not_supported",
      "Railway container updates are managed by the account service.",
      status_code=409,
    )
  if not _configured():
    raise DeploymentControlError(
      "not_configured",
      "The host replacement helper is not configured on this deployment.",
      status_code=409,
    )
  # An unresolved journal is host-owned even if a stray inbox file exists.
  if (await asyncio.to_thread(_read_host_status)).get("state") in _UNRESOLVED_STATES:
    raise DeploymentControlError(
      "recovery_required",
      "The previous container replacement needs host recovery before this request can be withdrawn.",
      status_code=409,
    )
  withdrawn = await asyncio.to_thread(_claim_unclaimed_request)
  nonce = str(withdrawn.get("nonce") or "") if withdrawn else ""
  if _OPERATION_RE.fullmatch(nonce):
    # The helper never saw it, so its binding can never be confirmed.
    await _release_binding({"controller": "host", "id": nonce})
  return await read_rebuild_status()


def _claim_unclaimed_request() -> dict | None:
  """Take the queued request out of the inbox atomically, racing the helper's
  own claim; returns its payload when this call won."""
  inbox = _inbox_dir()
  claimed = inbox / f".withdrawn-{secrets.token_hex(12)}.json"
  try:
    os.replace(inbox / "request.json", claimed)
  except FileNotFoundError:
    return None
  try:
    value = json.loads(claimed.read_text(encoding="utf-8"))
  except (OSError, UnicodeError, json.JSONDecodeError):
    value = None
  finally:
    claimed.unlink(missing_ok=True)
  return value if isinstance(value, dict) else {}


async def read_rebuild_status() -> RebuildStatus:
  deployment = platform_activation.deployment_kind()
  if deployment == "railway":
    if not managed_cutover_ready():
      return _empty_status(
        deployment,
        supported=False,
        code="controller_upgrade_required",
        message=(
          "Install a current Möbius image before managing Railway container "
          "updates. The retired bootstrap protocol is no longer available."
        ),
      )
    raw = await asyncio.to_thread(_managed_request, "GET", "status")
    if (
      raw.get("state") == "awaiting_handoff"
      and raw.get("operation_id") != _managed_handoff
    ):
      # Retrying prepares a fresh handoff over this one.
      raw = {
        **raw, "state": "idle",
        "message": "The last update attempt stopped before replacing the container.",
      }
    return _normalize_managed_status(raw)
  if not _configured():
    return _empty_status(
      deployment,
      supported=False,
      code="not_configured",
      message=(
        "Finish the one-time host setup by running sudo "
        "scripts/install-rebuild-helper.sh from the trusted Möbius checkout."
      ),
    )
  raw = await asyncio.to_thread(_read_host_status)
  if not _current_host_controller(raw):
    return _empty_status(
      deployment,
      supported=False,
      code="controller_upgrade_required",
      message=_UPGRADE_MESSAGE,
    )
  return _normalize_status(raw)


def _bound_operation() -> dict | None:
  record = platform_update.read_prepared_update()
  return record["operation"] if record else None


async def _bind_operation(
  expected_sha: str, operation: dict, replacing: dict | None,
) -> None:
  try:
    await asyncio.to_thread(
      lambda: platform_update.bind_update_operation(
        expected_sha, operation, replacing=replacing,
      ),
    )
  except platform_update.PlatformUpdateError as exc:
    if str(exc) == "update_operation_bound":
      raise DeploymentControlError(
        "already_running",
        "Another request to finish this update is already under way.",
        status_code=409,
      ) from exc
    raise DeploymentControlError(
      "update_plan_stale",
      "Möbius changed since this preview. Refresh and review the update again.",
      status_code=409,
    ) from exc


async def _release_binding(operation: dict) -> None:
  """Release exactly ``operation`` once its replacement definitively ended
  without starting."""
  record = await asyncio.to_thread(platform_update.read_prepared_update)
  if record is not None:
    await asyncio.to_thread(
      platform_update.unbind_update_operation, record["target"], operation,
    )


def _host_request_ended(nonce: str) -> bool:
  """Whether the host helper can no longer start the request with ``nonce``.

  The helper publishes a request's nonce in its status before it takes the
  request out of the inbox (``mobius-rebuild-host.run``). So a request that
  is neither still queued nor named by the status was never claimed and can
  never run; one the status names has ended once that status is terminal.
  Read the inbox first: a request gone from it is already in the status.
  """
  try:
    pending = json.loads((_inbox_dir() / "request.json").read_text(encoding="utf-8"))
  except FileNotFoundError:
    pending = None
  except (OSError, UnicodeError, json.JSONDecodeError):
    return False
  if isinstance(pending, dict) and pending.get("nonce") == nonce:
    return False
  try:
    status = json.loads(_status_path().read_text(encoding="utf-8"))
  except (OSError, UnicodeError, json.JSONDecodeError):
    return False
  if not isinstance(status, dict):
    return False
  if status.get("request_nonce") == nonce:
    return str(status.get("state") or "idle") not in (_ACTIVE_STATES | _UNRESOLVED_STATES)
  return True


async def _binding_ended(operation: dict) -> bool:
  """Proof that a bound replacement can no longer start or succeed: the
  exact operation reported terminal, or (Railway) a newer operation reported,
  or (host) the request provably never claimed. Merely seeing nothing
  running is not proof: a host request may be claimed but not yet reported."""
  if operation["controller"] == "host":
    return await asyncio.to_thread(_host_request_ended, operation["id"])
  try:
    status = await read_rebuild_status()
  except DeploymentControlError:
    return False
  if not status.get("supported") or status.get("state") in (_ACTIVE_STATES | _UNRESOLVED_STATES):
    return False
  return True


async def release_ended_binding() -> None:
  """Before cancelling a prepared update, release a binding whose
  replacement provably ended. Anything short of that proof keeps it, and
  cancelling then refuses."""
  # Cancellation must not silently discard an unresolved host transaction
  # even if an older prepared record lacks the operation binding.
  try:
    status = await read_rebuild_status()
  except DeploymentControlError:
    status = None
  if status is not None and status.get("state") in _UNRESOLVED_STATES:
    raise DeploymentControlError(
      "recovery_required",
      "The unresolved container replacement must be recovered before cancelling this update.",
      status_code=409,
    )
  operation = await asyncio.to_thread(_bound_operation)
  if operation is not None and await _binding_ended(operation):
    await _release_binding(operation)


_settle_window_ends: float | None = None


async def keep_settling_update() -> None:
  """The owner keeps a settling update only when its replacement can no
  longer confirm it: the bounded check after boot has ended and the
  controller reports that exact operation neither running nor succeeded."""
  if _settle_window_ends is not None and time.monotonic() < _settle_window_ends:
    raise DeploymentControlError(
      "confirmation_pending",
      "Möbius is still confirming the new container. Try again later.",
      status_code=409,
    )
  record = await asyncio.to_thread(platform_update.read_prepared_update)
  try:
    status: dict | None = dict(await read_rebuild_status())
  except DeploymentControlError:
    status = None
  if (
    record is not None and status is not None
    and platform_update.status_reports_bound_operation(status, record)
  ):
    if status.get("state") in (_ACTIVE_STATES | _UNRESOLVED_STATES):
      raise DeploymentControlError(
        "recovery_required" if status.get("state") in _UNRESOLVED_STATES else "already_running",
        "The unresolved container replacement must be recovered first."
        if status.get("state") in _UNRESOLVED_STATES else
        "The container replacement is still running. Wait for it to finish.",
        status_code=409,
      )
    if status.get("state") == "succeeded":
      await asyncio.to_thread(platform_update.reconcile_bound_operation, status)
      return
  try:
    await asyncio.to_thread(platform_update.keep_settling_update)
  except platform_update.PlatformUpdateError as exc:
    raise DeploymentControlError(
      str(exc), "This update is no longer waiting for confirmation.",
      status_code=409,
    ) from exc


async def settle_image_update() -> str | None:
  """Apply the bound replacement's outcome to the prepared update, if the
  controller reports one. The status is read here; the decision and the
  record change belong to ``platform_update.reconcile_bound_operation``."""
  if not await asyncio.to_thread(_bound_operation):
    return None
  try:
    status = await read_rebuild_status()
  except DeploymentControlError:
    return None
  return await asyncio.to_thread(
    platform_update.reconcile_bound_operation, dict(status),
  )


async def settle_image_update_after_boot(
  *, interval: float = 15.0, timeout: float = 30 * 60,
) -> None:
  """After startup, keep applying the bound replacement's outcome until it
  settles or the bound elapses, so confirmation never depends on Settings
  being open. Past the bound, Settings offers the owner's explicit decision
  (``keep_settling_update``)."""
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if not await asyncio.to_thread(_bound_operation):
      return
    try:
      outcome = await settle_image_update()
    except Exception:
      log.warning("could not check the bound platform update replacement", exc_info=True)
      outcome = None
    if outcome:
      log.info("platform update replacement settled: %s", outcome)
      return
    await asyncio.sleep(interval)


def schedule_settle_image_update_after_boot(timeout: float = 30 * 60) -> None:
  """Keep the post-boot confirmation check alive until it settles."""
  global _settle_window_ends
  _settle_window_ends = time.monotonic() + timeout
  task = asyncio.create_task(settle_image_update_after_boot(timeout=timeout))
  _managed_recovery_tasks.add(task)
  task.add_done_callback(_managed_recovery_tasks.discard)


def _ensure_can_rebuild(status: RebuildStatus) -> None:
  """Reject a rebuild the current controller cannot complete before mutation."""
  if not status.get("supported"):
    raise DeploymentControlError(
      status.get("code") or "not_configured",
      status.get("message") or "Container updates are not available here yet.",
      status_code=409,
    )
  if status.get("state") in (_ACTIVE_STATES | _UNRESOLVED_STATES):
    raise DeploymentControlError(
      "recovery_required" if status.get("state") in _UNRESOLVED_STATES else "already_running",
      "The previous container replacement needs recovery before another request."
      if status.get("state") in _UNRESOLVED_STATES else "A container rebuild is already running.",
      status_code=409,
    )


async def _request_self_hosted_rebuild(
  *, expected_sha: str, final_check: Callable[[], None],
) -> RebuildStatus:
  """Queue the host controller for the exact target already reviewed and applied.

  This internal dispatch never discovers a release. Recheck controller readiness
  then revalidate the complete review immediately before the durable inbox write.
  """
  if not _SHA_RE.fullmatch(expected_sha):
    raise DeploymentControlError(
      "target_unavailable",
      "Möbius cannot identify the official version to deploy.",
      status_code=409,
    )
  _ensure_can_rebuild(await read_rebuild_status())
  # A binding left by an attempt that provably ended may be replaced.
  stale = await asyncio.to_thread(_bound_operation)
  replacing = stale if stale and await _binding_ended(stale) else None
  await asyncio.to_thread(final_check)
  nonce = secrets.token_hex(16)
  operation = {"controller": "host", "id": nonce}
  # Bind before the helper can see the request: only this replacement's
  # success may retire the prepared update.
  await _bind_operation(expected_sha, operation, replacing)
  try:
    await asyncio.to_thread(_write_request, expected_sha, nonce)
  except BaseException:
    await asyncio.to_thread(
      platform_update.unbind_update_operation, expected_sha, operation,
    )
    raise
  return _normalize_status({
    "state": "queued",
    "expected_sha": expected_sha,
    "message": "Container rebuild queued.",
    "request_nonce": nonce,
  }, expected_sha=expected_sha)


async def request_reviewed_rebuild(
  *,
  db: Any,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None,
) -> RebuildStatus | platform_update.PlatformApplyResult:
  """Keep an admitted source-and-container update one coherent operation."""
  started = asyncio.Event()
  task = asyncio.create_task(_request_reviewed_rebuild_transaction(
    db=db,
    plan_id=plan_id,
    current_sha=current_sha,
    target_sha=target_sha,
    image_digest=image_digest,
    started=started,
  ))
  try:
    return await asyncio.shield(task)
  except asyncio.CancelledError:
    if not started.is_set():
      task.cancel()
    while not task.done():
      try:
        await asyncio.shield(task)
      except asyncio.CancelledError:
        continue
      except Exception:
        break
    if started.is_set():
      try:
        task.result()
      except Exception:
        log.exception("reviewed replacement failed after its client disconnected")
    raise


async def _request_reviewed_rebuild_transaction(
  *,
  db: Any,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None,
  started: asyncio.Event,
) -> RebuildStatus | platform_update.PlatformApplyResult:
  """Drive the container rebuild bound to an owner-reviewed update target.

  The update is prepared and checked on a frozen copy of the live source (or
  was already prepared by a resolver); the live checkout keeps serving until
  the cutover's shutdown drain swaps the checked update in, so nothing edited
  meanwhile enters it. Railway then cuts over to the exact digest-pinned GHCR
  image; self-hosted queues the host helper for the pinned ``sha-<target>``.

  Returns the :class:`RebuildStatus` of the queued/started replacement, or the
  :class:`~app.platform_update.PlatformApplyResult` when preparing stopped at a
  conflict (parked for the resolver; nothing was rebuilt).
  """
  deployment = platform_activation.deployment_kind()
  if deployment == "railway" and not image_digest:
    raise DeploymentControlError(
      "update_plan_invalid",
      "This update review is no longer valid. Refresh it and try again.",
      status_code=409,
    )
  def validate_reviewed_release() -> None:
    try:
      reviewed = platform_update.reviewed_container_rebuild_plan(
        plan_id=plan_id,
        current_sha=current_sha,
        target_sha=target_sha,
        image_digest=image_digest,
      )
    except platform_update.PlatformUpdateError as exc:
      code = str(exc)
      messages = {
        "update_plan_stale": (
          "Möbius changed since this preview. Refresh and review the update again."
        ),
        "update_plan_invalid": (
          "This update review is no longer valid. Refresh it and try again."
        ),
        "update_plan_target_missing": (
          "The reviewed release source is unavailable. Refresh and try again shortly."
        ),
        "finish_update_first": (
          "Another update is not finished yet. Finish it in Settings before starting a new one."
        ),
      }
      raise DeploymentControlError(
        code,
        messages.get(
          code,
          "This update could not be verified. Refresh it and try again.",
        ),
        status_code=409,
      ) from exc
    if platform_activation.requires_agent_activation(reviewed["activation"]):
      raise DeploymentControlError(
        "external_activation_required",
        "This update also needs deployment changes. Resolve it with Möbius before replacing the container.",
        status_code=409,
      )
    incoming_activation = reviewed.get(
      "incoming_activation", reviewed["activation"],
    )
    if platform_update.activation_changes_python_dependencies(
      incoming_activation,
    ) and not platform_update.image_activates_updates():
      # Source that imports a newly declared package runs only on the image
      # that has it. This image predates the boot transaction that lets that
      # image swap the source in (and revert it if the image is not kept).
      raise DeploymentControlError(
        "external_activation_required",
        "This update changes Python packages. It needs a separately verified "
        "system replacement before its source can be installed safely.",
        status_code=409,
      )
    if platform_activation.ActivationLevel.IMAGE_REBUILD.value not in (
      reviewed["activation"]["required_actions"]
    ):
      raise DeploymentControlError(
        "activation_changed",
        "This update no longer requires a container rebuild. Refresh the review.",
        status_code=409,
      )

  prepared = platform_update.read_prepared_update()
  final_check: Callable[[], None]
  if (
    prepared and prepared["state"] == "prepared"
    and prepared["target"] == target_sha
  ):
    # A resolver already prepared and checked this exact release.
    started.set()
    final_check = lambda: None  # noqa: E731
  else:
    await asyncio.to_thread(validate_reviewed_release)
    _ensure_can_rebuild(await read_rebuild_status())
    started.set()
    # Prepare from a snapshot of the live source; the live checkout keeps
    # serving it until the cutover swaps the checked update in.
    outcome = await platform_update.prepare_platform_update(
      plan_id=plan_id, current_sha=current_sha,
      target_sha=target_sha, image_digest=image_digest,
    )
    if outcome.get("state") != "prepared":
      return outcome  # parked for its resolver; nothing was prepared
    prepared = outcome
    final_check = validate_reviewed_release
  if not prepared["requires_image"]:
    raise DeploymentControlError(
      "activation_changed",
      "This update no longer requires a container rebuild. Refresh the review.",
      status_code=409,
    )
  if platform_update.frozen_image_sha() == target_sha:
    # The target image is already running; its own boot swaps the update in.
    raise DeploymentControlError(
      "restart_to_finish",
      "This container already runs the update's image. Restart Möbius to finish it.",
      status_code=409,
    )
  if deployment != "railway":
    return await _request_self_hosted_rebuild(
      expected_sha=target_sha, final_check=final_check,
    )
  return await _request_managed_rebuild(
    target_sha, image_digest or prepared["image_digest"] or "",
    final_check=final_check,
  )


def _verify_managed_release_echo(
  raw: dict[str, Any],
  expected_sha: str,
  expected_digest: str,
) -> None:
  """Require the account service to echo the exact immutable release pair."""
  reported_sha = str(raw.get("expected_sha") or "").strip().lower()
  reported_digest = str(raw.get("image_digest") or "").strip().lower()
  if reported_sha != expected_sha or reported_digest != expected_digest:
    raise DeploymentControlError(
      "controller_invalid_response",
      "The account service did not confirm the exact reviewed image.",
    )


async def _request_managed_rebuild(
  expected_sha: str,
  expected_digest: str,
  *,
  final_check: Callable[[], None] | None = None,
) -> RebuildStatus:
  global _managed_handoff
  from app import restart_ledger, restart_util

  if not managed_cutover_ready():
    raise DeploymentControlError(
      "controller_upgrade_required",
      "Install the current Möbius image once to enable managed container rebuilds.",
      status_code=409,
    )
  # Re-check the review at the last point the live checkout is still the one
  # reviewed: the drain swaps the prepared update in, so a check after it
  # would always see the update itself as a change. Edits made from here on
  # are carried across the swap as late edits.
  if final_check is not None:
    await asyncio.to_thread(final_check)
  prepared = await asyncio.to_thread(
    _managed_request,
    "POST",
    "prepare",
    {"expected_sha": expected_sha, "expected_digest": expected_digest},
  )
  _verify_managed_release_echo(prepared, expected_sha, expected_digest)
  if prepared.get("state") == "no_change":
    # Finish already asks for a restart when this image is the target, so the
    # account service and this container disagree about what is running.
    raise DeploymentControlError(
      "controller_inconsistent",
      "The account service reports this container already runs the update's "
      "image, but this container is a different version. Try again shortly.",
      status_code=409,
    )
  operation_id = str(prepared.get("operation_id") or "")
  handoff_nonce = str(prepared.get("handoff_nonce") or "")
  boot_id = restart_ledger.current_boot_id()
  if not operation_id or not handoff_nonce or not boot_id:
    raise DeploymentControlError(
      "controller_invalid_response", "The managed replacement handoff is incomplete."
    )
  # Bind before the drain: only this replacement's success may retire the
  # prepared update, never an earlier attempt at the same release.
  # The account service runs one replacement at a time, so after this fresh
  # prepare a binding left on the record belongs to an attempt that ended.
  operation = {"controller": "railway", "id": operation_id}
  replacing = await asyncio.to_thread(_bound_operation)
  await _bind_operation(expected_sha, operation, replacing)
  _managed_handoff = operation_id
  drained = False
  provider_start_attempted = False
  reconciling = False
  try:
    restart_ledger.request_managed_cutover(
      boot_id=boot_id, cutover_id=operation_id,
    )
    deadline = time.monotonic() + 15
    while not restart_ledger.authorized_cutover_challenge(operation_id):
      if time.monotonic() >= deadline:
        raise DeploymentControlError(
          "controller_upgrade_required",
          "The current container cannot authorize a Railway replacement.",
        )
      await asyncio.sleep(0.25)
    await restart_util.prepare_managed_container_cutover(operation_id)
    drained = True
    deadline = time.monotonic() + 15
    while not restart_ledger.accepted_cutover_receipt(operation_id):
      if time.monotonic() >= deadline:
        raise DeploymentControlError(
          "controller_unavailable", "The container could not finish the Railway handoff."
        )
      await asyncio.sleep(0.25)
    provider_start_attempted = True
    started = await asyncio.to_thread(
      _managed_request,
      "POST",
      "start",
      {"operation_id": operation_id, "handoff_nonce": handoff_nonce},
    )
    _verify_managed_release_echo(started, expected_sha, expected_digest)
    return _normalize_managed_status(
      started, image_digest=expected_digest,
    )
  except Exception as exc:
    code = exc.code if isinstance(exc, DeploymentControlError) else None
    if not (drained and provider_start_attempted and code != "controller_rejected"):
      # The replacement definitively never started; only an ambiguous start
      # keeps its binding, for the account service's outcome to settle.
      await _release_binding(operation)
    if drained and (
      not provider_start_attempted or code == "controller_rejected"
    ):
      # Before the start request, the provider cannot own the transition. After
      # it, only a definitive rejection proves local recovery cannot race an
      # accepted Railway cutover.
      _schedule_managed_recovery(restart_util.restart_this_worker)
    elif drained and provider_start_attempted and code != "controller_rejected":
      reconciling = True
      _schedule_ambiguous_start_reconciliation(
        operation_id,
        handoff_nonce,
        restart_util.restart_this_worker,
      )
    if isinstance(exc, DeploymentControlError):
      raise
    raise DeploymentControlError(
      "controller_unavailable",
      "The final container replacement check could not complete.",
    ) from exc
  finally:
    if not reconciling:
      _release_managed_handoff(operation_id)
