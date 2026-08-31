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
SUBMIT_TIMEOUT_SECONDS = 90
