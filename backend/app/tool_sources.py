"""Helpers for normalizing provider web-search source payloads."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


_SOURCE_LIST_KEYS = (
  "content",
  "data",
  "items",
  "results",
  "sources",
)
_SNIPPET_KEYS = ("snippet", "text", "description")


def _plain(value: Any) -> Any:
  """Return a JSON-like value for SDK/dataclass/Pydantic objects."""
  if value is None:
    return None
  if hasattr(value, "model_dump"):
    return value.model_dump(by_alias=True, exclude_none=True, mode="json")
  if hasattr(value, "__dict__"):
    return {
      key: val for key, val in vars(value).items()
      if not key.startswith("_")
    }
  return value


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str:
  for key in keys:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
      return value.strip()
  return ""


def _safe_http_url(value: Any) -> str:
  """Return a trimmed http(s) URL, or "" for anything else.

  A source feeds straight into an ``<a href>`` in the client, so a
  ``javascript:``/``data:`` URL would be a clickable XSS. Real web-search
  results are ordinary page URLs; this keeps the href safe regardless of what a
  provider payload happens to carry."""
  if not isinstance(value, str) or not value.strip():
    return ""
  candidate = value.strip()
  try:
    scheme = urlparse(candidate).scheme.lower()
  except ValueError:
    return ""
  return candidate if scheme in ("http", "https") else ""


def normalize_tool_sources(raw: Any) -> list[dict[str, str]]:
  """Normalize provider-specific source payloads to title/url/snippet.

  Claude currently returns server web-search results as a result block
  whose ``content`` contains a provider-specific result list. Codex's
  typed item can grow similar fields independently. This helper keeps
  both runners tolerant of those shapes without persisting raw SDK data.
  """
  sources: list[dict[str, str]] = []
  seen_urls: set[str] = set()

  def visit(value: Any) -> None:
    value = _plain(value)
    if not value:
      return
    if isinstance(value, list):
      for item in value:
        visit(item)
      return
    if not isinstance(value, dict):
      return

    clean_url = _safe_http_url(value.get("url") or value.get("uri"))
    if clean_url:
      if clean_url not in seen_urls:
        title = _first_string(value, ("title", "name")) or clean_url
        source: dict[str, str] = {"title": title, "url": clean_url}
        snippet = _first_string(value, _SNIPPET_KEYS)
        if snippet:
          source["snippet"] = snippet
        sources.append(source)
        seen_urls.add(clean_url)

    for key in _SOURCE_LIST_KEYS:
      child = value.get(key)
      if child is not None and child is not value:
        visit(child)

  visit(raw)
  return sources


def sources_from_websearch_text(text: str) -> list[dict[str, str]]:
  """Extract normalized sources from Claude WebSearch result text.

  The CLI runs WebSearch as a client tool whose result is plain text
  with a JSON ``Links`` array followed by prose (claude-agent-sdk 0.2.x
  never parses it into ServerToolResultBlock). Decode only that array
  and let ``normalize_tool_sources`` enforce URL safety and dedupe
  before anything reaches the client.
  """
  if not isinstance(text, str):
    return []

  marker = "Links: ["
  marker_index = text.find(marker)
  if marker_index < 0:
    return []

  array_index = marker_index + len("Links: ")
  try:
    parsed, _end = json.JSONDecoder().raw_decode(text[array_index:])
  except (TypeError, ValueError):
    return []

  if not isinstance(parsed, list):
    return []
  return normalize_tool_sources(parsed)
