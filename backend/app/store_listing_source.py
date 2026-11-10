"""Write an owner's Store listing edit into an app's editable source.

The listing is ordinary accepted source: text in ``mobius.json``'s ``store``
object, artwork under ``static/store/``. This module only changes the
worktree; the caller accepts the result through the normal apply path, and
uses the returned undo only if acceptance fails before Git advances. A
committed revision remains intact as the normal retry point.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Callable

from app import app_git, icon_assets, models, store_licenses, workspace_files
from app.storage_io import atomic_write
from app.community_publish import (
  DESCRIPTION_MAX_BYTES,
  MAX_STORE_SCREENSHOTS,
  SCREENSHOT_ALT_MAX_BYTES,
  SCREENSHOT_LABEL_MAX_BYTES,
  TAGLINE_MAX_BYTES,
  EDITOR_ART,
  CommunityPublicationError,
  check_listing_image,
  listing_image_has_metadata,
  strip_listing_image_metadata,
  read_accepted_file,
)
from app.manifest_contract import MANIFEST_MAX_BYTES, STORE_LISTING_IMAGE_MAX_BYTES


STORE_ART_DIR = "static/store"
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
  # An SPDX id the Store offers. None keeps the current license; there is
  # no way to remove one, since publishing requires it.
  license: str | None = None


def record_icon_for_save(root: Path, app: models.App, manifest: dict) -> bytes | None:
  """The icon on the app's record that saving a listing copies into source.

  An app from before manifest icons keeps its icon on its record only;
  accepting a revision without one would clear the icon the owner sees.
  ``manifest`` is the one the caller already parsed (BOM-aware), so a
  declared icon is never mistaken for a missing one and overwritten.
  """
  if manifest.get("icon") or (root / "icon.png").is_file():
    return None
  return app.icon_override_png or app.icon_png or None


def _icon_cleaned_in_place(declared: str) -> bool:
  """Whether removing an icon's metadata rewrites the declared file itself.

  An icon sharing content-named editor art gets a clean copy beside it, so
  the original stays published until an agent removes it.
  """
  return not declared.startswith(f"{STORE_ART_DIR}/")


def _icon_normalized_in_place(declared: str) -> bool:
  """Whether a new icon (always a PNG) can replace the declared file."""
  return declared.lower().endswith(".png") and _icon_cleaned_in_place(declared)


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
  # Only a save that can go ahead keeps the icon; one refused for missing or
  # unreadable details, or unapplied source changes, fixes nothing.
  manifest = _worktree_manifest(root)
  if icon["done"] or details.get("automatic") is False or manifest is None:
    return
  if icon.get("code") == "listing_image_metadata":
    declared = str(manifest.get("icon") or "").strip()
    if not _icon_cleaned_in_place(declared):
      # The save writes a clean copy, but the original stays published.
      icon["message"] = (
        f"Saving the listing writes a clean copy of the icon, but {declared} is "
        "published with the source too; ask an agent to remove it or save "
        "it without the metadata."
      )
      return
    message = "Saving the listing removes the icon's hidden metadata."
  elif record_icon_for_save(root, app, manifest) is not None:
    message = "Saving the listing keeps the app's current icon."
  else:
    return
  if not has_unapplied_changes(root, app.source_commit):
    icon["automatic"] = True
    icon["message"] = message


def _listing_fields_removed(manifest: dict, *, license_too: bool = False) -> dict:
  listing = {"icon", "store", "license"} if license_too else {"icon", "store"}
  return {key: value for key, value in manifest.items() if key not in listing}


def _worktree_manifest(root: Path) -> dict | None:
  """The worktree's mobius.json, parsed the one way every reader here uses."""
  path = root / "mobius.json"
  if not path.is_file() or path.stat().st_size > MANIFEST_MAX_BYTES:
    return None
  return _manifest_from(path.read_bytes())


def _manifest_from(raw: bytes | None) -> dict | None:
  """Parse manifest bytes; json.loads honours a UTF-8 byte-order mark."""
  try:
    manifest = json.loads(raw) if raw is not None else None
  except ValueError:
    return None
  return manifest if isinstance(manifest, dict) else None


def _named_by_its_content(rel: str, content: bytes) -> bool:
  return PurePosixPath(rel).stem == hashlib.sha256(content).hexdigest()[:16]


def _is_clean_listing_image(content: bytes) -> bool:
  try:
    check_listing_image(content, "listing image")
  except CommunityPublicationError:
    return False
  return not listing_image_has_metadata(content)


def changes_beyond_listing(root: Path, accepted: str, tree: str | None = None) -> bool:
  """Whether the candidate differs from the accepted revision beyond a listing.

  ``tree`` is the exact candidate apply will accept, read through Git, so
  nothing the worktree hides from a plain diff (skip-worktree or
  assume-unchanged entries) slips past; the save checks that. ``None``
  compares the worktree instead, read-only, for advisory checks that must
  not run apply's snapshot (it rewrites managed ignore rules).

  A save interrupted after writing, or one that failed after Git advanced,
  leaves listing files the next save may carry: the editor's hash-named art
  and the declared icon, each a clean listing image, mobius.json's icon,
  store and offered license fields, and the license text a save writes. So
  may the managed-ignore migration apply itself makes.
  Anything else (code, or other manifest fields) was never reviewed.
  """
  if tree is None:
    changed = app_git.worktree_diff_paths(root, accepted)

    def read(path: str, limit: int) -> bytes | None:
      target = root / path
      if not target.is_file() or target.is_symlink():
        return None
      return target.read_bytes() if target.stat().st_size <= limit else b""
  else:
    changed = app_git.endpoint_diff_paths(root, accepted, tree, read_only=True)

    def read(path: str, limit: int) -> bytes | None:
      size = app_git.ref_file_size(root, tree, path)
      if size is None:
        return None
      return app_git.ref_file(root, tree, path) if size <= limit else b""
  if changed is None:
    return True
  changed -= app_git.ignore_migration_paths(root, accepted, tree)
  if not changed:
    return False
  manifests = [
    _manifest_from(app_git.ref_file(root, accepted, "mobius.json")),
    _manifest_from(read("mobius.json", MANIFEST_MAX_BYTES)),
  ]
  icons = _carried_icon_paths(*manifests)
  license_paths = {path for path in changed if store_licenses.is_license_file(path)}
  license_choice_changed = (
    store_licenses.offered_license(manifests[0] or {})
    != store_licenses.offered_license(manifests[1] or {})
  )
  if (license_paths or license_choice_changed) and not _saved_license_text_only(
    root, accepted, changed, read, manifests[1] or {},
  ):
    return True
  for path in changed - license_paths:
    if path == "mobius.json":
      if None in manifests or not _manifest_change_is_listing(*manifests):
        return True
      continue
    # Only a listing file may differ; nothing else is even read.
    if path not in icons and not EDITOR_ART.fullmatch(path):
      return True
    content = read(path, STORE_LISTING_IMAGE_MAX_BYTES)
    if content is None:
      continue  # A deleted listing file carries nothing.
    # A present one must be a clean image, which a code file can never pass
    # as; editor art must also hold the bytes its name hashes.
    if not _is_clean_listing_image(content) or (
      path not in icons and not _named_by_its_content(path, content)
    ):
      return True
  return False


def _saved_license_text_only(
  root: Path,
  accepted: str,
  changed: set[str],
  read: Callable[[str, int], bytes | None],
  candidate: dict,
) -> bool:
  """Whether the candidate's root license texts are exactly what a save writes.

  A save writes LICENSE only for a changed license choice, or when no root
  license text exists, and always writes the generated text for the choice;
  a changed choice also removes every other root license text. So a
  touched license file, or a license choice that differs from the accepted
  one, rides along only beside that exact LICENSE and no other license text.
  Anything else, such as a manifest naming a new license above the old
  terms, was never saved and would publish contradictory licenses.
  """
  spdx = store_licenses.offered_license(candidate)
  if not spdx or read(store_licenses.LICENSE_FILE, MANIFEST_MAX_BYTES) != (
    store_licenses.license_text(spdx, candidate)
  ):
    return False
  others = {
    path for path in app_git.ref_root_files(root, accepted) | changed
    if store_licenses.is_license_file(path) and path != store_licenses.LICENSE_FILE
  }
  return all(read(path, 0) is None for path in others)


# The store fields the editor writes. Any other store key is curated
# elsewhere and must not change through a listing save.
_EDITOR_STORE_FIELDS = frozenset({"tagline", "description", "hero", "screenshots"})


def _manifest_change_is_listing(accepted: dict, candidate: dict) -> bool:
  """Whether the manifest changed only as a listing save would change it.

  Everything outside ``store``, ``icon`` and an offered ``license`` is
  unchanged; store keys the editor does not manage are unchanged; and the
  icon is the accepted one or a path the save itself writes. The owner sees
  only the applied listing, so anything else would be accepted unseen.
  """
  # A save may set the license only to one the Store offers.
  license_too = bool(store_licenses.offered_license(candidate))
  if (
    _listing_fields_removed(accepted, license_too=license_too)
    != _listing_fields_removed(candidate, license_too=license_too)
  ):
    return False

  def unmanaged(manifest: dict) -> dict:
    store = manifest.get("store")
    return {
      key: value for key, value in (store.items() if isinstance(store, dict) else ())
      if key not in _EDITOR_STORE_FIELDS
    }

  if unmanaged(accepted) != unmanaged(candidate):
    return False
  icon = str(candidate.get("icon") or "").strip()
  reviewed = str(accepted.get("icon") or "").strip()
  return icon == reviewed or (bool(icon) and icon in _carried_icon_paths(accepted, candidate))


def _carried_icon_paths(accepted: dict | None, candidate: dict | None) -> set[str]:
  """Icon paths a save may carry: the reviewed one, or one it could have written.

  The candidate manifest's icon value can itself be an unreviewed edit, so it
  only counts where write_listing itself could have put an icon: icon.png
  when none was declared, or content-named art under static/store/.
  """
  reviewed = str((accepted or {}).get("icon") or "").strip()
  proposed = str((candidate or {}).get("icon") or "").strip()
  paths = {reviewed} if reviewed else set()
  if proposed and (
    proposed == reviewed
    or (not reviewed and proposed == "icon.png")
    or EDITOR_ART.fullmatch(proposed)
  ):
    paths.add(proposed)
  return paths


def has_unapplied_changes(root: Path, accepted: str | None) -> bool:
  """Whether the worktree differs from the accepted revision beyond a listing.

  Read-only and advisory, for the preview and a save's early refusal; the
  save's authoritative check runs on the exact tree apply accepts. Source
  with no accepted revision counts as unapplied as a whole: with nothing to
  tell reviewed source from unreviewed commits, an apply records one first.
  """
  if not accepted or not app_git.is_repo(root):
    return True
  return changes_beyond_listing(root, accepted)


@dataclass
class _SourceChanges:
  """Original and written bytes of every path this edit touched, for undo."""

  root: Path
  originals: dict[str, bytes | None] = field(default_factory=dict)
  written: dict[str, bytes | None] = field(default_factory=dict)

  def _remember(self, rel: str) -> Path:
    target = _source_path(self.root, rel)
    if rel not in self.originals:
      self.originals[rel] = target.read_bytes() if target.is_file() else None
    return target

  def write(self, rel: str, content: bytes) -> None:
    target = self._remember(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, content)
    self.written[rel] = content

  def delete(self, rel: str) -> None:
    self._remember(rel).unlink(missing_ok=True)
    self.written[rel] = None

  def undo(self) -> None:
    for rel, content in self.originals.items():
      target = _source_path(self.root, rel)
      current = target.read_bytes() if target.is_file() else None
      if current != self.written.get(rel):
        continue  # Someone edited it since; their bytes win over the undo.
      if content is None:
        target.unlink(missing_ok=True)
      else:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, content)


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


def _accepted_file(root: Path, app: models.App, rel: str) -> bytes:
  """An image kept by path, read from the app's accepted revision.

  The worktree may hold ignored or generated images that were never accepted
  source; copying those into listing art would make them public.
  """
  _source_path(root, rel)
  try:
    content = read_accepted_file(app, rel, max_bytes=STORE_LISTING_IMAGE_MAX_BYTES)
  except CommunityPublicationError as exc:
    if exc.code == "payload_too_large":
      raise ListingEditError(
        "listing_image_too_large",
        f"The image {rel} is larger than "
        f"{STORE_LISTING_IMAGE_MAX_BYTES // (1024 * 1024)} MB.",
      ) from exc
    raise ListingEditError(exc.code, exc.detail, exc.status_code) from exc
  if content is None:
    raise ListingEditError(
      "listing_image_missing",
      f"The image {rel} is not part of the app's applied source. Add it again.",
    )
  return content


def _stripped(content: bytes, what: str) -> bytes:
  try:
    return strip_listing_image_metadata(content)
  except CommunityPublicationError as exc:
    raise ListingEditError(exc.code, f"The {what} could not be read.", exc.status_code) from exc


def _image_suffix(content: bytes, what: str) -> str:
  try:
    return check_listing_image(content, what)
  except CommunityPublicationError as exc:
    raise ListingEditError(exc.code, exc.detail, exc.status_code) from exc


def _content_named(content: bytes, suffix: str) -> str:
  """The editor's name for art holding ``content``: never another file's."""
  return f"{STORE_ART_DIR}/{hashlib.sha256(content).hexdigest()[:16]}{suffix}"


def _place_art(
  root: Path, app: models.App, changes: _SourceChanges, image: ListingImage, what: str,
) -> str:
  """Return the ``static/store/`` path holding this image, writing it if new.

  Listing art is public, so it never carries camera, location or other
  hidden metadata. New art is content-addressed by the bytes stored, so
  re-saving the same upload is a no-op and reordering screenshots never
  overwrites another one. A kept file outside ``static/store/`` (an older
  listing layout) is copied into it; kept art that still carries metadata
  (an agent committed it directly) is cleaned into a new content-named file.
  """
  content = image.data if image.data is not None else _accepted_file(root, app, image.path)
  suffix = _image_suffix(content, what)
  kept = image.data is None and image.path.startswith(f"{STORE_ART_DIR}/")
  if kept and not listing_image_has_metadata(content):
    rel = image.path
  else:
    # PNG and WebP strip losslessly; a JPEG is re-encoded only when it has
    # something to lose, so saving clean photos again never degrades them.
    if suffix != ".jpg" or listing_image_has_metadata(content):
      content = _stripped(content, what)
      suffix = _image_suffix(content, what)
    rel = _content_named(content, suffix)
  target = _source_path(root, rel)
  if not (target.is_file() and target.read_bytes() == content):
    changes.write(rel, content)
  return rel


def _manifest_text(manifest: dict, original: str) -> str:
  """Serialize ``manifest`` in the layout of the file it replaces.

  Keeping the author's indentation, line endings, escaping and final newline
  confines the diff to the listing, so a later Store update does not conflict
  on whitespace. Key order is already kept, since the edit updates the parsed
  dict in place.
  """
  match = re.match(r"\{[ \t]*\r?\n([ \t]+)\S", original)
  indent = match.group(1) if match else ("  " if "\n" in original.strip() else None)
  # Escape non-ASCII only for a file that already does and has no raw form.
  ascii_only = "\\u" in original and original.isascii()
  text = json.dumps(manifest, ensure_ascii=ascii_only, indent=indent)
  if original.endswith("\n"):
    text += "\n"
  return text.replace("\n", "\r\n") if "\r\n" in original else text


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


def _skip_string(text: str, at: int) -> int:
  at += 1
  while text[at] != '"':
    at += 2 if text[at] == "\\" else 1
  return at + 1


def _skip_value(text: str, at: int) -> int:
  if text[at] == '"':
    return _skip_string(text, at)
  if text[at] in "{[":
    depth = 0
    while True:
      if text[at] == '"':
        at = _skip_string(text, at)
        continue
      if text[at] in "{[":
        depth += 1
      elif text[at] in "}]":
        depth -= 1
        if depth == 0:
          return at + 1
      at += 1
  while text[at] not in ",}] \t\r\n":
    at += 1
  return at


def _skip_space(text: str, at: int) -> int:
  while text[at] in " \t\r\n":
    at += 1
  return at


def _top_level_members(text: str) -> tuple[dict[str, tuple[int, int]], str, int]:
  """Each top-level key's value span, the key separator, and the last value's end."""
  at = _skip_space(text, 0) + 1
  spans: dict[str, tuple[int, int]] = {}
  separator, last_end = ": ", at
  while text[at := _skip_space(text, at)] != "}":
    key_end = _skip_string(text, at)
    colon = _skip_space(text, key_end)
    value = _skip_space(text, colon + 1)
    if not spans:
      separator = ":" + text[colon + 1:value].replace("\r", "").replace("\n", "")[:1]
    # The last duplicate wins, as it does for json.loads.
    spans[json.loads(text[at:key_end])] = (value, last_end := _skip_value(text, value))
    at = _skip_space(text, last_end)
    if text[at] == ",":
      at += 1
  return spans, separator, last_end


def _spliced_manifest(original: str, manifest: dict, keys: tuple[str, ...]) -> str:
  """``original`` with only the top-level ``keys`` replaced or added.

  Everything else stays byte for byte, inline arrays and compact styles
  included, so a save's diff is the listing alone. Falls back to a whole
  rewrite in the file's own layout if the result would not parse back.
  """
  try:
    spans, separator, last_end = _top_level_members(original)
    newline = "\r\n" if "\r\n" in original else "\n"
    match = re.match(r"\{[ \t]*\r?\n([ \t]+)\S", original)
    indent = match.group(1) if match else None
    ascii_only = "\\u" in original and original.isascii()

    def value_text(value: object) -> str:
      text = json.dumps(
        value, ensure_ascii=ascii_only, indent=indent,
        separators=("," if indent is not None or separator == ":" else ", ", separator),
      )
      return text.replace("\n", newline + indent) if indent is not None else text

    edits = [
      (*spans[key], value_text(manifest[key])) for key in keys if key in spans
    ]
    added = "".join(
      ("," + newline + indent if indent is not None else ("," if separator == ":" else ", "))
      + json.dumps(key) + separator + value_text(manifest[key])
      for key in keys if key not in spans
    )
    if added:
      edits.append((last_end, last_end, added))
    text = original
    for start, end, replacement in sorted(edits, reverse=True):
      text = text[:start] + replacement + text[end:]
    if json.loads(text) == manifest:
      return text
  except Exception:  # Any surprise in the splice falls back to a rewrite.
    pass
  return _manifest_text(manifest, original)


def _icon_with_metadata(root: Path, declared: str) -> bytes | None:
  """The declared icon's bytes if it is a listing image with hidden metadata."""
  if not declared:
    return None
  try:
    target = _source_path(root, declared)
    if target.stat().st_size > STORE_LISTING_IMAGE_MAX_BYTES:
      return None  # Too large to be a listing image; readiness says so.
    content = target.read_bytes()
    check_listing_image(content, "app icon")
    return content if listing_image_has_metadata(content) else None
  except (OSError, ListingEditError, CommunityPublicationError):
    return None


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
        # Bytes, not text mode, which would fold CRLF line endings away.
        raw = manifest_path.read_bytes()
        # A byte-order mark is kept as it was; JSON itself never holds one.
        bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
        original = raw[len(bom):].decode("utf-8")
        current = json.loads(original)
      except (json.JSONDecodeError, UnicodeDecodeError) as exc:
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
        else _accepted_file(root, app, edit.icon.path)
      )
    elif (declared := _icon_with_metadata(root, declared_icon)) is not None:
      # The icon is published as it is, so only its metadata goes, losslessly
      # and in place; it keeps its size and shape. One sharing editor art is
      # written beside it instead, since art is named by its bytes.
      clean = _stripped(declared, "app icon")
      if not _icon_cleaned_in_place(icon_rel):
        icon_rel = _content_named(clean, check_listing_image(clean, "app icon"))
      changes.write(icon_rel, clean)
    else:
      icon_bytes = record_icon_for_save(root, app, current)
    if icon_bytes is not None:
      try:
        normalized = icon_assets.normalize_icon(icon_bytes)
      except icon_assets.InvalidIcon as exc:
        raise ListingEditError("listing_image_invalid", f"App icon: {exc}") from exc
      if declared_icon and not _icon_normalized_in_place(icon_rel):
        # Normalized icons are PNG; never write PNG bytes under another
        # suffix, nor over content-named art or an unrelated icon.png.
        icon_rel = _content_named(normalized, ".png")
      changes.write(icon_rel, normalized)
    has_icon = _source_path(root, icon_rel).is_file()

    screenshots = []
    for index, shot in enumerate(edit.screenshots, start=1):
      entry = {
        "src": _place_art(root, app, changes, shot.image, f"screenshot {index}"),
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
      store["hero"] = _place_art(root, app, changes, edit.hero, "banner image")
    else:
      store.pop("hero", None)
    store["screenshots"] = screenshots

    previous_art = _referenced_art(current.get("store"))
    manifest = dict(current)
    if has_icon:
      manifest["icon"] = icon_rel
    manifest["store"] = store
    if edit.license is not None:
      if edit.license not in store_licenses.OFFERED_SPDX:
        raise ListingEditError(
          "listing_license_invalid", "Choose one of the licenses the Store offers.",
        )
      license_changed = store_licenses.offered_license(current) != edit.license
      manifest["license"] = edit.license
      # The text follows the choice. An unchanged choice keeps the author's
      # own license text (their copyright line), writing one only when none
      # exists. A changed choice replaces every root license text, so the old
      # terms (in COPYING, LICENSE.md, ...) never sit beside the new LICENSE.
      root_files = [entry.name for entry in root.iterdir() if entry.is_file()]
      if license_changed or not store_licenses.has_license_file(root_files):
        changes.write(
          store_licenses.LICENSE_FILE,
          store_licenses.license_text(edit.license, manifest),
        )
      if license_changed:
        for name in root_files:
          if name != store_licenses.LICENSE_FILE and store_licenses.is_license_file(name):
            changes.delete(name)
    changes.write(
      "mobius.json",
      bom + _spliced_manifest(
        original, manifest,
        tuple(key for key in ("icon", "store", "license") if key in manifest),
      ).encode("utf-8"),
    )

    # Listing art the icon still uses is not stale, and only the editor's own
    # content-addressed files are removed.
    for stale in previous_art - _referenced_art(store) - {manifest.get("icon")}:
      # The editor's own files hold the bytes their names hash; an author's
      # file that merely looks like one is left alone.
      target = root / stale
      if not (
        EDITOR_ART.fullmatch(stale) and target.is_file()
        and _named_by_its_content(stale, target.read_bytes())
      ):
        continue
      changes.delete(stale)
  except BaseException:
    changes.undo()
    raise
  return changes.undo
