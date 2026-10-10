"""Build a bounded public snapshot from an app's accepted Git commit."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app import app_git, icon_assets, models
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
# Unicode UCD categories plus Default_Ignorable_Code_Point extras from
# DerivedCoreProperties.txt (Hangul/Khmer/Mongolian fillers and selectors),
# and U+2800 BRAILLE PATTERN BLANK; ASCII spaces are allowed inside names.
_UNSAFE_PATH_CATEGORIES = {"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp", "Zs"}
_INVISIBLE_PATH_EXTRAS = frozenset(
  "\u034f\u115f\u1160\u17b4\u17b5\u180b\u180c\u180d\u180f"
  "\u2800\u3164\uffa0"
)


def _is_unsafe_path_character(char: str) -> bool:
  return char != " " and (
    unicodedata.category(char) in _UNSAFE_PATH_CATEGORIES
    or char in _INVISIBLE_PATH_EXTRAS
    or "\ufe00" <= char <= "\ufe0f"
    or "\U000e0100" <= char <= "\U000e01ef"
  )


# Path segments refused anywhere in a published tree, each with the reason the
# author sees. Private folder names remain a guard beyond the content scan.
_UNPUBLISHED_SEGMENTS = {
  ".git": "Git's own metadata is never app source.",
  ".env": "environment files usually hold credentials.",
  "credentials": "private credential directories must not be published.",
  "secrets": "private secret directories must not be published.",
  ".github": (
    "Möbius does not publish .github files, because creating GitHub workflow "
    "files needs a broader GitHub token scope than publishing uses."
  ),
  "node_modules": (
    "installed apps use the platform's bundled libraries, so committed "
    "dependencies are never loaded and only add third-party code to the "
    "public repository."
  ),
}
_SENSITIVE_FILES = {
  ".netrc", ".npmrc", ".pypirc",
  "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
  "credentials.json", "service-account.json",
}
# Heuristics catch recognizable credentials, not every possible secret.
# Authors still need to review the accepted source before publication.
_SECRET_PATTERNS = (
  re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED |DSA )?PRIVATE KEY-----"),
  re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----"),
  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"),
  re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
  re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
  re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
  re.compile(r"\bsk-(?:ant-|proj-)[A-Za-z0-9_-]{20,}"),
  re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}\b"),
  re.compile(r"\bxox[abprs]-[0-9]{8,}-[0-9]{8,}-[A-Za-z0-9]{10,}\b"),
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


def accepted_history_contains(
  app: models.App, accepted_commit: str, commit: str,
) -> bool:
  """Whether ``commit`` is the accepted revision or one of its ancestors.

  An existing GitHub repository whose main branch is already part of this
  app's own source history (for example, the repository the owner first
  developed or installed it from) can be continued by a fast-forward commit
  without discarding anything on GitHub. An unknown object or unrelated
  history is not contained.
  """
  if not _OID.fullmatch(commit) or not _OID.fullmatch(accepted_commit):
    return False
  repo = Path(app.source_dir)
  if not repo.is_dir() or not app_git.is_repo(repo):
    return False
  result = subprocess.run(
    ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, accepted_commit],
    capture_output=True,
    timeout=30,
    check=False,
    env=app_git._git_env(repo),
  )
  return result.returncode == 0


def _validate_path(path: str) -> None:
  pure = PurePosixPath(path)
  parts = pure.parts
  if (
    not path
    or path.startswith("/")
    or "\\" in path
    or str(pure) != path
    or any(part in {"", ".", ".."} for part in parts)
    or any(part != part.strip(" ") for part in parts)
    or any(_is_unsafe_path_character(char) for char in path)
    or len(path.encode("utf-8")) > MAX_PATH_BYTES
  ):
    raise CommunityPublicationError(
      f"The path {path!r} cannot be published. Use a relative path of at most "
      f"{MAX_PATH_BYTES} bytes with '/' separators (no backslashes), no '.' "
      "or '..' segments, no leading or trailing spaces in a name, and no "
      "control, text-direction, invisible or blank-lookalike characters.",
      "invalid_path",
    )
  folded = [part.casefold() for part in parts]
  for part in folded:
    reason = _UNPUBLISHED_SEGMENTS.get(part)
    if reason is not None:
      raise CommunityPublicationError(
        f"Remove {path} before publishing: {reason}", "sensitive_path",
      )
  if folded[-1] in _SENSITIVE_FILES or folded[-1].startswith(".env."):
    raise CommunityPublicationError(
      f"Remove {path} before publishing: it is a common credential file name.",
      "sensitive_path",
    )


def _scan_content(path: str, content: bytes) -> None:
  # Replacement preserves ASCII token runs even in otherwise binary files.
  text = content.decode("utf-8", errors="replace")
  if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
    raise CommunityPublicationError(
      f"A likely credential was found in {path}. Remove it before publishing.",
      "secret_detected",
    )


def public_store_listing(files: list[dict[str, str]]) -> dict:
  """Validate and project storefront metadata from one accepted snapshot.

  Store-only artwork lives below ``static/store/`` so a commit-bound preview
  can read it directly without a copy step. It remains ordinary accepted source
  rather than ``static_assets`` package input; the manifest contract reserves
  this subtree so installer-owned runtime bytes cannot collide with it.
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
  }


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
        f"The path {raw_path.decode('utf-8', 'replace')!r} is not valid UTF-8 "
        "and cannot be published.",
        "invalid_path",
      ) from exc
    _validate_path(path)
    # Case-insensitive and macOS (decomposed Unicode) checkouts would merge
    # these names into one file.
    folded = unicodedata.normalize("NFC", path).casefold()
    if folded in seen:
      raise CommunityPublicationError(
        f"The path {path!r} collides with another file that differs only in "
        "letter case or Unicode form.",
        "invalid_path",
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
  listing = public_store_listing(listing_files)
  allowed = {str(listing["icon"])}
  hero = listing.get("hero")
  if isinstance(hero, dict):
    allowed.add(str(hero.get("path") or ""))
  allowed.update(
    str(item.get("src") or "")
    for item in listing.get("screenshots", [])
    if isinstance(item, dict)
  )
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
  icon = manifest.get("icon")
  icon_item = next((item for item in files if item["path"] == icon), None)
  if icon:
    if icon_item is None:
      raise CommunityPublicationError(
        f"The app icon {icon} is missing from the published snapshot.", "invalid_icon",
      )
    try:
      icon_assets.normalize_icon(base64.b64decode(icon_item["content_base64"]))
    except icon_assets.InvalidIcon as exc:
      raise CommunityPublicationError(
        f"The app icon {icon} cannot be used: {exc}", "invalid_icon",
      ) from exc
  # Install charges each declaration in full, so bound the same declared sum:
  # every snapshot the Store accepts is then installable.
  declared = package_bytes(manifest, lambda rel: sizes.get(rel, 0))
  if declared > PACKAGE_MAX_BYTES:
    raise CommunityPublicationError(
      package_limit_message(declared), "payload_too_large", 413,
    )
  return commit, files
