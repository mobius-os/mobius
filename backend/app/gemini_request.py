"""Translate a Codex engine Responses request into a Gemini request.

The model relay serves app providers declared with protocol ``gemini``: the
Codex engine still speaks OpenAI Responses to the relay, and this module turns
that request into a ``streamGenerateContent`` body for Google's Gemini API.
``gemini_stream`` translates the answer back.

Gemini's thought signatures must return with the function calls they came
with. The stream translator stores them in a reasoning item's
``encrypted_content``, which the engine replays verbatim (``store`` is false),
so the relay keeps no state between requests.
"""

from __future__ import annotations

import json
from typing import Any

# Gemini function names allow dots, so a namespaced Responses tool travels as
# ``<namespace>.<name>`` and is split back by the stream translator.
NAMESPACE_SEPARATOR = "."
SIGNATURE_MARKER = "gemini_signatures"

# Responses effort vocabulary -> Gemini 3 thinking levels.
_THINKING_LEVELS = {
  "none": "minimal", "minimal": "minimal", "low": "low", "medium": "medium",
  "high": "high", "xhigh": "high", "max": "high", "ultra": "high",
}
_SYSTEM_ROLES = ("developer", "system")


def to_gemini_request(payload: dict[str, Any]) -> dict[str, Any]:
  """The ``streamGenerateContent`` body for one Responses request."""
  items = [item for item in payload.get("input") or [] if isinstance(item, dict)]
  system, conversation = _split_leading_system(items)
  body: dict[str, Any] = {"contents": _contents(conversation)}
  if system:
    body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
  declarations = function_declarations(payload.get("tools") or [])
  if declarations:
    body["tools"] = [{"functionDeclarations": declarations}]
  body["generationConfig"] = _generation_config(payload)
  return body


def qualified_tool_names(tools: list[Any]) -> dict[str, tuple[str | None, str]]:
  """Gemini function name -> (Responses namespace, Responses name)."""
  names: dict[str, tuple[str | None, str]] = {}
  for tool in tools:
    if not isinstance(tool, dict):
      continue
    if tool.get("type") == "function" and tool.get("name"):
      names[tool["name"]] = (None, tool["name"])
    elif tool.get("type") == "namespace" and tool.get("name"):
      for inner in tool.get("tools") or []:
        if isinstance(inner, dict) and inner.get("type") == "function" and inner.get("name"):
          names[_qualified(tool["name"], inner["name"])] = (tool["name"], inner["name"])
  return names


def function_declarations(tools: list[Any]) -> list[dict[str, Any]]:
  """Function tools as Gemini declarations; other tool kinds are not offered."""
  declarations = []
  for tool in tools:
    if not isinstance(tool, dict):
      continue
    if tool.get("type") == "function" and tool.get("name"):
      declarations.append(_declaration(tool["name"], tool))
    elif tool.get("type") == "namespace" and tool.get("name"):
      declarations.extend(
        _declaration(_qualified(tool["name"], inner["name"]), inner)
        for inner in tool.get("tools") or []
        if isinstance(inner, dict) and inner.get("type") == "function" and inner.get("name")
      )
  return declarations


def _qualified(namespace: str, name: str) -> str:
  return f"{namespace}{NAMESPACE_SEPARATOR}{name}"


def _declaration(name: str, tool: dict[str, Any]) -> dict[str, Any]:
  declaration: dict[str, Any] = {"name": name, "description": tool.get("description") or ""}
  if isinstance(tool.get("parameters"), dict):
    declaration["parametersJsonSchema"] = tool["parameters"]
  return declaration


def _generation_config(payload: dict[str, Any]) -> dict[str, Any]:
  thinking: dict[str, Any] = {"includeThoughts": True}
  effort = (payload.get("reasoning") or {}).get("effort")
  if effort in _THINKING_LEVELS:
    thinking["thinkingLevel"] = _THINKING_LEVELS[effort]
  config: dict[str, Any] = {"thinkingConfig": thinking}
  if isinstance(payload.get("max_output_tokens"), int):
    config["maxOutputTokens"] = payload["max_output_tokens"]
  return config


def _split_leading_system(items: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
  """Leading developer/system messages become Gemini's system instruction."""
  system: list[str] = []
  for index, item in enumerate(items):
    if item.get("type", "message") != "message" or item.get("role") not in _SYSTEM_ROLES:
      return system, items[index:]
    system.append(_plain_text(item.get("content")))
  return system, []


def _contents(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Gemini contents, merging adjacent items of one role into one turn."""
  call_names = {
    item.get("call_id"): _gemini_call_name(item)
    for item in items if item.get("type") == "function_call"
  }
  signatures = _replayed_signatures(items)
  contents: list[dict[str, Any]] = []
  for item in items:
    role, parts = _item_parts(item, call_names, signatures)
    if not parts:
      continue
    if contents and contents[-1]["role"] == role:
      contents[-1]["parts"].extend(parts)
    else:
      contents.append({"role": role, "parts": parts})
  return contents


def _item_parts(
  item: dict[str, Any], call_names: dict[Any, str], signatures: dict[str, str],
) -> tuple[str, list[dict[str, Any]]]:
  kind = item.get("type", "message")
  if kind == "function_call":
    return "model", [_function_call_part(item, signatures)]
  if kind == "function_call_output":
    name = call_names.get(item.get("call_id"), "unknown_function")
    return "user", [_function_response_part(name, item.get("output"))]
  if kind != "message":
    return "model", []
  if item.get("role") == "assistant":
    text = _plain_text(item.get("content"))
    return "model", [{"text": text}] if text else []
  return "user", _user_parts(item.get("content"))


def _gemini_call_name(item: dict[str, Any]) -> str:
  name = item.get("name") or "unknown_function"
  namespace = item.get("namespace")
  return _qualified(namespace, name) if namespace else name


def _function_call_part(item: dict[str, Any], signatures: dict[str, str]) -> dict[str, Any]:
  try:
    args = json.loads(item.get("arguments") or "{}")
  except json.JSONDecodeError:
    args = {}
  part: dict[str, Any] = {"functionCall": {
    "name": _gemini_call_name(item), "args": args if isinstance(args, dict) else {},
  }}
  signature = signatures.get(item.get("call_id"))
  if signature:
    part["thoughtSignature"] = signature
  return part


def _function_response_part(name: str, output: Any) -> dict[str, Any]:
  text = output if isinstance(output, str) else _plain_text(output)
  return {"functionResponse": {"name": name, "response": {"output": text}}}


def _user_parts(content: Any) -> list[dict[str, Any]]:
  if isinstance(content, str):
    return [{"text": content}] if content else []
  return [part for part in map(_user_part, content or []) if part]


def _user_part(block: Any) -> dict[str, Any] | None:
  if not isinstance(block, dict):
    return None
  if block.get("type") in ("input_text", "output_text", "text") and block.get("text"):
    return {"text": block["text"]}
  if block.get("type") == "input_image":
    return _image_part(block.get("image_url"))
  return None


def _image_part(url: Any) -> dict[str, Any] | None:
  """Inline data-URL images; remote image URLs are not fetched."""
  if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
    return None
  header, data = url[5:].split(";base64,", 1)
  return {"inlineData": {"mimeType": header or "image/png", "data": data}}


def _plain_text(content: Any) -> str:
  if isinstance(content, str):
    return content
  if not isinstance(content, list):
    return ""
  return "\n".join(
    block["text"] for block in content
    if isinstance(block, dict) and isinstance(block.get("text"), str)
  )


def _replayed_signatures(items: list[dict[str, Any]]) -> dict[str, str]:
  """Signatures stored by the stream translator in replayed reasoning items."""
  signatures: dict[str, str] = {}
  for item in items:
    if item.get("type") != "reasoning" or not isinstance(item.get("encrypted_content"), str):
      continue
    try:
      stored = json.loads(item["encrypted_content"]).get(SIGNATURE_MARKER)
    except (json.JSONDecodeError, AttributeError):
      continue
    if isinstance(stored, dict):
      signatures.update({k: v for k, v in stored.items() if isinstance(v, str)})
  return signatures
