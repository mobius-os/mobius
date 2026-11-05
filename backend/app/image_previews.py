"""Bounded previews and intrinsic layout metadata for stored raster images."""

import hashlib
import io
import json
import os
import tempfile
import threading
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.icon_assets import MAX_IMAGE_PIXELS

# Preserve the 32 MP warning / 64 MP refusal regardless of icon import order.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


PREVIEW_MAX_EDGE = 1024
PREVIEW_WEBP_QUALITY = 72
PREVIEW_DIR = ".previews"
_EXIF_ORIENTATION = 274

# Remote bytes are untrusted and already in memory, so they get a tighter
# decode budget than stored images. A 16 MP image is 64 MiB of RGBA pixels;
# decoding, converting and resampling one peaks at about 150 MB, and the shared
# generation slots below cap how many run at once.
DOWNSCALE_MAX_PIXELS = 4096 * 4096
_DOWNSCALE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF")
# Commit-pinned downscales are cached by browsers as immutable. Changing this
# or the encoding in downscaled_webp changes those bytes, so bump
# PROXY_PINNED_CACHE in frontend/src/sw-cache-policy.js with it.
DOWNSCALE_WEBP_QUALITY = 72

# Pillow expands compressed images in memory. Two concurrent generators keep a
# transcript moving without letting a screenshot-heavy chat occupy the entire
# worker pool with decoded originals.
_GENERATION_SLOTS = threading.BoundedSemaphore(2)


def preview_cache_path(file_path: Path, base: Path) -> Path:
  """Return the single stable derivative path owned by one source path."""
  try:
    source_path = file_path.resolve()
  except (OSError, RuntimeError):
    source_path = Path(os.path.abspath(file_path))
  try:
    base_path = base.resolve()
  except (OSError, RuntimeError):
    base_path = Path(os.path.abspath(base))

  try:
    cache_key = os.fsencode(source_path.relative_to(base_path).as_posix())
  except ValueError:
    # Normal callers validate containment before reaching this module. Keep an
    # unexpected out-of-base path deterministic and confined to the cache by
    # hashing its normalized absolute path, separated from relative keys by a
    # byte that cannot occur in a filesystem path.
    cache_key = b"\0outside\0" + os.fsencode(source_path.as_posix())
  digest = hashlib.sha256(cache_key).hexdigest()[:24]
  return base / PREVIEW_DIR / f"{digest}.webp"


def dimensions_cache_path(file_path: Path, base: Path) -> Path:
  """Return the stable intrinsic-dimensions sidecar for one source filename."""
  return preview_cache_path(file_path, base).with_suffix(".json")


def _exif_orientation(source: Image.Image) -> int:
  try:
    return source.getexif().get(_EXIF_ORIENTATION, 1)
  except (AttributeError, OSError, ValueError):
    return 1


def _oriented_dimensions(source: Image.Image) -> tuple[int, int] | None:
  """Read display dimensions from headers, including EXIF rotation.

  Most formats expose size and rotation in their headers. Pillow may decode
  PNGs to find trailing EXIF metadata; the owning dimensions cache keeps that
  cold-read cost out of repeated chat-detail responses.
  """
  width, height = source.size
  if _exif_orientation(source) in {5, 6, 7, 8}:
    width, height = height, width
  if width <= 0 or height <= 0:
    return None
  return int(width), int(height)


def stored_image_dimensions(file_path: Path, base: Path) -> dict | None:
  """Return cached display dimensions for a stored raster image.

  The disk sidecar is keyed by the source's size and nanosecond mtime. A cold
  lookup reads size and rotation, then writes atomically; later chat reads
  do not open the image at all. Invalid or unsupported files deliberately have
  no dimensions so the renderer can show an explicit image error rather than a
  guessed aspect ratio that changes after decode.
  """
  cache_path = dimensions_cache_path(file_path, base)
  try:
    source_stat = file_path.stat()
  except OSError:
    return None

  try:
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    if (
      cached.get("source_mtime_ns") == source_stat.st_mtime_ns
      and cached.get("source_size") == source_stat.st_size
      and isinstance(cached.get("width"), int)
      and cached["width"] > 0
      and isinstance(cached.get("height"), int)
      and cached["height"] > 0
    ):
      return {"width": cached["width"], "height": cached["height"]}
  except (OSError, ValueError, TypeError, AttributeError):
    pass

  try:
    with Image.open(file_path) as source:
      dimensions = _oriented_dimensions(source)
  except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
    return None
  if dimensions is None:
    return None

  width, height = dimensions
  payload = {
    "source_mtime_ns": source_stat.st_mtime_ns,
    "source_size": source_stat.st_size,
    "width": width,
    "height": height,
  }
  temp_path = None
  try:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
      dir=cache_path.parent,
      prefix=".dimensions-",
      suffix=".json",
      mode="w",
      encoding="utf-8",
      delete=False,
    ) as temp:
      temp_path = Path(temp.name)
      json.dump(payload, temp, separators=(",", ":"))
      temp.flush()
      os.fsync(temp.fileno())
    os.replace(temp_path, cache_path)
    temp_path = None
  except OSError:
    # Metadata is a performance cache, not the source of truth. A read-only or
    # full cache directory may cost another header read but must not hide an
    # otherwise valid image.
    pass
  finally:
    if temp_path is not None:
      try:
        temp_path.unlink(missing_ok=True)
      except OSError:
        pass
  return {"width": width, "height": height}


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


def downscaled_webp(data: bytes, max_width: int) -> bytes | None:
  """Return a WebP of `data` at most `max_width` display pixels wide.

  None means the caller should serve the original bytes: they are not a
  supported raster image, cannot be decoded, exceed the pixel budget, are
  already narrow enough, or would not get smaller. Never upscales; aspect
  ratio, alpha, EXIF rotation and the colour profile are kept. Animated
  sources keep their first frame.
  """
  with _GENERATION_SLOTS:
    try:
      with Image.open(io.BytesIO(data), formats=_DOWNSCALE_FORMATS) as source:
        # Check the header size first: reading PNG EXIF may decode the image.
        if source.width * source.height > DOWNSCALE_MAX_PIXELS:
          return None
        # 16-bit and float pixels clip to white in an 8-bit conversion.
        if source.mode.startswith(("I", "F")):
          return None
        dimensions = _oriented_dimensions(source)
        if dimensions is None or dimensions[0] <= max_width:
          return None
        # exif_transpose returns a full copy even for an upright image.
        image = (
          ImageOps.exif_transpose(source)
          if _exif_orientation(source) in range(2, 9) else source
        )
        # Includes RGB with a tRNS colour key, which stays transparent only
        # once expanded to an alpha channel.
        target_mode = "RGBA" if image.has_transparency_data else "RGB"
        if image.mode != target_mode:
          image = image.convert(target_mode)
        image.thumbnail((max_width, image.height), Image.Resampling.LANCZOS)
        # A CMYK or grey profile would misdescribe the converted RGB pixels.
        rgb_source = source.mode in {"RGB", "RGBA", "P", "PA"}
        encoded = io.BytesIO()
        image.save(
          encoded,
          format="WEBP",
          quality=DOWNSCALE_WEBP_QUALITY,
          method=4,
          icc_profile=source.info.get("icc_profile") if rgb_source else None,
        )
    except Exception:
      # Arbitrary network bytes can fail inside any decoder; the original
      # response is always a correct answer.
      return None
  result = encoded.getvalue()
  return result if len(result) < len(data) else None


def discard_image_preview(file_path: Path, base: Path) -> None:
  """Best-effort cleanup of derivatives owned by a deleted source file."""
  for path in (
    preview_cache_path(file_path, base),
    dimensions_cache_path(file_path, base),
  ):
    try:
      path.unlink(missing_ok=True)
    except OSError:
      pass
