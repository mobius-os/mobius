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
          "src": f"{base}icon-192.png",
          "sizes": "192x192",
          "type": "image/png",
          "purpose": "any maskable",
        },
        {
          "src": f"{base}icon-512.png",
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
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
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
    /* Install confirm card: bottom-sheet style overlay that appears
       after the app renders, with the icon + name + Install button.
       The button is the install trigger — tapping it calls
       `BeforeInstallPromptEvent.prompt()` directly, then Chromium's
       native install dialog appears for the final OS-level confirm.
       Hidden when already running in standalone mode (install
       already happened) or after dismiss, unless `?install=1` is in
       the URL (drawer's "Install to home screen" forces show even
       on a previously-dismissed app). */
    #install-backdrop {{
      position: fixed; inset: 0;
      background: rgba(0, 0, 0, 0.5);
      backdrop-filter: blur(2px);
      -webkit-backdrop-filter: blur(2px);
      z-index: 9998;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.25s ease-out;
    }}
    #install-backdrop.visible {{ opacity: 1; pointer-events: auto; }}
    #install-card {{
      position: fixed; left: 0; right: 0; bottom: 0;
      background: var(--surface, #14181f);
      color: var(--text, #d4d4d8);
      padding: 24px 20px calc(24px + env(safe-area-inset-bottom));
      border-radius: 18px 18px 0 0;
      box-shadow: 0 -10px 40px rgba(0, 0, 0, 0.5);
      z-index: 9999;
      transform: translateY(100%);
      transition: transform 0.3s cubic-bezier(0.2, 0.9, 0.3, 1);
      max-width: 520px;
      margin: 0 auto;
      font-family: var(--font);
    }}
    #install-card.visible {{ transform: translateY(0); }}
    .ic-header {{
      display: flex; align-items: center; gap: 14px;
      margin-bottom: 16px;
    }}
    .ic-icon {{
      width: 56px; height: 56px;
      border-radius: 12px;
      flex-shrink: 0;
      background: var(--bg);
    }}
    .ic-title {{
      font-size: 17px; font-weight: 600; color: var(--text);
      margin: 0 0 2px 0;
    }}
    .ic-subtitle {{
      font-size: 13px; color: var(--muted, #52525b);
      margin: 0;
    }}
    .ic-info {{
      font-size: 13px; color: var(--muted);
      line-height: 1.5;
      margin: 0 0 18px 0;
      padding: 12px 14px;
      background: var(--bg);
      border-radius: 10px;
      border: 1px solid var(--border, #252b36);
    }}
    .ic-info-row {{
      display: flex; gap: 8px; align-items: flex-start;
    }}
    .ic-info-row + .ic-info-row {{ margin-top: 8px; }}
    .ic-info-dot {{
      color: var(--accent, #a78bfa);
      flex-shrink: 0;
    }}
    .ic-actions {{
      display: flex; gap: 10px;
    }}
    .ic-btn {{
      flex: 1;
      border: none; border-radius: 10px;
      padding: 13px 16px;
      font-size: 15px; font-weight: 600;
      font-family: inherit; cursor: pointer;
    }}
    .ic-btn--secondary {{
      background: transparent;
      color: var(--muted);
      border: 1px solid var(--border);
    }}
    .ic-btn--primary {{
      background: var(--accent, #a78bfa);
      color: #0c0f14;
    }}
    .ic-btn--primary:active {{
      background: var(--accent-hover, #c4b5fd);
    }}
    .ic-success {{ display: none; text-align: center; padding: 8px 0; }}
    .ic-success-icon {{
      font-size: 36px; line-height: 1; margin-bottom: 10px;
    }}
    .ic-success-title {{
      font-size: 17px; font-weight: 600; margin-bottom: 6px;
    }}
    .ic-success-hint {{
      font-size: 13px; color: var(--muted); line-height: 1.5;
      margin-bottom: 18px;
    }}
    #install-card.success .ic-header,
    #install-card.success .ic-info,
    #install-card.success .ic-actions {{ display: none; }}
    #install-card.success .ic-success {{ display: block; }}
  </style>
</head>
<body>
  <div id="root"></div>
  <div id="loading"><div class="spinner"></div><div>Loading {app_name_html}…</div></div>
  <div id="install-backdrop"></div>
  <div id="install-card" role="dialog" aria-modal="true" aria-labelledby="ic-title">
    <div class="ic-header">
      <img class="ic-icon" src="/apps/{slug}/icon-192.png" alt="">
      <div>
        <p class="ic-title" id="ic-title">{app_name_html}</p>
        <p class="ic-subtitle" id="ic-subtitle">Install to home screen</p>
      </div>
    </div>
    <div class="ic-info" id="ic-info">
      <div class="ic-info-row">
        <span class="ic-info-dot">›</span>
        <span>One-tap launch from your home screen.</span>
      </div>
      <div class="ic-info-row">
        <span class="ic-info-dot">›</span>
        <span>Edit anytime — just open Möbius and chat.</span>
      </div>
    </div>
    <div class="ic-actions">
      <button class="ic-btn ic-btn--secondary" id="ic-cancel">Not now</button>
      <button class="ic-btn ic-btn--primary" id="ic-install">Install</button>
    </div>
    <div class="ic-success">
      <div class="ic-success-icon" aria-hidden="true">✓</div>
      <div class="ic-success-title">Installed</div>
      <div class="ic-success-hint">
        Find {app_name_html} on your home screen and tap to launch.
      </div>
      <div class="ic-actions">
        <button class="ic-btn ic-btn--primary" id="ic-done">Done</button>
      </div>
    </div>
  </div>
  <script>
    // Capture `beforeinstallprompt` AS EARLY AS POSSIBLE — Chromium
    // fires it shortly after `DOMContentLoaded`, and if our listener
    // isn't attached yet the event is lost. The module-script below
    // does async fetches before its own listener attaches, so we'd
    // miss it without this pre-listener. Stash on `window` so the
    // module script can pick it up whenever it's ready.
    window.__bipDeferred = null;
    window.addEventListener('beforeinstallprompt', (e) => {{
      e.preventDefault();
      window.__bipDeferred = e;
      window.dispatchEvent(new CustomEvent('mobius:bip-ready'));
    }});
    window.addEventListener('appinstalled', () => {{
      window.dispatchEvent(new CustomEvent('mobius:installed'));
    }});
  </script>
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
      try {{
        // Fetch theme + app-scoped token in parallel, then import the
        // app module. App-scoped tokens are short-lived JWTs minted
        // from the owner token; the app component receives this one
        // (not the owner token) so a compromised app can't escalate.
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
        const module = await import('/api/apps/' + APP_ID + '/module?token=' + encodeURIComponent(appToken));
        const Component = module.default;
        if (!Component) throw new Error('App module has no default export');
        const React = await import('react');
        const {{ createRoot }} = await import('react-dom/client');
        const root = createRoot(document.getElementById('root'));
        root.render(React.createElement(Component, {{ appId: APP_ID, token: appToken }}));
        document.getElementById('loading').classList.add('hidden');
      }} catch (err) {{
        const loading = document.getElementById('loading');
        // Build error UI via DOM nodes (not innerHTML) — err.message
        // can carry attacker-controlled strings from a misbehaving
        // app module, and the standalone shell sits on the same
        // origin as Möbius (so an injected <script> would have JWT
        // access via localStorage).
        loading.textContent = '';
        const msg = document.createElement('div');
        msg.style.color = 'var(--danger)';
        msg.style.fontSize = '13px';
        msg.textContent = 'Failed to load: ' + (err && err.message || String(err));
        loading.appendChild(msg);
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
      const card = document.getElementById('install-card');
      const backdrop = document.getElementById('install-backdrop');
      const installBtn = document.getElementById('ic-install');
      const cancelBtn = document.getElementById('ic-cancel');
      const doneBtn = document.getElementById('ic-done');
      const subtitle = document.getElementById('ic-subtitle');
      const info = document.getElementById('ic-info');

      const forceShow = new URLSearchParams(window.location.search).get('install') === '1';
      const dismissKey = 'mobius-install-dismissed-' + APP_SLUG;

      // Honor display-mode standalone ONLY for the no-force path. A
      // user explicitly tapping Install in the drawer overrides this
      // even if they're inside the parent Möbius PWA window.
      if (!forceShow) {{
        const inStandalone =
          window.matchMedia('(display-mode: standalone)').matches ||
          window.navigator.standalone === true;
        if (inStandalone) return;
        if (sessionStorage.getItem(dismissKey)) return;
      }}

      function show() {{
        backdrop.classList.add('visible');
        card.classList.add('visible');
      }}
      function hideAndDismiss() {{
        backdrop.classList.remove('visible');
        card.classList.remove('visible');
        sessionStorage.setItem(dismissKey, '1');
      }}
      function showSuccess() {{
        card.classList.add('success');
      }}
      cancelBtn.addEventListener('click', hideAndDismiss);
      backdrop.addEventListener('click', hideAndDismiss);
      doneBtn.addEventListener('click', () => {{
        backdrop.classList.remove('visible');
        card.classList.remove('visible');
      }});

      function wireInstallButton() {{
        if (!window.__bipDeferred) return;
        installBtn.disabled = false;
        installBtn.style.opacity = '';
        subtitle.textContent = 'Install to home screen';
        installBtn.onclick = async () => {{
          const deferred = window.__bipDeferred;
          if (!deferred) return;
          deferred.prompt();
          const result = await deferred.userChoice;
          window.__bipDeferred = null;
          if (result.outcome === 'dismissed') hideAndDismiss();
        }};
      }}

      // iOS UA detect — share-menu fallback uses the same card.
      const isIOSSafari = /iphone|ipad|ipod/i.test(navigator.userAgent) &&
        !window.MSStream &&
        /safari/i.test(navigator.userAgent) &&
        !/(crios|fxios|edgios)/i.test(navigator.userAgent);

      function paintIOSFallback() {{
        subtitle.textContent = 'Add to Home Screen';
        info.textContent = '';
        const ios = document.createElement('div');
        ios.style.fontSize = '13px';
        ios.style.color = 'var(--muted)';
        ios.style.lineHeight = '1.6';
        ios.textContent =
          'Tap the Share button in Safari (the square with an ' +
          'arrow), then choose "Add to Home Screen".';
        info.appendChild(ios);
        installBtn.style.display = 'none';
        cancelBtn.textContent = 'Close';
        cancelBtn.style.flex = '1';
      }}

      // `shown` guards against double-show across the multiple
      // paths that can each request the card (immediate-wire,
      // bip-ready listener, forceShow timer, iOS timer). First
      // requester wins; subsequent calls are no-ops.
      let shown = false;
      function showOnce() {{
        if (shown) return;
        shown = true;
        show();
      }}

      // If the bip event is already in hand (fired before this
      // script ran), wire and show. Otherwise listen for the early
      // bridge event from the <script> at top of <body>.
      if (window.__bipDeferred) wireInstallButton();
      window.addEventListener('mobius:bip-ready', wireInstallButton);
      window.addEventListener('mobius:installed', showSuccess);

      // Show the card. Strategy:
      //   - forceShow (`?install=1`): show after a brief delay so
      //     the app paints first, regardless of event timing. If
      //     the event arrives later, the Install button activates
      //     in place. If 8s pass with no event AND we're not in
      //     iOS, swap to a manual-install fallback message rather
      //     than leaving the user stuck on "Preparing install…"
      //     forever — Chromium suppresses `beforeinstallprompt`
      //     after a user has dismissed it too many times, and the
      //     only path forward is the browser's own menu.
      //   - no forceShow: show only when the event fires or iOS UA
      //     is detected (lazy, doesn't interrupt unannounced).
      if (forceShow) {{
        if (isIOSSafari) paintIOSFallback();
        else if (!window.__bipDeferred) {{
          installBtn.disabled = true;
          installBtn.style.opacity = '0.5';
          subtitle.textContent = 'Preparing install…';
        }}
        setTimeout(showOnce, 600);
        // Fallback for Chromium install-suppression. 8s is long
        // enough that any real bip event will have fired; if it
        // hasn't, the browser likely won't fire it at all.
        if (!isIOSSafari) {{
          setTimeout(() => {{
            if (window.__bipDeferred) return;  // event arrived in time
            subtitle.textContent = "Can't install right now";
            info.textContent = '';
            const hint = document.createElement('div');
            hint.style.fontSize = '13px';
            hint.style.color = 'var(--muted)';
            hint.style.lineHeight = '1.6';
            hint.textContent =
              'Your browser blocked the automatic install (this ' +
              'happens after dismissing too many times). Try: ' +
              'Chrome menu (⋮) → "Install app" or "Add to Home ' +
              'screen".';
            info.appendChild(hint);
            installBtn.style.display = 'none';
            cancelBtn.textContent = 'Close';
            cancelBtn.style.flex = '1';
          }}, 8000);
        }}
      }} else {{
        // Listen-and-show paths: bip event lands, or iOS detected.
        // If `__bipDeferred` was ALREADY set when this script ran,
        // the bridge event already fired and we won't get another
        // — show immediately rather than waiting for a duplicate.
        if (window.__bipDeferred) {{
          setTimeout(showOnce, 600);
        }} else {{
          window.addEventListener('mobius:bip-ready', () => {{
            setTimeout(showOnce, 600);
          }});
        }}
        if (isIOSSafari) {{
          setTimeout(() => {{
            if (window.__bipDeferred) return;
            paintIOSFallback();
            showOnce();
          }}, 1200);
        }}
      }}
    }})();
  </script>
</body>
</html>"""
  return HTMLResponse(
    content=html,
    headers={"Cache-Control": "no-cache, must-revalidate"},
  )
