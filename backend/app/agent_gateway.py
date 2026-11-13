"""The Agent Gateway: the only way a confined app agent reaches its AI model.

A confined agent (``agent_roles``) is pointed at Möbius instead of at its
provider and presents its agent key. For each request the gateway:

1. allows only the model endpoints of the run's provider;
2. removes every *provider-run* tool the app did not declare, wherever the
   request carries it (Codex nests tools inside ``input``), and server-side
   execution fields such as hosted MCP servers. Tools the agent runs itself
   are left alone: they run in the room, which limits them;
3. adds the owner's real login to the outgoing request only, and streams the
   reply back with a fixed short list of headers.

The login is never placed in a body, logged, or returned. The gateway is plain
code and does not interpret prompts.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent_roles import TOOLS, AgentKey

# The wire format each room provider speaks, and the endpoints it may use.
WIRE = {"claude": "anthropic", "codex": "responses", "mobius": "responses"}
ENDPOINTS = {
  "anthropic": frozenset({"v1/messages", "v1/messages/count_tokens"}),
  "responses": frozenset({"v1/responses", "v1/alpha/search"}),
}
WEB_ENDPOINTS = frozenset({"v1/alpha/search"})
_DROP_REQUEST_HEADERS = frozenset(
  {
    "host",
    "authorization",
    "x-api-key",
    "content-length",
    "connection",
    "accept-encoding",
    "cookie",
    "transfer-encoding",
    "chatgpt-account-id",
  }
)
REPLY_HEADERS = frozenset({"content-type", "request-id", "x-request-id", "retry-after"})
# Tool types the provider runs itself and that ``web`` unlocks.
_PROVIDER_WEB = ("web_search", "web_fetch")
# Tool types the agent runs itself, inside the room. Anything else is dropped.
_AGENT_RUN = frozenset(
  {"", "custom", "function", "namespace", "local_shell", "apply_patch", "shell"}
)
_AGENT_RUN_PREFIXES = ("bash_", "text_editor_", "memory_", "computer_")
# Request fields that make the provider run something; never forwarded.
_SERVER_FIELDS = ("mcp_servers", "container")


class GatewayError(Exception):
  def __init__(self, status: int, message: str):
    super().__init__(message)
    self.status, self.message = status, message


@dataclass(frozen=True)
class Upstream:
  url: str
  headers: dict[str, str]


def _web(key: AgentKey) -> bool:
  return any(TOOLS[name].provider_web for name in key.tools if name in TOOLS)


def endpoint(key: AgentKey, path: str) -> str:
  """The checked endpoint for this run's provider, or a refusal."""
  path = path.strip("/")
  wire = WIRE.get(key.provider)
  if wire is None or path not in ENDPOINTS[wire]:
    raise GatewayError(
      404, "Only this run's model requests are available through the gateway."
    )
  if path in WEB_ENDPOINTS and not _web(key):
    raise GatewayError(403, "Web access is not one of this app's agent tools.")
  return path


def _keep(tool: dict, web: bool) -> bool:
  kind = str(tool.get("type") or "")
  if kind.startswith(_PROVIDER_WEB):
    return web
  return kind in _AGENT_RUN or kind.startswith(_AGENT_RUN_PREFIXES)


def _filter_tools(tools: Any, web: bool, removed: list[str]) -> Any:
  if not isinstance(tools, list):
    return tools
  kept = []
  for tool in tools:
    if not isinstance(tool, dict):
      continue
    if not _keep(tool, web):
      removed.append(str(tool.get("type") or tool.get("name") or "?"))
      continue
    if isinstance(tool.get("tools"), list):  # a namespace of tools
      tool = {**tool, "tools": _filter_tools(tool["tools"], web, removed)}
    kept.append(tool)
  return kept


def prepare_body(key: AgentKey, raw: bytes) -> tuple[bytes, list[str]]:
  """The request body with undeclared provider-run tools removed."""
  try:
    body = json.loads(raw or b"{}")
  except ValueError as exc:
    raise GatewayError(400, "The request body is not JSON.") from exc
  if not isinstance(body, dict):
    raise GatewayError(400, "The request body must be a JSON object.")
  web = _web(key)
  removed = [field for field in _SERVER_FIELDS if body.pop(field, None) is not None]
  if "tools" in body:
    body["tools"] = _filter_tools(body["tools"], web, removed)
    forced = (
      body.get("tool_choice") if isinstance(body.get("tool_choice"), dict) else {}
    )
    if forced.get("name") and forced["name"] not in {
      t.get("name") for t in body["tools"]
    }:
      # A request that exists only to run a removed tool fails honestly
      # instead of letting the model invent the tool's result.
      raise GatewayError(403, f"{forced['name']} is not one of this app's agent tools.")
    if not body["tools"]:
      body.pop("tools")
      body.pop("tool_choice", None)
  for item in body.get("input") if isinstance(body.get("input"), list) else ():
    for field in ("tools", "additional_tools"):
      if isinstance(item, dict) and isinstance(item.get(field), list):
        item[field] = _filter_tools(item[field], web, removed)
  return json.dumps(body).encode(), removed


def _forwarded(incoming: dict[str, str]) -> dict[str, str]:
  return {k: v for k, v in incoming.items() if k.lower() not in _DROP_REQUEST_HEADERS}


def _jwt_expired(token: str) -> bool:
  try:
    part = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    return float(claims.get("exp", 0)) < time.time() + 30
  except (IndexError, ValueError):
    return False


async def upstream(
  key: AgentKey, path: str, query: str, headers: dict[str, str], *, data_dir: str
) -> Upstream:
  """Where this run's request goes, with the owner's login added."""
  out = _forwarded(headers)
  suffix = f"?{query}" if query else ""
  if key.provider == "claude":
    from app.providers import claude_access_token

    try:
      token = await claude_access_token(data_dir)
    except Exception as exc:  # noqa: BLE001 - reported to the agent as a clean error
      raise GatewayError(
        503, "Claude is not connected or its login has expired."
      ) from exc
    out["Authorization"] = f"Bearer {token}"
    beta = out.get("anthropic-beta", "")
    if "oauth-2025-04-20" not in beta:
      out["anthropic-beta"] = ",".join(x for x in (beta, "oauth-2025-04-20") if x)
    return Upstream(f"https://api.anthropic.com/{path}{suffix}", out)
  if key.provider == "mobius":
    from app.providers import get_provider

    # The local broker obtains its own one-use capabilities; the client key is
    # a protocol marker (providers.MobiusProvider.build_env).
    out["Authorization"] = "Bearer local-broker"
    base = get_provider("mobius").declaration["base_url"].rstrip("/")
    return Upstream(f"{base}/{path.removeprefix('v1/')}{suffix}", out)
  try:
    auth = json.loads((Path(data_dir) / "cli-auth" / "codex" / "auth.json").read_text())
  except (OSError, ValueError) as exc:
    raise GatewayError(503, "Codex is not connected.") from exc
  tokens = auth.get("tokens") if isinstance(auth, dict) else None
  if isinstance(tokens, dict) and tokens.get("access_token"):
    if _jwt_expired(tokens["access_token"]):
      raise GatewayError(
        401,
        "The Codex login has expired. Run any Codex chat to refresh it, "
        "or sign in again.",
      )
    out["Authorization"] = f"Bearer {tokens['access_token']}"
    if tokens.get("account_id"):
      out["chatgpt-account-id"] = tokens["account_id"]
    return Upstream(
      f"https://chatgpt.com/backend-api/codex/{path.removeprefix('v1/')}{suffix}", out
    )
  if (
    isinstance(auth, dict) and auth.get("OPENAI_API_KEY") and path not in WEB_ENDPOINTS
  ):
    out["Authorization"] = f"Bearer {auth['OPENAI_API_KEY']}"
    return Upstream(f"https://api.openai.com/{path}{suffix}", out)
  raise GatewayError(503, "Codex is not connected.")
