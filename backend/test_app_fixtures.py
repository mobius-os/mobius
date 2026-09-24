"""Focused helpers for tests whose subject is not app source application."""

import json
import re
import subprocess
from pathlib import Path

from app import app_git
from app.config import get_settings


DEFAULT_JSX = "export default function App() { return <div>test</div> }\n"

_PUBLIC_HOST_SLOT = re.compile(
  r'<script type="application/json" id="mobius-public-host">(.*?)</script>',
  re.S,
)


def public_host_config(html: str) -> dict:
  """The configuration an anonymous host page hands its static script."""
  match = _PUBLIC_HOST_SLOT.search(html)
  assert match, html[:1000]
  return json.loads(match.group(1))


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


def write_git_package(root: str | Path, files: dict[str, str | bytes]) -> Path:
  """Write and publish one deterministic local Git package for route tests."""
  root = Path(root)
  work = root / "work"
  bare = root / "origin.git"
  if not (work / ".git").is_dir():
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
      ["git", "init", "-q", "-b", "main", str(work)], check=True,
      env=app_git._git_env(work),
    )
  tracked = subprocess.run(
    ["git", "-C", str(work), "ls-files"], capture_output=True, text=True,
    check=True, env=app_git._git_env(work),
  ).stdout.splitlines()
  for relative in set(tracked) - set(files):
    (work / relative).unlink(missing_ok=True)
  for relative, body in files.items():
    path = work / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if isinstance(body, bytes) else body.encode())
  subprocess.run(
    ["git", "-C", str(work), "add", "-A", "."], check=True,
    env=app_git._git_env(work),
  )
  changed = subprocess.run(
    ["git", "-C", str(work), "diff", "--cached", "--quiet"],
    env=app_git._git_env(work),
  ).returncode != 0
  if changed:
    subprocess.run(
      [
        "git", "-c", "user.name=Test", "-c",
        "user.email=test@example.invalid", "-C", str(work),
        "commit", "-q", "-m", "package",
      ],
      check=True, env=app_git._git_env(work),
    )
  if not bare.exists():
    subprocess.run(
      ["git", "clone", "-q", "--bare", str(work), str(bare)],
      check=True, env=app_git._git_env(work),
    )
  elif changed:
    subprocess.run(
      ["git", "-C", str(work), "push", "-q", str(bare), "main"],
      check=True, env=app_git._git_env(work),
    )
  return bare


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
