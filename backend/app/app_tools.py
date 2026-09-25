"""Agent tools contributed by installed apps.

An app declares tools in its manifest (`tools`: name, description,
input_schema). Install review freezes them in the accepted capability contract
(`agent.tools`), and that contract, never editable source, is what agent runs
see. The Möbius control server lists the live apps' tools beside its own and
forwards each call here; the platform then calls the app's own reviewed
service at ``POST /tools/<name>``. There is no second way to run app code.

Every call carries the exact moment it happened: the chat, the physical run,
the provider, and the provider's own id for the model's tool call. A later
reviewer can fork the provider session right after that call
(``scripts/fork_chat.py --after-call``) and ask the agent about that moment.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import app_services, models
from app.app_capabilities import agent_tools_from_contract


log = logging.getLogger(__name__)

# Codex's default MCP tool timeout is 60 seconds; a longer platform limit would
# only let an app keep working after the model has been told the call failed.
TOOL_TIMEOUT_SECONDS = 55
# The key under which each provider sends its own id for the model's tool call
# in the MCP request's `_meta`. Claude: the `tool_use` id. Codex: the id of the
# model's tool-call response item. fork_session.py cuts a fork at these ids.
PROVIDER_CALL_ID_KEYS = {
  "claude": "claudecode/toolUseId",
  "codex": "itemId",
}


@dataclass(frozen=True)
class AppTool:
  app_id: int
  # Agent-facing name: `<app slug>_<tool name>`, unique across live apps.
  exposed_name: str
  name: str
  description: str
  input_schema: dict[str, Any]

  def listing(self) -> dict[str, Any]:
    return {
      "name": self.exposed_name,
      "description": self.description,
      "inputSchema": self.input_schema,
    }


def _exposed_name(slug: str, name: str) -> str:
  return f"{slug.replace('-', '_')}_{name}"


def live_app_tools(db: Session) -> list[AppTool]:
  """Reviewed tools of every live app with an accepted service, oldest first."""
  rows = (
    db.query(models.App)
    .filter(
      models.App.deleted_at.is_(None),
      models.App.capability_contract.isnot(None),
    )
    .order_by(models.App.id.asc())
    .all()
  )
  tools: list[AppTool] = []
  seen: set[str] = set()
  for app in rows:
    contract = app.capability_contract
    if not isinstance(contract, dict) or not isinstance(contract.get("service"), dict):
      continue
    for declaration in agent_tools_from_contract(contract):
      exposed = _exposed_name(app.slug, declaration["name"])
      if exposed in seen:
        # Two apps whose slugs differ only by `-`/`_`. The older install keeps
        # the name; the newer one's tool is unavailable until renamed.
        log.warning("duplicate app tool name %s from app id=%s", exposed, app.id)
        continue
      seen.add(exposed)
      tools.append(AppTool(
        app_id=app.id,
        exposed_name=exposed,
        name=declaration["name"],
        description=declaration["description"],
        input_schema=declaration["input_schema"],
      ))
  return tools


def call_moment(
  db: Session, *, chat_id: str, run_id: str, meta: Any,
) -> dict[str, Any]:
  """The exact point in one provider session at which a tool was called."""
  run = db.get(models.ChatRun, run_id)
  provider = run.provider if run is not None and run.chat_id == chat_id else None
  key = PROVIDER_CALL_ID_KEYS.get(provider or "")
  call_id = meta.get(key) if key and isinstance(meta, dict) else None
  return {
    "chat_id": chat_id,
    "run_id": run_id,
    "provider": provider,
    # None when the provider did not identify the call; the moment is then
    # still a faithful record, just not a fork point.
    "call_id": call_id if isinstance(call_id, str) and call_id else None,
  }


async def call_app_tool(
  db: Session,
  owner: models.Owner,
  *,
  exposed_name: str,
  arguments: dict[str, Any],
  moment: dict[str, Any],
) -> tuple[Any, bool]:
  """Call one live app tool through its service; return (result, is_error)."""
  tool = next(
    (item for item in live_app_tools(db) if item.exposed_name == exposed_name),
    None,
  )
  if tool is None:
    raise HTTPException(404, "App tool not found.")
  app = db.get(models.App, tool.app_id)
  status, body, _headers, media_type = await app_services.invoke_service(
    app,
    owner,
    {
      "schema": 1,
      "method": "POST",
      "path": f"/tools/{tool.name}",
      "query": {},
      "headers": {},
      "body": {"arguments": arguments, "call": moment},
      "public": False,
      # The platform authenticated the agent run; the run itself is in `call`.
      "actor": {"scope": "platform"},
    },
    timeout_seconds=TOOL_TIMEOUT_SECONDS,
  )
  if media_type is not None:
    return "App tool returned binary data.", True
  if status >= 400:
    detail = body.get("detail") if isinstance(body, dict) else body
    return detail or "App tool failed.", True
  return body, False
