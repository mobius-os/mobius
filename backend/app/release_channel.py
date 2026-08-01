"""Deployment-managed platform release-channel configuration.

Normal Möbius installations follow ``origin/main`` and retain the interactive
update/contribution flows.  A deployment may instead set
``MOBIUS_PLATFORM_RELEASE_REF`` to a full branch ref.  In that mode the image
fetches the configured branch but reconciles only to the immutable commit baked
into that image.  This prevents an older image from ingesting a newer branch
tip merely because the branch advanced between build and boot.

The baked identity deliberately comes from the image-owned build-info file,
not from a runtime environment variable that the deployment (or an agent) can
override.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


PLATFORM_RELEASE_REF_ENV = "MOBIUS_PLATFORM_RELEASE_REF"
BUILD_INFO_PATH = Path("/app/build-info.json")
MAIN_RELEASE_REF = "refs/heads/main"
DEFAULT_TARGET_REF = "origin/main"
UPDATE_DISABLED_REASON = (
  "Updates are managed by this deployment's release channel. Pull or "
  "redeploy the next image to upgrade."
)
CONTRIBUTION_DISABLED_REASON = (
  "Platform contributions are disabled while this deployment follows a "
  "non-main release channel. Redeploy on main before preparing or sending "
  "platform changes."
)

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_REF_CHARS = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")


class ReleaseChannelError(RuntimeError):
  """The deployment requested a release channel that cannot be trusted."""


@dataclass(frozen=True)
class PlatformReleaseChannel:
  """Resolved fetch and immutable-target contract for this process."""

  release_ref: str | None
  target_ref: str
  tracking_ref: str
  fetch_refspec: str | None
  updates_disabled: bool
  contributions_disabled: bool

  @property
  def configured(self) -> bool:
    return self.release_ref is not None


def release_ref_requested() -> bool:
  """Whether the deployment attempted to opt into a managed channel."""
  return bool((os.environ.get(PLATFORM_RELEASE_REF_ENV) or "").strip())


def _validated_release_ref(raw: str) -> str:
  ref = raw.strip()
  suffix = ref.removeprefix("refs/heads/")
  components = suffix.split("/")
  if (
    not _REF_CHARS.fullmatch(ref)
    or ref.endswith(("/", "."))
    or "//" in ref
    or ".." in ref
    or "@{" in ref
    or any(
      not part
      or part.startswith(".")
      or part.endswith(".lock")
      for part in components
    )
  ):
    raise ReleaseChannelError("platform_release_ref_invalid")
  return ref


def baked_build_sha(path: Path | None = None) -> str:
  """Read the immutable commit identity stamped into the image.

  ``path`` exists only as a unit-test seam.  Runtime callers use the fixed
  image-owned location and never honor ``BUILD_SHA`` or a path environment
  override.
  """
  build_info = BUILD_INFO_PATH if path is None else path
  try:
    payload = json.loads(build_info.read_text(encoding="utf-8"))
  except Exception as exc:
    raise ReleaseChannelError("platform_baked_build_sha_missing") from exc
  sha = str(payload.get("sha") or "").strip() if isinstance(payload, dict) else ""
  if not _FULL_SHA.fullmatch(sha):
    raise ReleaseChannelError("platform_baked_build_sha_invalid")
  return sha.lower()


def platform_release_channel() -> PlatformReleaseChannel:
  """Return the normal main contract or the deployment-managed exact target."""
  raw = (os.environ.get(PLATFORM_RELEASE_REF_ENV) or "").strip()
  if not raw:
    return PlatformReleaseChannel(
      release_ref=None,
      target_ref=DEFAULT_TARGET_REF,
      tracking_ref=DEFAULT_TARGET_REF,
      fetch_refspec=None,
      updates_disabled=False,
      contributions_disabled=False,
    )

  release_ref = _validated_release_ref(raw)
  suffix = release_ref.removeprefix("refs/heads/")
  tracking_ref = f"refs/remotes/origin/{suffix}"
  non_main = release_ref != MAIN_RELEASE_REF
  return PlatformReleaseChannel(
    release_ref=release_ref,
    target_ref=baked_build_sha(),
    tracking_ref=tracking_ref,
    fetch_refspec=f"+{release_ref}:{tracking_ref}",
    updates_disabled=non_main,
    contributions_disabled=non_main,
  )


def platform_contributions_disabled() -> bool:
  """Fail closed when a requested non-main channel is invalid or incomplete."""
  if not release_ref_requested():
    return False
  try:
    return platform_release_channel().contributions_disabled
  except ReleaseChannelError:
    return True
