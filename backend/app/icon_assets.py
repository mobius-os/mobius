"""Validation and normalization for every accepted app-icon source."""

from __future__ import annotations

import hashlib
import io
import threading
from collections import OrderedDict

from PIL import Image

MAX_IMAGE_PIXELS = 32_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# The icon pixel ceiling is the only size rule: images within it are scaled
# down. Author-facing apply and publication reject invalid icons; imported
# packages instead warn and omit/preserve them for historical compatibility.
# Install and update-check digests both omit refused icons.


class InvalidIcon(ValueError):
  """The supplied bytes cannot become a bounded app icon."""


def normalize_icon(raw: bytes) -> bytes:
  """Return one bounded square RGB/RGBA PNG for install, apply, or override.

  Normalization is pure, and every update check and candidate fetch feeds the
  same unchanged icons through it to rebuild package digests; the optimized
  PNG encode costs 0.1-0.3 s of CPU per icon. Remember recent outputs by the
  source bytes' hash so repeated checks reuse the identical result.
  """
  key = hashlib.sha256(raw).digest()
  with _normalized_lock:
    cached = _normalized.get(key)
    if cached is not None:
      _normalized.move_to_end(key)
      return cached
  result = _normalize_uncached(raw)
  with _normalized_lock:
    _normalized[key] = result
    _normalized.move_to_end(key)
    while len(_normalized) > _NORMALIZED_CACHE_SIZE:
      _normalized.popitem(last=False)
  return result


_NORMALIZED_CACHE_SIZE = 128
_normalized: OrderedDict[bytes, bytes] = OrderedDict()
_normalized_lock = threading.Lock()


def _normalize_uncached(raw: bytes) -> bytes:
  too_large = (
    f"Icon has more than the {MAX_IMAGE_PIXELS // 1_000_000} million pixels "
    "an icon may have. Use a smaller image; 1024x1024 is plenty."
  )
  try:
    # Refuse from the header before load() allocates the pixel buffer. Do not
    # rely on Pillow's process-global warning filters in concurrent workers.
    image = Image.open(io.BytesIO(raw))
    if image.width * image.height > MAX_IMAGE_PIXELS:
      raise Image.DecompressionBombError(too_large)
    image.load()
  except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
    raise InvalidIcon(too_large) from exc
  except Exception as exc:
    raise InvalidIcon("Icon is not a valid image.") from exc

  if image.mode not in ("RGB", "RGBA"):
    # Palette PNG transparency lives in tRNS metadata. Treat palette images as
    # potentially transparent so normalization never bakes black corners in.
    has_alpha = (
      "A" in image.mode
      or "transparency" in image.info
      or image.mode == "P"
    )
    image = image.convert("RGBA" if has_alpha else "RGB")

  width, height = image.size
  if width != height:
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    image = image.crop((left, top, left + side, top + side))
  if image.size[0] > 1024:
    image = image.resize((1024, 1024), Image.LANCZOS)

  output = io.BytesIO()
  image.save(output, format="PNG", optimize=True)
  return output.getvalue()
