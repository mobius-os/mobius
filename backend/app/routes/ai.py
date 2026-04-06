"""AI proxy route: lets mini-apps stream Claude responses."""

import asyncio
import json
import os

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.deps import get_current_owner

router = APIRouter(prefix="/api/ai", tags=["ai"])


class AiRequest(BaseModel):
  messages: list[dict]
  system: str = ""
  tools: bool = False


def _sse(data: dict) -> str:
  return f"data: {json.dumps(data)}\n\n"


async def _stream(messages: list[dict], system: str, tools: bool):
  """Streams a Claude response for a mini-app conversation."""
  last = messages[-1].get("content", "") if messages else ""

  cmd = [
    "claude",
    "-p", last,
    "--output-format", "stream-json",
    "--verbose",
  ]
  if tools:
    cmd += ["--allowedTools", "Bash,Write,Read,Edit,Glob,Grep"]
  else:
    cmd += ["--allowedTools", "none"]
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
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Streams a Claude response for use inside mini-apps.

  Mini-apps pass their full conversation history in the system prompt
  and send the latest user message as the last item in messages.
  """
  return StreamingResponse(
    _stream(body.messages, body.system, body.tools),
    media_type="text/event-stream",
    headers={
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  )
