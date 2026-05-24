"""All `backend/app/` modules must import cleanly.

This catches syntax errors, missing imports, and undefined names at
unit-test time — even when no other test exercises the module. Added
after a syntax bug in `claude_sdk_runner.py` slipped past the full
test suite because nothing imported the runner from tests; live smoke
caught it three iterations later. See `_003-tech-debt-and-test-gaps.md`
TG-1 for context.
"""

import importlib
import pkgutil
from pathlib import Path


def _enumerate_app_modules() -> list[str]:
  """Returns the fully-qualified module name of every .py file under
  `backend/app/`, excluding `__pycache__` and dunder files."""
  app_root = Path(__file__).resolve().parents[1] / "app"
  modules: list[str] = []
  for info in pkgutil.walk_packages([str(app_root)], prefix="app."):
    if info.name.endswith(".__main__"):
      continue
    modules.append(info.name)
  # Include `app` itself.
  modules.append("app")
  return sorted(set(modules))


def test_all_app_modules_import_cleanly():
  """Every module under `backend/app/` imports without error."""
  failures: list[tuple[str, str]] = []
  for name in _enumerate_app_modules():
    try:
      importlib.import_module(name)
    except Exception as exc:
      failures.append((name, f"{type(exc).__name__}: {exc}"))
  if failures:
    detail = "\n".join(f"  {name}: {msg}" for name, msg in failures)
    raise AssertionError(
      f"{len(failures)} module(s) failed to import:\n{detail}"
    )
