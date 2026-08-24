"""GitHub credential connection state machine, independent of HTTP routes."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import HTTPException, Request

from app import github_auth, models
from app.config import get_settings

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_API_BASE = "https://api.github.com"
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_CONNECTION_LOCK_TIMEOUT = 70.0
_device_flow_poll_lock = asyncio.Lock()
_FULL_PR_SCOPES = ("public_repo", "workflow")

log = logging.getLogger("moebius.github")


def has_full_pr_access(scopes: object) -> bool:
  """Return whether one credential can publish every reviewed PR shape."""
  if not isinstance(scopes, (list, tuple, set, frozenset)):
    return False
  granted = {str(scope) for scope in scopes}
  return (
    "workflow" in granted
    and ("public_repo" in granted or "repo" in granted)
  )


def _bounded_provider_int(
  value: object,
  *,
  default: int,
  minimum: int,
  maximum: int,
) -> int:
  """Parse an untrusted provider duration without allowing a wedged attempt."""
  try:
    parsed = int(value)
  except (TypeError, ValueError):
    return default
  return max(minimum, min(maximum, parsed))


@asynccontextmanager
async def _github_connection_transaction():
  """Serialize every credential/attempt mutation across workers.

  The asyncio lock handles tasks in this worker. The non-blocking flock makes
  the same state machine safe if the platform later runs multiple workers,
  without blocking an event loop while another worker waits on GitHub.
  """
  async with _device_flow_poll_lock:
    deadline = asyncio.get_running_loop().time() + _CONNECTION_LOCK_TIMEOUT
    fd = github_auth.try_acquire_connection_lock()
    while fd is None:
      if asyncio.get_running_loop().time() >= deadline:
        raise HTTPException(
          status_code=503,
          detail="The GitHub connection is busy. Please try again.",
        )
      await asyncio.sleep(0.05)
      fd = github_auth.try_acquire_connection_lock()
    try:
      yield
    finally:
      github_auth.release_connection_lock(fd)


async def _github_user(token: str) -> tuple[int, str, int | None, list[str]]:
  """GET /user with `token`; returns (status, login, user_id, scopes).

  scopes come from the X-OAuth-Scopes response header — the only place
  GitHub reports a classic token's grants. login/user_id are "" / None
  on a non-200.
  """
  async with httpx.AsyncClient(timeout=15.0) as client:
    r = await client.get(
      f"{_API_BASE}/user",
      headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "mobius",
      },
    )
  scopes = [
    s.strip()
    for s in (r.headers.get("x-oauth-scopes") or "").split(",")
    if s.strip()
  ]
  if r.status_code != 200:
    return r.status_code, "", None, scopes
  data = r.json()
  if not isinstance(data, dict):
    return r.status_code, "", None, scopes
  login = data.get("login")
  return (
    r.status_code,
    login if isinstance(login, str) else "",
    data.get("id"),
    scopes,
  )


async def _start_device_attempt(
  request: Request,
) -> dict:
  """Request and persist one device code while the connection lock is held."""
  if await request.is_disconnected():
    raise HTTPException(status_code=499, detail="GitHub sign-in was cancelled.")
  client_id = get_settings().github_oauth_client_id
  if not client_id:
    raise HTTPException(
      status_code=409,
      detail=(
        "Device flow is not configured on this instance "
        "(GITHUB_OAUTH_CLIENT_ID is unset). Configure the Möbius GitHub "
        "OAuth app before connecting."
      ),
    )
  try:
    scopes = " ".join(_FULL_PR_SCOPES)
    async with httpx.AsyncClient(timeout=15.0) as client:
      r = await client.post(
        _DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": scopes},
        headers={"Accept": "application/json"},
      )
  except httpx.HTTPError:
    raise HTTPException(status_code=502, detail="Could not reach GitHub.")
  try:
    payload = r.json()
  except ValueError:
    payload = {}
  if payload.get("error") == "device_flow_disabled":
    raise HTTPException(
      status_code=409,
      detail=(
        "The configured GitHub OAuth app has the device flow disabled. "
        "Enable device flow for that OAuth app before connecting."
      ),
    )
  if r.status_code != 200 or "device_code" not in payload:
    log.error("GitHub device/code failed (%d)", r.status_code)
    raise HTTPException(
      status_code=502, detail="GitHub device flow could not be started.",
    )
  now = time.time()
  interval = _bounded_provider_int(
    payload.get("interval"),
    default=5,
    minimum=1,
    maximum=60,
  )
  expires_in = _bounded_provider_int(
    payload.get("expires_in"),
    default=900,
    minimum=60,
    maximum=1800,
  )
  attempt_id = secrets.token_urlsafe(18)
  expires_at = now + expires_in
  # A browser that timed out or unmounted while waiting behind the serialized
  # connection lock must not publish an invisible attempt over a newer tab.
  if await request.is_disconnected():
    raise HTTPException(status_code=499, detail="GitHub sign-in was cancelled.")
  github_auth.set_device_flow({
    "attempt_id": attempt_id,
    "status": "waiting",
    "device_code": payload["device_code"],
    "interval": interval,
    "next_poll_at": now + interval,
    "created_at": now,
    "expires_at": expires_at,
    "requested_scopes": scopes.split(),
    "user_code": payload["user_code"],
    "verification_uri": payload["verification_uri"],
  })
  return {
    "attempt_id": attempt_id,
    "user_code": payload["user_code"],
    "verification_uri": payload["verification_uri"],
    "expires_in": expires_in,
    "expires_at": expires_at,
    "interval": interval,
    "requested_scopes": scopes.split(),
  }


def _device_attempt_result(flow: dict, *, now: float | None = None) -> dict:
  """Returns the browser-safe state for one persisted device attempt."""
  response = {
    "attempt_id": flow["attempt_id"],
    "status": flow.get("status", "waiting"),
    "expires_at": flow.get("expires_at"),
  }
  if flow.get("reason"):
    response["reason"] = flow["reason"]
  if flow.get("message"):
    response["message"] = flow["message"]
  if flow.get("login"):
    response["login"] = flow["login"]
  if response["status"] == "waiting":
    current = time.time() if now is None else now
    response["status"] = "pending"
    response["expires_in"] = max(
      0, round(float(flow.get("expires_at", current)) - current, 3),
    )
    response["retry_after"] = max(
      0, round(float(flow.get("next_poll_at", current)) - current, 3),
    )
    if flow.get("last_error"):
      response["last_error"] = flow["last_error"]
    response["interval"] = flow.get("interval")
    response["user_code"] = flow.get("user_code")
    response["verification_uri"] = flow.get("verification_uri")
  return response


def _current_device_attempt(attempt_id: str) -> dict:
  flow = github_auth.get_device_flow()
  if not flow or flow.get("attempt_id") != attempt_id:
    raise HTTPException(
      status_code=404,
      detail="This GitHub connection attempt no longer exists.",
    )
  return flow
