"""Provider-neutral container replacement control for Settings.

The browser can request only "replace this installation's container".  This
module derives the applied upstream revision server-side, selects the external
controller that already owns the deployment, and normalizes its durable job
status.  No Docker, Railway, path, image, or command arguments cross the owner
API boundary.

The running container is deliberately not the job owner: it disappears during
cutover.  Self-hosted state lives in the root-owned host helper; managed state
lives in Möbius Launch.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import quote

import httpx

from app import platform_activation, platform_update
from app.config import get_settings


RebuildState = Literal[
  "idle", "queued", "preparing", "waiting_for_work", "replacing",
  "verifying", "succeeded", "no_change", "failed", "rolled_back",
  "needs_recovery",
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
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_KNOWN_STATES = {
  "idle", "queued", "preparing", "waiting_for_work", "replacing",
  "verifying", "succeeded", "no_change", "failed", "rolled_back",
  "needs_recovery",
}
_ACTIVE_STATES = {
  "queued", "preparing", "waiting_for_work", "replacing", "verifying",
}
_MANAGED_ACTIVE_ALIASES = {
  "running": "preparing",
  "initializing": "preparing",
  "building": "preparing",
  "deploying": "replacing",
  "waiting": "queued",
}
_MANAGED_TERMINAL_ALIASES = {
  "success": "succeeded",
  "rejected": "failed",
  "crashed": "failed",
}


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
  candidates = (
    status.get("contained_upstream_sha"),
    status.get("recorded_upstream_sha"),
    status.get("current_build_sha"),
  )
  for raw in candidates:
    value = str(raw or "").strip().lower()
    if _SHA_RE.fullmatch(value):
      return value
  return None


def _normalize_status(
  raw: dict[str, Any],
  *,
  deployment: platform_activation.DeploymentKind,
  default_supported: bool = True,
  expected_sha: str | None = None,
) -> RebuildStatus:
  raw_state = str(raw.get("state") or "idle").strip().lower()
  state = _MANAGED_ACTIVE_ALIASES.get(raw_state, raw_state)
  state = _MANAGED_TERMINAL_ALIASES.get(state, state)
  if state not in _KNOWN_STATES:
    raise DeploymentControlError(
      "controller_invalid_response",
      "The deployment controller returned an unknown rebuild state.",
    )
  operation_id = str(
    raw.get("operation_id") or raw.get("job_id") or ""
  ).strip() or None
  error = raw.get("error")
  error_map = error if isinstance(error, dict) else {}
  code = str(raw.get("code") or error_map.get("code") or "").strip() or None
  message = str(
    raw.get("message") or raw.get("detail") or error_map.get("message") or ""
  ).strip() or None
  reported_sha = str(raw.get("expected_sha") or expected_sha or "").strip()
  if reported_sha and not _SHA_RE.fullmatch(reported_sha):
    reported_sha = ""
  updated_at = str(raw.get("updated_at") or "").strip() or None
  return RebuildStatus(
    supported=bool(raw.get("supported", default_supported)),
    deployment=deployment,
    operation_id=operation_id,
    state=state,  # type: ignore[typeddict-item]
    expected_sha=reported_sha or None,
    code=code,
    message=message,
    updated_at=updated_at,
  )


def _self_host_connection_path() -> Path:
  return Path(get_settings().data_dir) / "cli-auth" / "mobius-rebuild" / "connection.json"


def _self_host_connection() -> dict[str, Any] | None:
  path = _self_host_connection_path()
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
  except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
    return None
  if not isinstance(value, dict) or value.get("version") != 1:
    return None
  host = str(value.get("host") or "").strip()
  user = str(value.get("user") or "").strip()
  identity = Path(str(value.get("identity_file") or ""))
  known_hosts = Path(str(value.get("known_hosts_file") or ""))
  try:
    port = int(value.get("port", 22))
  except (TypeError, ValueError):
    return None
  if (
    not _HOST_RE.fullmatch(host)
    or not _USER_RE.fullmatch(user)
    or not 1 <= port <= 65535
    or not identity.is_file()
    or not known_hosts.is_file()
  ):
    return None
  return {
    "host": host,
    "user": user,
    "port": port,
    "identity_file": str(identity),
    "known_hosts_file": str(known_hosts),
  }


async def _self_host_call(
  operation: Literal["rebuild", "status"], expected_sha: str | None = None,
) -> dict[str, Any]:
  connection = _self_host_connection()
  ssh = shutil.which("ssh")
  if not connection or not ssh:
    raise DeploymentControlError(
      "not_configured",
      "Container rebuilding is not set up on this installation.",
    )
  args = [
    ssh,
    "-T",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", f"UserKnownHostsFile={connection['known_hosts_file']}",
    "-o", "ConnectTimeout=5",
    "-i", connection["identity_file"],
    "-p", str(connection["port"]),
    f"{connection['user']}@{connection['host']}",
    operation,
  ]
  payload = b""
  if operation == "rebuild":
    if not expected_sha or not _SHA_RE.fullmatch(expected_sha):
      raise DeploymentControlError(
        "target_unavailable",
        "Möbius cannot identify the upstream version to rebuild.",
        status_code=409,
      )
    payload = json.dumps({
      "version": 1,
      "request_id": secrets.token_urlsafe(24),
      "expected_sha": expected_sha,
    }, separators=(",", ":")).encode("utf-8")
  try:
    process = await asyncio.create_subprocess_exec(
      *args,
      stdin=asyncio.subprocess.PIPE,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
      process.communicate(payload), timeout=15,
    )
  except TimeoutError as exc:
    raise DeploymentControlError(
      "controller_timeout",
      "The host accepted no timely rebuild response. Check status before retrying.",
      status_code=504,
    ) from exc
  except OSError as exc:
    raise DeploymentControlError(
      "controller_unavailable",
      "The host rebuild helper is unavailable.",
    ) from exc
  if process.returncode != 0:
    detail = stderr.decode("utf-8", errors="replace").strip()[:300]
    raise DeploymentControlError(
      "controller_rejected",
      detail or "The host rejected the rebuild request.",
      status_code=409,
    )
  try:
    value = json.loads(stdout.decode("utf-8"))
  except (UnicodeError, json.JSONDecodeError) as exc:
    raise DeploymentControlError(
      "controller_invalid_response",
      "The host rebuild helper returned an unreadable response.",
    ) from exc
  if not isinstance(value, dict):
    raise DeploymentControlError(
      "controller_invalid_response",
      "The host rebuild helper returned an unreadable response.",
    )
  return value


def _managed_urls() -> tuple[str, str, str] | None:
  settings = get_settings()
  if not settings.mobius_sso_enabled:
    return None
  base = settings.mobius_sso_issuer.rstrip("/")
  instance = quote(settings.mobius_sso_instance_id, safe="")
  collection = f"{base}/api/managed/instances/{instance}/container-rebuilds"
  return collection, f"{collection}/current", settings.mobius_sso_client_secret


async def _managed_call(
  operation: Literal["rebuild", "status"], expected_sha: str | None = None,
) -> dict[str, Any]:
  urls = _managed_urls()
  if not urls:
    raise DeploymentControlError(
      "not_configured",
      "Managed container rebuilding is not configured for this instance.",
    )
  collection, current, secret = urls
  headers = {
    "Authorization": f"Bearer {secret}",
    "Accept": "application/json",
  }
  if operation == "rebuild":
    headers["Idempotency-Key"] = secrets.token_urlsafe(24)
  try:
    async with httpx.AsyncClient(
      timeout=httpx.Timeout(12.0, connect=5.0), follow_redirects=False,
    ) as client:
      response = (
        await client.post(
          collection,
          json={"expected_sha": expected_sha},
          headers=headers,
        )
        if operation == "rebuild"
        else await client.get(current, headers=headers)
      )
  except httpx.TimeoutException as exc:
    raise DeploymentControlError(
      "controller_timeout",
      "Launch did not return a timely response. Check status before retrying.",
      status_code=504,
    ) from exc
  except httpx.HTTPError as exc:
    raise DeploymentControlError(
      "controller_unavailable",
      "Möbius Launch is temporarily unavailable.",
    ) from exc
  try:
    value = response.json()
  except ValueError:
    value = {}
  if response.status_code == 404 and operation == "status":
    return {"supported": False, "state": "idle", "code": "not_configured",
            "message": "Managed container rebuilding is not available yet."}
  if response.status_code >= 400:
    error = value if isinstance(value, dict) else {}
    code = str(error.get("code") or "controller_rejected")
    message = str(
      error.get("message") or error.get("detail")
      or "Launch rejected the rebuild request."
    )
    raise DeploymentControlError(
      code, message, status_code=response.status_code,
    )
  if not isinstance(value, dict):
    raise DeploymentControlError(
      "controller_invalid_response",
      "Möbius Launch returned an unreadable response.",
    )
  return value


async def read_rebuild_status() -> RebuildStatus:
  deployment = platform_activation.deployment_kind()
  if deployment == "railway":
    if not _managed_urls():
      return _empty_status(
        deployment, supported=False, code="not_configured",
        message="Managed container rebuilding is not available yet.",
      )
    raw = await _managed_call("status")
  else:
    if not _self_host_connection() or not shutil.which("ssh"):
      return _empty_status(
        deployment, supported=False, code="not_configured",
        message="Container rebuilding is not set up on this installation.",
      )
    raw = await _self_host_call("status")
  return _normalize_status(raw, deployment=deployment)


async def request_rebuild() -> RebuildStatus:
  deployment = platform_activation.deployment_kind()
  expected_sha = _expected_upstream_sha()
  if not expected_sha:
    raise DeploymentControlError(
      "target_unavailable",
      "Möbius cannot identify the upstream version to rebuild.",
      status_code=409,
    )
  raw = (
    await _managed_call("rebuild", expected_sha)
    if deployment == "railway"
    else await _self_host_call("rebuild", expected_sha)
  )
  status = _normalize_status(
    raw, deployment=deployment, expected_sha=expected_sha,
  )
  if status["state"] not in _ACTIVE_STATES | {
    "succeeded", "no_change", "failed", "rolled_back", "needs_recovery",
  }:
    raise DeploymentControlError(
      "controller_invalid_response",
      "The deployment controller did not acknowledge the rebuild.",
    )
  return status
