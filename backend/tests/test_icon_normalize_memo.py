"""Repeated update checks must not re-encode unchanged app icons."""

import io

from PIL import Image

from app import icon_assets


def _png(color: tuple[int, int, int]) -> bytes:
  out = io.BytesIO()
  Image.new("RGB", (40, 30), color).save(out, format="PNG")
  return out.getvalue()


def test_unchanged_icon_is_encoded_once_and_returns_identical_bytes(monkeypatch):
  icon_assets._normalized.clear()
  calls = []
  real = icon_assets._normalize_uncached
  monkeypatch.setattr(
    icon_assets, "_normalize_uncached",
    lambda raw: calls.append(raw) or real(raw),
  )
  raw = _png((10, 20, 30))

  first = icon_assets.normalize_icon(raw)
  second = icon_assets.normalize_icon(bytes(raw))

  assert first == second == real(raw)
  assert len(calls) == 1
  assert Image.open(io.BytesIO(first)).size == (30, 30)


def test_remembered_icons_stay_bounded(monkeypatch):
  icon_assets._normalized.clear()
  monkeypatch.setattr(icon_assets, "_NORMALIZED_CACHE_SIZE", 2)
  for shade in range(4):
    icon_assets.normalize_icon(_png((shade, shade, shade)))
  assert len(icon_assets._normalized) == 2
