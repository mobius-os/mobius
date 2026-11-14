"""HTTP face of the Agent Gateway (see app.agent_gateway).

A confined Claude agent runs with ``ANTHROPIC_BASE_URL=<api>/api/agent-gateway``
and a Codex agent with a model provider whose base URL is
``<api>/api/agent-gateway/v1``; both present their agent key.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import activity, agent_gateway, auth, models
from app.agent_roles import AgentKey
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal,
  get_agent_run_principal,
  get_principal,
  reject_cross_site,
)
from app.generated_files import chat_folder

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
    chat_id=key.chat,
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


# --- tools Möbius runs for a confined agent, outside its room -------------------

app_agent_router = APIRouter(prefix="/api/app-agent", tags=["app-agent"])
_SCREENSHOT_SCRIPT = (
  Path(__file__).resolve().parents[2] / "scripts" / "agent-screenshot.sh"
)
_SCREENSHOT_SECONDS = 230


class ScreenshotRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  width: int | None = Field(default=None, ge=320, le=3840)
  height: int | None = Field(default=None, ge=320, le=2160)


def _capture_env(
  chat_id: str, token: str, request: ScreenshotRequest
) -> dict[str, str]:
  """The browser settings a chat run's own captures use (chat._run_chat_impl)."""
  from app import browser_profiles
  from app.agent_scratch import scratch_for_chat
  from app.chat import (
    agent_browser_runtime_env,
    bounded_agent_browser_args,
    desktop_pointer_browser_env,
    viewport_env,
  )

  env = {
    k: v
    for k, v in os.environ.items()
    if k in ("PATH", "LANG", "LC_ALL", "USER", "SHELL")
  }
  env.update(
    {
      "HOME": os.environ.get("HOME", "/tmp"),
      "TMPDIR": str(scratch_for_chat(chat_id)),
      "API_BASE_URL": get_settings().api_base_url,
      "AGENT_TOKEN": token,
      "CHAT_ID": chat_id,
      "AGENT_BROWSER_SESSION": f"chat-{chat_id}",
      "AGENT_BROWSER_PROFILE": str(browser_profiles.chat_browser_profile_path(chat_id)),
      "AGENT_BROWSER_ARGS": bounded_agent_browser_args(
        os.environ.get("AGENT_BROWSER_ARGS")
      ),
      "MOBIUS_SCREENSHOT_BUDGET_SECONDS": str(_SCREENSHOT_SECONDS - 10),
    }
  )
  viewport = (
    {"width": request.width, "height": request.height} if request.width else None
  )
  env.update(viewport_env(viewport))
  env.update(agent_browser_runtime_env())
  env.update(desktop_pointer_browser_env(env))
  return env


@app_agent_router.post("/screenshot", dependencies=[Depends(reject_cross_site)])
async def app_screenshot(
  body: ScreenshotRequest,
  principal: Principal = Depends(get_agent_run_principal),
  db: Session = Depends(get_db),
):
  """The ``screenshot`` tool: this app's own standalone page, captured by
  Möbius with the owner's sign-in, which never enters the room."""
  key = principal.app_agent
  if key is None or "screenshot" not in key.tools:
    raise HTTPException(
      status_code=403, detail="Screenshots are not one of this app's agent tools."
    )
  app = db.get(models.App, key.app_id)
  if app is None:
    raise HTTPException(status_code=404, detail="App not found.")
  owner = principal.owner
  # Bound to this run, so it expires with it; only the capture browser holds it.
  token = auth.create_agent_token(
    key.chat, owner.username, owner.token_epoch, run_id=key.run
  )
  route = f"/apps/{app.slug}/"
  db.close()
  out = (
    chat_folder(get_settings().data_dir, key.chat)
    / "media"
    / f"app-screenshot-{uuid.uuid4().hex}.png"
  )
  out.parent.mkdir(parents=True, exist_ok=True)
  process = await asyncio.create_subprocess_exec(
    "bash",
    str(_SCREENSHOT_SCRIPT),
    "--content-only",
    route,
    str(out),
    env=_capture_env(key.chat, token, body),
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
  )
  try:
    _stdout, stderr = await asyncio.wait_for(process.communicate(), _SCREENSHOT_SECONDS)
  except TimeoutError as exc:
    process.kill()
    await process.wait()
    raise HTTPException(status_code=504, detail="The screenshot timed out.") from exc
  if process.returncode != 0 or not out.is_file():
    reason = " / ".join(stderr.decode("utf-8", "replace").strip().splitlines()[-2:])
    raise HTTPException(status_code=502, detail=f"The screenshot failed: {reason}")
  return {
    "image": base64.b64encode(out.read_bytes()).decode("ascii"),
    "note": f"This is {app.name} as the owner sees it.",
  }
