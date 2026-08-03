"""Runtime-code serving and validation routes for installed apps."""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app import activity, models, theme
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_owner, resolve_owner_or_app
from app.http_caching import strip_range
from app.net_utils import validate_url_safe
from app.resource_access import live_app, live_app_or_404


router = APIRouter()

_DEVICE_ASSET_CAPABILITY = "device.asset-cache"
_SPEECH_MODEL_CAPABILITY = "device.speech-models"
_DEVICE_ASSET_MAX_REDIRECTS = 5
_DEVICE_ASSET_USER_AGENT = "Mobius/1.0 (device asset relay)"


def _device_asset_declaration(app: models.App) -> dict:
  contract = app.capability_contract
  runtime = contract.get("runtime") if isinstance(contract, dict) else None
  declaration = None
  if isinstance(runtime, dict):
    declaration = runtime.get(_DEVICE_ASSET_CAPABILITY)
    if not isinstance(declaration, dict):
      declaration = runtime.get(_SPEECH_MODEL_CAPABILITY)
  if not isinstance(declaration, dict) or declaration.get("version") != 1:
    raise HTTPException(
      status_code=403,
      detail="This app has not declared device asset storage or speech-model management.",
    )
  return declaration


def _parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
  match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", value or "")
  if not match:
    return None
  total = None if match.group(3) == "*" else int(match.group(3))
  start = int(match.group(1))
  end = int(match.group(2))
  if start > end or (total is not None and end >= total):
    return None
  return start, end, total


def _is_https_device_asset_url(url: str) -> bool:
  parsed = urlparse(url)
  return bool(
    parsed.scheme == "https"
    and parsed.hostname
    and not parsed.username
    and not parsed.password
  )


async def _open_device_asset_range(
  url: str,
  offset: int,
  length: int,
) -> tuple[httpx.AsyncClient, httpx.Response, int | None]:
  """Open one exact public byte range without retaining it on the server."""
  current_url = url
  client = httpx.AsyncClient(
    follow_redirects=False,
    timeout=httpx.Timeout(60.0, connect=20.0),
  )
  expected_end = offset + length - 1
  try:
    for hop in range(_DEVICE_ASSET_MAX_REDIRECTS + 1):
      pinned_url, host_header, sni_host = validate_url_safe(current_url)
      request = client.build_request(
        "GET",
        pinned_url,
        headers={
          "Accept": "application/octet-stream,*/*;q=0.1",
          "Accept-Encoding": "identity",
          "Range": f"bytes={offset}-{expected_end}",
          "User-Agent": _DEVICE_ASSET_USER_AGENT,
        },
      )
      request.headers["host"] = host_header
      request.extensions["sni_hostname"] = sni_host
      try:
        upstream = await client.send(request, stream=True)
      except httpx.TimeoutException as exc:
        raise HTTPException(504, "Timed out downloading the device asset.") from exc
      except httpx.RequestError as exc:
        raise HTTPException(502, "Could not reach the device asset source.") from exc

      if upstream.status_code in (301, 302, 303, 307, 308):
        location = upstream.headers.get("location")
        await upstream.aclose()
        if not location:
          raise HTTPException(502, "Device asset redirect had no destination.")
        if hop >= _DEVICE_ASSET_MAX_REDIRECTS:
          raise HTTPException(502, "Too many device asset redirects.")
        current_url = urljoin(current_url, location)
        if not _is_https_device_asset_url(current_url):
          raise HTTPException(502, "Device asset redirect requires a public HTTPS URL.")
        continue

      content_encoding = upstream.headers.get("content-encoding", "identity")
      if content_encoding not in ("", "identity"):
        await upstream.aclose()
        raise HTTPException(502, "Device asset source changed the requested bytes.")

      declared_length = upstream.headers.get("content-length")
      try:
        actual_length = int(declared_length) if declared_length is not None else None
      except ValueError:
        actual_length = None
      total_bytes = None
      if upstream.status_code == 206:
        content_range = _parse_content_range(upstream.headers.get("content-range"))
        if not content_range or content_range[:2] != (offset, expected_end):
          await upstream.aclose()
          raise HTTPException(502, "Device asset source returned the wrong byte range.")
        total_bytes = content_range[2]
      elif not (
        upstream.status_code == 200
        and offset == 0
        and actual_length == length
      ):
        status = upstream.status_code
        await upstream.aclose()
        raise HTTPException(
          502,
          f"Device asset source did not honor the requested range ({status}).",
        )
      if actual_length is not None and actual_length != length:
        await upstream.aclose()
        raise HTTPException(502, "Device asset source returned an unexpected size.")
      return client, upstream, total_bytes
    raise HTTPException(502, "Too many device asset redirects.")
  except BaseException:
    await client.aclose()
    raise


@router.get("/{app_id}/device-assets/relay")
async def relay_device_asset_range(
  app_id: int,
  url: str = Query(min_length=1, max_length=4096),
  offset: int = Query(ge=0),
  length: int = Query(gt=0),
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Stream one reviewed, bounded public range into the shell's device cache.

  The trusted shell owns this request; an opaque app frame cannot call it with
  its scoped bearer. Bytes pass through the server only for CORS/SSRF safety
  and are never written to disk. The shell verifies the app-supplied SHA-256
  before retaining each chunk in browser Cache Storage.
  """
  app = live_app_or_404(db, app_id)
  declaration = _device_asset_declaration(app)
  limits = declaration.get("limits") or {}
  max_asset_bytes = int(limits.get("max_asset_bytes") or 0)
  max_chunk_bytes = int(limits.get("max_chunk_bytes") or 0)
  if length > max_chunk_bytes or offset + length > max_asset_bytes:
    raise HTTPException(413, "Requested device asset range exceeds its reviewed limit.")
  if not _is_https_device_asset_url(url):
    raise HTTPException(400, "Device assets require a public HTTPS URL.")
  db.close()

  client, upstream, total_bytes = await _open_device_asset_range(url, offset, length)
  if total_bytes is not None and total_bytes > max_asset_bytes:
    await upstream.aclose()
    await client.aclose()
    raise HTTPException(413, "Device asset exceeds its reviewed size limit.")

  async def stream():
    transferred = 0
    try:
      async for chunk in upstream.aiter_raw():
        transferred += len(chunk)
        if transferred > length:
          raise RuntimeError("Device asset source exceeded its declared range.")
        yield chunk
      if transferred != length:
        raise RuntimeError("Device asset source ended before the range was complete.")
    finally:
      await upstream.aclose()
      await client.aclose()

  headers = {
    "Cache-Control": "no-store",
    "Content-Length": str(length),
    "X-Content-Type-Options": "nosniff",
  }
  if total_bytes is not None:
    headers["X-Mobius-Asset-Total"] = str(total_bytes)
  return StreamingResponse(
    stream(),
    media_type="application/octet-stream",
    headers=headers,
  )


def _etag_for_app(app: models.App) -> str | None:
  """Weak ETag derived from `app.updated_at`. Microsecond precision
  so two updates within the same wall-clock second produce different
  validators — second-precision risks the agent shipping a fix and
  the user's cached browser refusing to revalidate."""
  if not app.updated_at:
    return None
  ts_us = int(app.updated_at.timestamp() * 1_000_000)
  return f'W/"{ts_us}"'


def _not_modified_if_match(
  request: Request,
  etag: str,
  offline: bool = False,
  response_headers: dict[str, str] | None = None,
) -> Response | None:
  """Returns a 304 Response if the request's If-None-Match matches
  `etag`, else None. The 304 keeps the ETag header so a browser
  re-validating an existing cache entry can keep its validator, and
  mirrors the X-Mobius-Offline marker so the 304 carries the same
  cache metadata as the 200 it stands in for. The SW's
  appCodeStoreAction policy keys on that header for the gated
  standalone-navigation cache. Callers whose representation metadata changes
  independently of the body (notably the frame CSP) pass it through so a 304
  freshens the cached policy instead of preserving obsolete headers."""
  match = request.headers.get("if-none-match")
  if match and etag in [v.strip() for v in match.split(",")]:
    headers = dict(response_headers or {})
    headers["ETag"] = etag
    if offline:
      headers["X-Mobius-Offline"] = "1"
    return Response(status_code=304, headers=headers)
  return None


def _frame_etag(
  app: models.App,
  frame_path: Path,
  frame_rev: str | None = None,
) -> str | None:
  """Validator for the `/frame` response, combining the app's
  `updated_at` with the shared runtime-frame file's content and the
  active theme.

  Unlike the per-app module, the frame serves `app-frame.html` — the
  isolation boundary + runtime bootstrap — which changes INDEPENDENTLY of any app
  row. Keying only on `app.updated_at` (as `_etag_for_app` does) means
  an edit to the frame (e.g. changing the broker protocol) never
  invalidates an already-installed PWA: it keeps revalidating against
  an unchanged validator, gets a 304, and runs the stale frame forever.
  That is exactly how a dropped `/vendor/three/` path pinned clients to
  a spinner. Folding a hash of the frame's CONTENT in busts every app's
  frame cache on the next load whenever app-frame.html changes.

  Content hash, not mtime: `cp`, bind-mounts, and backup/restore rewrite
  mtimes independently of content, which risks UNDER-invalidation (a
  real content change that keeps its mtime) — the precise failure mode
  here. The frame file is small, so hashing per request is cheap.

  `frame_rev`: the app-frame.html content hash, already computed once by
  `load_effective_theme` for the same request. Pass it so the frame file
  isn't hashed a SECOND time here — the theme bundle and this ETag share
  one read (both resolve the same candidate list, so the hash is identical;
  see get_frame). When omitted (None), the hash is computed from
  `frame_path` as before, so standalone callers and the unit tests are
  unaffected. An empty rev means the frame was unresolvable — no content
  part, matching the old read-failure fall-through."""
  parts: list[str] = []
  if app.updated_at:
    parts.append(str(int(app.updated_at.timestamp() * 1_000_000)))
  if frame_rev is None:
    try:
      parts.append(hashlib.sha256(frame_path.read_bytes()).hexdigest()[:16])
    except OSError:
      pass
  elif frame_rev:
    parts.append(frame_rev)
  if not parts:
    return None
  return 'W/"' + "-".join(parts) + '"'


@router.api_route("/{app_id}/frame", methods=["GET", "HEAD"])
def get_frame(
  app_id: int,
  request: Request,
  db: Session = Depends(get_db),
):
  """Serves the mini-app runtime frame HTML.

  Token-free as of 2026-04-27: the parent shell injects the auth
  token and the current theme via `postMessage` after the iframe
  loads, instead of having them server-templated into the body.

  Cache freshness model: two independent mechanisms COEXIST. The
  compound `_frame_etag` (folding `app.updated_at` with the shared
  frame file's content) plus `Cache-Control: no-cache` drives the
  browser's HTTP-cache revalidation on cold / non-SW paths — the
  browser sends `If-None-Match` and gets a 304 when nothing changed
  or a fresh 200 when `updated_at` advanced or the frame file
  changed. The service worker revalidates frame/module routes against
  the same ETag via `appCodeHandler` in `sw.js`; that cache is ungated
  and applies to every installed app.
  SEPARATELY, `AppCanvas` appends `?v=<app.updated_at>` to the frame
  URL, which the SW keeps as its offline cache key (it strips only
  token/_/install, not `v`), so an app edit changes the SW key and
  forces a fresh load. `v` is purely a client/SW cache-buster — this
  endpoint never reads it.

  Frame is intentionally public — it's just the runtime shell
  (error UI, postMessage broker/bootstrap). Actual app
  modules at `/api/apps/{id}/module` still require a token. An
  attacker embedding this frame in their own page would receive
  the iframe's `moebius:frame-mounted` postMessage on their parent window,
  but the iframe's origin check (against `window.location.origin`)
  rejects any reply from a non-Möbius origin, so no token can be
  coerced into the frame.
  """
  app = live_app(db, app_id)
  if not app or not app.compiled_path:
    raise HTTPException(status_code=404, detail="App not found.")
  compiled = Path(app.compiled_path)
  if not compiled.exists():
    raise HTTPException(status_code=404, detail="Compiled module missing.")

  # Frame priority: served platform frontend first, then the baked-in fallback.
  # Resolve this BEFORE the ETag so the validator reflects the frame file's
  # content (see _frame_etag) — otherwise a changed frame never reaches
  # installed PWAs.
  frame_candidates = [
    Path(get_settings().data_dir)
    / "platform" / "frontend" / "public" / "app-frame.html",
    # Repo-relative dev/test fallback (== served clone in-container).
    Path(__file__).resolve().parents[3] / "frontend" / "public" / "app-frame.html",
    Path("/app/app-frame.html"),
  ]
  frame_path = next((p for p in frame_candidates if p.exists()), None)
  if frame_path is None:
    raise HTTPException(status_code=404, detail="Frame not found.")

  # The frame is no longer theme-varying: theme-as-data moved theming to the
  # client (the frame's pre-paint IIFE reads the __mobius-theme__ slot +
  # localStorage and paints flash-free; the server no longer injects a
  # <style>). So the validator keys only on app.updated_at + the
  # app-frame.html content hash — NOT the theme. A light/dark toggle no
  # longer needs to bust the frame cache, because the served frame bytes
  # don't change with the theme. Compute the frame content hash and key the
  # validator on it plus app.updated_at.
  frame_rev = theme.frame_content_rev(get_settings().data_dir)
  etag = _frame_etag(app, frame_path, frame_rev=frame_rev)
  frame_cache_headers = {
    "Cache-Control": "no-cache",
  }
  if etag:
    not_modified = _not_modified_if_match(
      request,
      etag,
      app.offline_capable,
      response_headers=frame_cache_headers,
    )
    if not_modified is not None:
      return not_modified

  html = frame_path.read_text(encoding="utf-8")

  # Per-app server-side substitution of the app/chat ids the runtime needs.
  html = html.replace(
    "var _FRAME_APP_ID = 'unknown'",
    f"var _FRAME_APP_ID = {json.dumps(str(app_id))}",
  )
  html = html.replace(
    "var _FRAME_CHAT_ID = ''",
    f"var _FRAME_CHAT_ID = {json.dumps(app.chat_id or '')}",
  )

  # Theme-as-data: the frame no longer has the theme server-injected. Its
  # pre-paint IIFE reads the __mobius-theme__ slot (when the server fills
  # one) and the shell's same-origin localStorage to paint --bg / data-theme
  # / color-scheme flash-free from the fallback :root + the persisted owner
  # mode. The parent shell still posts moebius:frame-init/-theme for LIVE
  # swaps without a reload. Removing the injection means the served frame
  # bytes are theme-independent (so the ETag no longer folds the theme).

  # The element remains unsandboxed until navigation so the shell service
  # worker can intercept and serve a cached frame offline. The origin security
  # middleware applies the complete response sandbox on both 200 and 304. Popups
  # opened by an explicit app link must escape the opaque-origin sandbox:
  # otherwise the destination inherits Origin: null and sites such as GitHub
  # load their document but fail same-origin API/storage requests. This does
  # not relax the app frame itself or let it navigate the owner shell. Reverse
  # proxies pass this origin-owned contract through unchanged.
  headers = dict(frame_cache_headers)
  if etag:
    headers["ETag"] = etag
  # The X-Mobius-Offline header does not gate frame/module caching: the SW
  # caches code for every installed app via appCodeHandler(OFFLINE_APPS_CACHE,
  # {gated:false}), regardless of this header. It only gates the separate
  # standalone-navigation cache and offline write/open semantics.
  # Offline capability is a function of server state, not a client-pushed list.
  if app.offline_capable:
    headers["X-Mobius-Offline"] = "1"

  # app_open: emit on the GET 200 path only — the 304 short-circuit above
  # already returned for cache-revalidating loads (which would otherwise
  # double-count every freshness check on a navigation back), and a HEAD is
  # an existence probe, not a real open, so it must not count either. Best-
  # effort: a log failure must not block the frame response
  # (activity.log_event swallows its own OSError).
  if request.method != "HEAD":
    activity.log_event(
      "app_open", app_id=app.id, slug=app.slug,
    )
  return HTMLResponse(html, headers=headers)


@router.api_route("/{app_id}/module", methods=["GET", "HEAD"])
def get_module(
  app_id: int,
  request: Request,
  token: str | None = None,
  db: Session = Depends(get_db),
):
  """Serves the compiled JS module for a mini-app.

  Accepts a `token` query parameter so the iframe can load the
  module without custom request headers (dynamic `import()` doesn't
  set an Authorization header).

  Cache freshness: ETag derived from `app.updated_at` (microsecond
  precision) + `Cache-Control: no-cache`. Browser sends
  `If-None-Match` on every fetch; we return 304 when the app hasn't
  changed. Matches the `/frame` route's strategy — see comment
  there for the broader rationale.
  """
  # Apps share modules same as they share storage — every mini-app
  # is authored by the owner's own agent, and a multi-app workflow
  # may legitimately want to import or interop across them. Any
  # valid token (owner or app-scoped) is allowed to fetch any
  # module by id. See CLAUDE.md "Mini-app sandbox — accepted
  # same-origin decision" for the broader trust model. resolve_owner_
  # or_app runs the same decode + revocation check the header deps use,
  # so a signed-out token can't keep pulling module source; the empty-
  # token guard stays explicit to keep the "Valid token required" 401
  # (and to avoid feeding a None token into the JWT decoder).
  if not token:
    raise HTTPException(
      status_code=401, detail="Valid token required."
    )
  resolve_owner_or_app(token, db)
  app = live_app(db, app_id)
  if not app or not app.compiled_path:
    raise HTTPException(status_code=404, detail="Module not found.")
  path = Path(app.compiled_path)
  etag = _etag_for_app(app)
  offline_capable = bool(app.offline_capable)
  # FileResponse streams after this function returns. Do not make the stream's
  # lifetime the database checkout's lifetime.
  db.close()
  if not path.exists():
    raise HTTPException(
      status_code=404, detail="Compiled module not found on disk."
    )

  if etag:
    not_modified = _not_modified_if_match(request, etag, offline_capable)
    if not_modified is not None:
      return not_modified

  headers = {"Cache-Control": "no-cache"}
  if etag:
    headers["ETag"] = etag
  # See get_frame: X-Mobius-Offline does not gate in-shell module caching.
  # The SW caches modules for every installed app regardless of this header;
  # the header only gates the separate standalone-navigation cache and
  # offline write/open semantics.
  if offline_capable:
    headers["X-Mobius-Offline"] = "1"
  # The module is a REVALIDATING response (no-cache + stable ETag), so it
  # must never answer a 206. A `Range: bytes=0-0` probe of a FileResponse
  # would otherwise let Chromium store the 1-byte slice and later serve it
  # as a status-200 full body — a black mini-app until the next app update.
  # Stripping Range here keeps the streamed full-body 200 (see http_caching).
  strip_range(request)
  return FileResponse(
    path,
    media_type="application/javascript",
    headers=headers,
  )


@router.get("/{app_id}/validate")
async def validate_app(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Validates a compiled mini-app for common issues.

  Checks that the compiled file exists, is parseable JS, exports a
  default, and that the source JSX is present. Returns a report the
  agent can use to decide whether to offer debugging.
  """
  app = live_app_or_404(db, app_id)
  app_name = app.name
  jsx_source = app.jsx_source
  compiled_path = app.compiled_path
  db.close()

  issues = []

  if not jsx_source:
    issues.append("No JSX source stored in database.")
  if not compiled_path:
    issues.append("No compiled path set — compilation may have failed.")
  else:
    path = Path(compiled_path)
    if not path.exists():
      issues.append(
        f"Compiled file missing at {compiled_path}."
      )
    else:
      js = path.read_text(encoding="utf-8")
      if not js.strip():
        issues.append("Compiled file is empty.")
      elif not re.search(r"export\s+default\b|export\s*\{[^}]*\bas\s+default\b", js):
        issues.append(
          "Compiled JS has no default export — "
          "the component won't mount."
        )
      # Quick syntax check via node --check if available. Uses
      # asyncio.create_subprocess_exec so the FastAPI event loop
      # stays free while node runs (a blocking subprocess.run here
      # would stall every other request for up to the 5s timeout).
      proc = None
      try:
        proc = await asyncio.create_subprocess_exec(
          "node", "--check", str(path),
          stdout=asyncio.subprocess.PIPE,
          stderr=asyncio.subprocess.PIPE,
        )
        try:
          stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=5,
          )
        except asyncio.TimeoutError:
          # Kill the orphan node process; otherwise it lingers
          # holding the pipe open until the OS reaps it.
          try:
            proc.kill()
            await proc.wait()
          except ProcessLookupError:
            pass
          issues.append("Syntax check timed out.")
        else:
          if proc.returncode != 0:
            stderr = stderr_b.decode("utf-8", errors="replace")
            issues.append(
              f"JS syntax error: {stderr.strip()}"
            )
      except FileNotFoundError:
        pass  # node not available — skip this check

  return {
    "app_id": app_id,
    "name": app_name,
    "valid": len(issues) == 0,
    "issues": issues,
  }
