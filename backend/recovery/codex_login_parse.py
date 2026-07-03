"""Pure parsers for the `codex login --device-auth` banner.

The Codex CLI prints a human-readable login banner to stdout: a
verification URL and a device code interleaved with prose and ANSI color
codes. There is no `--json` mode for device-auth, so both the normal
provider-auth flow (`routes/auth.py`) and the break-glass recovery flow
(`recover_oauth.py`) parse this free-form text. These two pure helpers are
the shared half — the readline-loop readiness predicate and the
URL/code extraction — so the two flows can't drift to different banner
expectations (the recovery copy is the one you least want to discover is
stale). The process lifecycle (spawn, registry, timeout cleanup) stays in
each caller, which legitimately differs.

The input is the trusted first-party `codex` binary's own output, not user
input, so the regexes carry no ReDoS/injection concern — they're simple
linear patterns over bounded text.
"""

import re

# A device code looks like XXXX-XXXX (groups of 4+ alphanumerics joined by a
# hyphen). Used both as the readline-loop readiness probe and the final
# extraction.
_CODE_RE = re.compile(r"[A-Z0-9]{4,}-[A-Z0-9]{4,}")

# Strips ANSI escape sequences (e.g. the CLI wraps the URL in \x1b[4m…\x1b[0m).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# A bare https URL — stops at whitespace or quoting/bracket characters.
_URL_RE = re.compile(r"https://[^\s<>\"']+")


def banner_has_code(output: str) -> bool:
  """Whether enough of the login banner has arrived to stop reading.

  The readline loop breaks once the banner mentions a "code" and a
  code-shaped token is present, so we don't block until EOF or the 15s
  timeout when the device code is already on screen.
  """
  return "code" in output.lower() and bool(_CODE_RE.search(output))


def parse_login_banner(output: str):
  """Extract the verification URL and device code from banner text.

  Returns a `{"url": ..., "code": ...}` dict, or `None` if either piece is
  missing (the caller maps that to a parse failure). Trailing sentence
  punctuation captured from prose (e.g. "Visit https://example.com.") is
  trimmed off the URL.
  """
  clean = _ANSI_RE.sub("", output)
  url_match = _URL_RE.search(clean)
  code_match = _CODE_RE.search(clean)
  if not url_match or not code_match:
    return None
  return {
    "url": url_match.group(0).rstrip(".,;:!?"),
    "code": code_match.group(0),
  }
