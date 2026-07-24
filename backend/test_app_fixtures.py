"""Focused helpers for tests whose subject is not app source application."""

import json
import re
from pathlib import Path

from app.config import get_settings


DEFAULT_JSX = "export default function App() { return <div>test</div> }\n"


def write_local_source(
  root: str | Path,
  *,
  name: str,
  description: str = "Test app",
  jsx_source: str = DEFAULT_JSX,
  offline_capable: bool = False,
  capabilities: dict | None = None,
  manifest_extra: dict | None = None,
) -> Path:
  """Write the minimum complete local app contract without applying it."""
  root = Path(root)
  root.mkdir(parents=True, exist_ok=True)
  (root / "index.jsx").write_text(jsx_source, encoding="utf-8")
  manifest_id = re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-") or "app"
  manifest = {
    "id": manifest_id,
    "name": name,
    "version": "0.1.0",
    "description": description or "Test app",
    "entry": "index.jsx",
    "offline_capable": offline_capable,
    "permissions": {},
    "capabilities": capabilities or {},
    "source_files": [],
  }
  manifest.update(manifest_extra or {})
  (root / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  return root


def create_local_app(
  client,
  headers: dict,
  *,
  name: str = "Test App",
  description: str = "Test app",
  jsx_source: str = DEFAULT_JSX,
  source_dir: str | Path | None = None,
  offline_capable: bool = False,
  capabilities: dict | None = None,
  chat_id: str | None = None,
  cross_app_access: str = "none",
  share_with_apps: str = "none",
  manifest_extra: dict | None = None,
) -> dict:
  """Create through the production explicit-apply contract and return AppOut."""
  if source_dir is None:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"
    if base.isdigit():
      base = f"app-{base}"
    apps_root = Path(get_settings().data_dir) / "apps"
    slug = base
    suffix = 2
    while (apps_root / slug).exists():
      slug = f"{base}-{suffix}"
      suffix += 1
    root = apps_root / slug
  else:
    root = Path(source_dir)
    slug = root.name
  write_local_source(
    root,
    name=name,
    description=description,
    jsx_source=jsx_source,
    offline_capable=offline_capable,
    capabilities=capabilities,
    manifest_extra=manifest_extra,
  )
  response = client.post(
    "/api/apps/apply",
    headers=headers,
    json={"source_dir": str(root), "chat_id": chat_id},
  )
  assert response.status_code == 200, response.text
  app = response.json()["app"]
  metadata = {}
  if not description:
    metadata["description"] = ""
  if cross_app_access != "none":
    metadata["cross_app_access"] = cross_app_access
  if share_with_apps != "none":
    metadata["share_with_apps"] = share_with_apps
  if metadata:
    patched = client.patch(
      f"/api/apps/{app['id']}", headers=headers, json=metadata,
    )
    assert patched.status_code == 200, patched.text
    app = patched.json()
  return app
