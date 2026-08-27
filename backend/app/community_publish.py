"""Build a bounded public snapshot from an app's accepted Git commit."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app import app_git, models


MAX_SOURCE_FILES = 250
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_PATH_BYTES = 512
MAX_STORE_SCREENSHOTS = 5
_OID = re.compile(r"^[0-9a-f]{40,64}$")
_SOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9._@+ -]+$")
_SENSITIVE_DIRS = {
  ".git", ".github", ".env", "node_modules", "credentials", "secrets",
}
_SENSITIVE_FILES = {
  "id_rsa", "id_ed25519", "credentials.json", "service-account.json",
}
_SECRET_PATTERNS = (
  re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"),
  re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
  re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


@dataclass(frozen=True)
class CommunityPublicationError(Exception):
  detail: str
  code: str = "invalid_publication"
  status_code: int = 400


def _git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
  result = subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True,
    timeout=30,
    check=False,
    env=app_git._git_env(repo),
  )
  if result.returncode != 0:
    raise CommunityPublicationError(
      "Möbius could not read the accepted app revision.",
      "source_unavailable",
      409,
    )
  return result.stdout if binary else result.stdout.decode("utf-8", "strict").strip()


def _accepted_commit(app: models.App, repo: Path) -> str:
  candidate = str(app.source_commit or "").strip().lower()
  if candidate and _OID.fullmatch(candidate):
    resolved = str(_git(repo, "rev-parse", f"{candidate}^{{commit}}"))
  else:
    resolved = str(_git(repo, "rev-parse", f"{app_git.LOCAL_BRANCH}^{{commit}}"))
  if not _OID.fullmatch(resolved):
    raise CommunityPublicationError(
      "The app does not have an accepted source revision.",
      "source_unavailable",
      409,
    )
  return resolved


def _validate_path(path: str) -> None:
  pure = PurePosixPath(path)
  parts = pure.parts
  folded = [part.casefold() for part in parts]
  if (
    not path
    or path.startswith("/")
    or "\\" in path
    or len(path.encode("utf-8")) > MAX_PATH_BYTES
    or str(pure) != path
    or any(part in {"", ".", ".."} for part in parts)
    or any(not _SOURCE_SEGMENT.fullmatch(part) for part in parts)
  ):
    raise CommunityPublicationError(
      "The accepted app revision contains an unsupported path.", "invalid_path",
    )
  if any(part in _SENSITIVE_DIRS for part in folded) or folded[-1] in _SENSITIVE_FILES:
    raise CommunityPublicationError(
      f"Remove the sensitive path {path} before publishing.", "sensitive_path",
    )


def _scan_content(path: str, content: bytes) -> None:
  try:
    text = content.decode("utf-8")
  except UnicodeDecodeError:
    return
  if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
    raise CommunityPublicationError(
      f"A likely credential was found in {path}. Remove it before publishing.",
      "secret_detected",
    )


def public_store_listing(files: list[dict[str, str]]) -> dict:
  """Validate and project storefront metadata from one accepted snapshot.

  Store-only artwork lives below ``static/store/`` so the existing local asset
  route can preview it without a copy step. It remains ordinary source rather
  than ``static_assets`` package input, so installs do not fetch marketing
  media unless the app independently declares it as a runtime asset.
  """
  by_path = {
    str(item.get("path") or ""): item
    for item in files
    if isinstance(item, dict)
  }
  manifest_item = by_path.get("mobius.json")
  if manifest_item is None:
    raise CommunityPublicationError(
      "The accepted revision must include mobius.json before it can be public.",
      "invalid_manifest",
    )
  try:
    manifest = json.loads(base64.b64decode(manifest_item["content_base64"]))
  except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise CommunityPublicationError(
      "mobius.json is not valid JSON.", "invalid_manifest",
    ) from exc
  if not isinstance(manifest, dict):
    raise CommunityPublicationError("mobius.json must be an object.", "invalid_manifest")

  store = manifest.get("store")
  if not isinstance(store, dict):
    raise CommunityPublicationError(
      "Add a Store listing with a tagline, description, and screenshots before publishing.",
      "listing_incomplete",
    )

  def clean_text(field: str, maximum: int) -> str:
    value = store.get(field)
    if not isinstance(value, str):
      raise CommunityPublicationError(
        f"The Store {field} is required.", "listing_incomplete",
      )
    value = value.strip()
    if not value or len(value.encode("utf-8")) > maximum or "\x00" in value:
      raise CommunityPublicationError(
        f"The Store {field} must be 1–{maximum} bytes.", "listing_incomplete",
      )
    return value

  icon = str(manifest.get("icon") or "").strip()
  if not icon or icon not in by_path:
    raise CommunityPublicationError(
      "Add a tracked app icon before publishing.", "listing_incomplete",
    )

  def listing_asset(value: object, label: str) -> str:
    if not isinstance(value, str):
      raise CommunityPublicationError(
        f"The Store {label} is required.", "listing_incomplete",
      )
    source = value.strip()
    _validate_path(source)
    if not source.startswith("static/store/") or source not in by_path:
      raise CommunityPublicationError(
        f"The Store {label} must be a tracked file under static/store/.",
        "listing_incomplete",
      )
    return source

  hero = None
  if store.get("hero") not in (None, ""):
    hero = listing_asset(store.get("hero"), "hero")
  screenshots = store.get("screenshots")
  if not isinstance(screenshots, list) or not 1 <= len(screenshots) <= MAX_STORE_SCREENSHOTS:
    raise CommunityPublicationError(
      f"Add 1–{MAX_STORE_SCREENSHOTS} Store screenshots before publishing.",
      "listing_incomplete",
    )
  projected = []
  for index, item in enumerate(screenshots, start=1):
    if not isinstance(item, dict):
      raise CommunityPublicationError(
        f"Store screenshot {index} is invalid.", "listing_incomplete",
      )
    alt = item.get("alt")
    if not isinstance(alt, str) or not alt.strip() or len(alt.encode("utf-8")) > 300:
      raise CommunityPublicationError(
        f"Store screenshot {index} needs concise alternative text.",
        "listing_incomplete",
      )
    label = item.get("label")
    if label is not None and (
      not isinstance(label, str)
      or not label.strip()
      or len(label.encode("utf-8")) > 120
    ):
      raise CommunityPublicationError(
        f"Store screenshot {index} has an invalid label.", "listing_incomplete",
      )
    projected.append({
      "src": listing_asset(item.get("src"), f"screenshot {index}"),
      "alt": alt.strip(),
      **({"label": label.strip()} if isinstance(label, str) else {}),
    })

  return {
    "tagline": clean_text("tagline", 120),
    "description": clean_text("description", 4000),
    "icon": icon,
    **({"hero": {"path": hero}} if hero else {"hero": None}),
    "screenshots": projected,
    "featured": store.get("featured") is True,
  }


def build_public_snapshot(app: models.App) -> tuple[str, list[dict[str, str]]]:
  """Return the exact accepted commit and every regular tracked file.

  Reading a Git tree rather than the editable worktree prevents an unsaved or
  concurrently-changing draft from crossing the public-source consent boundary.
  """
  repo = Path(app.source_dir)
  if not repo.is_dir() or not app_git.is_repo(repo):
    raise CommunityPublicationError(
      "This app does not have a versioned source tree to publish.",
      "source_unavailable",
      409,
    )
  commit = _accepted_commit(app, repo)
  listing = _git(repo, "ls-tree", "-r", "-z", commit, binary=True)
  assert isinstance(listing, bytes)
  entries = [entry for entry in listing.split(b"\0") if entry]
  if not entries or len(entries) > MAX_SOURCE_FILES:
    raise CommunityPublicationError(
      f"A public app must contain 1–{MAX_SOURCE_FILES} files.",
      "payload_too_large",
      413,
    )

  files: list[dict[str, str]] = []
  total = 0
  seen: set[str] = set()
  for entry in entries:
    metadata, separator, raw_path = entry.partition(b"\t")
    if not separator:
      raise CommunityPublicationError("The app source tree is invalid.")
    try:
      mode, object_type, oid = metadata.decode("ascii").split(" ", 2)
      path = raw_path.decode("utf-8", "strict")
    except (UnicodeDecodeError, ValueError) as exc:
      raise CommunityPublicationError(
        "The app source tree contains an unsupported path.", "invalid_path",
      ) from exc
    _validate_path(path)
    folded = path.casefold()
    if folded in seen:
      raise CommunityPublicationError(
        "The app contains paths that collide when published.", "invalid_path",
      )
    seen.add(folded)
    if object_type != "blob" or mode not in {"100644", "100755"} or not _OID.fullmatch(oid):
      raise CommunityPublicationError(
        f"{path} is not a regular publishable file.", "invalid_file_type",
      )
    content = _git(repo, "cat-file", "-p", oid, binary=True)
    assert isinstance(content, bytes)
    total += len(content)
    if total > MAX_SOURCE_BYTES:
      raise CommunityPublicationError(
        "The public app snapshot is larger than 64 MiB.",
        "payload_too_large",
        413,
      )
    _scan_content(path, content)
    files.append({
      "path": path,
      "mode": mode,
      "content_base64": base64.b64encode(content).decode("ascii"),
    })

  manifest_item = next((item for item in files if item["path"] == "mobius.json"), None)
  if manifest_item is None:
    raise CommunityPublicationError(
      "The accepted revision must include mobius.json before it can be public.",
      "invalid_manifest",
    )
  try:
    manifest = json.loads(base64.b64decode(manifest_item["content_base64"]))
  except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise CommunityPublicationError(
      "mobius.json is not valid JSON.", "invalid_manifest",
    ) from exc
  if not isinstance(manifest, dict) or not manifest.get("id") or not manifest.get("entry"):
    raise CommunityPublicationError(
      "mobius.json is missing required publication fields.", "invalid_manifest",
    )
  return commit, files
