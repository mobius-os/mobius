"""Single source of truth for the default theme.

Theme-as-data handoff: the server serializes the effective theme
({css,bg,mode}, see `theme_data`) into the page's
`<script type="application/json" id="__mobius-theme__">` slot, and the
CLIENT paints it (src/lib/applyTheme.js). The server no longer injects a
`<style>` block into the served HTML. The default below is used when no
`/data/shared/theme.css` exists; the shell's index.css and
app-frame.html should NOT define their own :root variables — they come
from here.
"""

import hashlib
import math
import re
import shutil
import time
from html import escape as html_escape
from pathlib import Path
from typing import NamedTuple

# Matches @import url('...') or @import url("...") statements.
_IMPORT_RE = re.compile(
  r"""@import\s+url\(\s*['"]([^'"]+)['"]\s*\)\s*;[^\S\n]*\n?""",
)

# The secondary-surface token was renamed --surface2 -> --surface-2 with no
# compatibility alias. `migrate_theme_surface2_token` rewrites a saved theme once
# at boot; the lookahead matches only the exact legacy token, never a longer name.
_LEGACY_SURFACE2_RE = re.compile(r"--surface2(?![\w-])")

DEFAULT_THEME = """\
:root {
  /* Opaque fill colors — set by many shell components as solid
     backgrounds (.shell paints --bg across the viewport, chat
     bubbles + drawer + banners paint --surface / --surface-2,
     borders are 1px lines on top of those surfaces). Keep these
     SOLID — making them rgba(..., <1) lets whatever sits behind
     bleed through and makes text unreadable.

     Palette neutralized in 2026-05: dropped the slight blue tint
     (--bg #0d0f14 → #0d0d0d, surfaces same step) so the dark
     mode reads as a true charcoal stack rather than blue-grey. */
  --bg: #0d0d0d;
  --surface: #171717;
  --surface-2: #212121;
  --border: #2a2a2a;
  --border-light: #1f1f1f;

  /* Text colors — paint on top of the opaque fills above.
       2026-05-26: --muted #6b6b76 (~3.8:1 vs --bg, failed AA)
         → #9b9b9b (~6.4:1)
       2026-05-27: --muted #9b9b9b → #a8a8a8 (~6.1:1 on --surface-2
         #212121, was ~5.2:1 — comfortable AA on raised surfaces
         for the small text used in section labels and provider
         status indicators). */
  --text: #ececec;
  --muted: #a8a8a8;

  /* Accent palette. --accent is the brand mark (KEEP the purple) and
     the all-purpose accent for dots, borders, focus rings and washes;
     --accent-dim is allowed to be translucent because it's used as a
     glow, not as a fill. Text and button fills use the roles below. */
  --accent: #8b6cf7;
  --accent-hover: #7c5ce6;
  --accent-dim: rgba(139, 108, 247, 0.14);

  /* The foreground for text/icons on a role FILL: --accent-fill,
     --accent-fill-hover, --danger-fill and --green-fill each carry it at
     >= 4.5:1. On the legacy --accent itself white is only ~3.8:1 (large
     text and icons only), which is why the fills exist. A theme that
     changes --accent to a light colour gets a dark --accent-fg and fills
     derived to match unless it declares its own. */
  --accent-fg: #ffffff;

  /* Status colors: all-purpose like --accent (dots, borders, washes).
     --warning is also text-safe. */
  --danger: #f87171;
  --green: #10b981;
  --warning: #d8922f;

  /* Text-safe roles: >= 4.5:1 on --bg, --surface and --surface-2 of
     this mode. Use for links, status labels and any small coloured text. */
  --accent-text: #957cf8;
  --danger-text: #f87171;
  --green-text: #10b981;
  --warning-text: #d8922f;

  /* Fill roles for buttons and badges, labelled with --accent-fg. The
     action fill is a slightly deeper brand purple; hover/pressed deepens
     it further so every state keeps >= 4.5:1. */
  --accent-fill: #7c5ce6;
  --accent-fill-hover: #7053cf;
  --danger-fill: #dc2626;
  --green-fill: #047857;

  /* Typography. */
  --font: 'Inter', 'Inter Fallback', system-ui, -apple-system, sans-serif;
  --mono: 'JetBrains Mono', 'JetBrains Mono Fallback', ui-monospace, 'SF Mono', monospace;
}
"""


def get_theme_css(data_dir: str) -> str:
  """Returns the active EFFECTIVE theme CSS — the user override (or the
  built-in default), always augmented so every core variable the shell
  relies on is present.

  The `_ensure_core_vars` augment is what makes a partial theme.css
  safe: a file defining only --bg/--text still resolves --accent /
  --surface / --danger to readable defaults instead of dropping every
  CSS property that references them. Augmenting HERE — at the single
  effective-theme getter — means every consumer gets a complete theme:
  the SPA's `GET /api/theme` fetch, the app-frame iframe, and the
  `__mobius-theme__` data slot alike.

  Historically the augment ran ONLY at HTML-render time (the old
  server-side `<style>` injection); the SPA then re-fetched the RAW override
  via /api/theme and applied it LAST in the cascade, nullifying the
  server-augmented block. That was the "light mode completely broken"
  bug once a light/dark toggle had stripped theme.css down to its
  structural tokens (no --accent/--danger/--green). The raw,
  un-augmented override is still available verbatim at
  /api/storage/shared/theme.css for editors that want the source.
  """
  theme_path = Path(data_dir) / "shared" / "theme.css"
  if theme_path.exists():
    content = theme_path.read_text(encoding="utf-8").strip()
    if content:
      return _ensure_core_vars(content)
  return DEFAULT_THEME


def extract_imports(css: str) -> tuple[list[str], str]:
  """Split @import url() lines from CSS, return (urls, remaining_css).

  Browsers ignore @import inside <style> tags in some contexts, so
  callers should convert these to <link> tags instead.
  """
  urls = _IMPORT_RE.findall(css)
  remaining = _IMPORT_RE.sub("", css)
  return urls, remaining


def get_bg_color(data_dir: str) -> str:
  """Extracts the --bg color for use in the manifest."""
  return _theme_bg(get_theme_css(data_dir))


def get_theme_mode(data_dir: str) -> str:
  """Returns the active theme mode ("dark" or "light").

  Sourced from `/data/shared/theme-mode` (a JSON-encoded string),
  written by `themeService.toggleTheme` on every mode swap. Falls
  back to "dark" — the historical default — when the file is
  missing, unreadable, or contains an unrecognized value. The dark
  fallback is intentional and CORRECT: DEFAULT_THEME (served when
  theme.css is also absent) is the dark palette, so an absent
  theme-mode yields a dark theme AND a dark injected color-scheme —
  they agree. Returning "light" here would mismatch the dark default
  palette. Used by any server-rendered
  surface that needs to mirror the SPA's theme without re-parsing the
  CSS.
  """
  import json
  path = Path(data_dir) / "shared" / "theme-mode"
  if not path.exists():
    return "dark"
  try:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
      return "dark"
    # Storage layer stores values as JSON strings, so an extra
    # decode peels the quotes. Direct strings (legacy writes) work
    # too via the fallback.
    try:
      mode = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
      mode = raw
    if mode in ("dark", "light"):
      return mode
  except OSError:
    pass
  return "dark"


# =============================================================
# THEME REVERSIBILITY AFFORDANCES
# =============================================================
# A theme that makes the shell unresponsive (full-screen overlay,
# pointer-events: none on the root, opaque ::before with z-index
# 99999, etc.) traps the user inside a broken UI. The recovery
# story is: (a) the prior theme.css is snapshotted automatically
# before every overwrite, so the agent never silently destroys
# work; (b) an operator root shell can move theme.css aside so
# DEFAULT_THEME paints again; (c) the main shell honors
# `?reset-theme=1` in the URL for cases where
# the user can reach `/` from the address bar but can't click
# anything inside the page.
#
# This is the "build for reversibility, not prevention" lever from
# the design philosophy. The theme is allowed to break the UI;
# repair is trivial and reachable from outside the broken state.


def snapshot_theme_if_present(data_dir: str) -> str | None:
  """Copies the current theme.css to theme.css.bak-<ts> if it exists.

  Returns the absolute path of the backup, or None when there was
  nothing to snapshot. The convention (`theme.css.bak-<unix-ts>`
  alongside the live file) matches the informal pattern already
  present in `/data/shared/` from agent-driven swaps; this helper
  makes it automatic so the agent never has to remember.

  Idempotent on a missing source. Two snapshots within the same
  second overwrite each other (timestamp granularity); this is the
  same granularity the agent already uses informally and is fine
  for a recovery audit trail — the goal is "previous version is
  preserved," not "every keystroke."

  Only the newest few snapshots are kept. `*.bak-*` is gitignored in
  /data, so these files are the ONLY instant local recovery copies —
  but deep history already lives in the /data git safety net, and an
  unbounded pile (hundreds of files after a theme-editing session)
  buries the useful recent ones.
  """
  src = Path(data_dir) / "shared" / "theme.css"
  if not src.exists():
    return None
  ts = int(time.time())
  dst = src.with_name(f"theme.css.bak-{ts}")
  shutil.copy2(src, dst)
  _prune_theme_snapshots(src.parent)
  return str(dst)


# Instant-recovery depth: enough to survive a burst of bad edits in
# one session; anything older is recoverable from /data git history.
THEME_SNAPSHOT_KEEP = 5


def _prune_theme_snapshots(shared_dir: Path) -> None:
  """Deletes all but the newest THEME_SNAPSHOT_KEEP theme snapshots.

  Sort key is the embedded unix timestamp (filename suffix), not
  mtime — copy2 preserves the SOURCE file's mtime, so mtime ordering
  would reflect when the theme was edited, not when it was snapshot.
  Non-integer suffixes (hand-made backups) are left untouched.
  """
  snapshots = []
  for path in shared_dir.glob("theme.css.bak-*"):
    suffix = path.name.rsplit("-", 1)[-1]
    if suffix.isdigit() and path.is_file():
      snapshots.append((int(suffix), path))
  snapshots.sort(reverse=True)
  for _, path in snapshots[THEME_SNAPSHOT_KEEP:]:
    try:
      path.unlink()
    except OSError:
      pass


def migrate_theme_surface2_token(data_dir: str) -> bool:
  """One-time, idempotent in-place rewrite of a saved theme.css's legacy
  --surface2 token to the standard --surface-2.

  The secondary-surface token was renamed --surface2 -> --surface-2 with no
  compatibility alias, so a theme.css saved before the rename would keep the
  owner's custom value under a name the shell no longer reads. Boot runs this
  before anything is served (see startup.py), so the owner's value survives.

  Reads theme.css, rewrites the token word-boundary-safely
  (`_LEGACY_SURFACE2_RE`), and — only when something
  actually changed — snapshots the prior file first (the same recovery trail
  the storage write path keeps) and writes the result atomically. Returns True
  when it rewrote the file, False when there was nothing to do: missing file,
  empty file, or already migrated. Every other byte is preserved, and it is
  safe to run repeatedly.
  """
  path = Path(data_dir) / "shared" / "theme.css"
  try:
    content = path.read_text(encoding="utf-8")
  except OSError:
    # Missing file (fresh install using DEFAULT_THEME) or unreadable — nothing
    # to migrate. Never a hard failure: the served theme is already canonical.
    return False
  migrated = _LEGACY_SURFACE2_RE.sub("--surface-2", content)
  if migrated == content:
    return False
  from app.storage_io import atomic_write

  snapshot_theme_if_present(data_dir)
  atomic_write(path, migrated)
  return True


def reset_theme_override(data_dir: str) -> dict:
  """Moves /data/shared/theme.css aside so DEFAULT_THEME paints again.

  The override is preserved as `theme.css.reset-bak-<unix-ts>` so
  the user can recover their previous theme if the reset was a
  mistake. Idempotent — calling with no override present is a
  no-op that reports `reset=False`.

  Returns a dict shaped like the /api/theme/reset response:
    {"reset": True,  "backup": "<absolute path>"} on success
    {"reset": False, "reason": "no override"}   when no theme.css exists
  """
  src = Path(data_dir) / "shared" / "theme.css"
  if not src.exists():
    return {"reset": False, "reason": "no override"}
  ts = int(time.time())
  dst = src.with_name(f"theme.css.reset-bak-{ts}")
  src.rename(dst)
  return {"reset": True, "backup": str(dst)}


def _escape_for_style_tag(css: str) -> str:
  """Escapes any closing </style> sequence inside CSS so it can't break
  out of a <style> block. The HTML parser ends a <style> at the first
  literal `</`, regardless of what follows; so any user-controlled CSS
  injected verbatim is a stored-XSS vector. The CSS-spec-safe rewrite
  is `<\\/` (backslash escape inside CSS strings/comments) but for
  general CSS the simpler defense is to break the closing-tag pattern
  with an HTML comment-friendly substitution that keeps the CSS
  semantically identical: replace `</` with `<\\/` inside the embedded
  block. Browsers parse `<\\/style>` as text inside the <style>, never
  as a closing tag.

  RETAINED after theme-as-data: the server no longer injects a <style>
  block (the client paints theme.css into <style id="mobius-theme">), so
  the stored-XSS surface moved to the client. This stays as the canonical
  CSS `</`-escaper — the JSON-slot serializer in main.py applies an
  equivalent `</` -> `<\\/` escape to keep embedded `</script>` from
  breaking out of the slot's <script type="application/json"> wrapper.
  """
  return css.replace("</", "<\\/")


def _is_safe_import_url(url: str) -> bool:
  """Allow only http(s) URLs for @import — no javascript:, data:, etc."""
  return url.startswith("https://") or url.startswith("http://")


# =============================================================
# GENERATED DEFAULTS (debugger pointer)
# =============================================================
# `get_theme_css` serves the owner's theme.css with a generated block of
# EVERY core variable in the weakest cascade layer, `mobius-defaults` (see
# `_ensure_core_vars` for the exact layout and guarantee), so
# `GET /api/theme`, the app-frame iframe and the shell's theme-as-data slot
# always see a complete theme while owner declarations still win.  If
# theme.css on disk defines only --bg/--text yet the shell renders surfaces,
# accents and roles, that block supplied them.  Read the storage file for the
# owner's source, and `GET /api/theme` for what the browser received.

# Light-mode defaults for the structural + status vars whose correct
# value DEPENDS on mode. DEFAULT_THEME is the DARK palette, so defaulting a
# partial LIGHT theme.css from it gave dark surfaces/borders
# (--surface-2:#212121, --border-light:#1f1f1f) -- "dark surfaces in light
# mode". These values mirror the frontend LIGHT_COLORS/LIGHT_ROLES in
# frontend/src/theme.js so
# the server-augmented light theme matches what a client-side toggle
# produces (backend/tests/test_theme_roles.py enforces it). --accent,
# --accent-hover, the fills and --font/--mono are mode-agnostic, so they
# stay sourced from DEFAULT_THEME for both modes.
_LIGHT_DEFAULTS = {
  "--bg": "#f0eeeb",
  "--surface": "#ffffff",
  "--surface-2": "#e8e6e2",
  "--border": "#d4d1cc",
  "--border-light": "#e2dfdb",
  "--text": "#1c1b1a",
  "--muted": "#6b6864",
  "--accent-dim": "rgba(139, 108, 247, 0.08)",
  "--danger": "#ef4444",
  "--green": "#059669",
  "--warning": "#8f5a00",
  "--accent-text": "#6548d4",
  "--danger-text": "#b91c1c",
  "--green-text": "#046c4e",
  "--warning-text": "#8f5a00",
}


def _parse_css_rgb(value: str) -> tuple[int, int, int] | None:
  """(r, g, b) of an OPAQUE colour literal, else None.

  Accepts #rgb, #rrggbb and rgb()/rgba() whose alpha is absent or 1.  A
  translucent colour composites over an unknown backdrop, so it is
  unsupported rather than read as its opaque channels.  Channels round half
  up, like JavaScript's Math.round (Python's round() is bankers' rounding),
  so frontend/src/theme.js parses identically.
  """
  text = value.strip().lower()
  m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", text)
  if m:
    digits = m.group(1)
    if len(digits) == 3:
      digits = "".join(c * 2 for c in digits)
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
  m = re.fullmatch(r"rgba?\(([^)]*)\)", text)
  if m:
    # Accept both legacy comma-separated and modern space-separated syntax
    # (`rgb(255 255 255 / 50%)`); an unparseable form returns None and keeps
    # the previous fixed default rather than guessing.
    parts = [p for p in re.split(r"[,\s]+", m.group(1).replace("/", " ")) if p]
    if len(parts) not in (3, 4):
      return None
    try:
      if len(parts) == 4:
        alpha = float(parts[3][:-1]) / 100 if parts[3].endswith("%") else float(parts[3])
        if not alpha >= 1:
          return None
      channels = [
        _round_half_up(float(p[:-1]) * 255 / 100) if p.endswith("%") else _round_half_up(float(p))
        for p in parts[:3]
      ]
    except (ValueError, OverflowError):
      return None
    return tuple(max(0, min(255, c)) for c in channels)  # type: ignore[return-value]
  return None


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
  def channel(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

  r, g, b = (channel(c) for c in rgb)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrasting_accent_fg(accent: str | None) -> str | None:
  """DEFAULT_THEME's white, unless it would be illegible on `accent`.

  A fixed #ffffff is wrong for a light accent -- a theme setting
  --accent: #ffffff and omitting --accent-fg paints white on white.  White
  stays while it keeps 3:1 on the accent, the point past which deepening the
  fill would stop looking like the accent; otherwise black.  This only picks
  the label colour: small-text contrast comes from pairing it with
  --accent-fill, which `theme_role_vars` deepens until the label reaches
  4.5:1, so the brand purple keeps white labels on a slightly deeper fill.
  Returns None when the accent cannot be parsed, leaving the existing default
  in place.  Mirrors `contrastingAccentFg` in frontend/src/theme.js.
  """
  rgb = _parse_css_rgb(accent) if accent else None
  if rgb is None:
    return None
  return "#ffffff" if _rgb_contrast(_WHITE, rgb) >= 3.0 else "#000000"


# =============================================================
# TEXT- AND FILL-SAFE COLOUR ROLES
# =============================================================
# --accent, --danger and --green serve as dots, borders and washes, where
# 3:1 is enough, but small text on a surface and labels on a button fill
# need 4.5:1.  Each base therefore has a text role and (except --warning,
# which is itself text-safe) a fill role.  The shipped values live in
# DEFAULT_THEME / _LIGHT_DEFAULTS; `theme_role_vars` fills in roles a theme
# omitted, deriving them from the theme's own base when it is custom.
# Mirrored by `themeRoleVars` in frontend/src/theme.js; the shared case
# tables frontend/src/lib/__tests__/themeRoleCases.json (flat declarations)
# and themeCssCases.json (whole stylesheets) pin both.
_ROLE_SOURCES = (
  {"base": "--accent", "text": "--accent-text", "fill": "--accent-fill",
   "fill_hover": "--accent-fill-hover"},
  {"base": "--danger", "text": "--danger-text", "fill": "--danger-fill"},
  {"base": "--green", "text": "--green-text", "fill": "--green-fill"},
  {"base": "--warning", "text": "--warning-text"},
)
_ROLE_KEYS = frozenset(
  role for source in _ROLE_SOURCES for key, role in source.items() if key != "base"
)
_MIN_TEXT_CONTRAST = 4.5
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)

# Comment above the generated defaults block `_ensure_core_vars` places
# before the owner's CSS.  The shell's toggle finds and drops that block by
# this exact text (theme.js INJECTED_DEFAULTS_MARKER) so it is never
# persisted as owner CSS.
INJECTED_DEFAULTS_MARKER = "Möbius: injected defaults for variables the theme omitted"
# The cascade layer holding every generated default.  It is declared before
# any owner layer, so it is the weakest layer: owner rules in any layer, and
# unlayered owner rules, beat it.  app-frame.html's static fallbacks and the
# shell's index.css typography defaults use the same layer.
DEFAULTS_LAYER = "mobius-defaults"
# A generated block in the current layered form or either older form (the
# leading `:where(:root)` and the trailing `:root`), plus the layer-order
# statement, so a stale copy is dropped and regenerated.
# The current form is matched byte-exactly so regenerating is idempotent.
_GENERATED_BLOCK_RE = re.compile(
  r"/\* " + re.escape(INJECTED_DEFAULTS_MARKER) + r" \*/\n@layer " + DEFAULTS_LAYER
  + r" \{\n:where\(:root\) \{\n[^}]*\}\n\}\n"
  + r"|@layer " + DEFAULTS_LAYER + r";\n"
  + r"|/\*\s*" + re.escape(INJECTED_DEFAULTS_MARKER)
  + r"\s*\*/\s*(?::where\(:root\)|:root)\s*\{[^}]*\}\s*"
)
_SURFACE_KEYS = ("--bg", "--surface", "--surface-2")


def _rgb_contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
  la, lb = _relative_luminance(a), _relative_luminance(b)
  return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _to_hex(rgb) -> str:
  return "#" + "".join(f"{c:02x}" for c in rgb)


def _round_half_up(x: float) -> int:
  # JavaScript's Math.round, so both derivations land on the same hex.
  return math.floor(x + 0.5)


def _mix_toward(rgb, target, fraction: float) -> tuple[int, int, int]:
  return tuple(_round_half_up(c + (t - c) * fraction) for c, t in zip(rgb, target))  # type: ignore[return-value]


def _nearest_passing_mix(rgb, preferred, meets) -> str | None:
  """The smallest 1% sRGB mix of `rgb` toward black or white satisfying
  `meets`, trying `preferred` first and keeping it on a tie.  None when
  neither endpoint can (for example a mid-grey label): the caller must not
  present any colour as passing."""
  best: tuple[int, str] | None = None
  for target in (preferred, _WHITE if preferred == _BLACK else _BLACK):
    for i in range(101):
      if best is not None and i >= best[0]:
        break
      mixed = _mix_toward(rgb, target, i / 100)
      if meets(mixed):
        best = (i, _to_hex(mixed))
        break
  return best[1] if best else None


def _normalized_color(value: str) -> str:
  text = re.sub(r"\s+", "", str(value).strip().lower())
  m = re.fullmatch(r"#([0-9a-f])([0-9a-f])([0-9a-f])", text)
  return "#" + "".join(c * 2 for c in m.groups()) if m else text


def _shipped_palette(mode: str) -> dict[str, str]:
  palette = {}
  for line in DEFAULT_THEME.splitlines():
    m = re.match(r"\s*(--[\w-]+)\s*:\s*([^;]+);", line)
    if m:
      palette[m.group(1)] = m.group(2).strip()
  if mode == "light":
    palette.update(_LIGHT_DEFAULTS)
  return palette


def _is_shipped_value(key: str, value: str) -> bool:
  v = _normalized_color(value)
  return any(
    key in palette and _normalized_color(palette[key]) == v
    for palette in (_shipped_palette("dark"), _shipped_palette("light"))
  )


def theme_role_vars(declared: dict[str, str], mode: str) -> dict[str, str]:
  """Every role token, plus --accent-fg, for a theme whose plain-:root
  custom properties are `declared`.

  - A shipped base (of either mode) gets the shipped role when that meets
    its target on this theme's surfaces / label.
  - Otherwise the role is the nearest black/white mix of the theme's base
    that meets 4.5:1.  Guaranteed only for opaque hex/rgb() literals: a
    translucent or other colour syntax, or a target no mix can reach, keeps
    the theme's own base value -- passed through, not certified.
  - A label or fill the theme declared is used as the input for the fills
    and hover that pair with it.

  These are DEFAULTS: they sit in the leading generated block, so any role
  the owner declares anywhere outranks them by ordinary cascade.  Mirrors
  `themeRoleVars` in frontend/src/theme.js.
  """
  palette = _shipped_palette(mode)

  def value(key: str) -> str:
    return declared.get(key, palette.get(key, ""))

  surfaces = [_parse_css_rgb(value(k)) or _parse_css_rgb(palette[k]) for k in _SURFACE_KEYS]
  light_surfaces = _relative_luminance(surfaces[0]) > 0.179

  fg = declared.get("--accent-fg")
  if fg is None:
    fg = _contrasting_accent_fg(value("--accent")) or "#ffffff"
  out: dict[str, str] = {"--accent-fg": fg}
  fg_rgb = _parse_css_rgb(fg)

  def reads_on_surfaces(rgb) -> bool:
    return all(_rgb_contrast(rgb, s) >= _MIN_TEXT_CONTRAST for s in surfaces)

  def carries_fg(rgb) -> bool:
    return fg_rgb is not None and _rgb_contrast(fg_rgb, rgb) >= _MIN_TEXT_CONTRAST

  def away_from_fg(rgb):
    # Prefer moving away from the label: deeper under a light label, lighter under a dark one.
    if fg_rgb is not None and _relative_luminance(fg_rgb) < _relative_luminance(rgb):
      return _WHITE
    return _BLACK

  for source in _ROLE_SOURCES:
    base = value(source["base"])
    base_rgb = _parse_css_rgb(base)
    shipped_base = _is_shipped_value(source["base"], base)

    def pick(role, meets, preferred):
      if shipped_base and meets(_parse_css_rgb(palette[role])):
        return palette[role]
      derived = _nearest_passing_mix(base_rgb, preferred(base_rgb), meets) if base_rgb else None
      return derived or base

    toward = _BLACK if light_surfaces else _WHITE
    out[source["text"]] = pick(source["text"], reads_on_surfaces, lambda _rgb: toward)
    fill = source.get("fill")
    if fill:
      out[fill] = pick(fill, carries_fg, away_from_fg)
    hover = source.get("fill_hover")
    if hover:
      fill_value = declared.get(fill, out[fill])
      fill_rgb = _parse_css_rgb(fill_value)
      out[hover] = _to_hex(_mix_toward(fill_rgb, away_from_fg(fill_rgb), 0.1)) if fill_rgb else fill_value
  return out


_DECLARATION_RE = re.compile(r"\s*(--[A-Za-z_][\w-]*)\s*:(.*)", re.S)
_IMPORTANT_RE = re.compile(r"!\s*important$", re.I)
_URL_OPEN_RE = re.compile(r"url\(\s*(?=[^\s\"'])", re.I)


def _lex_theme_css(css: str) -> tuple[str, str, bool]:
  """The comment-blanked text plus a same-length mask in which string and
  unquoted url() contents and escaped characters are neutralised, so
  structure is read from the mask while values are sliced from the text.
  The bool is False for syntax the lexer cannot follow (an unterminated
  string, comment or url)."""
  text: list[str] = []
  mask: list[str] = []
  ok = True
  i, n = 0, len(css)
  while i < n:
    ch = css[i]
    url = _URL_OPEN_RE.match(css, i) if ch in "uU" else None
    if url:
      # An unquoted url() ends at its first UNESCAPED `)`: `url(a\);b)` is one
      # token, so its `;` must never read as structure.
      j = url.end()
      while j < n and css[j] != ")":
        j += 2 if css[j] == "\\" else 1
      if j >= n:
        ok, j = False, n
      text.append(css[i:j])
      mask.append(css[i:url.end()] + "x" * (j - url.end()))
      i = j
    elif ch == "/" and css[i + 1:i + 2] == "*":
      j = css.find("*/", i + 2)
      if j < 0:
        ok, j = False, n
      else:
        j += 2
      text.append(" " * (j - i))
      mask.append(" " * (j - i))
      i = j
    elif ch in "\"'":
      text.append(ch)
      mask.append(ch)
      i += 1
      closed = False
      while i < n:
        c = css[i]
        if c == "\\":
          pair = css[i:i + 2]
          text.append(pair)
          mask.append("x" * len(pair))
          i += len(pair)
        elif c == ch:
          text.append(c)
          mask.append(c)
          i += 1
          closed = True
          break
        elif c == "\n":
          break
        else:
          text.append(c)
          mask.append("x")
          i += 1
      if not closed:
        ok = False
    elif ch == "\\":
      pair = css[i:i + 2]
      text.append(pair)
      mask.append("x" * len(pair))
      i += len(pair)
    else:
      text.append(ch)
      mask.append(ch)
      i += 1
  return "".join(text), "".join(mask), ok


def scan_root_declarations(css: str) -> dict[str, str] | None:
  """The winning custom properties of the theme's unconditional top-level
  `:root { }` rules, in source order (the last declaration wins; an
  !important one beats later normal ones), or None when the CSS uses
  syntax this tokenizer cannot follow.

  These values only feed role DERIVATION and mode inference.  They never
  decide what is emitted: the generated defaults block always carries every
  variable and sits before the owner's CSS, so the owner wins regardless.
  A generated block is ignored; comments, strings, url() contents and
  escapes are lexed first, so a `}` or `;` inside them is never structure.
  Mirrors `scanRootDeclarations` in frontend/src/theme.js.
  """
  text, mask, supported = _lex_theme_css(_GENERATED_BLOCK_RE.sub("", css or ""))
  root: dict[str, str] = {}
  important: set[str] = set()

  def record(decl: str) -> None:
    m = _DECLARATION_RE.fullmatch(decl)
    if not m:
      return
    name, value = m.group(1), m.group(2).strip()
    is_important = bool(_IMPORTANT_RE.search(value))
    if is_important:
      value = _IMPORTANT_RE.sub("", value).strip()
    if name in important and not is_important:
      return
    if is_important:
      important.add(name)
    root[name] = value

  depth = 0
  segment = 0
  root_rule = False
  for i, ch in enumerate(mask):
    if ch == "{":
      if depth == 0:
        root_rule = " ".join(text[segment:i].split()) == ":root"
      depth += 1
      segment = i + 1
    elif ch == "}":
      if depth == 0:
        return None
      if depth == 1 and root_rule:
        record(text[segment:i])
      depth -= 1
      if depth == 0:
        root_rule = False
      segment = i + 1
    elif ch == ";":
      if depth == 1 and root_rule:
        record(text[segment:i])
      segment = i + 1
  if depth or not supported:
    return None
  return root


def _infer_theme_mode(css: str) -> str:
  """'light' or 'dark' from the theme's winning plain-:root --bg
  (`scan_root_declarations`), mirroring `inferThemeMode` in
  frontend/src/theme.js.  Anything else -- no such --bg, a non-hex value,
  or CSS the scanner cannot follow -- is 'dark', the historical default
  (DEFAULT_THEME is dark).  #RGB/#RGBA double their nibbles and
  #RRGGBB/#RRGGBBAA use their leading six digits: alpha never changes the
  dark-vs-light direction.
  """
  bg = (scan_root_declarations(css) or {}).get("--bg", "")
  m = re.fullmatch(r"#([0-9a-fA-F]{3,8})", bg.strip())
  if not m or len(m.group(1)) not in (3, 4, 6, 8):
    return "dark"
  raw = m.group(1)
  raw = "".join(c * 2 for c in raw[:3]) if len(raw) <= 4 else raw[:6]
  r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
  return "dark" if (r + g + b) / 3 < 128 else "light"


def theme_defaults_for_css(css: str) -> dict[str, str]:
  """Every core variable's default for `css`: the shipped palette of its
  inferred mode, with roles derived from its winning plain-:root bases (or
  from the shipped bases when the scanner cannot follow the CSS)."""
  mode = _infer_theme_mode(css)
  defaults = _shipped_palette(mode)
  defaults.update(theme_role_vars(scan_root_declarations(css) or {}, mode))
  return defaults


def _prelude_bounds(css: str) -> tuple[int, int]:
  """(end of a leading @charset, end of the leading statement prelude).

  CSS allows only @charset, @import and @layer *statements* before other
  rules.  Statement ends are found with the same lexer as the root scanner,
  so a `;` or `{` inside a string, url() or comment (a multi-weight Google
  Fonts URL, say) never ends a statement.  Both offsets fall between
  complete statements; nothing is rewritten.  Mirrors `preludeBounds` in
  frontend/src/theme.js.
  """
  text, mask, _ok = _lex_theme_css(css)
  charset_end = prelude_end = i = 0
  while True:
    while i < len(mask) and mask[i].isspace():
      i += 1
    m = re.match(r"@(charset|import|layer)\b", text[i:i + 8], re.I)
    if not m:
      break
    j = i
    while j < len(mask) and mask[j] not in ";{":
      j += 1
    if j >= len(mask) or mask[j] == "{":
      break  # an @layer block is a rule, not a statement
    i = prelude_end = j + 1
    if m.group(1).lower() == "charset" and charset_end == 0:
      charset_end = i
  return charset_end, prelude_end


def _ensure_core_vars(css: str) -> str:
  """The effective theme: the owner's CSS plus one generated block of every
  core variable, held in the `mobius-defaults` cascade layer.

  Layout: a leading @charset stays first; then `@layer mobius-defaults;`
  (declaring the layer before any owner layer, so it is the weakest); then
  the owner's @import/@layer statements; then the generated
  `@layer mobius-defaults { :where(:root) { ... } }`; then the rest of the
  owner's CSS.  Insertion points are between complete statements found by
  the lexer, and the owner's bytes are not rewritten -- except that a
  generated block or layer statement from an earlier read or toggle is
  dropped and regenerated, as the shell's toggle does.

  Guarantee: for every core variable, any normal owner declaration that
  applies to the root element (unlayered or in any owner layer, in any
  rule or condition) takes precedence over the generated default, and a
  variable the owner never declared still resolves.  An owner !important
  declaration also wins.  Not guaranteed: that the derived role colours
  suit an owner base set outside a plain :root rule (they are derived from
  the winning plain-:root bases, else the shipped ones), or that the served
  `bg` metadata matches a background set outside a plain :root -- it is a
  pre-paint hint only; the appliers read the background from the cascade.
  Mirrors `withThemeDefaults` in frontend/src/theme.js byte for byte.
  """
  owner = _GENERATED_BLOCK_RE.sub("", css)
  defaults = theme_defaults_for_css(owner)
  lines = "\n".join(f"  {name}: {defaults[name]};" for name in sorted(defaults))
  block = (f"/* {INJECTED_DEFAULTS_MARKER} */\n@layer {DEFAULTS_LAYER} {{\n"
           f":where(:root) {{\n{lines}\n}}\n}}\n")
  charset_end, prelude_end = _prelude_bounds(owner)
  return (owner[:charset_end] + f"@layer {DEFAULTS_LAYER};\n" + owner[charset_end:prelude_end]
          + block + owner[prelude_end:])


def _theme_bg(effective_css: str) -> str:
  """A pre-paint HINT for the theme's background: the owner's winning
  plain-:root --bg when it is a hex colour, else the generated default.  A
  background set in another rule (an `html` rule, a condition, a layer) is
  not seen here; that is why the appliers only use this value for chrome
  painted before the theme stylesheet applies (theme-color, the first
  frame) and read the real background from the cascade afterwards."""
  bg = (scan_root_declarations(effective_css) or {}).get("--bg", "").strip()
  if re.fullmatch(r"#[0-9a-fA-F]{3,8}", bg):
    return bg
  m = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,8})", effective_css)
  return m.group(1) if m else "#0d0d0d"


def frame_content_rev(data_dir: str) -> str:
  """Short content hash of the shared app-frame.html, injected into
  index.html as `<meta name="mobius-frame-rev">` so AppCanvas can fold it into
  the frame URL's `?v=` cache-buster. `app.updated_at` (the rest of `?v=`)
  only advances on an app EDIT, not when the shared app-frame.html is
  REDEPLOYED — so without this rev the service worker keeps serving the stale
  frame until a 2nd-open background revalidate. Mirrors the content hash in
  routes/app_runtime.py `_frame_etag`, which the SW cache KEY (unlike the
  HTTP ETag)
  ignores. Keep the path candidates in sync with the `/frame` route.
  """
  frame_path = _resolve_frame_path(data_dir)
  if frame_path is None:
    return ""
  return hashlib.sha256(frame_path.read_bytes()).hexdigest()[:16]


class EffectiveTheme(NamedTuple):
  """The bundle of theme facts a single page render needs, computed from
  one read each of theme.css + theme-mode + one hash of app-frame.html.

  Produced by `load_effective_theme` and threaded into both `theme_data`
  (the JSON-slot bundle) and the `/frame` ETag so a request resolves the
  theme ONCE instead of `theme_data` + `get_bg_color` + `get_theme_mode` +
  `frame_content_rev` each re-reading the same files.
  `css` is the effective (core-var-augmented) CSS; `bg` is its --bg; `mode`
  is "dark"/"light"; `rev` is the app-frame.html content hash.
  """

  css: str
  bg: str
  mode: str
  rev: str


# mtime+size-keyed memo for load_effective_theme. Keyed on the
# (mtime_ns, size) of theme.css + theme-mode + the resolved app-frame.html
# path, so a write to any of them changes the key and misses (recomputes)
# exactly once. stat() is cheap; the saved work is the theme.css read +
# _ensure_core_vars regex pass + the app-frame.html hash. Stale-key
# tolerance: stat granularity means a sub-tick write that preserves size
# could in theory under-invalidate, but theme writes go through the storage
# layer (which rewrites the whole file → mtime advances), so this matches
# the existing content-hash freshness model used by _frame_etag.
_EFFECTIVE_THEME_MEMO: dict[str, tuple[tuple, EffectiveTheme]] = {}


def _stat_key(path: Path) -> tuple[int, int]:
  """(mtime_ns, size) for a file, or (0, 0) if it's missing."""
  try:
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)
  except OSError:
    return (0, 0)


def load_effective_theme(data_dir: str) -> EffectiveTheme:
  """Resolve the active theme in a single pass: read theme.css + theme-mode
  once and hash app-frame.html once, returning the (css, bg, mode, rev)
  bundle every page render needs.

  Memoized on the stat-key of the three source files (see
  `_EFFECTIVE_THEME_MEMO`): a theme write changes the key and recomputes
  once, then subsequent requests hit the memo. Behavior is identical to
  calling get_theme_css / get_bg_color / get_theme_mode / frame_content_rev
  separately — this just avoids re-reading the same files 4x per request.
  """
  base = Path(data_dir) / "shared"
  theme_path = base / "theme.css"
  mode_path = base / "theme-mode"
  frame_path = _resolve_frame_path(data_dir)
  key = (
    _stat_key(theme_path),
    _stat_key(mode_path),
    _stat_key(frame_path) if frame_path else (0, 0),
  )
  cached = _EFFECTIVE_THEME_MEMO.get(data_dir)
  if cached is not None and cached[0] == key:
    return cached[1]

  css = get_theme_css(data_dir)
  bg = _theme_bg(css)
  mode = get_theme_mode(data_dir)
  rev = (
    hashlib.sha256(frame_path.read_bytes()).hexdigest()[:16]
    if frame_path else ""
  )
  bundle = EffectiveTheme(css=css, bg=bg, mode=mode, rev=rev)
  _EFFECTIVE_THEME_MEMO[data_dir] = (key, bundle)
  return bundle


def _resolve_frame_path(data_dir: str) -> Path | None:
  """First existing app-frame.html among the served platform and baked
  locations — the same candidate list frame_content_rev uses."""
  candidates = [
    Path(data_dir) / "platform" / "frontend" / "public" / "app-frame.html",
    # Repo-relative dev/test fallback (== served clone in-container).
    Path(__file__).resolve().parents[2] / "frontend" / "public" / "app-frame.html",
    Path("/app/app-frame.html"),
  ]
  return next((p for p in candidates if p.exists()), None)


def theme_data(data_dir: str, bundle: "EffectiveTheme | None" = None) -> dict:
  """The effective theme as the client-paintable data bundle:
  `{"css": ..., "bg": ..., "mode": ...}`.

  This is the theme-as-data handoff. `main.py` serializes this dict into
  the page's `<script type="application/json" id="__mobius-theme__">`
  slot (with a `</`-escape + U+2028/U+2029 escape so embedded CSS
  containing `</script>` can't break out of the slot), and the client's
  pre-paint script + `applyTheme` read it back. It replaces the old
  server-side `<style>` injection (`inject_theme_into_html`, removed):
  the server now hands the client DATA, not pre-rendered HTML, so the
  client owns the cascade and there is exactly one theme `<style>` block
  (the client's `<style id="mobius-theme">`).

  `css` is the effective (core-var-augmented) CSS — the same value the
  old injection used and that `GET /api/theme` returns. It is passed
  through VERBATIM (no `</style>` escaping here): the client injects it
  via `<style>`.textContent, which the HTML parser never reparses, so the
  `</style>`-breakout vector that `_escape_for_style_tag` guarded against
  at server-render time no longer applies. The slot's own breakout vector
  (an embedded `</script>`) is closed by the JSON serializer's escape in
  `main.py`.

  `bundle`: pass a precomputed `load_effective_theme(data_dir)` to skip
  the per-call file reads (the `/frame` route resolves the bundle once
  for its ETag and can thread it here). When omitted it's computed.
  """
  if bundle is None:
    bundle = load_effective_theme(data_dir)
  return {"css": bundle.css, "bg": bundle.bg, "mode": bundle.mode}
