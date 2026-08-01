"""Turning captured command output into text safe to store and display.

Subprocess transcripts arrive dressed for a terminal. Colour codes, cursor
moves and stray control bytes are correct there and wrong everywhere else:
they survive JSON, reach the owner's screen as literal `[1;31m`, and disfigure
the diagnostic they were meant to highlight. Every place that turns captured
output into stored or rendered text goes through here, so the set of sequences
we know about is defined once.
"""

from __future__ import annotations

import re

_TERMINAL_NOISE = re.compile(
  r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC ... BEL/ST (titles, hyperlinks)
  r"|\x1b\[[0-9;?]*[ -/]*[@-~]"           # CSI (colour, cursor moves)
  r"|\x1b[@-Z\\-_]"                       # two-byte escapes
  r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"    # other control bytes
)

_BLANK_RUN = re.compile(r"\n{3,}")

DEFAULT_LIMIT = 2000


def strip_terminal_noise(raw: str) -> str:
  """Remove escape sequences and control bytes, keeping tabs and newlines."""
  return _TERMINAL_NOISE.sub("", str(raw or ""))


def readable_output(raw: str, *, limit: int = DEFAULT_LIMIT) -> str:
  """Sanitize, tidy and cap a captured transcript.

  Keeps the TAIL when it overflows: a failing command states its reason last,
  so truncating from the front preserves the banner and discards the answer.
  """
  lines = strip_terminal_noise(raw).splitlines()
  cleaned = _BLANK_RUN.sub("\n\n", "\n".join(line.rstrip() for line in lines)).strip("\n")
  if len(cleaned) <= limit:
    return cleaned
  return "…\n" + cleaned[-limit:].split("\n", 1)[-1]
