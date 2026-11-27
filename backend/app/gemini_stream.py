"""Translate Gemini ``streamGenerateContent`` chunks into Responses events.

The Codex engine reads OpenAI Responses server-sent events. Each Gemini
chunk carries parts: visible text, thought summaries (``thought: true``) and
function calls, some with a ``thoughtSignature``. Text streams as it arrives;
each function call becomes one complete ``function_call`` item; thought
summaries and signatures close the turn as one reasoning item whose
``encrypted_content`` the engine replays to ``gemini_request``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.gemini_request import NAMESPACE_SEPARATOR, SIGNATURE_MARKER

_BLOCKED_FINISH = frozenset({
  "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY",
})


def sse_event(event: dict[str, Any]) -> bytes:
  return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class ResponsesStream:
  """One Gemini answer, re-emitted as one Responses stream."""

  def __init__(self, model: str, tool_names: dict[str, tuple[str | None, str]]):
    self.response_id = f"resp_{uuid.uuid4().hex}"
    self.model = model
    self.tool_names = tool_names
    self.output: list[dict[str, Any]] = []
    self.message: dict[str, Any] | None = None
    self.message_text = ""
    self.thoughts: list[str] = []
    self.signatures: dict[str, str] = {}
    self.usage: dict[str, Any] = {}
    self.failure: dict[str, str] | None = None

  def start(self) -> list[bytes]:
    return [sse_event({"type": "response.created", "response": self._response("in_progress")})]

  def feed(self, chunk: dict[str, Any]) -> list[bytes]:
    """Events for one decoded Gemini chunk."""
    if isinstance(chunk.get("error"), dict):
      self.failure = {"code": str(chunk["error"].get("status") or "api_error"),
                      "message": str(chunk["error"].get("message") or "Gemini error")}
      return []
    if chunk.get("usageMetadata"):
      self.usage = _usage(chunk["usageMetadata"])
    events: list[bytes] = []
    for candidate in chunk.get("candidates") or []:
      for part in (candidate.get("content") or {}).get("parts") or []:
        events.extend(self._part(part))
      self._note_finish(candidate.get("finishReason"))
    block = (chunk.get("promptFeedback") or {}).get("blockReason")
    if block:
      self.failure = {"code": "content_filter", "message": f"Gemini blocked the prompt ({block})."}
    return events

  def finish(self) -> list[bytes]:
    """Close open items and end the stream."""
    events = self._close_message()
    reasoning = self._reasoning_item()
    if reasoning:
      events.extend(self._add_done(reasoning))
    if self.failure and not self.output:
      failed = {**self._response("failed"), "error": self.failure}
      return events + [sse_event({"type": "response.failed", "response": failed})]
    done = {**self._response("completed"), "output": self.output, "usage": self.usage}
    return events + [sse_event({"type": "response.completed", "response": done})]

  def _part(self, part: dict[str, Any]) -> list[bytes]:
    if part.get("thought"):
      if isinstance(part.get("text"), str):
        self.thoughts.append(part["text"])
      return []
    if isinstance(part.get("functionCall"), dict):
      return self._close_message() + self._function_call(part)
    if isinstance(part.get("text"), str) and part["text"]:
      return self._text(part["text"])
    return []

  def _text(self, text: str) -> list[bytes]:
    events: list[bytes] = []
    if self.message is None:
      self.message = {"type": "message", "id": f"msg_{uuid.uuid4().hex}", "role": "assistant",
                      "status": "in_progress", "content": []}
      self.message_text = ""
      events.append(self._item_event("response.output_item.added", self.message, len(self.output)))
    self.message_text += text
    events.append(sse_event({
      "type": "response.output_text.delta", "item_id": self.message["id"],
      "output_index": len(self.output), "content_index": 0, "delta": text,
    }))
    return events

  def _close_message(self) -> list[bytes]:
    if self.message is None:
      return []
    item = {**self.message, "status": "completed",
            "content": [{"type": "output_text", "text": self.message_text, "annotations": []}]}
    self.message = None
    return self._done(item)

  def _function_call(self, part: dict[str, Any]) -> list[bytes]:
    call = part["functionCall"]
    call_id = f"call_{uuid.uuid4().hex}"
    if isinstance(part.get("thoughtSignature"), str):
      self.signatures[call_id] = part["thoughtSignature"]
    namespace, name = self._responses_name(str(call.get("name") or ""))
    item: dict[str, Any] = {
      "type": "function_call", "id": f"fc_{uuid.uuid4().hex}", "call_id": call_id,
      "name": name, "arguments": json.dumps(call.get("args") or {}), "status": "completed",
    }
    if namespace:
      item["namespace"] = namespace
    return self._add_done(item)

  def _responses_name(self, gemini_name: str) -> tuple[str | None, str]:
    if gemini_name in self.tool_names:
      return self.tool_names[gemini_name]
    namespace, _, name = gemini_name.rpartition(NAMESPACE_SEPARATOR)
    return (namespace or None, name) if namespace else (None, gemini_name)

  def _reasoning_item(self) -> dict[str, Any] | None:
    if not self.thoughts and not self.signatures:
      return None
    item: dict[str, Any] = {"type": "reasoning", "id": f"rs_{uuid.uuid4().hex}", "summary": []}
    if self.thoughts:
      item["summary"] = [{"type": "summary_text", "text": "".join(self.thoughts)}]
    if self.signatures:
      item["encrypted_content"] = json.dumps({SIGNATURE_MARKER: self.signatures})
    return item

  def _note_finish(self, reason: Any) -> None:
    if reason in _BLOCKED_FINISH:
      self.failure = {"code": "content_filter", "message": f"Gemini stopped the answer ({reason})."}

  def _add_done(self, item: dict[str, Any]) -> list[bytes]:
    added = self._item_event("response.output_item.added", item, len(self.output))
    return [added] + self._done(item)

  def _done(self, item: dict[str, Any]) -> list[bytes]:
    event = self._item_event("response.output_item.done", item, len(self.output))
    self.output.append(item)
    return [event]

  @staticmethod
  def _item_event(kind: str, item: dict[str, Any], index: int) -> bytes:
    return sse_event({"type": kind, "output_index": index, "item": item})

  def _response(self, status: str) -> dict[str, Any]:
    return {"id": self.response_id, "object": "response", "model": self.model,
            "status": status, "output": []}


def _usage(meta: dict[str, Any]) -> dict[str, Any]:
  prompt = int(meta.get("promptTokenCount") or 0)
  thoughts = int(meta.get("thoughtsTokenCount") or 0)
  output = int(meta.get("candidatesTokenCount") or 0) + thoughts
  return {
    "input_tokens": prompt,
    "input_tokens_details": {"cached_tokens": int(meta.get("cachedContentTokenCount") or 0)},
    "output_tokens": output,
    "output_tokens_details": {"reasoning_tokens": thoughts},
    "total_tokens": int(meta.get("totalTokenCount") or prompt + output),
  }
