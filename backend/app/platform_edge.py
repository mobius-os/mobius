"""Public-edge compatibility checks for owner-triggered platform updates.

The bundled reverse proxy can replace the backend's app-frame CSP. The browser
is therefore the only component that can observe the policy which will govern
the next served frame. Settings reports that exact response header with Apply;
this module validates the small, explicit contract before any source changes.
"""

from __future__ import annotations


APP_FRAME_EDGE_PROBE_PATH = "/api/apps/0/frame"


def csp_allows_blob_modules(value: str | None) -> bool:
  """Whether every enforced policy permits a ``blob:`` module script.

  A missing source-list directive imposes no script restriction, so a direct
  or policy-free deployment remains valid. When a policy does constrain
  scripts, ``script-src-elem`` takes precedence over ``script-src``, which in
  turn falls back to ``default-src``. Multiple CSP headers are enforced as an
  intersection; ASGI/browser header APIs expose them as a comma-joined value,
  so every policy must allow the module.
  """
  if value is None or not value.strip():
    return True

  for policy in value.split(","):
    directives: dict[str, tuple[str, ...]] = {}
    for raw_directive in policy.split(";"):
      parts = raw_directive.strip().lower().split()
      if parts and parts[0] not in directives:
        directives[parts[0]] = tuple(parts[1:])
    sources = None
    for name in ("script-src-elem", "script-src", "default-src"):
      if name in directives:
        sources = directives[name]
        break
    if sources is not None and "blob:" not in sources:
      return False
  return True


def app_frame_edge_preflight_passes(
  *,
  path: str,
  content_security_policy: str | None,
) -> bool:
  """Validate evidence from the one public path covered by the frame policy."""
  return (
    path == APP_FRAME_EDGE_PROBE_PATH
    and csp_allows_blob_modules(content_security_policy)
  )
