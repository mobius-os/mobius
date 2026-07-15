"""Unit tests for `app.providers.get_skill_path`.

The system prompt is the CONSTITUTION (`core.md`), resolved from the in-repo
`skill/` dir first (computed relative to the providers module file), then the
baked `/app/skill/` degraded-runtime fallback. These tests use a fake Path so
they're robust to call order and pass in both the local-venv and in-container
pytest environments.
"""

from unittest.mock import patch

from pathlib import Path

from app.providers import get_skill_origin, get_skill_path


class FakePath:
  """Minimal Path stand-in keyed by string; existence is set-controlled."""

  def __init__(self, p, exists_set):
    self.p = str(p)
    self._set = exists_set

  @property
  def name(self):
    return self.p.rsplit("/", 1)[-1]

  @property
  def parent(self):
    head = self.p.rsplit("/", 1)[0]
    return FakePath(head or "/", self._set)

  def __truediv__(self, other):
    return FakePath(self.p.rstrip("/") + "/" + str(other), self._set)

  def exists(self):
    return self.p in self._set

  def __repr__(self):
    return f"FakePath({self.p})"


def _resolve(exists_set, providers_file="/repo/backend/app/providers.py"):
  def factory(x):
    return FakePath(x, exists_set)

  with patch("app.providers.__file__", providers_file), \
       patch("app.providers.Path", side_effect=factory):
    return get_skill_path()


def test_prefers_repo_core_md():
  """The running checkout wins when both it and the baked fallback exist."""
  r = _resolve({"/app/skill/core.md", "/repo/skill/core.md"})
  assert r is not None and r.p == "/repo/skill/core.md"


def test_baked_core_when_repo_missing():
  """Degraded boot still has the immutable image-baked constitution."""
  r = _resolve({"/app/skill/core.md"})
  assert r is not None and r.p == "/app/skill/core.md"


def test_returns_none_when_nothing_exists():
  """No candidate anywhere → None (callers handle skill-less startup)."""
  assert _resolve(set()) is None


def test_skill_origin_distinguishes_checkout_fallback_and_missing():
  assert get_skill_origin(Path("/repo/skill/core.md")) == "platform"
  assert get_skill_origin(Path("/app/skill/core.md")) == "baked_fallback"
  with patch("app.providers.get_skill_path", return_value=None):
    assert get_skill_origin() == "missing"


def test_resolves_real_skill_in_test_env():
  """Sanity, unmocked: resolves to a real core.md."""
  result = get_skill_path()
  if result is None:
    return
  assert result.exists()
  assert result.name == "core.md"
