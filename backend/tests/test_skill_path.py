"""Unit tests for `app.providers.get_skill_path`.

The system prompt prefers the split CONSTITUTION (`core.md`) and falls back to
the legacy monolith (`agent-skill.md`), checking the baked `/app/skill/` path
first, then the in-repo `skill/` dir (computed relative to the providers module
file). These tests use a fake Path keyed by string so they're robust to call
order and pass in both the local-venv and in-container pytest environments.
"""

from unittest.mock import patch

from app.providers import get_skill_path


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


def test_prefers_baked_core_md():
  """`/app/skill/core.md` (the constitution) wins over everything."""
  r = _resolve({"/app/skill/core.md", "/app/skill/agent-skill.md"})
  assert r is not None and r.p == "/app/skill/core.md"


def test_falls_back_to_baked_agent_skill():
  """Mid-migration (no core.md yet) → the legacy monolith still loads."""
  r = _resolve({"/app/skill/agent-skill.md"})
  assert r is not None and r.p == "/app/skill/agent-skill.md"


def test_repo_core_when_baked_missing():
  """Local dev: no baked /app/skill, but the in-repo skill/core.md exists."""
  r = _resolve({"/repo/skill/core.md"})
  assert r is not None and r.p == "/repo/skill/core.md"


def test_returns_none_when_nothing_exists():
  """No candidate anywhere → None (callers handle skill-less startup)."""
  assert _resolve(set()) is None


def test_resolves_real_skill_in_test_env():
  """Sanity, unmocked: resolves to a real core.md or agent-skill.md."""
  result = get_skill_path()
  if result is None:
    return
  assert result.exists()
  assert result.name in ("core.md", "agent-skill.md")
