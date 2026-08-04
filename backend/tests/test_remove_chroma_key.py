"""Contract for recovering real alpha from a chroma-key rendered icon."""

import importlib.util
from pathlib import Path
import sys

from PIL import Image
import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "remove_chroma_key.py"
SPEC = importlib.util.spec_from_file_location("remove_chroma_key", SCRIPT)
assert SPEC and SPEC.loader
CHROMA = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHROMA
SPEC.loader.exec_module(CHROMA)


GREEN = (0, 255, 0)
MAGENTA = (255, 0, 255)


def _ring(size: int, key: tuple[int, int, int], subject: tuple[int, int, int]) -> Image.Image:
  """A subject with a hole in it, rendered opaque on a flat key background."""
  image = Image.new("RGB", (size, size), key)
  pixels = image.load()
  centre = size / 2
  for y in range(size):
    for x in range(size):
      radius = ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5
      if size * 0.15 < radius < size * 0.40:
        pixels[x, y] = subject
  return image


def test_enclosed_hole_becomes_transparent():
  """A flood fill from the edges would leave the ring's centre opaque."""
  icon = CHROMA.remove_chroma_key(_ring(64, GREEN, (200, 40, 90)))
  alpha = icon.getchannel("A")

  assert alpha.getpixel((32, 32)) == 0, "the hole inside the ring stayed filled"
  assert alpha.getpixel((32, 32 - 17)) == 255, "the ring itself was keyed away"
  assert alpha.getpixel((0, 0)) == 0


def test_opaque_subject_round_trips_exactly():
  """The rendered subject's own colour survives the cut unchanged."""
  subject = (200, 40, 90)
  icon = CHROMA.remove_chroma_key(_ring(64, GREEN, subject))

  assert icon.getpixel((32, 32 - 17)) == (*subject, 255)


def test_green_subject_keys_against_magenta():
  """The alternate key is what keeps a green subject from being erased."""
  subject = (20, 220, 60)
  icon = CHROMA.remove_chroma_key(_ring(64, MAGENTA, subject))

  assert icon.getpixel((32, 32 - 17)) == (*subject, 255)


def test_subject_matching_the_key_is_rejected():
  """Keying a green subject on green silently returns an empty icon."""
  icon = CHROMA.remove_chroma_key(_ring(64, GREEN, (0, 250, 4)))

  with pytest.raises(CHROMA.KeyRemovalError, match="every pixel was keyed out"):
    CHROMA.verify_icon(icon)


def test_unflat_background_is_rejected():
  """A gradient background cannot be keyed, and must not pass as an icon."""
  image = Image.new("RGB", (64, 64))
  pixels = image.load()
  for y in range(64):
    for x in range(64):
      pixels[x, y] = (0, 255, y * 3)

  with pytest.raises(CHROMA.KeyRemovalError, match="not a uniform key colour"):
    CHROMA.remove_chroma_key(image)


def test_edge_pixels_shed_the_key_colour():
  """A blended edge keeps the subject's hue rather than the key's cast."""
  image = Image.new("RGB", (8, 8), GREEN)
  blended = (0, 128, 128)  # pure blue, rendered half-covered over the key
  image.putpixel((4, 4), blended)

  icon = CHROMA.remove_chroma_key(image, clear_below=8, solid_above=200)
  red, green, blue, alpha = icon.getpixel((4, 4))

  assert 0 < alpha < 255
  assert green < blended[1] / 2, "the key's cast was left on the edge pixel"
  assert blue > green, "the subject's own hue did not survive the cut"
  assert red == 0


def test_edge_pixels_recomposite_onto_the_key_unchanged():
  """De-fringing corrects colour for the alpha it assigned, losing nothing."""
  image = Image.new("RGB", (8, 8), GREEN)
  blended = (0, 128, 128)
  image.putpixel((4, 4), blended)

  icon = CHROMA.remove_chroma_key(image, clear_below=8, solid_above=200)
  red, green, blue, alpha = icon.getpixel((4, 4))
  coverage = alpha / 255
  recomposited = tuple(
    round(coverage * channel + (1 - coverage) * key)
    for channel, key in zip((red, green, blue), GREEN)
  )

  assert all(abs(a - b) <= 1 for a, b in zip(recomposited, blended))
