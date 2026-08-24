"""Self-hosted container replacement control for Settings.

The browser can request only one fixed operation: replace this installation's
app container with the official image for its applied upstream revision.  The
request and durable status cross the existing /data mount; a root-owned
systemd.path job owns Docker, Compose topology, verification, and rollback.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Literal, TypedDict

from app import platform_activation, platform_update
from app.config import get_settings


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
  updated_at: str | None


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
_OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_KNOWN_STATES = {
  "idle", "queued", "preparing", "replacing", "verifying", "succeeded",
  "no_change", "failed", "rolled_back", "needs_recovery",
}
_ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}
_HANDOFF_VERSION = "external-cutover-v1"
_UPGRADE_MESSAGE = (
  "The Host replacement helper predates safe chat continuation. Re-run "
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
    updated_at=None,
  )


def _expected_upstream_sha() -> str | None:
  """Return the upstream revision represented by the served platform tree."""
  try:
    status = platform_update.platform_status()
  except Exception:
    return None
  for raw in (
    status.get("contained_upstream_sha"),
    status.get("recorded_upstream_sha"),
    status.get("current_build_sha"),
  ):
    value = str(raw or "").strip().lower()
    if _SHA_RE.fullmatch(value):
      return value
  return None


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
  return RebuildStatus(
    supported=True,
    deployment="self_hosted",
    operation_id=operation_id,
    state=state,  # type: ignore[typeddict-item]
    expected_sha=reported_sha or None,
    code=code,
    message=message,
    updated_at=updated_at,
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
  if str(value.get("state") or "idle") not in _ACTIVE_STATES and request.is_file():
    try:
      pending = json.loads(request.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
      pending = {}
    expected = str(pending.get("expected_sha") or "") if isinstance(pending, dict) else ""
    if _SHA_RE.fullmatch(expected):
      return {
        "state": "queued",
        "expected_sha": expected,
        "message": "Container replacement queued.",
        "handoff": value.get("handoff"),
      }
  return value


def _current_handoff(raw: dict[str, Any]) -> bool:
  return raw.get("handoff") == _HANDOFF_VERSION


def _write_request(expected_sha: str) -> None:
  inbox = _inbox_dir()
  request = inbox / "request.json"
  if request.exists():
    raise DeploymentControlError(
      "already_running",
      "A container replacement request is already queued.",
      status_code=409,
    )
  temp = inbox / f".request-{secrets.token_hex(12)}.tmp"
  payload = json.dumps(
    {"version": 1, "expected_sha": expected_sha}, separators=(",", ":"),
  )
  try:
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(payload)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temp, request)
  except FileExistsError as exc:
    raise DeploymentControlError(
      "already_running",
      "A container replacement request is already queued.",
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


async def read_rebuild_status() -> RebuildStatus:
  deployment = platform_activation.deployment_kind()
  if deployment == "railway":
    return _empty_status(
      deployment,
      supported=False,
      code="not_supported",
      message="Container replacement is not available on Railway yet.",
    )
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
  if not _current_handoff(raw):
    return _empty_status(
      deployment,
      supported=False,
      code="controller_upgrade_required",
      message=_UPGRADE_MESSAGE,
    )
  return _normalize_status(raw)


async def request_rebuild() -> RebuildStatus:
  deployment = platform_activation.deployment_kind()
  if deployment == "railway":
    raise DeploymentControlError(
      "not_supported",
      "Container replacement is not available on Railway yet.",
      status_code=409,
    )
  if not _configured():
    raise DeploymentControlError(
      "not_configured",
      "Finish the one-time host setup by running sudo "
      "scripts/install-rebuild-helper.sh from the trusted Möbius checkout.",
      status_code=409,
    )
  host_status = await asyncio.to_thread(_read_host_status)
  if not _current_handoff(host_status):
    raise DeploymentControlError(
      "controller_upgrade_required",
      _UPGRADE_MESSAGE,
      status_code=409,
    )
  expected_sha = _expected_upstream_sha()
  if not expected_sha:
    raise DeploymentControlError(
      "target_unavailable",
      "Möbius cannot identify the official version to deploy.",
      status_code=409,
    )
  blockers = platform_update.container_replacement_blockers()
  if blockers:
    raise DeploymentControlError(
      "local_runtime_changes",
      "The official image does not contain local runtime changes. "
      "Commit them upstream or remove them before replacing this container.",
      status_code=409,
    )
  current = _normalize_status(host_status)
  if current["state"] in _ACTIVE_STATES:
    raise DeploymentControlError(
      "already_running",
      "A container replacement is already running.",
      status_code=409,
    )
  await asyncio.to_thread(_write_request, expected_sha)
  return _normalize_status({
    "state": "queued",
    "expected_sha": expected_sha,
    "message": "Container replacement queued.",
  }, expected_sha=expected_sha)
