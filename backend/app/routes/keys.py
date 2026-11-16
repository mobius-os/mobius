"""Keys: the owner's vault, the agent's reveal, and the app handshake.

- The owner and the owner's top-level agents manage keys in Settings › Keys.
  Only the owner's own input lets an app use a key or reach more sites
  (approving its request, adding it to a key, widening its sites), so an
  agent can never widen what an app may borrow.
- The owner and the owner's top-level agents may read a key's value, unless
  the key's Agents switch is off. Delegated helpers only borrow.
- An app never receives a value. It asks once for a key by name
  (``POST /api/keys/requests``); after the owner approves, it sends requests
  through ``/api/keys/use/<key>/<host>/<path>`` with its own app token, and
  the vault adds the key on the way out. This route attests which app is
  calling from that token, so an app cannot borrow another app's grant.

Validation and storage belong to the vault; this module authenticates callers
and relays.
"""

from __future__ import annotations

import contextlib
from urllib.parse import quote, urlencode, urljoin

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import activity, key_vault, models, push
from app.database import get_db
from app.deps import (
  Principal, get_current_owner, get_principal, is_owner_input_principal,
  reject_cross_site, require_installation_owner_control,
)
from app.resource_access import authorized_app_nonce


def _installation_owner(principal: Principal = Depends(get_principal)) -> None:
  require_installation_owner_control(principal)


router = APIRouter(prefix="/api/keys", tags=["keys"])
# The owner and top-level agents; never an app, a guest or a delegated helper.
owner_only = [Depends(get_current_owner), Depends(_installation_owner)]

MAX_USE_BODY = 10 * 1024 * 1024
# Möbius's own credentials and routing must never travel to a third party.
# Neither may headers some servers read as the real method or path: the vault
# checks the wire method and path (it drops these too).
_DROPPED_REQUEST_HEADERS = frozenset({
  "authorization", "cookie", "host", "content-length", "transfer-encoding",
  "connection", "origin", "referer", "forwarded", "x-real-ip",
  "x-http-method-override", "x-http-method", "x-method-override",
  "x-original-url", "x-rewrite-url", "x-override-url",
})
# A third party's answer is served from this origin, so only headers that
# describe its content pass. Anything else could act on the origin itself
# (cookies, Clear-Site-Data, Refresh, Link preloads, CORS, framing).
_RELAYED_RESPONSE_HEADERS = frozenset({
  "content-type", "content-language", "content-disposition", "etag",
  "last-modified", "expires", "vary", "retry-after",
})
# Rate-limit families, and what the vault itself says (it drops the site's
# own X-Vault-* headers).
_RELAYED_RESPONSE_PREFIXES = ("ratelimit", "x-ratelimit-", "x-vault-")
_SHARED_BROWSER_REFUSAL = "Shared browser access cannot use keys."


@contextlib.contextmanager
def vault_outcomes_as_http():
  """Answer the vault's outcomes as HTTP: 503 when it is not running or
  closed without an answer, 504 when it (usually the site) was too slow,
  and its own refusals as it gave them (429 busy, 500 when it could not
  write its files or decrypt a key, ...)."""
  try:
    yield
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  except key_vault.VaultTimeout as exc:
    raise HTTPException(504, str(exc)) from exc
  except key_vault.VaultRefused as exc:
    raise HTTPException(exc.status, exc.reason) from exc


async def _relay(method: str, path: str, payload: dict | None = None, *, caller: str | None = None) -> dict:
  with vault_outcomes_as_http():
    return await key_vault.vault_call(method, path, payload, caller=caller)


def _is_agent(principal: Principal) -> bool:
  return principal.chat_id is not None or principal.delegation_id is not None


def _caller(principal: Principal) -> str:
  """The identity the vault acts for, taken from the caller's own token.

  A shared-browser guest never uses keys, nor does anything it started,
  including an app token it minted.
  """
  if principal.browser is not None:
    raise HTTPException(403, _SHARED_BROWSER_REFUSAL)
  if principal.app_id is not None:
    return f"app:{principal.app_id}"
  if principal.scope != "owner":
    raise HTTPException(403, "This credential cannot use keys.")
  if _is_agent(principal):
    return f"agent:{principal.chat_id}" if principal.chat_id else "agent"
  return "owner" if is_owner_input_principal(principal) else "agent"


def reader_caller(principal: Principal) -> str:
  """The caller a key's value is revealed to: the owner or a top-level agent.

  Apps borrow instead, and so do delegated helpers: the owner keeps values
  out of work an agent hands off.
  """
  if principal.app_id is not None:
    raise HTTPException(403, "Apps cannot read key values. Ask for the key and use it through /api/keys/use.")
  if principal.delegation_id is not None:
    raise HTTPException(
      403, "Delegated helpers cannot read key values. Use the key through /api/keys/use instead.",
    )
  return _caller(principal)


def _granted_app_ids(grants) -> list[int]:
  """Each app a save names; the vault refuses malformed grants."""
  apps = grants.get("apps") if isinstance(grants, dict) else None
  return [
    grant["id"] for grant in (apps if isinstance(apps, list) else [])
    if isinstance(grant, dict) and type(grant.get("id")) is int and grant["id"] >= 1
  ]


def _agent_changes(existing: dict, body: dict, widening: key_vault.GrantWidening) -> list[str]:
  """What an agent's save changes that could redirect or widen a key's use."""
  changes = []
  if body.get("value") is not None:
    changes.append("replaced its value")
  if body.get("inject") is not None and body["inject"] != existing["inject"]:
    changes.append("changed how it is sent")
  if widening.sites:
    changes.append("added sites")
  return changes


# -- the owner's view ---------------------------------------------------------

@router.get("", dependencies=owner_only)
async def list_keys() -> dict:
  try:
    keys = (await key_vault.vault_call("GET", "/keys"))["keys"]
    requests = (await key_vault.vault_call("GET", "/requests"))["requests"]
    available = True
  except (key_vault.VaultUnavailable, key_vault.VaultTimeout):
    keys, requests, available = [], [], False
  except key_vault.VaultRefused as exc:
    raise HTTPException(exc.status, exc.reason) from exc
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
  allowed = {
    k: body[k] for k in ("value", "inject", "grants", "revision", "create_only") if k in body
  }
  installations = body.get("installations")
  caller = _caller(principal)
  existing = next(
    (key for key in (await _relay("GET", "/keys"))["keys"] if key["name"] == name), None,
  )
  # Omitted grants are kept by the vault: no change.
  widening = key_vault.grant_widening(existing["grants"] if existing else None, allowed.get("grants"))
  if not is_owner_input_principal(principal):
    if widening.agents:
      raise HTTPException(403, "Only you can let agents use this key, from Settings › Keys.")
    if widening.apps:
      raise HTTPException(403, "Only you can let an app use a key or reach more sites, from Settings › Keys.")
    # Saved only onto the key just checked: a narrowing the owner makes
    # meanwhile is refused here (409), never undone by this save.
    if existing is None:
      allowed["create_only"] = True
    elif allowed.get("revision") is None:
      allowed["revision"] = existing["revision"]
  try:
    with vault_outcomes_as_http():
      async with key_vault.for_granted_apps(
        name, _granted_app_ids(allowed.get("grants")),
        installations if isinstance(installations, dict) else {},
      ):
        result = await key_vault.vault_call(
          "PUT", f"/keys/{quote(name, safe='')}", allowed, caller=caller,
        )
  except key_vault.AppGone as gone:
    raise HTTPException(
      409, f"App {gone.args[0]} is not the installation you chose; it may have been "
      "removed or replaced. Reload and choose it again.",
    ) from None
  changes = _agent_changes(existing, allowed, widening) if existing and _is_agent(principal) else []
  if changes:
    # Agents may set keys up, but a silent change could point every grantee
    # at another account or another site, so the owner hears about each one.
    activity.log_event("key_changed", key=name, chat_id=principal.chat_id, changes=changes)
    await push.notify_owner_async(
      db, principal.owner.id,
      title="An agent changed a saved key",
      body=f"An agent {' and '.join(changes)} for “{name}”. Review it in Settings › Keys.",
      source_type="system",
      target="/shell/?settings=keys",
      tag=f"key-changed-{name}",
    )
  return result


@router.delete("/{name}", dependencies=[*owner_only, Depends(reject_cross_site)])
async def revoke_key(name: str, principal: Principal = Depends(get_principal)) -> dict:
  return await _relay("DELETE", f"/keys/{quote(name, safe='')}", caller=_caller(principal))


@router.get("/{name}/value")
async def reveal_key(name: str, principal: Principal = Depends(get_principal)) -> dict:
  """The value itself, for the owner and the owner's top-level agents; never
  for apps or delegated helpers, which use keys through /api/keys/use."""
  return await _relay("GET", f"/keys/{quote(name, safe='')}/value", caller=reader_caller(principal))


@router.post("/requests/{request_id}/approve", dependencies=[*owner_only, Depends(reject_cross_site)])
async def approve_request(
  request_id: str, body: dict = Body(default={}),
  principal: Principal = Depends(get_principal),
) -> dict:
  """Grant exactly what the owner's answer carries: the app only proposed it.

  Letting an app use a key is the owner's own decision, so agents are refused.
  """
  if not is_owner_input_principal(principal):
    raise HTTPException(403, "Only you can let an app use a key, from Settings › Keys.")
  allowed = {k: body[k] for k in ("value", "inject", "sites", "ticket") if k in body}
  return await _relay("POST", f"/requests/{quote(request_id, safe='')}/approve", allowed,
                      caller=_caller(principal))


@router.post("/requests/{request_id}/deny", dependencies=[*owner_only, Depends(reject_cross_site)])
async def deny_request(
  request_id: str, body: dict = Body(default={}),
  principal: Principal = Depends(get_principal),
) -> dict:
  """Refuse an ask; a ``ticket`` names the exact request the owner saw."""
  allowed = {k: body[k] for k in ("ticket",) if k in body}
  return await _relay("POST", f"/requests/{quote(request_id, safe='')}/deny", allowed,
                      caller=_caller(principal))


@router.delete("/requests/{request_id}", dependencies=[*owner_only, Depends(reject_cross_site)])
async def forget_request(request_id: str, principal: Principal = Depends(get_principal)) -> dict:
  return await _relay("DELETE", f"/requests/{quote(request_id, safe='')}", caller=_caller(principal))


# -- the app handshake --------------------------------------------------------

@router.post("/requests")
async def request_key(
  body: dict = Body(...),
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> dict:
  """An app asks to use a key; asking again reports the current answer.

  The answer is ``granted``, ``pending`` or ``denied``. A granted app asking
  for sites it lacks gets ``sites_not_allowed`` and ``sites_request`` (the
  state of its ask for those sites). The owner is told once, when an ask is new.
  """
  if principal.browser is not None:
    raise HTTPException(403, _SHARED_BROWSER_REFUSAL)
  if principal.app_id is None:
    raise HTTPException(403, "Only apps ask for keys; you and your agents manage them in Settings › Keys.")
  app = db.query(models.App).filter(
    models.App.id == principal.app_id, models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(404, "App not found.")
  payload = {k: body[k] for k in ("key", "reason", "sites", "inject") if k in body}
  payload["app_name"] = app.name
  # Queued under the app's lock, for the installation the token names: a
  # request left behind by a removed app would otherwise grant its key to an
  # unrelated app that reuses the id.
  try:
    async with key_vault.for_live_app(app.id, authorized_app_nonce(principal, app)):
      answer = await _relay("POST", "/requests", payload, caller=f"app:{app.id}")
  except key_vault.AppGone:
    raise HTTPException(404, "App not found.") from None
  if answer.pop("created", False):
    activity.log_event("key_requested", app_id=app.id, key=answer["key"])
    await push.notify_owner_async(
      db, principal.owner.id,
      title=(f"{app.name} asks to use a key on more sites" if answer.get("sites_request")
             else f"{app.name} asks to use a key"),
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
  rest, query = raw[len(prefix):], request.scope.get("query_string") or b""
  # The vault reads the destination byte for byte; anything but printable
  # ASCII must arrive percent-encoded.
  if any(not 0x21 <= byte <= 0x7E for byte in rest + query):
    raise HTTPException(400, "Percent-encode spaces and non-ASCII characters in the path and query.")
  target = "/use/" + rest.decode("ascii") + ("?" + query.decode("ascii") if query else "")
  body = await _bounded_body(request)
  headers = [
    (k, v) for k, v in request.headers.items()
    if k.lower() not in _DROPPED_REQUEST_HEADERS
    and not k.lower().startswith(("x-vault-", "x-forwarded-", "x-mobius-", "sec-"))
  ]
  # The vault knows apps by their reusable id. An app's borrow is checked once
  # its body has arrived, for the installation its token names, and the app
  # cannot be purged (freeing the id) while the vault could still act on it.
  try:
    if principal.app_id is not None:
      answer = await key_vault.borrow(
        principal.app_id, principal.app_instance_id, request.method, target,
        caller=caller, headers=headers, body=body or None,
      )
    else:
      answer = await key_vault.vault_use(
        request.method, target, caller=caller, headers=headers, body=body or None,
      )
  except key_vault.AppGone:
    raise HTTPException(404, "App not found.") from None
  except key_vault.VaultUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  except key_vault.VaultTimeout as exc:
    raise HTTPException(504, str(exc)) from exc
  upstream = answer[0]
  response_headers = {
    k: v for k, v in upstream.headers.items()
    if k.lower() in _RELAYED_RESPONSE_HEADERS or k.lower().startswith(_RELAYED_RESPONSE_PREFIXES)
  }
  location = upstream.headers.get("location")
  if location is not None:
    # Never as Location: a browser would follow it, and a relative one
    # would send the app's request back into Möbius. The app reads where
    # the site points, resolved against the site, and decides itself.
    response_headers["X-Vault-Location"] = urljoin("https://" + target.split("/", 3)[3], location)
  # A third party's answer is served from this origin: it must never run here.
  response_headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
  response_headers["X-Content-Type-Options"] = "nosniff"
  response_headers["Cache-Control"] = "no-store"

  async def pieces():
    # Pieces pass on as the site sends them (streamed model output, server
    # events). A break mid-answer propagates, so the server ends this
    # response without its closing chunk: the app sees an incomplete body,
    # never a truncated one that looks complete.
    try:
      async for piece in upstream.aiter_raw():
        yield piece
    finally:
      await key_vault.close_use(answer)

  return StreamingResponse(pieces(), status_code=upstream.status_code, headers=response_headers)


async def _bounded_body(request: Request) -> bytes:
  """The request body, refused (413) once it exceeds ``MAX_USE_BODY``.

  A declared Content-Length over the limit is refused before reading
  anything, and a chunked body is read only up to the limit.
  """
  declared = request.headers.get("content-length")
  if declared is not None and declared.strip().isdigit() and int(declared) > MAX_USE_BODY:
    raise HTTPException(413, "The request body is too large.")
  body = bytearray()
  async for piece in request.stream():
    body += piece
    if len(body) > MAX_USE_BODY:
      raise HTTPException(413, "The request body is too large.")
  return bytes(body)
