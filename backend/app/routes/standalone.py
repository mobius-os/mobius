"""Top-level routes that make a mini-app installable as its own PWA.

Each installed mini-app gets its own URL scope at `/apps/<slug>/`, with
a unique manifest, an icon, and an HTML shell that boots the app's
React component directly (no parent postMessage handshake — same
origin means the JWT in localStorage works as-is).

The PWA install picks up the manifest at `/apps/<slug>/manifest.json`.
The `scope` is `/apps/<slug>/`, so it does not overlap with the Möbius
shell scope (currently `/`, planned `/shell/`). Once Möbius's own
manifest scope is narrowed, install prompts for these sub-app URLs
will fire on Chromium.

These routes live OUTSIDE the `/api/...` namespace because (a) they
serve user-facing HTML/manifest/image content, not JSON APIs; (b)
PWA scope is computed from the manifest URL's directory, so the
manifest MUST live at `/apps/<slug>/...` to scope correctly.

Auth: unauthenticated visitors are redirected to Möbius's login page
with a `return` param so they land back at the standalone URL after
logging in. The standalone shell itself is publicly cacheable —
secrets live in the JWT, which the user's browser supplies once
logged in.
"""

import io
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.orm import Session

from app import models
from app.database import get_db

router = APIRouter(tags=["standalone"])


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
  """Resolve `<slug>` to an App row. Also handles the lazy-backfill
  case where an old app has a NULL slug — we don't try to match
  against null, so legacy apps surface here via their lazily-assigned
  slug from the first time someone accessed them via the API."""
  app = db.query(models.App).filter(models.App.slug == slug).first()
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  return app


@router.get("/apps/{slug}/manifest.json")
def standalone_manifest(slug: str, db: Session = Depends(get_db)):
  """Per-app web app manifest.

  `id` is the stable install identity (`/apps/<slug>/`). `scope` and
  `start_url` are both `/apps/<slug>/` so the OS treats this as a
  distinct PWA from Möbius. `display: standalone` removes browser
  chrome on launch.
  """
  app = _get_app_by_slug(db, slug)
  base = f"/apps/{slug}/"
  # Version the icon URLs by `updated_at` so when the owner uploads
  # a fresh icon the browser refetches at install time instead of
  # baking the stale image into the home-screen entry.
  v = int(app.updated_at.timestamp()) if app.updated_at else 0
  return JSONResponse(
    {
      "id": base,
      "name": app.name,
      "short_name": app.name[:12] if app.name else slug,
      "description": app.description or "",
      "start_url": base,
      "scope": base,
      "display": "standalone",
      "background_color": "#0c0f14",
      "theme_color": "#0c0f14",
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
  )


# Match `icon-192.png` / `icon-512.png` / `icon-{N}.png`. Anything
# else 404s — we don't want the route accidentally serving arbitrary
# sizes that aren't declared in the manifest.
_ICON_NAME = re.compile(r"^icon-(\d+)\.png$")


@router.get("/apps/{slug}/{icon_name}")
def standalone_icon(
  slug: str, icon_name: str, db: Session = Depends(get_db),
):
  """Serves the per-app icon at the requested size.

  Two paths: user-uploaded `app.icon_png` is resized on the fly via
  Pillow; missing upload falls back to the auto-generated letter
  icon. Cached for 5 minutes so the home-screen install request and
  the splash screen request don't both regenerate.
  """
  m = _ICON_NAME.match(icon_name)
  if not m:
    raise HTTPException(status_code=404, detail="Not found.")
  size = int(m.group(1))
  if size < 16 or size > 1024:
    raise HTTPException(status_code=400, detail="Invalid icon size.")
  app = _get_app_by_slug(db, slug)
  if app.icon_png:
    from PIL import Image
    img = Image.open(io.BytesIO(app.icon_png))
    # Preserve mode — the upload path already normalized to RGB or
    # RGBA (and cropped to square), so don't strip alpha on serve.
    # A force-`convert("RGB")` here flattened transparent uploads
    # onto a black rectangle when the OS rendered them on a non-
    # dark home screen.
    if img.mode not in ("RGB", "RGBA"):
      img = img.convert("RGBA" if "A" in img.mode else "RGB")
    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    body = buf.getvalue()
  else:
    body = _generate_icon_png(app.name, app.slug or slug, size=size)
  return Response(
    content=body,
    media_type="image/png",
    headers={"Cache-Control": "public, max-age=300"},
  )


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
  # Escape user-controlled strings before interpolating into HTML.
  # The agent generates app names so they're nominally trusted, but
  # belt-and-suspenders: a stray `<script>` in a name would otherwise
  # execute in the standalone scope with the user's JWT.
  from html import escape
  app_name_html = escape(app_name)
  # JSON-encode for safe inline-script embedding: json.dumps handles
  # quotes/backslashes/control chars correctly, then neutralize the
  # three sequences JSON-encoding doesn't cover for in-HTML use —
  # `</` (script-tag breakout), U+2028, U+2029 (treated as line
  # terminators inside JS strings and would otherwise corrupt the
  # source). Emit without surrounding quotes since json.dumps
  # already wraps in double-quotes.
  # json.dumps already escapes U+2028 and U+2029 (Python's json
  # module is non-strict by default). All we need extra is to
  # neutralize `</` for in-HTML embedding.
  app_name_js_literal = json.dumps(app_name).replace("</", "<\\/")
  html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <meta name="referrer" content="no-referrer">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="{app_name_html}">
  <title>{app_name_html}</title>
  <link rel="manifest" href="/apps/{slug}/manifest.json">
  <link rel="icon" type="image/png" sizes="192x192" href="/apps/{slug}/icon-192.png">
  <link rel="apple-touch-icon" href="/apps/{slug}/icon-192.png">
  <script type="importmap">
  {{
    "imports": {{
      "react": "https://esm.sh/react@18.3.1",
      "react/jsx-runtime": "https://esm.sh/react@18.3.1/jsx-runtime",
      "react-dom": "https://esm.sh/react-dom@18.3.1",
      "react-dom/client": "https://esm.sh/react-dom@18.3.1/client",
      "recharts": "https://esm.sh/recharts@2.15.4?exports=LineChart,BarChart,PieChart,AreaChart,Line,Bar,Pie,Area,XAxis,YAxis,ZAxis,Tooltip,CartesianGrid,Legend,ResponsiveContainer,Cell,LabelList,Brush,ComposedChart,ScatterChart,Scatter,RadarChart,Radar,PolarGrid,PolarAngleAxis,PolarRadiusAxis,RadialBarChart,RadialBar&external=react,react-dom",
      "date-fns": "https://esm.sh/date-fns@4.3.0",
      "three": "/vendor/three@0.184.0/three.module.js",
      "three/addons/": "/vendor/three@0.184.0/addons/"
    }}
  }}
  </script>
  <style>
    :root {{
      --bg: #0c0f14; --surface: #14181f; --surface2: #1a1f28;
      --border: #252b36; --text: #d4d4d8; --muted: #52525b;
      --accent: #a78bfa; --accent-hover: #c4b5fd;
      --danger: #f87171;
      --font: 'Inter', system-ui, sans-serif;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
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
    /* Install confirm card. Bottom-sheet style overlay shown when the
       user explicitly asked to install (drawer ⋮ → Install lands here
       with `?install=1`). Has two action tiers — silent native prompt
       when BIP is available, Chrome-menu guidance when it isn't —
       because beforeinstallprompt is unreliable for sub-PWAs that
       share an origin with an already-installed parent. */
    #install-backdrop {{
      position: fixed; inset: 0; background: rgba(0,0,0,0.55);
      backdrop-filter: blur(4px);
      z-index: 9998; opacity: 0; pointer-events: none;
      transition: opacity 0.18s ease;
    }}
    #install-backdrop.visible {{ opacity: 1; pointer-events: auto; }}
    #install-card {{
      position: fixed; left: 0; right: 0; bottom: 0;
      background: var(--surface, #14181f); color: var(--text, #d4d4d8);
      border-top: 1px solid var(--border, #252b36);
      border-radius: 18px 18px 0 0;
      padding: 22px 22px max(22px, env(safe-area-inset-bottom)) 22px;
      box-shadow: 0 -12px 32px rgba(0,0,0,0.5);
      font-family: var(--font);
      z-index: 9999;
      transform: translateY(100%);
      transition: transform 0.22s cubic-bezier(.2,.7,.2,1);
    }}
    #install-card.visible {{ transform: translateY(0); }}
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
    </style>
</head>
<body>
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
    //
    // Every event is also beaconed to /api/install-log so we can see
    // server-side what actually fired in real user sessions. Temporary
    // diagnostic — remove once the install UX is stable.
    (function() {{
      const _APP_SLUG = {json.dumps(slug)};
      function beacon(event, ctx) {{
        try {{
          const body = JSON.stringify(Object.assign({{
            surface: 'standalone',
            slug: _APP_SLUG,
            event: event,
            url: location.href,
            display_mode: (
              window.matchMedia('(display-mode: standalone)').matches
                ? 'standalone'
                : (window.matchMedia('(display-mode: minimal-ui)').matches
                    ? 'minimal-ui'
                    : 'browser')
            ),
            referrer: document.referrer || null,
          }}, ctx || {{}}));
          // keepalive so the beacon survives a navigation away (the
          // install dialog can trigger a navigation in some flows).
          fetch('/api/install-log', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: body,
            keepalive: true,
          }}).catch(() => {{}});
        }} catch (_) {{}}
      }}
      window.__mobiusBeacon = beacon;

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

      beacon('page_load', {{
        has_install_param: new URLSearchParams(location.search).has('install'),
        // `getInstalledRelatedApps` is the closest thing Chrome gives
        // us to "is the parent Möbius PWA installed for this origin?".
        // Surface its result so we can tell whether suppression is the
        // expected behavior or something more exotic.
        related_apps_available:
          typeof navigator.getInstalledRelatedApps === 'function',
        platform: window.__mobiusPlatform,
      }});
      if (typeof navigator.getInstalledRelatedApps === 'function') {{
        navigator.getInstalledRelatedApps().then(
          apps => beacon('related_apps', {{
            apps: apps.map(a => ({{
              id: a.id, platform: a.platform, url: a.url,
            }})),
          }}),
          err => beacon('related_apps_error', {{
            error: String(err && err.message || err),
          }}),
        );
      }}

      window.addEventListener('beforeinstallprompt', function(e) {{
        e.preventDefault();
        window.__bipDeferred = e;
        beacon('bip_fired', {{ platforms: e.platforms || null }});
        window.dispatchEvent(new CustomEvent('mobius:bip-ready'));
      }});

      window.addEventListener('appinstalled', function() {{
        beacon('app_installed');
        window.__bipDeferred = null;
        window.dispatchEvent(new CustomEvent('mobius:installed'));
      }});

      // Display-mode transitions are the cleanest signal that an
      // install actually took effect on this origin, separate from
      // the `appinstalled` event (which can be missed if the page
      // navigated mid-install).
      try {{
        window.matchMedia('(display-mode: standalone)')
          .addEventListener('change', function(e) {{
            beacon('display_mode_change', {{
              matches: e.matches, mode: 'standalone',
            }});
          }});
      }} catch (_) {{}}
    }})();
  </script>
  <div id="root"></div>
  <div id="loading"><div class="spinner"></div><div>Loading {app_name_html}…</div></div>
  <div id="install-backdrop" aria-hidden="true"></div>
  <div id="install-card" role="dialog" aria-labelledby="ic-title" aria-modal="true">
    <div id="ic-body">
      <div class="ic-row">
        <button id="ic-icon-btn" class="ic-icon-wrap" type="button" aria-label="Change icon">
          <img id="ic-icon-img" class="ic-icon" alt="" src="/apps/{slug}/icon-192.png">
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
  <script type="module">
    const APP_ID = {app_id};
    const APP_SLUG = {json.dumps(slug)};
    const APP_NAME = {app_name_js_literal};

    // Auth: read the owner JWT from localStorage (same origin so it's
    // visible). If missing, redirect to login with a return URL.
    const token = localStorage.getItem('token');
    if (!token) {{
      const ret = encodeURIComponent(window.location.pathname);
      window.location.href = '/?return=' + ret;
    }} else {{
      // Module load can fail transiently during PWA install transitions,
      // SW state swaps, and minibrowser-overlay contexts — wrap so we
      // can silently auto-retry once, then surface a Retry button for
      // the user if the second attempt also fails.
      async function loadAndRender(cacheBust) {{
        const [themeRes, tokenRes] = await Promise.all([
          fetch('/api/theme', {{ headers: {{ Authorization: 'Bearer ' + token }} }}),
          fetch('/api/auth/app-token', {{
            method: 'POST',
            headers: {{
              'Content-Type': 'application/json',
              Authorization: 'Bearer ' + token,
            }},
            body: JSON.stringify({{ app_id: APP_ID }}),
          }}),
        ]);
        if (themeRes.ok) {{
          const theme = await themeRes.json();
          if (theme.css) {{
            const style = document.createElement('style');
            style.textContent = theme.css;
            document.head.appendChild(style);
          }}
          if (theme.bg) document.documentElement.style.setProperty('--bg', theme.bg);
        }}
        const appToken = tokenRes.ok ? (await tokenRes.json()).token : token;
        const bust = cacheBust ? '&_=' + Date.now() : '';
        const module = await import(
          '/api/apps/' + APP_ID + '/module?token=' +
          encodeURIComponent(appToken) + bust
        );
        const Component = module.default;
        if (!Component) throw new Error('App module has no default export');
        const React = await import('react');
        const {{ createRoot }} = await import('react-dom/client');
        const root = createRoot(document.getElementById('root'));
        root.render(React.createElement(Component, {{ appId: APP_ID, token: appToken }}));
        document.getElementById('loading').classList.add('hidden');
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

    // Install confirm card: bottom-sheet overlay with the icon,
    // app name, brief value-prop, and an Install button that
    // calls `BeforeInstallPromptEvent.prompt()` directly. That's
    // the only programmatic path to Chromium's native install
    // dialog — and only works when (a) we have the deferred event,
    // (b) the call happens inside a real user gesture on the page
    // whose manifest is being installed.
    //
    // The `beforeinstallprompt` listener is attached in a separate
    // <script> tag at the top of <body> (not here) because Chromium
    // fires the event very shortly after DOMContentLoaded — if our
    // module script attaches the listener after its async loads, the
    // event has already fired and been lost. The early listener
    // stashes the event on `window.__bipDeferred` and dispatches a
    // `mobius:bip-ready` event we listen for here.
    //
    // Visibility rules:
    //   - `?install=1` in URL → ALWAYS show (drawer-initiated intent)
    //     even if the page is somehow in display-mode: standalone
    //     (e.g. the user navigated here from inside the parent
    //     Möbius PWA window — Chromium reports the surrounding PWA's
    //     display mode, not the not-yet-installed sub-app's).
    //   - Without `?install=1`: skip when this app's PWA is already
    //     running standalone (nothing to install), OR when the user
    //     previously dismissed it this session.
    (function setupInstallCard() {{
      const beacon = window.__mobiusBeacon || function() {{}};
      const platform = window.__mobiusPlatform || {{}};

      // Element handles, all required (the markup is part of this
      // template, so missing elements would mean a template edit
      // broke the contract — log and bail).
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
      if (!backdrop || !card || !installBtn) {{
        beacon('card_dom_missing');
        return;
      }}

      // ----- Platform-specific copy + behavior -----
      // The fallback hint, install button label, and whether to show
      // the card at all all branch on what install path the user's
      // browser actually exposes. Centralized here so the controller
      // below stays agnostic.
      function copyForPlatform() {{
        if (platform.iosSafari) {{
          return {{
            installLabel: 'Show install steps',
            // No BIP on iOS Safari — install is always the manual
            // Share menu path. Tier-1 = reveal the steps panel.
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
            installLabel: 'Show install steps',
            bipExpected: false,
            fallbackHTML:
              '<span class="ic-fallback-arrow" aria-hidden="true">↑</span>' +
              'Tap the <strong>⋮</strong> menu at the top right, then choose ' +
              '<strong>Install</strong>.',
          }};
        }}
        if (platform.firefox && platform.desktop) {{
          return {{
            installLabel: 'Show install steps',
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
        // Fallback for unknown browsers — generic instruction.
        return {{
          installLabel: 'Show install steps',
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
      beacon('platform_copy_selected', {{
        label: copy.installLabel,
        bip_expected: copy.bipExpected,
        unsupported: !!copy.unsupported,
      }});

      // `?install=1` is the drawer's intent signal. We honor it as
      // the authoritative show-the-card override — display-mode
      // detection cannot tell "sub-app installed" apart from "we're
      // inside the parent PWA window" (the surrounding window's
      // display mode is what `matchMedia('(display-mode: standalone)'
      // returns, not the sub-app's). The drawer wouldn't have sent
      // us here if the sub-app was already installed.
      const url = new URL(window.location.href);
      const forceShow = url.searchParams.has('install');
      // Strip the param so a refresh doesn't keep retriggering. Done
      // BEFORE the controller acts so reload is idempotent.
      if (forceShow && window.history && window.history.replaceState) {{
        try {{
          url.searchParams.delete('install');
          window.history.replaceState(
            null, '', url.pathname + url.search + url.hash
          );
        }} catch (_) {{}}
      }}

      // If we're DEFINITELY in this sub-app's own installed standalone
      // (display-mode standalone AND no `?install=1` override), the
      // user is launching from their home screen — there's nothing
      // to install. Skip silently.
      const displayStandalone = window.matchMedia(
        '(display-mode: standalone)'
      ).matches;
      const skipAlreadyInstalled = displayStandalone && !forceShow;

      // Session-scoped dismiss memory: if the user tapped Maybe
      // later this browser session, don't re-show on subsequent
      // opportunistic BIPs in the same session. Cleared on session
      // end (window close).
      const DISMISS_KEY = 'mobius:install-card:dismissed:' + APP_SLUG;
      const wasDismissed = sessionStorage.getItem(DISMISS_KEY) === '1';

      // Strong-signal suppression detection. We KNOW Chrome will
      // suppress BIP for sibling-scope sub-PWAs when the parent is
      // installed at the same origin. We can't read the install
      // registry directly, but three signals — any one of them
      // sufficient on its own — tell us the user is in an
      // already-Möbius-installed session:
      //
      //   1. `display-mode: standalone` on this page  → surrounding
      //      window is the installed Möbius PWA
      //   2. Referrer is the Möbius shell             → drawer-Install
      //      from inside installed Möbius
      //   3. `?install=1`                             → drawer-Install
      //      from anywhere (drawer doesn't render outside Möbius)
      //
      // When suppression is likely, we DON'T wait for BIP and DON'T
      // promise a one-tap install. We surface the manual path
      // upfront with honest copy. This is the difference between
      // "card with disabled-feeling Install button" and "card that
      // does what it says immediately."
      const ref = document.referrer || '';
      const fromShell = ref.indexOf(location.origin + '/shell/') === 0;
      const suppressionLikely =
        displayStandalone || fromShell || forceShow;

      beacon('card_decision', {{
        force_show: forceShow,
        display_standalone: displayStandalone,
        from_shell: fromShell,
        suppression_likely: suppressionLikely,
        skip_already_installed: skipAlreadyInstalled,
        was_dismissed: wasDismissed,
        bip_already_captured: !!window.__bipDeferred,
        unsupported_platform: !!copy.unsupported,
      }});

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
        installBtn.textContent = 'Show install steps';
        beacon('suppression_aware_copy_applied');
      }}

      function showCard(reason) {{
        if (shown) return;
        shown = true;
        beacon('card_shown', {{
          reason: reason,
          has_bip: !!window.__bipDeferred,
          platform_label: installBtn.textContent,
          suppression_likely: suppressionLikely,
        }});
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
          beacon('fallback_pre_revealed', {{
            reason: !copy.bipExpected
              ? 'bip_not_expected_on_platform'
              : 'suppression_likely',
          }});
        }}
        // Chromium-only safety net: even when suppressionLikely is
        // false (e.g. user opened /apps/<slug>/ in a fresh tab with
        // no Möbius installed), Chrome might STILL not fire BIP fast
        // enough or at all. 3s probe flips the UI to manual steps
        // before the user concludes the button is broken.
        if (copy.bipExpected && !preReveal) {{
          setTimeout(() => {{
            if (!window.__bipDeferred && !installed) {{
              beacon('bip_still_missing_after_3s');
              installBtn.textContent = 'Show install steps';
              fallback.classList.add('visible');
              fallback.scrollIntoView({{
                behavior: 'smooth', block: 'nearest',
              }});
              beacon('fallback_pre_revealed', {{
                reason: 'bip_timeout',
              }});
            }}
          }}, 3000);
        }}
      }}

      function hideCard(reason) {{
        beacon('card_hidden', {{ reason: reason }});
        backdrop.classList.remove('visible');
        card.classList.remove('visible');
      }}

      function updateInstallBtnState() {{
        // Button is always tappable now. Label switches based on
        // whether we have BIP — Install (fast path) or Show steps
        // (fallback). This is intentionally permissive: the user
        // expressed install intent, the button must always do
        // SOMETHING in response to the tap.
        installBtn.disabled = false;
        if (!window.__bipDeferred && shown && card.classList.contains('visible')) {{
          // After-3s timer may have already run; the label refresh
          // is a no-op if it already matches.
          if (installBtn.textContent === 'Install') {{
            // Wait briefly for BIP to arrive after card show; if it
            // does, the label stays Install. Otherwise the 3s
            // timer above flips it.
          }}
        }}
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
        beacon('icon_picker_opened');
        fileInput.click();
      }});

      fileInput.addEventListener('change', async () => {{
        const file = fileInput.files && fileInput.files[0];
        fileInput.value = '';  // allow re-picking the same file
        if (!file) return;
        beacon('icon_upload_start', {{
          size: file.size,
          type: file.type,
        }});
        const token = localStorage.getItem('token');
        if (!token) {{
          showToast('Sign in first');
          beacon('icon_upload_no_token');
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
          beacon('icon_upload_success', {{
            uploaded_size: blob.size,
          }});
          showToast('Icon updated');
        }} catch (err) {{
          beacon('icon_upload_error', {{
            error: String(err && err.message || err),
          }});
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
        beacon('fallback_revealed', {{ reason: reason }});
      }}

      installBtn.addEventListener('click', async () => {{
        // iOS-non-Safari path: the button copies the URL so the
        // user can paste into Safari. No BIP, no install dialog
        // possible. Fallback panel was pre-revealed at card-show.
        if (copy.unsupported && platform.iosNonSafari) {{
          beacon('install_btn_tap_copy_url');
          try {{
            await navigator.clipboard.writeText(location.href);
            showToast('Link copied — paste in Safari');
          }} catch (err) {{
            beacon('clipboard_error', {{
              error: String(err && err.message || err),
            }});
            showToast('Copy failed — long-press the URL bar');
          }}
          return;
        }}

        const deferred = window.__bipDeferred;
        if (deferred) {{
          beacon('install_btn_tap_with_bip');
          bipUsed = true;
          try {{
            deferred.prompt();
            const result = await deferred.userChoice;
            beacon('prompt_result', {{ outcome: result.outcome }});
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
            beacon('prompt_error', {{
              error: String(err && err.message || err),
            }});
            revealFallback('prompt_threw');
            installBtn.textContent = 'Show install steps';
          }}
        }} else {{
          // No BIP available — reveal the platform-specific
          // instruction panel with pulse + scroll-into-view so the
          // user can't miss it (the brazil-trip trace showed two
          // dead taps before the user found the menu).
          beacon('install_btn_tap_without_bip');
          revealFallback('no_bip_on_tap');
          installBtn.textContent = copy.bipExpected
            ? 'Show install steps' : copy.installLabel;
        }}
      }});

      cancelBtn.addEventListener('click', () => {{
        beacon('card_dismissed');
        sessionStorage.setItem(DISMISS_KEY, '1');
        hideCard('cancel');
      }});

      doneBtn.addEventListener('click', () => {{
        beacon('card_done');
        hideCard('done');
      }});

      // Backdrop tap dismisses (only when the body is visible — once
      // we're in the success state, require the explicit Got it tap).
      backdrop.addEventListener('click', () => {{
        if (!success.classList.contains('visible')) {{
          beacon('card_dismissed', {{ via: 'backdrop' }});
          sessionStorage.setItem(DISMISS_KEY, '1');
          hideCard('backdrop');
        }}
      }});

      // BIP that arrives AFTER the card is open is good news — the
      // button is already labeled Install; just re-enable in case
      // the 3s fallback flipped it.
      window.addEventListener('mobius:bip-ready', () => {{
        if (shown) {{
          beacon('bip_arrived_while_card_open');
          // BIP showed up; whatever the card was advertising
          // (Show install steps / Install), make sure the button
          // now says Install so the user knows the native dialog
          // will fire on tap.
          installBtn.textContent = 'Install';
        }}
      }});

      // Success state: app installed (via our button OR Chrome menu).
      window.addEventListener('mobius:installed', () => {{
        if (installed) return;
        installed = true;
        beacon('card_success_state_shown', {{ used_bip: bipUsed }});
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
      if (forceShow || wasDismissed === false) {{
        setTimeout(() => showCard(forceShow ? 'force_show' : 'opportunistic'), 350);
      }} else {{
        beacon('card_skipped_dismissed_this_session');
      }}
    }})();
  </script>
</body>
</html>"""
  return HTMLResponse(
    content=html,
    headers={"Cache-Control": "no-cache, must-revalidate"},
  )
