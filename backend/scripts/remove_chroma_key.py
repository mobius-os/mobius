#!/usr/bin/env python3
"""Turn a chroma-key rendered image into real alpha.

Generated icon art arrives on a solid key background (`#00ff00`, or `#ff00ff`
when the subject itself is green). Removing that key by flood fill from the
edges leaves enclosed holes opaque -- the gap inside a ring, a handle, or a
loop stays filled with key colour. This keys on colour distance instead, so a
key pixel becomes transparent wherever it sits, including inside a closed
shape.

Distance is Chebyshev (largest per-channel difference), which separates a
saturated key from real artwork far more cleanly than a Euclidean average.

Pixels nearer than `--clear-below` are fully transparent, pixels beyond
`--solid-above` are fully opaque, and the band between ramps linearly so edges
stay soft instead of aliasing. Those partial-alpha pixels are a blend of the
subject and the key, so their stored colour still carries the key's cast --
the green fringe around an otherwise clean cut-out. Because the background
colour is known exactly, that blend is reversible: dividing the key's
contribution back out recovers the subject's own colour instead of merely
damping the offending channel.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from PIL import Image


class KeyRemovalError(RuntimeError):
  """The key colour or thresholds did not describe this image."""


def _parse_key(value: str) -> tuple[int, int, int]:
  text = value.strip().lstrip("#")
  if len(text) != 6:
    raise argparse.ArgumentTypeError(f"expected #rrggbb, got {value!r}")
  try:
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
  except ValueError:
    raise argparse.ArgumentTypeError(f"expected #rrggbb, got {value!r}") from None


def detect_key(image: Image.Image) -> tuple[int, int, int]:
  """The dominant colour on the 1px border, which the key fully occupies."""
  width, height = image.size
  pixels = image.load()
  border = Counter()
  for x in range(width):
    border[pixels[x, 0][:3]] += 1
    border[pixels[x, height - 1][:3]] += 1
  for y in range(height):
    border[pixels[0, y][:3]] += 1
    border[pixels[width - 1, y][:3]] += 1
  colour, count = border.most_common(1)[0]
  if count < sum(border.values()) * 0.5:
    raise KeyRemovalError(
      "border is not a uniform key colour -- pass --key explicitly, or "
      "regenerate the source against a flat background",
    )
  return colour


def _unblend(value: int, key_value: int, alpha: int) -> int:
  """The subject's own channel value, with the key's share divided out.

  A fringe pixel holds ``alpha*subject + (1-alpha)*key``. Inverting that is
  exact for the background actually used, and only ever runs where alpha is
  partial, so a fully opaque interior keeps its rendered colour untouched.
  """
  recovered = round((value - key_value) * 255 / alpha) + key_value
  return min(255, max(0, recovered))


def remove_chroma_key(
  image: Image.Image,
  *,
  key: tuple[int, int, int] | None = None,
  clear_below: int = 16,
  solid_above: int = 64,
  unblend: bool = True,
) -> Image.Image:
  if clear_below >= solid_above:
    raise KeyRemovalError("--clear-below must be smaller than --solid-above")
  rgb = image.convert("RGB")
  key = key or detect_key(rgb)
  key_r, key_g, key_b = key
  span = solid_above - clear_below

  source = rgb.tobytes()
  pixels = bytearray(len(source) // 3 * 4)
  for read, write in zip(range(0, len(source), 3), range(0, len(pixels), 4)):
    red, green, blue = source[read], source[read + 1], source[read + 2]
    distance = max(abs(red - key_r), abs(green - key_g), abs(blue - key_b))
    if distance <= clear_below:
      continue
    if distance >= solid_above:
      pixels[write:write + 4] = bytes((red, green, blue, 255))
      continue
    alpha = round((distance - clear_below) * 255 / span) or 1
    pixels[write:write + 4] = bytes((
      _unblend(red, key_r, alpha) if unblend else red,
      _unblend(green, key_g, alpha) if unblend else green,
      _unblend(blue, key_b, alpha) if unblend else blue,
      alpha,
    ))

  return Image.frombytes("RGBA", rgb.size, bytes(pixels))


def verify_icon(icon: Image.Image) -> None:
  """Reject the two failures that are invisible until the icon ships."""
  alpha = icon.getchannel("A")
  if alpha.getbbox() is None:
    raise KeyRemovalError(
      "every pixel was keyed out -- the detected key matches the subject, so "
      "regenerate against the other key colour",
    )
  width, height = icon.size
  corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
  if any(alpha.getpixel(point) for point in corners):
    raise KeyRemovalError(
      "corners are still opaque -- the background is not the detected key "
      "colour, or the subject bleeds to the canvas edge",
    )


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("input", type=Path, help="chroma-key source image")
  parser.add_argument("--out", type=Path, required=True, help="destination PNG")
  parser.add_argument(
    "--key", type=_parse_key, default=None,
    help="key colour as #rrggbb (default: detect from the border)",
  )
  parser.add_argument(
    "--clear-below", type=int, default=16,
    help="colour distance under which a pixel is fully transparent",
  )
  parser.add_argument(
    "--solid-above", type=int, default=64,
    help="colour distance over which a pixel is fully opaque",
  )
  parser.add_argument(
    "--keep-spill", action="store_true",
    help="leave the key's colour cast on edge pixels",
  )
  args = parser.parse_args()

  with Image.open(args.input) as source:
    source.load()
    icon = remove_chroma_key(
      source,
      key=args.key,
      clear_below=args.clear_below,
      solid_above=args.solid_above,
      unblend=not args.keep_spill,
    )
  verify_icon(icon)
  args.out.parent.mkdir(parents=True, exist_ok=True)
  icon.save(args.out, format="PNG", optimize=True)
  return 0


if __name__ == "__main__":
  try:
    sys.exit(main())
  except KeyRemovalError as error:
    print(f"remove_chroma_key: {error}", file=sys.stderr)
    sys.exit(1)
