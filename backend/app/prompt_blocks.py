"""Keep model-context data inside its platform-owned tagged carrier."""

from __future__ import annotations

import json
import re


def json_block_data(
  payload: object, *, ensure_ascii: bool = False, sort_keys: bool = False,
  separators: tuple[str, str] = (",", ":"),
) -> str:
  """Round-trip JSON without letting data forge markup or carrier boundaries.

  JSON quoting handles quotes and control characters, but leaves literal tags
  intact. JSON Unicode escapes keep <, > and & inert in the carrier while
  decoding to the original values (including keys), unlike HTML escaping.
  """
  return (
    json.dumps(payload, ensure_ascii=ensure_ascii, sort_keys=sort_keys, separators=separators)
    .replace("<", "\\u003c")
    .replace(">", "\\u003e")
    .replace("&", "\\u0026")
  )


def as_block_data(text: str, tag: str) -> str:
  """Neutralize ``<tag>`` and ``</tag>`` inside text recalled into that block.

  Recalled text — model output above all — could hold a literal closing tag
  that ends the block early and lets the rest read as prose outside it. The
  match tolerates case and stray whitespace (``</ Side_Questions >``) because
  a model reading the prompt would. Only the ``<`` of a match changes, so
  every other character — code, markup, markdown — reaches the agent intact.
  """
  pattern = re.compile(rf"<\s*/?\s*{re.escape(tag)}\b", re.IGNORECASE)
  return pattern.sub(lambda match: "&lt;" + match.group(0)[1:], text)
