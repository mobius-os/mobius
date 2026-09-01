"""Top-level routes that make a mini-app installable as its own PWA.

Each installed mini-app gets its own URL scope at `/apps/<slug>/`, with
a unique manifest, icon, and trusted HTML host. The host boots the signed
frontend bundle, which selects StandaloneApp from server-authored JSON and
mounts the mini-app through the same opaque AppCanvas frame used by the main
workspace. App-authored JavaScript therefore never executes at owner origin.

The PWA install picks up the manifest at `/apps/<slug>/manifest.json`.
The `scope` is `/apps/<slug>/`, so it does not overlap with the Möbius
shell scope, which is already narrowed to `/shell/`. That scope
separation lets install prompts for these sub-app URLs fire on
Chromium.

These routes live OUTSIDE the `/api/...` namespace because (a) they
serve user-facing HTML/manifest/image content, not JSON APIs; (b)
PWA scope is computed from the manifest URL's directory, so the
manifest MUST live at `/apps/<slug>/...` to scope correctly.

Auth belongs to the signed frontend bundle, so an unauthenticated owner sees
the ordinary setup/login boundary before StandaloneApp mounts. The response
uses `Cache-Control: no-cache, must-revalidate`; only the service-worker
offline cache is opted into separately for offline-capable apps.
"""

import io
import json
import re
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import icon_cache, models
from app.config import get_settings
from app.database import get_db
from app.frontend_assets import resolve_frontend_dir
from app.theme import get_bg_color, theme_data

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
def standalone_manifest(
  slug: str, request: Request, db: Session = Depends(get_db)
):
  """Per-app web app manifest.

  `id` is the stable install identity (`/apps/<slug>/`). `scope` and
  `start_url` are both `/apps/<slug>/` so the OS treats this as a
  distinct PWA from Möbius. `display` (from the app's mobius.json,
  default "standalone") requests the browser presentation on launch.
  "fullscreen" asks for the most immersive supported mode, but iOS may still
  retain its OS status bar; apps must honor safe-area insets in every mode.
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
  # A one-time install pass supplied by the installing page rides through to
  # `start_url` so the installed app's FIRST launch can redeem the owner
  # session and skip a login it could not otherwise avoid (iOS gives each
  # home-screen web app its own empty storage). This route never mints a pass
  # — it only forwards one the caller already holds — so an anonymous fetch
  # gets the plain start_url and the ordinary login.
  install_pass = request.query_params.get("pass") or ""
  start_url = (
    f"{base}?pass={quote(install_pass, safe='')}" if install_pass else base
  )
  return JSONResponse(
    {
      "id": base,
      "name": app.name,
      "short_name": app.name[:12] if app.name else slug,
      "description": app.description or "",
      "start_url": start_url,
      "scope": base,
      # Per-app display mode (mobius.json `display`); defaults to
      # "standalone". "fullscreen" is a request, not an iOS guarantee that
      # the OS status bar disappears; viewport-fit + safe insets remain the
      # portable edge-to-edge contract.
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
      # Screenshots upgrade Chromium's install prompt from the mini-infobar
      # to the richer install sheet (narrow serves phones, wide serves
      # desktop). Generated covers, not live captures — see
      # `_render_screenshot_png` for why this stays privacy-safe.
      "screenshots": [
        {
          "src": f"{base}{shot_name}?v={v}",
          "sizes": f"{w}x{h}",
          "type": "image/png",
          "form_factor": form,
          "label": app.name or slug,
        }
        for shot_name, (w, h, form) in _SCREENSHOT_SPECS.items()
      ],
    },
    media_type="application/manifest+json",
    # Revalidate on every fetch so a freshly-renamed app never serves a
    # stale name/short_name/icon to the OS at install time. The body is
    # tiny; `no-cache` keeps it cheap (304 when unchanged) without
    # letting the browser pin an old manifest. A manifest carrying an
    # install pass holds a credential, so that variant is never stored.
    headers={
      "Cache-Control": "no-store" if install_pass else "no-cache, must-revalidate",
      **({"Referrer-Policy": "no-referrer"} if install_pass else {}),
    },
  )


# Match `icon-192.png` / `icon-512.png` / `icon-{N}.png`. Anything
# else 404s — we don't want the route accidentally serving arbitrary
# sizes that aren't declared in the manifest.
_ICON_NAME = re.compile(r"^icon-(\d+)\.png$")

# Manifest `screenshots` assets. With at least one narrow and one wide entry,
# Chromium replaces the minimal install mini-infobar with the richer
# app-store-style install sheet — which also makes "Install" visually
# unmistakable next to "Create shortcut". Fixed names only; the cache can't
# be flooded with arbitrary dimensions.
_SCREENSHOT_SPECS = {
  "screenshot-narrow.png": (1080, 1920, "narrow"),
  "screenshot-wide.png": (1920, 1080, "wide"),
}


def _render_screenshot_png(
  icon_png: bytes | None, name: str, slug: str, bg_hex: str,
  width: int, height: int,
) -> bytes:
  """Generated install-sheet cover: the app's icon and name on the app's own
  background color, arranged like a launcher tile.

  Deliberately NOT a live capture of the running app. This route is public —
  the OS fetches manifest assets before any login — so a real screenshot
  could leak app content to an unauthenticated visitor. A generated cover
  carries the same information class as the icon route and is a pure
  function of the same inputs, so it shares the icon cache's keying.
  """
  from PIL import Image, ImageDraw, ImageFont
  r = int(bg_hex[1:3], 16)
  g = int(bg_hex[3:5], 16)
  b = int(bg_hex[5:7], 16)
  img = Image.new("RGB", (width, height), (r, g, b))

  # The icon reuses the standalone render (uploaded art composited onto the
  # background, or the generated letter mark), rounded like a launcher icon.
  icon_size = int(min(width, height) * 0.34)
  icon = Image.open(io.BytesIO(
    _render_standalone_icon(icon_png, name, slug, bg_hex, icon_size)
  )).convert("RGB")
  radius = int(icon_size * 0.22)
  mask = Image.new("L", (icon_size, icon_size), 0)
  ImageDraw.Draw(mask).rounded_rectangle(
    (0, 0, icon_size - 1, icon_size - 1), radius=radius, fill=255,
  )
  icon_x = (width - icon_size) // 2
  icon_y = (height // 2) - int(icon_size * 0.75)
  img.paste(icon, (icon_x, icon_y), mask)

  # Name below, in whichever of light/dark ink reads on this background.
  luminance = 0.299 * r + 0.587 * g + 0.114 * b
  fg = (28, 28, 30) if luminance > 150 else (255, 255, 255)
  draw = ImageDraw.Draw(img)
  font = None
  for path in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
  ):
    try:
      font = ImageFont.truetype(path, int(min(width, height) * 0.052))
      break
    except OSError:
      continue
  if font is None:
    font = ImageFont.load_default()
  label = (name or slug).strip() or slug
  max_w = int(width * 0.86)
  bbox = draw.textbbox((0, 0), label, font=font)
  while len(label) > 1 and bbox[2] - bbox[0] > max_w:
    label = label[:-2].rstrip() + "…"
    bbox = draw.textbbox((0, 0), label, font=font)
  draw.text(
    (
      (width - (bbox[2] - bbox[0])) / 2 - bbox[0],
      icon_y + icon_size + int(icon_size * 0.18) - bbox[1],
    ),
    label, fill=fg, font=font,
  )
  buf = io.BytesIO()
  img.save(buf, format="PNG", optimize=True)
  return buf.getvalue()


def _render_standalone_icon(
  icon_png: bytes | None, name: str, slug: str, bg_hex: str, size: int
) -> bytes:
  """The CPU-bound render for one standalone-icon variant: resize +
  background-composite the uploaded PNG, or draw the generated letter icon.
  Pure function of its arguments (all folded — via `updated_at` — into the
  cache key), so memoizing its output is safe.

  Takes plain primitives, not the live ORM row, so it can run on a worker
  thread without that thread touching the request's DB session (the caller
  snapshots the effective icon / app name / background color first)."""
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

  Two paths: the app's effective stored icon is resized + background-composited
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
  shot_spec = _SCREENSHOT_SPECS.get(icon_name)
  m = None if shot_spec else _ICON_NAME.match(icon_name)
  if not shot_spec and not m:
    raise HTTPException(status_code=404, detail="Not found.")
  size = 0
  if m:
    size = int(m.group(1))
    if size < 16 or size > 1024:
      raise HTTPException(status_code=400, detail="Invalid icon size.")
  app = _get_app_by_slug(db, slug)
  ts_us = int(app.updated_at.timestamp() * 1e6) if app.updated_at else 0
  etag = f'W/"{ts_us}-{icon_name if shot_spec else size}"'
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
  icon_png = app.effective_icon_png
  name = app.name
  app_slug = app.slug
  bg_inputs = (app.background_color, app.theme_color)

  if shot_spec:
    shot_w, shot_h, _form = shot_spec

    def _compute_shot() -> bytes:
      bg_hex = _resolve_bg_hex(bg_inputs[0], bg_inputs[1], icon_png)
      return _render_screenshot_png(
        icon_png, name, app_slug, bg_hex, shot_w, shot_h,
      )

    body = await icon_cache.get_or_compute(
      app_id=app_id,
      updated_us=ts_us,
      kind="standalone-shot",
      size=shot_w,
      compute=_compute_shot,
    )
    return Response(content=body, media_type="image/png", headers=headers)

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


def _frontend_index_path():
  """Resolve the same complete request-time build the main shell serves."""
  directory = resolve_frontend_dir(get_settings().data_dir)
  if (directory / "index.html").is_file():
    return directory / "index.html"
  raise HTTPException(
    status_code=503,
    detail="Frontend rebuilding, retry.",
    headers={"Retry-After": "1"},
  )


def _script_json(value) -> str:
  """Serialize data into a non-executable JSON script slot safely."""
  return (
    json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    .replace("</", "<\\/")
    .replace("\u2028", "\\u2028")
    .replace("\u2029", "\\u2029")
  )


def _replace_html_text(html: str, old: str, new: str) -> str:
  """Fail closed when the signed frontend template loses an expected seam."""
  if old not in html:
    raise HTTPException(
      status_code=503,
      detail="Frontend and standalone host are out of sync.",
      headers={"Retry-After": "1"},
    )
  return html.replace(old, new, 1)


def _standalone_boot_payload(app: models.App) -> dict:
  """Return the complete non-secret input consumed by StandaloneApp.

  The owner JWT deliberately is not part of this shape. The trusted frontend
  host mints an app-scoped token through AppCanvas and only that scoped token
  crosses into the opaque app frame.
  """
  return {
    "id": app.id,
    "slug": app.slug,
    "name": app.name,
    "description": app.description or "",
    "chat_id": app.chat_id,
    "updated_at": app.updated_at.isoformat() if app.updated_at else "0",
    "offline_capable": bool(app.offline_capable),
    "capability_contract": app.capability_contract or {},
    "theme_color": app.theme_color,
    "background_color": app.background_color,
    "display": app.display or "standalone",
  }


def _standalone_index_html(app: models.App, install_pass: str = "") -> str:
  """Compose app identity into the ordinary signed frontend entry document.

  Only platform code executes at the top-level owner origin. The mini-app is
  selected by a JSON boot slot and mounted later by the shared AppCanvas inside
  the response-CSP sandboxed /api/apps/{id}/frame document.

  `install_pass` is forwarded onto the manifest URL so the OS reads a manifest
  whose `start_url` carries it, and the installed app's first launch can redeem
  the opaque server-stored grant. Rendering it server-side
  rather than patching the link from JS means the manifest the OS fetches at
  Add-to-Home time is the right one on the first read.
  """
  try:
    html = _frontend_index_path().read_text(encoding="utf-8")
  except FileNotFoundError:
    raise HTTPException(
      status_code=503,
      detail="Frontend rebuilding, retry.",
      headers={"Retry-After": "1"},
    )

  slug = quote(app.slug, safe="")
  name = escape(app.name or app.slug, quote=True)
  description = escape(app.description or "", quote=True)
  version = int(app.updated_at.timestamp() * 1_000_000) if app.updated_at else 0
  app_bg = _app_background_color(app)

  html = _replace_html_text(html, "<title>Möbius</title>", f"<title>{name}</title>")
  html = _replace_html_text(
    html,
    '<meta name="description" content="AI-powered personal app platform." />',
    f'<meta name="description" content="{description}" />',
  )
  html = _replace_html_text(
    html,
    '<meta name="apple-mobile-web-app-title" content="Möbius" />',
    f'<meta name="apple-mobile-web-app-title" content="{name}" />',
  )
  html = _replace_html_text(
    html,
    '<link rel="manifest" href="/manifest.webmanifest" />',
    f'<link rel="manifest" href="/apps/{slug}/manifest.json'
    f'{"?pass=" + quote(install_pass, safe="") if install_pass else ""}" />',
  )
  html = _replace_html_text(
    html,
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png" />',
    f'<link rel="apple-touch-icon" href="/apps/{slug}/icon-192.png?v={version}" />',
  )
  # Match the semantic icon element so artwork changes remain frontend-owned.
  html, icon_replacements = re.subn(
    r'<link\s+rel="icon"[^>]*>',
    f'<link rel="icon" type="image/png" href="/apps/{slug}/icon-192.png?v={version}" />',
    html,
    count=1,
  )
  if icon_replacements != 1:
    raise HTTPException(
      status_code=503,
      detail="Frontend and standalone host are out of sync.",
      headers={"Retry-After": "1"},
    )
  html = _replace_html_text(
    html,
    '<meta name="theme-color" content="#0d0d0d" />',
    f'<meta name="theme-color" content="{app_bg}" />',
  )

  theme_payload = _script_json(theme_data(get_settings().data_dir))
  html = _replace_html_text(
    html,
    '<script type="application/json" id="__mobius-theme__"></script>',
    '<script type="application/json" id="__mobius-theme__">'
    f'{theme_payload}</script>',
  )
  boot_slot = (
    '<script type="application/json" id="__mobius-standalone-app__">'
    f'{_script_json(_standalone_boot_payload(app))}</script>'
  )
  html = _replace_html_text(html, "</head>", f"  {boot_slot}\n  </head>")

  # The ordinary shell launch cover is intentionally artwork-free. Standalone
  # installs still need their app-specific pre-JS loading mark, so the route
  # owns that variant and inserts it into the stable splash slot.
  html, splash_replacements = re.subn(
    r'(<div id="splash"[^>]*>)(\s*</div>)',
    rf'\g<1><img src="/apps/{slug}/icon-192.png?v={version}" '
    r'width="44" height="44" alt="" '
    r'style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);'
    r'opacity:.4;will-change:opacity" />\g<2>',
    html,
    count=1,
  )
  if splash_replacements != 1:
    raise HTTPException(
      status_code=503,
      detail="Frontend and standalone host are out of sync.",
      headers={"Retry-After": "1"},
    )
  return html


@router.get("/apps/{slug}/", response_class=HTMLResponse)
@router.get("/apps/{slug}", response_class=HTMLResponse)
def standalone_shell(
  slug: str, request: Request, db: Session = Depends(get_db)
):
  """Serve one installable app as a minimal trusted host around AppCanvas.

  The route remains public so browsers can discover its manifest and complete
  PWA installation before login. Authentication is owned by the signed shell
  bundle. Crucially, app-authored JavaScript never executes in this top-level,
  owner-origin document: StandaloneApp mounts the ordinary opaque frame and
  AppCanvas supplies only an app-scoped token through its verified handshake.
  """
  app = _get_app_by_slug(db, slug)
  # Forwarded, never minted here: this route is public, so it can only echo a
  # pass the caller already had. A document carrying one is never stored.
  install_pass = request.query_params.get("pass") or ""
  headers = {
    "Cache-Control": "no-store" if install_pass else "no-cache, must-revalidate"
  }
  if install_pass:
    headers["Referrer-Policy"] = "no-referrer"
  # An older controlling service worker decides whether to store this response
  # from this header and ignores Cache-Control. Never opt a pass-bearing body
  # into that cache: its manifest link echoes the secret.
  if app.offline_capable and not install_pass:
    headers["X-Mobius-Offline"] = "1"
  return HTMLResponse(
    content=_standalone_index_html(app, install_pass=install_pass),
    headers=headers,
  )
