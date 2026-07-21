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

# Source metadata is inline on the SSE event and persisted tool block, unlike a
# large tool output which has a sidecar fetch path. Keep one search result small
# enough to remain cheap on low-resource self-hosted installs while leaving
# ample room for normal provider responses (which are usually single digits).
MAX_TOOL_SOURCES = 24
MAX_SOURCE_URL_CHARS = 2048
MAX_SOURCE_TITLE_CHARS = 300
MAX_SOURCE_SNIPPET_CHARS = 700
_MAX_SOURCE_NODES = 512
_MAX_SOURCE_DEPTH = 8
_MAX_SOURCE_TEXT_SCAN_CHARS = 256_000


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


def _first_string(
  data: dict[str, Any], keys: tuple[str, ...], limit: int,
) -> str:
  for key in keys:
    value = data.get(key)
    if isinstance(value, str):
      # Slice before trimming so malformed, multi-megabyte metadata cannot
      # allocate another full-size string merely to produce a short label.
      candidate = value[:limit].strip()
      if candidate:
        return candidate
  return ""


def _safe_http_url(value: Any) -> str:
  """Return a trimmed http(s) URL, or "" for anything else.

  A source feeds straight into an ``<a href>`` in the client, so a
  ``javascript:``/``data:`` URL would be a clickable XSS. Real web-search
  results are ordinary page URLs; this keeps the href safe regardless of what a
  provider payload happens to carry."""
  if not isinstance(value, str):
    return ""
  # Normal URLs may have a little surrounding whitespace. Reject a wildly
  # oversized value before strip() so validation itself stays cheap.
  if len(value) > MAX_SOURCE_URL_CHARS + 64:
    return ""
  candidate = value.strip()
  if not candidate:
    return ""
  # Never truncate a URL: that can silently change its destination. A result
  # above the browser-friendly ceiling is cheaper and safer to omit.
  if len(candidate) > MAX_SOURCE_URL_CHARS:
    return ""
  try:
    parsed = urlparse(candidate)
  except ValueError:
    return ""
  if parsed.scheme.lower() not in ("http", "https"):
    return ""
  # A scheme alone ("https://") is technically parseable but is not a usable
  # browser destination. Whitespace in a provider-supplied host is likewise
  # rejected by URL() in the client, so keep the two safety gates aligned.
  if not parsed.hostname or any(char.isspace() for char in candidate):
    return ""
  return candidate


def enrich_tool_source(
  existing: dict[str, str], incoming: dict[str, str],
) -> bool:
  """Backfill useful metadata without changing first-seen source order."""
  changed = False
  url = existing.get("url", "")
  current_title = existing.get("title", "")
  incoming_title = incoming.get("title", "")
  if ((not current_title or current_title == url)
      and incoming_title and incoming_title != incoming.get("url")):
    existing["title"] = incoming_title
    changed = True
  if not existing.get("snippet") and incoming.get("snippet"):
    existing["snippet"] = incoming["snippet"]
    changed = True
  return changed


def normalize_tool_sources(raw: Any) -> list[dict[str, str]]:
  """Normalize provider-specific source payloads to title/url/snippet.

  Claude returns server web-search results as a result block whose ``content``
  contains a provider-specific result list. The pinned Codex SDK exposes URLs
  for ``openPage`` / ``findInPage`` actions; optional result fields are handled
  defensively for SDK versions that expose them. This helper keeps both runners
  tolerant of those shapes without persisting raw SDK data.
  """
  sources: list[dict[str, str]] = []
  source_by_url: dict[str, dict[str, str]] = {}
  seen_containers: set[int] = set()
  stack: list[tuple[Any, int]] = [(raw, 0)]
  visited_nodes = 0

  # Iterative, depth-bounded traversal avoids recursion failures and makes the
  # maximum work independent of a provider's nesting or cyclic SDK objects.
  while stack and visited_nodes < _MAX_SOURCE_NODES:
    raw_value, depth = stack.pop()
    if not isinstance(raw_value, (str, bytes, int, float, bool, type(None))):
      identity = id(raw_value)
      if identity in seen_containers:
        continue
      seen_containers.add(identity)
    visited_nodes += 1
    try:
      value = _plain(raw_value)
    except Exception:
      # Source metadata is optional. A provider object that cannot serialize
      # must not fail the answer itself.
      continue
    if not value:
      continue
    if isinstance(value, list):
      if depth >= _MAX_SOURCE_DEPTH:
        continue
      remaining = _MAX_SOURCE_NODES - visited_nodes - len(stack)
      if remaining > 0:
        for item in reversed(value[:remaining]):
          stack.append((item, depth + 1))
      continue
    if not isinstance(value, dict):
      continue

    clean_url = _safe_http_url(value.get("url") or value.get("uri"))
    if clean_url:
      title = _first_string(
        value, ("title", "name"), MAX_SOURCE_TITLE_CHARS,
      ) or clean_url
      source: dict[str, str] = {"title": title, "url": clean_url}
      snippet = _first_string(
        value, _SNIPPET_KEYS, MAX_SOURCE_SNIPPET_CHARS,
      )
      if snippet:
        source["snippet"] = snippet
      existing = source_by_url.get(clean_url)
      if existing is not None:
        enrich_tool_source(existing, source)
      elif len(sources) < MAX_TOOL_SOURCES:
        sources.append(source)
        source_by_url[clean_url] = source

    if depth >= _MAX_SOURCE_DEPTH:
      continue
    for key in reversed(_SOURCE_LIST_KEYS):
      child = value.get(key)
      if (child is not None and child is not value
          and len(stack) < _MAX_SOURCE_NODES - visited_nodes):
        stack.append((child, depth + 1))

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
  marker_index = text.find(marker, 0, _MAX_SOURCE_TEXT_SCAN_CHARS)
  if marker_index < 0:
    return []

  array_index = marker_index + len("Links: ")
  try:
    parsed, _end = json.JSONDecoder().raw_decode(
      text[array_index:array_index + _MAX_SOURCE_TEXT_SCAN_CHARS],
    )
  except (TypeError, ValueError):
    return []

  if not isinstance(parsed, list):
    return []
  return normalize_tool_sources(parsed)
