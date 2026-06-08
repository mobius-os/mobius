"""register_app.py decides create-vs-update by a stable key.

Feature 097: a core app renamed in place (Memory Graph -> Mind, same
/data/apps/<slug>/ source dir) must UPDATE its existing row, not create a
duplicate. Matching on the display name regressed here because the name is
exactly the field a rename changes; matching on source_dir (stable across a
rename) is the fix.
"""

import importlib.util
from pathlib import Path

_SCRIPT = (
  Path(__file__).resolve().parent.parent / "scripts" / "register_app.py"
)


def _load_module():
  """Load register_app.py by path — scripts/ is not an importable package."""
  spec = importlib.util.spec_from_file_location("register_app", _SCRIPT)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_rename_matches_existing_by_source_dir():
  """A renamed app (same source_dir, new name) resolves to the SAME row."""
  mod = _load_module()
  apps = [
    {"id": 7, "name": "Memory Graph", "source_dir": "/data/apps/mind"},
  ]
  # The app was renamed to "Mind" but its source dir is unchanged.
  existing = mod._find_existing(
    apps, source_dir="/data/apps/mind", name="Mind",
  )
  assert existing is not None, (
    "rename must match the existing row by source_dir, not create a duplicate"
  )
  assert existing["id"] == 7


def test_first_install_finds_no_match():
  """A genuinely new app (no row shares its source_dir) creates."""
  mod = _load_module()
  apps = [
    {"id": 7, "name": "Memory Graph", "source_dir": "/data/apps/mind"},
  ]
  existing = mod._find_existing(
    apps, source_dir="/data/apps/notes", name="Notes",
  )
  assert existing is None


def test_source_dir_match_beats_name_collision():
  """source_dir is the identity; a same-NAME row at a different dir is NOT it."""
  mod = _load_module()
  apps = [
    {"id": 3, "name": "Notes", "source_dir": "/data/apps/notes-old"},
    {"id": 4, "name": "Renamed", "source_dir": "/data/apps/notes"},
  ]
  existing = mod._find_existing(
    apps, source_dir="/data/apps/notes", name="Notes",
  )
  assert existing is not None and existing["id"] == 4
