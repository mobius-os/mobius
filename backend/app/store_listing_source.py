"""Write an owner's Store listing edit into an app's editable source.

The listing is ordinary accepted source: text in ``mobius.json``'s ``store``
object, artwork under ``static/store/``. This module only changes the
worktree; the caller accepts the result through the normal apply path, and
uses the returned undo only if acceptance fails before Git advances. A
committed revision remains intact as the normal retry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from typing import Callable
import warnings

from PIL import Image

from app import icon_assets, models, workspace_files
from app.community_publish import (
  DESCRIPTION_MAX_BYTES,
  MAX_STORE_SCREENSHOTS,
  SCREENSHOT_ALT_MAX_BYTES,
  SCREENSHOT_LABEL_MAX_BYTES,
  TAGLINE_MAX_BYTES,
)


STORE_ART_DIR = "static/store"
MAX_LISTING_IMAGE_BYTES = 8 * 1024 * 1024
MAX_LISTING_IMAGE_DIMENSION = 8192
_IMAGE_SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_DETAILS_NEED_AGENT = (
  "This older app needs an agent to create its app details without "
  "changing its permissions, capabilities, or scheduled jobs."
)


class ListingEditError(ValueError):
  def __init__(self, code: str, message: str, status_code: int = 422):
    super().__init__(message)
    self.code = code
    self.status_code = status_code


@dataclass(frozen=True)
class ListingImage:
  """Either an existing tracked source file or newly uploaded bytes."""

  path: str = ""
  data: bytes | None = None


@dataclass(frozen=True)
class ListingScreenshot:
  image: ListingImage
  alt: str = ""
  label: str = ""


@dataclass(frozen=True)
class ListingEdit:
  tagline: str = ""
  description: str = ""
  screenshots: tuple[ListingScreenshot, ...] = ()
  hero: ListingImage | None = None
  icon: ListingImage | None = None


def record_icon_for_save(root: Path, app: models.App) -> bytes | None:
  """The icon on the app's record that saving a listing copies into source.

  An app from before manifest icons keeps its icon on its record only;
  accepting a revision without one would clear the icon the owner sees.
  """
  try:
    declared = json.loads((root / "mobius.json").read_text(encoding="utf-8")).get("icon")
  except (OSError, ValueError, AttributeError):
    declared = None
  if declared or (root / "icon.png").is_file():
    return None
  return app.icon_override_png or app.icon_png or None


def note_what_saving_fixes(checklist: list[dict], root: Path, app: models.App) -> None:
  """Mark the checklist items that saving the listing completes by itself.

  ``automatic`` is True when a save fixes the item, False when only an agent
  can; absent means the owner (or an agent) supplies it.
  """
  items = {item["id"]: item for item in checklist}
  details = items["details"]
  if not details["done"] and not (root / "mobius.json").is_file():
    details["automatic"] = False
    details["message"] = _DETAILS_NEED_AGENT
  icon = items["icon"]
  if not icon["done"] and record_icon_for_save(root, app) is not None:
    icon["automatic"] = True
    icon["message"] = "Saving the listing keeps the app's current icon."


@dataclass
class _SourceChanges:
  """Original bytes of every path this edit touched, for an exact undo."""

  root: Path
  originals: dict[str, bytes | None] = field(default_factory=dict)

  def _remember(self, rel: str) -> Path:
    target = _source_path(self.root, rel)
    if rel not in self.originals:
      self.originals[rel] = target.read_bytes() if target.is_file() else None
    return target

  def write(self, rel: str, content: bytes) -> None:
    target = self._remember(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

  def delete(self, rel: str) -> None:
    self._remember(rel).unlink(missing_ok=True)

  def undo(self) -> None:
    for rel, content in self.originals.items():
      target = _source_path(self.root, rel)
      if content is None:
        target.unlink(missing_ok=True)
      else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _bounded(value: str, maximum: int, what: str) -> str:
  value = value.strip()
  if "\x00" in value or len(value.encode("utf-8")) > maximum:
    raise ListingEditError(
      "listing_field_too_long", f"The {what} must be {maximum} UTF-8 bytes or fewer.",
    )
  return value


def _source_path(root: Path, rel: str) -> Path:
  pure = PurePosixPath(rel)
  if (
    not rel or rel.startswith("/") or "\\" in rel or str(pure) != rel
    or any(part in {"", ".", ".."} for part in pure.parts)
  ):
    raise ListingEditError("listing_image_invalid", "That image path is not allowed.")
  try:
    return workspace_files.resolve_path(root, rel, hidden_dirs=workspace_files.GIT_METADATA_NAMES)
  except (workspace_files.InvalidWorkspacePath, workspace_files.UnavailableWorkspacePath) as exc:
    raise ListingEditError("listing_image_invalid", "That image path is not allowed.") from exc


def _source_file(root: Path, rel: str) -> bytes:
  target = _source_path(root, rel)
  if not target.is_file():
    raise ListingEditError(
      "listing_image_missing", f"The image {rel} no longer exists. Add it again.",
    )
  return target.read_bytes()


def _image_suffix(content: bytes, what: str) -> str:
  if not content or len(content) > MAX_LISTING_IMAGE_BYTES:
    raise ListingEditError(
      "listing_image_too_large",
      f"The {what} must be an image of at most "
      f"{MAX_LISTING_IMAGE_BYTES // (1024 * 1024)} MB.",
    )
  try:
    with warnings.catch_warnings():
      warnings.simplefilter("error", Image.DecompressionBombWarning)
      image = Image.open(io.BytesIO(content))
      width, height = image.size
      image.verify()
  except Exception as exc:
    raise ListingEditError(
      "listing_image_invalid", f"The {what} is not a readable image.",
    ) from exc
  suffix = _IMAGE_SUFFIXES.get(str(image.format or ""))
  if suffix is None:
    raise ListingEditError(
      "listing_image_invalid", f"Use a PNG, JPEG, or WebP file for the {what}.",
    )
  if max(width, height) > MAX_LISTING_IMAGE_DIMENSION:
    raise ListingEditError(
      "listing_image_too_large", f"The {what} is larger than 8192 pixels.",
    )
  return suffix


def _place_art(root: Path, changes: _SourceChanges, image: ListingImage, what: str) -> str:
  """Return the ``static/store/`` path holding this image, writing it if new.

  New art is content-addressed, so re-saving the same bytes is a no-op and
  reordering screenshots never overwrites another one. A kept file outside
  ``static/store/`` (an older listing layout) moves into it.
  """
  if image.data is None and image.path.startswith(f"{STORE_ART_DIR}/"):
    content = _source_file(root, image.path)
    _image_suffix(content, what)
    return image.path
  content = image.data if image.data is not None else _source_file(root, image.path)
  suffix = _image_suffix(content, what)
  rel = f"{STORE_ART_DIR}/{hashlib.sha256(content).hexdigest()[:16]}{suffix}"
  target = _source_path(root, rel)
  if not (target.is_file() and target.read_bytes() == content):
    changes.write(rel, content)
  return rel


def _manifest_text(manifest: dict) -> str:
  return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def _referenced_art(store: object) -> set[str]:
  if not isinstance(store, dict):
    return set()
  shots = store.get("screenshots")
  paths = [store.get("hero")] + [
    item.get("src") for item in (shots if isinstance(shots, list) else [])
    if isinstance(item, dict)
  ]
  return {
    path for path in paths
    if isinstance(path, str) and path.startswith(f"{STORE_ART_DIR}/")
  }


def write_listing(root: Path, app: models.App, edit: ListingEdit) -> Callable[[], None]:
  """Write ``edit`` into the app source at ``root`` and return its undo."""
  if len(edit.screenshots) > MAX_STORE_SCREENSHOTS:
    raise ListingEditError(
      "listing_too_many_screenshots",
      f"Keep at most {MAX_STORE_SCREENSHOTS} screenshots.",
    )
  tagline = _bounded(edit.tagline, TAGLINE_MAX_BYTES, "tagline")
  description = _bounded(edit.description, DESCRIPTION_MAX_BYTES, "description")
  changes = _SourceChanges(root)
  try:
    manifest_path = _source_path(root, "mobius.json")
    if manifest_path.is_file():
      try:
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
      except json.JSONDecodeError as exc:
        raise ListingEditError(
          "manifest_invalid", "mobius.json is not valid JSON. Ask an agent to repair it.",
        ) from exc
      if not isinstance(current, dict):
        raise ListingEditError("manifest_invalid", "mobius.json must be an object.")
    else:
      raise ListingEditError("details_need_agent", _DETAILS_NEED_AGENT, 409)

    declared_icon = str(current.get("icon") or "").strip()
    icon_rel = declared_icon or "icon.png"
    icon_bytes = None
    if edit.icon is not None:
      icon_bytes = (
        edit.icon.data if edit.icon.data is not None
        else _source_file(root, edit.icon.path)
      )
      # Normalized icons are PNG; never write PNG bytes under another suffix.
      if not icon_rel.lower().endswith(".png"):
        icon_rel = "icon.png"
    else:
      icon_bytes = record_icon_for_save(root, app)
    if icon_bytes is not None:
      try:
        changes.write(icon_rel, icon_assets.normalize_icon(icon_bytes))
      except icon_assets.InvalidIcon as exc:
        raise ListingEditError("listing_image_invalid", f"App icon: {exc}") from exc
    has_icon = _source_path(root, icon_rel).is_file()

    screenshots = []
    for index, shot in enumerate(edit.screenshots, start=1):
      entry = {
        "src": _place_art(root, changes, shot.image, f"screenshot {index}"),
        "alt": _bounded(shot.alt, SCREENSHOT_ALT_MAX_BYTES, f"screenshot {index} description"),
      }
      label = _bounded(shot.label, SCREENSHOT_LABEL_MAX_BYTES, f"screenshot {index} caption")
      if label:
        entry["label"] = label
      screenshots.append(entry)
    # Fields the editor does not manage (a curator's flag, a future field)
    # stay exactly as they were; assigning existing keys keeps their order.
    previous = current.get("store")
    store: dict[str, object] = dict(previous) if isinstance(previous, dict) else {}
    store["tagline"] = tagline
    store["description"] = description
    if edit.hero is not None:
      store["hero"] = _place_art(root, changes, edit.hero, "banner image")
    else:
      store.pop("hero", None)
    store["screenshots"] = screenshots

    previous_art = _referenced_art(current.get("store"))
    manifest = dict(current)
    if has_icon:
      manifest["icon"] = icon_rel
    manifest["store"] = store
    changes.write("mobius.json", _manifest_text(manifest).encode("utf-8"))

    for stale in previous_art - _referenced_art(store):
      changes.delete(stale)
  except BaseException:
    changes.undo()
    raise
  return changes.undo
