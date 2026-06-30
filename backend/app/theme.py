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

DEFAULT_THEME = """\
:root {
  /* Opaque fill colors — set by many shell components as solid
     backgrounds (.shell paints --bg across the viewport, chat
     bubbles + drawer + banners paint --surface / --surface2,
     borders are 1px lines on top of those surfaces). Keep these
     SOLID — making them rgba(..., <1) lets whatever sits behind
     bleed through and makes text unreadable.

     Palette neutralized in 2026-05: dropped the slight blue tint
     (--bg #0d0f14 → #0d0d0d, surfaces same step) so the dark
     mode reads as a true charcoal stack rather than blue-grey. */
  --bg: #0d0d0d;
  --surface: #171717;
  --surface2: #212121;
  --border: #2a2a2a;
  --border-light: #1f1f1f;

  /* Text colors — paint on top of the opaque fills above.
       2026-05-26: --muted #6b6b76 (~3.8:1 vs --bg, failed AA)
         → #9b9b9b (~6.4:1)
       2026-05-27: --muted #9b9b9b → #a8a8a8 (~6.1:1 on --surface2
         #212121, was ~5.2:1 — comfortable AA on raised surfaces
         for the small text used in section labels and provider
         status indicators). */
  --text: #ececec;
  --muted: #a8a8a8;

  /* Accent palette — small accents (buttons, links, focus rings,
     glow). Free to be vivid; --accent-dim is allowed to be
     translucent because it's used as a glow, not as a fill.
     KEEP the purple — it's the platform's brand mark. */
  --accent: #8b6cf7;
  --accent-hover: #7c5ce6;
  --accent-dim: rgba(139, 108, 247, 0.14);

  /* The ONLY legal foreground for text/icons sitting on an --accent
     or --danger FILL (a primary button, a danger button, an accent
     chip). Resolves a prior three-way split where apps hardcoded
     #fff / #0d0d0d / #062016 for that foreground. White is chosen
     for the #8b6cf7 purple accent (and the #f87171 danger), where
     it's the legible choice. A custom theme that changes --accent
     to a light color must also set --accent-fg to a dark value so
     fill-foreground contrast holds. */
  --accent-fg: #ffffff;

  /* Status colors. */
  --danger: #f87171;
  --green: #10b981;

  /* Typography. */
  --font: 'Inter', system-ui, -apple-system, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, 'SF Mono', monospace;
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
  css = get_theme_css(data_dir)
  m = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,8})", css)
  return m.group(1) if m else "#0d0d0d"


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
  palette. Used by the recovery page and any other server-rendered
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
# THEME RECOVERY AFFORDANCES
# =============================================================
# A theme that makes the shell unresponsive (full-screen overlay,
# pointer-events: none on the root, opaque ::before with z-index
# 99999, etc.) traps the user inside a broken UI. The recovery
# story is: (a) the prior theme.css is snapshotted automatically
# before every overwrite, so the agent never silently destroys
# work; (b) the recovery page has a "Reset theme" button that
# moves theme.css aside so DEFAULT_THEME paints again; (c) the
# main shell honors `?reset-theme=1` in the URL for cases where
# the user can reach `/` from the address bar but can't click
# anything inside the page.
#
# This is the "build for reversibility, not prevention" lever from
# the design philosophy. The theme is allowed to break the UI;
# recovery is trivial and reachable from outside the broken state.


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
  """
  src = Path(data_dir) / "shared" / "theme.css"
  if not src.exists():
    return None
  ts = int(time.time())
  dst = src.with_name(f"theme.css.bak-{ts}")
  shutil.copy2(src, dst)
  return str(dst)


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
# SILENT CSS-VARIABLE AUGMENT (debugger pointer)
# =============================================================
# `_ensure_core_vars` (below) appends a `:root { ... }` block to
# any theme that omits one of the variables listed in `_CORE_VARS`.
# It is applied inside `get_theme_css`, so the EFFECTIVE theme is
# complete for every consumer — `GET /api/theme`, the app-frame
# iframe, and the shell's theme-as-data JSON slot all see it. The
# augmentation is purely additive: the agent's CSS is never
# rewritten, only gap-filled. The raw, un-augmented override is
# still readable verbatim at `/api/storage/shared/theme.css` for
# editors that want the source.
#
# Why this matters for debugging: a partial theme "works" not
# because it's complete, but because `get_theme_css` filled the gap.
# If a debugger looks at `theme.css` on disk and sees only --bg /
# --text defined, the shell's surfaces are STILL rendering correctly
# because `--surface`, `--border`, --accent, etc. were injected by
# the getter. To inspect the on-disk source, read the storage file;
# to see what the browser received, read `GET /api/theme` or the
# shell's theme-as-data JSON slot.
#
# Variables augmented when missing (full list lives in
# `_CORE_VARS`):
#   --bg, --surface, --surface2, --text, --muted,
#   --accent, --accent-hover, --accent-dim,
#   --border, --border-light, --danger, --green,
#   --font, --mono
#
# This is the ONLY structural enforcement applied to agent-
# authored themes. Other patterns (blur, translucent fills,
# overlays, focus rules) are intentionally NOT rewritten — the
# right lever for those is the agent's seed/experience file, not
# server-side mutation.
_CORE_VARS = {
  # Variables the shell relies on. If the agent's theme omits any,
  # we inject the default value so the shell never falls back to an
  # invisible-on-dark-mode hardcoded literal (e.g. `var(--fg, #111)`
  # where --fg doesn't exist).
  "--bg", "--surface", "--surface2", "--text", "--muted",
  "--accent", "--accent-hover", "--accent-dim",
  "--border", "--border-light", "--danger", "--green",
  "--font", "--mono",
}


# Light-mode defaults for the structural + status vars whose correct
# value DEPENDS on mode. DEFAULT_THEME is the DARK palette, so filling a
# partial LIGHT theme.css from it injected dark surfaces/borders
# (--surface2:#212121, --border-light:#1f1f1f) appended in a cascade-
# winning :root block — "dark surfaces in light mode". These values
# mirror the frontend LIGHT_COLORS in frontend/src/theme.js so the
# server-augmented light theme matches what a client-side toggle
# produces. --accent/--accent-hover and --font/--mono are mode-agnostic
# (the brand purple + typography are shared), so they stay sourced from
# DEFAULT_THEME for both modes.
_LIGHT_DEFAULTS = {
  "--bg": "#f0eeeb",
  "--surface": "#ffffff",
  "--surface2": "#e8e6e2",
  "--border": "#d4d1cc",
  "--border-light": "#e2dfdb",
  "--text": "#1c1b1a",
  "--muted": "#6b6864",
  "--accent-dim": "rgba(139, 108, 247, 0.08)",
  "--danger": "#ef4444",
  "--green": "#059669",
}


def _infer_theme_mode(css: str) -> str:
  """Return 'light' or 'dark' for a theme's CSS by --bg luminance,
  mirroring the frontend themeService._inferThemeMode so server-side
  augmentation and a client-side toggle agree on mode from the same
  signal. A missing/unparseable --bg defaults to 'dark' (the historical
  behavior — DEFAULT_THEME is dark — so existing dark themes are
  unaffected)."""
  m = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,8})", css)
  if not m:
    return "dark"
  raw = m.group(1)[1:]
  # Normalize any valid CSS hex length to a 6-digit RRGGBB before reading
  # luminance. The short forms carry one nibble per channel, so the three
  # RGB nibbles each double (#RGB -> #RRGGBB, #RGBA -> RGB + dropped alpha);
  # the long forms already use byte-pairs, so the leading six characters are
  # RGB and a trailing alpha byte is dropped. Alpha never changes the
  # dark-vs-light direction, and dropping it keeps this in step with the
  # frontend themeService._inferThemeMode (whose slice(0, 6) classifies
  # #ffff / #ffffffff as light). Without this a 4- or 8-digit --bg fell
  # through to the dark default and injected dark structural vars into a
  # light theme.
  if len(raw) in (3, 4):
    raw = "".join(c * 2 for c in raw[:3])
  else:
    raw = raw[:6]
  try:
    r = int(raw[0:2], 16)
    g = int(raw[2:4], 16)
    b = int(raw[4:6], 16)
  except ValueError:
    return "dark"
  return "dark" if (r + g + b) / 3 < 128 else "light"


def _ensure_core_vars(css: str) -> str:
  """Append a `:root` block with default values for any core
  variable the theme omitted.

  This is the ONLY structural enforcement we apply to agent-authored
  themes. It is purely additive — your CSS is never rewritten, only
  augmented when something the shell needs is missing. The goal is
  to make sure the shell can always paint readable defaults even if
  the theme uses a totally different palette and forgets one or two
  variables, without taking creative space away.

  Other patterns we deliberately do NOT enforce — blur filters,
  translucent surfaces, fixed-position overlays, global focus rules —
  are valid design tools when used with intent. Documentation in the
  seed (and a richer DEFAULT_THEME vocabulary) is the right lever
  for those.
  """
  defined = set(re.findall(r"(--[a-zA-Z][\w-]*)\s*:", css))
  missing = _CORE_VARS - defined
  if not missing:
    return css
  # Source defaults from DEFAULT_THEME (the DARK palette), then override
  # mode-dependent vars with their LIGHT values when the theme's own --bg
  # reads as light. Without this a partial light theme.css got dark
  # surfaces/borders injected in a cascade-winning :root block.
  defaults: dict[str, str] = {}
  for line in DEFAULT_THEME.splitlines():
    m = re.match(r"\s*(--[\w-]+)\s*:\s*([^;]+);", line)
    if m:
      defaults[m.group(1)] = m.group(2).strip()
  if _infer_theme_mode(css) == "light":
    defaults.update(_LIGHT_DEFAULTS)
  injected = "\n".join(
    f"  {name}: {defaults[name]};"
    for name in sorted(missing)
    if name in defaults
  )
  if not injected:
    return css
  return css + (
    f"\n/* Möbius: injected defaults for variables the theme omitted */\n"
    f":root {{\n{injected}\n}}\n"
  )


def frame_content_rev(data_dir: str) -> str:
  """Short content hash of the shared app-frame.html, injected into
  index.html as `<meta name="mobius-frame-rev">` so AppCanvas can fold it into
  the frame URL's `?v=` cache-buster. `app.updated_at` (the rest of `?v=`)
  only advances on an app EDIT, not when the shared app-frame.html is
  REDEPLOYED — so without this rev the service worker keeps serving the stale
  frame until a 2nd-open background revalidate. Mirrors the content hash in
  routes/apps.py `_frame_etag`, which the SW cache KEY (unlike the HTTP ETag)
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
  m = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,8})", css)
  bg = m.group(1) if m else "#0d0d0d"
  mode = get_theme_mode(data_dir)
  rev = (
    hashlib.sha256(frame_path.read_bytes()).hexdigest()[:16]
    if frame_path else ""
  )
  bundle = EffectiveTheme(css=css, bg=bg, mode=mode, rev=rev)
  _EFFECTIVE_THEME_MEMO[data_dir] = (key, bundle)
  return bundle


def _resolve_frame_path(data_dir: str) -> Path | None:
  """First existing app-frame.html among the agent-editable, dev, and
  baked-in locations — the same candidate list frame_content_rev uses."""
  candidates = [
    Path(data_dir) / "shell" / "public" / "app-frame.html",
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
