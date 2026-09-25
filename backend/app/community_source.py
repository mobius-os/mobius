"""Resolve a community Store package URL to the exact Git commit it serves.

A community install names a stable registry package URL,
``<registry>/v1/community/source/<app_id>/<current|rev_id>/mobius.json``. The
registry serves those bytes from a content-addressed mirror, but every revision
it accepts is an exact commit in the publisher's GitHub repository. Store
updates are Git-only (``install.fetch_git_install_candidate``), so the
installer and updater ask the registry which commit the URL names and fetch
that commit from its real origin. The registry that serves the package bytes is
also trusted to name that origin; ``install._select_install_target`` treats it
like a canonical ``mobius-os`` catalog origin. The registry's ``repository_url``
check below is self-consistency, not independent proof.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.community_broker import (
  COMMUNITY_BASE_URL,
  COMMUNITY_PREFIX,
  CommunityBrokerError,
  community_broker,
)


_PUBLIC_ID = r"[A-Za-z0-9_:-]{8,200}"
_SOURCE_PATH = re.compile(
  rf"{COMMUNITY_PREFIX}/source/(app_{_PUBLIC_ID})/(current|rev_{_PUBLIC_ID})"
  r"(?:/mobius\.json)?/?"
)
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REPOSITORY = re.compile(
  r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}"
)


class CommunitySourceUnavailable(ValueError):
  """The registry could not name which Git commit a package URL serves."""


def community_package(url: str | None) -> tuple[str, str] | None:
  """Return ``(app_id, selector)`` for a registry package URL, else None.

  Only this instance's configured registry host qualifies: another host
  serving the same path shape proves nothing about a registry revision.
  """
  if not isinstance(url, str) or not url:
    return None
  parsed = urlparse(url)
  registry = urlparse(COMMUNITY_BASE_URL)
  if (
    parsed.scheme != "https"
    or parsed.username is not None
    or parsed.password is not None
    or parsed.netloc.lower() != registry.netloc.lower()
  ):
    return None
  match = _SOURCE_PATH.fullmatch(parsed.path)
  if match is None:
    return None
  return match.group(1), match.group(2)


async def resolve_git_source(url: str | None) -> tuple[str, str] | None:
  """Return ``(origin_url, commit)`` for a registry package URL.

  ``None`` means the URL is not a community package. A community package
  whose revision cannot be resolved raises ``CommunitySourceUnavailable`` so
  callers never mistake a registry outage for "no Git source".
  """
  package = community_package(url)
  if package is None:
    return None
  app_id, selector = package
  path = f"{COMMUNITY_PREFIX}/apps/{app_id}"
  if selector != "current":
    path += f"/revisions/{selector}"
  try:
    record, _status, _headers = await community_broker.request("GET", path)
  except CommunityBrokerError as exc:
    raise CommunitySourceUnavailable(
      f"community registry lookup failed: {exc.detail}",
    ) from exc
  if not isinstance(record, dict):
    raise CommunitySourceUnavailable("community registry record is invalid")
  revision = record.get(
    "latest_revision" if selector == "current" else "revision",
  )
  if not isinstance(revision, dict) or (
    selector != "current" and revision.get("id") != selector
  ):
    raise CommunitySourceUnavailable("community revision is unavailable")
  commit = str(revision.get("commit_sha") or "").lower()
  repository = str(record.get("repository") or "")
  if not _COMMIT.fullmatch(commit) or not _REPOSITORY.fullmatch(repository):
    raise CommunitySourceUnavailable("community revision has no Git source")
  repository_url = str(record.get("repository_url") or "").rstrip("/")
  if repository_url.lower() != f"https://github.com/{repository}".lower():
    raise CommunitySourceUnavailable("community repository is inconsistent")
  return f"https://github.com/{repository}.git", commit
