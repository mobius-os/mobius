"""Single source of truth for the default theme.

Both the shell (index.html) and the app-frame inject theme CSS from
/data/shared/theme.css.  When no theme.css exists, this default is
used.  The shell's index.css and app-frame.html should NOT define
their own :root variables — they come from here.
"""

import re
from html import escape as html_escape
from pathlib import Path

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

  /* Status colors. */
  --danger: #f87171;
  --green: #10b981;

  /* Typography. */
  --font: 'Inter', system-ui, -apple-system, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, 'SF Mono', monospace;
}
"""


def get_theme_css(data_dir: str) -> str:
  """Returns the active theme CSS — user override or default."""
  theme_path = Path(data_dir) / "shared" / "theme.css"
  if theme_path.exists():
    content = theme_path.read_text(encoding="utf-8").strip()
    if content:
      return content
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
  return m.group(1) if m else "#0c0f14"


def get_theme_mode(data_dir: str) -> str:
  """Returns the active theme mode ("dark" or "light").

  Sourced from `/data/shared/theme-mode` (a JSON-encoded string),
  written by `themeService.toggleTheme` on every mode swap. Falls
  back to "dark" — the historical default — when the file is
  missing, unreadable, or contains an unrecognized value. Used by
  the recovery page and any other server-rendered surface that
  needs to mirror the SPA's theme without re-parsing the CSS.
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
# The augmentation is SILENT — there is no log line, no header
# comment in the original theme, and `GET /api/theme` still
# returns the agent-authored source. What gets shipped to the
# browser is `<style>{augmented_css}</style>` injected by
# `inject_theme_into_html` below.
#
# Why this matters for debugging: a partial theme "works" not
# because it's complete, but because the server filled the gap.
# If a debugger looks at `theme.css` and sees only --bg / --text
# defined, the shell's surfaces are STILL rendering correctly
# because `--surface`, `--border`, etc. were silently injected
# at HTML-render time. To inspect what the browser actually
# received, look at the `<style>` block in the served HTML (or
# DevTools → Sources → the inline style tag), not at `theme.css`.
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
  defaults: dict[str, str] = {}
  for line in DEFAULT_THEME.splitlines():
    m = re.match(r"\s*(--[\w-]+)\s*:\s*([^;]+);", line)
    if m:
      defaults[m.group(1)] = m.group(2).strip()
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


def inject_theme_into_html(html: str, data_dir: str) -> str:
  """Inject the active theme CSS and background color into an HTML string.

  Replaces the </head> tag with a <style> block containing the theme CSS,
  and replaces the default #0c0f14 background color placeholder with the
  active theme's --bg color. Used by both the SPA fallback and the
  app-frame endpoint.

  Security: the theme CSS is owner-controlled via the storage API but
  the agent (running autonomously) writes it. We escape `</` sequences
  to defend against `</style><script>...` breakouts even from agent-
  authored CSS, and restrict @import URLs to http(s) schemes to block
  `javascript:` / `data:` URIs in font import declarations.

  Core variables: `_ensure_core_vars` appends defaults for any
  variable the shell relies on that the theme didn't define, so a
  partial theme can't render shell text invisibly. Otherwise the
  theme is passed through verbatim — the agent has full creative
  freedom.
  """
  css = get_theme_css(data_dir)
  bg = get_bg_color(data_dir)
  imports, css = extract_imports(css)
  safe_imports = [u for u in imports if _is_safe_import_url(u)]
  link_tags = "".join(
    f'<link rel="stylesheet" href="{html_escape(url, quote=True)}">\n'
    for url in safe_imports
  )
  css = _ensure_core_vars(css)
  safe_css = _escape_for_style_tag(css)
  html = html.replace(
    "</head>", f"{link_tags}<style>{safe_css}</style>\n</head>"
  )
  html = html.replace("background:#0c0f14", f"background:{bg}")
  html = html.replace('content="#0c0f14"', f'content="{bg}"')
  return html
