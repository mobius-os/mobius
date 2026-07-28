"""Validation and normalization for every accepted app-icon source."""

from __future__ import annotations

import io
import warnings

from PIL import Image


# Pillow's default (~89M pixels) still permits a tiny hostile file to request
# a very large allocation. App icons never need that headroom.
Image.MAX_IMAGE_PIXELS = 32_000_000
MAX_ICON_DIMENSION = 4096


class InvalidIcon(ValueError):
  """The supplied bytes cannot become a bounded app icon."""


def normalize_icon(raw: bytes) -> bytes:
  """Return one bounded square RGB/RGBA PNG for install, apply, or override."""
  try:
    image = Image.open(io.BytesIO(raw))
    # Header dimensions are available before load(), so reject oversized
    # images before Pillow allocates their decoded pixel buffer.
    with warnings.catch_warnings():
      warnings.simplefilter("error", Image.DecompressionBombWarning)
      width, height = image.size
      if width > MAX_ICON_DIMENSION or height > MAX_ICON_DIMENSION:
        raise InvalidIcon(
          f"Icon dimensions {width}x{height} exceed "
          f"{MAX_ICON_DIMENSION}x{MAX_ICON_DIMENSION} cap."
        )
      image.load()
  except InvalidIcon:
    raise
  except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
    raise InvalidIcon(f"Icon rejected as decompression bomb: {exc}") from exc
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
