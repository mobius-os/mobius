"""HTTP face of the Agent Gateway (see app.agent_gateway).

A confined Claude agent runs with ``ANTHROPIC_BASE_URL=<api>/api/agent-gateway``
and a Codex agent with a model provider whose base URL is
``<api>/api/agent-gateway/v1``; both present their agent key.
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app import activity, agent_gateway
from app.agent_roles import AgentKey
from app.config import get_settings
from app.database import get_db
from app.deps import Principal, get_principal

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent-gateway", tags=["agent-gateway"])


def _agent_key(principal: Principal) -> AgentKey:
  if principal.app_agent is None:
    raise HTTPException(
      status_code=403, detail="The Agent Gateway serves only confined app agents."
    )
  return principal.app_agent


def _error(exc: agent_gateway.GatewayError) -> JSONResponse:
  return JSONResponse(
    {"type": "error", "error": {"type": "gateway_error", "message": exc.message}},
    status_code=exc.status,
  )


def _record_search(key: AgentKey, body: bytes) -> None:
  """Codex sends its searches and the pages it opens as ``commands``."""
  try:
    commands = json.loads(body).get("commands")
  except (ValueError, AttributeError):
    commands = None
  activity.log_event(
    "app_agent_web",
    app_id=key.app_id,
    kind="search",
    commands=json.dumps(commands)[:2000] if commands else None,
  )


async def _relay(
  upstream: agent_gateway.Upstream,
  body: bytes,
  *,
  key: AgentKey,
  path: str,
  removed: list[str],
):
  client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20))
  try:
    reply = await client.send(
      client.build_request(
        "POST", upstream.url, headers=upstream.headers, content=body
      ),
      stream=True,
    )
  except httpx.HTTPError as exc:
    await client.aclose()
    return _error(
      agent_gateway.GatewayError(
        502, f"The AI provider is unreachable ({type(exc).__name__})."
      )
    )
  log.info(
    "agent gateway app=%s provider=%s path=%s status=%s removed_tools=%s",
    key.app_id,
    key.provider,
    path,
    reply.status_code,
    removed,
  )

  async def stream():
    try:
      # Decoded bytes: the reply's content-encoding is not passed on, so the
      # agent must receive plain bytes (httpx asks upstream for gzip).
      async for chunk in reply.aiter_bytes():
        yield chunk
    finally:
      await reply.aclose()
      await client.aclose()

  headers = {
    k: v for k, v in reply.headers.items() if k.lower() in agent_gateway.REPLY_HEADERS
  }
  return StreamingResponse(
    stream(),
    status_code=reply.status_code,
    headers=headers,
    media_type=reply.headers.get("content-type"),
  )


@router.get("/v1/models")
def list_models(principal: Principal = Depends(get_principal)):
  """Answered locally: the run's model is already chosen."""
  _agent_key(principal)
  return {"data": [], "models": [], "object": "list"}


@router.post("/{path:path}")
async def model_request(
  path: str,
  request: Request,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  key = _agent_key(principal)
  # A model reply can stream for minutes; hold no database connection meanwhile.
  db.close()
  raw = await request.body()
  try:
    target = agent_gateway.endpoint(key, path)
    body, removed = agent_gateway.prepare_body(key, raw)
    upstream = await agent_gateway.upstream(
      key,
      target,
      request.url.query,
      dict(request.headers),
      data_dir=get_settings().data_dir,
    )
  except agent_gateway.GatewayError as exc:
    return _error(exc)
  if target in agent_gateway.WEB_ENDPOINTS:
    _record_search(key, raw)
  return await _relay(upstream, body, key=key, path=target, removed=removed)
