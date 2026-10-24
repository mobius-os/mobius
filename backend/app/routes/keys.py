"""Keys: the owner's vault, the agent's reveal, and the app handshake.

- The owner manages keys in Settings › Keys and answers app requests.
- The owner and the owner's agents may read a key's value.
- An app never receives a value. It asks once for a key by name
  (``POST /api/keys/requests``); after the owner approves, it sends requests
  through ``/api/keys/use/<key>/<host>/<path>`` with its own app token, and
  the vault adds the key on the way out. This route attests which app is
  calling from that token, so an app cannot borrow another app's grant.

Validation and storage belong to the vault; this module authenticates callers
and relays.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app import activity, key_vault, models, push
from app.database import get_db
from app.deps import (
  Principal, get_current_owner, get_principal, is_owner_input_principal,
  reject_cross_site, require_installation_owner_control,
)


def _installation_owner(principal: Principal = Depends(get_principal)) -> None:
  require_installation_owner_control(principal)


router = APIRouter(prefix="/api/keys", tags=["keys"])
# The owner and top-level agents; never an app, a guest or a delegated helper.
owner_only = [Depends(get_current_owner), Depends(_installation_owner)]

MAX_USE_BODY = 10 * 1024 * 1024
# Möbius's own credentials and routing must never travel to a third party.
_DROPPED_REQUEST_HEADERS = frozenset({
  "authorization", "cookie", "host", "content-length", "transfer-encoding",
  "connection", "origin", "referer", "forwarded", "x-real-ip",
})
_DROPPED_RESPONSE_HEADERS = frozenset({
  "content-length", "content-encoding", "transfer-encoding", "connection", "set-cookie",
  "content-security-policy", "x-frame-options", "access-control-allow-origin",
  "access-control-allow-credentials", "strict-transport-security",
})


async def _relay(method: str, path: str, payload: dict | None = None, *, caller: str | None = None) -> dict:
  try:
    return await key_vault.vault_call(method, path, payload, caller=caller)
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  except key_vault.VaultRefused as exc:
    raise HTTPException(exc.status, exc.reason) from exc


def _is_agent(principal: Principal) -> bool:
  return principal.chat_id is not None or principal.delegation_id is not None


def _caller(principal: Principal) -> str:
  """The identity the vault acts for, taken from the caller's own token."""
  if principal.app_id is not None:
    return f"app:{principal.app_id}"
  if principal.browser is not None:
    raise HTTPException(403, "Shared browser access cannot use keys.")
  if principal.scope != "owner":
    raise HTTPException(403, "This credential cannot use keys.")
  if _is_agent(principal):
    return f"agent:{principal.chat_id}" if principal.chat_id else "agent"
  return "owner" if is_owner_input_principal(principal) else "agent"


# -- the owner's view ---------------------------------------------------------

@router.get("", dependencies=owner_only)
async def list_keys() -> dict:
  try:
    keys = (await key_vault.vault_call("GET", "/keys"))["keys"]
    requests = (await key_vault.vault_call("GET", "/requests"))["requests"]
    available = True
  except key_vault.VaultUnavailable:
    keys, requests, available = [], [], False
  return {"available": available, "keys": keys, "requests": requests}


@router.get("/usage", dependencies=owner_only)
async def key_usage(
  key: str | None = Query(None, max_length=64),
  limit: int = Query(50, ge=1, le=500),
) -> dict:
  params = {"limit": limit, **({"key": key} if key else {})}
  return await _relay("GET", f"/usage?{urlencode(params)}")


@router.put("/{name}", dependencies=[*owner_only, Depends(reject_cross_site)])
async def put_key(
  name: str, body: dict = Body(...),
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> dict:
  allowed = {k: body[k] for k in ("value", "inject", "grants") if k in body}
  result = await _relay("PUT", f"/keys/{quote(name, safe='')}", allowed)
  if "value" in allowed and not result.get("created") and _is_agent(principal):
    # Agents may set keys up, but silently swapping one could point every
    # grantee at another account, so the owner hears about each replacement.
    activity.log_event("key_replaced", key=name, chat_id=principal.chat_id)
    await push.notify_owner_async(
      db, principal.owner.id,
      title="An agent replaced a saved key",
      body=f"“{name}” was replaced by an agent. Review it in Settings › Keys.",
      source_type="system",
      target="/shell/?settings=keys",
      tag=f"key-replaced-{name}",
    )
  return result


@router.delete("/{name}", dependencies=[*owner_only, Depends(reject_cross_site)])
async def revoke_key(name: str) -> dict:
  return await _relay("DELETE", f"/keys/{quote(name, safe='')}")


@router.get("/{name}/value")
async def reveal_key(name: str, principal: Principal = Depends(get_principal)) -> dict:
  """The value itself, for the owner and the owner's agents; never for apps."""
  if principal.app_id is not None:
    raise HTTPException(403, "Apps cannot read key values. Ask for the key and use it through /api/keys/use.")
  return await _relay("GET", f"/keys/{quote(name, safe='')}/value", caller=_caller(principal))


@router.post("/requests/{request_id}/approve", dependencies=[*owner_only, Depends(reject_cross_site)])
async def approve_request(request_id: str, body: dict = Body(default={})) -> dict:
  allowed = {k: body[k] for k in ("value", "inject", "sites") if k in body}
  return await _relay("POST", f"/requests/{quote(request_id, safe='')}/approve", allowed)


@router.post("/requests/{request_id}/deny", dependencies=[*owner_only, Depends(reject_cross_site)])
async def deny_request(request_id: str) -> dict:
  return await _relay("POST", f"/requests/{quote(request_id, safe='')}/deny")


@router.delete("/requests/{request_id}", dependencies=[*owner_only, Depends(reject_cross_site)])
async def forget_request(request_id: str) -> dict:
  return await _relay("DELETE", f"/requests/{quote(request_id, safe='')}")


# -- the app handshake --------------------------------------------------------

@router.post("/requests")
async def request_key(
  body: dict = Body(...),
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> dict:
  """An app asks to use a key; asking again reports the current answer.

  The answer is ``granted``, ``pending`` or ``denied``. The owner is told once,
  when the request is new.
  """
  if principal.app_id is None:
    raise HTTPException(403, "Only apps ask for keys; you and your agents manage them in Settings › Keys.")
  app = db.query(models.App).filter(
    models.App.id == principal.app_id, models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(404, "App not found.")
  payload = {k: body[k] for k in ("key", "reason", "sites", "inject") if k in body}
  payload["app_name"] = app.name
  answer = await _relay("POST", "/requests", payload, caller=f"app:{app.id}")
  if answer.pop("created", False):
    activity.log_event("key_requested", app_id=app.id, key=answer["key"])
    await push.notify_owner_async(
      db, principal.owner.id,
      title=f"{app.name} asks to use a key",
      body=f"Review “{answer['key']}” in Settings › Keys.",
      source_type="system",
      target="/shell/?settings=keys",
      tag=f"key-request-{app.id}-{answer['key']}",
    )
  return answer


@router.api_route(
  "/use/{name}/{host}/{path:path}",
  methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def use_key(
  name: str, host: str, path: str, request: Request,
  principal: Principal = Depends(get_principal),
) -> Response:
  """Send one HTTPS request with a key the caller may use but never sees."""
  caller = _caller(principal)
  raw = request.scope.get("raw_path") or request.url.path.encode()
  prefix = b"/api/keys/use/"
  if not raw.startswith(prefix):
    raise HTTPException(400, "Use /api/keys/use/<key>/<host>/<path>.")
  target = "/use/" + raw[len(prefix):].decode("ascii")
  if request.url.query:
    target += "?" + request.url.query
  body = await request.body()
  if len(body) > MAX_USE_BODY:
    raise HTTPException(413, "The request body is too large.")
  headers = [
    (k, v) for k, v in request.headers.items()
    if k.lower() not in _DROPPED_REQUEST_HEADERS
    and not k.lower().startswith(("x-vault-", "x-forwarded-", "x-mobius-", "sec-"))
  ]
  try:
    upstream = await key_vault.vault_use(
      request.method, target, caller=caller, headers=headers, body=body or None,
    )
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  response_headers = {
    k: v for k, v in upstream.headers.items() if k.lower() not in _DROPPED_RESPONSE_HEADERS
  }
  # A third party's answer is served from this origin: it must never run here.
  response_headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
  response_headers["X-Content-Type-Options"] = "nosniff"
  response_headers["Cache-Control"] = "no-store"
  return Response(upstream.content, status_code=upstream.status_code, headers=response_headers)
