"""Top-level routes that make a mini-app installable as its own PWA.

Each installed mini-app gets its own URL scope at `/apps/<slug>/`, with
a unique manifest, an icon, and an HTML shell that boots the app's
React component directly (no parent postMessage handshake — same
origin means the JWT in localStorage works as-is).

The PWA install picks up the manifest at `/apps/<slug>/manifest.json`.
The `scope` is `/apps/<slug>/`, so it does not overlap with the Möbius
shell scope, which is already narrowed to `/shell/`. That scope
separation lets install prompts for these sub-app URLs fire on
Chromium.

These routes live OUTSIDE the `/api/...` namespace because (a) they
serve user-facing HTML/manifest/image content, not JSON APIs; (b)
PWA scope is computed from the manifest URL's directory, so the
manifest MUST live at `/apps/<slug>/...` to scope correctly.

Auth: unauthenticated visitors are redirected to Möbius's login page
with a `return` param so they land back at the standalone URL after
logging in. The standalone shell uses `Cache-Control: no-cache,
must-revalidate`; only the service-worker offline cache is opted into
separately for offline-capable apps.
"""

import io
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import icon_cache, models, runtime_libs
from app.config import get_settings
from app.database import get_db
from app.theme import get_bg_color

router = APIRouter(tags=["standalone"])

_HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")


# A small deterministic palette so the same app name always produces
# the same default icon color. Picked for visual distinctness on both
# light + dark backgrounds; tuned with `--bg` shades from theme.py.
_PALETTE = [
  "#a78bfa",  # violet (matches default theme accent)
  "#6ee7b7",  # mint
  "#fbbf24",  # amber
  "#f87171",  # coral
  "#60a5fa",  # sky
  "#f472b6",  # pink
  "#34d399",  # emerald
  "#c084fc",  # lavender
]


def _color_for(slug: str) -> str:
  """Deterministic color from the slug so an app's default icon is
  stable across reloads — the user learns to recognize it before
  they upload a custom one."""
  if not slug:
    return _PALETTE[0]
  return _PALETTE[sum(ord(c) for c in slug) % len(_PALETTE)]


def _initial_for(name: str) -> str:
  """First letter of the app name, uppercased, with non-alpha
  characters skipped. Empty name falls back to '?'."""
  for ch in (name or ""):
    if ch.isalpha():
      return ch.upper()
  return "?"


def _dominant_opaque_color(icon_png: bytes | None, fallback: str = "#0c0f14") -> str:
  """Returns a #RRGGBB hex from the most common opaque-ish pixel of the
  app icon. Used to set the standalone PWA's `theme_color` /
  `background_color` so the OS splash + status bar match the icon's
  natural fill instead of being a uniform dark slab.

  A transparent icon (the canonical Möbius app-news case — a cream
  newspaper on alpha=0 background) used to render with a hardcoded
  `#0c0f14` background underneath, giving the OS-level splash a
  jarring black halo around the cream paper. Sampling the icon's
  own dominant non-transparent color, then setting BOTH theme/
  background colors to it, makes the splash bleed seamlessly into
  the icon — what we already do for Möbius itself.

  Quantizes to 32-step buckets so noise doesn't fragment the
  count. Returns `fallback` when the icon is missing or fully
  transparent.
  """
  if not icon_png:
    return fallback
  try:
    from PIL import Image
    from collections import Counter
    img = Image.open(io.BytesIO(icon_png)).convert("RGBA")
    # Downsample first — analysing 1024x1024 of pixels is wasted CPU
    # for a coarse dominant-color check. 64x64 still has 4K samples,
    # which is more than enough resolution for the most-common bucket.
    img.thumbnail((64, 64))
    buckets = Counter()
    for r, g, b, a in img.getdata():
      if a < 200:
        continue
      buckets[(r // 32 * 32, g // 32 * 32, b // 32 * 32)] += 1
    if not buckets:
      return fallback
    r, g, b = buckets.most_common(1)[0][0]
    return f"#{r:02x}{g:02x}{b:02x}"
  except Exception:
    return fallback


def _resolve_bg_hex(
  background_color: str | None, theme_color: str | None, icon_png: bytes | None
) -> str:
  """The background-color resolution as a pure function of its inputs (no ORM
  row), so it can run on a worker thread off the request's DB session. Prefers
  an explicit `background_color`/`theme_color`, else samples the icon's
  dominant opaque color."""
  for value in (background_color, theme_color):
    if isinstance(value, str) and re.match(r"^#[0-9a-fA-F]{6}$", value.strip()):
      return value.strip().lower()
  return _dominant_opaque_color(icon_png)


def _app_background_color(app: models.App) -> str:
  """Splash / status-bar / loading-shell background for the served PWA manifest.

  An explicitly declared `background_color`/`theme_color` (from mobius.json)
  wins; otherwise we fall back to the live Möbius theme `--bg`, so an app that
  declares no color gets a status bar matching the owner's current theme rather
  than a color sampled from its icon. (Icon *compositing* still samples the
  icon — see `_resolve_bg_hex` — because the solid fill behind a transparent
  icon should match the icon art, not the theme.)"""
  for value in (app.background_color, app.theme_color):
    if isinstance(value, str) and _HEX6.match(value.strip()):
      return value.strip().lower()
  return get_bg_color(get_settings().data_dir)


def _app_theme_color(app: models.App) -> str:
  if isinstance(app.theme_color, str) and _HEX6.match(app.theme_color.strip()):
    return app.theme_color.strip().lower()
  return _app_background_color(app)


def _generate_icon_png(name: str, slug: str, size: int = 512) -> bytes:
  """Default icon: a single letter centered on a colored background.

  Returns PNG bytes at the requested size. The letter is sized to
  ~55% of the canvas so it reads at small home-screen scales (the
  Android maskable safe zone clips ~12% on each edge). No
  anti-aliasing tricks — Pillow's default text rendering is plenty
  for this use.
  """
  from PIL import Image, ImageDraw, ImageFont
  bg = _color_for(slug)
  letter = _initial_for(name)
  img = Image.new("RGB", (size, size), color=bg)
  draw = ImageDraw.Draw(img)
  # Hunt for a usable bold sans-serif from the few that ship with
  # python:3.12-slim. If none of them are present, Pillow's default
  # bitmap font still draws something (tiny, but recognizable).
  font = None
  for path in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
  ):
    try:
      font = ImageFont.truetype(path, int(size * 0.55))
      break
    except OSError:
      continue
  if font is None:
    font = ImageFont.load_default()
  bbox = draw.textbbox((0, 0), letter, font=font)
  w = bbox[2] - bbox[0]
  h = bbox[3] - bbox[1]
  # bbox origin isn't at (0,0) for most fonts — subtract the offset
  # so centering uses the visible glyph bounds, not the font box.
  draw.text(
    ((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]),
    letter, fill="white", font=font,
  )
  buf = io.BytesIO()
  img.save(buf, format="PNG", optimize=True)
  return buf.getvalue()


def _get_app_by_slug(db: Session, slug: str) -> models.App:
  """Resolve `<slug>` to a LIVE App row. Also handles the lazy-backfill
  case where an old app has a NULL slug — we don't try to match
  against null, so legacy apps surface here via their lazily-assigned
  slug from the first time someone accessed them via the API.

  Excludes tombstoned (soft-deleted) apps so a home-screen PWA deep-link to
  `/apps/<slug>/` can't render an uninstalled app — same rule the in-shell
  get/module/frame routes apply (feature 110)."""
  app = (
    db.query(models.App)
    .filter(models.App.slug == slug, models.App.deleted_at.is_(None))
    .first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  return app


@router.get("/apps/{slug}/manifest.json")
def standalone_manifest(slug: str, db: Session = Depends(get_db)):
  """Per-app web app manifest.

  `id` is the stable install identity (`/apps/<slug>/`). `scope` and
  `start_url` are both `/apps/<slug>/` so the OS treats this as a
  distinct PWA from Möbius. `display` (from the app's mobius.json,
  default "standalone") removes browser chrome on launch; "fullscreen"
  additionally drops the OS status bar so a game covers the notch.
  """
  app = _get_app_by_slug(db, slug)
  base = f"/apps/{slug}/"
  # Version the icon URLs by `updated_at` so when the owner uploads
  # a fresh icon the browser refetches at install time instead of
  # baking the stale image into the home-screen entry. Microsecond
  # resolution (matching the apps-module ETag) so a name PATCH + icon
  # PUT landing in the same second still produce distinct `?v=`.
  v = int(app.updated_at.timestamp() * 1_000_000) if app.updated_at else 0
  bg = _app_background_color(app)
  theme = _app_theme_color(app)
  return JSONResponse(
    {
      "id": base,
      "name": app.name,
      "short_name": app.name[:12] if app.name else slug,
      "description": app.description or "",
      "start_url": base,
      "scope": base,
      # Per-app display mode (mobius.json `display`); defaults to
      # "standalone". Games declare "fullscreen" so the installed PWA
      # launches with no OS status bar and paints under the notch/cutout
      # (the standalone <head> already carries viewport-fit=cover).
      "display": app.display or "standalone",
      "background_color": bg,
      "theme_color": theme,
      "icons": [
        {
          "src": f"{base}icon-192.png?v={v}",
          "sizes": "192x192",
          "type": "image/png",
          "purpose": "any maskable",
        },
        {
          "src": f"{base}icon-512.png?v={v}",
          "sizes": "512x512",
          "type": "image/png",
          "purpose": "any maskable",
        },
      ],
    },
    media_type="application/manifest+json",
    # Revalidate on every fetch so a freshly-renamed app never serves a
    # stale name/short_name/icon to the OS at install time. The body is
    # tiny; `no-cache` keeps it cheap (304 when unchanged) without
    # letting the browser pin an old manifest.
    headers={"Cache-Control": "no-cache, must-revalidate"},
  )


# Match `icon-192.png` / `icon-512.png` / `icon-{N}.png`. Anything
# else 404s — we don't want the route accidentally serving arbitrary
# sizes that aren't declared in the manifest.
_ICON_NAME = re.compile(r"^icon-(\d+)\.png$")


def _render_standalone_icon(
  icon_png: bytes | None, name: str, slug: str, bg_hex: str, size: int
) -> bytes:
  """The CPU-bound render for one standalone-icon variant: resize +
  background-composite the uploaded PNG, or draw the generated letter icon.
  Pure function of its arguments (all folded — via `updated_at` — into the
  cache key), so memoizing its output is safe.

  Takes plain primitives, not the live ORM row, so it can run on a worker
  thread without that thread touching the request's DB session (the caller
  snapshots `app.icon_png` / `app.name` / the background color first)."""
  if icon_png:
    from PIL import Image
    img = Image.open(io.BytesIO(icon_png))
    if img.mode not in ("RGB", "RGBA"):
      img = img.convert("RGBA" if "A" in img.mode else "RGB")
    img = img.resize((size, size), Image.LANCZOS)
    # Composite onto the dominant-color background so transparency
    # renders as the manifest's background_color instead of black on
    # iOS / Android Chrome splash screens. The OS spec says splash
    # should fill with background_color and paint the icon on top;
    # in practice browsers vary, and a transparent PNG over a
    # background_color CSS frequently shows a halo around the icon
    # edges on iOS. Server-side composite eliminates the variability.
    if img.mode == "RGBA":
      r = int(bg_hex[1:3], 16)
      g = int(bg_hex[3:5], 16)
      b = int(bg_hex[5:7], 16)
      bg_layer = Image.new("RGB", img.size, (r, g, b))
      bg_layer.paste(img, mask=img.split()[3])
      img = bg_layer
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
  return _generate_icon_png(name, slug, size=size)


# The sizes the manifest + install shell actually request (192/512 manifest,
# 180 apple-touch). Only these get the disk+LRU cache, so a request for an
# arbitrary off-manifest size can't flood the cache directory — it is rendered
# uncached and served with the same headers.
_CACHED_STANDALONE_SIZES = frozenset((180, 192, 512))


@router.get("/apps/{slug}/{icon_name}")
async def standalone_icon(
  slug: str, icon_name: str, request: Request, db: Session = Depends(get_db),
):
  """Serves the per-app icon at the requested size.

  Two paths: user-uploaded `app.icon_png` is resized + background-composited
  on the fly via Pillow; a missing upload falls back to the auto-generated
  letter icon. Both renders are memoized in `icon_cache` keyed on the app's
  `updated_at` (which a name / icon / background change bumps), so the
  home-screen install request and the splash-screen request — and every later
  open — reuse one render instead of each re-running Pillow. The render runs
  off the threadpool on a cold miss (this handler is async), so concurrent
  icon fetches don't serialize through a synchronous resize.

  A strong-ish `ETag` on `updated_at`+size gives the browser a 304 path, and
  `max-age` + `stale-while-revalidate` keep warm opens free; an icon change
  advances the validator so a stale icon is never pinned.
  """
  m = _ICON_NAME.match(icon_name)
  if not m:
    raise HTTPException(status_code=404, detail="Not found.")
  size = int(m.group(1))
  if size < 16 or size > 1024:
    raise HTTPException(status_code=400, detail="Invalid icon size.")
  app = _get_app_by_slug(db, slug)
  ts_us = int(app.updated_at.timestamp() * 1e6) if app.updated_at else 0
  etag = f'W/"{ts_us}-{size}"'
  headers = {
    "ETag": etag,
    "Cache-Control": "public, max-age=3600, stale-while-revalidate=86400",
  }
  if request.headers.get("if-none-match") == etag:
    return Response(status_code=304, headers=headers)

  # Snapshot every value the render reads off the live ORM row HERE, on the
  # request thread, so the worker thread never touches the DB session. The
  # background color (which may itself decode the icon for dominant-color
  # sampling) is resolved INSIDE `_compute` from these snapshots, so its cost
  # lands on the cold miss only — a warm hit skips it.
  app_id = app.id
  icon_png = app.icon_png
  name = app.name
  app_slug = app.slug or slug
  bg_inputs = (app.background_color, app.theme_color)

  def _compute() -> bytes:
    bg_hex = _resolve_bg_hex(bg_inputs[0], bg_inputs[1], icon_png)
    return _render_standalone_icon(icon_png, name, app_slug, bg_hex, size)

  if size in _CACHED_STANDALONE_SIZES:
    body = await icon_cache.get_or_compute(
      app_id=app_id,
      updated_us=ts_us,
      kind="standalone",
      size=size,
      compute=_compute,
    )
  else:
    body = await run_in_threadpool(_compute)
  return Response(content=body, media_type="image/png", headers=headers)


@router.get("/apps/{slug}/", response_class=HTMLResponse)
@router.get("/apps/{slug}", response_class=HTMLResponse)
def standalone_shell(slug: str, db: Session = Depends(get_db)):
  """Standalone HTML shell for the installed PWA.

  This is the page the home-screen launcher opens. It does NOT live
  inside the Möbius shell SPA — the user has no drawer, no toolbar,
  no chat. Just the app, plus a small "Edit in Möbius" floating
  affordance.

  Auth note: this page renders publicly (no token check). The token
  lookup happens client-side from localStorage (same origin as the
  shell, so the owner's JWT is readable). Unauthenticated visitors
  see the shell briefly, then it redirects to the Möbius login page
  with a return-URL. We deliberately don't 401 server-side because
  the standalone PWA needs to be installable before login (the
  browser fetches the manifest + icons during install, and a 401 on
  the start_url would break the install flow).
  """
  # `_get_app_by_slug` finds rows by exact slug match, so anything
  # that resolves here already has a slug populated — the proactive
  # migration backfill ensures legacy NULL-slug rows are filled at
  # boot time. The earlier lazy-ensure call here was dead code.
  app = _get_app_by_slug(db, slug)
  app_id = app.id
  app_name = app.name or slug
  # Cache-bust the install-card icon preview + tab/apple-touch icons on
  # every app update (same microsecond version the manifest uses), so a
  # just-changed name/icon doesn't show a stale preview here while the
  # 5-minute icon cache is warm.
  app_v = int(app.updated_at.timestamp() * 1_000_000) if app.updated_at else 0
  # Escape user-controlled strings before interpolating into HTML.
  # The agent generates app names so they're nominally trusted, but
  # belt-and-suspenders: a stray `<script>` in a name would otherwise
  # execute in the standalone scope with the user's JWT.
  from html import escape
  app_name_html = escape(app_name)
  # JSON-encode for safe inline-script embedding. json.dumps handles
  # quotes, backslashes, control chars, and U+2028/U+2029 (Python's
  # json module escapes them by default). We additionally neutralize
  # `</` to prevent script-tag breakout when the literal is emitted
  # inside <script>. json.dumps already wraps the result in double
  # quotes, so callers interpolate it bare.
  app_name_js_literal = json.dumps(app_name).replace("</", "<\\/")
  app_bg = _app_background_color(app)
  # Single source of truth: pull the mini-app importmap from app-frame.html
  # instead of carrying a hand-synced brace-doubled copy here. Precomputed as a
  # ready-to-embed <script> string so the f-string interpolates {import_map_html}
  # with no brace-doubling (the JSON's own braces never reach str.format).
  import_map_html = (
    '<script type="importmap">\n' + runtime_libs.importmap_block() + "\n</script>"
  )
  html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1, user-scalable=no, viewport-fit=cover" />
  <meta name="referrer" content="no-referrer">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="{app_name_html}">
  <title>{app_name_html}</title>
  <link rel="manifest" href="/apps/{slug}/manifest.json">
  <link rel="icon" type="image/png" sizes="192x192" href="/apps/{slug}/icon-192.png?v={app_v}">
  <link rel="apple-touch-icon" href="/apps/{slug}/icon-192.png?v={app_v}">
  {import_map_html}
  <style>
    :root {{
      --bg: {app_bg}; --surface: #14181f; --surface2: #1a1f28;
      --border: #252b36; --text: #d4d4d8; --muted: #52525b;
      --accent: #a78bfa; --accent-hover: #c4b5fd;
      --danger: #f87171;
      --font: 'Inter', system-ui, sans-serif;
    }}
    /* Prevent iOS Safari page-level pinch-zoom (user-scalable=no is ignored
       by iOS Safari; this CSS lock covers that gap). Elements with their own
       pinch gesture set touch-action: none to override. */
    html, body {{ touch-action: pan-x pan-y; }}
    /* No grey/blue tap-flash on any interactive element across mini-apps (mobile). */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }}
    /* Native-app feel: no text-selection box over buttons / UI chrome. Inputs,
       textareas and contenteditable (incl. CodeMirror editors) stay selectable. */
    body {{ -webkit-user-select: none; user-select: none; }}
    input, textarea, [contenteditable], [contenteditable] * {{ -webkit-user-select: text; user-select: text; }}
    html, body, #root {{ height: 100%; }}
    body {{
      background: var(--bg); color: var(--text);
      font-family: var(--font); font-size: 14px;
    }}
    #loading {{
      position: fixed; inset: 0;
      display: flex; align-items: center; justify-content: center;
      flex-direction: column; gap: 12px;
      background: var(--bg); color: var(--muted);
      font-size: 13px;
    }}
    #loading.hidden {{ display: none; }}
    .spinner {{
      width: 24px; height: 24px;
      border: 2px solid var(--border); border-top-color: var(--accent);
      border-radius: 50%; animation: spin 0.8s linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    /* Install confirm card. Centered modal overlay shown when the user
       explicitly asked to install (drawer ⋮ → Install lands here with
       `?install=1`). Has two action tiers — silent native prompt when
       BIP is available, platform-specific manual guidance when it
       isn't — because beforeinstallprompt is unreliable for sub-PWAs
       that share an origin with an already-installed parent. */
    #install-backdrop {{
      position: fixed; inset: 0; background: rgba(0,0,0,0.55);
      backdrop-filter: blur(4px);
      z-index: 9998; opacity: 0; pointer-events: none;
      transition: opacity 0.18s ease;
    }}
    #install-backdrop.visible {{ opacity: 1; pointer-events: auto; }}
    #install-card {{
      position: fixed; top: 50%; left: 50%;
      width: calc(100% - 32px); max-width: 420px;
      max-height: calc(100% - 32px);
      overflow-y: auto;
      background: var(--surface, #14181f); color: var(--text, #d4d4d8);
      border: 1px solid var(--border, #252b36);
      border-radius: 20px;
      padding: 22px;
      box-shadow: 0 24px 60px rgba(0,0,0,0.55);
      font-family: var(--font);
      z-index: 9999;
      transform: translate(-50%, -50%) scale(0.92);
      opacity: 0;
      pointer-events: none;
      transition: transform 0.22s cubic-bezier(.2,.7,.2,1),
                  opacity 0.18s ease;
    }}
    #install-card.visible {{
      transform: translate(-50%, -50%) scale(1);
      opacity: 1;
      pointer-events: auto;
    }}
    .ic-row {{ display: flex; gap: 14px; align-items: center; }}
    .ic-icon-wrap {{
      position: relative; width: 64px; height: 64px; flex: 0 0 64px;
      border-radius: 14px; overflow: hidden;
      background: var(--surface2, #1a1f28);
      cursor: pointer;
    }}
    .ic-icon-wrap:focus-visible {{ outline: 2px solid var(--accent, #a78bfa); }}
    .ic-icon {{ width: 100%; height: 100%; display: block; object-fit: cover; }}
    .ic-icon-edit {{
      position: absolute; bottom: 0; right: 0;
      width: 22px; height: 22px;
      background: var(--accent, #a78bfa); color: #0c0f14;
      border-radius: 50%; border: 2px solid var(--surface, #14181f);
      display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 700; line-height: 1;
    }}
    .ic-text {{ flex: 1; min-width: 0; }}
    .ic-title {{ font-size: 16px; font-weight: 600; color: var(--text, #d4d4d8); }}
    .ic-sub {{ font-size: 12px; color: var(--muted, #9ca3af); margin-top: 2px; }}
    .ic-hint {{
      font-size: 12px; color: var(--muted, #9ca3af);
      margin-top: 12px; line-height: 1.45;
    }}
    .ic-actions {{ display: flex; gap: 10px; margin-top: 16px; }}
    .ic-btn {{
      flex: 1; padding: 12px 16px; border-radius: 12px;
      border: none; font-family: inherit; font-size: 14px; font-weight: 600;
      cursor: pointer; transition: opacity 0.15s;
    }}
    .ic-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .ic-btn--primary {{ background: var(--accent, #a78bfa); color: #0c0f14; }}
    .ic-btn--secondary {{
      background: transparent; color: var(--muted, #9ca3af);
      border: 1px solid var(--border, #252b36);
    }}
    .ic-fallback {{
      display: none; margin-top: 14px;
      padding: 14px 16px; border-radius: 12px;
      background: var(--surface2, #1a1f28); color: var(--text, #d4d4d8);
      font-size: 13px; line-height: 1.55;
      border: 1px solid var(--border, #252b36);
    }}
    .ic-fallback.visible {{
      display: block;
      animation: ic-fallback-pulse 1.6s ease-out;
    }}
    .ic-fallback strong {{ color: var(--accent, #a78bfa); font-weight: 600; }}
    .ic-fallback-arrow {{
      display: inline-block; margin-right: 6px;
      animation: ic-arrow-bounce 1.6s ease-in-out infinite;
    }}
    @keyframes ic-fallback-pulse {{
      0%   {{ box-shadow: 0 0 0 0 rgba(167, 139, 250, 0.5); }}
      60%  {{ box-shadow: 0 0 0 10px rgba(167, 139, 250, 0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(167, 139, 250, 0); }}
    }}
    @keyframes ic-arrow-bounce {{
      0%, 100% {{ transform: translateY(0); }}
      50%      {{ transform: translateY(-4px); }}
    }}
    .ic-success {{ display: none; text-align: center; padding: 8px 0; }}
    .ic-success.visible {{ display: block; }}
    .ic-success-icon {{
      width: 48px; height: 48px; margin: 0 auto 12px;
      border-radius: 50%; background: var(--accent, #a78bfa); color: #0c0f14;
      display: flex; align-items: center; justify-content: center;
      font-size: 24px; font-weight: 700;
    }}
    #ic-toast {{
      position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
      background: var(--surface2, #1a1f28); color: var(--text, #d4d4d8);
      padding: 10px 18px; border-radius: 10px;
      font-size: 13px; font-family: var(--font);
      box-shadow: 0 4px 14px rgba(0,0,0,0.4);
      z-index: 10001; opacity: 0;
      transition: opacity 0.2s;
      pointer-events: none;
    }}
    #ic-toast.visible {{ opacity: 1; }}
    /* Live-update pill (feature 214): a non-disruptive "Updated — tap to
       refresh" OFFER anchored bottom-center. Active use is sacred — the shell
       NEVER auto-reloads; the pill is the offer and the tap is the apply.
       Themed from the shell's own CSS variables so it tracks the owner's
       theme. Hidden via visibility (not display) so it stays out of the tab
       order and the a11y tree until an update lands. */
    #update-pill {{
      position: fixed; left: 50%; bottom: 24px;
      transform: translateX(-50%) translateY(8px);
      display: flex; align-items: center; gap: 8px;
      max-width: calc(100% - 32px);
      padding: 10px 18px; border-radius: 999px;
      background: var(--surface, #14181f); color: var(--text, #d4d4d8);
      border: 1px solid var(--border, #252b36);
      box-shadow: 0 6px 22px rgba(0,0,0,0.45);
      font-family: var(--font); font-size: 13px; font-weight: 600;
      cursor: pointer;
      z-index: 10002;
      opacity: 0; visibility: hidden; pointer-events: none;
      transition: opacity 0.2s ease, transform 0.2s ease;
    }}
    #update-pill.visible {{
      opacity: 1; visibility: visible; pointer-events: auto;
      transform: translateX(-50%) translateY(0);
    }}
    #update-pill::before {{
      content: '\\21bb'; color: var(--accent, #a78bfa);
      font-size: 15px; font-weight: 700; line-height: 1;
    }}
    #update-pill:focus-visible {{ outline: 2px solid var(--accent, #a78bfa); }}
    /* Respect reduced-motion on the entrance: pin the transform so only
       opacity fades in (no slide). */
    @media (prefers-reduced-motion: reduce) {{
      #update-pill {{
        transform: translateX(-50%); transition: opacity 0.2s ease;
      }}
      #update-pill.visible {{ transform: translateX(-50%); }}
    }}
    </style>
</head>
<body>
  <script>
    // Register the shared root-scoped SW (/sw.js, scope /) so
    // standalone-only launches — the user installed this mini-app but
    // never opened the Möbius shell — still get the offline navigation
    // fallback that keeps the PWA in standalone mode offline. No
    // auto-reload watchdog (that's a shell concern); a silent SW swap
    // here is harmless. Fire-and-forget so it never delays BIP capture.
    if ('serviceWorker' in navigator) {{
      navigator.serviceWorker.register('/sw.js', {{ updateViaCache: 'none' }}).catch(function(){{}});
    }}
  </script>
  <script>
    // Early BIP capture — MUST run before any async script load.
    // Chromium fires `beforeinstallprompt` shortly after
    // DOMContentLoaded; the module script below waits for the React
    // imports + theme fetch + app-token fetch before attaching its
    // listener. By that time BIP has already fired and been dropped.
    //
    // This handler stashes the event on `window.__bipDeferred` and
    // dispatches a `mobius:bip-ready` event so the later module can
    // wire up the install UI. `appinstalled` is captured here too for
    // the same reason — it can fire before our module finishes booting
    // (the user can install via Chrome's own ⋮ menu while the app is
    // still loading).
    (function() {{
      // Platform detection. Used by the install card to tailor the
      // fallback hint per browser/OS, since PWA install affordances
      // differ wildly:
      //   - Chrome/Edge Android  : ⋮ → Install app
      //   - iOS Safari           : Share ↑ → Add to Home Screen
      //   - iOS Chrome/Firefox   : impossible (until iOS 17.4 EU)
      //   - Firefox Android      : ⋮ → Install
      //   - Desktop Chrome/Edge  : install icon in address bar
      //   - Desktop Firefox/Safari : varies, mostly menu items
      // Detection is UA-based, which is fragile but acceptable for
      // a hint that's user-actionable — if we guess wrong, the user
      // still has the menu. Feature-detect when possible (matchMedia,
      // navigator.standalone for iOS Safari).
      function detectPlatform() {{
        const ua = navigator.userAgent || '';
        const ios = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
        // iOS bundle: every browser is Safari/WebKit (Apple gates
        // engine choice until iOS 17.4 EU). We test for the non-
        // Safari iOS shells explicitly — CriOS=Chrome, FxiOS=Firefox,
        // EdgiOS=Edge, OPiOS=Opera, GSA=Google app.
        const iosNonSafari = ios && /CriOS|FxiOS|EdgiOS|OPiOS|GSA/.test(ua);
        const iosSafari = ios && !iosNonSafari;
        const android = /Android/.test(ua);
        const samsung = /SamsungBrowser/.test(ua);
        const edge = /\\bEdg\\//.test(ua);
        const firefox = /Firefox|FxiOS/.test(ua);
        // Chromium check — Chrome OR Edge OR Samsung (the BIP-capable
        // family). CriOS is Chrome-on-iOS which is Safari-engine so
        // does NOT support BIP, exclude it.
        const chromium = !ios && (
          (/Chrome/.test(ua) && !/Edge\\//.test(ua)) || edge || samsung
        );
        const desktop = !ios && !android;
        return {{
          ua: ua,
          ios: ios,
          iosSafari: iosSafari,
          iosNonSafari: iosNonSafari,
          android: android,
          chromium: chromium,
          edge: edge,
          firefox: firefox,
          samsung: samsung,
          desktop: desktop,
          // BIP can fire here? (Chromium-family browsers only.)
          bip_capable: chromium,
          // PWA install possible at all?
          install_possible: iosSafari || chromium ||
            // Firefox desktop has limited install support via menu
            (firefox && !ios),
        }};
      }}
      window.__mobiusPlatform = detectPlatform();

      window.addEventListener('beforeinstallprompt', function(e) {{
        e.preventDefault();
        window.__bipDeferred = e;
        window.dispatchEvent(new CustomEvent('mobius:bip-ready'));
      }});

      window.addEventListener('appinstalled', function() {{
        window.__bipDeferred = null;
        window.dispatchEvent(new CustomEvent('mobius:installed'));
      }});
    }})();
  </script>
  <div id="root"></div>
  <div id="loading"><div class="spinner"></div><div>Loading {app_name_html}…</div></div>
  <div id="install-backdrop" aria-hidden="true"></div>
  <div id="install-card" role="dialog" aria-labelledby="ic-title" aria-modal="true">
    <div id="ic-body">
      <div class="ic-row">
        <button id="ic-icon-btn" class="ic-icon-wrap" type="button" aria-label="Change icon">
          <img id="ic-icon-img" class="ic-icon" alt="" src="/apps/{slug}/icon-192.png?v={app_v}">
          <span class="ic-icon-edit" aria-hidden="true">✎</span>
        </button>
        <div class="ic-text">
          <div class="ic-title" id="ic-title">Install {app_name_html}</div>
          <div class="ic-sub">to your home screen</div>
        </div>
      </div>
      <div class="ic-hint">Tap the icon to upload a custom image, or skip to keep the default.</div>
      <div class="ic-actions">
        <button id="ic-cancel" class="ic-btn ic-btn--secondary" type="button">Maybe later</button>
        <button id="ic-install" class="ic-btn ic-btn--primary" type="button">Install</button>
      </div>
      <div id="ic-fallback" class="ic-fallback"></div>
    </div>
    <div id="ic-success" class="ic-success">
      <div class="ic-success-icon" aria-hidden="true">✓</div>
      <div class="ic-title" id="ic-success-title">{app_name_html} is on your home screen</div>
      <div class="ic-actions">
        <button id="ic-done" class="ic-btn ic-btn--primary" type="button">Got it</button>
      </div>
    </div>
  </div>
  <input id="ic-file" type="file" accept="image/png,image/jpeg,image/webp" hidden>
  <div id="ic-toast" role="status" aria-live="polite"></div>
  <button id="update-pill" type="button" aria-live="polite">Updated — tap to refresh</button>
  <script type="module">
    const APP_ID = {app_id};
    const APP_SLUG = {json.dumps(slug)};
    const APP_NAME = {app_name_js_literal};
    const APP_VERSION = {app_v};

    // Auth: read the owner JWT from localStorage (same origin so it's
    // visible). If missing, redirect to login with a return URL.
    const token = localStorage.getItem('token');
    if (!token) {{
      const ret = encodeURIComponent(window.location.pathname);
      window.location.href = '/?return=' + ret;
    }} else {{
      // Live-update pill (feature 214). An installed standalone PWA otherwise
      // never learns the agent edited its app mid-build: the in-shell preview
      // hot-updates on app_updated, but this separate /apps/<slug>/ scope did
      // not. Subscribe to THIS app's own update stream and OFFER a refresh; the
      // shell NEVER auto-reloads (active use is sacred - the pill is the offer,
      // the tap is the apply).
      let liveUpdatesStarted = false;
      function startLiveUpdates(appToken) {{
        // Guard the retry path: loadAndRender can run twice (transient-failure
        // auto-retry / manual Try again), but the subscription starts once.
        if (liveUpdatesStarted) return;
        liveUpdatesStarted = true;
        const pill = document.getElementById('update-pill');
        let controller = null;
        let backoffMs = 1000;
        let stopped = false;
        let reconnectTimer = null;
        // SINGLE-FLIGHT invariant: at most one connect() owns the stream.
        // connect() and disconnect() each bump `generation`; a connect
        // captures its value on entry and bails after any await once
        // superseded. Without this, a hide->show during the reconnect sleep
        // started a second stream while the first still slept - each
        // overwrote `controller`, so disconnect() aborted only the newest
        // and orphaned streams accumulated one per visibility flip.
        let generation = 0;

        function showPill() {{
          // Re-assign the text so the aria-live region announces on reveal.
          pill.textContent = 'Updated \\u2014 tap to refresh';
          pill.classList.add('visible');
        }}

        // Tap APPLIES the update. A plain location.reload() can serve STALE for
        // an offline_capable app: the SW caches /apps/<slug>/ cache-first, so
        // the reload returns the old HTML (old baked APP_VERSION, hence old
        // module ?v=). Rotating a fresh ?v= on the URL changes the standalone-
        // nav cache key, forcing a cache miss then a network fetch of fresh
        // HTML whose new APP_VERSION busts the module cache key in turn - the
        // ?v= freshness scheme docs/offline.md prescribes.
        pill.addEventListener('click', function() {{
          // Offline guard: the rotated ?v= is BY DESIGN a SW cache miss (v is
          // part of the standalone-nav cache key), so navigating offline
          // rejects at the network and the SW catch handler swaps a WORKING
          // cached app for the branded offline page. Refuse the navigation,
          // say why, and re-offer when connectivity returns (the 'online'
          // listener below restores the actionable copy).
          if (navigator.onLine === false) {{
            pill.textContent =
              'You\\u2019re offline \\u2014 refresh when back online';
            return;
          }}
          const u = new URL(window.location.href);
          u.searchParams.set('v', String(Date.now()));
          window.location.replace(u.pathname + u.search + u.hash);
        }});

        window.addEventListener('online', function() {{
          // A pending offer whose tap was refused offline becomes actionable
          // again the moment connectivity returns.
          if (pill.classList.contains('visible')) showPill();
        }});

        function clearReconnect() {{
          if (reconnectTimer !== null) {{
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
          }}
        }}

        async function connect() {{
          if (stopped || document.hidden) return;
          // Adopt a fresh generation and cancel any pending reconnect wakeup:
          // from here on this call is the sole owner of the stream.
          const gen = ++generation;
          clearReconnect();
          controller = new AbortController();
          try {{
            const res = await fetch('/api/apps/' + APP_ID + '/events', {{
              headers: {{ Authorization: 'Bearer ' + appToken }},
              signal: controller.signal,
            }});
            if (gen !== generation) return;  // superseded while awaiting
            // A 401 means the app token is stale; retrying it would loop
            // forever. A genuine relaunch mints a fresh token, so stop here.
            if (res.status === 401) {{ stopped = true; return; }}
            if (!res.ok || !res.body) throw new Error('events ' + res.status);
            backoffMs = 1000;  // reset on a clean connect
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (!stopped) {{
              const chunk = await reader.read();
              if (gen !== generation) return;  // superseded while awaiting
              if (chunk.done) break;
              buffer += decoder.decode(chunk.value, {{ stream: true }});
              let nl;
              while ((nl = buffer.indexOf('\\n\\n')) !== -1) {{
                const frame = buffer.slice(0, nl);
                buffer = buffer.slice(nl + 2);
                for (const line of frame.split('\\n')) {{
                  if (!line.startsWith('data: ')) continue;
                  try {{
                    const ev = JSON.parse(line.slice(6));
                    // The server already filters to THIS app's app_updated;
                    // the client id-check is belt-and-suspenders.
                    if (ev && ev.type === 'app_updated' &&
                        String(ev.appId) === String(APP_ID)) {{
                      showPill();
                    }}
                  }} catch (e) {{ /* malformed frame - skip */ }}
                }}
              }}
            }}
          }} catch (e) {{
            if (stopped || gen !== generation ||
                (e && e.name === 'AbortError')) return;
          }} finally {{
            // Only the CURRENT owner may clear the shared handle - a
            // superseded connect must not null out its successor's controller.
            if (gen === generation) controller = null;
          }}
          // Reconnect via a CANCELLABLE timer, never an awaited sleep: hidden/
          // pagehide clears the timer, and its wakeup re-enters connect()
          // (which bumps the generation), so a hide->show during the backoff
          // can never stack a second stream on top of this one.
          if (!stopped && !document.hidden && gen === generation) {{
            const delay = backoffMs;
            backoffMs = Math.min(backoffMs * 2, 30000);
            reconnectTimer = setTimeout(function() {{
              reconnectTimer = null;
              connect();
            }}, delay);
          }}
        }}

        function disconnect() {{
          // Full teardown of the single-flight invariant: invalidate any
          // in-flight connect (its next stale-generation check returns
          // early), cancel the pending reconnect wakeup, then abort the
          // socket.
          generation++;
          clearReconnect();
          if (controller) {{ try {{ controller.abort(); }} catch (e) {{}} }}
          controller = null;
        }}

        // Battery/lifecycle: hold the socket open only while the app is
        // visible. Backgrounding aborts it; returning to the foreground
        // reconnects. No polling, no wakeful timers. (SystemBroadcast has no
        // catch-up, so an edit made entirely while backgrounded is not
        // replayed on resume - acceptable for v1; the live cross-device build
        // case is foreground.)
        document.addEventListener('visibilitychange', function() {{
          if (document.hidden) {{
            disconnect();
          }} else if (!stopped) {{
            backoffMs = 1000;
            connect();
          }}
        }});
        // pagehide (BFCache freeze / navigation away) tears the stream down
        // through the same cancellation path as backgrounding.
        window.addEventListener('pagehide', disconnect);

        connect();
      }}

      // Module load can fail transiently during PWA install transitions,
      // SW state swaps, and minibrowser-overlay contexts — wrap so we
      // can silently auto-retry once, then surface a Retry button for
      // the user if the second attempt also fails.
      async function loadAndRender(cacheBust) {{
        // Reject-resilient: offline these fetches THROW (network error),
        // not return a non-ok response. Degrade instead of failing the
        // whole boot, so an offline-capable app whose code is cached
        // still renders — theme falls back to the inline default
        // palette, the app token falls back to the owner JWT (which
        // authenticates as owner against /api/storage; offline that
        // call queues anyway via window.mobius).
        const [themeRes, tokenRes] = await Promise.all([
          fetch('/api/theme', {{ headers: {{ Authorization: 'Bearer ' + token }} }}).catch(function(){{ return null }}),
          fetch('/api/auth/app-token', {{
            method: 'POST',
            headers: {{
              'Content-Type': 'application/json',
              Authorization: 'Bearer ' + token,
            }},
            body: JSON.stringify({{ app_id: APP_ID }}),
          }}).catch(function(){{ return null }}),
        ]);
        if (themeRes && themeRes.ok) {{
          const theme = await themeRes.json();
          if (theme.css) {{
            const style = document.createElement('style');
            style.textContent = theme.css;
            document.head.appendChild(style);
          }}
          if (theme.bg) document.documentElement.style.setProperty('--bg', theme.bg);
        }}
        const appToken = (tokenRes && tokenRes.ok) ? (await tokenRes.json()).token : token;
        // Expose window.mobius (offline storage queue + sync on
        // reconnect, Tier 4b) before rendering so the component sees it
        // on mount. Precached, so the import resolves offline.
        try {{
          const rt = await import('/mobius-runtime.js');
          rt.init({{ appId: APP_ID, getToken: async function(){{ return appToken; }} }});
        }} catch (e) {{}}
        const bust = cacheBust ? '&_=' + Date.now() : '';
        // &v=APP_VERSION is REQUIRED as the SW offline cache-buster: the server
        // /module route ignores v (its ETag keys on app.updated_at), but the SW
        // offline handler keeps v in its cache key (it strips token, _ and install but keeps v), so
        // an app update changes the key and forces a fresh fetch.
        const module = await import(
          '/api/apps/' + APP_ID + '/module?token=' +
          encodeURIComponent(appToken) + '&v=' + encodeURIComponent(APP_VERSION) + bust
        );
        const Component = module.default;
        if (!Component) throw new Error('App module has no default export');
        const React = await import('react');
        const {{ createRoot }} = await import('react-dom/client');
        const root = createRoot(document.getElementById('root'));
        root.render(React.createElement(Component, {{ appId: APP_ID, token: appToken }}));
        document.getElementById('loading').classList.add('hidden');
        // Offer live updates once the app is actually on screen.
        startLiveUpdates(appToken);
      }}

      function paintLoadError(err, allowRetry) {{
        const loading = document.getElementById('loading');
        // Build error UI via DOM nodes (not innerHTML) — err.message
        // can carry attacker-controlled strings from a misbehaving
        // app module, and the standalone shell sits on the same
        // origin as Möbius (an injected <script> would have JWT
        // access via localStorage).
        loading.textContent = '';
        const msg = document.createElement('div');
        msg.style.color = 'var(--danger)';
        msg.style.fontSize = '13px';
        msg.style.maxWidth = '420px';
        msg.style.textAlign = 'center';
        msg.style.lineHeight = '1.5';
        msg.textContent = 'Failed to load: ' + (err && err.message || String(err));
        loading.appendChild(msg);
        if (allowRetry) {{
          const btn = document.createElement('button');
          btn.textContent = 'Try again';
          btn.style.cssText =
            'margin-top:16px;background:var(--accent,#a78bfa);color:#0c0f14;' +
            'border:none;border-radius:8px;padding:10px 20px;font-size:13px;' +
            'font-weight:600;font-family:inherit;cursor:pointer';
          btn.onclick = () => {{
            loading.textContent = '';
            const sp = document.createElement('div');
            sp.className = 'spinner';
            const tx = document.createElement('div');
            tx.textContent = 'Loading…';
            loading.appendChild(sp);
            loading.appendChild(tx);
            loadAndRender(true).catch((e) => paintLoadError(e, true));
          }};
          loading.appendChild(btn);
        }}
      }}

      try {{
        await loadAndRender(false);
      }} catch (firstErr) {{
        // Silent auto-retry once with cache-bust — covers the common
        // transient-network case during PWA install/SW swap. If it
        // also fails, surface a manual Retry button.
        try {{
          await new Promise(r => setTimeout(r, 400));
          await loadAndRender(true);
        }} catch (secondErr) {{
          paintLoadError(secondErr, true);
        }}
      }}
    }}

    // Install confirm card controller. The Install button calls
    // `BeforeInstallPromptEvent.prompt()` directly when we have the
    // deferred event (captured by the early <script> at top of body
    // and stashed on `window.__bipDeferred`); otherwise it reveals a
    // platform-specific manual-steps panel.
    //
    // Visibility rules:
    //   - `?install=1` in URL → ALWAYS show (drawer-initiated intent),
    //     even if display-mode reports standalone. The surrounding
    //     parent-PWA window can report standalone while the sub-app
    //     itself isn't installed, so display-mode alone can't suppress.
    //   - Without `?install=1`: skip when this app's PWA is already
    //     running standalone (nothing to install), OR when the user
    //     dismissed earlier this session.
    (function setupInstallCard() {{
      const platform = window.__mobiusPlatform || {{}};

      // Element handles. All of these are rendered above in the same
      // template — a missing node here means the template was edited
      // without updating this controller, which is a build-time bug,
      // not a runtime condition worth guarding against.
      const backdrop = document.getElementById('install-backdrop');
      const card = document.getElementById('install-card');
      const body = document.getElementById('ic-body');
      const success = document.getElementById('ic-success');
      const successTitle = document.getElementById('ic-success-title');
      const iconImg = document.getElementById('ic-icon-img');
      const iconBtn = document.getElementById('ic-icon-btn');
      const fileInput = document.getElementById('ic-file');
      const installBtn = document.getElementById('ic-install');
      const cancelBtn = document.getElementById('ic-cancel');
      const doneBtn = document.getElementById('ic-done');
      const fallback = document.getElementById('ic-fallback');
      const toast = document.getElementById('ic-toast');

      // ----- Platform-specific copy + behavior -----
      // The fallback hint, install button label, and whether to show
      // the card at all all branch on what install path the user's
      // browser actually exposes. Centralized here so the controller
      // below stays agnostic.
      function copyForPlatform() {{
        if (platform.iosSafari) {{
          return {{
            // The steps panel is pre-revealed for every non-BIP path
            // (see preReveal below), so the instructions are already on
            // screen — a "Show install steps" button would just be a
            // redundant tap revealing what's visible. Make the button a
            // plain dismiss instead.
            installLabel: 'Got it',
            dismissOnAction: true,
            // No BIP on iOS Safari — install is always the manual
            // Share menu path. The panel is pre-revealed; nothing to do
            // on tap but dismiss.
            bipExpected: false,
            fallbackHTML:
              '<span class="ic-fallback-arrow" aria-hidden="true">↓</span>' +
              'Tap the <strong>Share</strong> button below ' +
              '<span aria-hidden="true">(the square with the up-arrow)</span>, ' +
              'then choose <strong>Add to Home Screen</strong>.',
          }};
        }}
        if (platform.iosNonSafari) {{
          return {{
            installLabel: 'Open in Safari',
            // iOS Chrome / Firefox / Edge are Safari-engine shells
            // that don't expose install. The user must literally
            // open the URL in Safari.app.
            bipExpected: false,
            unsupported: true,
            fallbackHTML:
              'On iPhone and iPad, only <strong>Safari</strong> can install ' +
              'web apps. Copy this page\\'s address and open it in Safari, ' +
              'then tap Share → <strong>Add to Home Screen</strong>.',
          }};
        }}
        if (platform.firefox && platform.android) {{
          return {{
            // Steps pre-revealed (non-BIP path) — button just dismisses.
            installLabel: 'Got it',
            dismissOnAction: true,
            bipExpected: false,
            fallbackHTML:
              '<span class="ic-fallback-arrow" aria-hidden="true">↑</span>' +
              'Tap the <strong>⋮</strong> menu at the top right, then choose ' +
              '<strong>Install</strong>.',
          }};
        }}
        if (platform.firefox && platform.desktop) {{
          return {{
            // Steps pre-revealed (non-BIP path) — button just dismisses.
            installLabel: 'Got it',
            dismissOnAction: true,
            bipExpected: false,
            unsupported: true,
            fallbackHTML:
              'Firefox on desktop doesn\\'t install web apps. Open this ' +
              'page in <strong>Chrome</strong> or <strong>Edge</strong>, ' +
              'or use a bookmark.',
          }};
        }}
        if (platform.chromium && platform.android) {{
          return {{
            installLabel: 'Install',
            bipExpected: true,
            // The user successfully used this path in the 11:41 trace.
            // Bottom-bar ⋮ on Chrome Android, top-bar ⋮ on Edge/Samsung.
            fallbackHTML:
              '<span class="ic-fallback-arrow" aria-hidden="true">↑</span>' +
              'Tap the <strong>⋮</strong> menu in the address bar, then ' +
              '<strong>Install app</strong> ' +
              '<span aria-hidden="true">(or <strong>Add to Home screen</strong>)</span>.',
          }};
        }}
        if (platform.chromium && platform.desktop) {{
          return {{
            installLabel: 'Install',
            bipExpected: true,
            fallbackHTML:
              '<span class="ic-fallback-arrow" aria-hidden="true">↑</span>' +
              'Click the <strong>install icon</strong> ' +
              '<span aria-hidden="true">(⊕ on the right side of the address bar)</span>, ' +
              'or open the <strong>⋮</strong> menu and choose ' +
              '<strong>Install {app_name_html}</strong>.',
          }};
        }}
        // Fallback for unknown browsers — generic instruction. Steps
        // pre-revealed (non-BIP path) — button just dismisses.
        return {{
          installLabel: 'Got it',
          dismissOnAction: true,
          bipExpected: false,
          fallbackHTML:
            'Look for an <strong>Install</strong> or <strong>Add to ' +
            'Home Screen</strong> option in your browser\\'s menu ' +
            '<span aria-hidden="true">(usually ⋮ or ⋯)</span>.',
        }};
      }}
      const copy = copyForPlatform();
      installBtn.textContent = copy.installLabel;
      fallback.innerHTML = copy.fallbackHTML;

      // `?install=1` is the drawer's intent signal — see the
      // visibility-rules comment above. Strip it after reading so a
      // refresh doesn't keep retriggering the card.
      const url = new URL(window.location.href);
      const forceShow = url.searchParams.has('install');
      if (forceShow && window.history && window.history.replaceState) {{
        try {{
          url.searchParams.delete('install');
          window.history.replaceState(
            null, '', url.pathname + url.search + url.hash
          );
        }} catch (_) {{}}
      }}

      // Skip silently when this sub-app is already running standalone
      // and the user didn't ask for the card via `?install=1` — they
      // launched from the home screen and there's nothing to install.
      const displayStandalone = window.matchMedia(
        '(display-mode: standalone)'
      ).matches;
      const skipAlreadyInstalled = displayStandalone && !forceShow;

      // Session-scoped dismiss memory: tapping Maybe later suppresses
      // opportunistic re-shows for the rest of the browser session.
      const DISMISS_KEY = 'mobius:install-card:dismissed:' + APP_SLUG;
      const wasDismissed = sessionStorage.getItem(DISMISS_KEY) === '1';

      // Suppression detection. Chrome silently swallows BIP for
      // sibling-scope sub-PWAs when the parent Möbius PWA is already
      // installed at this origin. We can't read the install registry,
      // but any of these three signals means "the parent is already
      // installed, so promising a one-tap install would be a lie":
      //   1. display-mode: standalone here  → wrapping window is the
      //      installed Möbius PWA
      //   2. referrer is the Möbius shell   → drawer-Install from
      //      inside the installed parent
      //   3. ?install=1                     → drawer-Install at all
      //      (the drawer only renders inside Möbius)
      // When suppression is likely we pre-reveal the manual-steps
      // panel and swap the title to honest "Add to home screen" copy.
      const ref = document.referrer || '';
      const fromShell = ref.indexOf(location.origin + '/shell/') === 0;
      const suppressionLikely =
        displayStandalone || fromShell || forceShow;

      if (skipAlreadyInstalled) return;

      let shown = false;
      let bipUsed = false;
      let installed = false;

      // When suppression is likely on a Chromium platform, swap to
      // honest "Add to home screen" framing — the user already has
      // Möbius installed, so we tell them this needs a quick manual
      // step instead of teasing a one-tap install that won't fire.
      const cardTitle = document.getElementById('ic-title');
      const cardSub = document.querySelector('.ic-sub');
      const cardHint = document.querySelector('.ic-hint');
      if (suppressionLikely && copy.bipExpected) {{
        if (cardTitle) cardTitle.textContent = 'Add {app_name_html} to your home screen';
        if (cardSub) cardSub.textContent =
          'Möbius is already installed, so this needs one quick step';
        // Suppression means BIP won't fire here, so we pre-reveal the
        // steps panel below (suppressionLikely → preReveal). With the
        // instructions already on screen, the button reveals nothing —
        // make it a dismiss instead of the redundant "Show install
        // steps" tap.
        installBtn.textContent = 'Got it';
        copy.dismissOnAction = true;
      }}

      function showCard(reason) {{
        if (shown) return;
        shown = true;
        backdrop.classList.add('visible');
        card.classList.add('visible');
        // Pre-reveal the fallback panel whenever we KNOW BIP won't
        // give us a one-tap install — either because the platform
        // doesn't support BIP, OR because we've detected suppression
        // signals. Saves the user from a dead tap and surfaces the
        // real path immediately.
        const preReveal = !copy.bipExpected || suppressionLikely;
        if (preReveal) {{
          fallback.classList.add('visible');
        }}
        // Chromium-only safety net: even when suppressionLikely is
        // false (e.g. user opened /apps/<slug>/ in a fresh tab with
        // no Möbius installed), Chrome might STILL not fire BIP fast
        // enough or at all. 3s probe flips the UI to manual steps
        // before the user concludes the button is broken.
        if (copy.bipExpected && !preReveal) {{
          setTimeout(() => {{
            if (!window.__bipDeferred && !installed) {{
              installBtn.textContent = 'Show install steps';
              fallback.classList.add('visible');
              fallback.scrollIntoView({{
                behavior: 'smooth', block: 'nearest',
              }});
            }}
          }}, 3000);
        }}
      }}

      function hideCard(reason) {{
        backdrop.classList.remove('visible');
        card.classList.remove('visible');
      }}

      function showToast(msg, duration) {{
        toast.textContent = msg;
        toast.classList.add('visible');
        setTimeout(
          () => toast.classList.remove('visible'),
          duration || 2500
        );
      }}

      // ----- Icon picker + upload -----
      function downscaleToSquarePNG(file, maxSide) {{
        return new Promise((resolve, reject) => {{
          const fr = new FileReader();
          fr.onerror = () => reject(new Error('Could not read file'));
          fr.onload = () => {{
            const img = new Image();
            img.onerror = () => reject(new Error('Could not decode image'));
            img.onload = () => {{
              const side = Math.min(img.width, img.height);
              const sx = (img.width - side) / 2;
              const sy = (img.height - side) / 2;
              const out = Math.min(maxSide, side);
              const canvas = document.createElement('canvas');
              canvas.width = out; canvas.height = out;
              const ctx = canvas.getContext('2d');
              ctx.drawImage(img, sx, sy, side, side, 0, 0, out, out);
              canvas.toBlob(
                b => b ? resolve(b) : reject(new Error('Canvas encode failed')),
                'image/png'
              );
            }};
            img.src = fr.result;
          }};
          fr.readAsDataURL(file);
        }});
      }}

      iconBtn.addEventListener('click', () => {{
        fileInput.click();
      }});

      fileInput.addEventListener('change', async () => {{
        const file = fileInput.files && fileInput.files[0];
        fileInput.value = '';  // allow re-picking the same file
        if (!file) return;
        const token = localStorage.getItem('token');
        if (!token) {{
          showToast('Sign in first');
          return;
        }}
        try {{
          const blob = await downscaleToSquarePNG(file, 1024);
          const res = await fetch('/api/apps/' + APP_ID + '/icon', {{
            method: 'PUT',
            headers: {{
              'Content-Type': 'image/png',
              'Authorization': 'Bearer ' + token,
            }},
            body: blob,
          }});
          if (!res.ok) {{
            throw new Error('HTTP ' + res.status);
          }}
          // Cache-bust the preview so the new icon shows immediately.
          iconImg.src = '/apps/' + APP_SLUG + '/icon-192.png?t=' + Date.now();
          showToast('Icon updated');
        }} catch (err) {{
          showToast('Could not upload icon');
        }}
      }});

      // ----- Install button -----
      function revealFallback(reason) {{
        fallback.classList.add('visible');
        // Remove + re-add the visible class is a no-op for the
        // pulse animation, so explicitly retrigger by reflowing.
        fallback.style.animation = 'none';
        // eslint-disable-next-line no-unused-expressions
        fallback.offsetHeight;  // force reflow
        fallback.style.animation = '';
        fallback.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
      }}

      installBtn.addEventListener('click', async () => {{
        // iOS-non-Safari path: the button copies the URL so the
        // user can paste into Safari. No BIP, no install dialog
        // possible. Fallback panel was pre-revealed at card-show.
        if (copy.unsupported && platform.iosNonSafari) {{
          try {{
            await navigator.clipboard.writeText(location.href);
            showToast('Link copied — paste in Safari');
          }} catch (err) {{
            showToast('Copy failed — long-press the URL bar');
          }}
          return;
        }}

        // Dismiss-on-action paths: the manual steps are already
        // pre-revealed (every non-BIP path, plus the suppressed-Chromium
        // case), so the only thing left for the button to do is close
        // the card. Treat it like the user acknowledged the steps.
        if (copy.dismissOnAction) {{
          sessionStorage.setItem(DISMISS_KEY, '1');
          hideCard('got_it');
          return;
        }}

        const deferred = window.__bipDeferred;
        if (deferred) {{
          bipUsed = true;
          try {{
            deferred.prompt();
            const result = await deferred.userChoice;
            window.__bipDeferred = null;
            // Don't hide the card on accept — the appinstalled
            // listener swaps to the success state explicitly.
            if (result.outcome !== 'accepted') {{
              // Dismissed in the native dialog: surface the fallback
              // hint so the user can try the menu route if they
              // actually do want to install.
              revealFallback('native_dismissed');
            }}
          }} catch (err) {{
            revealFallback('prompt_threw');
            installBtn.textContent = 'Show install steps';
          }}
        }} else {{
          // No BIP available — reveal the platform-specific
          // instruction panel with pulse + scroll-into-view so the
          // user can't miss it (the brazil-trip trace showed two
          // dead taps before the user found the menu).
          revealFallback('no_bip_on_tap');
          installBtn.textContent = copy.bipExpected
            ? 'Show install steps' : copy.installLabel;
        }}
      }});

      cancelBtn.addEventListener('click', () => {{
        sessionStorage.setItem(DISMISS_KEY, '1');
        hideCard('cancel');
      }});

      doneBtn.addEventListener('click', () => {{
        hideCard('done');
        // After install, return to Möbius rather than leaving the user
        // stranded on the sub-app's not-yet-launched-from-home-screen
        // page. Prefer history.back() when the referrer was the shell
        // (covers drawer-Install); fall back to /shell/ for fresh-tab
        // /apps/<slug>/?install=1 flows where back-stack is empty.
        setTimeout(() => {{
          const cameFromShell = /\\/shell\\//.test(document.referrer);
          if (cameFromShell && history.length > 1) {{
            history.back();
          }} else {{
            location.href = '/shell/';
          }}
        }}, 250);
      }});

      // Backdrop tap dismisses (only when the body is visible — once
      // we're in the success state, require the explicit Got it tap).
      backdrop.addEventListener('click', () => {{
        if (!success.classList.contains('visible')) {{
          sessionStorage.setItem(DISMISS_KEY, '1');
          hideCard('backdrop');
        }}
      }});

      // BIP arriving after the card opened reverts the label to
      // Install, even if the 3s timeout or no-BIP path already flipped
      // it to "Show install steps" — the native dialog will fire now.
      window.addEventListener('mobius:bip-ready', () => {{
        if (shown) {{
          installBtn.textContent = 'Install';
        }}
      }});

      // Success state: app installed (via our button OR Chrome menu).
      window.addEventListener('mobius:installed', () => {{
        if (installed) return;
        installed = true;
        successTitle.textContent = APP_NAME + ' is on your home screen';
        body.style.display = 'none';
        success.classList.add('visible');
        // If the card was hidden when install fired (Chrome menu
        // path with backdrop dismissed), surface it again so the
        // user sees confirmation.
        if (!card.classList.contains('visible')) {{
          backdrop.classList.add('visible');
          card.classList.add('visible');
        }}
      }});

      // Show the card. Slight delay so the React app gets first
      // paint in — the card sliding in over an empty black canvas
      // looks broken.
      if (forceShow || !wasDismissed) {{
        setTimeout(() => showCard(forceShow ? 'force_show' : 'opportunistic'), 350);
      }}
    }})();
  </script>
</body>
</html>"""
  headers = {"Cache-Control": "no-cache, must-revalidate"}
  # Lets the service worker cache this standalone page for offline use
  # (Tier 4a) — only for apps the agent declared offline_capable. The
  # gated appCodeHandler's appCodeStoreAction policy keys on this header.
  if app.offline_capable:
    headers["X-Mobius-Offline"] = "1"
  return HTMLResponse(content=html, headers=headers)
