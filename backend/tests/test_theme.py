import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-exactly-32-chars!!")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/mobius_test/test.db")
os.environ.setdefault("DATA_DIR", "/tmp/mobius_test")
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:5173")

from app.theme import (
  theme_data,
  extract_imports,
  _ensure_core_vars,
  get_theme_css,
  snapshot_theme_if_present,
  reset_theme_override,
  EffectiveTheme,
)


def test_extract_imports_splits_imports_from_css():
  css = (
    "@import url('https://fonts.googleapis.com/css2?family=Poppins');\n"
    "@import url(\"https://fonts.googleapis.com/css2?family=Fira+Code\");\n"
    ":root { --font: 'Poppins', sans-serif; }\n"
  )
  imports, remaining = extract_imports(css)
  assert imports == [
    "https://fonts.googleapis.com/css2?family=Poppins",
    "https://fonts.googleapis.com/css2?family=Fira+Code",
  ]
  assert "@import" not in remaining
  assert "--font" in remaining


def test_extract_imports_no_imports():
  css = ":root { --bg: #fff; }"
  imports, remaining = extract_imports(css)
  assert imports == []
  assert remaining == css


def test_ensure_core_vars_injects_missing():
  """If the theme defines only some variables, missing core
  variables get filled in from DEFAULT_THEME so the shell can't
  fall back to an invisible hardcoded literal like #111."""
  css = ":root { --accent: #ff00aa; }"
  out = _ensure_core_vars(css)
  # Original kept verbatim.
  assert "--accent: #ff00aa" in out
  # Missing variables injected.
  assert "--bg:" in out
  assert "--text:" in out
  assert "--surface:" in out


def test_ensure_core_vars_skips_when_all_present():
  """A complete theme stays byte-identical."""
  css = """\
:root {
  --bg: #000;
  --surface: #111;
  --surface2: #222;
  --text: #fff;
  --muted: #999;
  --accent: #f0f;
  --accent-hover: #faf;
  --accent-dim: rgba(255, 0, 255, 0.1);
  --border: #333;
  --border-light: #444;
  --danger: #f00;
  --green: #0f0;
  --font: sans-serif;
  --mono: monospace;
}
"""
  assert _ensure_core_vars(css) == css


def test_ensure_core_vars_leaves_creative_css_alone():
  """Anything other than variable injection is a no-op: blur,
  translucent surfaces, fixed-position overlays, global focus
  rules, animations — all preserved. The agent has creative
  freedom; readability is a documentation concern, not a
  server-side rewrite."""
  css = """\
:root {
  --bg: #082015;
  --surface: rgba(20, 60, 38, 0.78);
  --surface2: rgba(28, 78, 50, 0.88);
  --text: #f0e6c8;
  --muted: #9bb3a3;
  --accent: #d4a437;
  --accent-hover: #f0c451;
  --accent-dim: rgba(212, 164, 55, 0.14);
  --border: #2d6e47;
  --border-light: #1a4d2e;
  --danger: #c4554e;
  --green: #1a8c4a;
  --font: 'Inter', sans-serif;
  --mono: monospace;
}
.sidenav { backdrop-filter: blur(8px); }
.hero { filter: blur(4px); }
body::before { position: fixed; inset: 0; z-index: 10; }
input:focus-visible { outline: 2px solid red; }
"""
  out = _ensure_core_vars(css)
  # Output is byte-identical because all core vars are defined.
  assert out == css


# /api/theme endpoint tests --------------------------------------------------
# The endpoint returns the *effective* theme — user override if present,
# DEFAULT_THEME otherwise. Lets the agent reset to defaults via DELETE on
# the storage URL without writing a complete default block in JS.

def test_api_theme_returns_default_when_no_override(client, auth):
  """No theme.css → endpoint returns DEFAULT_THEME and default --bg."""
  from app.theme import DEFAULT_THEME
  res = client.get("/api/theme", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert body["css"] == DEFAULT_THEME
  # DEFAULT_THEME has --bg: #0d0d0d (neutralized 2026-05).
  assert body["bg"] == "#0d0d0d"


def test_api_theme_returns_user_override_augmented(client, auth):
  """User-written theme.css → endpoint returns it, AUGMENTED with any
  core vars it omitted. The effective theme is always complete so the
  SPA's last-wins client re-apply can't drop accents (the "light mode
  completely broken" regression). The override is preserved verbatim
  inside the augmented CSS."""
  import os
  data_dir = os.environ["DATA_DIR"]
  shared = os.path.join(data_dir, "shared")
  os.makedirs(shared, exist_ok=True)
  custom = ":root { --bg: #ff0000; --accent: #00ff00; }"
  with open(os.path.join(shared, "theme.css"), "w") as f:
    f.write(custom)
  try:
    res = client.get("/api/theme", headers=auth)
    assert res.status_code == 200
    body = res.json()
    # The override is preserved verbatim...
    assert "--bg: #ff0000" in body["css"]
    assert "--accent: #00ff00" in body["css"]
    # ...and the omitted core vars are filled so nothing renders invisibly.
    assert "--surface:" in body["css"]
    assert "--text:" in body["css"]
    assert "--danger:" in body["css"]
    assert body["bg"] == "#ff0000"
  finally:
    os.remove(os.path.join(shared, "theme.css"))


def test_get_theme_css_augments_accent_stripped_override(tmp_path):
  """The "light mode completely broken" regression: a theme.css that a
  prior light/dark toggle stripped down to ONLY structural tokens (no
  --accent/--accent-hover/--accent-dim/--danger/--green) must come back
  from get_theme_css with those filled. Otherwise the SPA fetches the
  raw incomplete CSS from /api/theme, applies it last in the cascade,
  and every `var(--accent)` reference resolves to nothing."""
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "theme.css").write_text(
    ":root {\n"
    "  --bg: #f0eeeb;\n  --surface: #ffffff;\n  --surface2: #e8e6e2;\n"
    "  --border: #d4d1cc;\n  --border-light: #e2dfdb;\n"
    "  --text: #1c1b1a;\n  --muted: #6b6864;\n"
    "  color-scheme: light;\n}\n"
  )
  out = get_theme_css(str(tmp_path))
  assert "--bg: #f0eeeb" in out  # the override's structural tokens survive
  for token in (
    "--accent:", "--accent-hover:", "--accent-dim:", "--danger:", "--green:",
  ):
    assert token in out, f"{token} must be filled by get_theme_css; got:\n{out}"


def test_ensure_core_vars_fills_light_theme_with_light_defaults():
  """A partial LIGHT theme.css (light --bg, but missing structural
  tokens) must be gap-filled with LIGHT defaults, not the DARK palette
  from DEFAULT_THEME. The bug: _ensure_core_vars sourced every default
  from DEFAULT_THEME (dark), so a hand-written / toggle-stripped light
  theme got --surface2:#212121 + --border-light:#1f1f1f injected in a
  cascade-winning :root block — dark surfaces in light mode."""
  # Light --bg + --text only; every other structural token is missing
  # and must be filled from the LIGHT palette.
  css = ":root {\n  --bg: #f0eeeb;\n  --text: #1c1b1a;\n}\n"
  out = _ensure_core_vars(css)
  # Mode-dependent structural defaults must be the LIGHT values.
  assert "--surface: #ffffff" in out
  assert "--surface2: #e8e6e2" in out
  assert "--border: #d4d1cc" in out
  assert "--border-light: #e2dfdb" in out
  assert "--muted: #6b6864" in out
  # The DARK structural literals must NOT leak in.
  assert "#212121" not in out  # dark --surface2
  assert "#1f1f1f" not in out  # dark --border-light
  assert "#171717" not in out  # dark --surface
  # Mode-agnostic brand accent stays the shared purple in either mode.
  assert "--accent: #8b6cf7" in out


def test_ensure_core_vars_dark_theme_unchanged_by_mode_awareness():
  """A partial DARK theme still gets DARK defaults — the mode-aware
  branch must not regress the original behavior. A dark --bg infers
  'dark', so missing structural vars come from DEFAULT_THEME."""
  css = ":root {\n  --bg: #0d0d0d;\n  --text: #ececec;\n}\n"
  out = _ensure_core_vars(css)
  assert "--surface: #171717" in out
  assert "--surface2: #212121" in out
  assert "--border-light: #1f1f1f" in out
  # No light-mode value leaked into a dark theme.
  assert "#ffffff" not in out  # light --surface
  assert "#e8e6e2" not in out  # light --surface2


def test_ensure_core_vars_light_4digit_rgba_bg():
  """A 4-digit #RGBA light --bg (#ffff = opaque white) must infer LIGHT.
  The previous _infer_theme_mode only expanded 3-digit hex, so a valid
  4-digit value parsed as garbage, fell to the dark default, and injected
  the DARK palette into a light theme — diverging from the frontend, which
  classifies #ffff as light."""
  css = ":root {\n  --bg: #ffff;\n  --text: #1c1b1a;\n}\n"
  out = _ensure_core_vars(css)
  # LIGHT structural defaults must be injected.
  assert "--surface: #ffffff" in out
  assert "--surface2: #e8e6e2" in out
  # The DARK structural literals must NOT leak in.
  assert "#212121" not in out  # dark --surface2
  assert "#171717" not in out  # dark --surface


def test_ensure_core_vars_light_8digit_rrggbbaa_bg():
  """An 8-digit #RRGGBBAA light --bg (#ffffffff = opaque white) must infer
  LIGHT — the leading six hex chars are RGB and the trailing alpha byte is
  dropped, matching the frontend's slice(0, 6)."""
  css = ":root {\n  --bg: #ffffffff;\n  --text: #1c1b1a;\n}\n"
  out = _ensure_core_vars(css)
  assert "--surface: #ffffff" in out
  assert "--surface2: #e8e6e2" in out
  assert "#212121" not in out  # dark --surface2
  assert "#171717" not in out  # dark --surface


def test_ensure_core_vars_dark_4digit_rgba_bg():
  """A 4-digit #RGBA DARK --bg (#000f = opaque black) must still infer
  DARK — the alpha nibble is dropped and the RGB nibbles read as black."""
  css = ":root {\n  --bg: #000f;\n  --text: #ececec;\n}\n"
  out = _ensure_core_vars(css)
  assert "--surface: #171717" in out
  assert "--surface2: #212121" in out
  # No light-mode value leaked into a dark theme.
  assert "#ffffff" not in out  # light --surface
  assert "#e8e6e2" not in out  # light --surface2


def test_ensure_core_vars_missing_bg_defaults_to_dark():
  """A theme with no --bg at all (can't infer mode) defaults to DARK
  defaults — the historical behavior, preserved so existing themes are
  unaffected."""
  css = ":root {\n  --accent: #ff00aa;\n}\n"
  out = _ensure_core_vars(css)
  assert "--surface2: #212121" in out  # dark default
  assert "--bg: #0d0d0d" in out


def test_api_theme_returns_default_when_override_is_empty(client, auth):
  """Empty theme.css → endpoint falls back to DEFAULT_THEME."""
  import os
  from app.theme import DEFAULT_THEME
  data_dir = os.environ["DATA_DIR"]
  shared = os.path.join(data_dir, "shared")
  os.makedirs(shared, exist_ok=True)
  with open(os.path.join(shared, "theme.css"), "w") as f:
    f.write("")
  try:
    res = client.get("/api/theme", headers=auth)
    assert res.status_code == 200
    assert res.json()["css"] == DEFAULT_THEME
  finally:
    os.remove(os.path.join(shared, "theme.css"))


def test_api_theme_reset_via_delete(client, auth):
  """Agent's reset path: DELETE the storage URL → /api/theme returns
  defaults. End-to-end verification that the architecture works."""
  from app.theme import DEFAULT_THEME

  # Set a custom theme via the storage API.
  custom = ":root { --bg: #123456; }"
  res = client.put(
    "/api/storage/shared/theme.css",
    headers=auth,
    json={"content": custom},
  )
  assert res.status_code in (200, 201, 204)

  # Verify endpoint returns the override (augmented; preserved verbatim).
  body = client.get("/api/theme", headers=auth).json()
  assert "--bg: #123456" in body["css"]

  # Reset by deleting.
  res = client.delete("/api/storage/shared/theme.css", headers=auth)
  assert res.status_code in (200, 204)

  # Endpoint now returns defaults.
  body = client.get("/api/theme", headers=auth).json()
  assert body["css"] == DEFAULT_THEME


def test_api_theme_requires_auth(client):
  """Unauth requests return 401 — theme isn't a public endpoint."""
  res = client.get("/api/theme")
  assert res.status_code == 401


# theme_data() — the theme-as-data bundle the JSON slot serializes -----------
# Replaces the old inject_theme_into_html <style> path. theme_data returns
# the effective {css, bg, mode}; main.py serializes it into the page's
# __mobius-theme__ slot (slot-XSS escaping is covered in
# test_index_theme_slot.py against the real GET / path).

def test_theme_data_returns_default_bundle(tmp_path):
  """No theme.css → theme_data returns DEFAULT_THEME css, default --bg, dark."""
  from app.theme import DEFAULT_THEME
  shared = tmp_path / "shared"
  shared.mkdir()
  d = theme_data(str(tmp_path))
  assert d == {"css": DEFAULT_THEME, "bg": "#0d0d0d", "mode": "dark"}


def test_theme_data_returns_light_override(tmp_path):
  """A light theme.css → theme_data reports its --bg and infers light mode
  (no theme-mode file needed; get_theme_mode falls back to dark, but the
  bundle's mode comes from theme-mode while bg comes from the css). Here we
  assert the css is augmented + the bg is the override's --bg."""
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "theme.css").write_text(":root { --bg: #f0eeeb; --text: #1c1b1a; }")
  d = theme_data(str(tmp_path))
  assert d["bg"] == "#f0eeeb"
  assert "--bg: #f0eeeb" in d["css"]
  # mode comes from the theme-mode file (absent → dark default); bg-derived
  # mode is the client's job. Server bundle mode is dark when no theme-mode.
  assert d["mode"] == "dark"


def test_theme_data_mode_from_theme_mode_file(tmp_path):
  """theme_data's mode reflects /data/shared/theme-mode (light)."""
  import json as _json
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "theme.css").write_text(":root { --bg: #f0eeeb; }")
  (shared / "theme-mode").write_text(_json.dumps("light"))
  d = theme_data(str(tmp_path))
  assert d["mode"] == "light"
  assert d["bg"] == "#f0eeeb"


def test_theme_data_accepts_precomputed_bundle(tmp_path):
  """Passing a bundle skips the file reads and returns its fields verbatim."""
  bundle = EffectiveTheme(css=":root{--bg:#123456;}", bg="#123456", mode="dark", rev="x")
  d = theme_data(str(tmp_path), bundle=bundle)
  assert d == {"css": ":root{--bg:#123456;}", "bg": "#123456", "mode": "dark"}


def test_theme_data_passes_css_verbatim_no_style_escape(tmp_path):
  """Unlike the old injection, theme_data does NOT </style>-escape the css —
  the client injects it via <style>.textContent (never reparsed), so the css
  is the effective css verbatim. (The slot's own </script> breakout is
  escaped by main.py's serializer, covered in test_index_theme_slot.py.)"""
  shared = tmp_path / "shared"
  shared.mkdir()
  css = ":root { --bg: #0d0d0d; content: '</style>'; }"
  (shared / "theme.css").write_text(css)
  d = theme_data(str(tmp_path))
  # The </style> survives verbatim in the css value (augment may append vars).
  assert "</style>" in d["css"]


# Recovery affordance tests ------------------------------------------------
# Three changes ship together: auto-snapshot on theme write,
# `reset_theme_override` helper, and `POST /api/theme/reset`. Each
# is verified independently so a regression in one doesn't mask
# breakage in another.

def test_snapshot_theme_creates_backup_when_present(tmp_path):
  """An existing theme.css produces a theme.css.bak-<ts> sibling."""
  shared = tmp_path / "shared"
  shared.mkdir()
  src = shared / "theme.css"
  src.write_text(":root { --bg: #ff0000; }")
  backup = snapshot_theme_if_present(str(tmp_path))
  assert backup is not None
  bp = type(src)(backup)
  assert bp.exists()
  assert bp.name.startswith("theme.css.bak-")
  # Backup contains the same bytes as the source pre-snapshot.
  assert bp.read_text() == ":root { --bg: #ff0000; }"
  # Source is left untouched — snapshot is copy, not move.
  assert src.exists()
  assert src.read_text() == ":root { --bg: #ff0000; }"


def test_snapshot_theme_no_op_when_missing(tmp_path):
  """A missing theme.css returns None without raising."""
  (tmp_path / "shared").mkdir()
  assert snapshot_theme_if_present(str(tmp_path)) is None


def test_reset_theme_renames_to_reset_bak(tmp_path):
  """reset_theme_override moves theme.css aside, returns the backup path."""
  shared = tmp_path / "shared"
  shared.mkdir()
  src = shared / "theme.css"
  src.write_text(":root { --bg: #00ff00; }")
  result = reset_theme_override(str(tmp_path))
  assert result["reset"] is True
  backup_path = type(src)(result["backup"])
  assert backup_path.exists()
  assert backup_path.name.startswith("theme.css.reset-bak-")
  assert backup_path.read_text() == ":root { --bg: #00ff00; }"
  # Source is gone — DEFAULT_THEME would paint on next read.
  assert not src.exists()


def test_reset_theme_idempotent_when_no_override(tmp_path):
  """reset_theme_override with no theme.css reports no-op."""
  (tmp_path / "shared").mkdir()
  result = reset_theme_override(str(tmp_path))
  assert result == {"reset": False, "reason": "no override"}


def test_api_theme_reset_endpoint_idempotent(client, auth):
  """POST /api/theme/reset with no override returns reset=False."""
  res = client.post("/api/theme/reset", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert body["reset"] is False
  assert "reason" in body


def test_theme_reset_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/theme/reset",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_api_theme_reset_endpoint_creates_backup(client, auth):
  """POST /api/theme/reset moves the override to a reset-bak file
  and subsequent GET /api/theme returns DEFAULT_THEME."""
  import os
  from app.theme import DEFAULT_THEME
  data_dir = os.environ["DATA_DIR"]
  shared = os.path.join(data_dir, "shared")
  os.makedirs(shared, exist_ok=True)
  custom = ":root { --bg: #abcdef; }"
  with open(os.path.join(shared, "theme.css"), "w") as f:
    f.write(custom)
  try:
    res = client.post("/api/theme/reset", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["reset"] is True
    assert "reset-bak-" in body["backup"]
    # Backup file actually exists on disk.
    assert os.path.exists(body["backup"])
    # The override is gone — GET /api/theme falls back to defaults.
    assert client.get("/api/theme", headers=auth).json()["css"] == DEFAULT_THEME
  finally:
    # Cleanup: remove backup + any stale source.
    for entry in os.listdir(shared):
      if entry.startswith("theme.css"):
        os.remove(os.path.join(shared, entry))


def test_api_theme_reset_requires_auth(client):
  """Unauth POST to /api/theme/reset returns 401."""
  res = client.post("/api/theme/reset")
  assert res.status_code == 401


def test_storage_write_theme_css_auto_snapshots(client, auth):
  """Writing to /api/storage/shared/theme.css snapshots the prior
  version automatically. First write has no prior — no backup; the
  SECOND write produces theme.css.bak-<ts>."""
  import os
  data_dir = os.environ["DATA_DIR"]
  shared = os.path.join(data_dir, "shared")
  os.makedirs(shared, exist_ok=True)
  # Clear any leftover bak files from prior tests.
  for entry in list(os.listdir(shared)):
    if entry.startswith("theme.css"):
      os.remove(os.path.join(shared, entry))
  try:
    # First write: no prior, no backup expected.
    res = client.put(
      "/api/storage/shared/theme.css",
      headers=auth,
      json={"content": ":root { --bg: #111111; }"},
    )
    assert res.status_code in (200, 204)
    baks = [
      e for e in os.listdir(shared)
      if e.startswith("theme.css.bak-")
    ]
    assert baks == []
    # Second write: prior exists, snapshot must fire.
    res = client.put(
      "/api/storage/shared/theme.css",
      headers=auth,
      json={"content": ":root { --bg: #222222; }"},
    )
    assert res.status_code in (200, 204)
    baks = [
      e for e in os.listdir(shared)
      if e.startswith("theme.css.bak-")
    ]
    assert len(baks) == 1
    # Backup contains the FIRST write's contents — the snapshot
    # fires before the new write lands.
    bak_path = os.path.join(shared, baks[0])
    with open(bak_path) as f:
      assert f.read() == ":root { --bg: #111111; }"
  finally:
    for entry in list(os.listdir(shared)):
      if entry.startswith("theme.css"):
        os.remove(os.path.join(shared, entry))



def test_snapshot_theme_prunes_old_backups(tmp_path):
  """Only the newest THEME_SNAPSHOT_KEEP snapshots survive.

  Ordering must come from the filename timestamp, not mtime — copy2
  preserves the source mtime, which reflects when the theme was
  edited, not when it was snapshot.
  """
  from app.theme import THEME_SNAPSHOT_KEEP

  shared = tmp_path / "shared"
  shared.mkdir()
  src = shared / "theme.css"
  src.write_text(":root {}")
  for ts in range(1000, 1000 + THEME_SNAPSHOT_KEEP + 3):
    (shared / f"theme.css.bak-{ts}").write_text("old")
  # A hand-made backup with a non-numeric suffix is never pruned.
  keeper = shared / "theme.css.bak-manual"
  keeper.write_text("mine")

  snapshot_theme_if_present(str(tmp_path))

  numeric = sorted(
    p.name for p in shared.glob("theme.css.bak-*") if p != keeper
  )
  assert len(numeric) == THEME_SNAPSHOT_KEEP
  # The oldest seeded snapshots are gone; the newest survive.
  assert "theme.css.bak-1000" not in numeric
  assert keeper.exists()
