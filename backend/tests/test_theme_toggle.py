"""The light/dark switch: `toggled_theme_css` and `POST /api/theme/mode`.

A toggle swaps the colours that belong to a mode and keeps every choice of
the owner's; the shell paints `GET /api/theme`'s `toggle` preview on tap, so
the preview must be exactly what the save then serves.
"""

import os
import re
from pathlib import Path

import pytest

from app import theme
from app.theme import _ensure_core_vars, _served_theme, toggled_theme_css

DARK = theme._base_colors("dark")
LIGHT = theme._base_colors("light")
ROLES = ("--accent-text", "--accent-fill", "--accent-fill-hover", "--danger-text",
         "--danger-fill", "--green-text", "--green-fill", "--warning-text")
DARK_ROLES = {k: theme._shipped_palette("dark")[k] for k in ROLES}
LIGHT_ROLES = {k: theme._shipped_palette("light")[k] for k in ROLES}

CUSTOM_DARK = """:root {
  --bg: #0d0f14;
  --surface: #151820;
  --surface-2: #1c2028;
  --border: #2a2f3a;
  --border-light: #1e2330;
  --text: #d8d8dc;
  --muted: #6b6b76;
  --accent: #ff00ff;
  --font: 'Inter', sans-serif;
  --mono: 'JetBrains Mono', monospace;
  font-size: 15px;
  color-scheme: dark;
}
"""


def shipped_css(colors: dict[str, str], mode: str) -> str:
  lines = "\n".join(f"  {k}: {v};" for k, v in colors.items())
  return (f":root {{\n  /* Colors - {mode} theme */\n{lines}\n  --font: 'Inter', sans-serif;\n"
          f"  --mono: monospace;\n  font-size: 15px;\n  color-scheme: {mode};\n}}\n")


def root_var(css: str, name: str) -> str | None:
  m = re.search(rf"(?:^|[\s;{{]){re.escape(name)}\s*:\s*([^;]+);", css)
  return m.group(1).strip() if m else None


def toggle_once(css: str, from_mode: str) -> tuple[str, str]:
  """(persisted theme.css, painted css) for toggling a saved `css`, read the
  way the server reads it."""
  new_mode = "light" if from_mode == "dark" else "dark"
  persisted = toggled_theme_css(_ensure_core_vars(css.strip()), from_mode, new_mode)
  return persisted, _served_theme(persisted)["css"]


# --- what a toggle writes ----------------------------------------------------


def test_structural_colours_swap_and_a_custom_accent_survives():
  persisted, painted = toggle_once(CUSTOM_DARK.replace("#ff00ff", "#abcdef"), "dark")
  assert root_var(persisted, "--bg") == "#f0eeeb"
  assert root_var(persisted, "--accent") == "#abcdef"
  assert theme._theme_bg(painted) == "#f0eeeb"
  persisted, painted = toggle_once(CUSTOM_DARK.replace("#0d0f14", "#f0eeeb"), "light")
  assert theme._theme_bg(painted) == "#0d0d0d"


def test_selectors_containing_double_dashes_stay_selectors():
  css = CUSTOM_DARK + """
  .settings__section--server,
  .settings .settings__section.settings__section--compact:last-child .row { gap: 14px; }
  """
  persisted, _ = toggle_once(css, "dark")
  assert ".settings__section.settings__section--compact:last-child" in persisted
  assert not re.search(r"^\s*--compact\s*:", persisted, re.M)


def test_a_stripped_theme_gets_its_missing_tokens_back():
  stripped = shipped_css({k: v for k, v in LIGHT.items() if k in (
    "--bg", "--surface", "--surface-2", "--border", "--border-light", "--text", "--muted")}, "light")
  persisted, _ = toggle_once(stripped, "light")
  for token in ("--accent", "--accent-hover", "--accent-dim", "--danger", "--green"):
    assert root_var(persisted, token), token
  assert root_var(persisted, "--bg") == "#0d0d0d"


def test_the_label_follows_the_kept_accent_and_is_painted_not_saved():
  persisted, painted = toggle_once(CUSTOM_DARK.replace("#ff00ff", "#ffffff"), "dark")
  assert root_var(persisted, "--accent") == "#ffffff"
  assert "--accent-fg: #000000;" in painted
  assert "--accent-fg" not in persisted


def test_a_declared_label_is_kept():
  persisted, _ = toggle_once(CUSTOM_DARK.replace("--accent: #ff00ff", "--accent: #ffffff; --accent-fg: #123456"), "dark")
  assert root_var(persisted, "--accent-fg") == "#123456"


def test_a_round_trip_returns_every_shipped_default():
  to_light, _ = toggle_once(shipped_css(DARK, "dark"), "dark")
  assert {k: root_var(to_light, k) for k in LIGHT} == LIGHT
  to_dark, _ = toggle_once(to_light, "light")
  assert {k: root_var(to_dark, k) for k in DARK} == DARK


def test_the_other_modes_shipped_values_are_swapped_not_kept_as_custom():
  carried = shipped_css({**DARK, **{k: LIGHT[k] for k in ("--danger", "--green", "--accent-dim")}}, "dark")
  to_light, _ = toggle_once(carried, "dark")
  assert root_var(to_light, "--danger") == LIGHT["--danger"]
  to_dark, _ = toggle_once(to_light, "light")
  for key in ("--danger", "--green", "--accent-dim"):
    assert root_var(to_dark, key) == DARK[key]


def test_custom_values_survive_both_directions():
  custom = shipped_css({**DARK, "--danger": "#ff2d55", "--accent-text": "#c4b5fd",
                        "--brand-glow": "rgba(1, 2, 3, 0.5)"}, "dark")
  to_light, _ = toggle_once(custom, "dark")
  to_dark, _ = toggle_once(to_light, "light")
  for css in (to_light, to_dark):
    assert root_var(css, "--danger") == "#ff2d55"
    assert root_var(css, "--accent-text") == "#c4b5fd"
    assert root_var(css, "--brand-glow") == "rgba(1, 2, 3, 0.5)"


def test_roles_are_painted_for_the_new_mode_and_not_saved():
  persisted, painted = toggle_once(shipped_css(DARK, "dark"), "dark")
  block = painted[painted.index(theme.INJECTED_DEFAULTS_MARKER):]
  for key, value in LIGHT_ROLES.items():
    assert root_var(block, key) == value, key
    assert root_var(persisted, key) is None, key
  assert root_var(persisted, "--accent-fg") is None


def test_shipped_roles_saved_by_an_older_toggle_are_swapped():
  old = shipped_css({**LIGHT, **LIGHT_ROLES, "--accent-fg": "#ffffff"}, "light")
  persisted, painted = toggle_once(old, "light")
  assert root_var(persisted, "--accent-text") is None
  assert root_var(painted[painted.index(theme.INJECTED_DEFAULTS_MARKER):], "--accent-text") == DARK_ROLES["--accent-text"]


def test_a_role_frozen_at_its_derived_value_is_re_derived():
  teal = {**DARK, "--accent": "#0ea5e9"}
  frozen = theme.theme_role_vars(teal, "dark")["--accent-text"]
  assert frozen not in (DARK_ROLES["--accent-text"], LIGHT_ROLES["--accent-text"])
  persisted, _ = toggle_once(shipped_css({**teal, "--accent-text": frozen}, "dark"), "dark")
  assert root_var(persisted, "--accent-text") is None


def test_a_custom_accent_re_derives_its_roles_in_each_mode():
  teal = shipped_css({**DARK, "--accent": "#0ea5e9"}, "dark")
  to_light, light_paint = toggle_once(teal, "dark")
  to_dark, dark_paint = toggle_once(to_light, "light")
  light_text, dark_text = root_var(light_paint, "--accent-text"), root_var(dark_paint, "--accent-text")
  assert light_text and dark_text and light_text != dark_text
  assert root_var(to_dark, "--accent-text") is None
  assert root_var(to_dark, "--accent") == "#0ea5e9"


def test_an_older_generated_block_is_not_saved_as_owner_css():
  served = shipped_css(DARK, "dark") + (
    f".keep-me {{ gap: 4px; }}\n/* {theme.INJECTED_DEFAULTS_MARKER} */\n"
    ":root {\n  --accent-fg: #ffffff;\n  --accent-text: #957cf8;\n}\n")
  persisted, _ = toggle_once(served, "dark")
  assert ".keep-me { gap: 4px; }" in persisted
  assert theme.INJECTED_DEFAULTS_MARKER not in persisted
  assert theme.DEFAULTS_LAYER not in persisted


def test_an_owner_role_in_a_later_root_still_wins():
  css = shipped_css(DARK, "dark") + ".keep { gap: 4px; }\n:root { --accent: #00ffff; --accent-text: #abcdef; }\n"
  persisted, painted = toggle_once(css, "dark")
  assert ":root { --accent: #00ffff; --accent-text: #abcdef; }" in persisted
  assert painted.count(theme.INJECTED_DEFAULTS_MARKER) == 1
  assert painted.index(theme.INJECTED_DEFAULTS_MARKER) < painted.index("--accent-text: #abcdef")
  assert theme.scan_root_declarations(painted)["--accent-text"] == "#abcdef"


def test_the_root_rule_is_read_lexically():
  css = """:root {
  --glyph: "}";
  --sep: 'a;b';
  /* --accent: #ff0000; */
  --bg: #0d0d0d;
  --accent: #00ffff;
  font-size: 16px;
}
.keep { content: "{"; }
"""
  persisted, _ = toggle_once(css, "dark")
  assert root_var(persisted, "--accent") == "#00ffff"
  assert root_var(persisted, "--bg") == LIGHT["--bg"]
  assert '--glyph: "}";' in persisted and "--sep: 'a;b';" in persisted
  assert "font-size: 16px;" in persisted
  assert '.keep { content: "{"; }' in persisted
  assert "#ff0000" not in persisted
  assert persisted.count(":root") == 1


def test_css_the_lexer_cannot_follow_is_kept_verbatim():
  css = ':root { --bg: #0d0d0d; --accent: #00ffff; }\n.broken { content: "open;\n}\n'
  persisted, _ = toggle_once(css, "dark")
  assert css.strip() in persisted


def test_a_winning_important_declaration_keeps_its_importance():
  persisted, _ = toggle_once(":root { --bg: #0d0d0d; --accent: #00ffff !important; --accent: #ff0000; }\n", "dark")
  assert "--accent: #00ffff !important;" in persisted
  assert "#ff0000" not in persisted
  assert theme.scan_root_declarations(persisted)["--accent"] == "#00ffff"


def test_imports_stay_first():
  css = "@import url('https://fonts.example.test/a.css');\n" + CUSTOM_DARK
  persisted, painted = toggle_once(css, "dark")
  assert persisted.startswith("@import url('https://fonts.example.test/a.css');\n\n:root {")
  assert "@import url('https://fonts.example.test/a.css');" in painted


def test_every_import_spelling_stays_first():
  imports = ("@import url(https://fonts.example.test/a.css);\n"
             "@import \"https://fonts.example.test/b.css\";\n"
             "@import url(\"https://fonts.example.test/c.css\");\n")
  persisted, _ = toggle_once(imports + CUSTOM_DARK, "dark")
  assert persisted.startswith(imports + "\n:root {")
  assert persisted.count("@import") == 3


def test_only_real_leading_statements_are_kept_first():
  prelude = ("@charset \"utf-8\";\n/* @import url(https://fonts.example.test/off.css); */\n"
             "@import url(https://fonts.example.test/a.css) layer(fonts) screen;\n@layer base, theme;")
  rule = ".x::before { content: '@import \"b.css\";'; }"
  persisted, _ = toggle_once(f"{prelude}\n{CUSTOM_DARK}\n{rule}\n", "dark")
  assert persisted.startswith(prelude + "\n\n:root {")
  assert persisted.count("@import") == 3, "the comment and the string are not imports"
  assert persisted.rstrip().endswith(rule)
  assert "mobius-defaults" not in persisted


# --- the endpoint ------------------------------------------------------------


@pytest.fixture
def shared():
  path = Path(os.environ["DATA_DIR"]) / "shared"
  path.mkdir(parents=True, exist_ok=True)
  yield path
  for entry in path.iterdir():
    if entry.name.startswith(("theme.css", "theme-mode")):
      entry.unlink()


@pytest.fixture
def events(monkeypatch):
  published = []
  monkeypatch.setattr("app.routes.theme.publish_system_event", published.append)
  return published


def test_the_preview_is_what_the_save_serves(client, auth, shared, events):
  (shared / "theme.css").write_text(CUSTOM_DARK)
  preview = client.get("/api/theme", headers=auth).json()["toggle"]
  assert preview["mode"] == "light"
  saved = client.post("/api/theme/mode", headers=auth, json={"mode": "light"})
  assert saved.status_code == 200
  body = saved.json()
  assert {k: body[k] for k in ("css", "bg", "mode")} == preview
  assert client.get("/api/theme", headers=auth).json()["css"] == preview["css"]
  # And the next preview is the way back.
  assert body["toggle"]["mode"] == "dark"
  assert theme.scan_root_declarations(body["toggle"]["css"])["--accent"] == "#ff00ff"
  assert events == [{"type": "theme_updated"}]


def test_the_default_theme_toggles_to_the_shipped_light_theme(client, auth, shared, events):
  body = client.post("/api/theme/mode", headers=auth, json={"mode": "light"}).json()
  assert body["mode"] == "light" and body["bg"] == LIGHT["--bg"]
  saved = (shared / "theme.css").read_text()
  assert {k: root_var(saved, k) for k in LIGHT} == LIGHT
  assert (shared / "theme-mode").read_text() == '"light"'


def test_the_save_rewrites_what_is_on_disk_not_what_the_shell_loaded(client, auth, shared, events):
  (shared / "theme.css").write_text(CUSTOM_DARK)
  client.get("/api/theme", headers=auth)
  # An agent edits the theme before the shell has reloaded it.
  (shared / "theme.css").write_text(CUSTOM_DARK.replace("#ff00ff", "#00ffaa"))
  client.post("/api/theme/mode", headers=auth, json={"mode": "light"})
  saved = (shared / "theme.css").read_text()
  assert root_var(saved, "--accent") == "#00ffaa"
  assert root_var(saved, "--bg") == LIGHT["--bg"]
  assert any(p.name.startswith("theme.css.bak-") for p in shared.iterdir())


def test_a_failed_snapshot_does_not_block_the_switch(client, auth, shared, events, monkeypatch):
  (shared / "theme.css").write_text(CUSTOM_DARK)

  def fail(_data_dir):
    raise OSError("disk full")

  monkeypatch.setattr(theme, "snapshot_theme_if_present", fail)
  saved = client.post("/api/theme/mode", headers=auth, json={"mode": "light"})
  assert saved.status_code == 200 and saved.json()["mode"] == "light"
  assert root_var((shared / "theme.css").read_text(), "--bg") == LIGHT["--bg"]


def test_the_preview_is_built_once_per_theme(client, auth, shared, events, monkeypatch):
  (shared / "theme.css").write_text(CUSTOM_DARK)
  theme._toggle_preview.cache_clear()
  builds = []
  real = theme.toggled_theme_css
  monkeypatch.setattr(theme, "toggled_theme_css", lambda *a: builds.append(a) or real(*a))
  first = client.get("/api/theme", headers=auth).json()["toggle"]
  assert client.get("/api/theme", headers=auth).json()["toggle"] == first
  assert len(builds) == 1
  # A different size, so the stat-keyed theme memo sees the edit at once.
  (shared / "theme.css").write_text(CUSTOM_DARK.replace("#ff00ff", "#0fa"))
  assert client.get("/api/theme", headers=auth).json()["toggle"] != first
  assert len(builds) == 2


def test_a_failing_preview_never_stops_the_theme_loading(client, auth, shared, events, monkeypatch):
  (shared / "theme.css").write_text(CUSTOM_DARK)
  theme._toggle_preview.cache_clear()

  def fail(*_args):
    raise ValueError("unexpected css")

  monkeypatch.setattr(theme, "toggled_theme_css", fail)
  served = client.get("/api/theme", headers=auth)
  assert served.status_code == 200
  assert "toggle" not in served.json() and served.json()["mode"] == "dark"


def test_saving_the_current_mode_changes_nothing(client, auth, shared, events):
  (shared / "theme.css").write_text(CUSTOM_DARK)
  body = client.post("/api/theme/mode", headers=auth, json={"mode": "dark"}).json()
  assert (shared / "theme.css").read_text() == CUSTOM_DARK
  assert body["mode"] == "dark"
  assert events == []


def test_the_mode_endpoint_is_guarded(client, auth, shared, events):
  assert client.post("/api/theme/mode", json={"mode": "light"}).status_code == 401
  cross = client.post("/api/theme/mode", headers={**auth, "Sec-Fetch-Site": "cross-site"}, json={"mode": "light"})
  assert cross.status_code == 403
  assert client.post("/api/theme/mode", headers=auth, json={"mode": "sepia"}).status_code == 422
  assert not (shared / "theme.css").exists()
  assert events == []
