"""Bounded, best-effort transcript previews for stored raster images."""

import hashlib
import os
import tempfile
import threading
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


PREVIEW_MAX_EDGE = 1024
PREVIEW_WEBP_QUALITY = 72
PREVIEW_DIR = ".previews"

# Pillow expands compressed images in memory. Two concurrent generators keep a
# transcript moving without letting a screenshot-heavy chat occupy the entire
# worker pool with decoded originals.
_GENERATION_SLOTS = threading.BoundedSemaphore(2)


def preview_cache_path(file_path: Path, base: Path) -> Path:
  """Return the single stable derivative path owned by one source filename."""
  digest = hashlib.sha256(file_path.name.encode("utf-8")).hexdigest()[:24]
  return base / PREVIEW_DIR / f"{digest}.webp"


def display_image_preview(file_path: Path, base: Path) -> Path | None:
  """Return a cached display-sized WebP, or None when decoding fails.

  Freshness follows the source mtime, keeping the cache bounded to one file per
  source name even when a file is replaced. Generation is best-effort so an
  otherwise valid image still falls back to its original response.
  """
  preview_path = preview_cache_path(file_path, base)

  def fresh_preview() -> Path | None:
    try:
      source_mtime_ns = file_path.stat().st_mtime_ns
      if (
        preview_path.is_file()
        and preview_path.stat().st_size > 0
        and preview_path.stat().st_mtime_ns >= source_mtime_ns
      ):
        return preview_path
    except OSError:
      return None
    return None

  cached = fresh_preview()
  if cached is not None:
    return cached

  try:
    preview_path.parent.mkdir(parents=True, exist_ok=True)
  except OSError:
    return None
  with _GENERATION_SLOTS:
    # Another request may have filled this entry while the current request
    # waited for a bounded generation slot.
    cached = fresh_preview()
    if cached is not None:
      return cached

    temp_path = None
    try:
      with Image.open(file_path) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source)
        if image.mode not in {"RGB", "RGBA"}:
          image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        image.thumbnail(
          (PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE),
          Image.Resampling.LANCZOS,
        )
        with tempfile.NamedTemporaryFile(
          dir=preview_path.parent,
          prefix=".preview-",
          suffix=".webp",
          delete=False,
        ) as temp:
          temp_path = Path(temp.name)
          image.save(
            temp,
            format="WEBP",
            quality=PREVIEW_WEBP_QUALITY,
            method=4,
          )
        os.replace(temp_path, preview_path)
        temp_path = None
        return preview_path
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
      return None
    finally:
      if temp_path is not None:
        try:
          temp_path.unlink(missing_ok=True)
        except OSError:
          pass


def discard_image_preview(file_path: Path, base: Path) -> None:
  """Best-effort cleanup of the derivative owned by a deleted source file."""
  try:
    preview_cache_path(file_path, base).unlink(missing_ok=True)
  except OSError:
    pass
