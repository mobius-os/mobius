"""Build a bounded public snapshot from an app's accepted Git commit."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import io
import json
import logging
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps, JpegImagePlugin, UnidentifiedImageError

from app import app_git, icon_assets, models
from app.config import get_settings
from app.manifest_contract import (
  MANIFEST_MAX_BYTES,
  MAX_STORE_SCREENSHOTS,
  PACKAGE_MAX_BYTES,
  STORE_LISTING_IMAGE_MAX_BYTES,
  ManifestContractError,
  package_bytes,
  package_limit_message,
  validate_manifest_contract,
)
from app.storage_io import atomic_write


log = logging.getLogger(__name__)

MAX_SOURCE_FILES = 250
MAX_PATH_BYTES = 512
MAX_JOURNAL_BYTES = 16 * 1024
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
# author sees. Credentials are found by file name and content below, not
# guessed from a folder name: apps legitimately ship folders such as secrets/
# (a secrets-manager UI) or credentials/ (setup docs).
_UNPUBLISHED_SEGMENTS = {
  ".git": "Git's own metadata is never app source.",
  ".env": "environment files usually hold credentials.",
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
# Environment files under any name, and key stores the content scan cannot
# read because they are binary (.jks, .keystore, .p12, .pfx) or have no
# PEM header (.ppk).
_SENSITIVE_SUFFIXES = (".env", ".jks", ".keystore", ".p12", ".pfx", ".ppk")
# Heuristics catch recognizable credentials, not every possible secret.
# Authors still need to review the accepted source before publication.
_SECRET_PATTERNS = (
  re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED |DSA )?PRIVATE KEY-----"),
  re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----"),
  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"),
  re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
  re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
  re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
  re.compile(r"\bGOCSPX-[0-9A-Za-z_-]{28}"),
  re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
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
  size: int


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
  name = folded[-1]
  if (
    name in _SENSITIVE_FILES
    or name.startswith(".env.")
    or name.endswith(_SENSITIVE_SUFFIXES)
  ):
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
LISTING_IMAGE_MAX_DIMENSION = 8192
# A multi-picture JPEG (MPO, such as an HDR phone photo) is a JPEG whose
# primary image is all a listing shows.
_LISTING_IMAGE_SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "MPO": ".jpg", "WEBP": ".webp"}
LISTING_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}


def check_listing_image(content: bytes, what: str) -> str:
  """Return the file suffix of a usable Store listing image, or raise why not.

  Readiness, the listing save and the public preview all apply this one rule,
  so an image the checklist accepts is one the Store can show.
  """
  if len(content) > STORE_LISTING_IMAGE_MAX_BYTES:
    raise CommunityPublicationError(
      f"The {what} must be an image of at most "
      f"{STORE_LISTING_IMAGE_MAX_BYTES // (1024 * 1024)} MB.",
      "listing_image_too_large", 422,
    )
  # The pixel cap is checked from the header before any decoding, against
  # the same process-wide limit icon_assets sets for Pillow; only then is
  # every pixel decoded, so truncated or corrupt data is caught here.
  too_many_pixels = CommunityPublicationError(
    f"The {what} has more than {icon_assets.MAX_IMAGE_PIXELS // 1_000_000} "
    "million pixels. Use a smaller image.",
    "listing_image_too_large", 422,
  )
  try:
    image = Image.open(io.BytesIO(content), formats=["PNG", "JPEG", "WEBP"])
    width, height = image.size
    if width * height > icon_assets.MAX_IMAGE_PIXELS:
      raise too_many_pixels
    suffix = _LISTING_IMAGE_SUFFIXES.get(str(image.format))
    if suffix is None:
      raise UnidentifiedImageError(image.format)
    image.verify()
    with Image.open(io.BytesIO(content), formats=["PNG", "JPEG", "WEBP"]) as decoded:
      decoded.load()
  except CommunityPublicationError:
    raise
  except Image.DecompressionBombError as exc:
    raise too_many_pixels from exc
  except UnidentifiedImageError as exc:
    raise CommunityPublicationError(
      f"Use a PNG, JPEG, or WebP file for the {what}.", "listing_image_invalid", 422,
    ) from exc
  except Exception as exc:
    raise CommunityPublicationError(
      f"The {what} is not a readable image.", "listing_image_invalid", 422,
    ) from exc
  if max(width, height) > LISTING_IMAGE_MAX_DIMENSION:
    raise CommunityPublicationError(
      f"The {what} is larger than {LISTING_IMAGE_MAX_DIMENSION} pixels.",
      "listing_image_too_large", 422,
    )
  return suffix


# Only chunks that shape how the image looks are kept; anything else (EXIF,
# XMP, text, provenance manifests, unknown or future chunks) is dropped.
# The colour profile (iCCP, and ICC data in JPEG and WebP) stays because
# dropping it changes the colours, so its free-text name, description and
# copyright fields are published as-is.
_PNG_KEPT_CHUNKS = {
  b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"iCCP",
  b"sBIT", b"sRGB", b"cICP", b"mDCV", b"cLLI", b"bKGD", b"pHYs",
  b"acTL", b"fcTL", b"fdAT",
}
# A bare last-modified timestamp (tIME) says nothing about who or where, and
# many editors add one to every PNG; stripping still drops it, but it alone
# does not hold a listing back. Text chunks always count, whatever their key:
# a "Software" value is free text, so it can hold a name or an address.
_PNG_HARMLESS_CHUNKS = _PNG_KEPT_CHUNKS | {b"tIME"}
_WEBP_KEPT_CHUNKS = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}
# Tables, frame headers, restart intervals and scans: what draws a JPEG.
_JPEG_DRAWING_MARKERS = {0xC4, 0xCC, 0xDA, 0xDB, 0xDD} | {
  marker for marker in range(0xC0, 0xD0) if marker not in {0xC4, 0xC8, 0xCC}
}
_EXIF_ORIENTATION = 0x0112


def _png_chunks_kept(content: bytes, kept: set[bytes] = _PNG_KEPT_CHUNKS) -> bytes:
  out, at = [content[:8]], 8
  while at + 12 <= len(content):
    length = int.from_bytes(content[at:at + 4], "big")
    kind = content[at + 4:at + 8]
    end = at + 12 + length
    if kind in kept:
      out.append(content[at:end])
    if kind == b"IEND":
      break  # Anything after the end of the image is not part of it.
    at = end
  return b"".join(out)


def _webp_chunks_kept(content: bytes) -> bytes:
  riff_end = min(len(content), 8 + int.from_bytes(content[4:8], "little"))
  chunks, at = [], 12
  while at + 8 <= riff_end:
    kind = content[at:at + 4]
    size = int.from_bytes(content[at + 4:at + 8], "little")
    end = at + 8 + size + (size & 1)
    chunk = content[at:end]
    if kind == b"VP8X" and len(chunk) > 8:
      # Clear the EXIF and XMP presence flags along with their chunks.
      chunk = chunk[:8] + bytes([chunk[8] & ~0x0C]) + chunk[9:]
    if kind in _WEBP_KEPT_CHUNKS:
      chunks.append(chunk)
    at = end
  body = b"WEBP" + b"".join(chunks)
  return b"RIFF" + len(body).to_bytes(4, "little") + body


def _jpeg_drawing_segment(marker: int, payload: bytes) -> bool:
  return (
    marker in _JPEG_DRAWING_MARKERS
    # JFIF without an embedded thumbnail, which could be a different picture.
    or (marker == 0xE0 and payload.startswith(b"JFIF\0") and payload[12:14] == b"\0\0")
    or (marker == 0xE2 and payload.startswith(b"ICC_PROFILE\0"))
    or (marker == 0xEE and payload.startswith(b"Adobe"))
  )


def _jpeg_has_metadata(content: bytes) -> bool:
  """Whether a JPEG holds any segment that does not draw it, or trailing bytes.

  An allow-list: EXIF, XMP, comments, C2PA and any unknown APPn segment
  count as metadata, as does anything after the end-of-image marker.
  """
  size, at = len(content), 2
  while at + 1 < size:
    if content[at] != 0xFF:
      return True
    marker = content[at + 1]
    if marker == 0xFF:  # Fill byte before a marker.
      at += 1
      continue
    if marker == 0xD9:
      return at + 2 != size
    length = int.from_bytes(content[at + 2:at + 4], "big")
    if not _jpeg_drawing_segment(marker, content[at + 4:at + 2 + length]):
      return True
    at += 2 + length
    if marker == 0xDA:
      # Entropy-coded data runs to the next marker; 0xFF is only ever
      # followed there by a stuffed zero or a restart marker.
      while (at := content.find(b"\xff", at)) != -1 and at + 1 < size and (
        content[at + 1] == 0 or 0xD0 <= content[at + 1] <= 0xD7
      ):
        at += 2
      if at == -1:
        return True
  return True  # No end-of-image marker.


def listing_image_has_metadata(content: bytes) -> bool:
  """Whether a PNG, JPEG or WebP file carries anything beyond how it looks.

  Decided from the bytes alone, without decoding, so it is cheap enough to
  run on every image in a source tree. Anything else returns False. A
  multi-picture JPEG's extra images count, through its MPF segment.
  """
  if content.startswith(b"\x89PNG\r\n\x1a\n"):
    return _png_chunks_kept(content, _PNG_HARMLESS_CHUNKS) != content
  if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
    return _webp_chunks_kept(content) != content
  if content.startswith(b"\xff\xd8\xff"):
    return _jpeg_has_metadata(content)
  return False


def strip_listing_image_metadata(content: bytes) -> bytes:
  """The image without hidden metadata; any failure is an invalid image."""
  try:
    return _stripped_image(content)
  except Exception as exc:
    raise CommunityPublicationError(
      "The image could not be read to remove its metadata.",
      "listing_image_invalid", 422,
    ) from exc


def _stripped_image(content: bytes) -> bytes:
  """The image as it looks, without camera, location, text or other metadata.

  PNG and WebP keep only the chunks that draw the image, losslessly. A JPEG
  has no such clean split (EXIF carries thumbnails, an MPO appends whole
  images), so it is re-encoded from its primary image with its own
  quantization tables, which keeps its quality. An image that relies on EXIF
  orientation is turned upright first, since that tag goes.
  """
  with Image.open(io.BytesIO(content)) as image:
    orientation = image.getexif().get(_EXIF_ORIENTATION, 1)
    image_format = image.format
    if orientation == 1 and image_format == "PNG":
      return _png_chunks_kept(content)
    if orientation == 1 and image_format == "WEBP":
      return _webp_chunks_kept(content)
    icc = image.info.get("icc_profile")
    lossless = bool(image.info.get("lossless"))
    progressive = bool(image.info.get("progressive"))
    jpeg = image_format in {"JPEG", "MPO"}
    options: dict = {"icc_profile": icc} if icc else {}
    if jpeg and orientation == 1:
      sampling = JpegImagePlugin.get_sampling(image)
      options.update(
        qtables=image.quantization, progressive=progressive,
        **({"subsampling": sampling} if sampling != -1 else {}),
      )
      clean = image.copy()
    else:
      clean = ImageOps.exif_transpose(image)
      options.update(
        {"quality": 95} if jpeg
        else {"quality": 95, "lossless": lossless} if image_format == "WEBP"
        else {},
      )
  # A bare copy carries no comment, XMP or EXIF for the encoder to re-save.
  clean.info = {}
  output = io.BytesIO()
  clean.save(output, format="JPEG" if jpeg else image_format, **options)
  stripped = output.getvalue()
  if image_format == "PNG":
    return _png_chunks_kept(stripped)
  if image_format == "WEBP":
    return _webp_chunks_kept(stripped)
  return stripped


def _file_bytes(item: dict) -> bytes:
  try:
    return base64.b64decode(item.get("content_base64") or "")
  except ValueError:
    return b""


def _incomplete(message: str) -> CommunityPublicationError:
  return CommunityPublicationError(message, "listing_incomplete")


def _parsed_manifest(by_path: dict[str, dict]) -> dict:
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
  return manifest


def _manifest_problems(
  manifest: dict, by_path: dict[str, dict],
) -> list[CommunityPublicationError]:
  """Everything publication refuses in a parsed manifest, in check order.

  Icon problems carry ``invalid_icon`` so the checklist can file them under
  the icon; every other one belongs to the app details.
  """
  if not manifest.get("id") or not manifest.get("entry"):
    return [CommunityPublicationError(
      "mobius.json is missing required publication fields.", "invalid_manifest",
    )]
  try:
    validate_manifest_contract(manifest)
  except ManifestContractError as exc:
    return [CommunityPublicationError(str(exc), "invalid_manifest")]
  problems = []
  icon = manifest.get("icon")
  if icon:
    icon_item = by_path.get(icon)
    if icon_item is None:
      problems.append(CommunityPublicationError(
        f"The app icon {icon} is missing from the published snapshot.", "invalid_icon",
      ))
    else:
      try:
        icon_assets.normalize_icon(_file_bytes(icon_item))
      except icon_assets.InvalidIcon as exc:
        problems.append(CommunityPublicationError(
          f"The app icon {icon} cannot be used: {exc}", "invalid_icon",
        ))
  # Install charges each declaration in full, so bound the same declared sum:
  # every snapshot the Store accepts is then installable.
  declared = package_bytes(
    manifest,
    lambda rel: len(_file_bytes(by_path[rel])) if rel in by_path else 0,
  )
  if declared > PACKAGE_MAX_BYTES:
    problems.append(CommunityPublicationError(
      package_limit_message(declared), "payload_too_large", 413,
    ))
  return problems


def _listing_text(store: dict, field: str, label: str, maximum: int) -> str:
  value = store.get(field)
  value = value.strip() if isinstance(value, str) else ""
  if not value or "\x00" in value:
    raise _incomplete(f"Add {label}.")
  if len(value.encode("utf-8")) > maximum:
    raise _incomplete(f"Shorten the {field} to {maximum} UTF-8 bytes or fewer.")
  return value


# Art the editor writes is named by its content; only those files are the
# listing's own. Other static/store/ files may also be app media.
EDITOR_ART = re.compile(r"static/store/[0-9a-f]{16}\.(?:png|jpg|webp)")


def _clean_listing_image(content: bytes, label: str, path: str, *, icon: bool = False) -> None:
  """Apply the image rule, then refuse hidden metadata publishing would expose."""
  check_listing_image(content, label)
  if not listing_image_has_metadata(content):
    return
  editor_art = (
    EDITOR_ART.fullmatch(path) is not None
    and PurePosixPath(path).stem == hashlib.sha256(content).hexdigest()[:16]
  )
  if editor_art or (icon and not path.startswith("static/store/")):
    # The save replaces the editor's own file, or cleans the icon in place.
    fix = "Saving the listing again removes it."
  else:
    fix = (
      f"Saving the listing writes a clean copy, but {path} stays published "
      "with the source until an agent removes it or saves it without the "
      "metadata."
    )
  raise CommunityPublicationError(
    f"The {label} carries camera, location or other hidden metadata that "
    f"publishing would make public. {fix}",
    "listing_image_metadata",
  )


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
      "again copies it there.",
    )
  _clean_listing_image(_file_bytes(by_path[source]), label, source)
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


def _published_image_flagged(content: bytes) -> bool:
  """Metadata check for one published file; an image it can't parse is flagged.

  One malformed image anywhere in the source must name itself on the
  checklist rather than take down the whole review.
  """
  try:
    return listing_image_has_metadata(content)
  except Exception:
    return True


def _published_images_with_metadata(
  by_path: dict[str, dict], listed: set[str],
) -> list[CommunityPublicationError]:
  """Every image in the snapshot, other than listed ones, still carrying metadata.

  Publishing ships the whole source, so an original photo stays public even
  after a save copies it into clean listing art, wherever it lives. Such
  files may be loaded by app code under their own names, so they are
  reported for an agent to clean or remove rather than deleted, all in one
  problem so they can be fixed in one pass. Detection reads bytes the
  snapshot already holds and decodes nothing.
  """
  paths = [
    path for path in sorted(by_path)
    if path not in listed  # Listed art and the icon have their own items.
    and _published_image_flagged(_file_bytes(by_path[path]))
  ]
  if not paths:
    return []
  named = ", ".join(paths[:5]) + (f", and {len(paths) - 5} more" if len(paths) > 5 else "")
  return [CommunityPublicationError(
    f"{len(paths)} image{'s' if len(paths) > 1 else ''} in the source "
    f"carr{'y' if len(paths) > 1 else 'ies'} camera, location or other hidden "
    f"metadata that publishing would make public, or can't be read: {named}. "
    "Ask an agent to remove them or save them without the metadata.",
    "listing_image_metadata",
  )]


def store_listing_review(
  files: list[dict[str, str]],
  source_problems: list[CommunityPublicationError] | tuple = (),
) -> dict:
  """Check every Store listing requirement of one accepted snapshot at once.

  The Store's checklist and the publish gate both read this review, so the
  checklist can never call a listing ready that publishing would refuse.
  ``source_problems`` are those ``read_public_snapshot`` found in the source
  itself; they and the manifest's problems fall under the details and icon
  items, so a broken manifest still leaves an editable draft. Store
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
    except Exception:
      # One unreadable item must not take down the whole checklist.
      log.exception("Checking the Store listing's %s failed", item)
      problems[item] = CommunityPublicationError(
        "This could not be checked. Add it again.", "listing_check_failed",
      )

  # A manifest missing publication fields still holds an editable listing;
  # only unreadable JSON leaves nothing to show.
  check("manifest", lambda: _parsed_manifest(by_path))
  manifest = results.get("manifest") or {}
  details = [problems.pop("manifest")] if "manifest" in problems else []
  manifest_problems = _manifest_problems(manifest, by_path) if not details else []
  details += source_problems
  details += [p for p in manifest_problems if p.code != "invalid_icon"]
  details += _published_images_with_metadata(by_path, _listing_paths(manifest))
  if details:
    problems["details"] = details[0]
  store = manifest.get("store") if isinstance(manifest.get("store"), dict) else {}

  def icon() -> str:
    for problem in manifest_problems:
      if problem.code == "invalid_icon":
        raise problem
    source = str(manifest.get("icon") or "").strip()
    if not source or source not in by_path:
      raise _incomplete("Add an app icon.")
    _clean_listing_image(_file_bytes(by_path[source]), "app icon", source, icon=True)
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


def listing_draft_assets(manifest: dict, by_path: dict) -> set[str]:
  """Only artwork paths safe for the unauthenticated preview endpoint."""
  store = manifest.get("store") if isinstance(manifest.get("store"), dict) else {}
  draft = _listing_draft(manifest, store, by_path)
  # The accepted manifest's icon has always been public. An incomplete draft
  # must not make arbitrary other tracked source files public as artwork.
  paths = {draft["icon"]}
  paths.update(
    path for path in [draft["hero"], *(shot["src"] for shot in draft["screenshots"])]
    if path.startswith("static/store/")
  )
  paths.discard("")
  return paths


def _public_tree_entries(
  repo: Path, commit: str,
) -> tuple[list[_PublicTreeEntry], list[CommunityPublicationError]]:
  """Every regular file of ``commit``, and each problem publishing it would hit.

  Problems are collected rather than raised so a listing preview can still
  read the manifest and artwork of a tree that is not yet publishable.
  """
  listing = _git(repo, "ls-tree", "-r", "-l", "-z", commit, binary=True)
  assert isinstance(listing, bytes)
  raw_entries = [entry for entry in listing.split(b"\0") if entry]
  problems: list[CommunityPublicationError] = []
  if not raw_entries or len(raw_entries) > MAX_SOURCE_FILES:
    problems.append(CommunityPublicationError(
      f"A public app must contain 1–{MAX_SOURCE_FILES} files.",
      "payload_too_large",
      413,
    ))

  entries: list[_PublicTreeEntry] = []
  seen: set[str] = set()
  for raw_entry in raw_entries:
    metadata, separator, raw_path = raw_entry.partition(b"\t")
    try:
      if not separator:
        raise ValueError(raw_entry)
      mode, object_type, oid, size = metadata.decode("ascii").split()
      path = raw_path.decode("utf-8", "strict")
    except (UnicodeDecodeError, ValueError):
      problems.append(CommunityPublicationError(
        f"The path {raw_path.decode('utf-8', 'replace')!r} is not valid UTF-8 "
        "and cannot be published.",
        "invalid_path",
      ))
      continue
    try:
      _validate_path(path)
    except CommunityPublicationError as exc:
      problems.append(exc)
    # Case-insensitive and macOS (decomposed Unicode) checkouts would merge
    # these names into one file.
    folded = unicodedata.normalize("NFC", path).casefold()
    if folded in seen:
      problems.append(CommunityPublicationError(
        f"The path {path!r} collides with another file that differs only in "
        "letter case or Unicode form.",
        "invalid_path",
      ))
    seen.add(folded)
    if object_type != "blob" or mode not in {"100644", "100755"} or not _OID.fullmatch(oid):
      problems.append(CommunityPublicationError(
        f"{path} is not a regular publishable file.", "invalid_file_type",
      ))
      continue
    entries.append(_PublicTreeEntry(path=path, mode=mode, oid=oid, size=int(size)))
  return entries, problems


def _blob(repo: Path, entry: _PublicTreeEntry) -> bytes:
  content = _git(repo, "cat-file", "-p", entry.oid, binary=True)
  assert isinstance(content, bytes)
  return content


def _tree_entry(repo: Path, commit: str, path: str) -> _PublicTreeEntry | None:
  """One regular file of ``commit``, looked up by path alone."""
  listing = _git(repo, "ls-tree", "-l", "-z", commit, "--", path, binary=True)
  assert isinstance(listing, bytes)
  for raw_entry in listing.split(b"\0"):
    metadata, _, raw_path = raw_entry.partition(b"\t")
    if raw_path != path.encode("utf-8"):
      continue
    mode, object_type, oid, size = metadata.decode("ascii").split()
    if object_type != "blob" or mode not in {"100644", "100755"}:
      return None
    return _PublicTreeEntry(path=path, mode=mode, oid=oid, size=int(size))
  return None


def read_accepted_file(app: models.App, path: str, *, max_bytes: int) -> bytes | None:
  """One regular file of the app's accepted revision, or None if it has none.

  The editable worktree can also hold ignored or generated files that were
  never accepted source, so anything that may become public reads from here.
  """
  repo = Path(app.source_dir)
  entry = _tree_entry(repo, _accepted_commit(app, repo), path)
  if entry is None:
    return None
  if entry.size > max_bytes:
    raise CommunityPublicationError(
      f"{path} is larger than {max_bytes} bytes.", "payload_too_large", 413,
    )
  return _blob(repo, entry)


def read_public_store_asset(
  app: models.App, accepted_commit: str, asset_path: str,
) -> tuple[bytes, str]:
  """Read one listing asset and prepare it for serving, with its media type."""
  return public_store_asset(read_public_store_asset_bytes(app, accepted_commit, asset_path))


def public_store_asset(content: bytes) -> tuple[bytes, str]:
  """Check and clean listing asset bytes already read; needs no source lock.

  An incomplete listing's draft is served too, so the editor can show it;
  its icon may name any tracked file, so only real listing images go out.
  The type comes from the image rule's verdict on the bytes, never the name,
  so a file that is also valid HTML cannot be served as a page.
  """
  try:
    suffix = check_listing_image(content, "listing image")
  except CommunityPublicationError as exc:
    raise CommunityPublicationError(
      "That file is not a Store listing image.", "listing_asset_unavailable", 404,
    ) from exc
  if listing_image_has_metadata(content):
    # Unauthenticated: an incomplete listing's draft art may still carry
    # location or camera data, which the preview must never hand out.
    try:
      content = strip_listing_image_metadata(content)
    except CommunityPublicationError as exc:
      raise CommunityPublicationError(
        "That file is not a Store listing image.", "listing_asset_unavailable", 404,
      ) from exc
  return content, LISTING_MEDIA_TYPES[suffix]


def read_public_store_asset_bytes(
  app: models.App, accepted_commit: str, asset_path: str,
) -> bytes:
  """Read one listing asset's raw bytes from the exact accepted Git tree."""
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
  # Two path lookups, not a listing of the whole tree under the source lock.
  manifest: object = {}
  manifest_entry = _tree_entry(repo, commit, "mobius.json")
  if manifest_entry is not None and manifest_entry.size <= MANIFEST_MAX_BYTES:
    try:
      manifest = json.loads(_blob(repo, manifest_entry))
    except ValueError:
      manifest = {}
  entry = _tree_entry(repo, commit, asset_path)
  if (
    entry is None or not isinstance(manifest, dict)
    or asset_path not in listing_draft_assets(manifest, {asset_path: entry})
    or entry.size > STORE_LISTING_IMAGE_MAX_BYTES
  ):
    raise CommunityPublicationError(
      "That file is not part of the accepted Store listing.",
      "listing_asset_unavailable",
      404,
    )
  content = _blob(repo, entry)
  _scan_content(asset_path, content)
  return content


def _listing_paths(manifest: object) -> set[str]:
  """Every file path the manifest names as its icon or listing artwork."""
  if not isinstance(manifest, dict):
    return set()
  store = manifest.get("store") if isinstance(manifest.get("store"), dict) else {}
  shots = store.get("screenshots") if isinstance(store.get("screenshots"), list) else []
  values = [manifest.get("icon"), store.get("hero")] + [
    shot.get("src") for shot in shots if isinstance(shot, dict)
  ]
  return {value.strip() for value in values if isinstance(value, str) and value.strip()}


def read_public_snapshot(
  app: models.App,
) -> tuple[str, list[dict[str, str]], list[CommunityPublicationError]]:
  """The accepted commit, its files, and each problem publishing them would hit.

  Only source that cannot be read at all raises; the listing preview reports
  everything else as checklist items. Over the package budget, only the
  manifest and listing artwork are read, which is all the review needs.

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
  entries, problems = _public_tree_entries(repo, commit)
  over_budget = sum(entry.size for entry in entries) > PACKAGE_MAX_BYTES
  if over_budget:
    problems.append(CommunityPublicationError(
      "The public app snapshot is larger than the "
      f"{PACKAGE_MAX_BYTES // (1024 * 1024)} MiB app package limit.",
      "payload_too_large",
      413,
    ))
  if over_budget or len(entries) > MAX_SOURCE_FILES:
    # Unpublishable as it stands: read no more than the review needs, so a
    # huge tree costs a handful of reads under the source lock, not one each.
    by_path = {entry.path: entry for entry in entries}
    manifest: object = {}
    manifest_entry = by_path.get("mobius.json")
    if manifest_entry is not None and manifest_entry.size <= MANIFEST_MAX_BYTES:
      try:
        manifest = json.loads(_blob(repo, manifest_entry))
      except ValueError:
        manifest = {}
    wanted = {"mobius.json"} | _listing_paths(manifest)
    entries = [
      entry for entry in entries
      if entry.path in wanted and entry.size <= STORE_LISTING_IMAGE_MAX_BYTES
    ]

  files: list[dict[str, str]] = []
  for entry in entries:
    content = _blob(repo, entry)
    try:
      _scan_content(entry.path, content)
    except CommunityPublicationError as exc:
      problems.append(exc)
    files.append({
      "path": entry.path,
      "mode": entry.mode,
      "content_base64": base64.b64encode(content).decode("ascii"),
    })
  return commit, files, problems


def build_public_snapshot(app: models.App) -> tuple[str, list[dict[str, str]]]:
  """Return the exact accepted commit and every regular tracked file.

  Raises the first problem publication would hit: the same ones the listing
  review reports, so a ready listing always passes here.
  """
  commit, files, problems = read_public_snapshot(app)
  by_path = {item["path"]: item for item in files}
  try:
    manifest = _parsed_manifest(by_path)
  except CommunityPublicationError as exc:
    problems.append(exc)
  else:
    problems.extend(_manifest_problems(manifest, by_path))
  if problems:
    raise problems[0]
  return commit, files
