"""Serve published mini-app site snapshots at /sites/<token>/ (feature 136).

A Web Studio project publishes its built static site (build/site/) to a
snapshot under DATA_DIR/published/<token>/; this serves it at a stable,
unguessable token URL — the owner's shareable "live preview". The token is
per-project (stable across re-publishes, stored in the project's build/ dir).

Security: <token> is validated to a hex slug and the resolved file path is
confined to the token dir (traversal-safe). Served same-origin with nosniff.
Because a page on this origin can read the shell's localStorage, only the
owner's OWN built static sites are meant to be published (the publish action is
owner/app-gated and the content is the owner's own build output).
"""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get_settings

published_router = APIRouter(tags=["published"])

_TOKEN_RE = re.compile(r"^[a-f0-9]{16,64}$")


def _published_root() -> Path:
  return Path(get_settings().data_dir) / "published"


def _serve(token: str, path: str):
  if not _TOKEN_RE.match(token or ""):
    raise HTTPException(status_code=404, detail="Not found.")
  base = (_published_root() / token).resolve()
  if not base.is_dir():
    raise HTTPException(status_code=404, detail="Not found.")
  target = (base / (path or "index.html")).resolve()
  # Confine to the token dir — a resolved path that escapes it is a 404.
  if base != target and base not in target.parents:
    raise HTTPException(status_code=404, detail="Not found.")
  if target.is_dir():
    target = target / "index.html"
  if not target.is_file():
    # SPA-style fallback: serve the site's own index.html for client routes.
    idx = base / "index.html"
    if not idx.is_file():
      raise HTTPException(status_code=404, detail="Not found.")
    target = idx
  resp = FileResponse(str(target))
  resp.headers["X-Content-Type-Options"] = "nosniff"
  resp.headers["Cache-Control"] = "no-cache"
  return resp


@published_router.get("/sites/{token}/{path:path}", include_in_schema=False)
def serve_published(token: str, path: str = ""):
  return _serve(token, path)


@published_router.get("/sites/{token}", include_in_schema=False)
def serve_published_root(token: str):
  return _serve(token, "")


# The routes/__init__ _load() scaffold returns `mod.router`; expose it.
router = published_router
