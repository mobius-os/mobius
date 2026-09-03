"""Partner-actionable failures from reviewed contribution operations."""

from __future__ import annotations

import re

from app.terminal_output import readable_output

# The verdict line Möbius's own pre-push gate prints when it refuses a push.
# It is a declared contract between two Möbius-owned components — see the
# matching printf in scripts/githooks/pre-push — so this reads structure the
# hook already computed rather than re-deriving it from the prose above it.
_GATE_VERDICT = re.compile(
  r"^\[pre-push\] verdict=blocked cause=(\w+) checks=(\S*)\s*$",
  re.MULTILINE,
)
# Git puts ref races in this structured summary. Matching the whole line avoids
# mistaking the same words inside a failed pre-push test transcript for a race.
_MOVED_REMOTE_REF = re.compile(
  r"^\s*!\s+\[rejected\]\s+.+"
  r"\((?:non-fast-forward|fetch first|stale info)\)\s*$",
  re.IGNORECASE | re.MULTILINE,
)


class ContributionSubmitError(Exception):
  """A reviewed GitHub action failed without exposing unsafe raw state.

  ``message`` is the single owner-facing sentence. ``detail`` is optional
  diagnostic text for the disclosure beneath it — already sanitized, never a
  substitute for a real message, and never the thing shown first.
  """

  def __init__(
    self,
    message: str,
    status_code: int = 409,
    *,
    record_patch: dict | None = None,
    code: str | None = None,
    detail: str | None = None,
  ):
    super().__init__(message)
    self.message = message
    self.status_code = status_code
    self.record_patch = record_patch or {}
    self.code = code
    self.detail = detail or ""


def push_rejected(
  raw: str,
  *,
  record_patch: dict | None = None,
) -> ContributionSubmitError:
  """Explain a refused push in one sentence, with the transcript behind it.

  Git hands back whatever the transport and the local hooks printed. That is a
  developer's terminal artifact, not an owner-facing explanation, so it belongs
  in ``detail`` while ``message`` says who refused and what is true of the
  branch now — in every case, that nothing was published.
  """
  detail = readable_output(raw)
  verdict = _GATE_VERDICT.search(detail)

  code = None
  if verdict is not None and verdict.group(1) == "privacy":
    message = (
      "Möbius's privacy gate stopped this push: the branch still contains "
      "private workspace paths. Nothing was published."
    )
  elif verdict is not None:
    checks = verdict.group(2).replace(",", ", ")
    named = f" ({checks})" if checks else ""
    message = (
      f"This did not pass the checks Möbius runs before publishing{named}. "
      "Nothing was published — fix it locally, then send again."
    )
  elif _MOVED_REMOTE_REF.search(detail):
    message = (
      "GitHub's copy of this branch changed after it was reviewed. Nothing "
      "was pushed. Ask the agent to refresh and review it against the current "
      "branch."
    )
    code = "review_refresh_needed"
  else:
    message = "GitHub would not accept this branch, so nothing was published."

  return ContributionSubmitError(
    message,
    record_patch=record_patch,
    code=code,
    detail=detail,
  )
