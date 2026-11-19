"""Keys: the owner's vault, the agent's reveal, and the app handshake.

- The owner and the owner's top-level agents manage keys in Settings › Keys.
  Only the owner's own input lets an app use a key, reach more sites or
  switch back on after a pause (approving its request, adding it to a key,
  widening its sites), or lets the owner and agents send a key to more
  sites, so an agent can never widen where a key goes.
- The owner and the owner's top-level agents may read a key's value, unless
  the key's Agents switch is off. Delegated helpers only borrow.
- An app never receives a value. It asks once for a key by name
  (``POST /api/keys/requests``); after the owner approves, it sends requests
  through ``/api/keys/use/<key>/<host>/<path>`` with its own app token, and
  the vault adds the key on the way out. This route attests which app is
  calling from that token (its id and installation), so an app cannot borrow
  another app's grant, nor one given to an earlier app with the same id.
- A top-level agent that needs a key on a new site asks with the
  ``request_key`` control, which saves an owner card in its chat; the owner
  answers there or in Settings › Keys, which lists every waiting request.

Validation and storage belong to the vault; this module authenticates callers
and relays.
"""

from __future__ import annotations

import contextlib
import re
from urllib.parse import quote, urlencode, urljoin

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import activity, key_vault, models, push
from app.database import get_db
from app.deps import (
  Principal, get_current_owner, get_principal, is_owner_input_principal,
  refuse_app_agent, reject_cross_site, require_installation_owner_control,
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
# own X-Vault-* headers). An app frame is an opaque origin, so of everything
# relayed it reads only the CORS-safelisted headers and those main.py's CORS
# setup exposes (named one by one: CORS has no prefixes); services and jobs
# read them all.
_RELAYED_RESPONSE_PREFIXES = ("ratelimit", "x-ratelimit-", "x-vault-")
_SHARED_BROWSER_REFUSAL = "Shared browser access cannot use keys."
_ASK_FOR_SITES = (
  "Only the owner adds sites a key is sent to. Ask with the request_key tool "
  "(key, reason, sites); the owner answers its card in your chat."
)
# An HTTP header name (RFC 9110 token).
_HEADER_NAME = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


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
    # The installation of the row the token was checked against, never one
    # read afterwards: by then the id may belong to an app that reused it.
    if principal.app_install_id is None:
      raise HTTPException(401, "App token is not bound to an installation.")
    return key_vault.app_caller(principal.app_id, principal.app_install_id)
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


def _agent_changes(existing: dict, body: dict) -> list[str]:
  """What an agent's save changes that could redirect a key's use.

  Agents never widen a key's grants (``put_key`` refuses that), so only the
  value and how it is sent are left to tell the owner about.
  """
  changes = []
  if body.get("value") is not None:
    changes.append("replaced its value")
  replaced = [item.get("name") for item in body.get("parts") or [] if isinstance(item, dict)]
  if replaced:
    changes.append(f"replaced {', '.join(map(str, replaced))}")
  if body.get("remove_parts"):
    changes.append(f"removed {', '.join(map(str, body['remove_parts']))}")
  if body.get("inject") is not None and key_vault.stored_slot(body["inject"]) != existing["inject"]:
    changes.append("changed how it is sent")
  return changes


# -- the owner's view ---------------------------------------------------------

@router.get("", dependencies=owner_only)
async def list_keys(db: Session = Depends(get_db)) -> dict:
  try:
    keys = (await key_vault.vault_call("GET", "/keys"))["keys"]
    requests = (await key_vault.vault_call("GET", "/requests"))["requests"]
    available = True
  except (key_vault.VaultUnavailable, key_vault.VaultTimeout):
    keys, requests, available = [], [], False
  except key_vault.VaultRefused as exc:
    raise HTTPException(exc.status, exc.reason) from exc
  _name_request_chats(db, requests)
  return {"available": available, "keys": keys, "requests": requests}


def _name_request_chats(db: Session, requests: list[dict]) -> None:
  """Give each agent request its chat's title (``chat_title``), or None when
  that chat is gone, so Settings › Keys can say where it was asked."""
  chat_ids = {request["chat_id"] for request in requests if request.get("chat_id")}
  titles = dict(db.query(models.Chat.id, models.Chat.title).filter(
    models.Chat.id.in_(chat_ids), models.Chat.deleted_at.is_(None),
  ).all()) if chat_ids else {}
  for request in requests:
    chat_id = request.get("chat_id")
    if chat_id:
      request["chat_title"] = (titles[chat_id] or "Untitled chat") if chat_id in titles else None


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
  # Each app grant names the app's installation (``install``, from the apps
  # list), and the vault matches app callers by it, so a page loaded before
  # an app was removed and its id reused can only write a grant no app can use.
  allowed = {
    k: body[k] for k in ("value", "parts", "remove_parts", "inject", "grants", "revision", "create_only")
    if k in body
  }
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
      raise HTTPException(
        403, "Only you can let an app use a key, reach more sites or switch back on, from Settings › Keys.",
      )
    if widening.sites:
      raise HTTPException(403, _ASK_FOR_SITES)
    # Saved only onto the key just checked: a narrowing the owner makes
    # meanwhile is refused here (409), never undone by this save.
    if existing is None:
      allowed["create_only"] = True
    elif allowed.get("revision") is None:
      allowed["revision"] = existing["revision"]
  result = await _relay("PUT", f"/keys/{quote(name, safe='')}", allowed, caller=caller)
  changes = _agent_changes(existing, allowed) if existing and _is_agent(principal) else []
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
async def reveal_key(
  name: str, principal: Principal = Depends(get_principal),
  part: str | None = Query(None, max_length=64),
) -> dict:
  """One part's value (``value`` unless ``part`` names another), for the
  owner and the owner's top-level agents; never for apps or delegated
  helpers, which use keys through /api/keys/use."""
  query = f"?{urlencode({'part': part})}" if part else ""
  return await _relay("GET", f"/keys/{quote(name, safe='')}/value{query}", caller=reader_caller(principal))


@router.post("/requests/{request_id}/approve", dependencies=[*owner_only, Depends(reject_cross_site)])
async def approve_request(
  request_id: str, body: dict = Body(default={}),
  principal: Principal = Depends(get_principal),
) -> dict:
  """Grant exactly what the owner's answer carries: the app only proposed it.

  Letting an app use a key is the owner's own decision, so agents are refused.
  """
  _refuse_unless_owners_own_input(principal)
  allowed = {k: body[k] for k in ("value", "parts", "inject", "sites", "ticket") if k in body}
  return await _relay("POST", f"/requests/{quote(request_id, safe='')}/approve", allowed,
                      caller=_caller(principal))


@router.post("/requests/{request_id}/deny", dependencies=[*owner_only, Depends(reject_cross_site)])
async def deny_request(
  request_id: str, body: dict = Body(default={}),
  principal: Principal = Depends(get_principal),
) -> dict:
  """Refuse an ask; a ``ticket`` names the exact request the owner saw.

  A refusal is the owner's answer about the key, so agents are refused.
  """
  _refuse_unless_owners_own_input(principal)
  allowed = {k: body[k] for k in ("ticket",) if k in body}
  return await _relay("POST", f"/requests/{quote(request_id, safe='')}/deny", allowed,
                      caller=_caller(principal))


@router.delete("/requests/{request_id}", dependencies=[*owner_only, Depends(reject_cross_site)])
async def forget_request(request_id: str, principal: Principal = Depends(get_principal)) -> dict:
  """Remove a request, or let an asker the owner refused ask again: the
  owner's own choice, never an agent's."""
  _refuse_unless_owners_own_input(principal)
  return await _relay("DELETE", f"/requests/{quote(request_id, safe='')}", caller=_caller(principal))


def _refuse_unless_owners_own_input(principal: Principal) -> None:
  """Answering or removing a key request is the owner's own decision: an
  agent could otherwise refuse another chat's request, close its card, or
  undo the owner's refusal of an app."""
  if not is_owner_input_principal(principal):
    raise HTTPException(403, "Only you can answer or remove a key request, from Settings › Keys.")


# -- the app handshake --------------------------------------------------------

@router.post("/requests")
async def request_key(
  body: dict = Body(...),
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
) -> dict:
  """An app asks to use a key; asking again reports the current answer.

  The answer is ``granted``, ``paused`` (an app the owner switched off on the
  key), ``pending`` or ``denied``. A granted asker wanting sites it lacks
  gets ``sites_not_allowed`` and ``sites_request`` (the state of its ask for
  those sites). Nothing notifies the owner: a new ask nudges an open shell
  (_key_requests_changed), and the owner answers it in the moment.
  """
  if principal.browser is not None:
    raise HTTPException(403, _SHARED_BROWSER_REFUSAL)
  # The app itself asks, from its own window, where the owner answers.
  refuse_app_agent(principal, "ask for keys; it uses the keys its app may use")
  if principal.app_id is None:
    # An agent's ask is saved as a card in its chat, so it never waits where
    # the owner can't answer it.
    raise HTTPException(403, "Only apps ask here. An agent asks with the request_key tool, which shows "
                        "the owner a card in its chat; you manage keys in Settings › Keys.")
  payload = {k: body[k] for k in ("key", "reason", "sites", "inject", "parts") if k in body}
  app = db.query(models.App).filter(
    models.App.id == principal.app_id, models.App.deleted_at.is_(None),
  ).first()
  if app is None:
    raise HTTPException(404, "App not found.")
  payload["app_name"] = app.name
  caller = _caller(principal)
  # Queued under the app's lock while the token's installation is still the
  # app, so a removed app leaves no request behind (the vault would never let
  # another installation use what approving it grants, but the owner would
  # still see it over the app that reused the id).
  try:
    async with key_vault.for_live_app(app.id, authorized_app_nonce(principal, app)):
      answer = await _relay("POST", "/requests", payload, caller=caller)
  except key_vault.AppGone:
    raise HTTPException(404, "App not found.") from None
  if answer.pop("created", False):
    activity.log_event("key_requested", app_id=app.id, key=answer["key"])
    _key_requests_changed()
  return answer


async def agent_asks(payload: dict, principal: Principal) -> dict:
  """A top-level agent asks the owner to send a key to sites; the owner
  answers the saved card the ``request_key`` control shows in the agent's
  chat (app/key_request_cards.py). Delegated helpers ask the agent that
  started them."""
  if principal.delegation_id is not None:
    raise HTTPException(403, "Delegated helpers cannot ask for keys; the agent that started you can.")
  refuse_app_agent(principal, "ask for keys; it uses the keys its app may use")
  # Never work a shared-browser guest started: the ask would act for the owner.
  require_installation_owner_control(principal)
  if is_owner_input_principal(principal):
    raise HTTPException(403, "Only apps and your agents ask for keys; you manage them in Settings › Keys.")
  answer = await _relay("POST", "/requests", payload, caller=_caller(principal))
  if answer.pop("created", False):
    activity.log_event("key_requested", chat_id=principal.chat_id, key=answer["key"])
    _key_requests_changed()
  return answer


def _key_requests_changed() -> None:
  """Asks are answered in the moment, never through a notification: an
  app's over its window while the owner has it open (straight away, or when
  they next open it), an agent's in the chat that asked. This content-free
  nudge on the system bus makes an open shell re-read the requests, so a
  card appears over a visible app as soon as it asks. Not in
  SYSTEM_EVENT_TYPES: only the server announces it."""
  from app.broadcast import get_system_broadcast
  get_system_broadcast().publish({"type": "key_requests_changed"})


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
  headers = _relayed_request_headers(request)
  body = await _bounded_body(request)
  # An app's borrow is checked once its body has arrived: the app its token
  # names must still be installed, and the vault matches its installation.
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
  if "x-vault-key" in upstream.headers:
    # The vault sets this on every answer once the request had left with the
    # key: the site's own answer, or a failure after sending (the site hung
    # up, an encoding it cannot read). A refusal before sending lacks it.
    from app.agent_roles import record_agent_use

    record_agent_use(request, "use_key", key=name, site=host)
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


def _relayed_request_headers(request: Request) -> list[tuple[str, str]]:
  """The caller's headers that travel on to the site, refused (400) when
  one cannot be carried there.

  The relay to the vault encodes header text as ASCII, and the vault refuses
  a value spanning lines, so a name must be an HTTP token and a value
  printable ASCII (tabs allowed), as the destination's path and query must
  be; whitespace around a value is trimmed. Refused here, before a borrow
  starts, such a request is a caller's mistake, never a failed relay.
  Dropped headers (a browser's ``Referer``) never count.
  """
  headers = []
  for raw_name, raw_value in request.headers.raw:
    name = raw_name.decode("latin-1")
    lowered = name.lower()
    if lowered in _DROPPED_REQUEST_HEADERS or lowered.startswith(("x-vault-", "x-forwarded-", "x-mobius-", "sec-")):
      continue
    if not _HEADER_NAME.fullmatch(raw_name):
      raise HTTPException(400, "A header name has characters HTTP can't carry.")
    # Whitespace around a value is not part of it (RFC 9110 §5.5), and the
    # relay refuses to send it; inside a value it is ordinary text.
    raw_value = raw_value.strip(b" \t")
    if any(not (0x20 <= byte <= 0x7E or byte == 0x09) for byte in raw_value):
      raise HTTPException(
        400, f"The {name} header has characters HTTP can't carry; send it as plain ASCII.",
      )
    headers.append((name, raw_value.decode("ascii")))
  return headers


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
