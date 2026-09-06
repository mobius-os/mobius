"""Anonymous, exact-app runtime publication and bounded public fetching.

Public app sessions reuse the normal opaque app frame, but their bearer has no
owner identity and no access to owner/app APIs. The only server-mediated
network capability is GET against the app manifest's exact reviewed allowlist.
"""

from __future__ import annotations

import hashlib
import html
import json
from copy import deepcopy
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from slowapi import Limiter

from app import auth, models
from app.compiler import owned_public_bundle_path
from app.config import get_settings
from app.database import SessionLocal
from app.deps import resolve_public_app_token
from app.frontend_assets import baked_frontend_dir, resolve_frontend_dir
from app.public_app_transport import fetch_public_url
from app.routes.public_storage import (
  delete_public_value,
  legacy_public_app_read_limit,
  legacy_public_app_write_limit,
  list_public_values,
  read_public_value,
  write_public_value,
)

router = APIRouter(prefix="/api/public-apps", tags=["public-apps"])

# The host page's script, built from frontend/src/publicHost/ by
# scripts/build-runtime.mjs and served like every other frontend/public asset.
PUBLIC_HOST_SCRIPT = "mobius-public-host.js"

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
  PUBLIC_HOST_SCRIPT,
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


def _json_for_slot(value) -> str:
  # The HTML parser ends the slot at the first literal `</`; escaping `<` (a
  # valid JSON escape) keeps owner-authored app names from closing it.
  return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c")


def _host_script_rev() -> str:
  # Folded into the script URL so a redeployed bundle is never masked by the
  # browser's heuristic caching of an unversioned root asset.
  data_dir = get_settings().data_dir
  for directory in (resolve_frontend_dir(data_dir), baked_frontend_dir()):
    try:
      bytes_ = (directory / PUBLIC_HOST_SCRIPT).read_bytes()
    except OSError:
      continue
    return hashlib.sha256(bytes_).hexdigest()[:16]
  return "0"


def _public_host_html(app: models.App, token: str) -> str:
  title = html.escape(app.public_name, quote=True)
  installed_runtime = (
    app.capability_contract.get("runtime", {})
    if isinstance(app.capability_contract, dict)
    else {}
  )
  device_storage = installed_runtime.get("device.storage")
  config = {
    "appId": app.id,
    "token": token,
    "version": app.public_bundle_digest or "0",
    "appInstance": app.token_nonce or "",
    # Only device.storage has a trusted provider in this minimal host; every
    # other declared capability is withheld so the runtime treats it as
    # undeclared.
    "capabilityContract": {
      "runtime": {
        "device.storage": device_storage,
      } if isinstance(device_storage, dict) else {},
    },
  }
  script_url = f"/{PUBLIC_HOST_SCRIPT}?v={_host_script_rev()}"
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
  <script type="application/json" id="mobius-public-host">{_json_for_slot(config)}</script>
  <script type="module" src="{script_url}"></script>
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
        models.App.public_name.isnot(None),
        models.App.public_bundle_path.isnot(None),
      )
      .first()
    )
    if app is None or not app.public_token_nonce:
      return None
    token = auth.create_public_app_token(app.id, app.public_token_nonce)
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


def _bearer(authorization: str | None) -> str:
  scheme, _, token = (authorization or "").partition(" ")
  if scheme.lower() != "bearer" or not token:
    raise HTTPException(status_code=401, detail="Valid public app token required.")
  return token


@router.get("/{app_id}/module")
async def public_app_module(
  app_id: int,
  authorization: str | None = Header(default=None),
):
  """Serve the immutable module bound to one active hosted publication."""
  token = _bearer(authorization)
  db = SessionLocal()
  try:
    access = resolve_public_app_token(token, db, expected_app_id=app_id)
    path = owned_public_bundle_path(app_id, access.app.public_bundle_path)
    digest = access.app.public_bundle_digest
  finally:
    db.close()
  if path is None or not path.is_file() or not digest:
    raise HTTPException(status_code=404, detail="Published module not found.")
  return FileResponse(
    Path(path),
    media_type="text/javascript; charset=utf-8",
    headers={
      "Cache-Control": "private, max-age=31536000, immutable",
      "ETag": f'"{digest}"',
      "X-Mobius-Offline": "false",
    },
  )


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
  return await fetch_public_url(app_id, url, rules)


@router.get("/{app_id}/storage/{path:path}")
@legacy_public_app_read_limit
async def public_app_storage_read(
  app_id: int,
  path: str,
  request: Request,
  authorization: str | None = Header(default=None),
):
  """Compatibility alias for pre-unification hosted mini-app bundles."""
  return await read_public_value(
    _bearer(authorization), path, expected_app_id=app_id,
  )


@router.get("/{app_id}/storage-list/{prefix:path}")
@legacy_public_app_read_limit
async def public_app_storage_list(
  app_id: int,
  prefix: str,
  request: Request,
  include_content: bool = False,
  limit: int = 100,
  cursor: str | None = None,
  authorization: str | None = Header(default=None),
):
  """Compatibility alias for pre-unification hosted mini-app bundles."""
  return await list_public_values(
    _bearer(authorization), prefix,
    include_content=include_content,
    limit=limit,
    cursor=cursor,
    expected_app_id=app_id,
  )


@router.put("/{app_id}/storage/{path:path}", status_code=204)
@legacy_public_app_write_limit
async def public_app_storage_write(
  app_id: int,
  path: str,
  request: Request,
  authorization: str | None = Header(default=None),
):
  """Compatibility alias for pre-unification hosted mini-app bundles."""
  return await write_public_value(
    _bearer(authorization), path, request, expected_app_id=app_id,
  )


@router.delete("/{app_id}/storage/{path:path}", status_code=204)
@legacy_public_app_write_limit
async def public_app_storage_delete(
  app_id: int,
  path: str,
  request: Request,
  authorization: str | None = Header(default=None),
):
  """Compatibility alias for pre-unification hosted mini-app bundles."""
  return await delete_public_value(
    _bearer(authorization), path, expected_app_id=app_id,
  )
