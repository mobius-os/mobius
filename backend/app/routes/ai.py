"""AI proxy route: lets mini-apps stream Claude responses."""

import asyncio
import json
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import get_current_owner_or_app

router = APIRouter(prefix="/api/ai", tags=["ai"])


# Tools the mini-app surface is allowed to request. `true` expands to
# this full set; a list-form `tools` is intersected with it. Keeping
# the allowlist here (not in the JWT or DB) so an app can ask for a
# subset on a per-call basis without re-registering itself.
_ALLOWED_TOOLS = {"Bash", "Write", "Read", "Edit", "Glob", "Grep"}


class AiRequest(BaseModel):
  messages: list[dict]
  system: str = ""
  # false = no tools (the default); true = full allowlist; list =
  # exact subset the app wants for this call. List form lets an app
  # declare "I only need Read + Glob" and have the spawned subprocess
  # match. Unknown tool names are rejected (400).
  tools: bool | list[str] = False


def _sse(data: dict) -> str:
  return f"data: {json.dumps(data)}\n\n"


def _resolve_tools(tools: bool | list[str]) -> str:
  """Returns the value passed to `--allowedTools` for the Claude CLI."""
  if tools is False:
    return "none"
  if tools is True:
    return ",".join(sorted(_ALLOWED_TOOLS))
  unknown = [t for t in tools if t not in _ALLOWED_TOOLS]
  if unknown:
    raise HTTPException(
      status_code=400,
      detail=(
        f"Unknown tool(s): {', '.join(unknown)}. "
        f"Allowed: {', '.join(sorted(_ALLOWED_TOOLS))}."
      ),
    )
  return ",".join(tools) if tools else "none"


async def _stream(messages: list[dict], system: str, allowed_tools: str):
  """Streams a Claude response for a mini-app conversation."""
  last = messages[-1].get("content", "") if messages else ""

  cmd = [
    "claude",
    "-p", last,
    "--output-format", "stream-json",
    "--verbose",
    "--allowedTools", allowed_tools,
  ]
  if system:
    cmd += ["--system-prompt", system]
  try:
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = "/data/cli-auth/claude"
    proc = await asyncio.create_subprocess_exec(
      *cmd,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      env=env,
    )

    async for raw in proc.stdout:
      line = raw.decode("utf-8", errors="replace").strip()
      if not line:
        continue
      try:
        event = json.loads(line)
      except json.JSONDecodeError:
        continue

      etype = event.get("type")
      if etype == "assistant":
        for block in event.get("message", {}).get("content", []):
          if block.get("type") == "text" and block.get("text"):
            yield _sse({"type": "text", "content": block["text"]})
      elif etype == "result":
        if event.get("is_error"):
          yield _sse({
            "type": "error",
            "message": event.get("result", "Unknown error."),
          })
        else:
          yield _sse({"type": "done"})
        break

    await proc.wait()
  except Exception as exc:
    yield _sse({"type": "error", "message": str(exc)})


@router.post("")
async def ai_chat(
  body: AiRequest,
  _: models.Owner = Depends(get_current_owner_or_app),
  db: Session = Depends(get_db),
):
  """Streams a Claude response for use inside mini-apps.

  Mini-apps pass their full conversation history in the system prompt
  and send the latest user message as the last item in messages.
  """
  # Validate tools BEFORE opening the stream so a bad tool name
  # returns 400 instead of an SSE error mid-stream.
  allowed_tools = _resolve_tools(body.tools)
  return StreamingResponse(
    _stream(body.messages, body.system, allowed_tools),
    media_type="text/event-stream",
    headers={
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  )
