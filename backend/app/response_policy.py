"""Origin-owned browser response policies shared by every deployment path."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse


def absolute_csp_origin(value: str) -> str | None:
  """Return one origin-only HTTP(S) CSP source or fail closed."""
  try:
    parsed = urlparse(value.strip())
    port = parsed.port
  except (AttributeError, TypeError, ValueError):
    return None
  if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path not in ("", "/")
    or parsed.params
    or parsed.query
    or parsed.fragment
  ):
    return None
  hostname = parsed.hostname
  if ":" in hostname:
    try:
      ipaddress.IPv6Address(hostname)
    except ValueError:
      return None
    host = f"[{hostname}]"
  else:
    if not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
      return None
    host = hostname
  authority = f"{host}:{port}" if port is not None else host
  return f"{parsed.scheme}://{authority}"


def _validated_frontend_origin(frontend_origin: str) -> str:
  origin = absolute_csp_origin(frontend_origin)
  if origin is None:
    raise RuntimeError("FRONTEND_ORIGIN must be one absolute HTTP(S) origin")
  return origin


def app_frame_csp(
  frontend_origin: str,
  gateway_origin: str = "",
  api_origin: str = "",
  delivery_origin: str = "",
) -> str:
  """Complete policy for the opaque mini-app document."""
  origin = _validated_frontend_origin(frontend_origin)
  resource_sources = [origin]
  delivery = absolute_csp_origin(delivery_origin)
  if delivery is not None and delivery != origin:
    resource_sources.append(delivery)
  resource_source = " ".join(resource_sources)
  frame_sources = list(resource_sources)
  connect_sources = list(resource_sources)
  api = absolute_csp_origin(api_origin)
  if api is not None and api != origin:
    connect_sources.append(api)
  gateway = absolute_csp_origin(gateway_origin)
  if gateway is not None and gateway != origin:
    frame_sources.append(gateway)
  return (
    "sandbox allow-scripts allow-forms allow-popups "
    "allow-popups-to-escape-sandbox "
    "allow-top-navigation-by-user-activation; "
    f"default-src {resource_source}; "
    # `'wasm-unsafe-eval'` is the narrow modern source for WebAssembly, but older
    # WebKit-based installed apps (older iOS Safari / installed-PWA engines)
    # ignore it and still gate WebAssembly compilation on `'unsafe-eval'`.
    # On-device Pocket TTS runs as Wasm inside these opaque, sandboxed mini-app
    # frames, so both sources are required for it to start on those browsers.
    # The permission stays confined to the isolated app-frame policy; the shell
    # and other documents keep only the narrow modern source.
    f"script-src {resource_source} 'unsafe-inline' 'wasm-unsafe-eval' 'unsafe-eval' "
    "blob: https://esm.sh; "
    f"style-src {resource_source} 'unsafe-inline' https://fonts.googleapis.com; "
    f"font-src {resource_source} https://fonts.gstatic.com https://cdn.openai.com; "
    f"connect-src {' '.join(connect_sources)}; "
    f"img-src {resource_source} data: blob:; "
    f"frame-src {' '.join(frame_sources)}; "
    "frame-ancestors 'self'"
  )


def shell_csp(gateway_origin: str = "") -> str:
  """Policy for ordinary shell/API documents, independent of proxy syntax.

  ``'wasm-unsafe-eval'`` permits WebAssembly and nothing else, rather than the
  general ``'unsafe-eval'``. On-device Pocket TTS runs as Wasm in a same-origin
  shell-owned worker, which cannot compile without it. ``worker-src 'self'``
  keeps that worker restricted to the real same-origin asset rather than a
  generated or third-party worker.
  """
  frame_sources = ["'self'"]
  gateway = absolute_csp_origin(gateway_origin)
  if gateway is not None:
    frame_sources.append(gateway)
  return (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' https://esm.sh; "
    "worker-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com https://cdn.openai.com; "
    "connect-src 'self'; "
    "img-src 'self' data: blob:; "
    f"frame-src {' '.join(frame_sources)}; "
    "frame-ancestors 'self'"
  )


CHAT_EMBED_CSP = (
  "default-src 'self'; "
  "script-src 'self' 'unsafe-inline' https://esm.sh; "
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
  "font-src 'self' https://fonts.gstatic.com https://cdn.openai.com; "
  "connect-src 'self'; "
  "img-src 'self' data: blob:; "
  "frame-src 'self'"
)


def static_embed_csp(
  frontend_origin: str, *additional_origins: str,
) -> str:
  origins = [_validated_frontend_origin(frontend_origin)]
  for candidate in additional_origins:
    origin = _validated_frontend_origin(candidate)
    if origin not in origins:
      origins.append(origin)
  source = " ".join(origins)
  return (
    "sandbox allow-scripts allow-forms allow-pointer-lock; "
    f"default-src {source}; "
    f"script-src {source} 'unsafe-inline'; "
    f"style-src {source} 'unsafe-inline'; "
    f"font-src {source} data:; "
    f"connect-src {source}; "
    f"img-src {source} data: blob:; "
    f"media-src {source} blob:; "
    f"worker-src {source} blob:"
  )


PUBLISHED_SITE_CSP = (
  "sandbox allow-scripts allow-forms allow-popups; "
  "object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
)
