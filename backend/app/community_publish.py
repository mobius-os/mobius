"""Build a bounded public snapshot from an app's accepted Git commit."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app import app_git, models
from app.config import get_settings
from app.manifest_contract import (
  PACKAGE_MAX_BYTES,
  ManifestContractError,
  package_bytes,
  package_limit_message,
  validate_manifest_contract,
)
from app.storage_io import atomic_write


MAX_SOURCE_FILES = 250
MAX_PATH_BYTES = 512
MAX_JOURNAL_BYTES = 16 * 1024
MAX_STORE_SCREENSHOTS = 5
_OID = re.compile(r"^[0-9a-f]{40,64}$")
_SOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9._@+ -]+$")
_SENSITIVE_DIRS = {
  ".git", ".github", ".env", "node_modules", "credentials", "secrets",
}
_SENSITIVE_FILES = {
  ".netrc", ".npmrc", ".pypirc",
  "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
  "credentials.json", "service-account.json",
}
_SECRET_PATTERNS = (
  re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"),
  re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
  re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_JOURNAL_STATES = {"listing_pending", "failed", "live"}
_LOCAL_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_REPOSITORY = re.compile(
  r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$",
)
_REPOSITORY_NAME = re.compile(r"^[a-z0-9_.-]{1,100}$")
_ADMISSION_CODE = re.compile(r"^[A-Za-z0-9_.:-]{0,128}$")


@dataclass(frozen=True)
class CommunityPublicationError(Exception):
  detail: str
  code: str = "invalid_publication"
  status_code: int = 400


@dataclass(frozen=True)
class _PublicTreeEntry:
  path: str
  mode: str
  oid: str


@dataclass
class CommunityPublicationJournal:
  """Public-source coordinates and the resumable Host admission outcome."""

  id: str
  local_app_id: str
  accepted_commit: str
  repository_name: str
  repository: str
  source_commit_sha: str
  admission_commit_sha: str
  state: str
  admission_code: str
  admission_message: str
  admission_status_code: int | None
  admission_retryable: bool
  created_at: str
  updated_at: str


def _journal_root() -> Path:
  return Path(get_settings().data_dir) / "community-publications"


def _validated_journal_root() -> Path:
  root = _journal_root()
  if root.is_symlink() or root.exists() and not root.is_dir():
    raise CommunityPublicationError(
      "The saved publication state is invalid.",
      "publication_journal_invalid",
      409,
    )
  return root


def _journal_id(local_app_id: str) -> str:
  return hashlib.sha256(local_app_id.encode("utf-8")).hexdigest()


def _journal_path(local_app_id: str) -> Path:
  return _validated_journal_root() / f"{_journal_id(local_app_id)}.json"


def _validate_journal(journal: CommunityPublicationJournal) -> None:
  valid_status = (
    journal.admission_status_code is None
    or isinstance(journal.admission_status_code, int)
    and 100 <= journal.admission_status_code <= 599
  )
  valid_time = True
  for value in (journal.created_at, journal.updated_at):
    try:
      parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
      valid_time = False
      break
    if parsed.tzinfo is None or not 1 <= len(value) <= 64:
      valid_time = False
      break
  if (
    journal.id != _journal_id(journal.local_app_id)
    or not _LOCAL_APP_ID.fullmatch(journal.local_app_id)
    or not _OID.fullmatch(journal.accepted_commit)
    or not _REPOSITORY_NAME.fullmatch(journal.repository_name)
    or not _REPOSITORY.fullmatch(journal.repository)
    or not re.fullmatch(r"[0-9a-f]{40}", journal.source_commit_sha)
    or journal.admission_commit_sha != ""
    and not re.fullmatch(r"[0-9a-f]{40}", journal.admission_commit_sha)
    or journal.state not in _JOURNAL_STATES
    or not _ADMISSION_CODE.fullmatch(journal.admission_code)
    or len(journal.admission_message) > 400
    or not valid_status
    or not isinstance(journal.admission_retryable, bool)
    or not valid_time
  ):
    raise CommunityPublicationError(
      "The saved publication state is invalid.",
      "publication_journal_invalid",
      409,
    )


def _decode_journal(path: Path) -> CommunityPublicationJournal:
  try:
    if path.is_symlink() or path.stat().st_size > MAX_JOURNAL_BYTES:
      raise ValueError("unsafe journal path")
    payload = json.loads(path.read_text(encoding="utf-8"))
    journal = CommunityPublicationJournal(
      id=str(payload["id"]),
      local_app_id=str(payload["local_app_id"]),
      accepted_commit=str(payload["accepted_commit"]),
      repository_name=str(payload["repository_name"]),
      repository=str(payload["repository"]),
      source_commit_sha=str(payload["source_commit_sha"]),
      admission_commit_sha=str(payload.get("admission_commit_sha") or ""),
      state=str(payload["state"]),
      admission_code=str(payload.get("admission_code") or ""),
      admission_message=str(payload.get("admission_message") or ""),
      admission_status_code=payload.get("admission_status_code"),
      admission_retryable=payload.get("admission_retryable"),
      created_at=str(payload["created_at"]),
      updated_at=str(payload["updated_at"]),
    )
    _validate_journal(journal)
    if path.name != f"{journal.id}.json":
      raise ValueError("journal identity mismatch")
    return journal
  except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise CommunityPublicationError(
      "The saved publication state is invalid.",
      "publication_journal_invalid",
      409,
    ) from exc


def read_publication_journal(
  local_app_id: str,
) -> CommunityPublicationJournal | None:
  path = _journal_path(local_app_id)
  if not path.exists():
    return None
  return _decode_journal(path)


def list_publication_journals() -> list[CommunityPublicationJournal]:
  root = _validated_journal_root()
  if not root.is_dir():
    return []
  paths = sorted(root.glob("*.json"))
  if len(paths) > 1000:
    raise CommunityPublicationError(
      "There are too many saved publication states.",
      "publication_journal_too_large",
      409,
    )
  return [_decode_journal(path) for path in paths]


def new_publication_journal(
  *,
  local_app_id: str,
  accepted_commit: str,
  repository_name: str,
  repository: str,
  source_commit_sha: str,
) -> CommunityPublicationJournal:
  now = datetime.now(UTC).isoformat()
  return CommunityPublicationJournal(
    id=_journal_id(local_app_id),
    local_app_id=local_app_id,
    accepted_commit=accepted_commit,
    repository_name=repository_name,
    repository=repository,
    source_commit_sha=source_commit_sha,
    admission_commit_sha="",
    state="listing_pending",
    admission_code="admission_pending",
    admission_message="The public source is waiting for Store admission.",
    admission_status_code=None,
    admission_retryable=True,
    created_at=now,
    updated_at=now,
  )


def write_publication_journal(journal: CommunityPublicationJournal) -> None:
  _validate_journal(journal)
  atomic_write(
    _journal_path(journal.local_app_id),
    json.dumps(journal.__dict__, ensure_ascii=False, sort_keys=True) + "\n",
  )


def delete_publication_journal(local_app_id: str) -> None:
  try:
    _journal_path(local_app_id).unlink()
  except FileNotFoundError:
    pass


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
  if (
    any(part in _SENSITIVE_DIRS for part in folded)
    or folded[-1] in _SENSITIVE_FILES
    or folded[-1].startswith(".env.")
  ):
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


LISTING_ITEMS = (
  ("details", "App details"),
  ("icon", "App icon"),
  ("tagline", "Tagline"),
  ("description", "Description"),
  ("screenshots", "Screenshots"),
  ("hero", "Banner image"),
)
_OPTIONAL_LISTING_ITEMS = {"hero"}
TAGLINE_MAX_BYTES = 120
DESCRIPTION_MAX_BYTES = 4000
SCREENSHOT_ALT_MAX_BYTES = 300
SCREENSHOT_LABEL_MAX_BYTES = 120


def _incomplete(message: str) -> CommunityPublicationError:
  return CommunityPublicationError(message, "listing_incomplete")


def _listing_manifest(by_path: dict[str, dict]) -> dict:
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
  if not manifest.get("id") or not manifest.get("entry"):
    raise CommunityPublicationError(
      "mobius.json is missing required publication fields.", "invalid_manifest",
    )
  return manifest


def _listing_text(store: dict, field: str, label: str, maximum: int) -> str:
  value = store.get(field)
  value = value.strip() if isinstance(value, str) else ""
  if not value or "\x00" in value:
    raise _incomplete(f"Add {label}.")
  if len(value.encode("utf-8")) > maximum:
    raise _incomplete(f"Shorten the {field} to {maximum} UTF-8 bytes or fewer.")
  return value


def _listing_asset(value: object, label: str, by_path: dict[str, dict]) -> str:
  source = value.strip() if isinstance(value, str) else ""
  if not source:
    raise _incomplete(f"Add the {label}.")
  try:
    _validate_path(source)
  except CommunityPublicationError as exc:
    raise _incomplete(f"The {label} has an unsupported file name.") from exc
  if source not in by_path:
    raise _incomplete(f"The {label} file is missing. Add it again.")
  if not source.startswith("static/store/"):
    raise _incomplete(
      f"The {label} must be saved under static/store/. Saving the listing "
      "again moves it there.",
    )
  return source


def _listing_screenshots(store: dict, by_path: dict[str, dict]) -> list[dict]:
  screenshots = store.get("screenshots")
  if not isinstance(screenshots, list) or not screenshots:
    raise _incomplete(f"Add 1–{MAX_STORE_SCREENSHOTS} screenshots.")
  if len(screenshots) > MAX_STORE_SCREENSHOTS:
    raise _incomplete(f"Keep at most {MAX_STORE_SCREENSHOTS} screenshots.")
  projected = []
  for index, item in enumerate(screenshots, start=1):
    if not isinstance(item, dict):
      raise _incomplete(f"Screenshot {index} is invalid. Add it again.")
    src = _listing_asset(item.get("src"), f"screenshot {index}", by_path)
    alt = item.get("alt")
    alt = alt.strip() if isinstance(alt, str) else ""
    if not alt or len(alt.encode("utf-8")) > SCREENSHOT_ALT_MAX_BYTES:
      raise _incomplete(
        f"Screenshot {index} needs a short description of what it shows.",
      )
    label = item.get("label")
    if label is not None and (
      not isinstance(label, str)
      or not label.strip()
      or len(label.encode("utf-8")) > SCREENSHOT_LABEL_MAX_BYTES
    ):
      raise _incomplete(f"Screenshot {index} has an invalid caption.")
    projected.append({
      "src": src,
      "alt": alt,
      **({"label": label.strip()} if isinstance(label, str) else {}),
    })
  return projected


def _listing_draft(manifest: dict, store: dict, by_path: dict[str, dict]) -> dict:
  """The listing as currently written, valid or not, for editing."""
  def text(field: str) -> str:
    value = store.get(field)
    return value if isinstance(value, str) else ""

  def tracked(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() in by_path else ""

  screenshots = []
  raw_shots = store.get("screenshots")
  for item in raw_shots if isinstance(raw_shots, list) else []:
    if not isinstance(item, dict):
      continue
    screenshots.append({
      "src": tracked(item.get("src")),
      "alt": item.get("alt") if isinstance(item.get("alt"), str) else "",
      "label": item.get("label") if isinstance(item.get("label"), str) else "",
    })
  return {
    "tagline": text("tagline"),
    "description": text("description"),
    "icon": tracked(manifest.get("icon")),
    "hero": tracked(store.get("hero")),
    "screenshots": screenshots,
  }


def store_listing_review(files: list[dict[str, str]]) -> dict:
  """Check every Store listing requirement of one accepted snapshot at once.

  The Store's checklist and the publish gate both read this review, so the
  checklist can never call a listing ready that publishing would refuse. Store
  artwork lives below ``static/store/`` so a commit-bound preview can read it
  directly without a copy step. It remains ordinary accepted source rather
  than ``static_assets`` package input; the manifest contract reserves this
  subtree so installer-owned runtime bytes cannot collide with it.
  """
  by_path = {
    str(item.get("path") or ""): item
    for item in files
    if isinstance(item, dict)
  }
  problems: dict[str, CommunityPublicationError] = {}
  results: dict[str, object] = {}

  def check(item: str, validate) -> None:
    try:
      results[item] = validate()
    except CommunityPublicationError as exc:
      problems[item] = exc

  check("details", lambda: _listing_manifest(by_path))
  manifest = results.get("details") if isinstance(results.get("details"), dict) else {}
  store = manifest.get("store") if isinstance(manifest.get("store"), dict) else {}

  def icon() -> str:
    source = str(manifest.get("icon") or "").strip()
    if not source or source not in by_path:
      raise _incomplete("Add an app icon.")
    return source

  check("icon", icon)
  check("tagline", lambda: _listing_text(
    store, "tagline", "a one-line tagline", TAGLINE_MAX_BYTES,
  ))
  check("description", lambda: _listing_text(
    store, "description", "a description", DESCRIPTION_MAX_BYTES,
  ))
  check("screenshots", lambda: _listing_screenshots(store, by_path))
  check("hero", lambda: (
    _listing_asset(store.get("hero"), "banner image", by_path)
    if store.get("hero") not in (None, "") else None
  ))

  checklist = [
    {
      "id": item,
      "label": label,
      "optional": item in _OPTIONAL_LISTING_ITEMS,
      "done": item not in problems,
      "code": problems[item].code if item in problems else "",
      "message": problems[item].detail if item in problems else "",
    }
    for item, label in LISTING_ITEMS
  ]
  listing = None
  if not problems:
    hero = results["hero"]
    listing = {
      "tagline": results["tagline"],
      "description": results["description"],
      "icon": results["icon"],
      "hero": {"path": hero} if hero else None,
      "screenshots": results["screenshots"],
    }
  draft = _listing_draft(manifest, store, by_path)
  return {
    "ready": listing is not None,
    "checklist": checklist,
    "listing": listing,
    "draft": draft,
  }


def public_store_listing(files: list[dict[str, str]]) -> dict:
  """Return the publishable listing, or raise its first unmet requirement."""
  review = store_listing_review(files)
  if review["listing"] is None:
    first = next(item for item in review["checklist"] if not item["done"])
    raise CommunityPublicationError(first["message"], first["code"])
  return review["listing"]


def listing_draft_assets(review: dict) -> set[str]:
  """Only artwork paths safe for the unauthenticated preview endpoint."""
  draft = review["draft"]
  # The accepted manifest's icon has always been public. An incomplete draft
  # must not make arbitrary other tracked source files public as artwork.
  paths = {draft["icon"]}
  paths.update(
    path for path in [draft["hero"], *(shot["src"] for shot in draft["screenshots"])]
    if path.startswith("static/store/")
  )
  paths.discard("")
  return paths


def _public_tree_entries(repo: Path, commit: str) -> list[_PublicTreeEntry]:
  listing = _git(repo, "ls-tree", "-r", "-z", commit, binary=True)
  assert isinstance(listing, bytes)
  raw_entries = [entry for entry in listing.split(b"\0") if entry]
  if not raw_entries or len(raw_entries) > MAX_SOURCE_FILES:
    raise CommunityPublicationError(
      f"A public app must contain 1–{MAX_SOURCE_FILES} files.",
      "payload_too_large",
      413,
    )

  entries: list[_PublicTreeEntry] = []
  seen: set[str] = set()
  for raw_entry in raw_entries:
    metadata, separator, raw_path = raw_entry.partition(b"\t")
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
    entries.append(_PublicTreeEntry(path=path, mode=mode, oid=oid))
  return entries


def read_public_store_asset(
  app: models.App, accepted_commit: str, asset_path: str,
) -> bytes:
  """Read one listing asset from the exact currently accepted Git tree."""
  repo = Path(app.source_dir)
  if not repo.is_dir() or not app_git.is_repo(repo):
    raise CommunityPublicationError(
      "This app does not have a versioned source tree to preview.",
      "source_unavailable",
      409,
    )
  commit = _accepted_commit(app, repo)
  if commit != accepted_commit.casefold():
    raise CommunityPublicationError(
      "The accepted app revision changed. Prepare the listing again.",
      "accepted_revision_changed",
      409,
    )
  _validate_path(asset_path)
  entries = _public_tree_entries(repo, commit)
  by_path = {entry.path: entry for entry in entries}
  manifest_entry = by_path.get("mobius.json")
  if manifest_entry is None:
    raise CommunityPublicationError(
      "The accepted revision must include mobius.json before it can be public.",
      "invalid_manifest",
    )
  manifest_content = _git(repo, "cat-file", "-p", manifest_entry.oid, binary=True)
  assert isinstance(manifest_content, bytes)
  listing_files = [
    {
      "path": entry.path,
      "content_base64": (
        base64.b64encode(manifest_content).decode("ascii")
        if entry.path == "mobius.json"
        else ""
      ),
    }
    for entry in entries
  ]
  allowed = listing_draft_assets(store_listing_review(listing_files))
  entry = by_path.get(asset_path)
  if asset_path not in allowed or entry is None:
    raise CommunityPublicationError(
      "That file is not part of the accepted Store listing.",
      "listing_asset_unavailable",
      404,
    )
  content = _git(repo, "cat-file", "-p", entry.oid, binary=True)
  assert isinstance(content, bytes)
  if len(content) > PACKAGE_MAX_BYTES:
    raise CommunityPublicationError(
      "The Store listing asset is too large.", "payload_too_large", 413,
    )
  _scan_content(asset_path, content)
  return content


def build_public_snapshot(
  app: models.App, *, allow_missing_manifest: bool = False,
) -> tuple[str, list[dict[str, str]]]:
  """Return the exact accepted commit and every regular tracked file.

  Listing previews may inspect legacy source without a manifest. Publication
  still requires a valid, installable manifest and the declared package budget.

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
  entries = _public_tree_entries(repo, commit)

  files: list[dict[str, str]] = []
  sizes: dict[str, int] = {}
  total = 0
  for entry in entries:
    content = _git(repo, "cat-file", "-p", entry.oid, binary=True)
    assert isinstance(content, bytes)
    sizes[entry.path] = len(content)
    total += len(content)
    if total > PACKAGE_MAX_BYTES:
      raise CommunityPublicationError(
        "The public app snapshot is larger than the "
        f"{PACKAGE_MAX_BYTES // (1024 * 1024)} MiB app package limit.",
        "payload_too_large",
        413,
      )
    _scan_content(entry.path, content)
    files.append({
      "path": entry.path,
      "mode": entry.mode,
      "content_base64": base64.b64encode(content).decode("ascii"),
    })

  manifest_item = next((item for item in files if item["path"] == "mobius.json"), None)
  if manifest_item is None and allow_missing_manifest:
    return commit, files
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
  try:
    validate_manifest_contract(manifest)
  except ManifestContractError as exc:
    raise CommunityPublicationError(str(exc), "invalid_manifest") from exc
  # Install charges each declaration in full, so bound the same declared sum:
  # every snapshot the Store accepts is then installable.
  declared = package_bytes(manifest, lambda rel: sizes.get(rel, 0))
  if declared > PACKAGE_MAX_BYTES:
    raise CommunityPublicationError(
      package_limit_message(declared), "payload_too_large", 413,
    )
  return commit, files
