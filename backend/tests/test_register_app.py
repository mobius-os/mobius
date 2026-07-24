"""register_app.py decides create-vs-update by a stable key.

Feature 097: an app renamed in place (Memory Graph -> Memory, same
/data/apps/<slug>/ source dir) must UPDATE its existing row, not create a
duplicate. Matching on the display name regressed here because the name is
exactly the field a rename changes; matching on source_dir (stable across a
rename) is the fix.
"""

import importlib.util
import json
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


def test_migration_matches_explicit_legacy_source_dir():
  """A migration hint can match a row at an older source tree without creating
  a duplicate."""
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


def test_migration_ignores_unrelated_legacy_source_dir():
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
  """Registering an existing source dir should carry display renames."""
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


def test_registration_reads_runtime_capabilities_from_adjacent_manifest(
  monkeypatch, tmp_path,
):
  """Local-first registration carries the same declaration as Store install."""
  mod = _load_module()
  entry = tmp_path / "index.jsx"
  entry.write_text("export default function App() { return null }")
  declared = {
    "media.microphone.capture": {
      "version": 1,
      "reason": "Record a sound",
      "limits": {"max_duration_ms": 8000},
    },
  }
  (tmp_path / "mobius.json").write_text(json.dumps({
    "capabilities": declared,
  }))
  posts = []

  def fake_call(url, token, method, data=None):
    if method == "GET":
      return []
    if method == "POST" and url.endswith("/api/apps/"):
      posts.append(data)
      return {"id": 9}
    raise AssertionError(f"unexpected call: {method} {url}")

  monkeypatch.setenv("AGENT_TOKEN", "token")
  monkeypatch.setattr(sys, "argv", [
    "register_app.py", "Recorder", "Records", str(entry),
  ])
  monkeypatch.setattr(mod, "_call", fake_call)
  monkeypatch.setattr(mod, "_notify", lambda *_args, **_kwargs: None)

  mod.main()

  assert posts[0]["capabilities"] == declared


def test_registration_applies_offline_capable_from_adjacent_manifest(
  monkeypatch, tmp_path,
):
  """The live app row should match the local manifest without a later PATCH."""
  mod = _load_module()
  entry = tmp_path / "index.jsx"
  entry.write_text("export default function App() { return null }")
  (tmp_path / "mobius.json").write_text(json.dumps({
    "offline_capable": True,
  }))
  posts = []

  def fake_call(url, token, method, data=None):
    if method == "GET":
      return []
    if method == "POST" and url.endswith("/api/apps/"):
      posts.append(data)
      return {"id": 9}
    raise AssertionError(f"unexpected call: {method} {url}")

  monkeypatch.setenv("AGENT_TOKEN", "token")
  monkeypatch.setattr(sys, "argv", [
    "register_app.py", "Offline", "Works offline", str(entry),
  ])
  monkeypatch.setattr(mod, "_call", fake_call)
  monkeypatch.setattr(mod, "_notify", lambda *_args, **_kwargs: None)

  mod.main()

  assert posts[0]["offline_capable"] is True
  assert posts[0]["capabilities"] == {}


def test_registration_omits_offline_capable_when_manifest_omits_it(
  monkeypatch, tmp_path,
):
  """Re-registering a legacy manifest must not reset its existing live flag."""
  mod = _load_module()
  entry = tmp_path / "index.jsx"
  entry.write_text("export default function App() { return null }")
  (tmp_path / "mobius.json").write_text("{}")
  patches = []

  def fake_call(url, token, method, data=None):
    if method == "GET":
      return [{
        "id": 7,
        "name": "Legacy",
        "source_dir": str(tmp_path),
      }]
    if method == "PATCH":
      patches.append(data)
      return {"id": 7}
    raise AssertionError(f"unexpected call: {method} {url}")

  monkeypatch.setenv("AGENT_TOKEN", "token")
  monkeypatch.setattr(sys, "argv", [
    "register_app.py", "Legacy", "Existing app", str(entry),
  ])
  monkeypatch.setattr(mod, "_call", fake_call)
  monkeypatch.setattr(mod, "_notify", lambda *_args, **_kwargs: None)

  mod.main()

  assert "offline_capable" not in patches[0]


def test_registration_rejects_non_boolean_offline_capable(tmp_path, capsys):
  mod = _load_module()
  (tmp_path / "mobius.json").write_text(json.dumps({
    "offline_capable": "yes",
  }))

  try:
    mod._read_manifest_registration(str(tmp_path))
  except SystemExit as exc:
    assert exc.code == 1
  else:
    raise AssertionError("invalid offline_capable must fail registration")

  assert "must be true or false" in capsys.readouterr().err


def test_registration_rejects_empty_capability_array(tmp_path, capsys):
  """Falsey non-object declarations must not silently become no capabilities."""
  mod = _load_module()
  (tmp_path / "mobius.json").write_text(json.dumps({
    "capabilities": [],
  }))

  try:
    mod._read_manifest_registration(str(tmp_path))
  except SystemExit as exc:
    assert exc.code == 1
  else:
    raise AssertionError("capabilities must remain an object")

  assert "must be an object" in capsys.readouterr().err
