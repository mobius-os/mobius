"""Container rebuild control for Settings.

The browser can request only one fixed operation: replace this installation's
app container with the official image for its applied upstream revision.
Self-hosting crosses the /data mount to a narrow host helper; managed Railway
uses its account service, while the root-owned restart ledger preserves chat
continuation across either cutover.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
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
  bootstrap_available: bool
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
_RUNTIME_OVERLAY_VERSION = "active-runtime-v1"
_managed_recovery_tasks: set[asyncio.Task[None]] = set()
_UPGRADE_MESSAGE = (
  "The Host replacement helper predates safe active-runtime carry-forward. "
  "Re-run "
  "scripts/install-rebuild-helper.sh from the current trusted checkout."
)


def _empty_status(
  deployment: platform_activation.DeploymentKind,
  *,
  supported: bool,
  bootstrap_available: bool = False,
  message: str | None = None,
  code: str | None = None,
) -> RebuildStatus:
  return RebuildStatus(
    supported=supported,
    bootstrap_available=bootstrap_available,
    deployment=deployment,
    operation_id=None,
    state="idle",
    expected_sha=None,
    code=code,
    message=message,
    updated_at=None,
  )


def _schedule_managed_recovery(
  restart: Callable[[], Awaitable[None]],
) -> None:
  """Keep the sole post-rejection restart alive until it settles."""
  task = asyncio.create_task(restart())
  _managed_recovery_tasks.add(task)
  task.add_done_callback(_managed_recovery_tasks.discard)


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
    bootstrap_available=False,
    deployment="self_hosted",
    operation_id=operation_id,
    state=state,  # type: ignore[typeddict-item]
    expected_sha=reported_sha or None,
    code=code,
    message=message,
    updated_at=updated_at,
  )


def _managed_headers() -> dict[str, str]:
  settings = get_settings()
  if not settings.mobius_sso_enabled:
    raise DeploymentControlError(
      "not_configured",
      "This Railway deployment is not linked to its Möbius account service.",
      status_code=409,
    )
  return {
    "Authorization": f"Bearer {settings.mobius_sso_client_secret}",
    "X-Mobius-Instance-Id": settings.mobius_sso_instance_id,
    "Accept": "application/json",
  }


def managed_cutover_ready() -> bool:
  """Whether this exact boot owns the baked managed-cutover supervisor."""
  marker = Path(get_settings().data_dir) / "run" / "managed-cutover-ready"
  try:
    marker_boot_id = marker.read_text(encoding="utf-8").strip()
  except (FileNotFoundError, OSError, UnicodeError):
    return False
  boot_id = str(os.environ.get("MOBIUS_BOOT_ID") or "").strip()
  return bool(boot_id and secrets.compare_digest(marker_boot_id, boot_id))


class _NoManagedRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
    return None


def _managed_request(method: str, suffix: str, payload: dict[str, str] | None = None) -> dict[str, Any]:
  settings = get_settings()
  body = None
  headers = _managed_headers()
  if payload is not None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers["Content-Type"] = "application/json"
  request_object = urllib.request.Request(
    settings.mobius_account_origin + "/api/instance/v1/container-replacement/" + suffix,
    data=body,
    headers=headers,
    method=method,
  )
  try:
    with urllib.request.build_opener(_NoManagedRedirect()).open(
      request_object, timeout=15,
    ) as response:
      raw = response.read(64 * 1024 + 1)
      if len(raw) > 64 * 1024:
        raise DeploymentControlError(
          "controller_invalid_response", "The account service returned too much data."
        )
      value = json.loads(raw.decode("utf-8"))
  except urllib.error.HTTPError as exc:
    try:
      error = json.loads(exc.read(16 * 1024).decode("utf-8"))
      detail = (
        str(error.get("detail") or error.get("message") or "")
        if isinstance(error, dict) else ""
      )
    except (ValueError, UnicodeError):
      detail = ""
    if exc.code not in {400, 409} or not detail:
      raise DeploymentControlError(
        "controller_unavailable", "The Möbius account service is unavailable."
      ) from exc
    raise DeploymentControlError(
      "controller_rejected",
      detail[:360],
      status_code=409,
    ) from exc
  except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError) as exc:
    raise DeploymentControlError(
      "controller_unavailable", "The Möbius account service is unavailable."
    ) from exc
  if not isinstance(value, dict):
    raise DeploymentControlError(
      "controller_invalid_response", "The account service returned invalid status."
    )
  return value


def _normalize_managed_status(
  raw: dict[str, Any], *, bootstrap_available: bool = False,
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
  return RebuildStatus(
    supported=not bootstrap_available,
    bootstrap_available=bootstrap_available,
    deployment="railway",
    operation_id=str(raw.get("operation_id") or "") or None,
    state=states[remote_state],
    expected_sha=expected if _SHA_RE.fullmatch(expected) else None,
    code=None,
    message=str(raw.get("message") or "")[:360] or None,
    updated_at=str(raw.get("updated_at") or "") or None,
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
        "message": "Container rebuild queued.",
        "handoff": value.get("handoff"),
        "runtime_overlay": value.get("runtime_overlay"),
      }
  return value


def _current_handoff(raw: dict[str, Any]) -> bool:
  return (
    raw.get("handoff") == _HANDOFF_VERSION
    and raw.get("runtime_overlay") == _RUNTIME_OVERLAY_VERSION
  )


def _write_request(expected_sha: str) -> None:
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


async def read_rebuild_status() -> RebuildStatus:
  deployment = platform_activation.deployment_kind()
  if deployment == "railway":
    if not managed_cutover_ready():
      # A current served checkout can outlive its baked image. Surface an
      # account-owned bootstrap operation while it is moving, but never claim
      # the normal root handoff exists until the new image proves it with the
      # baked marker.
      try:
        raw = await asyncio.to_thread(_managed_request, "GET", "status")
      except DeploymentControlError as exc:
        if exc.code == "not_configured":
          return _empty_status(
            deployment,
            supported=False,
            code="not_configured",
            message=(
              "Link this Railway deployment to its Möbius account before "
              "enabling container updates."
            ),
          )
        return _empty_status(
          deployment,
          supported=False,
          code=exc.code,
          message=exc.message,
        )
      if (
        raw.get("mode") == "bootstrap"
        and raw.get("state") not in {None, "", "idle", "awaiting_bootstrap"}
      ):
        return _normalize_managed_status(raw, bootstrap_available=True)
      return _empty_status(
        deployment,
        supported=False,
        bootstrap_available=True,
        code="controller_upgrade_required",
        message=(
          "This Railway installation needs one managed upgrade before it can "
          "rebuild containers safely."
        ),
      )
    raw = await asyncio.to_thread(_managed_request, "GET", "status")
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
  if deployment != "railway":
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
    current = _normalize_status(host_status)
    if current["state"] in _ACTIVE_STATES:
      raise DeploymentControlError(
        "already_running",
        "A container rebuild is already running.",
        status_code=409,
      )
  expected_sha = _expected_upstream_sha()
  if not expected_sha:
    raise DeploymentControlError(
      "target_unavailable",
      "Möbius cannot identify the official version to deploy.",
      status_code=409,
    )
  blockers = platform_update.container_replacement_blockers(
    expected_sha,
    preserve_active_runtime=deployment == "self_hosted",
  )
  if blockers:
    visible = ", ".join(blockers[:5])
    if len(blockers) > 5:
      visible += f", and {len(blockers) - 5} more"
    raise DeploymentControlError(
      "local_runtime_changes",
      "The official image cannot preserve these local image inputs: "
      f"{visible}. Commit them upstream, rebuild locally, or remove them "
      "before rebuilding this container.",
      status_code=409,
    )
  if deployment == "railway":
    if not managed_cutover_ready():
      return await _request_managed_bootstrap(expected_sha)
    return await _request_managed_rebuild(expected_sha)
  await asyncio.to_thread(_write_request, expected_sha)
  return _normalize_status({
    "state": "queued",
    "expected_sha": expected_sha,
    "message": "Container rebuild queued.",
  }, expected_sha=expected_sha)


async def _request_managed_rebuild(expected_sha: str) -> RebuildStatus:
  from app import restart_ledger, restart_util

  if not managed_cutover_ready():
    raise DeploymentControlError(
      "controller_upgrade_required",
      "Install the current Möbius image once to enable managed container rebuilds.",
      status_code=409,
    )
  prepared = await asyncio.to_thread(
    _managed_request, "POST", "prepare", {"expected_sha": expected_sha},
  )
  if prepared.get("state") == "no_change":
    return _normalize_managed_status(prepared)
  operation_id = str(prepared.get("operation_id") or "")
  handoff_nonce = str(prepared.get("handoff_nonce") or "")
  boot_id = restart_ledger.current_boot_id()
  if not operation_id or not handoff_nonce or not boot_id:
    raise DeploymentControlError(
      "controller_invalid_response", "The managed replacement handoff is incomplete."
    )
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

  drained = False
  provider_start_attempted = False
  try:
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
    return _normalize_managed_status(started)
  except DeploymentControlError as exc:
    if drained and (
      not provider_start_attempted or exc.code == "controller_rejected"
    ):
      # Before the start request, the provider cannot own the transition. After
      # it, only a definitive rejection proves local recovery cannot race an
      # accepted Railway cutover.
      _schedule_managed_recovery(restart_util.restart_this_worker)
    raise


async def _request_managed_bootstrap(expected_sha: str) -> RebuildStatus:
  """Move one legacy Railway image onto the root-owned handoff protocol.

  The account service validates and owns the Railway mutation. The old image
  cannot authenticate automatic continuation, so this migration starts only
  while no agent runtime is alive. Admission closes immediately before the
  one-use start nonce is consumed; sends arriving during deployment remain in
  the durable queue for the new worker.
  """
  from app import chat
  from app.runner_registry import registry

  prepared = await asyncio.to_thread(
    _managed_request, "POST", "bootstrap/prepare", {"expected_sha": expected_sha},
  )
  operation_id = str(prepared.get("operation_id") or "")
  handoff_nonce = str(prepared.get("handoff_nonce") or "")
  if not operation_id or not handoff_nonce:
    raise DeploymentControlError(
      "controller_invalid_response", "The managed upgrade handoff is incomplete."
    )
  if registry.all_alive_chat_ids():
    raise DeploymentControlError(
      "active_chats",
      "Finish active chat responses, then enable container updates again.",
      status_code=409,
    )

  chat.begin_drain()
  try:
    started = await asyncio.to_thread(
      _managed_request,
      "POST",
      "bootstrap/start",
      {"operation_id": operation_id, "handoff_nonce": handoff_nonce},
    )
  except DeploymentControlError as exc:
    if exc.code == "controller_rejected":
      chat.cancel_idle_drain()
    raise
  return _normalize_managed_status(started, bootstrap_available=True)
