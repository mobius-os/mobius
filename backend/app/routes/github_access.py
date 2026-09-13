"""GitHub credential management and bounded read-only access routes.

This module owns the GitHub device flow, local source inspection, and the
remote read proxy. It deliberately contains no contribution publication path:
REST registers GET only, GraphQL rejects write operations, and credentials
never appear in responses or logs.
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import fs_locks, github_auth, models, source_status
from app.config import get_settings
from app.contribution_records import now_iso as _now_iso
from app.database import get_db
from app.deps import (
  get_owner_or_app_with_github_access,
  get_owner_or_app_with_github_connect,
  reject_cross_site,
)
from app.github_connection import (
  _ACCESS_TOKEN_URL,
  _API_BASE,
  _FULL_PR_SCOPES,
  _GITHUB_LOGIN,
  _PRIVATE_PR_SCOPES,
  _bounded_provider_int,
  _current_device_attempt,
  _device_attempt_result,
  _github_connection_transaction,
  _github_user,
  _start_device_attempt,
  has_full_pr_access,
  has_private_repo_access,
)

router = APIRouter(prefix="/api/github", tags=["github"])
_limiter = Limiter(key_func=get_remote_address)
log = logging.getLogger("moebius.github.access")

# Response cap + timeout mirror routes/proxy.py: GitHub payloads the
# dashboard needs are small; anything bigger is truncated, not buffered.
_MAX_BYTES = 2 * 1024 * 1024

# Matching block strings, strings, and comments in one pass keeps ambiguous
# GraphQL text visible to the write-operation scanner and therefore rejected.
_GQL_NOISE = re.compile(
  r'"""(?:[^"]|"(?!""))*"""'
  r'|"(?:\\.|[^"\\\n])*"'
  r"|#[^\n]*"
)
_GQL_WRITE_OP = re.compile(r"\b(?:mutation|subscription)\b", re.IGNORECASE)


class GithubConnectStartRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")

  private_repos: bool = False


class GithubConnectAttemptRequest(BaseModel):
  attempt_id: str


class GraphqlRequest(BaseModel):
  query: str
  variables: dict | None = None


@router.post("/connect/start", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("3/minute")
async def connect_start(
  request: Request,
  body: GithubConnectStartRequest | None = None,
  _: models.Owner = Depends(get_owner_or_app_with_github_connect),
):
  """Starts exactly one GitHub device flow and returns its user code.

  The default connection requests the least-privilege public-contribution
  scopes. When the owner opts into private-repository access, the flow requests
  the broader `repo` scope so pushes to their private repos succeed; the
  credential store and `/status` then reflect whatever GitHub actually granted.
  """
  scope_set = (
    _PRIVATE_PR_SCOPES if (body and body.private_repos) else _FULL_PR_SCOPES
  )
  # All credential/attempt mutations share this lock. In particular, a start
  # cannot publish a ghost attempt after its client timed out behind an older
  # poll or Disconnect.
  async with _github_connection_transaction():
    return await _start_device_attempt(request, scope_set=scope_set)


@router.post("/connect/poll", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("30/minute")
async def connect_poll(
  request: Request,
  body: GithubConnectAttemptRequest,
  _: models.Owner = Depends(get_owner_or_app_with_github_connect),
):
  """Advances one identified device attempt at most once.

  Polls arriving before GitHub's requested interval are answered pending
  without an upstream call. Terminal states remain addressable so the UI can
  explain the actual outcome rather than translating every failure to expiry.
  """
  async with _github_connection_transaction():
    flow = _current_device_attempt(body.attempt_id)
    if flow.get("status") != "waiting":
      return _device_attempt_result(flow)

    now = time.time()
    if now >= float(flow["expires_at"]) and not flow.get("pending_token"):
      flow.update(status="expired", reason="expired_token")
      flow.pop("device_code", None)
      github_auth.set_device_flow(flow)
      return _device_attempt_result(flow, now=now)
    if now < float(flow["next_poll_at"]):
      return _device_attempt_result(flow, now=now)

    # Claim the interval before waiting on GitHub. A concurrent worker that
    # reloads the persisted attempt will observe the future next_poll_at and
    # return pending instead of issuing a second provider request.
    flow["next_poll_at"] = now + int(flow["interval"])
    flow.pop("last_error", None)
    github_auth.set_device_flow(flow)
    token = flow.get("pending_token")
    if not token:
      try:
        async with httpx.AsyncClient(timeout=15.0) as client:
          r = await client.post(
            _ACCESS_TOKEN_URL,
            data={
              "client_id": get_settings().github_oauth_client_id,
              "device_code": flow["device_code"],
              "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
          )
      except httpx.HTTPError:
        flow["last_error"] = "github_unreachable"
        github_auth.set_device_flow(flow)
        raise HTTPException(status_code=502, detail="Could not reach GitHub.")

      try:
        payload = r.json()
      except ValueError:
        payload = {}
      error = payload.get("error")
      if error == "authorization_pending":
        github_auth.set_device_flow(flow)
        return _device_attempt_result(flow, now=now)
      if error == "slow_down":
        # GitHub sends the new minimum interval; honor it, never shrink,
        # and always back off at least 5s beyond the previous pace.
        flow["interval"] = max(
          _bounded_provider_int(
            payload.get("interval"),
            default=0,
            minimum=0,
            maximum=60,
          ),
          _bounded_provider_int(
            flow.get("interval"),
            default=5,
            minimum=1,
            maximum=60,
          ) + 5,
        )
        flow["interval"] = min(60, flow["interval"])
        flow["next_poll_at"] = now + flow["interval"]
        github_auth.set_device_flow(flow)
        return _device_attempt_result(flow, now=now)
      if error:
        flow.update(status="failed", reason=error)
        flow.pop("device_code", None)
        github_auth.set_device_flow(flow)
        return _device_attempt_result(flow, now=now)

      token = payload.get("access_token")
      if not token:
        flow.update(status="failed", reason="no_access_token")
        flow.pop("device_code", None)
        github_auth.set_device_flow(flow)
        return _device_attempt_result(flow, now=now)
      # GitHub device codes are single-use. Persist the exchanged token before
      # user lookup so a network failure or worker restart resumes validation
      # instead of retrying a consumed code.
      flow["pending_token"] = token
      flow.pop("device_code", None)
      github_auth.set_device_flow(flow)
    try:
      status, login, user_id, scopes = await _github_user(token)
    except (httpx.HTTPError, ValueError):
      flow["last_error"] = "github_unreachable"
      github_auth.set_device_flow(flow)
      raise HTTPException(status_code=502, detail="Could not reach GitHub.")
    if status == 429 or status >= 500:
      # The device code has already been consumed, so dropping this token on a
      # transient /user response would make the attempt unrecoverable. Keep the
      # private pending token and retry only the user lookup on the next poll.
      flow["last_error"] = "github_unreachable"
      github_auth.set_device_flow(flow)
      raise HTTPException(status_code=502, detail="Could not reach GitHub.")
    if status != 200 or not _GITHUB_LOGIN.fullmatch(login):
      flow.update(status="failed", reason="user_lookup_failed")
      flow.pop("pending_token", None)
      github_auth.set_device_flow(flow)
      return _device_attempt_result(flow, now=now)
    if not has_full_pr_access(scopes):
      flow.update(
        status="failed",
        reason="insufficient_scopes",
        message=(
          "GitHub did not grant the full PR access Contribute needs. "
          "Try connecting again and approve the complete permission request."
        ),
      )
      flow.pop("pending_token", None)
      github_auth.set_device_flow(flow)
      return _device_attempt_result(flow, now=now)
    github_auth.write_credentials(
      token=token, login=login, user_id=user_id, scopes=scopes,
      source="device",
    )
    flow.update(status="complete", login=login)
    flow.pop("pending_token", None)
    github_auth.set_device_flow(flow)
    return _device_attempt_result(flow, now=now)


@router.post("/connect/cancel", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("10/minute")
async def connect_cancel(
  request: Request,
  body: GithubConnectAttemptRequest,
  _: models.Owner = Depends(get_owner_or_app_with_github_connect),
):
  """Cancels exactly one attempt without affecting a newer browser tab."""
  async with _github_connection_transaction():
    flow = _current_device_attempt(body.attempt_id)
    if flow.get("status") == "waiting":
      flow.update(status="cancelled", reason="cancelled")
      flow.pop("device_code", None)
      flow.pop("pending_token", None)
      github_auth.set_device_flow(flow)
    return _device_attempt_result(flow)


@router.get("/status")
async def github_status(
  _: models.Owner = Depends(get_owner_or_app_with_github_connect),
):
  """Connection metadata for the Contribute app's UI. Never the token
  (INV1).

  Gated on github_connect: status discloses the owner's GitHub login, scope
  list, and any resumable device attempt. Read-only GitHub consumers do not
  inherit those credential-management details.

  ``autopilot_available`` advertises the background review-response loop so an
  app paired with an older backend hides that UI.
  """
  state = github_auth.read_state() or {}
  connected = bool(state.get("token"))
  flow = github_auth.get_device_flow()
  active_attempt = None
  if (
    not connected
    and flow
    and flow.get("status") == "waiting"
    and (
      flow.get("pending_token")
      or time.time() < float(flow.get("expires_at", 0))
    )
  ):
    active_attempt = _device_attempt_result(dict(flow))
  return {
    "connected": connected,
    "login": state.get("login") if connected else None,
    "scopes": (state.get("scopes") or []) if connected else [],
    "can_push_private": (
      has_private_repo_access(state.get("scopes")) if connected else False
    ),
    "token_source": state.get("token_source") if connected else None,
    "device_flow_available": bool(get_settings().github_oauth_client_id),
    "gh_version": github_auth.gh_version(),
    "active_attempt": active_attempt,
    "autopilot_available": True,
  }


@router.get("/source-status")
async def github_source_status(
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
  db: Session = Depends(get_db),
):
  """Fetch-free local source map for the Contribute app.

  Returns refs, diff magnitudes, and working-tree metadata for the platform and
  every live app source repository.  It deliberately does not fetch remotes,
  expose source contents/absolute paths, or grant Contribute the much broader
  filesystem capability. App reads take the same per-source lock as explicit
  apply and Store install, so a commit/update cannot split one status snapshot.
  """
  rows = (
    db.query(models.App)
    .filter(
      models.App.deleted_at.is_(None),
      models.App.source_dir.isnot(None),
    )
    .order_by(models.App.name.asc())
    .all()
  )
  apps = [{
    "id": row.id,
    "name": row.name,
    "slug": row.slug,
    "version": row.version,
    "manifest_url": row.manifest_url,
    "published_manifest_url": row.published_manifest_url,
    "source_dir": row.source_dir,
  } for row in rows]

  # Repository inspection may wait on the same source lock held by an app
  # compile/update. Release the request's database connection before that wait
  # so overlapping map refreshes cannot exhaust the pool and deadlock the
  # compiler that will release the source lock.
  # FastAPI's dependency finalizer will close it again; SQLAlchemy close is
  # safe and idempotent.
  db.close()

  platform = await asyncio.to_thread(source_status.build_platform_status)
  semaphore = asyncio.Semaphore(4)

  async def inspect(app: dict) -> dict | None:
    async with semaphore:
      async with fs_locks.source_dir_lock(app["source_dir"]):
        try:
          return await asyncio.to_thread(source_status.build_app_status, app)
        except Exception:
          # One damaged checkout must not blank the complete repository map.
          # The omitted app can recover on the next refresh after its source is
          # repaired, while every healthy source remains useful now.
          log.warning(
            "Could not inspect source status for app %s",
            app.get("id"),
            exc_info=True,
          )
          return None

  inspected = await asyncio.gather(*(inspect(app) for app in apps))
  projects = [item for item in inspected if item is not None]
  projects.sort(key=lambda item: item["name"].casefold())
  return {
    "schema": 1,
    "generated_at": _now_iso(),
    "fetch_free": True,
    "platform": platform,
    "apps": projects,
  }


@router.get("/source-diff")
async def github_source_diff(
  project: str,
  head: str,
  comparison: str | None = None,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
  db: Session = Depends(get_db),
):
  """Return a bounded unified diff for one source-map project snapshot."""
  if not re.fullmatch(r"[0-9a-f]{40}", head):
    raise HTTPException(status_code=422, detail="Invalid source revision.")
  if comparison is not None and not re.fullmatch(r"[0-9a-f]{40}", comparison):
    raise HTTPException(status_code=422, detail="Invalid comparison revision.")

  async def build_diff(repo: Path, inspected: dict) -> dict:
    """Map the narrow source-preview outcomes consistently for every owner."""
    try:
      return await asyncio.to_thread(
        source_status.build_project_diff,
        repo,
        inspected,
        expected_head=head,
        expected_comparison=comparison,
      )
    except RuntimeError as exc:
      if str(exc) != "source_snapshot_changed":
        raise
      raise HTTPException(
        status_code=409,
        detail={
          "code": "source_snapshot_changed",
          "message": "The project changed; refresh before opening its diff.",
        },
      ) from exc
    except ValueError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc

  if project == "platform":
    db.close()
    repo = Path(get_settings().data_dir).resolve() / "platform"
    async with fs_locks.source_dir_lock(repo):
      inspected = await asyncio.to_thread(source_status.build_platform_status)
      return await build_diff(repo, inspected)

  match = re.fullmatch(r"app:([1-9][0-9]*)", project)
  if match is None:
    raise HTTPException(status_code=404, detail="Project not found.")
  row = (
    db.query(models.App)
    .filter(
      models.App.id == int(match.group(1)),
      models.App.deleted_at.is_(None),
      models.App.source_dir.isnot(None),
    )
    .first()
  )
  if row is None:
    raise HTTPException(status_code=404, detail="Project not found.")
  app = {
    "id": row.id,
    "name": row.name,
    "slug": row.slug,
    "version": row.version,
    "manifest_url": row.manifest_url,
    "published_manifest_url": row.published_manifest_url,
    "source_dir": row.source_dir,
  }
  db.close()
  async with fs_locks.source_dir_lock(app["source_dir"]):
    inspected = await asyncio.to_thread(source_status.build_app_status, app)
    if inspected is None:
      raise HTTPException(status_code=404, detail="Project not found.")
    return await build_diff(Path(app["source_dir"]), inspected)


@router.delete("/connect", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("5/minute")
async def github_disconnect(
  request: Request,
  _: models.Owner = Depends(get_owner_or_app_with_github_connect),
):
  """Disconnects GitHub and invalidates every pending connection attempt."""
  async with _github_connection_transaction():
    github_auth.set_device_flow(None)
    github_auth.clear_credentials()
  return {"ok": True}


async def _forward_capped(
  client: httpx.AsyncClient, req: httpx.Request
) -> Response:
  """Sends `req` streaming and reads at most _MAX_BYTES (the
  routes/proxy.py idiom — the cap bounds memory BEFORE the body is
  buffered). Surfaces X-RateLimit-Remaining so callers can self-pace.
  Failure details stay generic: the request carries the GitHub token
  in its Authorization header and must never be echoed (INV1)."""
  try:
    r = await client.send(req, stream=True)
  except httpx.HTTPError:
    raise HTTPException(status_code=502, detail="GitHub request failed.")
  try:
    buf = bytearray()
    async for chunk in r.aiter_bytes():
      room = _MAX_BYTES - len(buf)
      buf.extend(chunk[:room])
      if len(buf) >= _MAX_BYTES:
        break
    headers = {}
    remaining = r.headers.get("x-ratelimit-remaining")
    if remaining is not None:
      headers["X-RateLimit-Remaining"] = remaining
    return Response(
      content=bytes(buf),
      status_code=r.status_code,
      media_type=r.headers.get("content-type", "application/json"),
      headers=headers,
    )
  finally:
    await r.aclose()


@router.get("/api/{path:path}")
@_limiter.limit("120/minute")
async def github_rest(
  request: Request,
  path: str,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Authenticated GET passthrough to api.github.com (INV2: only GET
  is registered, so the surface is read-only by construction)."""
  token = github_auth.get_token()
  if not token:
    raise HTTPException(status_code=401, detail="GitHub not connected.")
  # urljoin resolves any ../, //host, or absolute-URL smuggling in the
  # captured path; the result must still land on api.github.com.
  target = urljoin(_API_BASE + "/", path)
  parsed = urlparse(target)
  if parsed.scheme != "https" or parsed.netloc != "api.github.com":
    raise HTTPException(
      status_code=400, detail="Path resolves outside api.github.com.",
    )
  if request.url.query:
    target = f"{target}?{request.url.query}"
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    req = client.build_request("GET", target, headers={
      "Authorization": f"Bearer {token}",
      "Accept": (
        request.headers.get("accept") or "application/vnd.github+json"
      ),
      "User-Agent": "mobius",
    })
    return await _forward_capped(client, req)


@router.post("/graphql")
@_limiter.limit("60/minute")
async def github_graphql(
  request: Request,
  body: GraphqlRequest,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Read-only GraphQL passthrough to api.github.com/graphql.

  INV2: the document is scrubbed of strings + comments, then rejected
  if a mutation/subscription keyword remains. The word inside a string
  literal is data, not an operation, and passes; a keyword the scrubber
  can't prove inert is rejected.
  """
  token = github_auth.get_token()
  if not token:
    raise HTTPException(status_code=401, detail="GitHub not connected.")
  scrubbed = _GQL_NOISE.sub(" ", body.query)
  if _GQL_WRITE_OP.search(scrubbed):
    raise HTTPException(
      status_code=400,
      detail=(
        "This surface is read-only: mutations and subscriptions are "
        "not allowed. GitHub writes go through the agent with your "
        "explicit approval."
      ),
    )
  payload: dict = {"query": body.query}
  if body.variables is not None:
    payload["variables"] = body.variables
  async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
    req = client.build_request(
      "POST", f"{_API_BASE}/graphql", json=payload, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "mobius",
      },
    )
    return await _forward_capped(client, req)
