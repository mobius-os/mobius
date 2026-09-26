"""Validated wire and repository primitives for contribution services."""

from __future__ import annotations

import re

GITHUB_REPO = re.compile(
  r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)
BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,160}$")
GIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
COAUTHOR_TRAILER = (
  "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>"
)


def coauthor_trailer_required(record: dict | None) -> bool:
  """Whether the reviewed commits must carry ``COAUTHOR_TRAILER``.

  Disclosure is the default. Only an explicit ``plan.coauthor_trailer: false``
  in the reviewed plan opts out, for a target project whose contribution policy
  forbids the trailer or at the owner's request.
  """
  plan = (record or {}).get("plan")
  return not (isinstance(plan, dict) and plan.get("coauthor_trailer") is False)


SUBMIT_TIMEOUT_SECONDS = 90
# A reviewed push runs the repository's mandatory pre-push gate before it can
# reach GitHub. Keep its deadline distinct from ordinary GitHub reads: the full
# frontend unit gate currently takes about two minutes on the live platform,
# while a stalled read should still fail fast enough to remain actionable.
PUSH_TIMEOUT_SECONDS = 300
