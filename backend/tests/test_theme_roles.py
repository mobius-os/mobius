"""Theme colour roles: text-safe and fill-safe tokens, their augmentation, and
parity between every place that spells the shipped palette.

The shipped palette is written three times on purpose (server default and
augmentation, the shell's toggle palettes, the app frame's no-signal
fallbacks) because each must paint before the others can be consulted. These
tests are what keeps the three copies identical, so a palette edit fails here
instead of flashing a stale colour in one surface.
"""

import json
import re
from pathlib import Path

import pytest

from app import theme
from app.theme import DEFAULT_THEME, _LIGHT_DEFAULTS, _ensure_core_vars

REPO = Path(__file__).resolve().parents[2]
THEME_JS = REPO / "frontend" / "src" / "theme.js"
APP_FRAME = REPO / "frontend" / "public" / "app-frame.html"
ROLE_CASES = REPO / "frontend" / "src" / "lib" / "__tests__" / "themeRoleCases.json"
CSS_CASES = REPO / "frontend" / "src" / "lib" / "__tests__" / "themeCssCases.json"

SURFACES = ("--bg", "--surface", "--surface-2")
TEXT_ROLES = ("--accent-text", "--danger-text", "--green-text", "--warning", "--warning-text")
FILL_ROLES = ("--accent-fill", "--accent-fill-hover", "--danger-fill", "--green-fill")
TYPOGRAPHY = ("--font", "--mono")


def _vars(block: str) -> dict[str, str]:
  return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def _js_object(name: str) -> dict[str, str]:
  source = THEME_JS.read_text(encoding="utf-8")
  m = re.search(rf"export const {name} = \{{(.*?)\n\}}", source, re.S)
  assert m, f"{name} not found in theme.js"
  return {k: v for k, v in re.findall(r"'(--[\w-]+)':\s*'([^']+)'", m.group(1))}


def _frame_blocks() -> tuple[dict[str, str], dict[str, str]]:
  html = APP_FRAME.read_text(encoding="utf-8")
  dark = re.search(r"\n\s*:where\(:root\) \{(.*?)\n\s*\}", html, re.S)
  light = re.search(r':where\(:root\[data-theme="light"\]\) \{(.*?)\}', html, re.S)
  assert dark and light
  frame_dark = {k: v for k, v in _vars(dark.group(1)).items() if not k.startswith("--mobius-")}
  return frame_dark, _vars(light.group(1))


def _shipped(mode: str) -> dict[str, str]:
  palette = _vars(DEFAULT_THEME)
  if mode == "light":
    palette.update(_LIGHT_DEFAULTS)
  return palette


def _contrast(a: str, b: str) -> float:
  la = theme._relative_luminance(theme._parse_css_rgb(a))
  lb = theme._relative_luminance(theme._parse_css_rgb(b))
  hi, lo = max(la, lb), min(la, lb)
  return (hi + 0.05) / (lo + 0.05)


# --- palette parity --------------------------------------------------------


@pytest.mark.parametrize("mode,colors,roles", [
  ("dark", "DARK_COLORS", "DARK_ROLES"),
  ("light", "LIGHT_COLORS", "LIGHT_ROLES"),
])
def test_server_palette_matches_the_shell_toggle_palette(mode, colors, roles):
  expected = {**_js_object(colors), **_js_object(roles), "--accent-fg": "#ffffff"}
  served = {k: v for k, v in _shipped(mode).items() if k not in TYPOGRAPHY}
  assert served == expected


def test_generated_typography_matches_the_shell():
  source = THEME_JS.read_text(encoding="utf-8")
  block = re.search(r"export const THEME_TYPOGRAPHY = \{(.*?)\n\}", source, re.S).group(1)
  typography = dict(re.findall(r"'(--[\w-]+)':\s*\"([^\"]+)\"", block))
  assert typography == {k: _shipped("dark")[k] for k in TYPOGRAPHY}


def test_app_frame_fallbacks_match_the_server_palette():
  frame_dark, frame_light = _frame_blocks()
  assert frame_dark == _vars(DEFAULT_THEME)
  assert {**frame_dark, **frame_light} == _shipped("light")


def test_injected_defaults_marker_matches_the_shell_parser():
  """The shell drops a previously served injected block when it rebuilds
  theme.css; it finds that block by this exact comment."""
  m = re.search(r"'(Möbius: injected defaults[^']*)'", THEME_JS.read_text(encoding="utf-8"))
  assert m and m.group(1) == theme.INJECTED_DEFAULTS_MARKER


# --- shipped contrast --------------------------------------------------------


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_shipped_text_roles_read_on_every_surface(mode):
  palette = _shipped(mode)
  for role in TEXT_ROLES:
    for surface in SURFACES:
      ratio = _contrast(palette[role], palette[surface])
      assert ratio >= 4.5, f"{mode} {role} on {surface}: {ratio:.2f}"


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_shipped_fills_carry_the_accent_foreground_for_small_text(mode):
  palette = _shipped(mode)
  for role in FILL_ROLES:
    ratio = _contrast(palette["--accent-fg"], palette[role])
    assert ratio >= 4.5, f"{mode} --accent-fg on {role}: {ratio:.2f}"


def test_brand_accent_is_unchanged():
  assert _shipped("dark")["--accent"] == "#8b6cf7"
  assert _shipped("light")["--accent"] == "#8b6cf7"


# --- the generated defaults block and the cascade ----------------------------
#
# Contract: the served theme is the owner's CSS preceded by one generated
# `:where(:root)` block of every core variable. `_resolve_root` applies the
# effective CSS's root-targeting rules in cascade order (importance, then
# specificity, then source order), which is what proves that owner
# declarations always win and every core variable still resolves -- rather
# than trusting either implementation's own reading of the CSS.

_ROOT_SPECIFICITY = {":where(:root)": 0, "html": 1, ":root": 10}


def _resolve_root(css: str, conditions_match: bool = False) -> dict[str, str]:
  """Root custom properties after the cascade, for the rule shapes the
  fixtures use: cascade layers (named, anonymous, nested, order statements
  and `@import ... layer(x)`), @media/@supports (all matching or none),
  `:where(:root)`, `html` and `:root`.  Precedence for normal declarations:
  unlayered beats any layer, a later-declared layer beats an earlier one,
  then specificity, then source order; !important beats normal."""
  text, mask, ok = theme._lex_theme_css(css)
  assert ok, "fixture must be lexable for the cascade oracle"
  layer_order: list[str] = []
  decls: list[tuple] = []
  anonymous = iter(range(10_000))

  def declare(name: str) -> None:
    if name not in layer_order:
      layer_order.append(name)

  def walk(start: int, end: int, layer: str | None) -> None:
    i = segment = start
    while i < end:
      if mask[i] == ";":
        statement = " ".join(text[segment:i].split())
        m = re.fullmatch(r"@layer (.+)", statement)
        if m:
          for name in m.group(1).split(","):
            declare(f"{layer}.{name.strip()}" if layer else name.strip())
        m = re.search(r"\blayer\(([^)]+)\)", statement) if statement.startswith("@import") else None
        if m:
          declare(m.group(1).strip())
        segment = i + 1
      elif mask[i] == "{":
        depth, j = 1, i + 1
        while depth:
          depth += {"{": 1, "}": -1}.get(mask[j], 0)
          j += 1
        prelude = " ".join(text[segment:i].split())
        body = (i + 1, j - 1)
        if prelude.startswith("@layer"):
          name = prelude[len("@layer"):].strip() or f"<anonymous {next(anonymous)}>"
          qualified = f"{layer}.{name}" if layer else name
          declare(qualified)
          walk(*body, qualified)
        elif prelude.startswith(("@media", "@supports")):
          if conditions_match:
            walk(*body, layer)
        elif prelude in _ROOT_SPECIFICITY:
          cuts = [i] + [k for k in range(i + 1, j - 1) if mask[k] == ";"] + [j - 1]
          for a, b in zip(cuts, cuts[1:]):
            m = re.fullmatch(r"\s*(--[\w-]+)\s*:(.*)", text[a + 1:b], re.S)
            if m:
              value = m.group(2).strip()
              important = value.lower().endswith("!important")
              if important:
                value = value[:-len("!important")].strip()
              decls.append((important, layer, _ROOT_SPECIFICITY[prelude], len(decls), m.group(1), value))
        i = segment = j
        continue
      i += 1

  walk(0, len(text), None)

  def rank(decl):
    important, layer, specificity, order, _name, _value = decl
    layer_rank = len(layer_order) if layer is None else layer_order.index(layer)
    return (important, layer_rank if not important else -layer_rank, specificity, order)

  resolved: dict[str, str] = {}
  for decl in sorted(decls, key=rank):
    resolved[decl[4]] = decl[5]
  return resolved


CORE_VARS = set(_shipped("dark"))


def _css_cases() -> list[dict]:
  return json.loads(CSS_CASES.read_text(encoding="utf-8"))


def test_cross_language_stylesheet_cases():
  """The same table runs through withThemeDefaults in
  frontend/src/lib/__tests__/themeRoles.test.js, which the shell's toggle
  paints, so the optimistic paint and /api/theme are the same bytes."""
  cases = _css_cases()
  assert len(cases) >= 20
  for case in cases:
    assert _ensure_core_vars(case["css"]) == case["expected"], case["name"]


def test_owner_declarations_always_win_and_every_core_var_resolves():
  for case in _css_cases():
    if case["owner"] is None:
      continue  # unsupported syntax: the oracle cannot lex it either
    resolved = _resolve_root(case["expected"])
    assert CORE_VARS <= set(resolved), f'{case["name"]}: missing {CORE_VARS - set(resolved)}'
    for name, value in case["owner"].items():
      assert resolved[name] == value, f'{case["name"]}: {name} resolved to {resolved[name]}'
    when = _resolve_root(case["expected"], conditions_match=True)
    for name, value in case["ownerWhenConditionsMatch"].items():
      assert when[name] == value, f'{case["name"]} (conditions match): {name}'


def test_owner_prelude_statements_survive_byte_for_byte():
  """Every owner @charset/@import/@layer statement appears verbatim in
  the served CSS, in order, with the complete generated block after them."""
  statement = re.compile(r"@(?:charset|import|layer)\b[^{]*?;(?=\s*(?:@|:|/|$))", re.S)
  for case in _css_cases():
    css, out = case["css"], case["expected"]
    position = 0
    for stmt in statement.findall(css):
      at = out.find(stmt, position)
      assert at >= 0, f'{case["name"]}: {stmt!r} not intact'
      position = at + len(stmt)
    block = out[out.index(theme.INJECTED_DEFAULTS_MARKER):]
    block = block[:block.index("}\n}\n")]
    assert CORE_VARS <= set(_vars(block)), case["name"]


@pytest.mark.parametrize("url", [r"https://example.test/a\);b.css", r"https://example.test/a\"b;c.css"])
def test_an_escape_inside_an_unquoted_import_url_is_not_structure(url):
  statement = f"@import url({url});"
  out = _ensure_core_vars(f"{statement}\n:root {{ --accent: #00ffff; }}\n")
  # The import stays whole, the complete block follows it, and the owner wins.
  assert out.startswith(f"@layer {theme.DEFAULTS_LAYER};\n{statement}/* {theme.INJECTED_DEFAULTS_MARKER}")
  resolved = _resolve_root(out)
  assert CORE_VARS <= set(resolved)
  assert resolved["--accent"] == "#00ffff"


def test_a_fake_declaration_inside_a_string_suppresses_nothing():
  css = ':root {\n  --bg: #0d0d0d;\n  --note: " --text: #ffffff; --accent: red;";\n}\n'
  resolved = _resolve_root(_ensure_core_vars(css))
  assert resolved["--text"] == _shipped("dark")["--text"]
  assert resolved["--accent"] == "#8b6cf7"


def test_conditional_only_base_has_a_default_and_the_owner_override():
  css = ":root { --bg: #0d0d0d; }\n@media (prefers-contrast: more) { :root { --accent: #00ffff; } }\n"
  out = _ensure_core_vars(css)
  assert _resolve_root(out)["--accent"] == "#8b6cf7"
  assert _resolve_root(out, conditions_match=True)["--accent"] == "#00ffff"


def test_generated_layer_is_declared_first_and_its_block_follows_imports():
  css = '@import url("https://fonts.example.test/a.css");\n:root { --bg: #f0eeeb; }\n.x { gap: 1px; }\n'
  out = _ensure_core_vars(css)
  # The defaults layer is declared before any owner statement (weakest
  # layer); its block follows the owner's imports (CSS requires @import first).
  assert out.startswith(f"@layer {theme.DEFAULTS_LAYER};\n" + '@import url("https://fonts.example.test/a.css");/* '
                        + theme.INJECTED_DEFAULTS_MARKER)
  assert out.endswith(":root { --bg: #f0eeeb; }\n.x { gap: 1px; }\n")
  assert out.count(theme.INJECTED_DEFAULTS_MARKER) == 1


def test_stale_generated_block_is_replaced_not_kept_active():
  stale = (":root { --bg: #0d0d0d; --accent-fg: #000000; }\n.keep { gap: 4px; }\n"
           f"/* {theme.INJECTED_DEFAULTS_MARKER} */\n:root {{ --accent-fg: #ffffff; }}\n.after {{ gap: 2px; }}\n")
  served = _ensure_core_vars(stale)
  assert served.count(theme.INJECTED_DEFAULTS_MARKER) == 1
  assert "--accent-fg: #ffffff" not in served
  assert ".keep { gap: 4px; }" in served and ".after { gap: 2px; }" in served
  resolved = _resolve_root(served)
  assert resolved["--accent-fg"] == "#000000"
  assert _contrast("#000000", resolved["--danger-fill"]) >= 4.5


def test_serving_is_idempotent():
  for case in _css_cases():
    assert _ensure_core_vars(case["expected"]) == case["expected"], case["name"]


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_partial_theme_resolves_the_shipped_roles_of_its_mode(mode):
  bg = _shipped(mode)["--bg"]
  resolved = _resolve_root(_ensure_core_vars(f":root {{ --bg: {bg}; }}\n"))
  for role in (*TEXT_ROLES, *FILL_ROLES):
    assert resolved[role] == _shipped(mode)[role]


def test_custom_accent_roles_follow_the_theme_not_the_brand_purple():
  css = ":root { --bg: #ffffff; --surface: #ffffff; --surface-2: #f4f4f5; --accent: #0ea5e9; }\n"
  resolved = _resolve_root(_ensure_core_vars(css))
  assert resolved["--accent-text"] not in (_shipped("dark")["--accent-text"], _shipped("light")["--accent-text"])
  for surface in ("#ffffff", "#f4f4f5"):
    assert _contrast(resolved["--accent-text"], surface) >= 4.5
  assert _contrast(resolved["--accent-fg"], resolved["--accent-fill"]) >= 4.5
  assert _contrast(resolved["--accent-fg"], resolved["--accent-fill-hover"]) >= 4.5


def test_theme_bg_is_the_owner_winning_root_value():
  assert theme._theme_bg(_ensure_core_vars(":root { --bg: #102030; }\n:root { --bg: #f0eeeb; }\n")) == "#f0eeeb"
  assert theme._theme_bg(_ensure_core_vars(".x { --bg: #ffffff; }\n")) == "#0d0d0d"


def test_cross_language_role_cases():
  """The same table runs in frontend/src/lib/__tests__/themeRoles.test.js."""
  cases = json.loads(ROLE_CASES.read_text(encoding="utf-8"))
  assert len(cases) >= 10
  for case in cases:
    assert theme.theme_role_vars(case["declared"], case["mode"]) == case["expected"], case["name"]


def test_parsed_channels_round_half_up_and_translucency_is_unsupported():
  assert theme._parse_css_rgb("rgb(10.5 100.5 200.5)") == (11, 101, 201)
  assert theme._parse_css_rgb("rgba(1, 2, 3, 0.5)") is None
  assert theme._parse_css_rgb("rgb(1 2 3 / 100%)") == (1, 2, 3)


# --- composed states, read from the consuming CSS -----------------------------
#
# Each state's colour and background are read from the rule that paints it,
# then evaluated against the shipped palette the way CSS composites them
# (var() fallbacks, color-mix in srgb with premultiplied alpha, source-over the
# backdrop). Editing one of these rules re-tests it; nothing here restates a
# recipe by hand.
#
# Recorded exception, not a pass: truly disabled controls (`:disabled`, e.g.
# .qcard__submit at opacity .4, .secure-card__submit at .55) are dimmed below
# 4.5:1 on purpose; WCAG 1.4.3 exempts inactive components.

SRC = REPO / "frontend" / "src"


def _rule(path: str, selector: str) -> str:
  css = (SRC / path).read_text(encoding="utf-8")
  m = re.search(r"(?:^|[}\n])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
  assert m, f"{selector} not found in {path}"
  return m.group(1)


def _prop(block: str, name: str) -> str:
  values = re.findall(r"(?:^|[;{\s])" + re.escape(name) + r"\s*:\s*([^;]+)", block)
  assert values, f"{name} missing"
  return re.sub(r"/\*.*?\*/", "", values[-1]).strip()


def _split_args(text: str) -> list[str]:
  parts, depth, cur = [], 0, ""
  for ch in text:
    if ch == "(":
      depth += 1
    elif ch == ")":
      depth -= 1
    if ch == "," and depth == 0:
      parts.append(cur.strip())
      cur = ""
    else:
      cur += ch
  return parts + [cur.strip()]


def _eval_color(expr: str, palette: dict[str, str]):
  """(r, g, b, alpha) for the CSS colour expressions these rules use."""
  expr = expr.strip()
  if expr.startswith("var(") and expr.endswith(")"):
    name, *fallback = _split_args(expr[4:-1])
    if name in palette:
      return _eval_color(palette[name], palette)
    assert fallback, f"unresolved {name}"
    return _eval_color(fallback[0], palette)
  if expr.startswith("color-mix(") and expr.endswith(")"):
    space, a, b = _split_args(expr[len("color-mix("):-1])
    assert space == "in srgb"
    m = re.fullmatch(r"(.*\S)\s+([\d.]+)%", a)
    p = float(m.group(2)) / 100
    x, y = _eval_color(m.group(1), palette), _eval_color(b, palette)
    alpha = p * x[3] + (1 - p) * y[3]
    rgb = [(p * x[3] * x[k] + (1 - p) * y[3] * y[k]) / alpha if alpha else 0 for k in range(3)]
    return (*rgb, alpha)
  if expr == "transparent":
    return (0, 0, 0, 0)
  named = {"black": "#000000", "white": "#ffffff", "#fff": "#ffffff"}
  if expr in named:
    return _eval_color(named[expr], palette)
  m = re.fullmatch(r"rgba\(([^)]*)\)", expr)
  if m:
    r, g, b, a = (float(v) for v in m.group(1).split(","))
    return (r, g, b, a)
  rgb = theme._parse_css_rgb(expr)
  assert rgb, f"unsupported colour {expr}"
  return (*rgb, 1.0)


def _over(top, backdrop) -> str:
  a = top[3]
  return "#" + "".join(
    f"{theme._round_half_up(top[k] * a + backdrop[k] * (1 - a)):02x}" for k in range(3)
  )


def _composed_states():
  user = _rule("index.css", ".chat__text--user")
  start, end = _split_args(re.search(r"linear-gradient\((.*)\)", _prop(user, "background")).group(1))[1:3]
  nudge = _rule("components/ChatView/ChatView.css", ".chat__question-nudge,\n.chat__resume-nudge")
  receipt = _rule("components/ChatView/SecureInputCard.css", ".secure-card__receipt")
  reveal = _rule("components/ChatView/SecureInputCard.css", ".secure-card--reveal .secure-card__receipt")
  submit = _rule("components/ChatView/QuestionCard.css", ".qcard__submit")
  # (label, text colour expr, background expr, backdrop expr)
  return [
    ("active drawer row", _prop(_rule("components/Drawer/Drawer.css", ".drawer__item--active"), "color"),
     _prop(_rule("components/Drawer/Drawer.css", ".drawer__item--active"), "background"), "var(--bg)"),
    ("question nudge at rest", _prop(nudge, "color"), _prop(nudge, "background"), "var(--bg)"),
    ("secure receipt (green)",
     _prop(_rule("components/ChatView/SecureInputCard.css", ".secure-card__receipt strong"), "color"),
     _prop(receipt, "background"), "var(--surface)"),
    ("secure receipt (danger)",
     _prop(_rule("components/ChatView/SecureInputCard.css", ".secure-card--reveal .secure-card__receipt strong"), "color"),
     _prop(reveal, "background"), "var(--surface)"),
    ("user bubble start", _prop(user, "color"), start, "var(--bg)"),
    ("user bubble end", _prop(user, "color"), end, "var(--bg)"),
    ("question submit", _prop(submit, "color"), _prop(submit, "background"), "var(--surface)"),
    ("question submit hover", _prop(submit, "color"),
     _prop(_rule("components/ChatView/QuestionCard.css",
                 ".qcard__submit:hover:not(:disabled)"), "background"),
     "var(--surface)"),
  ]


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_composed_states_read_at_4_5_on_their_real_backgrounds(mode):
  palette = _shipped(mode)
  failures = []
  for label, fg, bg, backdrop in _composed_states():
    base = _eval_color(backdrop, palette)
    assert base[3] == 1, f"{label}: backdrop must be opaque"
    surface = _over(_eval_color(bg, palette), base)
    text = _over(_eval_color(fg, palette), _eval_color(surface, palette))
    ratio = _contrast(text, surface)
    if ratio < 4.5:
      failures.append(f"{label}: {text} on {surface} = {ratio:.2f}")
  assert not failures, f"{mode}: " + "; ".join(failures)
