"""Anonymous, exact-app runtime publication and bounded public fetching.

Public app sessions reuse the normal opaque app frame, but their bearer has no
owner identity and no access to owner/app APIs. The only server-mediated
network capability is GET against the app manifest's exact reviewed allowlist.
"""

from __future__ import annotations

import html
import json
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from slowapi import Limiter

from app import auth, models
from app.database import SessionLocal
from app.deps import resolve_public_app_token
from app.net_utils import validate_url_safe
from app.routes.proxy import _capped_response

router = APIRouter(prefix="/api/public-apps", tags=["public-apps"])

PUBLIC_APP_RESERVED_SLUGS = frozenset({
  "api",
  "app",
  "app-assets",
  "app-embeds",
  "apps",
  "assets",
  "chat",
  "index.html",
  "manifest.webmanifest",
  "mobius-runtime.js",
  "recover",
  "shell",
  "sites",
  "sw.js",
  "sw-push.js",
  "vendor",
})


def public_slug_is_available(slug: str) -> bool:
  return bool(
    slug
    and "/" not in slug
    and all(ch.isalnum() or ch in "-_" for ch in slug)
    and slug not in PUBLIC_APP_RESERVED_SLUGS
  )


def _json_for_script(value) -> str:
  # JSON is executable JavaScript here. Escape '<' so owner-authored app names
  # cannot manufacture a closing script tag inside the platform-owned page.
  return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c")


def _public_host_html(app: models.App, token: str) -> str:
  app_id = app.id
  frame_version = app.updated_at.isoformat() if app.updated_at else "0"
  title = html.escape(app.name, quote=True)
  app_id_json = _json_for_script(app_id)
  token_json = _json_for_script(token)
  version_json = _json_for_script(frame_version)
  return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <meta name="color-scheme" content="dark light" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: dark; background: #101514; }}
    * {{ box-sizing: border-box; }}
    html, body, iframe {{ width: 100%; height: 100%; margin: 0; border: 0; }}
    body {{ overflow: hidden; background: #101514; }}
    iframe {{ display: block; background: #101514; }}
    #status {{
      position: fixed; inset: 0; display: grid; place-items: center;
      padding: 24px; background: #101514; color: #d7dfdc;
      font: 500 14px/1.5 ui-sans-serif, system-ui, sans-serif;
      text-align: center; transition: opacity .16s ease; pointer-events: none;
    }}
    #status.is-ready {{ opacity: 0; }}
    #status.is-error {{ pointer-events: auto; }}
  </style>
</head>
<body>
  <iframe id="app" title="{title}"></iframe>
  <div id="status" role="status">Opening {title}…</div>
  <script>
  (() => {{
    const APP_ID = {app_id_json};
    const TOKEN = {token_json};
    const VERSION = {version_json};
    const FRAME_URL = '/api/apps/' + APP_ID + '/frame?v=' + encodeURIComponent(VERSION);
    const frame = document.getElementById('app');
    const status = document.getElementById('status');

    function send(message, transfer) {{
      try {{ frame.contentWindow.postMessage(message, '*', transfer || []); }} catch {{}}
    }}

    function showError(message) {{
      status.textContent = message || 'This public app could not be opened.';
      status.className = 'is-error';
    }}

    frame.addEventListener('load', () => send({{
      type: 'moebius:frame-init', token: TOKEN, themeCss: '', bg: '#101514',
      storage: {{}}, capabilityContract: null,
    }}));
    frame.src = FRAME_URL;

    window.addEventListener('message', event => {{
      if (event.source !== frame.contentWindow) return;
      if (event.origin !== 'null' && event.origin !== window.location.origin) return;
      const message = event.data;
      if (!message || typeof message !== 'object') return;

      if (message.type === 'moebius:module-request') {{
        if (!message.requestId || String(message.appId) !== String(APP_ID)) return;
        send({{ type: 'moebius:module-ack', requestId: message.requestId, appId: APP_ID }});
        const retry = message.retry === 1 ? '&retry=1' : '';
        fetch('/api/apps/' + APP_ID + '/module?token=' + encodeURIComponent(TOKEN)
          + '&v=' + encodeURIComponent(VERSION) + retry, {{ cache: 'no-cache' }})
          .then(async response => {{
            if (!response.ok) {{
              const error = new Error('The app module returned ' + response.status + '.');
              error.status = response.status;
              throw error;
            }}
            return response.arrayBuffer();
          }})
          .then(bytes => send({{
            type: 'moebius:module-result', requestId: message.requestId,
            appId: APP_ID, ok: true, bytes,
          }}, [bytes]))
          .catch(error => send({{
            type: 'moebius:module-result', requestId: message.requestId,
            appId: APP_ID, ok: false,
            error: {{ code: 'module-load-failed', message: error.message,
              status: error.status || null }},
          }}));
        return;
      }}

      if (message.type === 'moebius:storage-rpc') {{
        if (!message.requestId) return;
        const method = typeof message.method === 'string' ? message.method : '';
        const empty = method === 'list' ? []
          : method === 'pendingCount' || method === 'pendingSignalCount' ? 0
          : method === 'getWithVersion' ? {{ value: null, version: null }}
          : method === 'subscribe' || method === 'unsubscribe' ? true
          : method === 'get' || method === 'getText' || method === 'getBlob' ? null
          : undefined;
        if (empty !== undefined) {{
          send({{ type: 'moebius:storage-rpc-result', requestId: message.requestId,
            ok: true, result: empty }});
        }} else {{
          send({{ type: 'moebius:storage-rpc-result', requestId: message.requestId,
            ok: false, error: {{ name: 'Error', code: 'public_storage_readonly',
              status: 403, message: 'Public app sessions do not use owner storage.' }} }});
        }}
        return;
      }}

      if (message.type === 'moebius:capability-open' ||
          message.type === 'moebius:capability-control') {{
        if (!message.requestId) return;
        send({{ type: 'moebius:capability-error', requestId: message.requestId,
          code: 'public_capability_unavailable', name: 'NotAllowedError',
          message: 'This device capability is unavailable in a public session.' }});
        return;
      }}

      if (message.type === 'moebius:frame-mounted' &&
          String(message.appId) === String(APP_ID)) {{
        status.className = 'is-ready';
        return;
      }}
      if (message.type === 'moebius:frame-error') {{
        showError(message.error?.message || message.message);
        return;
      }}
      if (message.type === 'moebius:token-expired' ||
          message.type === 'moebius:token-refresh-request') {{
        window.location.reload();
      }}
    }});
  }})();
  </script>
</body>
</html>"""


def public_app_page_for_path(path: str) -> HTMLResponse | None:
  """Return an anonymous app host for one exact published slug, if any."""
  slug = path.strip("/")
  if not public_slug_is_available(slug):
    return None
  db = SessionLocal()
  try:
    app = (
      db.query(models.App)
      .filter(
        models.App.slug == slug,
        models.App.deleted_at.is_(None),
        models.App.public_enabled.is_(True),
      )
      .first()
    )
    if app is None or not app.token_nonce:
      return None
    token = auth.create_public_app_token(app.id, app.token_nonce)
    body = _public_host_html(app, token)
  finally:
    db.close()
  return HTMLResponse(body, headers={
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  })


def _public_app_key(request: Request) -> str:
  return str(request.path_params.get("app_id", "invalid"))


_fetch_limiter = Limiter(key_func=_public_app_key, key_style="endpoint")
_fetch_limit = _fetch_limiter.shared_limit(
  "3000/minute", scope="public-app-fetch",
)

_usage_lock = threading.Lock()
_usage: dict[int, dict] = {}


def _record_usage(app_id: int, *, elapsed: float, response_bytes: int, failed: bool):
  with _usage_lock:
    row = _usage.setdefault(app_id, {
      "requests": 0,
      "failures": 0,
      "response_bytes": 0,
      "upstream_seconds": 0.0,
      "started_at": datetime.now(UTC).isoformat(),
      "last_request_at": None,
    })
    row["requests"] += 1
    row["failures"] += int(failed)
    row["response_bytes"] += max(0, response_bytes)
    row["upstream_seconds"] += max(0.0, elapsed)
    row["last_request_at"] = datetime.now(UTC).isoformat()


def public_app_usage_snapshot() -> dict[str, dict]:
  """Cheap per-process counters for spotting a public app's server footprint."""
  with _usage_lock:
    return {str(app_id): deepcopy(row) for app_id, row in _usage.items()}


def _bearer(authorization: str | None) -> str:
  scheme, _, token = (authorization or "").partition(" ")
  if scheme.lower() != "bearer" or not token:
    raise HTTPException(status_code=401, detail="Valid public app token required.")
  return token


def _target_allowed(url: str, rules: list[dict]) -> bool:
  try:
    parsed = urlsplit(url)
    port = parsed.port
  except ValueError:
    return False
  if parsed.scheme != "https" or not parsed.hostname:
    return False
  host = parsed.hostname.lower()
  origin = f"https://{host if port in (None, 443) else f'{host}:{port}'}"
  path = parsed.path or "/"
  lowered_path = path.lower()
  # Do not let an allowlisted prefix be escaped through a server that decodes
  # encoded path separators/dot segments before routing the request.
  if (
    "\\" in path
    or any(segment in (".", "..") for segment in path.split("/"))
    or any(encoded in lowered_path for encoded in ("%2e", "%2f", "%5c"))
  ):
    return False
  for rule in rules:
    if not isinstance(rule, dict) or rule.get("origin") != origin:
      continue
    prefix = rule.get("path_prefix")
    if not isinstance(prefix, str):
      continue
    if path == prefix or prefix == "/" or (
      prefix.endswith("/") and path.startswith(prefix)
    ) or path.startswith(prefix + "/"):
      return True
  return False


@router.get("/{app_id}/fetch")
@_fetch_limit
async def public_app_fetch(
  app_id: int,
  request: Request,
  url: str,
  authorization: str | None = Header(default=None),
):
  """GET one app-declared public URL with exact-app anonymous authority."""
  token = _bearer(authorization)
  db = SessionLocal()
  try:
    access = resolve_public_app_token(token, db, expected_app_id=app_id)
    rules = deepcopy(access.network)
  finally:
    db.close()
  if not _target_allowed(url, rules):
    raise HTTPException(status_code=403, detail="URL is not allowed for this public app.")

  pinned_url, host_header, sni_host = validate_url_safe(url)
  started = time.monotonic()
  try:
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
      upstream = client.build_request("GET", pinned_url)
      upstream.headers["host"] = host_header
      upstream.extensions["sni_hostname"] = sni_host
      response = await _capped_response(
        client, upstream, forward_cache_headers=True,
      )
  except Exception:
    _record_usage(
      app_id, elapsed=time.monotonic() - started, response_bytes=0, failed=True,
    )
    raise
  body = getattr(response, "body", b"") or b""
  _record_usage(
    app_id,
    elapsed=time.monotonic() - started,
    response_bytes=len(body),
    failed=response.status_code >= 400,
  )
  return response
