"""register_app.py decides create-vs-update by a stable key.

Feature 097: a core app renamed in place (Memory Graph -> Memory, same
/data/apps/<slug>/ source dir) must UPDATE its existing row, not create a
duplicate. Matching on the display name regressed here because the name is
exactly the field a rename changes; matching on source_dir (stable across a
rename) is the fix.
"""

import importlib.util
import sys
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
    {"id": 7, "name": "Memory Graph", "source_dir": "/data/apps/memory"},
  ]
  # The app was renamed to "Memory" but its source dir is unchanged.
  existing = mod._find_existing(
    apps, source_dir="/data/apps/memory", name="Memory",
  )
  assert existing is not None, (
    "rename must match the existing row by source_dir, not create a duplicate"
  )
  assert existing["id"] == 7


def test_first_install_finds_no_match():
  """A genuinely new app (no row shares its source_dir) creates."""
  mod = _load_module()
  apps = [
    {"id": 7, "name": "Memory Graph", "source_dir": "/data/apps/memory"},
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


def test_core_migration_matches_explicit_legacy_source_dir():
  """The core installer can move a row from the old copied source tree to
  /data/platform/core-apps without creating a duplicate."""
  mod = _load_module()
  apps = [
    {"id": 10, "name": "Beat Machine", "source_dir": "/data/apps/beatmachine"},
  ]
  existing = mod._find_existing(
    apps,
    source_dir="/data/platform/core-apps/beat-machine",
    name="Beat Machine",
    legacy_source_dirs=["/data/apps/beatmachine"],
  )
  assert existing is not None and existing["id"] == 10


def test_core_migration_ignores_unrelated_legacy_source_dir():
  """A legacy source-dir hint is not enough to adopt an unrelated row."""
  mod = _load_module()
  apps = [
    {
      "id": 10,
      "name": "Notebook",
      "slug": "notebook",
      "source_dir": "/data/apps/memory",
    },
  ]
  existing = mod._find_existing(
    apps,
    source_dir="/data/platform/core-apps/memory",
    name="Memory",
    legacy_source_dirs=["/data/apps/memory"],
  )
  assert existing is None


def test_update_patch_includes_name(monkeypatch, tmp_path):
  """Registering an existing source dir should carry core display renames."""
  mod = _load_module()
  entry = tmp_path / "index.jsx"
  entry.write_text("export default function App() { return null }")
  patches = []

  def fake_call(url, token, method, data=None):
    if method == "GET":
      return [{
        "id": 7,
        "name": "Memory Graph",
        "slug": "memory",
        "source_dir": str(tmp_path),
      }]
    if method == "PATCH":
      patches.append(data)
      return {"id": 7}
    raise AssertionError(f"unexpected call: {method} {url}")

  monkeypatch.setenv("AGENT_TOKEN", "token")
  monkeypatch.setattr(sys, "argv", [
    "register_app.py", "Memory", "new description", str(entry),
  ])
  monkeypatch.setattr(mod, "_call", fake_call)
  monkeypatch.setattr(mod, "_notify", lambda *_args, **_kwargs: None)

  mod.main()

  assert patches and patches[0]["name"] == "Memory"
