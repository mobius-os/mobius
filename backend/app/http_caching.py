"""Range/206 hardening for revalidating FileResponses.

A `FileResponse` honors a request `Range` header and answers a 206 partial
slice. That is correct for immutable (content-hashed) media — a browser
seeking inside an audio/video file wants it, and an immutable entry is never
revalidated, so the slice is always re-fetched against a fresh URL.

It is a CACHE-POISONING trap for a REVALIDATING response (one carrying
`Cache-Control: no-cache` / `must-revalidate` + a stable ETag). Chromium
stores the 206 slice keyed by the request URL, later revalidates it with
`If-None-Match`, receives a 304, and then serves the stored SLICE as a
status-200 full body. A `Range: bytes=0-0` probe thus turns the resource into
a one-byte response for every later open — the 2026-06-12 black-screen outage,
where CubeRun's index.html became the single character '<', and the same
signature on the `/api/apps/{id}/module` route (a one-byte module = a black
mini-app until the next app update) and on `sw.js` (a one-byte service worker).

The fix is structural and RFC 9110-compliant: a server MAY ignore `Range`.
Strip `range` + `if-range` from the request scope BEFORE constructing the
`FileResponse`, so the response is an unconditional streamed full-body 200.
`FileResponse` is kept (not a `read_bytes()` Response) so the full body
streams off disk instead of loading whole files into memory.

HEAD is the other half of the defense: a 405 on these routes pushes
well-meaning client probes ("are the files installed?") into a
`Range: bytes=0-0` GET fallback, which is exactly the poisoning trigger.
Register HEAD alongside GET so probes get header-only answers instead.
"""

from fastapi import Request


def strip_range(request: Request) -> None:
  """Remove `range` + `if-range` from the request scope in place.

  After this, a `FileResponse` built from `request` serves a full-body 200
  regardless of any client `Range` header. Call it for every REVALIDATING
  (non-immutable) FileResponse; leave immutable/hashed responses alone so
  media seeking still works there (those are never revalidated, so the
  304-served-as-200-slice trap cannot fire).
  """
  scope_headers = request.scope.get("headers")
  if not scope_headers:
    return
  request.scope["headers"] = [
    (name, value)
    for (name, value) in scope_headers
    if name not in (b"range", b"if-range")
  ]
