"""GitHub connection routes: device flow, PAT fallback, read surface.

Connect endpoints persist a token via app.github_auth (owner OR a
github_access app — so the Contribute app can drive connect from its
own UI — CSRF-guarded, rate-limited — INV4). github_access is a
connection-management grant, not a read scope: an app with it can
start/complete the connect flow, submit a PAT, and disconnect. A
normal connect still needs the owner to authorize on github.com or
paste their own token, but the grant itself is powerful — see the
get_owner_or_app_with_github_access docstring. The read surface
(/api/{path}, /graphql) is read-only by construction (INV2): the REST
passthrough registers GET only, and the GraphQL endpoint rejects any
document containing a mutation or subscription operation. This surface
has no GitHub write endpoint at all; GitHub writes happen through the
agent's gh CLI, where the contributing.md skill (not this code) is what
holds the per-action owner-approval gate.

The token itself never appears in any response or log line (INV1).
"""

import logging
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app import github_auth, models
from app.config import get_settings
from app.deps import (
  get_owner_or_app_with_github_access,
  reject_cross_site,
)

router = APIRouter(prefix="/api/github", tags=["github"])
_limiter = Limiter(key_func=get_remote_address)
log = logging.getLogger("moebius.github")

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
_API_BASE = "https://api.github.com"

# Response cap + timeout mirror routes/proxy.py: GitHub payloads the
# dashboard needs are small; anything bigger is truncated, not buffered.
_MAX_BYTES = 2 * 1024 * 1024

# INV2 scanner. The single alternation matters: matching block strings,
# strings, and comments in ONE left-to-right pass means a `"""` inside a
# comment (or a `#` inside a string) can't confuse the scrubber into
# eating — or keeping — the wrong span, which a strip-strings-then-
# comments sequence would allow. Unterminated constructs simply don't
# match, so their content stays visible to the operation scan and an
# ambiguous document is rejected rather than trusted.
_GQL_NOISE = re.compile(
  r'"""(?:[^"]|"(?!""))*"""'  # block strings (may span lines)
  r'|"(?:\\.|[^"\\\n])*"'     # single-line strings with escapes
  r"|#[^\n]*"                 # comments
)
_GQL_WRITE_OP = re.compile(r"\b(?:mutation|subscription)\b", re.IGNORECASE)


class GithubTokenRequest(BaseModel):
  token: str


class GraphqlRequest(BaseModel):
  query: str
  variables: dict | None = None


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
  return r.status_code, data.get("login") or "", data.get("id"), scopes


@router.post("/connect/start", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("3/minute")
async def connect_start(
  request: Request,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Starts the GitHub device flow; returns the code the owner enters."""
  client_id = get_settings().github_oauth_client_id
  if not client_id:
    raise HTTPException(
      status_code=409,
      detail=(
        "Device flow is not configured on this instance "
        "(GITHUB_OAUTH_CLIENT_ID is unset). Connect with a classic "
        "personal access token instead."
      ),
    )
  try:
    async with httpx.AsyncClient(timeout=15.0) as client:
      r = await client.post(
        _DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": "public_repo"},
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
        "Connect with a classic personal access token instead."
      ),
    )
  if r.status_code != 200 or "device_code" not in payload:
    log.error("GitHub device/code failed (%d)", r.status_code)
    raise HTTPException(
      status_code=502, detail="GitHub device flow could not be started.",
    )
  now = time.time()
  interval = int(payload.get("interval", 5))
  expires_in = int(payload.get("expires_in", 900))
  github_auth.set_device_flow({
    "device_code": payload["device_code"],
    "interval": interval,
    "next_poll_at": now + interval,
  })
  return {
    "user_code": payload["user_code"],
    "verification_uri": payload["verification_uri"],
    "expires_in": expires_in,
    "interval": interval,
  }


@router.post("/connect/poll", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("30/minute")
async def connect_poll(
  request: Request,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Polls the in-flight device flow once.

  Statuses: none (no flow), pending (keep polling), failed (flow
  cleared; `reason` says why), complete (credentials stored). Polls
  arriving before GitHub's requested interval are answered pending
  WITHOUT an upstream call — the server enforces the pacing so an
  eager frontend can't trip GitHub's slow_down escalation.
  """
  flow = github_auth.get_device_flow()
  if not flow:
    return {"status": "none"}
  now = time.time()
  if now < flow["next_poll_at"]:
    return {"status": "pending"}
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
    raise HTTPException(status_code=502, detail="Could not reach GitHub.")
  try:
    payload = r.json()
  except ValueError:
    payload = {}
  error = payload.get("error")
  if error == "authorization_pending":
    flow["next_poll_at"] = now + flow["interval"]
    return {"status": "pending"}
  if error == "slow_down":
    # GitHub sends the new minimum interval; honor it, never shrink,
    # and always back off at least 5s beyond the previous pace.
    flow["interval"] = max(
      int(payload.get("interval", 0)), flow["interval"] + 5,
    )
    flow["next_poll_at"] = now + flow["interval"]
    return {"status": "pending"}
  if error:
    # expired_token / access_denied / anything unexpected: the flow is
    # dead either way — clear it so the frontend can offer a restart.
    github_auth.set_device_flow(None)
    return {"status": "failed", "reason": error}
  token = payload.get("access_token")
  if not token:
    github_auth.set_device_flow(None)
    return {"status": "failed", "reason": "no_access_token"}
  status, login, user_id, scopes = await _github_user(token)
  if status != 200 or not login:
    github_auth.set_device_flow(None)
    return {"status": "failed", "reason": "user_lookup_failed"}
  github_auth.write_credentials(
    token=token, login=login, user_id=user_id, scopes=scopes,
    source="device",
  )
  github_auth.set_device_flow(None)
  return {"status": "complete", "login": login}


@router.post("/connect/token", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("5/minute")
async def connect_token(
  request: Request,
  body: GithubTokenRequest,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Connects GitHub with a pasted classic personal access token."""
  token = body.token.strip()
  if token.startswith("github_pat_"):
    raise HTTPException(
      status_code=400,
      detail=(
        "That is a fine-grained personal access token — GitHub does "
        "not let those write to public repos you don't own, so it "
        "can't open pull requests upstream. Use a classic token with "
        "the public_repo scope (or the device flow)."
      ),
    )
  if not token:
    raise HTTPException(status_code=400, detail="Token is empty.")
  status, login, user_id, scopes = await _github_user(token)
  if status != 200 or not login:
    raise HTTPException(
      status_code=400, detail="GitHub rejected the token.",
    )
  if "repo" not in scopes and "public_repo" not in scopes:
    granted = ", ".join(scopes) if scopes else "none"
    raise HTTPException(
      status_code=400,
      detail=(
        "The token lacks the public_repo (or repo) scope needed to "
        f"contribute — its scopes are: {granted}."
      ),
    )
  github_auth.write_credentials(
    token=token, login=login, user_id=user_id, scopes=scopes, source="pat",
  )
  return {"login": login}


@router.get("/status")
async def github_status(
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Connection metadata for the Contribute app's UI. Never the token
  (INV1).

  Gated on github_access like the rest of the surface: status still discloses
  the owner's GitHub login + scope list, so an app without the grant shouldn't
  read it. The owner (Settings) always passes; the Contribute app holds the
  grant. (A malicious same-origin app can already read the owner JWT and call
  the granted endpoints directly — this is least-privilege consistency, not a
  new boundary.)"""
  state = github_auth.read_state() or {}
  connected = bool(state.get("token"))
  return {
    "connected": connected,
    "login": state.get("login") if connected else None,
    "scopes": (state.get("scopes") or []) if connected else [],
    "token_source": state.get("token_source") if connected else None,
    "device_flow_available": bool(get_settings().github_oauth_client_id),
    "gh_version": github_auth.gh_version(),
  }


@router.delete("/connect", dependencies=[Depends(reject_cross_site)])
@_limiter.limit("5/minute")
def github_disconnect(
  request: Request,
  _: models.Owner = Depends(get_owner_or_app_with_github_access),
):
  """Disconnects GitHub — removes the stored credentials."""
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
