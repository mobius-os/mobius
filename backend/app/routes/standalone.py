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
    /* Install pill: small floating affordance in the bottom-right,
       shown only when beforeinstallprompt actually fires (rare when
       the user already has Möbius installed at this origin — see
       web.dev "Build multiple PWAs on the same domain" for the
       Chromium suppression that gates BIP in that case). When BIP
       doesn't fire, nothing overlays the app — the user can still
       install via Chrome's own ⋮ → "Add to Home screen" menu, which
       bypasses BIP entirely. */
    #install-pill {{
      position: fixed; bottom: 18px; right: 18px;
      background: var(--accent, #a78bfa); color: #0c0f14;
      border: none; border-radius: 999px;
      padding: 10px 18px;
      font-size: 13px; font-weight: 600;
      font-family: var(--font);
      cursor: pointer;
      box-shadow: 0 6px 20px rgba(0,0,0,0.35);
      display: none;
      z-index: 9999;
    }}
    #install-pill.visible {{ display: inline-flex; align-items: center; gap: 6px; }}
    #install-toast {{
      position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
      background: var(--surface2, #1a1f28); color: var(--text, #d4d4d8);
      padding: 10px 18px; border-radius: 10px;
      font-size: 13px; font-family: var(--font);
      box-shadow: 0 4px 14px rgba(0,0,0,0.4);
      z-index: 10000; opacity: 0;
      transition: opacity 0.2s;
      pointer-events: none;
    }}
    #install-toast.visible {{ opacity: 1; }}
    </style>
</head>
<body>
  <div id="root"></div>
  <div id="loading"><div class="spinner"></div><div>Loading {app_name_html}…</div></div>
  <button id="install-pill" type="button" aria-label="Install to home screen">
    <span aria-hidden="true">+</span>
    <span>Install</span>
  </button>
  <div id="install-toast" role="status" aria-live="polite"></div>
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
    (function setupInstall() {{
      // Two opportunistic UI hooks, no instructions, no overlays.
      //
      // 1. `beforeinstallprompt` — when Chromium fires it (desktop
      //    Chrome, first-time visitors who haven't installed
      //    Möbius), reveal a small floating Install pill bottom-
      //    right. The pill is the entire install UI. When BIP
      //    doesn't fire (the common case after the user has
      //    Möbius installed — Chromium's installed-app registry
      //    suppresses BIP for sibling-scope sub-PWAs), the pill
      //    stays hidden and the user can install via Chrome's
      //    own ⋮ menu (which bypasses BIP).
      //
      // 2. `appinstalled` — fires regardless of which path the
      //    user took (our pill OR Chrome's menu). Shows a brief
      //    Möbius-themed toast confirming the install.
      //
      // Strip `?install=1` from the URL on load so a refresh
      // doesn't look weird (we don't act on it anymore, but it
      // got there from the drawer's nav).
      if (window.history && window.history.replaceState) {{
        try {{
          const u = new URL(window.location.href);
          if (u.searchParams.has('install')) {{
            u.searchParams.delete('install');
            window.history.replaceState(null, '', u.pathname + u.search + u.hash);
          }}
        }} catch (_) {{}}
      }}

      const pill = document.getElementById('install-pill');
      const toast = document.getElementById('install-toast');

      // Skip the pill entirely if we're already running standalone
      // (sub-app already installed — install would be a no-op).
      const inThisStandalone = window.matchMedia(
        '(display-mode: standalone)'
      ).matches && window.location.pathname.startsWith('/apps/');

      function wirePill() {{
        const deferred = window.__bipDeferred;
        if (!deferred || inThisStandalone) return;
        pill.classList.add('visible');
        pill.onclick = async () => {{
          try {{
            deferred.prompt();
            const result = await deferred.userChoice;
            window.__bipDeferred = null;
            if (result.outcome === 'accepted') {{
              pill.classList.remove('visible');
            }}
          }} catch (_) {{
            // Prompt can throw if called after consumption — hide
            // and let the user retry from Chrome menu if they want.
            pill.classList.remove('visible');
          }}
        }};
      }}

      if (window.__bipDeferred) wirePill();
      window.addEventListener('mobius:bip-ready', wirePill);

      window.addEventListener('mobius:installed', () => {{
        pill.classList.remove('visible');
        toast.textContent = APP_NAME + ' is on your home screen';
        toast.classList.add('visible');
        setTimeout(() => toast.classList.remove('visible'), 3000);
      }});
    }})();
  </script>
</body>
</html>"""
  return HTMLResponse(
    content=html,
    headers={"Cache-Control": "no-cache, must-revalidate"},
  )
