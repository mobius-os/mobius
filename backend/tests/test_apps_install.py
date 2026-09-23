"""POST /api/apps/install — atomic install + update + rollback.

We exercise the endpoint against a mocked httpx.AsyncClient (no real
network) so tests run inside the existing pytest container without
external connectivity. The mocked layer returns canned (status, body)
tuples per URL so we can drive the install paths deterministically
and force failure modes.
"""

import asyncio
import errno
from datetime import UTC, datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException

from app import app_git, models
from app.config import get_settings
from test_app_fixtures import create_local_app


@pytest.fixture(autouse=True)
def _bypass_cron_scaffold():
  """Force every test through the no-scaffold warning branch so the
  install endpoint doesn't shell out to init-cron-scaffold.sh (which
  hardcodes `/data/apps/...` and fails under DATA_DIR=/tmp/testdata)."""
  with patch("app.install.CRON_SCAFFOLD", Path("/nonexistent/scaffold.sh")):
    yield


@pytest.fixture(autouse=True)
def _stub_resolver_run_chat():
  """Resolver-chat endpoint tests never want a real agent turn, so stub
  run_chat to a no-op. A test that needs the real spawn behavior patches over
  this locally."""
  async def _noop(*args, **kwargs):
    return None
  with patch("app.chat.run_chat", new=_noop):
    yield


@pytest.fixture
def bypass_url_validation(tmp_path):
  """Turn mocked HTTP packages into real Git-backed test packages.

  Production accepts no HTTP-only package model. Most lifecycle tests use a
  tiny mocked transport because the behavior under test is compilation,
  metadata or rollback rather than remote Git. This fixture builds the fetched
  candidate into the same local Git repository shape production would clone,
  while still bypassing DNS for the fake hosts.
  """
  # Return (url, host_header, sni_host) — the validate-and-pin shape — WITHOUT
  # pinning, so the mocked httpx still sees the ORIGINAL url (the response map
  # is keyed by it).
  from app import install

  state = {"commit": None, "manifest_url": None}
  original_derive = install._derive_repo_ref
  original_snapshot = install._load_git_package_snapshot
  original_clone = app_git.clone_upstream
  original_fetch = app_git.fetch_upstream
  original_fetch_ref = app_git.fetch_origin_ref
  original_set_origin = app_git.set_origin_url
  remote_work = tmp_path / "mock-store-work"
  remote_bare = tmp_path / "mock-store.git"

  async def snapshot(
    *, repo_url, ref,
  ):
    if not repo_url.startswith("https://github.com/"):
      return await original_snapshot(
        repo_url=repo_url,
        ref=ref,
      )
    manifest_url = state["manifest_url"]
    if not isinstance(manifest_url, str):
      raise RuntimeError("test Git snapshot requested without a manifest URL")
    async with install.httpx.AsyncClient(
      timeout=install._HTTP_TIMEOUT,
      follow_redirects=False,
    ) as cli:
      manifest, raw_base = await install._fetch_and_validate_manifest(
        cli,
        manifest_url=manifest_url,
        manifest=None,
        raw_base=None,
      )
      tree = {
        "mobius.json": json.dumps(manifest).encode(),
        manifest["entry"]: await install._http_get(
          cli, raw_base + manifest["entry"], install._ENTRY_MAX_BYTES,
        ),
      }
      for rel in manifest.get("source_files") or []:
        tree[rel] = await install._http_get(
          cli, raw_base + rel, install._ENTRY_MAX_BYTES,
        )
      schedule = manifest.get("schedule")
      executable = set()
      if isinstance(schedule, dict) and schedule.get("job"):
        job = schedule["job"]
        tree[job] = await install._http_get(
          cli, raw_base + job, install._ENTRY_MAX_BYTES,
        )
        executable.add(job)
      for _destination, relative in install.static_asset_entries(
        manifest.get("static_assets") or {},
      ).items():
        tree[relative] = await install._http_get(
          cli, raw_base + relative, install._STATIC_ASSET_MAX_BYTES,
        )
      for _destination, declared in (manifest.get("storage_seeds") or {}).items():
        if not install._seed_value_is_inline(declared):
          tree[declared] = await install._http_get(
            cli, raw_base + declared, install._SEED_MAX_BYTES,
          )
      if manifest.get("icon"):
        try:
          tree[manifest["icon"]] = await install._http_get(
            cli, raw_base + manifest["icon"], install._ICON_MAX_BYTES,
          )
        except Exception:
          pass
    if not remote_work.exists():
      subprocess.run(
        ["git", "init", "-q", "-b", "main", str(remote_work)], check=True,
      )
    for child in remote_work.iterdir():
      if child.name == ".git":
        continue
      if child.is_dir():
        shutil.rmtree(child)
      else:
        child.unlink()
    for rel, content in tree.items():
      target = remote_work / rel
      target.parent.mkdir(parents=True, exist_ok=True)
      target.write_bytes(content)
      if rel in executable:
        target.chmod(0o755)
    status = app_git._run(
      remote_work, "status", "--porcelain", read_only=True,
    ).stdout.strip()
    if status:
      commit = _fixture_commit(
        remote_work,
        f"install v{manifest.get('version', 'unknown')} from {raw_base}",
      )
    else:
      commit = app_git._run(remote_work, "rev-parse", "HEAD").stdout.strip()
    if not remote_bare.exists():
      subprocess.run(
        ["git", "clone", "-q", "--bare", str(remote_work), str(remote_bare)],
        check=True, env=app_git._git_env(remote_work),
      )
    else:
      subprocess.run(
        ["git", "-C", str(remote_work), "push", "-q", "--force",
         str(remote_bare), "main"],
        check=True, env=app_git._git_env(remote_work),
      )
    state["commit"] = commit
    return install.GitPackageSnapshot(commit=commit, tree=tree)

  def clone(source_dir, repo_url, ref, **kwargs):
    if not repo_url.startswith("https://github.com/"):
      return original_clone(source_dir, repo_url, ref, **kwargs)
    sha = original_clone(
      source_dir, remote_bare.as_uri(), state["commit"], **kwargs,
    )
    app_git._run(Path(source_dir), "remote", "set-url", "origin", repo_url)
    app_git._run(
      Path(source_dir), "config",
      f"url.{remote_bare.as_uri()}.insteadOf", repo_url,
    )
    return sha

  def set_origin(source_dir, url):
    original_set_origin(source_dir, url)
    app_git._run(
      Path(source_dir), "config", "--add",
      f"url.{remote_bare.as_uri()}.insteadOf", url,
    )

  def fetch(source_dir, ref, **kwargs):
    origin = app_git.origin_url(source_dir)
    if not remote_bare.exists() or state["commit"] != ref:
      return original_fetch(source_dir, ref, **kwargs)
    app_git._run(
      Path(source_dir), "remote", "set-url", "origin", remote_bare.as_uri(),
    )
    try:
      return original_fetch(source_dir, state["commit"], **kwargs)
    finally:
      app_git._run(
        Path(source_dir), "remote", "set-url", "origin", origin,
      )

  def fetch_ref(source_dir, ref, **kwargs):
    origin = app_git.origin_url(source_dir)
    if not remote_bare.exists() or state["commit"] != ref:
      return original_fetch_ref(source_dir, ref, **kwargs)
    app_git._run(
      Path(source_dir), "remote", "set-url", "origin", remote_bare.as_uri(),
    )
    try:
      return original_fetch_ref(source_dir, state["commit"], **kwargs)
    finally:
      app_git._run(
        Path(source_dir), "remote", "set-url", "origin", origin,
      )

  def derive(url):
    state["manifest_url"] = url
    parsed = original_derive(url)
    return parsed or ("https://github.com/test-fixtures/mock-store.git", "main")

  with (
    patch("app.install._validate_url_safe",
          lambda url: (url, urlparse(url).netloc, urlparse(url).hostname)),
    patch(
      "app.install._derive_repo_ref",
      side_effect=derive,
    ),
    patch("app.install._load_git_package_snapshot", side_effect=snapshot),
    patch("app.app_git.clone_upstream", side_effect=clone),
    patch("app.app_git.fetch_upstream", side_effect=fetch),
    patch("app.app_git.fetch_origin_ref", side_effect=fetch_ref),
    patch("app.app_git.set_origin_url", side_effect=set_origin),
  ):
    yield


JSX = "export default function App() { return <div>ok</div> }"
PROMPT = "# default prompt\nDo the work.\n"


def _finish_materialized_rebase(repo: Path) -> None:
  conflict_paths = app_git._run(
    repo, "diff", "--name-only", "--diff-filter=U", check=False,
  ).stdout.splitlines()
  resolutions = {
    path: (repo / path).read_bytes()
    for path in conflict_paths
    if (repo / path).is_file()
  }
  continued = None
  for _ in range(20):
    app_git._run(repo, "add", "-A")
    with patch.dict(os.environ, {"GIT_EDITOR": "true"}):
      continued = app_git._run(repo, "rebase", "--continue", check=False)
    if continued.returncode == 0:
      break
    remaining = app_git._run(
      repo, "diff", "--name-only", "--diff-filter=U", check=False,
    ).stdout.splitlines()
    if not app_git.rebase_in_progress(repo) or any(
      path not in resolutions for path in remaining
    ):
      break
    for path in remaining:
      (repo / path).write_bytes(resolutions[path])
  assert continued is not None and continued.returncode == 0, (
    continued.stderr if continued is not None else "rebase did not run"
  )
  assert not app_git.rebase_in_progress(repo)


def _make_response(status: int, body: bytes, headers: dict | None = None):
  r = MagicMock()
  r.status_code = status
  r.content = body
  r.text = body.decode("utf-8", errors="replace")
  r.headers = headers or {}
  r.json = lambda: json.loads(body.decode("utf-8"))
  return r


class _StreamCtx:
  """Async-context-manager wrapping a single response, mirroring
  `httpx.AsyncClient.stream(...)`. `aiter_bytes()` yields the whole
  body as one chunk for happy-path tests; pass `chunks=` for tests
  that need to verify mid-stream abort behavior."""

  def __init__(self, status, body, headers=None, chunks=None):
    self._resp = _make_response(status, body, headers)
    self._chunks = chunks if chunks is not None else [body]

  async def __aenter__(self):
    return self

  async def __aexit__(self, *exc):
    return False

  def __getattr__(self, name):
    return getattr(self._resp, name)

  async def aiter_bytes(self):
    for chunk in self._chunks:
      yield chunk


def _fake_async_client(responses: dict):
  """`responses` maps URL → (status, bytes) or (status, bytes, headers).
  Returns a context-manager factory matching `httpx.AsyncClient(...)`
  usage. Exposes `.stream("GET", url)` since the install module
  switched from `.get(url)` + `r.content` to streamed reads."""

  class _FakeClient:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc):
      return False

    def stream(self, method, url, **kwargs):
      if url not in responses:
        return _StreamCtx(404, b"")
      tup = responses[url]
      if len(tup) == 2:
        status, body = tup
        # Historical install fixtures used empty bytes as an inert stand-in
        # for the otherwise-unexamined scheduled job. Accepted jobs now own an
        # explicit interpreter, so keep those unrelated fixtures valid.
        if status == 200 and body == b"" and url.endswith("/fetch.sh"):
          body = b"#!/bin/sh\n"
        return _StreamCtx(status, body)
      status, body, headers = tup
      return _StreamCtx(status, body, headers=headers)

  return lambda *a, **kw: _FakeClient()


def _png_bytes() -> bytes:
  """Tiny valid PNG so the PIL pipeline accepts it."""
  from PIL import Image
  buf = io.BytesIO()
  Image.new("RGB", (16, 16), (139, 108, 247)).save(buf, format="PNG")
  return buf.getvalue()


MANIFEST_NEWS = {
  "id": "test-news",
  "name": "Test News",
  "version": "1.0.0",
  "description": "Test app",
  "entry": "index.jsx",
  "icon": "icon.png",
  "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  "storage_seeds": {
    "prompt.md": "prompt.md",
    "schedule.json": {"hour": 10, "minute": 0},
  },
  "schedule": {
    "default": "0 10 * * *",
    "user_configurable": True,
    "job": "fetch.sh",
  },
  "runtime": {"imports": ["react"], "esm_deps": []},
}


def test_install_rejects_a_scheduled_job_without_runtime_declaration(
  client, auth, bypass_url_validation,
):
  base = "https://x.test/no-job-shebang/"
  manifest = {**MANIFEST_NEWS, "id": "no-job-shebang"}
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b"echo ambiguous runtime\n"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })

  assert response.status_code == 400
  assert response.json()["detail"] == "Schedule job is missing a shebang."


def test_validate_url_safe_blocks_ipv6_embedded_ipv4():
  # SSRF: an IPv6 that embeds a blocked IPv4 must be rejected — IPv4-compatible
  # (::127.0.0.1), IPv4-mapped (::ffff:169.254.169.254), and well-known NAT64
  # (64:ff9b::a9fe:a9fe == 169.254.169.254) all reach an internal v4 host.
  import socket as _socket
  from app.install import _validate_url_safe

  def _gai(ip_str):
    return [(_socket.AF_INET6, _socket.SOCK_STREAM, 0, "", (ip_str, 0, 0, 0))]

  for ip_str in ("::127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe"):
    with patch("app.net_utils.socket.getaddrinfo", return_value=_gai(ip_str)):
      with pytest.raises(Exception):  # HTTPException(400)
        _validate_url_safe("https://evil.example/mobius.json")

  # A genuine public IPv6 is allowed through AND pins to that exact IP, with the
  # authority preserved for the Host header and the bare hostname for TLS SNI.
  with patch("app.net_utils.socket.getaddrinfo",
             return_value=_gai("2606:4700:4700::1111")):
    pinned, host_header, sni = _validate_url_safe(
      "https://cloudflare.example/mobius.json")
    assert sni == "cloudflare.example"
    assert host_header == "cloudflare.example"
    assert "[2606:4700:4700::1111]" in pinned
    # A non-default port survives in the Host header (RFC 7230 §5.4).
    _, host_header2, _ = _validate_url_safe("https://cloudflare.example:8443/m")
    assert host_header2 == "cloudflare.example:8443"

  # Credentialed manifest URLs are rejected outright (before any resolution).
  with pytest.raises(Exception):
    _validate_url_safe("https://user:pass@cloudflare.example/m")


def test_install_fresh_app_writes_everything(client, auth, tmp_path, bypass_url_validation):
  """Happy path: install creates DB row, compiles JSX, populates
  source_dir, seeds storage, processes icon, returns mode=install."""
  base = "https://packages.test/x/app-test-news/main/"
  manifest = {
    **MANIFEST_NEWS,
    "theme_color": "#223344",
    "background_color": "#101820",
    "display": "fullscreen",
    "static_assets": {
      "index.html": "build/index.html",
      "static/js/main.js": "build/static/js/main.js",
    },
  }
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b"#!/bin/bash\necho hi\n"),
    base + "build/index.html": (200, b"<!doctype html><title>Static app</title>"),
    base + "build/static/js/main.js": (200, b"console.log('static app')"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "install"
  assert payload["version"] == "1.0.0"
  assert payload["theme_color"] == "#223344"
  assert payload["background_color"] == "#101820"
  assert payload["display"] == "fullscreen"
  assert payload["slug"] == "test-news"
  app_id = payload["id"]

  data_dir = Path(get_settings().data_dir)
  # The installer materializes the accepted source tree.
  jsx_file = data_dir / "apps" / "test-news" / "index.jsx"
  assert jsx_file.read_text() == JSX
  assert (
    data_dir / "apps" / "test-news" / "static" / "index.html"
  ).read_text() == "<!doctype html><title>Static app</title>"
  assert (
    data_dir / "apps" / "test-news" / "static" / "static" / "js" / "main.js"
  ).read_text() == "console.log('static app')"
  # storage seeds live at /data/apps/<id>/ (storage API is id-keyed)
  assert (data_dir / "apps" / str(app_id) / "prompt.md").read_text() == PROMPT
  sched = json.loads((data_dir / "apps" / str(app_id) / "schedule.json").read_text())
  assert sched == {"hour": 10, "minute": 0}
  # warning expected: scaffold script isn't on PATH in the test image
  assert any("cron" in w for w in payload["warnings"])
  # A clean install (no pre-existing app owns "test-news") must NOT emit
  # a slug_collision telemetry event.
  assert not [e for e in _read_activity() if e["ev"] == "slug_collision"]

  listed = client.get("/api/apps/", headers=auth).json()
  row = next(a for a in listed if a["id"] == app_id)
  assert row["theme_color"] == "#223344"
  assert row["background_color"] == "#101820"
  assert row["display"] == "fullscreen"


def test_fresh_install_never_deletes_a_rowless_existing_source_folder(
  client, auth, db, bypass_url_validation,
):
  """A refused clone does not claim cleanup ownership of an owner folder."""
  manifest = {
    "id": "owner-folder",
    "name": "Owner folder",
    "version": "1.0.0",
    "description": "Existing source folder fixture",
    "entry": "index.jsx",
    "permissions": {},
  }
  base = "https://x.test/owner-folder/"
  source = Path(get_settings().data_dir) / "apps" / manifest["id"]
  source.mkdir(parents=True)
  keeper = source / "owner-draft.txt"
  keeper.write_text("keep me\n", encoding="utf-8")

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(_check_responses(base, manifest, JSX)),
  ):
    response = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "git_install_unavailable"
  assert keeper.read_text(encoding="utf-8") == "keep me\n"
  assert db.query(models.App).filter_by(slug=manifest["id"]).first() is None


def test_install_fresh_service_app_syncs_aliases_during_activation(
  client, auth, db, bypass_url_validation,
):
  base = "https://packages.test/x/app-svc-alias/main/"
  manifest = {
    "id": "svc-alias",
    "name": "Svc Alias",
    "version": "1.0.0",
    "description": "Service app with a transition alias",
    "entry": "index.jsx",
    "icon": "icon.png",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
    "service": {
      "id": "svc-alias",
      "entry": "service.py",
      "access": "public",
      "aliases": ["svc-legacy"],
    },
    "source_files": ["service.py"],
    "runtime": {"imports": ["react"], "esm_deps": []},
  }
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "service.py": (200, b"def handle(req):\n    return {}\n"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })

  assert response.status_code == 201, response.text
  app_id = response.json()["id"]
  aliases = (
    db.query(models.AppServiceAlias)
    .filter(models.AppServiceAlias.app_id == app_id)
    .all()
  )
  assert [alias.service_id for alias in aliases] == ["svc-legacy"]


def test_install_static_site_assets_route_css_fonts_and_chunks(
  client, auth, bypass_url_validation,
):
  """CubeRun-style static bundles keep HTML, CSS, chunks, and media together.

  The important regression here is path shape: CSS is served from
  /app-assets/<slug>/static/css/..., so relative font URLs must resolve to
  sibling static/media assets and missing app assets must stay a 404, never
  the Mobius shell HTML.
  """
  base = "https://packages.test/x/cuberun-lite/main/"
  manifest = {
    "id": "cuberun-lite",
    "name": "CubeRun Lite",
    "version": "1.0.0",
    "description": "Static WebGL-style app",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
    "static_assets": {
      "index.html": "build/index.html",
      "static/css/main.css": "build/static/css/main.css",
      "static/js/main.js": "build/static/js/main.js",
      "static/media/ship.gltf": "build/static/media/ship.gltf",
      "static/media/commando.ttf": "build/static/media/commando.ttf",
    },
  }
  css = (
    "@font-face{font-family:Commando;"
    "src:url(../media/commando.ttf) format('truetype')}"
  )
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (
      200,
      (
        "export default function App({ appId }) {"
        "return <iframe title=\"game\" src={`/app-assets/by-id/${appId}/index.html`} />"
        "}"
      ).encode(),
    ),
    base + "build/index.html": (
      200,
      b"<!doctype html><link rel='stylesheet' href='./static/css/main.css'>"
      b"<script src='./static/js/main.js'></script>",
    ),
    base + "build/static/css/main.css": (200, css.encode()),
    base + "build/static/js/main.js": (200, b"console.log('game')"),
    base + "build/static/media/ship.gltf": (200, b'{"asset":{"version":"2.0"}}'),
    base + "build/static/media/commando.ttf": (200, b"fake-font"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]

  html = client.get(f"/app-assets/by-id/{app_id}/index.html")
  assert html.status_code == 200
  assert "static/css/main.css" in html.text
  assert "text/html" in html.headers["content-type"]

  css_res = client.get("/app-assets/cuberun-lite/static/css/main.css")
  assert css_res.status_code == 200
  assert "../media/commando.ttf" in css_res.text
  assert "text/css" in css_res.headers["content-type"]

  font = client.get("/app-assets/cuberun-lite/static/media/commando.ttf")
  assert font.status_code == 200
  assert font.content == b"fake-font"
  assert font.headers["x-content-type-options"] == "nosniff"

  js = client.get("/app-assets/cuberun-lite/static/js/main.js")
  assert js.status_code == 200
  assert "console.log" in js.text

  bad_font_path = client.get(
    "/app-assets/cuberun-lite/static/css/static/media/commando.ttf"
  )
  assert bad_font_path.status_code == 404
  assert "text/html" not in bad_font_path.headers.get("content-type", "")


def test_install_bundles_static_module_from_logical_destination(
  client, auth, bypass_url_validation,
):
  """Logical static destination x is compiled from source path static/x."""
  base = "https://static-module.test/repo/"
  entry = (
    "import label from './static/generated.js';\n"
    "export default function App(){ return <div>{label}</div> }"
  )
  manifest = {
    **MANIFEST_NEWS,
    "id": "static-module",
    "icon": None,
    "storage_seeds": {},
    "schedule": None,
    "source_files": [],
    "static_assets": {"generated.js": "build/generated.js"},
  }
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, entry.encode()),
    base + "build/generated.js": (200, b"export default 'STATIC_MODULE_OK'"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    result = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })

  assert result.status_code == 201, result.text
  bundle = Path(result.json()["compiled_path"])
  assert "STATIC_MODULE_OK" in bundle.read_text()


def test_static_site_asset_update_removes_old_manifest_owned_files(
  client, auth, bypass_url_validation,
):
  """Hashed static bundles are declarative, not append-only.

  When v2 stops declaring a v1 chunk, the old chunk must disappear so
  missing manifest declarations surface as 404s. Files not owned by the
  manifest survive because app/user code may keep its own static files in
  the same directory.
  """
  base = "https://packages.test/x/static-prune/main/"
  manifest_v1 = {
    "id": "static-prune",
    "name": "Static Prune",
    "version": "1.0.0",
    "description": "Static update cleanup",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
    "static_assets": {
      "index.html": "build/index.html",
      "static/js/old.js": "build/static/js/old.js",
    },
  }
  responses_v1 = {
    base + "mobius.json": (200, json.dumps(manifest_v1).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "build/index.html": (200, b"<!doctype html><script src='./static/js/old.js'></script>"),
    base + "build/static/js/old.js": (200, b"console.log('old')"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v1),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  data_dir = Path(get_settings().data_dir)
  source_static = data_dir / "apps" / "static-prune" / "static"
  unrelated = source_static / "user-kept.txt"
  unrelated.write_text("do not prune")

  manifest_v2 = {
    **manifest_v1,
    "version": "2.0.0",
    "static_assets": {
      "index.html": "build/index.html",
      "static/js/new.js": "build/static/js/new.js",
    },
  }
  responses_v2 = {
    base + "mobius.json": (200, json.dumps(manifest_v2).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "build/index.html": (200, b"<!doctype html><script src='./static/js/new.js'></script>"),
    base + "build/static/js/new.js": (200, b"console.log('new')"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"

  assert client.get(f"/app-assets/by-id/{app_id}/static/js/old.js").status_code == 404
  new_js = client.get(f"/app-assets/by-id/{app_id}/static/js/new.js")
  assert new_js.status_code == 200
  assert "console.log('new')" in new_js.text
  assert unrelated.read_text() == "do not prune"


MANIFEST_ONDEMAND = {
  "id": "test-build",
  "name": "Test Build",
  "version": "2.0.0",
  "description": "On-demand build job, no recurring schedule.",
  "entry": "index.jsx",
  "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  # schedule.job ships build.sh but there is NO recurring `default` — the
  # script is invoked only via POST /api/apps/{id}/run-job (a Build click).
  "schedule": {"job": "build.sh"},
  "runtime": {"imports": ["react"], "esm_deps": []},
}


def test_install_on_demand_job_writes_script_without_cron(
    client, auth, tmp_path, bypass_url_validation):
  """A manifest with `schedule.job` but no `schedule.default` ships its job
  script to source_dir (so run-job can find it) WITHOUT registering a cron
  or emitting a cron-pending sentinel/warning. Regression: the write used to
  be gated on `schedule.default`, so an on-demand-only job (the LaTeX app's
  build.sh) was fetched but never landed and run-job 400'd."""
  base = "https://packages.test/x/app-test-build/main/"
  script = b"#!/bin/bash\necho build\n"
  responses = {
    base + "mobius.json": (200, json.dumps(MANIFEST_ONDEMAND).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "build.sh": (200, script),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  payload = r.json()
  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "test-build"
  # the on-demand job script landed in source_dir, executable
  build_sh = src / "build.sh"
  assert build_sh.read_bytes() == script
  assert build_sh.stat().st_mode & 0o111  # executable bit set
  # no recurring schedule → no cron sentinel and no cron warning
  assert not (src / ".cron-pending.json").exists()
  assert not any("cron" in w for w in payload["warnings"])


def _read_activity() -> list[dict]:
  """Parse /data/logs/activity.jsonl into a list of event dicts (empty
  if the file doesn't exist). Mirrors test_activity.py's reader so the
  install tests can assert on the telemetry the installer emits."""
  path = Path(get_settings().data_dir) / "logs" / "activity.jsonl"
  if not path.exists():
    return []
  return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_register_cron_passes_job_name_to_scaffold(tmp_path):
  """The scaffold defaults its job filename to job.sh; the installer must
  pass the manifest's job name (e.g. fetch.sh) as the 3rd scaffold arg so
  the crontab points at the real bundled job, not the empty stub.
  Regression for the bug where every scheduled app fired an empty job.sh.
  The job script itself is written in the transactional source write, not
  here, so register_cron only installs the crontab entry."""
  from app import app_cron

  app_dir = tmp_path / "reflection"
  app_dir.mkdir()
  job_path = app_dir / "fetch.sh"
  fake_scaffold = tmp_path / "init-cron-scaffold.sh"
  fake_scaffold.write_text("#!/bin/bash\n")

  with patch.dict(os.environ, {"MOBIUS_ALLOW_TEST_CRON": "1"}), \
       patch("app.app_cron.subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stderr="")
    app_cron.register_cron(
      "reflection", "0 6 * * *", job_path, 42, scaffold=fake_scaffold,
    )

  # 5th arg is the app id — so a reusable fetch.sh that reads "$1" fires
  # from cron, not just from the run-job endpoint. Regression for news-2:
  # a bundled fetch.sh requires its id and exits 2 without it.
  assert mock_run.call_args.args[0] == [
    str(fake_scaffold), "reflection", "0 6 * * *", "fetch.sh", "42",
  ]
  assert mock_run.call_args.kwargs["env"]["API_BASE_URL"] == (
    get_settings().api_base_url
  )
  assert mock_run.call_args.kwargs["env"]["MOBIUS_APP_JOB_RUNNER"].endswith(
    "scripts/app-job-runner.py"
  )


def test_cron_scaffold_prefers_the_served_checkout():
  from app import app_cron

  assert app_cron.cron_scaffold(app_cron.BAKED_CRON_SCAFFOLD) == (
    Path(app_cron.__file__).resolve().parent.parent
    / "scripts"
    / "init-cron-scaffold.sh"
  )


def test_register_cron_gives_scaffold_the_complete_zone_identity(tmp_path):
  """The scaffold owns durable declaration + live update as one ordered unit."""
  from app import app_cron

  app_dir = tmp_path / "reflection"
  app_dir.mkdir()
  job_path = app_dir / "fetch.sh"
  fake_scaffold = tmp_path / "init-cron-scaffold.sh"
  fake_scaffold.write_text("#!/bin/bash\n")

  with patch.dict(os.environ, {"MOBIUS_ALLOW_TEST_CRON": "1"}), \
       patch("app.app_cron.subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stderr="")
    app_cron.register_cron(
      "reflection", "* * * * *", job_path, 42,
      timezone="Europe/Belgrade", zone_cron="30 2 * * *",
      scaffold=fake_scaffold,
    )

  assert mock_run.call_args.args[0] == [
    str(fake_scaffold), "reflection", "* * * * *", "fetch.sh", "42",
    "Europe/Belgrade", "30 2 * * *",
  ]


@pytest.mark.parametrize("timezone, zone_cron, app_id", [
  ("Europe/Belgrade", None, 42),
  (None, "30 2 * * *", 42),
  ("Not/AZone", "30 2 * * *", 42),
  ("Europe/Belgrade", "30 2 * * 1", 42),
  ("Europe/Belgrade", "30 2 * * *", None),
])
def test_register_cron_rejects_invalid_zone_contract_before_subprocess(
  tmp_path, timezone, zone_cron, app_id,
):
  from app import app_cron

  fake_scaffold = tmp_path / "init-cron-scaffold.sh"
  fake_scaffold.write_text("#!/bin/sh\n")
  with patch.dict(os.environ, {"MOBIUS_ALLOW_TEST_CRON": "1"}), \
       patch("app.app_cron.subprocess.run") as mock_run, \
       pytest.raises(app_cron.HTTPException):
    app_cron.register_cron(
      "memory", "* * * * *", tmp_path / "fetch.sh", app_id,
      timezone=timezone, zone_cron=zone_cron, scaffold=fake_scaffold,
    )
  mock_run.assert_not_called()


def test_register_cron_omits_app_id_when_none(tmp_path):
  """A self-contained job (hardcoded id) needs no app-id arg — the
  scaffold call stays 4 elements so the crontab command stays bare."""
  from app import app_cron

  app_dir = tmp_path / "selfcontained"
  app_dir.mkdir()
  job_path = app_dir / "job.sh"
  fake_scaffold = tmp_path / "init-cron-scaffold.sh"
  fake_scaffold.write_text("#!/bin/bash\n")

  with patch.dict(os.environ, {"MOBIUS_ALLOW_TEST_CRON": "1"}), \
       patch("app.app_cron.subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stderr="")
    app_cron.register_cron(
      "selfcontained", "0 6 * * *", job_path, scaffold=fake_scaffold,
    )

  assert mock_run.call_args.args[0] == [
    str(fake_scaffold), "selfcontained", "0 6 * * *", "job.sh",
  ]


def test_register_cron_refuses_real_subprocess_in_test_runtime(
  tmp_path, monkeypatch,
):
  """The Python boundary blocks the exact production-container pytest leak."""
  from app import app_cron

  fake_scaffold = tmp_path / "init-cron-scaffold.sh"
  fake_scaffold.write_text("#!/bin/sh\n")
  monkeypatch.delenv("MOBIUS_ALLOW_TEST_CRON", raising=False)

  with patch("app.app_cron.subprocess.run") as mock_run, \
       pytest.raises(app_cron.HTTPException) as exc:
    app_cron.register_cron(
      "memory", "30 5 * * *", tmp_path / "fetch.sh", 3,
      scaffold=fake_scaffold,
    )

  assert exc.value.status_code == 500
  assert "disabled in the test runtime" in exc.value.detail
  mock_run.assert_not_called()


def test_register_cron_timeout_is_infrastructure_failure(tmp_path):
  from app import app_cron

  scaffold = tmp_path / "init-cron-scaffold.sh"
  scaffold.write_text("#!/bin/sh\n")
  with patch.dict(os.environ, {"MOBIUS_ALLOW_TEST_CRON": "1"}), \
       patch(
         "app.app_cron.subprocess.run",
         side_effect=subprocess.TimeoutExpired([str(scaffold)], 30),
       ), \
       pytest.raises(app_cron.CronInfrastructureError):
    app_cron.register_cron(
      "memory", "0 5 * * *", tmp_path / "fetch.sh", 3,
      scaffold=scaffold,
    )


def test_unregister_cron_refuses_real_subprocess_in_test_runtime(
  tmp_path, monkeypatch,
):
  """Uninstall cleanup cannot rewrite a production crontab during pytest."""
  from app import install

  monkeypatch.delenv("MOBIUS_ALLOW_TEST_CRON", raising=False)
  with patch("app.install.subprocess.run") as mock_run:
    install._unregister_cron(tmp_path / "apps" / "memory")

  mock_run.assert_not_called()


def test_crontab_without_app_is_prefix_safe_and_preserves_header():
  """Deleting an app drops only its own crontab lines. The dir match
  carries a trailing slash so 'news' never strips 'news-2', and the
  non-job PATH= header is always kept."""
  from pathlib import Path
  from app import install

  crontab = (
    "PATH=/usr/local/bin:/usr/bin:/bin\n"
    "0 9 * * * /data/apps/news/fetch.sh 12\n"
    "0 10 * * * /data/apps/news-2/fetch.sh 42\n"
    "*/10 * * * * /data/apps/news/sync-cron.sh\n"
  )

  # Removing "news" keeps the PATH header AND every news-2 line.
  out = install._crontab_without_app(crontab, Path("/data/apps/news"))
  assert out is not None
  assert "/data/apps/news/fetch.sh" not in out
  assert "/data/apps/news/sync-cron.sh" not in out
  assert "/data/apps/news-2/fetch.sh 42" in out      # prefix not clobbered
  assert "PATH=/usr/local/bin:/usr/bin:/bin" in out   # header preserved

  # Removing "news-2" leaves both news lines untouched.
  out2 = install._crontab_without_app(crontab, Path("/data/apps/news-2"))
  assert out2 is not None
  assert "/data/apps/news-2/fetch.sh" not in out2
  assert "/data/apps/news/fetch.sh 12" in out2

  # An unrelated app whose ARGS merely reference the deleted app's dir is
  # NOT collateral — only the line whose COMMAND is under the dir is dropped.
  with_argref = (
    "0 9 * * * /data/apps/news/fetch.sh 12\n"
    "0 6 * * * /data/apps/agg/run.sh --feed /data/apps/news/headlines\n"
  )
  out3 = install._crontab_without_app(with_argref, Path("/data/apps/news"))
  assert out3 is not None
  assert "/data/apps/news/fetch.sh" not in out3
  assert "/data/apps/agg/run.sh --feed /data/apps/news/headlines" in out3

  # No matching entry → None (caller skips the rewrite entirely).
  assert install._crontab_without_app(crontab, Path("/data/apps/ghost")) is None

  # Removing the only entries yields an empty crontab (not None).
  single = "0 9 * * * /data/apps/solo/job.sh\n"
  assert install._crontab_without_app(single, Path("/data/apps/solo")) == ""

  # Edge shapes: @shorthand schedules + inline VAR=val command prefixes are
  # cleaned; comments + env lines that merely contain the path are kept.
  edge = (
    "MAILTO=root\n"
    "# nightly /data/apps/news/fetch.sh — note, keep me\n"
    "@daily /data/apps/news/fetch.sh\n"
    "0 6 * * * TZ=UTC /data/apps/news/fetch.sh\n"
    "@reboot /data/apps/other/boot.sh\n"
  )
  out4 = install._crontab_without_app(edge, Path("/data/apps/news"))
  assert out4 is not None
  assert "@daily /data/apps/news/fetch.sh" not in out4        # shorthand dropped
  assert "TZ=UTC /data/apps/news/fetch.sh" not in out4        # env-prefixed dropped
  assert "# nightly /data/apps/news/fetch.sh" in out4          # comment kept
  assert "MAILTO=root" in out4                                 # env line kept
  assert "@reboot /data/apps/other/boot.sh" in out4           # other app kept


def test_install_validates_required_fields(client, auth, bypass_url_validation):
  """Missing id / version / description / entry → 400 with field names."""
  bad = {"name": "no fields"}
  base = "https://x.test/"
  responses = {base + "mobius.json": (200, json.dumps(bad).encode())}
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 400
  detail = r.json()["detail"]
  for field in ("id", "version", "description", "entry"):
    assert field in detail


def test_install_update_path_in_place(client, auth, bypass_url_validation):
  """Second install of the same manifest_url PATCHes the existing app:
  same row, fresh jsx_source, preserved user data in seeds. Identity
  is keyed on manifest_url (the URL the app was installed from), so
  the two installs must use the same URL to land on the update path."""
  base = "https://x.test/repo/"
  responses_v1 = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "version": "1.0.0",
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v1 prompt"),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v1),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201
  v1_id = r1.json()["id"]

  # User edits the prompt seed before the update lands.
  data_dir = Path(get_settings().data_dir)
  user_prompt_path = data_dir / "apps" / str(v1_id) / "prompt.md"
  user_prompt_path.write_text("USER EDITED")

  jsx_v2 = JSX.replace("ok", "ok v2")
  responses_v2 = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "version": "1.2.0",
    }).encode()),
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v2 default prompt"),  # should NOT clobber
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "update"
  assert payload["version"] == "1.2.0"
  assert payload["id"] == v1_id  # same row, not a duplicate
  # User's edit is preserved
  assert user_prompt_path.read_text() == "USER EDITED"
  # JSX got refreshed in source_dir
  jsx_file = data_dir / "apps" / "test-news" / "index.jsx"
  assert jsx_file.read_text() == jsx_v2


def test_store_update_removes_package_icon_without_erasing_owner_override(
  client, auth, db, bypass_url_validation,
):
  """Manifest artwork and home-screen customization have distinct owners."""
  from app import icon_assets, models
  from PIL import Image

  base = "https://icons.test/repo/"
  manifest_v1 = {
    "id": "icon-ownership",
    "name": "Icon Ownership",
    "version": "1.0.0",
    "description": "Icon lifecycle test.",
    "entry": "index.jsx",
    "icon": "icon.png",
    "permissions": {},
    "runtime": {"imports": ["react"], "esm_deps": []},
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client({
      base + "mobius.json": (200, json.dumps(manifest_v1).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "icon.png": (200, _png_bytes()),
    }),
  ):
    installed = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]

  override_raw = io.BytesIO()
  Image.new("RGB", (24, 18), (230, 70, 90)).save(
    override_raw, format="PNG",
  )
  expected_override = icon_assets.normalize_icon(override_raw.getvalue())
  overridden = client.put(
    f"/api/apps/{app_id}/icon", content=override_raw.getvalue(), headers=auth,
  )
  assert overridden.status_code == 204, overridden.text

  manifest_v2 = {**manifest_v1, "version": "2.0.0", "icon": None}
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client({
      base + "mobius.json": (200, json.dumps(manifest_v2).encode()),
      base + "index.jsx": (200, JSX.encode()),
    }),
  ):
    updated = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert updated.status_code == 201, updated.text
  assert updated.json()["mode"] == "update"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.icon_png is None
  assert row.icon_override_png == expected_override
  assert client.get(f"/api/apps/{app_id}/icon").content == expected_override

  reset = client.put(f"/api/apps/{app_id}/icon", content=b"", headers=auth)
  assert reset.status_code == 204, reset.text
  assert client.get(f"/api/apps/{app_id}/icon").status_code == 404


















def test_derive_repo_ref_from_raw_github_manifest_url():
  from app.install import _derive_repo_ref

  assert _derive_repo_ref(
    "https://raw.githubusercontent.com/acme/widgets/main/mobius.json"
  ) == ("https://github.com/acme/widgets.git", "main")


def test_derive_repo_ref_returns_none_for_non_github_and_inline():
  from app.install import _derive_repo_ref

  assert _derive_repo_ref(
    "https://example.test/acme/widgets/main/mobius.json"
  ) is None
  assert _derive_repo_ref("inline-manifest") is None


def test_derive_repo_ref_only_root_manifest_single_segment_ref():
  """Only the canonical root-manifest / single-segment-ref shape clones.

  A subdir-hosted manifest or a slash-containing branch would make a greedy
  parse clone the wrong tree (repo root at a mis-read ref), so both must be
  rejected as unsupported sources. A leading-dash ref is also rejected before
  it can reach Git as an option."""
  from app.install import _derive_repo_ref

  # subdir-hosted manifest — clone would get the wrong index.jsx
  assert _derive_repo_ref(
    "https://raw.githubusercontent.com/acme/widgets/main/pkg/mobius.json"
  ) is None
  # slash-containing branch — greedy parts[:3] would mis-read ref as "feature"
  assert _derive_repo_ref(
    "https://raw.githubusercontent.com/acme/widgets/feature/x/mobius.json"
  ) is None
  # too few segments (no manifest file)
  assert _derive_repo_ref(
    "https://raw.githubusercontent.com/acme/widgets/main"
  ) is None
  # leading-dash ref (defense-in-depth: git would read it as an option)
  assert _derive_repo_ref(
    "https://raw.githubusercontent.com/acme/widgets/-x/mobius.json"
  ) is None


def test_commit_pinned_install_clones_exact_reviewed_commit(
  client, auth, tmp_path,
):
  """Production Git loading installs the exact reviewed complete commit."""
  pinned_jsx = "export default function App() { return <div>pinned git</div> }"
  work, bare, _initial = _make_clone_fixture(
    tmp_path, pinned_jsx, "export const cards = ['pinned']",
  )
  manifest = {
    "id": "pinned-fetch",
    "name": "Pinned Fetch",
    "version": "1.0.0",
    "description": "Immutable fetched-source install",
    "entry": "index.jsx",
    "source_files": ["cards.js", "tool.sh"],
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  (work / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  (work / "tool.sh").write_text("#!/bin/sh\necho pinned\n", encoding="utf-8")
  (work / "tool.sh").chmod(0o755)
  commit = _fixture_commit(work, "complete reviewed package")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  _push_clone_fixture(
    work, bare,
    "export default function App() { return <div>later tip</div> }",
    "export const cards = ['later']",
  )
  base = f"https://raw.githubusercontent.com/acme/pinned/{commit}/"
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), commit),
  ), patch(
    "app.install._validate_url_safe",
    side_effect=lambda url: (
      url, urlparse(url).netloc, urlparse(url).hostname,
    ),
  ):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })

  assert response.status_code == 201, response.text
  source_dir = Path(get_settings().data_dir) / "apps" / "pinned-fetch"
  assert (source_dir / "index.jsx").read_text(encoding="utf-8") == pinned_jsx
  assert app_git.is_repo(source_dir)
  assert app_git.origin_url(source_dir) == bare.as_uri()
  assert app_git.head_sha(source_dir, "main") == commit
  assert app_git.head_sha(source_dir, "upstream") == commit
  assert (source_dir / "cards.js").read_text() == "export const cards = ['pinned']"
  assert (source_dir / "tool.sh").stat().st_mode & 0o111


def test_install_rejects_discovery_manifest_that_differs_from_git_commit(
  client, auth, tmp_path,
):
  """HTTP locates a package; the Git commit remains its only authority."""
  git_manifest = {
    "id": "git-authority",
    "name": "Git Authority",
    "version": "1.0.0",
    "description": "Committed package",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  work, bare, _initial = _make_clone_fixture(
    tmp_path, JSX, "export const cards = []",
  )
  (work / "mobius.json").write_text(json.dumps(git_manifest), encoding="utf-8")
  commit = _fixture_commit(work, "commit package manifest")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  base = "https://raw.githubusercontent.com/acme/git-authority/main/"
  discovered = {**git_manifest, "permissions": {"cross_app_access": "read"}}
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client({
      base + "mobius.json": (200, json.dumps(discovered).encode()),
    }),
  ), patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), commit),
  ), patch(
    "app.install._validate_url_safe",
    side_effect=lambda url: (
      url, urlparse(url).netloc, urlparse(url).hostname,
    ),
  ):
    response = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "git_manifest_changed"
  assert not (Path(get_settings().data_dir) / "apps" / "git-authority").exists()



def test_known_git_origin_clone_failure_rolls_back_instead_of_importing_http(
  client, auth, db, tmp_path, bypass_url_validation,
):
  """A transient clone failure must not permanently disconnect update history."""
  base = "https://raw.githubusercontent.com/acme/required-git/main/"
  manifest = {
    "id": "required-git", "name": "Required Git", "version": "1.0.0",
    "description": "Git lineage is required", "entry": "index.jsx",
  }
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses),
  ), patch(
    "app.install.app_git.clone_upstream", side_effect=RuntimeError("offline"),
  ):
    failed = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert failed.status_code == 409, failed.text
  assert failed.json()["detail"]["code"] == "git_install_unavailable"
  assert db.query(models.App).filter_by(slug="required-git").first() is None
  source_dir = Path(get_settings().data_dir) / "apps" / "required-git"
  assert not source_dir.exists()

  work, bare, _initial = _make_clone_fixture(
    tmp_path, JSX, "export const cards = []",
  )
  (work / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  commit = _fixture_commit(work, "commit package manifest")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  with patch(
    "app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses),
  ), patch("app.install._derive_repo_ref", return_value=(bare.as_uri(), "main")):
    retry = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert retry.status_code == 201, retry.text
  assert app_git.origin_url(source_dir) == bare.as_uri()
  assert app_git.head_sha(source_dir, "upstream") == commit


def test_fresh_clone_cannot_replace_owner_reviewed_source(
  client, auth, db, tmp_path, bypass_url_validation,
):
  from app import install
  work, bare, _initial = _make_clone_fixture(
    tmp_path, JSX.replace("ok", "unreviewed change"), "export const cards = []",
  )
  base = "https://raw.githubusercontent.com/acme/reviewed/main/"
  manifest = {
    "id": "reviewed", "name": "Reviewed", "version": "1.0.0",
    "description": "Reviewed source is binding", "entry": "index.jsx",
  }
  (work / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  commit = _fixture_commit(work, "commit package manifest")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  digest = install._source_review_digest(
    manifest=manifest, entry_bytes=JSX.encode(), bundled_job=None, source_files={},
  )
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
  }
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses)), patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), commit),
  ):
    response = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json", "reviewed_source_digest": digest,
    })
  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "update_changed"
  assert db.query(models.App).filter_by(slug="reviewed").first() is None
  assert not (Path(get_settings().data_dir) / "apps" / "reviewed").exists()

def test_update_dropping_schedule_unregisters_orphan_cron(
    client, auth, bypass_url_validation):
  """Recurring → on-demand migration must converge cron state, not just add.

  Install v1 with `schedule.default` (a crontab line + init-cron.sh on disk),
  then update to a manifest that DROPS `schedule.default`. The old crontab
  entry and the replayable init-cron.sh must be gone — otherwise the boot
  replay resurrects a dead job every restart (card 099). The autouse
  scaffold-bypass keeps the install off the real crontab, so we drop a stub
  init-cron.sh in by hand (as the real scaffold would have) and spy on
  _unregister_cron to confirm the update tears the live entry down too."""
  base = "https://x.test/repo-drop/"
  responses_v1 = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "version": "1.0.0",
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v1 prompt"),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v1),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201
  v1_id = r1.json()["id"]
  assert r1.json()["version"] == "1.0.0"

  # Stand in for the crontab line the real scaffold would have written.
  data_dir = Path(get_settings().data_dir)
  source_dir = data_dir / "apps" / "test-news"
  init_cron = source_dir / "init-cron.sh"
  init_cron.write_text("#!/bin/bash\nexit 0\n")

  # v2 drops `schedule.default` entirely (becomes on-demand-only): the
  # crontab entry registered for v1 is now an orphan.
  manifest_v2 = {
    **MANIFEST_NEWS,
    "version": "2.0.0",
    "schedule": {"job": "fetch.sh"},  # no `default`
  }
  responses_v2 = {
    base + "mobius.json": (200, json.dumps(manifest_v2).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v2 prompt"),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ), patch("app.install._unregister_cron") as mock_unregister:
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["id"] == v1_id

  # Live crontab line dropped for THIS app's source dir.
  assert mock_unregister.called, "update never unregistered the v1 cron"
  assert mock_unregister.call_args.args[0] == source_dir
  # The replayable init-cron.sh is gone so boot replay can't resurrect it.
  assert not init_cron.exists()




def test_update_keeping_schedule_still_registers_cron(
    client, auth, bypass_url_validation):
  """The drop-then-maybe-reregister convergence must NOT regress the common
  case: an update that still declares `schedule.default` re-registers cron
  (the scaffold-bypass routes that through the cron-pending sentinel/warning,
  the same observable signal the fresh-install test asserts on)."""
  base = "https://x.test/repo-keep/"
  responses = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "version": "1.0.0",
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"prompt"),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201
  v1_id = r1.json()["id"]

  responses_v2 = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "version": "2.0.0",  # schedule.default kept
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"prompt v2"),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "update"
  assert payload["id"] == v1_id
  # Schedule still declared → cron re-registration attempted (warning under
  # the test scaffold-bypass).
  assert any("cron" in w for w in payload["warnings"])


def test_installed_version_persisted_in_app_list(
  client, auth, bypass_url_validation,
):
  """The installed manifest version is persisted on the App row and
  surfaced by GET /api/apps/ (AppOut.version) — not just echoed once in
  the install response. This is what lets the store read the installed
  version of ANY app (agent-installed, pre-seeded, out-of-band), not
  only the ones it installed through its own UI; an update re-stamps it."""
  base = "https://x.test/versioned/"

  def responses(version, jsx):
    return {
      base + "mobius.json": (200, json.dumps({
        **MANIFEST_NEWS, "version": version,
      }).encode()),
      base + "index.jsx": (200, jsx.encode()),
      base + "icon.png": (200, _png_bytes()),
      base + "prompt.md": (200, b"p"),
      base + "fetch.sh": (200, b""),
    }

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses("1.0.0", JSX)),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]

  # The fix: GET /api/apps/ carries the installed version. Before this,
  # AppOut had no version field and the store read "unknown".
  listed = client.get("/api/apps/", headers=auth).json()
  row = next(a for a in listed if a["id"] == app_id)
  assert row["version"] == "1.0.0"

  # An update re-stamps the row's version so update-detection stays honest.
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses("1.3.0", JSX.replace("ok", "ok2"))),
  ):
    client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  row2 = next(
    a for a in client.get("/api/apps/", headers=auth).json()
    if a["id"] == app_id
  )
  assert row2["version"] == "1.3.0"


def test_inline_reinstall_is_rejected_even_when_manifest_matches(
  client, auth, bypass_url_validation,
):
  """Matching inline bytes cannot bypass the real Git source contract."""
  base = "https://x.test/dup/"
  manifest = {**MANIFEST_NEWS, "id": "dup-target"}
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }

  # First install: URL form.
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201, r1.text
  assert r1.json()["mode"] == "install"
  first_id = r1.json()["id"]

  # Matching inline bytes still have no repository identity or commit.
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest": manifest,
      "raw_base": base,
    })
  assert r2.status_code == 400, r2.text
  assert r2.json()["detail"]["code"] == "git_source_required"
  rows = client.get("/api/apps/", headers=auth).json()
  assert [row["id"] for row in rows if row["slug"] == "dup-target"] == [
    first_id,
  ]


def test_install_rolls_back_on_compile_failure(client, auth, bypass_url_validation):
  """Bad JSX leaves no App row and one bounded source recovery."""
  base = "https://x.test/bad/"
  bad_jsx = "this is not valid JSX <<>>"
  responses = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "id": "rollback-target",
    }).encode()),
    base + "index.jsx": (200, bad_jsx.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code in (422, 500)
  data_dir = Path(get_settings().data_dir)
  source = data_dir / "apps" / "rollback-target"
  assert not source.exists()
  retained = source.parent / ".rollback-target.mobius-failed.bak"
  assert app_git.is_repo(retained)
  assert (retained / "index.jsx").read_text() == bad_jsx
  list_r = client.get("/api/apps/", headers=auth)
  slugs = [a["slug"] for a in list_r.json()]
  assert "rollback-target" not in slugs


def test_install_inline_manifest_is_rejected(client, auth):
  """Inline packages cannot establish a real repository commit."""
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest": {**MANIFEST_NEWS, "id": "inline-test"},
  })
  assert r.status_code == 400
  assert r.json()["detail"]["code"] == "git_source_required"


def test_install_inline_raw_base_does_not_create_a_package_source(
  client, auth, bypass_url_validation,
):
  """A raw asset base is not a substitute for repository provenance."""
  base = "https://packages.test/x/app-inline-main"
  manifest = {**MANIFEST_NEWS, "id": "inline-noslash"}
  responses = {
    base + "/index.jsx": (200, JSX.encode()),
    base + "/icon.png": (200, _png_bytes()),
    base + "/prompt.md": (200, PROMPT.encode()),
    base + "/fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest": manifest,
      "raw_base": base,
    })
  assert r.status_code == 400, r.text
  assert r.json()["detail"]["code"] == "git_source_required"


@pytest.mark.parametrize("raw_base", [
  "not-a-url",
  "ftp://example.com/app/",
  "https://example.com/app/?branch=main",
  "https://example.com/app/#main",
])
def test_install_inline_rejects_malformed_raw_base(client, auth, raw_base):
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest": {**MANIFEST_NEWS, "id": "bad-raw-base"},
    "raw_base": raw_base,
  })
  assert r.status_code == 400
  assert r.json()["detail"]["code"] == "git_source_required"


@pytest.mark.parametrize("field_patch, expected_field", [
  ({"entry": "../index.jsx"}, "entry"),
  ({"entry": "index.jsx?ref=main"}, "entry"),
  ({"entry": "%2e%2e/index.jsx"}, "entry"),
  ({"entry": "src%2findex.jsx"}, "entry"),
  ({"icon": "/icon.png"}, "icon"),
  ({"storage_seeds": {"prompt.md": "https://example.com/prompt.md"}}, "storage_seeds.prompt.md"),
  ({"storage_seeds": []}, "storage_seeds"),
  ({"static_assets": {"../index.html": "build/index.html"}}, "static_assets.../index.html"),
  ({"static_assets": {"index.html": "/build/index.html"}}, "static_assets.index.html"),
  ({"static_assets": {"store/screen.png": "art/screen.png"}}, "static_assets.store/screen.png"),
  ({"static_assets": "build/index.html"}, "static_assets"),
])
def test_install_rejects_non_repo_relative_manifest_asset_paths(
  field_patch, expected_field,
):
  """External manifests must point asset references inside their repo.

  This mirrors the public schema and keeps mistakes/hostile manifests as
  precise 400s rather than odd URL concatenations or late install 500s.
  """
  from app import install

  manifest = {**MANIFEST_NEWS, "id": "bad-asset-path", **field_patch}
  with pytest.raises(Exception) as exc:
    install._validate_manifest(manifest)
  assert expected_field in str(getattr(exc.value, "detail", exc.value))


def test_storage_seeds_inline_content_400_teaches_the_contract():
  """A string seed value that is really inline content fails the path check,
  and the 400 names the path-vs-inline-JSON contract — not just "must be a
  relative path" — so the author sees the wrong shape, not a phantom typo.
  This is the footgun that made Web Studio mis-encode its starter files."""
  from app import install

  inline_html = '<!DOCTYPE html>\n<a href="#features">hi</a>\n'
  manifest = {
    **MANIFEST_NEWS,
    "id": "seed-inline-content",
    "storage_seeds": {"files/index.html": inline_html},
  }
  with pytest.raises(Exception) as exc:
    install._validate_manifest(manifest)
  detail = str(getattr(exc.value, "detail", exc.value))
  assert "storage_seeds.files/index.html" in detail
  assert "non-string" in detail and "installer fetches" in detail


def test_non_seed_path_rejection_omits_the_seed_hint():
  """The seed-specific teaching hint attaches only to storage_seeds fields;
  entry/icon/static_assets strings are always paths, so their 400 stays
  generic and never mentions storage_seeds."""
  from app import install

  manifest = {**MANIFEST_NEWS, "id": "bad-entry-path", "entry": "../index.jsx"}
  with pytest.raises(Exception) as exc:
    install._validate_manifest(manifest)
  detail = str(getattr(exc.value, "detail", exc.value))
  assert "entry" in detail
  assert "storage_seeds" not in detail


def test_install_rejects_both_manifest_and_url(client, auth):
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest_url": "https://x/m.json",
    "manifest": {"id": "x"},
  })
  assert r.status_code == 400


def test_install_icon_404_is_warning_not_failure(client, auth, bypass_url_validation):
  """No icon at the declared path → install succeeds, warning records it."""
  base = "https://x.test/noicon/"
  responses = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "id": "no-icon",
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (404, b""),  # explicitly missing
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201
  assert any("icon" in w.lower() for w in r.json()["warnings"])


def test_install_rejects_slug_with_path_traversal(client, auth, bypass_url_validation):
  """Manifest `id` with characters that would let the cron script
  treat the slug as a path is rejected upfront."""
  base = "https://x.test/evil/"
  bad = {**MANIFEST_NEWS, "id": "../../etc/passwd"}
  responses = {base + "mobius.json": (200, json.dumps(bad).encode())}
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 400


# --- SSRF + argv-injection hardening (security review follow-up) ----


@pytest.mark.parametrize("bad_url", [
  "http://127.0.0.1:8000/api/owner/secret",
  "http://localhost/admin",
  "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
  "http://10.0.0.5/internal",
  "http://192.168.1.1/router",
])
def test_install_rejects_private_and_loopback_targets(client, auth, bad_url):
  """SSRF: manifest URLs that resolve to loopback / private / link-local /
  cloud-metadata addresses are rejected before any fetch happens."""
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest_url": bad_url,
  })
  assert r.status_code == 400
  assert r.json()["detail"]["code"] == "git_source_required"


def test_install_rejects_non_http_scheme(client, auth):
  """SSRF: file:// and other schemes are rejected."""
  for url in ("file:///etc/passwd", "ftp://x/y.json", "gopher://x/"):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": url,
    })
    assert r.status_code == 400, url
    assert r.json()["detail"]["code"] == "git_source_required"


def test_install_rejects_redirect_to_private_ip(client, auth, bypass_url_validation):
  """SSRF: a 302 response pointing at 127.0.0.1 must be re-validated and
  rejected by the manual redirect handler — even when the initial URL
  passed validation."""
  # We bypass the first validation via the fixture, then patch back IN
  # the validation only for the redirect target so we exercise the
  # manual-rewalk behavior independent of getaddrinfo.
  base = "https://x.test/redir/"
  evil = "http://127.0.0.1:8000/internal"
  responses = {
    base + "mobius.json": (
      302, b"", {"Location": evil},
    ),
  }

  class _FakeClient:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc):
      return False

    def stream(self, method, url, **kwargs):
      if url == base + "mobius.json":
        return _StreamCtx(302, b"", headers={"Location": evil})
      return _StreamCtx(404, b"")

  # Only validate the redirect target — the initial fetch goes through
  # the bypass fixture. This mirrors the real-world threat: legitimate
  # CDN host issues a redirect to a private IP.
  real_validate = __import__("app.install", fromlist=["_validate_url_safe"])._validate_url_safe
  def _selective_validate(url):
    if url == evil:
      from fastapi import HTTPException
      raise HTTPException(400, f"URL {url} resolves to blocked address")
    return url, urlparse(url).netloc, urlparse(url).hostname
  with patch(
    "app.install.httpx.AsyncClient", lambda *a, **kw: _FakeClient(),
  ), patch(
    "app.install._validate_url_safe", side_effect=_selective_validate,
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 400
  assert "block" in r.json()["detail"].lower()


def test_install_rejects_slug_with_leading_dash(client, auth, bypass_url_validation):
  """Argv injection: a slug like `-rf` could be parsed as a flag by
  whatever tool downstream consumes it. Reject at the boundary."""
  base = "https://x.test/argv/"
  bad = {**MANIFEST_NEWS, "id": "-rf"}
  responses = {base + "mobius.json": (200, json.dumps(bad).encode())}
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 400
  assert "start with" in r.json()["detail"].lower()


@pytest.mark.parametrize("bad_expr", [
  "; rm -rf /",         # shell metachar — should never reach subprocess
  "$(curl evil)",       # command substitution attempt
  "`whoami`",           # backtick command substitution
  "-flag */10 * * * *", # leading dash
  "0 10",               # too few cron fields
  "0 0 * * * *",        # sixth field would be parsed as the command
])
def test_install_rejects_malformed_cron(client, auth, bypass_url_validation, bad_expr):
  base = "https://x.test/cron/"
  bad = {**MANIFEST_NEWS, "schedule": {"default": bad_expr, "job": "fetch.sh"}}
  responses = {base + "mobius.json": (200, json.dumps(bad).encode())}
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 400, f"{bad_expr!r}: {r.text}"


def test_install_accepts_valid_cron_shapes(client, auth, bypass_url_validation):
  """Sanity: real cron expressions don't trip the new validator."""
  good_exprs = ["0 10 * * *", "*/10 * * * *", "0,30 8-17 * * 1-5"]
  for expr in good_exprs:
    base = f"https://x.test/cronok-{hash(expr) & 0xffff}/"
    m = {**MANIFEST_NEWS,
         "id": f"cronok-{abs(hash(expr)) & 0xffff}",
         "schedule": {"default": expr, "job": "fetch.sh"}}
    responses = {
      base + "mobius.json": (200, json.dumps(m).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "icon.png": (200, _png_bytes()),
      base + "prompt.md": (200, PROMPT.encode()),
      base + "fetch.sh": (200, b""),
    }
    with patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ):
      r = client.post("/api/apps/install", headers=auth, json={
        "manifest_url": base + "mobius.json",
      })
    assert r.status_code == 201, f"{expr!r}: {r.text}"


# --- Decompression-bomb defense (fix 2) -----------------------------


def test_install_rejects_decompression_bomb_icon(client, auth, bypass_url_validation):
  """Fix 2: a tiny PNG that decodes to a giant image must be rejected
  before PIL's `load()` allocates gigabytes. We patch `Image.open` to
  return a mock whose `.size` reports 50000x50000 — the dimension gate
  fires before `load()`, so the install endpoint treats it as a 415
  icon error and surfaces it as a non-fatal warning (icons are
  optional). The app installs without the icon."""
  from unittest.mock import patch as _patch, MagicMock
  base = "https://x.test/bomb/"
  responses = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "id": "bomb-icon",
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, b"\x89PNG\r\n\x1a\n" + b"bogus"),  # any bytes
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  fake_img = MagicMock()
  fake_img.size = (50000, 50000)
  fake_img.mode = "RGB"
  with _patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), _patch("PIL.Image.open", return_value=fake_img):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  # Icon rejection is non-fatal — the install succeeds, the icon path
  # surfaces as a warning. The important assertion is that PIL.load()
  # was NEVER called (i.e. no gigabyte allocation).
  fake_img.load.assert_not_called()
  assert r.status_code == 201, r.text
  assert any("icon" in w.lower() for w in r.json()["warnings"])


# --- Stream byte counter aborts mid-download (fix 3) ----------------


@pytest.mark.asyncio
async def test_http_get_passes_sni_hostname_as_text(monkeypatch):
  """The live httpcore/anyio stack requires str, not pre-encoded bytes."""
  from app import install

  monkeypatch.setattr(
    install, "_validate_url_safe",
    lambda _url: ("https://203.0.113.8/file", "example.test", "example.test"),
  )

  class _Client:
    def stream(self, method, url, **kwargs):
      assert method == "GET"
      assert kwargs["extensions"]["sni_hostname"] == "example.test"
      assert isinstance(kwargs["extensions"]["sni_hostname"], str)
      return _StreamCtx(200, b"ok")

  assert await install._http_get(_Client(), "https://example.test/file", 10) == b"ok"


def test_install_aborts_when_stream_exceeds_cap(client, auth, bypass_url_validation):
  """Fix 3: `_http_get` now reads via `client.stream()` and tracks
  bytes per chunk, aborting once the running total crosses the cap.
  A response that totals well over the manifest cap MUST 413 — and
  it must do so without buffering the whole body. We assert the
  endpoint returns the upstream 413 surfaced as an install failure."""
  base = "https://x.test/huge/"
  # Build a multi-chunk body that crosses _MANIFEST_MAX_BYTES (64KB).
  big_chunks = [b"x" * 32 * 1024 for _ in range(5)]  # 160 KB total
  # We need a custom client that returns chunked bodies for the
  # manifest URL specifically.
  class _ChunkedClient:
    async def __aenter__(self):
      return self
    async def __aexit__(self, *exc):
      return False
    def stream(self, method, url, **kwargs):
      if url == base + "mobius.json":
        return _StreamCtx(200, b"".join(big_chunks), chunks=big_chunks)
      return _StreamCtx(404, b"")
  with patch(
    "app.install.httpx.AsyncClient",
    lambda *a, **kw: _ChunkedClient(),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  # The install handler surfaces upstream 4xx fetch errors as 4xx;
  # 413 cap-exceeded should reach the response.
  assert r.status_code == 413, r.text
  assert "cap" in r.json()["detail"].lower() or "exceeds" in r.json()["detail"].lower()


def test_install_surfaces_github_rate_limit_as_429(client, auth, bypass_url_validation):
  base = "https://raw.githubusercontent.com/mobius-os/app-test/main/"
  responses = {
    base + "mobius.json": (
      429,
      b"rate limited",
      {"retry-after": "60"},
    ),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 429, r.text
  assert "GitHub rate-limited" in r.json()["detail"]
  assert "minute" in r.json()["detail"]


# --- Update path rolls back compiled bundle (fix 4) -----------------


def test_update_compile_failure_preserves_old_bundle(client, auth, bypass_url_validation):
  """Fix 4: a failed v2 install must not leave the on-disk compiled
  bundle in the broken-v2 state. We install v1 (good JSX), record the
  compiled bytes, then attempt a v2 install with broken JSX — assert
  the v2 install fails AND the v1 compiled bytes are still on disk.
  The update branch is now keyed on manifest_url, so both installs
  use the same URL to exercise that path."""
  base = "https://x.test/upd/"
  responses_v1 = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "id": "upd-target", "version": "1.0.0",
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v1),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  data_dir = Path(get_settings().data_dir)
  compiled_path = Path(r1.json()["compiled_path"])
  assert compiled_path.exists(), "v1 bundle should be on disk"
  v1_bytes = compiled_path.read_bytes()
  assert len(v1_bytes) > 0

  # v2 attempt: same manifest_url (forces update path), broken JSX → compile fails
  responses_v2 = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "id": "upd-target", "version": "2.0.0",
    }).encode()),
    base + "index.jsx": (200, b"this is not valid JSX <<>>"),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code in (422, 500), r2.text

  # The compiled bundle on disk must still be the v1 bytes — the
  # rollback path restored the .bak snapshot.
  assert compiled_path.exists(), "v1 bundle should still exist after failed v2"
  assert compiled_path.read_bytes() == v1_bytes, (
    "v1 bundle on disk was clobbered by failed v2 compile — "
    "rollback didn't restore the snapshot"
  )
  # No staging artifact or second content bundle may leak from the failed v2.
  assert not (data_dir / "compiled" / f"app-{app_id}.js.staging").exists()
  assert list((data_dir / "compiled").glob(f"app-{app_id}-*.js")) == [
    compiled_path,
  ]


def test_update_source_change_during_build_keeps_the_later_draft(
  client, auth, db, bypass_url_validation, monkeypatch,
):
  """A Store build never compiles one tree and accepts later editable bytes."""
  from app import install as install_module

  base = "https://x.test/stale-build/"
  manifest = {
    "id": "stale-build",
    "name": "Stale build",
    "version": "1.0.0",
    "description": "Build transaction fixture",
    "entry": "index.jsx",
    "permissions": {},
  }
  responses_v1 = _check_responses(base, manifest, JSX)
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v1),
  ):
    first = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]
  row = db.get(models.App, app_id)
  previous_bundle = row.compiled_path
  previous_source_commit = row.source_commit
  previous_runtime = row.runtime_revision
  source = Path(row.source_dir)

  original_compile = install_module.compile_jsx
  later_draft = "export default () => <div>later draft</div>\n"

  async def compile_then_edit(*args, **kwargs):
    result = await original_compile(*args, **kwargs)
    (source / "index.jsx").write_text(later_draft, encoding="utf-8")
    return result

  monkeypatch.setattr(install_module, "compile_jsx", compile_then_edit)
  manifest_v2 = {**manifest, "version": "2.0.0"}
  responses_v2 = _check_responses(
    base, manifest_v2, "export default () => <div>candidate v2</div>\n",
  )
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    update = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert update.status_code == 409, update.text
  assert update.json()["detail"]["code"] == "source_changed"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert row.source_commit == previous_source_commit
  assert row.runtime_revision == previous_runtime
  assert (source / "index.jsx").read_text(encoding="utf-8") == later_draft


@pytest.mark.parametrize(
  ("race", "expected_path", "expected_content"),
  [
    ("edit", "index.jsx", "export default () => <div>later draft</div>\n"),
    ("create", "new.js", "export const ownerDraft = true\n"),
    ("delete", "index.jsx", None),
    ("edit_removed", "old.js", "export const ownerDraft = true\n"),
  ],
)
def test_update_source_change_after_final_snapshot_preserves_owner_state(
  client, auth, db, bypass_url_validation, monkeypatch,
  race, expected_path, expected_content,
):
  """The journal never clobbers a change made after its final snapshot."""
  from app import install as install_module

  base = f"https://x.test/final-snapshot-{race}/"
  manifest = {
    "id": f"final-snapshot-{race}",
    "name": f"Final snapshot {race}",
    "version": "1.0.0",
    "description": "Final publication race fixture",
    "entry": "index.jsx",
    "source_files": ["old.js"],
    "permissions": {},
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(_check_responses(
      base, manifest, JSX,
      sources={"old.js": b"export const old = true\n"},
    )),
  ):
    first = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]
  row = db.get(models.App, app_id)
  previous_bundle = row.compiled_path
  previous_source_commit = row.source_commit
  previous_runtime = row.runtime_revision
  source = Path(row.source_dir)

  original_identity = install_module._editable_snapshot_identity
  calls = 0

  def snapshot_then_change(*args, **kwargs):
    nonlocal calls
    result = original_identity(*args, **kwargs)
    calls += 1
    if calls == 2:
      target = source / expected_path
      if expected_content is None:
        target.unlink()
      else:
        target.write_text(expected_content, encoding="utf-8")
    return result

  monkeypatch.setattr(
    install_module, "_editable_snapshot_identity", snapshot_then_change,
  )
  manifest_v2 = {
    **manifest,
    "version": "2.0.0",
    "source_files": ["new.js"],
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(_check_responses(
      base, manifest_v2, "export default () => <div>candidate v2</div>\n",
      sources={"new.js": b"export const candidate = true\n"},
    )),
  ):
    update = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert update.status_code == 409, update.text
  assert update.json()["detail"]["code"] == "source_changed"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert row.source_commit == previous_source_commit
  assert row.runtime_revision == previous_runtime
  target = source / expected_path
  if expected_content is None:
    assert not target.exists()
  else:
    assert target.read_text(encoding="utf-8") == expected_content


def test_update_source_change_during_publication_keeps_the_later_draft(
  client, auth, db, bypass_url_validation, monkeypatch,
):
  """Compensation never overwrites the edit that made publication stale."""
  from app import install as install_module

  base = "https://x.test/publication-draft/"
  manifest = {
    "id": "publication-draft",
    "name": "Publication draft",
    "version": "1.0.0",
    "description": "Publication transaction fixture",
    "entry": "index.jsx",
    "source_files": ["old.js"],
    "permissions": {},
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(_check_responses(
      base, manifest, JSX,
      sources={"old.js": b"export const old = true\n"},
    )),
  ):
    first = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]
  row = db.get(models.App, app_id)
  previous_bundle = row.compiled_path
  previous_source_commit = row.source_commit
  previous_runtime = row.runtime_revision
  source = Path(row.source_dir)
  later_draft = "export default () => <div>owner later draft</div>\n"
  later_new_draft = "export const ownerDraft = true\n"
  later_recreated_draft = "export const recreated = true\n"
  original_publish = install_module._publish_install_bundle

  def publish_then_edit(*args, **kwargs):
    result = original_publish(*args, **kwargs)
    (source / "index.jsx").write_text(later_draft, encoding="utf-8")
    (source / "new.js").write_text(later_new_draft, encoding="utf-8")
    (source / "old.js").write_text(later_recreated_draft, encoding="utf-8")
    return result

  monkeypatch.setattr(
    install_module, "_publish_install_bundle", publish_then_edit,
  )
  manifest_v2 = {
    **manifest,
    "version": "2.0.0",
    "source_files": ["new.js"],
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(_check_responses(
      base, manifest_v2, "export default () => <div>candidate v2</div>\n",
      sources={"new.js": b"export const candidate = true\n"},
    )),
  ):
    update = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert update.status_code == 409, update.text
  assert update.json()["detail"]["code"] == "source_changed"
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.compiled_path == previous_bundle
  assert row.source_commit == previous_source_commit
  assert row.runtime_revision == previous_runtime
  assert (source / "index.jsx").read_text(encoding="utf-8") == later_draft
  assert (source / "new.js").read_text(encoding="utf-8") == later_new_draft
  assert (source / "old.js").read_text(encoding="utf-8") == later_recreated_draft


def test_source_write_cleanup_restores_a_late_backup_edit(tmp_path):
  """Success cleanup never deletes a draft written through the old inode."""
  from app.install import _write_source_file

  target = tmp_path / "index.jsx"
  target.write_bytes(b"reviewed old\n")
  rollback_actions = []
  commit_actions = []
  _write_source_file(
    target,
    b"candidate\n",
    rollback_actions,
    commit_actions,
    executable=False,
    expected_previous=b"reviewed old\n",
    expected_previous_executable=False,
  )
  [backup] = list(tmp_path.glob(".index.jsx.mobius-old-*.bak"))
  backup.write_bytes(b"owner late draft\n")

  for action in commit_actions:
    action()

  assert target.read_bytes() == b"owner late draft\n"
  assert not backup.exists()


def test_source_write_rollback_keeps_a_later_owner_edit(tmp_path):
  from app.install import _run_rollback_actions, _write_source_file

  target = tmp_path / "index.jsx"
  target.write_bytes(b"reviewed old\n")
  rollback_actions = []
  commit_actions = []
  _write_source_file(
    target,
    b"candidate\n",
    rollback_actions,
    commit_actions,
    executable=False,
    expected_previous=b"reviewed old\n",
    expected_previous_executable=False,
  )
  target.write_bytes(b"owner later edit\n")

  _run_rollback_actions(rollback_actions)

  assert target.read_bytes() == b"owner later edit\n"
  [recovery] = list(tmp_path.glob(".index.jsx.mobius-old-*.bak"))
  assert recovery.read_bytes() == b"reviewed old\n"


def test_source_write_rollback_keeps_a_later_owner_deletion(tmp_path):
  from app.install import _run_rollback_actions, _write_source_file

  target = tmp_path / "index.jsx"
  target.write_bytes(b"reviewed old\n")
  rollback_actions = []
  commit_actions = []
  _write_source_file(
    target,
    b"candidate\n",
    rollback_actions,
    commit_actions,
    executable=False,
    expected_previous=b"reviewed old\n",
    expected_previous_executable=False,
  )
  target.unlink()

  _run_rollback_actions(rollback_actions)

  assert not target.exists()


def test_source_write_rollback_atomically_captures_a_racing_edit(
  tmp_path, monkeypatch,
):
  from app import install as install_module

  target = tmp_path / "index.jsx"
  target.write_bytes(b"reviewed old\n")
  rollback_actions = []
  commit_actions = []
  install_module._write_source_file(
    target,
    b"candidate\n",
    rollback_actions,
    commit_actions,
    executable=False,
    expected_previous=b"reviewed old\n",
    expected_previous_executable=False,
  )
  original_replace = install_module.os.replace
  injected = False

  def edit_then_capture(src, dst):
    nonlocal injected
    if (
      not injected
      and Path(src) == target
      and ".mobius-rollback-" in Path(dst).name
    ):
      injected = True
      target.write_bytes(b"owner racing edit\n")
    return original_replace(src, dst)

  monkeypatch.setattr(install_module.os, "replace", edit_then_capture)
  install_module._run_rollback_actions(rollback_actions)

  assert injected is True
  assert target.read_bytes() == b"owner racing edit\n"


def test_source_write_keeps_changed_capture_when_owner_recreates_target(
  tmp_path, monkeypatch,
):
  from app import install as install_module

  target = tmp_path / "index.jsx"
  target.write_bytes(b"reviewed old\n")
  original_link = install_module.os.link
  injected = False

  def recreate_before_candidate_link(src, dst, *args, **kwargs):
    nonlocal injected
    if (
      not injected
      and Path(dst) == target
      and ".mobius-new-" in Path(src).name
    ):
      injected = True
      [captured] = list(tmp_path.glob(".index.jsx.mobius-old-*.bak"))
      captured.write_bytes(b"owner edit through old inode\n")
      target.write_bytes(b"owner replacement\n")
    return original_link(src, dst, *args, **kwargs)

  monkeypatch.setattr(install_module.os, "link", recreate_before_candidate_link)
  with pytest.raises(HTTPException) as exc:
    install_module._write_source_file(
      target,
      b"candidate\n",
      [],
      [],
      executable=False,
      expected_previous=b"reviewed old\n",
      expected_previous_executable=False,
    )

  assert exc.value.status_code == 409
  assert target.read_bytes() == b"owner replacement\n"
  assert any(
    path.read_bytes() == b"owner edit through old inode\n"
    for path in tmp_path.glob(".index.jsx.mobius-old-*.bak")
  )


def test_source_write_link_failure_restores_previous_file_on_rollback(
  tmp_path, monkeypatch,
):
  """A publish failure after capture must leave rollback able to restore."""
  from app import install as install_module

  target = tmp_path / "index.jsx"
  target.write_bytes(b"reviewed old\n")
  rollback_actions = []
  commit_actions = []
  original_link = install_module.os.link

  def fail_candidate_link(src, dst, *args, **kwargs):
    if Path(dst) == target and ".mobius-new-" in Path(src).name:
      raise OSError(errno.ENOSPC, "synthetic full disk")
    return original_link(src, dst, *args, **kwargs)

  monkeypatch.setattr(install_module.os, "link", fail_candidate_link)
  with pytest.raises(OSError, match="synthetic full disk"):
    install_module._write_source_file(
      target,
      b"candidate\n",
      rollback_actions,
      commit_actions,
      executable=False,
      expected_previous=b"reviewed old\n",
      expected_previous_executable=False,
    )

  assert not target.exists()
  assert len(rollback_actions) == 1
  install_module._run_rollback_actions(rollback_actions)

  assert target.read_bytes() == b"reviewed old\n"
  assert list(tmp_path.glob(".index.jsx.mobius-*.bak")) == []


def test_source_write_rollback_needs_no_new_inode_after_disk_full(
  tmp_path, monkeypatch,
):
  """Rollback resources are reserved before publication can exhaust disk."""
  from app import install as install_module

  target = tmp_path / "index.jsx"
  target.write_bytes(b"reviewed old\n")
  rollback_actions = []
  commit_actions = []
  original_link = install_module.os.link
  original_mkstemp = install_module.tempfile.mkstemp
  exhausted = False

  def fail_candidate_link(src, dst, *args, **kwargs):
    nonlocal exhausted
    if Path(dst) == target and ".mobius-new-" in Path(src).name:
      exhausted = True
      raise OSError(errno.ENOSPC, "synthetic full disk")
    return original_link(src, dst, *args, **kwargs)

  def fail_late_allocation(*args, **kwargs):
    if exhausted:
      raise OSError(errno.ENOSPC, "disk remains full")
    return original_mkstemp(*args, **kwargs)

  monkeypatch.setattr(install_module.os, "link", fail_candidate_link)
  monkeypatch.setattr(install_module.tempfile, "mkstemp", fail_late_allocation)
  with pytest.raises(OSError, match="synthetic full disk"):
    install_module._write_source_file(
      target,
      b"candidate\n",
      rollback_actions,
      commit_actions,
      executable=False,
      expected_previous=b"reviewed old\n",
      expected_previous_executable=False,
    )

  install_module._run_rollback_actions(rollback_actions)

  assert target.read_bytes() == b"reviewed old\n"
  assert list(tmp_path.glob(".index.jsx.mobius-*.bak")) == []


def test_source_write_cleanup_retains_inode_opened_before_publication(tmp_path):
  from app.install import _write_source_file

  target = tmp_path / "index.jsx"
  target.write_bytes(b"reviewed old\n")
  rollback_actions = []
  commit_actions = []
  with target.open("r+b", buffering=0) as owner_handle:
    _write_source_file(
      target,
      b"candidate\n",
      rollback_actions,
      commit_actions,
      executable=False,
      expected_previous=b"reviewed old\n",
      expected_previous_executable=False,
    )
    for action in commit_actions:
      action()
    owner_handle.seek(0)
    owner_handle.write(b"owner late draft\n")
    owner_handle.truncate()

  [backup] = list(tmp_path.glob(".index.jsx.mobius-old-*.bak"))
  assert target.read_bytes() == b"candidate\n"
  assert backup.read_bytes() == b"owner late draft\n"


def test_failed_fresh_clone_retains_one_recovery_without_growth(tmp_path):
  from app.install import _retain_failed_fresh_clone

  def checkout(path: Path, owner_value: str) -> None:
    path.mkdir()
    app_git.ensure_repo(path)
    (path / "index.jsx").write_text("export default 1\n")
    app_git.commit_local(path, "accepted source")
    (path / "owner.txt").write_text(owner_value)

  source = tmp_path / "cards"
  checkout(source, "first")
  _retain_failed_fresh_clone(source)
  retained = tmp_path / ".cards.mobius-failed.bak"
  assert (retained / "owner.txt").read_text() == "first"

  checkout(source, "second")
  _retain_failed_fresh_clone(source)
  assert (source / "owner.txt").read_text() == "second"
  assert (retained / "owner.txt").read_text() == "first"
  assert list(tmp_path.glob(".cards.mobius-failed*.bak")) == [retained]


def test_prune_rollback_binds_each_captured_file_identity(tmp_path):
  from app.install import _prune_dropped_source_files, _run_rollback_actions

  repo = tmp_path / "app"
  repo.mkdir()
  app_git.ensure_repo(repo)
  (repo / "a.js").write_bytes(b"a reviewed\n")
  (repo / "z.js").write_bytes(b"z reviewed\n")
  _fixture_commit(repo, "reviewed files")
  rollback_actions = []
  commit_actions = []
  _prune_dropped_source_files(
    repo,
    {"a.js", "z.js"},
    rollback_actions,
    commit_actions,
    {"a.js": b"a reviewed\n", "z.js": b"z reviewed\n"},
    set(),
  )
  [a_backup] = list(repo.glob(".a.js.mobius-old-*.bak"))
  a_backup.write_bytes(b"z reviewed\n")
  (repo / "a.js").write_bytes(b"owner replacement\n")

  _run_rollback_actions(rollback_actions)

  assert (repo / "a.js").read_bytes() == b"owner replacement\n"
  assert a_backup.exists()
  assert a_backup.read_bytes() == b"z reviewed\n"


def test_prune_cleanup_retains_inode_opened_before_publication(tmp_path):
  from app.install import _prune_dropped_source_files

  repo = tmp_path / "app"
  repo.mkdir()
  app_git.ensure_repo(repo)
  target = repo / "old.js"
  target.write_bytes(b"reviewed old\n")
  _fixture_commit(repo, "reviewed file")
  rollback_actions = []
  commit_actions = []
  with target.open("r+b", buffering=0) as owner_handle:
    _prune_dropped_source_files(
      repo,
      {"old.js"},
      rollback_actions,
      commit_actions,
      {"old.js": b"reviewed old\n"},
      set(),
    )
    for action in commit_actions:
      action()
    owner_handle.seek(0)
    owner_handle.write(b"owner late draft\n")
    owner_handle.truncate()

  [backup] = list(repo.glob(".old.js.mobius-old-*.bak"))
  assert not target.exists()
  assert backup.read_bytes() == b"owner late draft\n"


def test_rollback_runs_every_action_after_one_fails(caplog):
  from app.install import _run_rollback_actions

  completed = []

  def fail():
    raise RuntimeError("synthetic rollback failure")

  _run_rollback_actions([
    lambda: completed.append("first"),
    fail,
    lambda: completed.append("last"),
  ])

  assert completed == ["last", "first"]
  assert "synthetic rollback failure" in caplog.text


def test_install_commit_failure_preserves_unselected_source_checkout(
  client, auth, db, bypass_url_validation, monkeypatch,
):
  """A failed row commit leaves no bundle/runtime and never rmtree's source."""
  from app import install as install_module

  base = "https://x.test/commit-failure/"
  manifest = {
    "id": "commit-failure",
    "name": "Commit failure",
    "version": "1.0.0",
    "description": "Transaction cleanup fixture",
    "entry": "index.jsx",
    "permissions": {},
  }
  responses = _check_responses(base, manifest, JSX)

  def fail_commit(_session):
    raise RuntimeError("synthetic commit failure")

  monkeypatch.setattr(install_module.Session, "commit", fail_commit)
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    failed = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert failed.status_code == 500, failed.text
  assert db.query(models.App).filter_by(slug="commit-failure").first() is None
  data_dir = Path(get_settings().data_dir)
  source = data_dir / "apps" / "commit-failure"
  assert not source.exists()
  retained = source.parent / ".commit-failure.mobius-failed.bak"
  assert app_git.is_repo(retained)
  assert (retained / "index.jsx").read_text(encoding="utf-8") == JSX
  assert not list((data_dir / "compiled").glob("app-*-*.js"))
  runtime = data_dir / "app-runtime"
  assert not runtime.exists() or not any(
    path.is_file() for path in runtime.rglob("*")
  )


def test_install_runtime_publish_failure_removes_staged_runtime(
  client, auth, db, bypass_url_validation, monkeypatch,
):
  """A failed runtime rename leaves no detached candidate behind."""
  from app import applied_app_runtime

  base = "https://x.test/runtime-publish-failure/"
  manifest = {
    "id": "runtime-publish-failure",
    "name": "Runtime publish failure",
    "version": "1.0.0",
    "description": "Runtime publication cleanup fixture",
    "entry": "index.jsx",
    "permissions": {},
  }
  staged_roots = []

  def fail_publish(_app, staged):
    staged_roots.append(staged.root)
    raise OSError("synthetic runtime publication failure")

  monkeypatch.setattr(applied_app_runtime, "publish_runtime", fail_publish)
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(_check_responses(base, manifest, JSX)),
  ):
    failed = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert failed.status_code == 500, failed.text
  assert staged_roots and all(not path.exists() for path in staged_roots)
  assert db.query(models.App).filter_by(
    slug="runtime-publish-failure",
  ).first() is None


# --- manifest_url is the new identity key (slug is routing only) ----


def test_install_with_same_slug_different_manifest_keeps_both(
  client, auth, bypass_url_validation,
):
  """A user-built app and a store-installed app may want the same
  slug stem. After the manifest_url refactor, identity is keyed on
  manifest_url, so the store install must NOT clobber the user app —
  it lands as a fresh row with slug='news-2' (or similar) instead."""
  # 1. User builds an app named "News" via the regular create path.
  user_app = create_local_app(
    client, auth, name="News", description="user-built news reader",
    jsx_source=JSX,
  )
  user_id = user_app["id"]
  assert user_app["slug"] == "news"
  assert user_app["manifest_url"] is None

  # 2. Store installs a manifest whose id is also "news".
  base = "https://packages.test/x/app-news/main/"
  manifest = {**MANIFEST_NEWS, "id": "news", "name": "News"}
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  installed = r.json()
  # Must be a fresh install (NOT an update of the user's app).
  assert installed["mode"] == "install"
  assert installed["id"] != user_id
  # Slug collided so allocate_unique_slug bumped it.
  assert installed["slug"] != "news"
  assert installed["slug"].startswith("news-")
  # `manifest_url` is stored in the canonical identity-key shape so
  # the same app installed via inline-manifest + raw_base lands on
  # the same row instead of duplicating.
  assert installed["manifest_url"] == (
    base.rstrip("/") + "#manifest-id=news"
  )

  # Telemetry fired: the requested slug ("news") collided with the
  # user-built app, so the installer logged requested-vs-assigned. The
  # install still succeeded — this is observability, not a behavior change.
  collisions = [e for e in _read_activity() if e["ev"] == "slug_collision"]
  assert len(collisions) == 1
  assert collisions[0]["requested_slug"] == "news"
  assert collisions[0]["assigned_slug"] == installed["slug"]

  # User's app is untouched.
  r_user = client.get(f"/api/apps/{user_id}", headers=auth)
  assert r_user.status_code == 200
  preserved = r_user.json()
  assert preserved["name"] == "News"
  assert preserved["slug"] == "news"
  assert preserved["manifest_url"] is None


def test_trusted_catalog_adopts_legacy_row_only_with_matching_git_origin(
  client, auth, db,
):
  """A pre-identity catalog app is adopted without weakening slug safety."""
  from app import install

  legacy = create_local_app(
    client, auth, name="Codex", description="legacy Subagents", jsx_source=JSX,
  )
  repo = Path(legacy["source_dir"])
  subprocess.run(
    [
      "git", "-C", str(repo), "remote", "add", "origin",
      "https://github.com/mobius-os/app-subagents.git",
    ],
    check=True,
  )
  db.expire_all()

  matched = install._find_install_identity_row(
    db,
    source_url=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-subagents/main/mobius.json"
    ),
    manifest_id="codex",
  )
  assert matched is not None
  assert matched.id == legacy["id"]

  unrelated = install._find_install_identity_row(
    db,
    source_url=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-something-else/main/mobius.json"
    ),
    manifest_id="codex",
  )
  assert unrelated is None

  matched.deleted_at = datetime.now(UTC)
  db.commit()
  tombstone = install._find_install_identity_row(
    db,
    source_url=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-subagents/main/mobius.json"
    ),
    manifest_id="codex",
  )
  assert tombstone is not None
  assert tombstone.id == legacy["id"]


def test_trusted_origin_adoption_is_always_reconciled_as_local_source(
  client, auth, db,
):
  """A proven legacy identity must still preserve its local source tree."""
  from app import install

  legacy = create_local_app(
    client, auth, name="Codex", description="legacy Subagents", jsx_source=JSX,
  )
  repo = Path(legacy["source_dir"])
  subprocess.run(
    [
      "git", "-C", str(repo), "remote", "add", "origin",
      "https://github.com/mobius-os/app-subagents.git",
    ],
    check=True,
  )
  db.expire_all()
  candidate = install.InstallCandidate(
    manifest={**MANIFEST_NEWS, "id": "codex", "name": "Subagents"},
    raw_base=(
      "https://raw.githubusercontent.com/mobius-os/app-subagents/main/"
    ),
    entry_bytes=JSX.encode(),
    icon_processed=None,
    icon_warning=None,
    bundled_job=None,
    static_assets={},
    source_files={},
    seeds={},
    capability_contract={},
    capability_digest="a" * 64,
    candidate_digest="b" * 64,
    source_review_digest="c" * 64,
  )
  target = install._select_install_target(
    db,
    candidate=candidate,
    manifest_url=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-subagents/main/mobius.json"
    ),
    source="store",
    expected_app_id=None,
  )
  assert target.existing is not None
  assert target.existing.id == legacy["id"]
  assert target.mode == "update"
  assert target.adopting_trusted_origin is True


def test_genuine_repo_without_upstream_never_overwrites_owner_history(
  client, auth, db, tmp_path, bypass_url_validation, monkeypatch,
):
  """Unknown provenance must share history or fail before source promotion."""
  manifest_url = (
    "https://raw.githubusercontent.com/mobius-os/"
    "app-owner-history/main/mobius.json"
  )
  raw_base = manifest_url.rsplit("/", 1)[0] + "/"
  manifest = {
    "id": "owner-history", "name": "Owner History", "version": "2.0.0",
    "description": "Published package", "entry": "index.jsx",
    "permissions": {},
  }
  legacy = create_local_app(
    client, auth, name="Owner History", description="Owner source",
    jsx_source="export default () => <div>owner</div>\n",
  )
  repo = Path(legacy["source_dir"])
  owner_head = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  app_git._run(repo, "branch", "-D", app_git.UPSTREAM_BRANCH)
  row = db.get(models.App, legacy["id"])
  row.manifest_url = None
  row.upstream_commit = None
  db.commit()

  remote_work = tmp_path / "owner-history-work"
  remote_bare = tmp_path / "owner-history.git"
  subprocess.run(
    ["git", "init", "-q", "-b", "main", str(remote_work)], check=True,
  )
  (remote_work / "mobius.json").write_text(json.dumps(manifest))
  (remote_work / "index.jsx").write_text(
    "export default () => <div>store</div>\n",
  )
  _fixture_commit(remote_work, "published package")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(remote_work), str(remote_bare)],
    check=True,
  )
  subprocess.run(
    [
      "git", "-C", str(repo), "remote", "add", "origin",
      "https://github.com/mobius-os/app-owner-history.git",
    ],
    check=True,
  )
  monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
  monkeypatch.setenv(
    "GIT_CONFIG_KEY_0",
    "url.%s.insteadOf" % remote_bare.as_uri(),
  )
  monkeypatch.setenv(
    "GIT_CONFIG_VALUE_0",
    "https://github.com/mobius-os/app-owner-history.git",
  )

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client({
      manifest_url: (200, json.dumps(manifest).encode()),
      raw_base + "index.jsx": (
        200, b"export default () => <div>store</div>\n",
      ),
    }),
  ):
    result = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": manifest_url},
    )

  assert result.status_code == 409, result.text
  assert result.json()["detail"]["code"] == "git_update_unavailable"
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == owner_head
  assert (repo / "index.jsx").read_text() == (
    "export default () => <div>owner</div>\n"
  )


def test_trusted_origin_first_adoption_conflict_reviews_and_finalizes(
  client, auth, db, tmp_path, bypass_url_validation,
):
  """A proven local app stays unprivileged until its conflict is accepted."""
  from app import install

  original = (
    'const title = "ORIGINAL";\n'
    'export default function App() { return <div>{title}</div> }\n'
  )
  local = original.replace("ORIGINAL", "LOCAL")
  incoming = original.replace("ORIGINAL", "STORE")
  resolved = original.replace("ORIGINAL", "RESOLVED")
  manifest_id = "trusted-adoption"
  manifest_url = (
    "https://raw.githubusercontent.com/mobius-os/"
    "app-trusted-adoption/main/mobius.json"
  )
  raw_base = manifest_url.rsplit("/", 1)[0] + "/"
  manifest = {
    "id": manifest_id,
    "name": "Trusted Adoption",
    "version": "1.0.0",
    "description": "Published package",
    "entry": "index.jsx",
    "permissions": {"manage_apps": True, "connect_manage": True},
  }
  legacy = create_local_app(
    client,
    auth,
    name="Trusted Adoption",
    description="Local source",
    jsx_source=original,
  )
  app_id = legacy["id"]
  repo = Path(legacy["source_dir"])
  (repo / "index.jsx").write_text(local, encoding="utf-8")
  origin_work = tmp_path / "trusted-adoption-work"
  origin_bare = tmp_path / "trusted-adoption.git"
  subprocess.run(
    ["git", "init", "-q", "-b", "main", str(origin_work)], check=True,
  )
  (origin_work / "mobius.json").write_text(
    json.dumps(manifest), encoding="utf-8",
  )
  (origin_work / "index.jsx").write_text(incoming, encoding="utf-8")
  _fixture_commit(origin_work, "published package")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(origin_work), str(origin_bare)],
    check=True,
  )
  subprocess.run(
    [
      "git", "-C", str(repo), "remote", "add", "origin",
      origin_bare.as_uri(),
    ],
    check=True,
  )
  responses = {
    manifest_url: (200, json.dumps(manifest).encode()),
    raw_base + "index.jsx": (200, incoming.encode()),
  }

  # Identity remains the canonical catalog URL while the local bare remote
  # supplies the exact Git commit without external network access.
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), patch(
    "app.install.app_git.origin_url",
    return_value="https://github.com/mobius-os/app-trusted-adoption.git",
  ):
    conflicted = client.post(
      "/api/apps/install",
      headers=auth,
      json={"manifest_url": manifest_url},
    )

  assert conflicted.status_code == 201, conflicted.text
  assert conflicted.json()["mode"] == "conflict"
  assert conflicted.json()["id"] == app_id
  assert (repo / "index.jsx").read_text(encoding="utf-8") == local
  app_git._run(
    repo, "remote", "set-url", "origin",
    "https://github.com/mobius-os/app-trusted-adoption.git",
  )
  db.expire_all()
  row = db.query(models.App).filter(models.App.id == app_id).one()
  assert row.manifest_url is None
  assert row.manage_apps is False
  assert row.connect_manage is False

  selected = client.post(
    "/api/apps/resolve-update/policy",
    headers=auth,
    json={"source_dir": str(repo), "policy": "preserve_local"},
  )
  assert selected.status_code == 200, selected.text
  assert app_git.rebase_in_progress(repo)
  (repo / "index.jsx").write_text(resolved, encoding="utf-8")
  (repo / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  _finish_materialized_rebase(repo)

  reviewed = client.post(
    "/api/apps/resolve-update/review",
    headers=auth,
    json={"source_dir": str(repo)},
  )
  assert reviewed.status_code == 200, reviewed.text
  assert "RESOLVED" in reviewed.json()["diff"]

  db.expire_all()
  row = db.query(models.App).filter(models.App.id == app_id).one()
  assert row.manifest_url is None
  assert row.manage_apps is False
  assert row.connect_manage is False

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client({raw_base + "index.jsx": (200, incoming.encode())}),
  ):
    finalized = client.post(
      "/api/apps/resolve-update",
      headers=auth,
      json={
        "source_dir": str(repo),
        "reviewed_tree_oid": reviewed.json()["tree_oid"],
      },
    )

  assert finalized.status_code == 200, finalized.text
  assert finalized.json()["mode"] == "updated"
  assert finalized.json()["app"]["id"] == app_id
  assert (repo / "index.jsx").read_text(encoding="utf-8") == resolved
  db.expire_all()
  row = db.query(models.App).filter(models.App.id == app_id).one()
  assert row.manifest_url == (
    raw_base.rstrip("/") + f"#manifest-id={manifest_id}"
  )
  assert row.manage_apps is True
  assert row.connect_manage is True
  assert install.read_pending_conflict_update_receipt(
    repo,
    app_id=app_id,
    upstream_commit=row.upstream_commit,
  ) is None


def test_trusted_origin_pending_adoption_rejects_mismatched_and_stale_receipts(
  client, auth, db,
):
  """A receipt cannot attach another package or outlive its upstream proof."""
  from app import install

  legacy = create_local_app(
    client,
    auth,
    name="Receipt Guard",
    description="Local source",
    jsx_source="export default function App(){return <div>local</div>}\n",
  )
  app_id = legacy["id"]
  repo = Path(legacy["source_dir"])
  subprocess.run(
    [
      "git", "-C", str(repo), "remote", "add", "origin",
      "https://github.com/mobius-os/app-receipt-guard.git",
    ],
    check=True,
  )
  row = db.query(models.App).filter(models.App.id == app_id).one()
  assert row.manifest_url is None
  row.upstream_commit = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  db.commit()
  assert row.upstream_commit
  upstream_commit = row.upstream_commit
  manifest = {
    "id": "receipt-guard",
    "name": "Receipt Guard",
    "version": "1.0.0",
    "description": "Published package",
    "entry": "index.jsx",
    "permissions": {"manage_apps": True},
  }

  install.stage_pending_conflict_update(
    repo,
    app_id=app_id,
    upstream_commit=upstream_commit,
    manifest=manifest,
    raw_base=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-another-package/main/"
    ),
    capability_digest="a" * 64,
    candidate_digest="b" * 64,
  )
  mismatched = client.post(
    "/api/apps/resolve-update/policy",
    headers=auth,
    json={"source_dir": str(repo), "policy": "preserve_local"},
  )
  assert mismatched.status_code == 409, mismatched.text
  assert mismatched.json()["detail"]["code"] == "pending_update_identity_changed"
  assert not (repo / ".git" / "MERGE_HEAD").exists()

  install.stage_pending_conflict_update(
    repo,
    app_id=app_id,
    upstream_commit="0" * 40,
    manifest=manifest,
    raw_base=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-receipt-guard/main/"
    ),
    capability_digest="a" * 64,
    candidate_digest="b" * 64,
  )
  stale = client.post(
    "/api/apps/resolve-update/policy",
    headers=auth,
    json={"source_dir": str(repo), "policy": "preserve_local"},
  )
  assert stale.status_code == 409, stale.text
  assert not (repo / ".git" / "MERGE_HEAD").exists()
  db.expire_all()
  row = db.query(models.App).filter(models.App.id == app_id).one()
  assert row.manifest_url is None
  assert row.manage_apps is False


def test_trusted_origin_adoption_accepts_equal_tree_with_unrelated_history(
  client, auth, db, bypass_url_validation, tmp_path,
):
  """Equal trusted-origin bytes replace unrelated legacy provenance."""
  manifest = {
    "id": "codex",
    "name": "Subagents",
    "version": "1.0.0",
    "description": "trusted legacy app",
    "entry": "index.jsx",
    "permissions": {},
  }
  legacy = create_local_app(
    client,
    auth,
    name="Codex",
    description="trusted legacy app",
    jsx_source=JSX,
    manifest_extra={"version": "1.0.0"},
  )
  repo = Path(legacy["source_dir"])

  # Model Atlas's exact legacy shape. The installer-owned upstream branch has
  # synthetic history + a generated .gitignore, while a prior explicit apply
  # rewrote main onto the canonical repository's complete tree. The configured
  # origin proves package identity even though neither history shares a root.
  fixture = tmp_path / "trusted-origin"
  bare = tmp_path / "trusted-origin.git"
  fixture.mkdir()
  subprocess.run(
    ["git", "init", "-q", "-b", "main", str(fixture)], check=True,
  )
  canonical_files = {
    ".gitignore": "node_modules/\ntests/.build/\n",
    "README.md": "canonical package data\n",
    "index.jsx": JSX,
    "mobius.json": json.dumps(manifest, sort_keys=True) + "\n",
  }
  for rel, content in canonical_files.items():
    (fixture / rel).write_text(content, encoding="utf-8")
  subprocess.run(["git", "-C", str(fixture), "add", "-A"], check=True)
  subprocess.run(
    [
      "git", "-c", "user.name=Mobius", "-c",
      "user.email=mobius@localhost", "-C", str(fixture),
      "commit", "-q", "-m", "canonical release",
    ],
    check=True,
  )
  canonical_head = subprocess.run(
    ["git", "-C", str(fixture), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
  ).stdout.strip()
  canonical_tree = subprocess.run(
    ["git", "-C", str(fixture), "rev-parse", "HEAD^{tree}"],
    capture_output=True, text=True, check=True,
  ).stdout.strip()
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(fixture), str(bare)],
    check=True,
  )
  subprocess.run(
    ["git", "-C", str(repo), "remote", "add", "origin", bare.as_uri()],
    check=True,
  )
  subprocess.run(
    ["git", "-C", str(repo), "fetch", "-q", "origin", "main"], check=True,
  )

  synthetic_upstream = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "upstream"],
    capture_output=True, text=True, check=True,
  ).stdout.strip()
  # An orphan commit preserves the canonical tree but intentionally shares no
  # ancestry with the installer-owned upstream branch.
  rewritten_main = subprocess.run(
    [
      "git", "-c", "user.name=Mobius", "-c",
      "user.email=mobius@localhost", "-C", str(repo),
      "commit-tree", canonical_tree, "-m", "apply app source",
    ],
    capture_output=True, text=True, check=True,
  ).stdout.strip()
  subprocess.run(
    ["git", "-C", str(repo), "update-ref", "refs/heads/main", rewritten_main],
    check=True,
  )
  subprocess.run(
    ["git", "-C", str(repo), "reset", "-q", "--hard", "main"], check=True,
  )
  assert subprocess.run(
    ["git", "-C", str(repo), "merge-base", "main", "upstream"],
    capture_output=True,
  ).returncode != 0
  assert subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "main^{tree}"],
    capture_output=True, text=True, check=True,
  ).stdout.strip() == canonical_tree

  expected_origin = "https://github.com/mobius-os/app-subagents.git"

  manifest_url = (
    "https://raw.githubusercontent.com/mobius-os/"
    "app-subagents/main/mobius.json"
  )
  raw_base = manifest_url.rsplit("/", 1)[0] + "/"
  responses = {
    manifest_url: (200, json.dumps(manifest).encode()),
    raw_base + "index.jsx": (200, JSX.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ), patch(
    "app.install.app_git.origin_url",
    return_value=expected_origin,
  ):
    adopted = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": manifest_url},
    )

  assert adopted.status_code == 201, adopted.text
  payload = adopted.json()
  assert payload["id"] == legacy["id"]
  assert payload["mode"] == "update"
  assert payload["manifest_url"] == (
    raw_base.rstrip("/") + "#manifest-id=codex"
  )
  assert payload["divergence"] == "fast_forward"
  assert subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "upstream"],
    capture_output=True, text=True, check=True,
  ).stdout.strip() == canonical_head
  assert subprocess.run(
    ["git", "-C", str(repo), "merge-base", "--is-ancestor", canonical_head, "main"],
  ).returncode == 0
  assert subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "main^{tree}"],
    capture_output=True, text=True, check=True,
  ).stdout.strip() == canonical_tree
  assert subprocess.run(
    ["git", "-C", str(repo), "status", "--porcelain"],
    capture_output=True, text=True, check=True,
  ).stdout == ""
  assert (repo / "README.md").read_text() == canonical_files["README.md"]
  assert (repo / ".gitignore").read_text() == canonical_files[".gitignore"]
  assert subprocess.run(
    ["git", "-C", str(repo), "merge-base", "--is-ancestor", synthetic_upstream, "main"],
  ).returncode != 0


def test_install_same_manifest_twice_updates(
  client, auth, bypass_url_validation,
):
  """Re-installing the same manifest_url updates the existing row
  in place (mode='update', same id) — identity now keyed on URL."""
  base = "https://packages.test/x/app-same/main/"
  manifest = {**MANIFEST_NEWS, "id": "same-manifest"}
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201, r1.text
  first = r1.json()
  assert first["mode"] == "install"
  first_id = first["id"]
  # The literal URL gets folded into the canonical identity shape
  # before it lands in the column. See `_canonical_identity_key`.
  canonical = base.rstrip("/") + "#manifest-id=same-manifest"
  assert first["manifest_url"] == canonical

  # Second install of the same manifest_url.
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code == 201, r2.text
  second = r2.json()
  assert second["mode"] == "update"
  assert second["id"] == first_id
  assert second["manifest_url"] == canonical


# --------------------------------------------------------------------------
# Tests for the install-authority gate on /api/apps/install
# (post-073 — the App Store mini-app drives installs via its
# app-scoped JWT instead of the owner JWT it doesn't hold).
#
# Three branches:
#   1. App row carries manage_apps=True             → accept (canonical).
#   2. App row carries cross_app_access='write'     → accept (TRANSITIONAL
#      fallback so pre-073 installs of the app-store keep working until
#      they update; logs a deprecation warning).
#   3. Neither granted                              → 403 with an error
#      that names manage_apps as the canonical permission.
# --------------------------------------------------------------------------

def _seed_app_with_perms(
  db,
  perms_cross_write: str = "none",
  manage_apps: bool = False,
):
  """Insert an App row with the given install-authority shape, return id."""
  from app import models
  app = models.App(
    name="test-installer",
    description="",
    jsx_source="export default function App() { return null }",
    source_dir="/tmp/test-installer",
    slug="test-installer",
    manifest_url="https://example/test-installer/mobius.json",
    cross_app_access=perms_cross_write,
    share_with_apps="none",
    offline_capable=False,
    manage_apps=manage_apps,
  )
  db.add(app)
  db.flush()
  return app.id


def _install_responses(base):
  """Stock manifest+entry pair for happy-path install tests."""
  return {
    base + "mobius.json": (200, json.dumps({
      "id": "installable",
      "name": "Installable",
      "version": "1.0.0",
      "description": "x",
      "author": "x",
      "license": "MIT",
      "homepage": "https://example",
      "entry": "index.jsx",
      "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
  }


def test_install_accepts_opaque_app_frame_with_manage_apps(
  client, db, owner_token, bypass_url_validation,
):
  """The Store's opaque frame may install with its scoped capability."""
  # owner_token is requested for its side-effect: it creates the Owner
  # row with sub='test' that the minted app-scoped JWT below resolves
  # against. Without it the dep returns 401 "Owner not found."
  from app.auth import create_access_token
  app_id = _seed_app_with_perms(db, perms_cross_write="none", manage_apps=True)
  db.commit()
  token = create_access_token({"sub": "test", "scope": "app", "app_id": app_id})

  base = "https://packages.test/x/app-installable/main/"
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(_install_responses(base)),
  ):
    r = client.post(
      "/api/apps/install",
      headers={
        "Authorization": f"Bearer {token}",
        "Origin": "null",
        "Sec-Fetch-Site": "cross-site",
      },
      json={"manifest_url": base + "mobius.json"},
    )
  assert r.status_code == 201, r.text
  # `*`, not the echoed `null`: WebKit refuses to match the literal value
  # and blocks the response before the app frame sees it (see
  # test_opaque_origin_cors.py).
  assert r.headers["access-control-allow-origin"] == "*"


def test_install_rejects_app_token_with_cross_write_but_no_manage_apps(
  client, db, owner_token, bypass_url_validation,
):
  """cross_app_access='write' alone is NOT enough — manage_apps is the
  install-authority key now. Apps that want to drive installs must
  declare permissions.manage_apps=true in their manifest."""
  from app.auth import create_access_token
  app_id = _seed_app_with_perms(db, perms_cross_write="write", manage_apps=False)
  db.commit()
  token = create_access_token({"sub": "test", "scope": "app", "app_id": app_id})

  r = client.post(
    "/api/apps/install",
    headers={"Authorization": f"Bearer {token}"},
    json={"manifest_url": "https://x/y/mobius.json"},
  )
  assert r.status_code == 403, r.text
  assert "manage_apps" in r.json()["detail"].lower()


def test_install_rejects_app_token_with_cross_read(
  client, db, owner_token, bypass_url_validation,
):
  """cross_app_access='read' alone is not install authority."""
  from app.auth import create_access_token
  app_id = _seed_app_with_perms(db, perms_cross_write="read", manage_apps=False)
  db.commit()
  token = create_access_token({"sub": "test", "scope": "app", "app_id": app_id})

  r = client.post(
    "/api/apps/install",
    headers={"Authorization": f"Bearer {token}"},
    json={"manifest_url": "https://x/y/mobius.json"},
  )
  assert r.status_code == 403, r.text
  assert "manage_apps" in r.json()["detail"].lower()


def test_install_rejects_app_token_with_cross_none(
  client, db, owner_token, bypass_url_validation,
):
  """Default-perms app (cross_app_access='none', manage_apps=False) is denied."""
  from app.auth import create_access_token
  app_id = _seed_app_with_perms(db, perms_cross_write="none", manage_apps=False)
  db.commit()
  token = create_access_token({"sub": "test", "scope": "app", "app_id": app_id})

  r = client.post(
    "/api/apps/install",
    headers={"Authorization": f"Bearer {token}"},
    json={"manifest_url": "https://x/y/mobius.json"},
  )
  assert r.status_code == 403, r.text


# --- SystemBroadcast notification on install/update -----------------


def test_install_publishes_app_updated_on_success(
  client, auth, bypass_url_validation,
):
  """Shell drawer auto-refresh: a successful install must emit an
  `app_updated` SystemBroadcast event with the new app's id.
  Without this the Shell only learns about the new app on the next
  page reload — which is exactly the "install succeeded but the
  drawer is empty" failure the app-store currently reports."""
  base = "https://x.test/notify/"
  responses = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "id": "notify-target",
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), patch("app.routes.apps.get_system_broadcast") as mock_get_sb:
    fake_sb = MagicMock()
    mock_get_sb.return_value = fake_sb
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]
  # Exactly one publish for this install — and it carries the new
  # app's id as a string (the stable app lifecycle event shape).
  fake_sb.publish.assert_called_once_with({
    "type": "app_updated", "appId": str(app_id),
  })


def test_install_does_not_publish_when_install_fails(
  client, auth, bypass_url_validation,
):
  """No SSE event when the install rolls back — the Shell would
  refetch only to find the row absent, but emitting an event for a
  non-event is noise. install_from_manifest raises before we reach
  the publish call, so the assertion is on `not_called`."""
  base = "https://x.test/fail-notify/"
  responses = {
    base + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "id": "fail-notify",
    }).encode()),
    base + "index.jsx": (200, b"this is not valid JSX <<>>"),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), patch("app.routes.apps.get_system_broadcast") as mock_get_sb:
    fake_sb = MagicMock()
    mock_get_sb.return_value = fake_sb
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code in (422, 500), r.text
  fake_sb.publish.assert_not_called()


def test_delete_publishes_exact_app_deleted_event(client, auth):
  """Uninstall publishes committed deletion evidence, not a staleable refetch
  hint, so every live shell can remove the exact drawer row immediately."""
  app_id = create_local_app(
    client, auth, name="Doomed", description="", jsx_source=JSX,
  )["id"]
  with patch("app.routes.apps.get_system_broadcast") as mock_get_sb:
    fake_sb = MagicMock()
    mock_get_sb.return_value = fake_sb
    r = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert r.status_code == 204, r.text
  fake_sb.publish.assert_called_once_with({
    "type": "app_deleted", "appId": str(app_id),
  })


def test_delete_stays_successful_after_post_commit_job_cleanup_failure(
  client, auth,
):
  """A committed tombstone remains a successful deletion when cleanup fails."""
  app_id = create_local_app(
    client, auth, name="Cleanup Failure", description="", jsx_source=JSX,
  )["id"]

  with (
    patch(
      "app.routes.apps.app_jobs.terminate_app_jobs",
      side_effect=RuntimeError("cleanup failed"),
    ),
    patch("app.routes.apps.get_system_broadcast") as mock_get_sb,
  ):
    fake_sb = MagicMock()
    mock_get_sb.return_value = fake_sb
    deleted = client.delete(f"/api/apps/{app_id}", headers=auth)

  assert deleted.status_code == 204, deleted.text
  assert client.get(f"/api/apps/{app_id}", headers=auth).status_code == 404
  fake_sb.publish.assert_called_once_with({
    "type": "app_deleted", "appId": str(app_id),
  })


def test_recover_stays_successful_after_post_commit_skill_cleanup_failure(
  client, auth,
):
  """Ancillary restoration cannot make a durably recovered app look failed."""
  app_id = create_local_app(
    client,
    auth,
    name="Recovery Cleanup Failure",
    description="",
    jsx_source=JSX,
  )["id"]
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204

  with patch(
    "app.install.restore_app_skills",
    new=AsyncMock(side_effect=RuntimeError("restore failed")),
  ):
    recovered = client.post(f"/api/apps/{app_id}/recover", headers=auth)

  assert recovered.status_code == 200, recovered.text
  assert client.get(f"/api/apps/{app_id}", headers=auth).status_code == 200


# --- Per-app git model (feature 084) ---------------------------------
# The flag is OFF by default, so every test above runs the legacy
# overwrite path. These pin both halves of the contract: OFF is
# byte-identical to today (no .git anywhere), ON engages the merge model.

# A multi-line component with the two editable regions (title near the
# top, footer near the bottom) separated by several unchanged lines.
# git's line-based 3-way merge needs unchanged context BETWEEN two hunks
# to interleave them cleanly — adjacent single-line edits conflict even
# when "logically" disjoint, so the spacing here is deliberate.
JSX_MULTI = (
  "export default function App() {\n"
  "  const title = 'ORIGINAL TITLE'\n"
  "  const a = 1\n"
  "  const b = 2\n"
  "  const c = 3\n"
  "  const d = 4\n"
  "  const e = 5\n"
  "  const footer = 'ORIGINAL FOOTER'\n"
  "  return <div>{title}{footer}{a}{b}{c}{d}{e}</div>\n"
  "}\n"
)


def _install_v1(client, auth, base, manifest, jsx):
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, jsx.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


def _seed_legacy_catalog_app(
  client, auth, base, manifest, jsx, *, source_files=None,
):
  """Create pre-Git-source catalog state without using today's Store path."""
  from app import install
  from app.database import SessionLocal
  from app.models import App

  source_files = dict(source_files or {})
  schedule = manifest.get("schedule")
  if isinstance(schedule, dict) and schedule.get("job"):
    source_files.setdefault(schedule["job"], b"#!/bin/sh\n")
  for declared in (manifest.get("storage_seeds") or {}).values():
    if isinstance(declared, str):
      source_files.setdefault(declared, PROMPT.encode())
  if manifest.get("icon"):
    source_files.setdefault(manifest["icon"], _png_bytes())
  source_dir = Path(get_settings().data_dir) / "apps" / manifest["id"]
  created = create_local_app(
    client,
    auth,
    name=manifest["name"],
    description=manifest["description"],
    # Create the row with a self-contained source first; legacy fixtures may
    # import sibling files that are written immediately below.
    jsx_source=JSX,
    source_dir=source_dir,
  )
  (source_dir / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  for rel, content in source_files.items():
    target = source_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if isinstance(content, bytes) else content.encode())
    if isinstance(schedule, dict) and rel == schedule.get("job"):
      target.chmod(0o755)
  applied = client.post(
    "/api/apps/apply", headers=auth, json={"source_dir": str(source_dir)},
  )
  assert applied.status_code == 200, applied.text
  tree = {
    "mobius.json": json.dumps(manifest).encode(),
    manifest["entry"]: jsx.encode(),
    **{
      rel: content if isinstance(content, bytes) else content.encode()
      for rel, content in source_files.items()
    },
  }
  upstream = app_git.record_upstream(
    source_dir, tree, base, str(manifest.get("version") or "unknown"),
    exec_paths=frozenset(
      {schedule["job"]}
      if isinstance(schedule, dict) and schedule.get("job") else set()
    ),
  )
  app_git.align_local_to_upstream(source_dir)
  db = SessionLocal()
  try:
    row = db.get(App, created["id"])
    assert row is not None
    row.manifest_url = install._canonical_identity_key(base, manifest["id"])
    row.upstream_commit = upstream
    row.upstream_jsx_sha = hashlib.sha256(jsx.encode()).hexdigest()
    db.commit()
  finally:
    db.close()
  return created


def _update_v2(client, auth, base, manifest, jsx):
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, jsx.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v2 prompt"),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


def test_pending_update_receipt_upgrades_schema_one_on_policy_choice(tmp_path):
  """A rolling restart preserves old pending conflicts until owner choice."""
  from app import install

  source = tmp_path / "legacy-pending"
  pending = source / ".git" / "mobius-pending-update"
  pending.mkdir(parents=True)
  receipt_path = pending / "receipt.json"
  receipt_path.write_text(json.dumps({
    "schema": 1,
    "app_id": 42,
    "upstream_commit": "a" * 40,
    "manifest": {"id": "legacy"},
    "raw_base": "https://legacy.test/repo/",
    "capability_digest": "capability",
    "candidate_digest": "b" * 64,
  }))

  legacy = install.read_pending_conflict_update_receipt(
    source,
    app_id=42,
    upstream_commit="a" * 40,
  )
  assert legacy is not None
  assert legacy["resolution_policy"] is None
  install.set_pending_conflict_update_policy(
    source,
    app_id=42,
    upstream_commit="a" * 40,
    policy="preserve_local",
  )
  upgraded = json.loads(receipt_path.read_text())
  assert upgraded["schema"] == 2
  assert upgraded["resolution_policy"] == "preserve_local"
  assert upgraded["reviewed_tree_oid"] is None


@pytest.mark.asyncio
async def test_legacy_candidate_digest_requires_the_exact_git_commit(monkeypatch):
  from app import install

  manifest = {
    "id": "legacy-digest",
    "name": "Legacy digest",
    "version": "2.0.0",
    "description": "Legacy pending update",
    "entry": "index.jsx",
    "permissions": {},
  }
  raw_base = "https://raw.githubusercontent.com/acme/legacy/main/"
  commit = "a" * 40
  entry = b"export default function App() { return null }\n"
  snapshot = install.GitPackageSnapshot(
    commit=commit,
    tree={"mobius.json": json.dumps(manifest).encode(), "index.jsx": entry},
  )
  legacy_digest = install._install_candidate_digest(
    manifest=manifest,
    raw_base=raw_base,
    source_identity=None,
    predecessor_source_identity=None,
    canonical_source_url=raw_base,
    upstream_commit="",
    entry_bytes=entry,
    icon_processed=None,
    bundled_job=None,
    static_assets={},
    source_files={},
    seeds={},
  )
  monkeypatch.setattr(
    install,
    "_prepare_git_package_source",
    AsyncMock(return_value=(manifest, raw_base, snapshot)),
  )

  candidate = await install.prepare_install_candidate(
    manifest_url=raw_base + "mobius.json",
    manifest=None,
    raw_base=None,
    reviewed_capability_digest=None,
    reviewed_source_digest=None,
    expected_app_id=7,
    expected_upstream_commit=commit,
    expected_candidate_digest=legacy_digest,
  )
  assert candidate.upstream_commit == commit
  assert candidate.candidate_digest != legacy_digest

  with pytest.raises(HTTPException) as raised:
    await install.prepare_install_candidate(
      manifest_url=raw_base + "mobius.json",
      manifest=None,
      raw_base=None,
      reviewed_capability_digest=None,
      reviewed_source_digest=None,
      expected_app_id=7,
      expected_upstream_commit="b" * 40,
      expected_candidate_digest=legacy_digest,
    )
  assert raised.value.status_code == 409
  assert raised.value.detail["code"] == "pending_update_changed"


@pytest.mark.asyncio
async def test_schema_one_preserve_local_persists_base_before_rebase(
  tmp_path, monkeypatch,
):
  from app import install
  from app.routes import apps as app_routes

  repo = tmp_path / "legacy-resolution"
  app_git.record_upstream(
    repo, {"index.jsx": b"base\n"}, "https://example.test/app/", "1",
  )
  app_git.align_local_to_upstream(repo)
  replay_base = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  (repo / "local.js").write_bytes(b"local\n")
  assert app_git.commit_local(repo, "local edit")
  upstream = app_git.record_upstream(
    repo, {"index.jsx": b"upstream\n"},
    "https://example.test/app/", "2",
  )
  pending = repo / ".git" / "mobius-pending-update"
  pending.mkdir(parents=True)
  receipt_path = pending / "receipt.json"
  receipt_path.write_text(json.dumps({
    "schema": 1,
    "app_id": 42,
    "upstream_commit": upstream,
    "manifest": {"id": "legacy-resolution"},
    "raw_base": "https://example.test/app/",
    "capability_digest": "capability",
    "candidate_digest": "b" * 64,
  }))
  receipt = install.read_pending_conflict_update_receipt(
    repo, app_id=42, upstream_commit=upstream,
  )
  observed = {}

  def observe_rebase(_repo, *, base, onto):
    observed.update(base=base, onto=onto, receipt=json.loads(
      receipt_path.read_text(encoding="utf-8")
    ))
    return []

  monkeypatch.setattr(app_git, "start_overlay_rebase", observe_rebase)
  conflicts = await app_routes._apply_update_resolution_policy(
    SimpleNamespace(id=42, upstream_commit=upstream),
    str(repo), receipt, "preserve_local",
  )

  assert conflicts == []
  assert observed["base"] == replay_base
  assert observed["onto"] == upstream
  assert observed["receipt"]["schema"] == 3
  assert observed["receipt"]["resolution_policy"] == "preserve_local"
  assert observed["receipt"]["replay_base"] == replay_base


def test_schema_two_policy_upgrade_keeps_reviewed_tree(tmp_path):
  from app import install

  source = tmp_path / "legacy-reviewed"
  pending = source / ".git" / "mobius-pending-update"
  pending.mkdir(parents=True)
  receipt_path = pending / "receipt.json"
  reviewed = "c" * 40
  receipt_path.write_text(json.dumps({
    "schema": 2,
    "app_id": 42,
    "upstream_commit": "a" * 40,
    "manifest": {"id": "legacy-reviewed"},
    "raw_base": "https://example.test/app/",
    "capability_digest": "capability",
    "candidate_digest": "b" * 64,
    "resolution_policy": "preserve_local",
    "reviewed_tree_oid": reviewed,
  }))

  upgraded = install.set_pending_conflict_update_policy(
    source,
    app_id=42,
    upstream_commit="a" * 40,
    policy="preserve_local",
    replay_base="d" * 40,
  )

  assert upgraded["schema"] == 3
  assert upgraded["replay_base"] == "d" * 40
  assert upgraded["reviewed_tree_oid"] == reviewed


@pytest.mark.asyncio
async def test_legacy_preserve_local_without_shared_history_fails_closed(
  tmp_path,
):
  from app import install
  from app.routes import apps as app_routes

  repo = tmp_path / "legacy-unrelated"
  app_git.ensure_repo(repo)
  (repo / "index.jsx").write_bytes(b"local\n")
  assert app_git.commit_local(repo, "local root")
  tree = app_git._run(repo, "rev-parse", "main^{tree}").stdout.strip()
  unrelated = app_git._run(
    repo, "commit-tree", tree, "-m", "unrelated upstream",
  ).stdout.strip()
  app_git._run(repo, "update-ref", "refs/heads/upstream", unrelated)
  pending = repo / ".git" / "mobius-pending-update"
  pending.mkdir(parents=True)
  receipt_path = pending / "receipt.json"
  receipt_path.write_text(json.dumps({
    "schema": 1,
    "app_id": 42,
    "upstream_commit": unrelated,
    "manifest": {"id": "legacy-unrelated"},
    "raw_base": "https://example.test/app/",
    "capability_digest": "capability",
    "candidate_digest": "b" * 64,
  }))
  receipt = install.read_pending_conflict_update_receipt(
    repo, app_id=42, upstream_commit=unrelated,
  )
  before = app_git.head_sha(repo, app_git.LOCAL_BRANCH)

  with pytest.raises(HTTPException) as raised:
    await app_routes._apply_update_resolution_policy(
      SimpleNamespace(id=42, upstream_commit=unrelated),
      str(repo), receipt, "preserve_local",
    )

  assert raised.value.status_code == 409
  assert raised.value.detail["code"] == "replay_base_missing"
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == before
  assert not app_git.update_operation_in_progress(repo)
  assert "resolution_policy" not in json.loads(receipt_path.read_text())


def _frozen_pending_candidate(manifest=None, *, upstream_commit=""):
  from app import install
  manifest = manifest or {
    **MANIFEST_NEWS,
    "id": "frozen-candidate",
    "static_assets": {"binary.bin": "binary.bin"},
    "source_files": ["extra.js"],
  }
  contract, capability_digest = install.contract_and_digest(manifest)
  candidate = install.InstallCandidate(
    manifest=manifest,
    raw_base="https://packages.example/frozen/",
    entry_bytes=b"export default function App() {}\x00",
    icon_processed=b"\x89PNG\x00binary-icon",
    icon_warning=None,
    bundled_job=b"#!/bin/sh\n\x00job",
    static_assets={"binary.bin": b"\x00\xffstatic"},
    source_files={"extra.js": b"\x00source"},
    seeds={"prompt.md": b"\x00seed"},
    capability_contract=contract,
    capability_digest=capability_digest,
    candidate_digest="",
    source_review_digest="",
    upstream_commit=upstream_commit,
    source_identity="https://github.com/example/frozen.git#manifest-id=frozen-candidate",
    predecessor_source_identity=None,
    canonical_source_url="https://packages.example/frozen/",
  )
  candidate = candidate.__class__(
    **{
      **candidate.__dict__,
      "candidate_digest": install._install_candidate_digest(
        manifest=candidate.manifest, raw_base=candidate.raw_base,
        source_identity=candidate.source_identity,
        predecessor_source_identity=candidate.predecessor_source_identity,
        canonical_source_url=candidate.canonical_source_url,
        upstream_commit=candidate.upstream_commit,
        entry_bytes=candidate.entry_bytes,
        icon_processed=candidate.icon_processed,
        bundled_job=candidate.bundled_job,
        static_assets=candidate.static_assets,
        source_files=candidate.source_files,
        seeds=candidate.seeds,
      ),
      "source_review_digest": install._source_review_digest(
        manifest=candidate.manifest, entry_bytes=candidate.entry_bytes,
        bundled_job=candidate.bundled_job,
        source_files=candidate.source_files,
        upstream_commit=candidate.upstream_commit,
      ),
    }
  )
  return candidate


def test_pending_candidate_snapshot_replays_binary_package_without_moving_refs(tmp_path):
  from app import install

  repo = tmp_path / "frozen-repo"
  app_git.ensure_repo(repo)
  app_git.record_upstream(
    repo, {"index.jsx": b"old"}, "https://packages.example/frozen/",
    "0.1.0",
  )
  upstream = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  main = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  candidate = _frozen_pending_candidate(upstream_commit=upstream)
  install.stage_pending_conflict_update(
    repo, app_id=7, upstream_commit=upstream, manifest=candidate.manifest,
    raw_base=candidate.raw_base, capability_digest=candidate.capability_digest,
    candidate_digest=candidate.candidate_digest, candidate=candidate,
  )
  receipt = install.read_pending_conflict_update_receipt(
    repo, app_id=7, upstream_commit=upstream,
  )
  assert receipt["schema"] == 4
  replayed = install.read_pending_conflict_update_candidate(repo, receipt)
  assert replayed == candidate
  assert app_git.head_sha(repo, app_git.UPSTREAM_BRANCH) == upstream
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == main
  install.clear_pending_conflict_update(repo)
  assert not app_git.ref_exists(repo, receipt["candidate_ref"])


def test_pending_candidate_snapshot_rejects_missing_or_tampered_ref(tmp_path):
  from app import install

  repo = tmp_path / "tampered-repo"
  app_git.ensure_repo(repo)
  app_git.record_upstream(repo, {"index.jsx": b"old"}, "https://packages.example/", "0.1.0")
  upstream = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  candidate = _frozen_pending_candidate(upstream_commit=upstream)
  install.stage_pending_conflict_update(
    repo, app_id=8, upstream_commit=upstream, manifest=candidate.manifest,
    raw_base=candidate.raw_base, capability_digest=candidate.capability_digest,
    candidate_digest=candidate.candidate_digest, candidate=candidate,
  )
  receipt = install.read_pending_conflict_update_receipt(
    repo, app_id=8, upstream_commit=upstream,
  )
  app_git._run(repo, "update-ref", receipt["candidate_ref"], upstream)
  assert install.read_pending_conflict_update_candidate(repo, receipt) is None
  with pytest.raises(RuntimeError, match="invalid bytes"):
    install._write_pending_candidate_snapshot(
      repo, candidate=candidate, upstream_commit=upstream,
    )
  app_git._run(repo, "update-ref", "-d", receipt["candidate_ref"])
  assert install.read_pending_conflict_update_candidate(repo, receipt) is None


@pytest.mark.asyncio
async def test_pending_candidate_without_exact_git_commit_cannot_replay():
  from app import install

  candidate = _frozen_pending_candidate()
  with pytest.raises(HTTPException) as raised:
    await install.install_candidate(
      None,
      candidate=candidate,
      manifest_url=None,
      source="store",
      expected_app_id=7,
      expected_upstream_commit="a" * 40,
    )
  assert raised.value.status_code == 409
  assert raised.value.detail["code"] == "pending_update_changed"


@pytest.mark.asyncio
async def test_install_cancellation_waits_for_admitted_publication():
  from app import install

  entered = asyncio.Event()
  finish = asyncio.Event()
  settled = asyncio.Event()

  async def transaction(*args, started, **kwargs):
    started.set()
    entered.set()
    await finish.wait()
    settled.set()
    return MagicMock()

  with patch(
    "app.install._install_candidate_transaction", side_effect=transaction,
  ):
    request = asyncio.create_task(install.install_candidate(
      MagicMock(),
      candidate=MagicMock(),
      manifest_url="https://example.test/mobius.json",
    ))
    await entered.wait()
    request.cancel()
    await asyncio.sleep(0)
    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
      await request

  assert settled.is_set()


@pytest.mark.asyncio
async def test_install_cancellation_stays_cancelled_when_admitted_worker_fails():
  from app import install

  entered = asyncio.Event()
  finish = asyncio.Event()

  async def transaction(*args, started, **kwargs):
    started.set()
    entered.set()
    await finish.wait()
    raise RuntimeError("publication failed after disconnect")

  with patch(
    "app.install._install_candidate_transaction", side_effect=transaction,
  ), patch.object(install.log, "exception") as logged:
    request = asyncio.create_task(install.install_candidate(
      MagicMock(),
      candidate=MagicMock(),
      manifest_url="https://example.test/mobius.json",
    ))
    await entered.wait()
    request.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
      await request

  logged.assert_called_once_with(
    "app publication failed after its client disconnected"
  )


@pytest.mark.asyncio
async def test_install_cancellation_before_admission_cancels_worker():
  from app import install

  entered = asyncio.Event()
  worker_cancelled = asyncio.Event()

  async def transaction(*args, started, **kwargs):
    entered.set()
    try:
      await asyncio.Event().wait()
    finally:
      worker_cancelled.set()

  with patch(
    "app.install._install_candidate_transaction", side_effect=transaction,
  ):
    request = asyncio.create_task(install.install_candidate(
      MagicMock(),
      candidate=MagicMock(),
      manifest_url="https://example.test/mobius.json",
    ))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
      await request

  assert worker_cancelled.is_set()


def test_git_candidate_requires_a_committed_manifest(tmp_path):
  from app import install

  repo = tmp_path / "missing-manifest"
  app_git.ensure_repo(repo)
  commit = app_git.record_upstream(
    repo, {"index.jsx": b"export default function App() {}"},
    "https://packages.example/", "1.0.0",
  )
  candidate = _frozen_pending_candidate(upstream_commit=commit)
  with pytest.raises(HTTPException) as raised:
    install._verify_git_install_candidate(repo, commit, candidate)
  assert raised.value.status_code == 409
  assert raised.value.detail["code"] == "git_source_mismatch"
  assert raised.value.detail["reason"] == "missing mobius.json"


def test_pending_candidate_receipt_failure_preserves_reused_snapshot_and_main(tmp_path):
  from app import install

  repo = tmp_path / "receipt-failure-repo"
  app_git.ensure_repo(repo)
  app_git.record_upstream(repo, {"index.jsx": b"old"}, "https://packages.example/", "0.1.0")
  upstream = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  candidate = _frozen_pending_candidate(upstream_commit=upstream)
  install.stage_pending_conflict_update(
    repo, app_id=9, upstream_commit=upstream, manifest=candidate.manifest,
    raw_base=candidate.raw_base, capability_digest=candidate.capability_digest,
    candidate_digest=candidate.candidate_digest, candidate=candidate,
  )
  receipt_path = repo / ".git" / "mobius-pending-update" / "receipt.json"
  receipt = json.loads(receipt_path.read_text())
  main_before = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  with patch("app.install.atomic_write", side_effect=OSError("disk full")):
    with pytest.raises(OSError):
      install.stage_pending_conflict_update(
        repo, app_id=9, upstream_commit=upstream, manifest=candidate.manifest,
        raw_base=candidate.raw_base, capability_digest=candidate.capability_digest,
        candidate_digest=candidate.candidate_digest, candidate=candidate,
      )
  assert app_git.ref_exists(repo, receipt["candidate_ref"])
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == main_before
  assert install.read_pending_conflict_update_candidate(repo, receipt) == candidate

  from dataclasses import replace
  changed_warning = replace(candidate, icon_warning="icon: another transient failure")
  reused_ref, created = install._write_pending_candidate_snapshot(
    repo, candidate=changed_warning, upstream_commit=upstream,
  )
  assert reused_ref == receipt["candidate_ref"] and not created
  assert install.read_pending_conflict_update_candidate(repo, receipt) == candidate

  receipt["candidate_ref"] = "refs/heads/main"
  receipt["candidate_digest"] = 123  # Malformed metadata cannot name a branch to delete.
  receipt_path.write_text(json.dumps(receipt))
  install.clear_pending_conflict_update(repo)
  assert app_git.ref_exists(repo, app_git.LOCAL_BRANCH)


def test_git_install_creates_repo_and_records_upstream(
  client, auth, bypass_url_validation,
):
  """a fresh install inits the per-app repo and stamps the
  upstream commit + jsx sha on the App row."""
  base = "https://on.test/repo/"
  r = _install_v1(client, auth, base, {**MANIFEST_NEWS, "id": "on-install"}, JSX)
  assert r.status_code == 201, r.text
  assert r.json()["divergence"] == "none"
  data_dir = Path(get_settings().data_dir)
  assert (data_dir / "apps" / "on-install" / ".git").is_dir()
  # The App row carries the upstream provenance.
  from app.models import App
  from app.database import SessionLocal
  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "on-install").first()
    assert app.upstream_commit
    assert app.upstream_jsx_sha
  finally:
    db.close()


def test_git_clean_update_carries_local_edits_forward(
  client, auth, bypass_url_validation,
):
  """a local edit to one region + an upstream edit to a DISJOINT
  region merges cleanly — the served source contains BOTH changes."""
  base = "https://on2.test/repo/"
  m = {**MANIFEST_NEWS, "id": "on-clean"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "on-clean" / "index.jsx"

  # Agent edits the title locally.
  jsx_file.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE"))

  # Upstream v2 edits the footer — a disjoint region.
  jsx_v2 = JSX_MULTI.replace("ORIGINAL FOOTER", "UPSTREAM FOOTER")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "clean_merge"
  merged = jsx_file.read_text()
  assert "AGENT TITLE" in merged       # local edit carried forward
  assert "UPSTREAM FOOTER" in merged   # upstream change applied


# A multi-line job script with two editable regions (the first and last
# step) separated by unchanged context, so git's line-based 3-way merge can
# interleave a local edit and a disjoint upstream edit cleanly — the same
# spacing reason JSX_MULTI documents.
JOB_MULTI = (
  "#!/bin/bash\n"
  "echo step ONE\n"
  "echo a\n"
  "echo b\n"
  "echo c\n"
  "echo d\n"
  "echo step FIVE\n"
)


def _install_with_job(client, auth, base, manifest, jsx, job):
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, jsx.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, job.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


def test_fresh_install_writes_bundled_job_script(
  client, auth, bypass_url_validation,
):
  """A fresh install writes the manifest's bundled job script to source_dir,
  executable, so cron + run-job can find it. The transactional source write
  (not the removed post-commit blind overwrite) is the writer now."""
  base = "https://job-fresh.test/repo/"
  m = {**MANIFEST_NEWS, "id": "job-fresh"}
  r = _install_with_job(client, auth, base, m, JSX, JOB_MULTI)
  assert r.status_code == 201, r.text
  data_dir = Path(get_settings().data_dir)
  job_file = data_dir / "apps" / "job-fresh" / "fetch.sh"
  assert job_file.read_text() == JOB_MULTI
  assert job_file.stat().st_mode & 0o111  # executable bit set


def test_clean_update_preserves_local_job_script_edit(
  client, auth, bypass_url_validation,
):
  """A locally edited job script survives a clean update: the agent edits one
  step of fetch.sh, an upstream v2 edits a DISJOINT step, and the served job
  script contains BOTH changes — the bundled copy no longer clobbers the
  local edit. The schedule job now flows through the same 3-way merge as
  index.jsx."""
  base = "https://job-clean.test/repo/"
  m = {**MANIFEST_NEWS, "id": "job-clean"}
  r1 = _install_with_job(client, auth, base, m, JSX, JOB_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  job_file = data_dir / "apps" / "job-clean" / "fetch.sh"

  # Agent edits the FIRST step of the job locally.
  job_file.write_text(JOB_MULTI.replace("step ONE", "step ONE LOCAL"))

  # Upstream v2 edits the LAST step — a disjoint region.
  job_v2 = JOB_MULTI.replace("step FIVE", "step FIVE UPSTREAM")
  r2 = _install_with_job(
    client, auth, base, {**m, "version": "2.0.0"}, JSX, job_v2,
  )
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "clean_merge"
  served = job_file.read_text()
  assert "step ONE LOCAL" in served      # local job edit carried forward
  assert "step FIVE UPSTREAM" in served  # upstream job change applied
  assert "<<<<<<<" not in served


def test_git_repeated_updates_to_same_region_stay_clean(
  client, auth, bypass_url_validation,
):
  """A clean merge must advance the merge base so the NEXT update only
  reconciles the genuinely-new upstream delta.

  Upstream evolves the footer across v2 and v3 while the agent's local
  edit sits on the disjoint title line. Each update is individually a
  disjoint clean merge, so BOTH should apply seamlessly. If a clean merge
  is recorded as a plain commit (upstream never an ancestor of the local
  branch), the v3 merge re-runs against the v1 install point: it sees the
  footer changed on both sides (local already holds v2's footer, upstream
  ships v3's) and reports a spurious conflict. Recording the merge so the
  base advances keeps v3 clean.
  """
  base = "https://on-repeat.test/repo/"
  m = {**MANIFEST_NEWS, "id": "on-repeat"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "on-repeat" / "index.jsx"

  # Agent edits the title locally — a region upstream never touches.
  jsx_file.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE"))

  jsx_v2 = JSX_MULTI.replace("ORIGINAL FOOTER", "FOOTER V2")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "clean_merge"

  # Upstream evolves the SAME footer line again. With the base advanced to
  # v2 this is still disjoint from the local title edit -> clean.
  jsx_v3 = JSX_MULTI.replace("ORIGINAL FOOTER", "FOOTER V3")
  r3 = _update_v2(client, auth, base, {**m, "version": "3.0.0"}, jsx_v3)
  assert r3.status_code == 201, r3.text
  assert r3.json()["mode"] == "update", (
    "v3 update should merge cleanly, not conflict against a stale base; "
    f"got {r3.json()}"
  )
  merged = jsx_file.read_text()
  assert "AGENT TITLE" in merged   # local edit still preserved
  assert "FOOTER V3" in merged     # latest upstream footer applied
  assert "<<<<<<<" not in merged


def test_git_clean_update_advances_merge_base(
  client, auth, bypass_url_validation,
):
  """After a clean update the local branch records upstream as an
  ancestor, so the recorded upstream tip is reachable from `main`. This is
  the structural invariant that keeps repeated updates from re-litigating
  already-merged history."""
  import subprocess

  base = "https://on-advance.test/repo/"
  m = {**MANIFEST_NEWS, "id": "on-advance"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  repo = data_dir / "apps" / "on-advance"
  (repo / "index.jsx").write_text(JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE"))

  jsx_v2 = JSX_MULTI.replace("ORIGINAL FOOTER", "FOOTER V2")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  assert r2.json()["divergence"] == "clean_merge"

  env = app_git._git_env(repo)
  proc = subprocess.run(
    ["git", "-C", str(repo), "merge-base", "--is-ancestor", "upstream", "main"],
    env=env, capture_output=True,
  )
  assert proc.returncode == 0, (
    "upstream tip must be an ancestor of main after a clean merge so the "
    "next update's base is the just-merged version"
  )


def test_git_clean_update_without_local_edits_is_fast_forward(
  client, auth, bypass_url_validation,
):
  """when local main still matches the previous upstream, a
  clean update reports fast_forward for the seamless store path."""
  base = "https://on-fast.test/repo/"
  m = {
    **MANIFEST_NEWS,
    "id": "on-fast-forward",
    "icon": None,
    "storage_seeds": {},
    "schedule": None,
  }
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text

  jsx_v2 = JSX_MULTI.replace("ORIGINAL FOOTER", "UPSTREAM FOOTER")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "update"
  assert payload["divergence"] == "fast_forward"
  assert "index.jsx" in payload["reconciliation"]["new_upstream_paths"]
  assert payload["reconciliation"]["proven_present"] == []
  assert payload["reconciliation"]["local_only_paths"] == []
  # With no local edits the served source must be the new upstream verbatim.
  # The latent bug let a failed in-memory merge leave the OLD bytes on disk
  # while still bumping the version, so assert the new content actually
  # landed rather than trusting the divergence label alone.
  data_dir = Path(get_settings().data_dir)
  served = (data_dir / "apps" / "on-fast-forward" / "index.jsx").read_text()
  assert "UPSTREAM FOOTER" in served
  assert "ORIGINAL FOOTER" not in served


def test_git_consecutive_no_edit_updates_advance_base(
  client, auth, bypass_url_validation,
):
  """Successive no-local-edit updates must each carry the new upstream
  content and keep upstream an ancestor of `main`.

  Without the no-edit fast path, the first update commits a single-parent
  local commit (upstream unreachable from `main`), so the second update's
  merge base is the original install point. The overlapping footer diff
  then resolves to the LOCAL (stale) side and v3's content never lands.
  Each update must advance the base so v3's bytes are served and the
  merge-base invariant holds.
  """
  import subprocess

  base = "https://on-consec.test/repo/"
  m = {
    **MANIFEST_NEWS,
    "id": "on-consecutive",
    "icon": None,
    "storage_seeds": {},
    "schedule": None,
  }
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  repo = data_dir / "apps" / "on-consecutive"
  jsx_file = repo / "index.jsx"

  jsx_v2 = JSX_MULTI.replace("ORIGINAL FOOTER", "FOOTER V2")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  assert r2.json()["divergence"] == "fast_forward"
  assert "FOOTER V2" in jsx_file.read_text()

  jsx_v3 = JSX_MULTI.replace("ORIGINAL FOOTER", "FOOTER V3")
  r3 = _update_v2(client, auth, base, {**m, "version": "3.0.0"}, jsx_v3)
  assert r3.status_code == 201, r3.text
  assert r3.json()["mode"] == "update"
  served = jsx_file.read_text()
  assert "FOOTER V3" in served, (
    "v3 upstream content must land on disk; a stale merge base resolves the "
    f"footer to the local side and serves old bytes. got: {served!r}"
  )
  assert "FOOTER V2" not in served

  from app import app_git
  proc = subprocess.run(
    ["git", "-C", str(repo), "merge-base", "--is-ancestor", "upstream", "main"],
    env=app_git._git_env(repo), capture_output=True,
  )
  assert proc.returncode == 0, (
    "upstream tip must stay an ancestor of main across consecutive no-edit "
    "updates so each update's merge base is the just-installed version"
  )


def test_git_static_asset_update_leaves_clean_app_repo(
  client, auth, bypass_url_validation,
):
  """Static asset rollback snapshots must never land in per-app git.

  CubeRun-style packages update dozens of static files. The installer uses
  temporary snapshots for rollback, but those snapshots must live outside the
  source repo so the post-write local commit stays clean and future updates
  do not see installer noise as local edits.
  """
  base = "https://static-clean.test/repo/"
  manifest_v1 = {
    **MANIFEST_NEWS,
    "id": "static-clean",
    "icon": None,
    "storage_seeds": {},
    "schedule": None,
    "static_assets": {
      "index.html": "build/index.html",
      "static/css/main.css": "build/static/css/main.css",
    },
  }
  responses_v1 = {
    base + "mobius.json": (200, json.dumps(manifest_v1).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "build/index.html": (200, b"<!doctype html><title>v1</title>"),
    base + "build/static/css/main.css": (200, b"body{color:red}"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v1),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201, r1.text

  manifest_v2 = {**manifest_v1, "version": "2.0.0"}
  responses_v2 = {
    base + "mobius.json": (200, json.dumps(manifest_v2).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "build/index.html": (200, b"<!doctype html><title>v2</title>"),
    base + "build/static/css/main.css": (200, b"body{color:blue}"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"

  from app import app_git
  data_dir = Path(get_settings().data_dir)
  source_dir = data_dir / "apps" / "static-clean"
  assert (source_dir / "static" / "index.html").read_text() == (
    "<!doctype html><title>v2</title>"
  )
  assert list(source_dir.rglob("*.mobius-bak")) == []
  assert not (data_dir / "apps" / ".static-clean.mobius-static-bak").exists()
  assert app_git._run(source_dir, "status", "--porcelain").stdout == ""
  assert app_git._run(source_dir, "ls-files", "*.mobius-bak").stdout == ""


def test_app_store_update_recognizes_squashed_local_contribution(
  client, auth, bypass_url_validation,
):
  """App Store update uses the same provenance engine as the shell updater."""
  from app import app_git

  base_url = "https://equivalent.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "equivalent-update"}
  r1 = _install_v1(client, auth, base_url, manifest, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  source = Path(get_settings().data_dir) / "apps" / "equivalent-update"
  entry = source / "index.jsx"
  base_sha = app_git.head_sha(source, app_git.UPSTREAM_BRANCH)

  shared = JSX_MULTI.replace("ORIGINAL TITLE", "SHARED REVIEW")
  entry.write_text(shared)
  reviewed = app_git.commit_local(source, "reviewed contribution")
  assert reviewed
  reviewed_diff = app_git._canonical_diff(source, base_sha, reviewed)
  assert reviewed_diff is not None
  digest = hashlib.sha256(reviewed_diff).hexdigest()
  assert app_git.record_pending_equivalent_change(
    source,
    base_sha=base_sha,
    head_sha=reviewed,
    source_sha=reviewed,
    diff_sha256=digest,
    contribution_id="app-store-reviewed-change",
  )
  landed = app_git.mark_equivalent_change_landed(source, digest)
  assert landed

  # Local keeps evolving the contributed line before the Store fetches the
  # squash result, which is exactly the shape ordinary three-way Git conflicts.
  entry.write_text(shared.replace("SHARED REVIEW", "LOCAL FOLLOWUP"))
  r2 = _update_v2(
    client,
    auth,
    base_url,
    {**manifest, "version": "2.0.0"},
    shared,
  )

  assert r2.status_code == 201, r2.text
  body = r2.json()
  assert body["mode"] == "update"
  assert body["divergence"] == "clean_merge"
  assert "LOCAL FOLLOWUP" in entry.read_text()
  assert not app_git.ref_exists(source, landed)


def test_git_conflicting_update_leaves_source_unchanged_until_resolve(
  client, auth, bypass_url_validation,
):
  """A local edit + an upstream edit to the SAME region conflicts. The endpoint
  returns mode='conflict' but leaves the live source untouched. The DB row is
  NOT stamped with the upstream bytes (the served version stays local/old), and
  the new upstream is recorded for the click-gated resolver. (Per-app git is
  unconditional now — no enabler needed.)"""
  base = "https://on3.test/repo/"
  m = {
    **MANIFEST_NEWS,
    "id": "on-conflict",
    # Receipt serialization sorts nested manifest keys. The candidate digest
    # must treat equivalent inline JSON as the same bytes on replay.
    "storage_seeds": {
      **MANIFEST_NEWS["storage_seeds"],
      "unordered.json": {"z": 1, "a": 2},
    },
  }
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  app_dir = data_dir / "apps" / "on-conflict"
  jsx_file = app_dir / "index.jsx"

  local = JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE")
  jsx_file.write_text(local)

  # Upstream v2 edits the SAME title line differently → conflict.
  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "conflict"
  assert payload["version"] == "1.0.0"
  assert payload["upstream_version"] == "2.0.0"
  assert "index.jsx" in payload["conflict_paths"]
  assert payload["reconciliation"]["unresolved_conflict_paths"] == [
    "index.jsx",
  ]

  # The update attempt itself does not write conflict markers or leave a merge
  # in progress. The owner must click Resolve in chat before that happens.
  served = jsx_file.read_text()
  assert served == local
  assert "<<<<<<<" not in served and ">>>>>>>" not in served
  assert not (app_dir / ".git" / "MERGE_HEAD").exists()
  pending = app_dir / ".git" / "mobius-pending-update" / "receipt.json"
  assert pending.is_file()

  # The DB row is NOT stamped with the upstream bytes — served version stays
  # local/old until the agent resolves; the new upstream is recorded for it.
  from app.models import App
  from app.database import SessionLocal
  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "on-conflict").first()
    assert app.jsx_source != jsx_v2
    assert "UPSTREAM TITLE" not in app.jsx_source
    assert app.upstream_commit
  finally:
    db.close()

  # The unresolved/resumable receipt is itself an update signal. This check is
  # local and must not depend on another upstream fetch.
  update = client.get(f"/api/apps/{payload['id']}/update-check", headers=auth)
  assert update.status_code == 200, update.text
  assert update.json()["update_available"] is True
  assert update.json()["pending_update_state"] == "needs_resolution"
  assert update.json()["needs_resolution"] is True
  assert update.json()["upstream_version"] == "2.0.0"
  with patch("app.app_git.ref_is_ancestor", return_value=None):
    unknown = client.get(
      f"/api/apps/{payload['id']}/update-check", headers=auth,
    )
  assert unknown.status_code == 200, unknown.text
  assert unknown.json()["update_available"] is True
  assert unknown.json()["pending_update_state"] == "unknown"
  assert unknown.json()["needs_resolution"] is False

  bypass = client.patch(
    f"/api/apps/{payload['id']}",
    headers=auth,
    json={"jsx_source": "export default function App(){return <div>bypass</div>}"},
  )
  assert bypass.status_code == 422, bypass.text

  # The explicit whole-tree policy is carried by the resolver-open request,
  # persisted before the real merge, and therefore precedes the agent turn.
  resolver = client.post(
    f"/api/apps/{payload['id']}/conflict-resolver-chat", headers=auth,
    json={"resolution_policy": "preserve_local"},
  )
  assert resolver.status_code == 200, resolver.text
  assert app_git.rebase_in_progress(app_dir)
  resolved = JSX_MULTI.replace("ORIGINAL TITLE", "RESOLVED TITLE")
  jsx_file.write_text(resolved)
  _finish_materialized_rebase(app_dir)

  reviewed = client.post(
    "/api/apps/resolve-update/review",
    headers=auth,
    json={"source_dir": str(app_dir)},
  )
  assert reviewed.status_code == 200, reviewed.text
  reviewed_tree = reviewed.json()["tree_oid"]
  assert "RESOLVED TITLE" in reviewed.json()["diff"]
  assert json.loads(pending.read_text())["reviewed_tree_oid"] == reviewed_tree

  # Resolution and promotion are separate crash-safe phases. Once the resolved
  # source is committed, the receipt remains so the canonical installer can
  # replay bundle/static/DB promotion. The existing resolver chat remains the
  # resumable surface while upstream is already an ancestor of local source.
  pending_update = client.get(
    f"/api/apps/{payload['id']}/update-check", headers=auth,
  )
  assert pending_update.status_code == 200, pending_update.text
  assert pending_update.json()["update_available"] is True
  assert pending_update.json()["pending_update_state"] == "replay_pending"
  assert pending_update.json()["needs_resolution"] is False
  redundant_resolver = client.post(
    f"/api/apps/{payload['id']}/conflict-resolver-chat", headers=auth,
    json={"resolution_policy": "preserve_local"},
  )
  assert redundant_resolver.status_code == 200, redundant_resolver.text
  assert redundant_resolver.json()["created"] is False

  replay_responses = {
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v2 prompt"),
    base + "fetch.sh": (200, b""),
  }

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(replay_responses),
  ):
    replayed = client.post(
      "/api/apps/resolve-update",
      headers=auth,
      # Simulate a retry after the review response was lost: the exact tree
      # identity survives in the pending receipt.
      json={"source_dir": str(app_dir)},
    )
  assert replayed.status_code == 200, replayed.text
  assert replayed.json()["mode"] == "updated"

  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "on-conflict").first()
    assert app.version == "2.0.0"
    assert app.jsx_source == resolved
  finally:
    db.close()
  assert jsx_file.read_text() == resolved
  assert not (app_dir / ".git" / "MERGE_HEAD").exists()
  assert not pending.exists()


def test_version_only_conflict_uses_the_ordinary_resolver(
  client, auth, bypass_url_validation,
):
  """Version constants have no hidden updater exception."""
  base = "https://ver-only.test/repo/"
  m = {**MANIFEST_NEWS, "id": "ver-only"}
  jsx_v1 = (
    'const APP_VERSION = "1.0.0"\n'
    "export default function App() { return <div>ok</div> }\n"
  )
  r1 = _install_v1(client, auth, base, m, jsx_v1)
  assert r1.status_code == 201, r1.text

  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "ver-only" / "index.jsx"
  # A prior local "agent edit" bumped only the version constant.
  jsx_file.write_text(jsx_v1.replace('"1.0.0"', '"1.0.1"'))

  # The release bumps the same constant — a version-only clash.
  jsx_v2 = jsx_v1.replace('"1.0.0"', '"2.0.0"')
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "conflict", payload
  assert payload["conflict_paths"] == ["index.jsx"]
  assert 'APP_VERSION = "1.0.1"' in jsx_file.read_text()


def test_resolved_conflict_replays_frozen_candidate_when_url_moves(
  client, auth, bypass_url_validation,
):
  """A moving URL cannot mix release-C artifacts into a release-B resolve."""
  from app.models import App
  from app.database import SessionLocal

  base = "https://resolve-digest.test/repo/"
  manifest_v1 = {**MANIFEST_NEWS, "id": "resolve-digest"}
  installed = _install_v1(client, auth, base, manifest_v1, JSX_MULTI)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]

  data_dir = Path(get_settings().data_dir)
  app_dir = data_dir / "apps" / "resolve-digest"
  jsx_file = app_dir / "index.jsx"
  jsx_file.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL TITLE"))
  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  conflicted = _update_v2(
    client, auth, base, {**manifest_v1, "version": "2.0.0"}, jsx_v2,
  )
  assert conflicted.status_code == 201, conflicted.text
  assert conflicted.json()["mode"] == "conflict"

  resolver = client.post(
    f"/api/apps/{app_id}/conflict-resolver-chat", headers=auth,
    json={"resolution_policy": "preserve_local"},
  )
  assert resolver.status_code == 200, resolver.text
  resolved = JSX_MULTI.replace("ORIGINAL TITLE", "RESOLVED TITLE")
  jsx_file.write_text(resolved)
  _finish_materialized_rebase(app_dir)
  origin = app_git.origin_url(app_dir)
  assert origin
  app_git._run(app_dir, "remote", "remove", "origin")
  reviewed = client.post(
    "/api/apps/resolve-update/review",
    headers=auth,
    json={"source_dir": str(app_dir)},
  )
  assert reviewed.status_code == 200, reviewed.text
  pending = app_dir / ".git" / "mobius-pending-update" / "receipt.json"
  bundle = Path(installed.json()["compiled_path"])

  without_origin = client.post(
    "/api/apps/resolve-update",
    headers=auth,
    json={
      "source_dir": str(app_dir),
      "reviewed_tree_oid": reviewed.json()["tree_oid"],
    },
  )
  assert without_origin.status_code == 409, without_origin.text
  assert without_origin.json()["detail"]["code"] == "git_origin_required"
  assert pending.exists()
  app_git._run(app_dir, "remote", "add", "origin", origin)
  reviewed = client.post(
    "/api/apps/resolve-update/review",
    headers=auth,
    json={"source_dir": str(app_dir)},
  )
  assert reviewed.status_code == 200, reviewed.text

  # The URL now serves a different seed, but the reviewed candidate must be
  # replayed from its immutable Git snapshot without consulting the URL.
  changed_responses = {
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"release C changed this byte"),
    base + "fetch.sh": (200, b""),
  }

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(changed_responses),
  ) as moved_client:
    replayed = client.post(
      "/api/apps/resolve-update",
      headers=auth,
      json={
        "source_dir": str(app_dir),
        "reviewed_tree_oid": reviewed.json()["tree_oid"],
      },
    )
  assert replayed.status_code == 200, replayed.text
  assert replayed.json()["mode"] == "updated"
  moved_client.assert_not_called()

  db = SessionLocal()
  try:
    app = db.query(App).filter(App.id == app_id).first()
    assert app.version == "2.0.0"
    assert app.jsx_source == resolved
  finally:
    db.close()
  assert Path(replayed.json()["app"]["compiled_path"]).is_file()
  assert not pending.exists()
  assert not app_git.update_operation_in_progress(app_dir)
  assert jsx_file.read_text() == resolved


def test_resolved_conflict_converges_static_metadata_and_bundle_once(
  client, auth, bypass_url_validation,
):
  """CubeRun-class add/drop assets land with source and manifest metadata."""
  from app.models import App
  from app.database import SessionLocal

  base = "https://resolve-static.test/repo/"
  manifest_v1 = {
    **MANIFEST_NEWS,
    "id": "resolve-static",
    "icon": None,
    "storage_seeds": {},
    "schedule": None,
    "static_assets": {"old.js": "build/old.js"},
  }
  responses_v1 = {
    base + "mobius.json": (200, json.dumps(manifest_v1).encode()),
    base + "index.jsx": (200, JSX_MULTI.encode()),
    base + "build/old.js": (200, b"window.release='v1'"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v1),
  ):
    installed = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]

  data_dir = Path(get_settings().data_dir)
  app_dir = data_dir / "apps" / "resolve-static"
  jsx_file = app_dir / "index.jsx"
  jsx_file.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL TITLE"))
  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  manifest_v2 = {
    **manifest_v1,
    "version": "2.0.0",
    "offline_capable": True,
    "static_assets": {"new.js": "build/new.js"},
  }
  responses_v2 = {
    base + "mobius.json": (200, json.dumps(manifest_v2).encode()),
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "build/new.js": (200, b"window.release='v2'"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    conflicted = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )
  assert conflicted.status_code == 201, conflicted.text
  assert conflicted.json()["mode"] == "conflict"
  assert (app_dir / "static" / "old.js").read_bytes() == b"window.release='v1'"
  assert not (app_dir / "static" / "new.js").exists()

  resolver = client.post(
    f"/api/apps/{app_id}/conflict-resolver-chat", headers=auth,
    json={"resolution_policy": "preserve_local"},
  )
  assert resolver.status_code == 200, resolver.text
  resolved = JSX_MULTI.replace("ORIGINAL TITLE", "RESOLVED TITLE")
  jsx_file.write_text(resolved)
  _finish_materialized_rebase(app_dir)
  reviewed = client.post(
    "/api/apps/resolve-update/review",
    headers=auth,
    json={"source_dir": str(app_dir)},
  )
  assert reviewed.status_code == 200, reviewed.text

  replay_responses = {
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "build/new.js": (200, b"window.release='v2'"),
  }

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(replay_responses),
  ):
    replayed = client.post(
      "/api/apps/resolve-update",
      headers=auth,
      json={
        "source_dir": str(app_dir),
        "reviewed_tree_oid": reviewed.json()["tree_oid"],
      },
    )
  assert replayed.status_code == 200, replayed.text
  assert replayed.json()["mode"] == "updated"

  db = SessionLocal()
  try:
    app = db.query(App).filter(App.id == app_id).first()
    assert app.version == "2.0.0"
    assert app.offline_capable is True
    assert app.jsx_source == resolved
  finally:
    db.close()
  assert not (app_dir / "static" / "old.js").exists()
  assert (app_dir / "static" / "new.js").read_bytes() == b"window.release='v2'"
  assert not (app_dir / ".git" / "mobius-pending-update").exists()


def test_clean_merge_with_unreadable_bytes_is_treated_as_conflict(
  client, auth, bypass_url_validation, monkeypatch,
):
  """A clean merge VERDICT whose merged tree has NO index.jsx (an unreadable
  tree) must NOT fall through to a silent pure-upstream overwrite + single-parent
  commit — that strands the merge base and resolves the NEXT update to stale
  local content. The fix routes it to the same safe path as a real conflict:
  local source is preserved (served version unchanged) and the new upstream is
  recorded for an agent-resolution pass. Regression for the clean-verdict-no-
  entry gap."""
  base = "https://on-cleanempty.test/repo/"
  m = {**MANIFEST_NEWS, "id": "on-cleanempty"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  app_dir = data_dir / "apps" / "on-cleanempty"
  jsx_file = app_dir / "index.jsx"

  # Local edit (diverged) that collides with the upstream edit below.
  jsx_file.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE"))
  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")

  # Force a clean merge verdict whose merged tree can't be read into an
  # index.jsx — the contract violation the fix guards against (clean status
  # normally implies the entry is in the tree). merge_upstream returns clean with
  # a tree oid, but read_merged_tree yields a dict WITHOUT index.jsx.
  from app.app_git import MergeResult
  monkeypatch.setattr(
    "app.app_git.merge_upstream",
    lambda *a, **k: MergeResult(status="clean", merged_tree_oid="deadbeef"),
  )
  monkeypatch.setattr(
    "app.app_git.read_merged_tree",
    lambda *a, **k: {},
  )

  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "conflict", payload
  assert "index.jsx" in payload["conflict_paths"]

  from app.models import App
  from app.database import SessionLocal
  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "on-cleanempty").first()
    # Local source preserved (NOT clobbered with pure upstream); the new
    # upstream provenance recorded for the resolution pass.
    assert "UPSTREAM TITLE" not in app.jsx_source
    assert app.upstream_commit
  finally:
    db.close()


def test_conflicting_update_returns_conflict_without_auto_spawning(
  client, auth, bypass_url_validation,
):
  """A conflicting update returns mode=conflict and leaves source untouched.
  It does NOT auto-spawn a resolver chat, materialize markers, or fire a
  notification. Whether to involve the agent is the owner's call, made via the
  store's click-gated "Resolve in chat" affordance — auto-spawning here preempted
  that choice and raced a duplicate chat against the store's own. A repeated
  conflict behaves the same."""
  base = "https://spawn-conflict.test/repo/"
  m = {**MANIFEST_NEWS, "id": "spawn-conflict", "name": "Spawn Conflict"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "spawn-conflict" / "index.jsx"

  local = JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE")
  jsx_file.write_text(local)
  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "conflict"
  assert r2.json()["conflict_paths"]  # the store surfaces these to the owner
  # No markers until the owner clicks Resolve in chat; the app keeps serving its
  # prior version meanwhile.
  assert jsx_file.read_text() == local
  assert "<<<<<<<" not in jsx_file.read_text()

  from app.models import Chat, Notification
  from app.database import SessionLocal
  db = SessionLocal()
  try:
    # No resolver chat was auto-spawned and no app_conflict notification fired.
    assert db.query(Chat).filter(Chat.title.like("%Spawn Conflict%")).count() == 0
    assert (
      db.query(Notification)
      .filter(Notification.source_type == "app_conflict")
      .count()
      == 0
    )
  finally:
    db.close()

  # A repeated conflicting update still just returns mode=conflict with no chat
  # and no live source mutation.
  jsx_v3 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE 3")
  r3 = _update_v2(client, auth, base, {**m, "version": "3.0.0"}, jsx_v3)
  assert r3.status_code == 201, r3.text
  assert r3.json()["mode"] == "conflict"
  assert jsx_file.read_text() == local
  db = SessionLocal()
  try:
    assert db.query(Chat).filter(Chat.title.like("%Spawn Conflict%")).count() == 0
  finally:
    db.close()


def test_conflict_resolver_requires_policy_before_materializing_merge(
  client, auth, bypass_url_validation, monkeypatch,
):
  """Neither update nor chat open mutates source; the selected policy does."""
  base = "https://click-conflict.test/repo/"
  m = {**MANIFEST_NEWS, "id": "click-conflict", "name": "Click Conflict"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]
  data_dir = Path(get_settings().data_dir)
  app_dir = data_dir / "apps" / "click-conflict"
  jsx_file = app_dir / "index.jsx"

  local = JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE")
  jsx_file.write_text(local)
  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "conflict"
  assert jsx_file.read_text() == local
  assert not (app_dir / ".git" / "MERGE_HEAD").exists()

  async def fake_start_turn(*args, **kwargs):
    assert app_git.rebase_in_progress(app_dir)
    return True

  monkeypatch.setattr(
    "app.routes.apps._start_conflict_resolver_turn",
    fake_start_turn,
  )
  monkeypatch.setattr(
    "app.background_agents.resolve_background_chat_choice",
    lambda data_dir, db: {
      "provider": "codex",
      "agent_settings": {"model": "gpt-5.5", "effort": "xhigh"},
    },
  )
  missing_policy = client.post(
    f"/api/apps/{app_id}/conflict-resolver-chat",
    headers=auth,
  )
  assert missing_policy.status_code == 422, missing_policy.text
  assert not app_git.update_operation_in_progress(app_dir)
  r3 = client.post(
    f"/api/apps/{app_id}/conflict-resolver-chat",
    headers=auth,
    json={"resolution_policy": "preserve_local"},
  )
  assert r3.status_code == 200, r3.text
  payload = r3.json()
  assert payload["chat_id"]
  assert payload["created"] is True
  assert payload["started"] is True
  from app.database import SessionLocal
  db = SessionLocal()
  try:
    resolver = db.get(models.Chat, payload["chat_id"])
    assert resolver.provider == "codex"
    assert resolver.agent_settings_json["model"] == "gpt-5.5"
    assert resolver.agent_settings_json["effort"] == "xhigh"
  finally:
    db.close()

  materialized = jsx_file.read_text()
  assert "<<<<<<<" in materialized and ">>>>>>>" in materialized
  assert "AGENT TITLE" in materialized and "UPSTREAM TITLE" in materialized
  assert app_git.rebase_in_progress(app_dir)


def test_conflict_resolver_batch_uses_one_chat_for_every_selected_app(
  client, auth, bypass_url_validation, monkeypatch,
):
  apps = []
  for suffix in ("one", "two"):
    base = f"https://batch-conflict-{suffix}.test/repo/"
    manifest = {
      **MANIFEST_NEWS,
      "id": f"batch-conflict-{suffix}",
      "name": f"Batch Conflict {suffix.title()}",
    }
    installed = _install_v1(client, auth, base, manifest, JSX_MULTI)
    assert installed.status_code == 201, installed.text
    app_dir = (
      Path(get_settings().data_dir) / "apps" / f"batch-conflict-{suffix}"
    )
    app_dir.joinpath("index.jsx").write_text(
      JSX_MULTI.replace("ORIGINAL TITLE", f"LOCAL {suffix.upper()}"),
    )
    updated = _update_v2(
      client,
      auth,
      base,
      {**manifest, "version": "2.0.0"},
      JSX_MULTI.replace("ORIGINAL TITLE", f"UPSTREAM {suffix.upper()}"),
    )
    assert updated.status_code == 201, updated.text
    assert updated.json()["mode"] == "conflict"
    apps.append((installed.json()["id"], app_dir, manifest["name"]))

  starts = []

  async def fake_start_turn(*args, **kwargs):
    starts.append({
      "content": kwargs.get("content", args[3] if len(args) > 3 else ""),
    })
    return True

  monkeypatch.setattr(
    "app.routes.apps._start_conflict_resolver_turn",
    fake_start_turn,
  )
  response = client.post(
    "/api/apps/conflict-resolver-batch",
    headers=auth,
    json={
      "app_ids": [app_id for app_id, _path, _name in apps],
      "resolution_policy": "preserve_local",
    },
  )
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["created"] is True
  assert body["started"] is True
  assert len(starts) == 1
  assert all(name in starts[0]["content"] for _id, _path, name in apps)
  assert all(app_git.rebase_in_progress(path) for _id, path, _name in apps)

  from app.database import SessionLocal
  db = SessionLocal()
  try:
    stored = db.query(models.App).filter(
      models.App.id.in_([app_id for app_id, _path, _name in apps]),
    ).all()
    assert {app.conflict_resolver_chat_id for app in stored} == {
      body["chat_id"],
    }
  finally:
    db.close()

  repeated = client.post(
    "/api/apps/conflict-resolver-batch",
    headers=auth,
    json={
      "app_ids": [app_id for app_id, _path, _name in apps],
      "resolution_policy": "preserve_local",
    },
  )
  assert repeated.status_code == 200, repeated.text
  assert repeated.json() == {
    "chat_id": body["chat_id"],
    "created": False,
    "started": False,
  }
  assert len(starts) == 1


def test_preserve_resolution_reviews_whole_tree_and_rejects_drift(
  client, auth, bypass_url_validation,
):
  """Review covers local-only files and finalize binds that exact tree."""
  base = "https://whole-tree-review.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "whole-tree-review"}
  installed = _install_v1(client, auth, base, manifest, JSX_MULTI)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]
  app_dir = Path(get_settings().data_dir) / "apps" / "whole-tree-review"
  entry = app_dir / "index.jsx"
  entry.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL TITLE"))
  (app_dir / "local-only.js").write_text("export const localOnly = true\n")

  upstream = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  conflicted = _update_v2(
    client, auth, base, {**manifest, "version": "2.0.0"}, upstream,
  )
  assert conflicted.status_code == 201, conflicted.text
  assert conflicted.json()["mode"] == "conflict"

  selected = client.post(
    "/api/apps/resolve-update/policy",
    headers=auth,
    json={"source_dir": str(app_dir), "policy": "preserve_local"},
  )
  assert selected.status_code == 200, selected.text
  entry.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "RESOLVED TITLE"))
  _finish_materialized_rebase(app_dir)

  reviewed = client.post(
    "/api/apps/resolve-update/review",
    headers=auth,
    json={"source_dir": str(app_dir)},
  )
  assert reviewed.status_code == 200, reviewed.text
  payload = reviewed.json()
  assert "local-only.js" in payload["diff"]
  assert "RESOLVED TITLE" in payload["diff"]

  (app_dir / "local-only.js").write_text("export const localOnly = false\n")
  rejected = client.post(
    "/api/apps/resolve-update",
    headers=auth,
    json={
      "source_dir": str(app_dir),
      "reviewed_tree_oid": payload["tree_oid"],
    },
  )
  assert rejected.status_code == 409, rejected.text
  assert rejected.json()["detail"]["code"] == "reviewed_tree_changed"
  assert not app_git.update_operation_in_progress(app_dir)


def test_exact_upstream_policy_replaces_complete_tracked_source_tree(
  client, auth, bypass_url_validation,
):
  """Explicit exact policy removes both conflicting and local-only source."""
  base = "https://whole-tree-exact.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "whole-tree-exact"}
  installed = _install_v1(client, auth, base, manifest, JSX_MULTI)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]
  app_dir = Path(get_settings().data_dir) / "apps" / "whole-tree-exact"
  entry = app_dir / "index.jsx"
  entry.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL TITLE"))
  local_only = app_dir / "local-only.js"
  local_only.write_text("export const localOnly = true\n")

  upstream = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  manifest_v2 = {**manifest, "version": "2.0.0"}
  conflicted = _update_v2(client, auth, base, manifest_v2, upstream)
  assert conflicted.status_code == 201, conflicted.text
  assert conflicted.json()["mode"] == "conflict"

  selected = client.post(
    f"/api/apps/{app_id}/conflict-resolver-chat",
    headers=auth,
    json={
      "resolution_policy": "accept_reviewed_upstream_exact",
    },
  )
  assert selected.status_code == 200, selected.text
  assert entry.read_text() != upstream
  assert local_only.exists()
  assert not (app_dir / ".git" / "MERGE_HEAD").exists()

  replay_responses = {
    base + "index.jsx": (200, upstream.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v2 prompt"),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(replay_responses),
  ):
    finalized = client.post(
      "/api/apps/resolve-update",
      headers=auth,
      json={"source_dir": str(app_dir)},
    )
  assert finalized.status_code == 200, finalized.text
  assert finalized.json()["mode"] == "updated"
  assert entry.read_text() == upstream
  assert not local_only.exists()
  diff = subprocess.run(
    ["git", "-C", str(app_dir), "diff", "upstream..main"],
    capture_output=True,
    text=True,
    check=True,
  )
  assert diff.stdout == ""
  assert finalized.json()["app"]["id"] == app_id


def test_git_conflict_does_not_apply_upstream_capabilities(
  client, auth, bypass_url_validation,
):
  """A conflicting update keeps serving the OLD code, so it must NOT jump the
  App row's capability/offline fields to the NEW manifest's values — otherwise
  an unreviewed old version could gain manage_apps install authority, or lose
  the offline semantics its service-worker code relies on, while still running
  the old bytes."""
  base = "https://on-cap-conflict.test/repo/"
  m = {
    **MANIFEST_NEWS,
    "id": "on-cap-conflict",
    "permissions": {
      "cross_app_access": "none", "share_with_apps": "none",
      "manage_apps": False,
    },
    "offline_capable": False,
  }
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "on-cap-conflict" / "index.jsx"

  # Local edit + upstream edit to the SAME region → conflict. The v2 manifest
  # also flips every capability/offline field "up".
  jsx_file.write_text(JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE"))
  m2 = {
    **m,
    "version": "2.0.0",
    "permissions": {
      "cross_app_access": "read", "share_with_apps": "read",
      "manage_apps": True,
    },
    "offline_capable": True,
  }
  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  r2 = _update_v2(client, auth, base, m2, jsx_v2)
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "conflict"

  from app.models import App
  from app.database import SessionLocal
  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "on-cap-conflict").first()
    # Served code is still v1, so capability/offline fields stay at v1 values.
    assert app.manage_apps is False
    assert app.offline_capable is False
    assert app.cross_app_access == "none"
    assert app.share_with_apps == "none"
  finally:
    db.close()


def test_verified_publication_handoff_connects_identity_across_source_conflict(
  client, auth, db, bypass_url_validation,
):
  """A published identity connects in place while local bytes stay untouched."""
  from app import install

  base = "https://publication-conflict.test/repo/"
  manifest = {
    **MANIFEST_NEWS,
    "id": "publication-conflict",
    "permissions": {
      "cross_app_access": "none",
      "share_with_apps": "none",
      "manage_apps": False,
    },
  }
  first = _install_v1(client, auth, base, manifest, JSX_MULTI)
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]
  source = Path(get_settings().data_dir) / "apps" / "publication-conflict"
  entry = source / "index.jsx"
  local = JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL TITLE")
  entry.write_text(local)

  incoming = JSX_MULTI.replace("ORIGINAL TITLE", "PUBLISHED TITLE")
  next_manifest = {
    **manifest,
    "version": "2.0.0",
    "permissions": {
      "cross_app_access": "read",
      "share_with_apps": "read",
      "manage_apps": True,
      "connect_manage": True,
    },
  }
  responses = {
    base + "mobius.json": (200, json.dumps(next_manifest).encode()),
    base + "index.jsx": (200, incoming.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    result = asyncio.run(install.install_from_manifest(
      db,
      manifest_url=base + "mobius.json",
      manifest=None,
      raw_base=None,
      source="publication_handoff",
      publication_handoff_app_id=app_id,
    ))

  assert result.mode == "conflict"
  assert result.app.id == app_id
  assert entry.read_text() == local
  assert result.app.version == "2.0.0"
  assert result.app.manifest_url == (
    base.rstrip("/") + "#manifest-id=publication-conflict"
  )
  assert result.app.manage_apps is True
  assert result.app.connect_manage is True
  assert result.app.cross_app_access == "read"
  assert result.app.share_with_apps == "read"


def test_core_app_store_self_update_uses_the_same_conflict_policy(
  client, auth, tmp_path, bypass_url_validation,
):
  """The App Store has no upstream-wins exception."""
  base = "https://raw.githubusercontent.com/mobius-os/app-store/main/"
  m = {
    "id": "store",
    "name": "App Store",
    "version": "1.0.0",
    "description": "Core store",
    "entry": "index.jsx",
  }
  work, bare, _ = _make_clone_fixture(tmp_path, JSX_MULTI, "cards\n")
  r1 = _install_clone_fixture(
    client, auth, base, m, JSX_MULTI, "cards\n", bare, work=work,
  )
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "store" / "index.jsx"

  local = JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL STORE TITLE")
  jsx_file.write_text(local)

  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM STORE TITLE")
  _push_clone_fixture(work, bare, jsx_v2, "cards\n")
  r2 = _install_clone_fixture(
    client, auth, base, {**m, "version": "2.0.0"},
    jsx_v2, "cards\n", bare, work=work,
  )
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "conflict"
  assert payload["conflict_paths"] == ["index.jsx"]

  served = jsx_file.read_text()
  assert served == local
  assert "LOCAL STORE TITLE" in served
  assert "<<<<<<<" not in served

  from app.models import App
  from app.database import SessionLocal
  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "store").first()
    assert app.version == "1.0.0"
    assert app.jsx_source == JSX_MULTI
  finally:
    db.close()


def test_store_id_from_spoofed_path_still_preserves_local_conflict(
  client, auth, bypass_url_validation,
):
  """Only the exact raw.githubusercontent.com/mobius-os/app-store source is forced."""
  base = "https://example.test/raw.githubusercontent.com/mobius-os/app-store/main/"
  m = {
    **MANIFEST_NEWS,
    "id": "store",
    "name": "Spoof Store",
    "version": "1.0.0",
  }
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "store" / "index.jsx"

  local = JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL SPOOF TITLE")
  jsx_file.write_text(local)

  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM SPOOF TITLE")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "conflict"
  assert "index.jsx" in payload["conflict_paths"]
  # NOT force-take-upstream (only the exact mobius-os/app-store source is) — a
  # normal conflict leaves local source untouched until the owner resolves.
  served = jsx_file.read_text()
  assert served == local
  assert "<<<<<<<" not in served and "UPSTREAM SPOOF TITLE" not in served


def test_update_preview_clean_returns_upstream_diff(
  client, auth, bypass_url_validation,
):
  """Preview on a clean update reports clean status and the upstream diff."""
  base = "https://preview-clean.test/repo/"
  m = {**MANIFEST_NEWS, "id": "preview-clean"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  jsx_v2 = JSX_MULTI.replace("ORIGINAL FOOTER", "UPSTREAM FOOTER")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text

  preview = client.get(f"/api/apps/{app_id}/update-preview", headers=auth)
  assert preview.status_code == 200, preview.text
  payload = preview.json()
  assert payload["upstream_version"] == "2.0.0"
  assert payload["upstream_commit"]
  assert payload["conflict_paths"] == []
  assert payload["conflicts"] == []
  assert "UPSTREAM FOOTER" in payload["upstream_diff"]


def test_update_preview_reports_non_linear_local_history_for_review(
  client, auth, bypass_url_validation,
):
  """A merge commit in the local overlay is a controlled review outcome."""
  base = "https://preview-nonlinear.test/repo/"
  m = {**MANIFEST_NEWS, "id": "preview-nonlinear"}
  installed = _install_v1(client, auth, base, m, JSX_MULTI)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]
  source_dir = Path(get_settings().data_dir) / "apps" / "preview-nonlinear"
  before = (source_dir / "index.jsx").read_text()

  with (
    patch(
      "app.install.read_pending_conflict_update_receipt",
      return_value={"replay_base": "recorded-base"},
    ),
    patch(
      "app.routes.apps._preview_overlay_conflicts",
      side_effect=app_git.OverlayNotLinear("local overlay is not linear"),
    ),
  ):
    preview = client.get(f"/api/apps/{app_id}/update-preview", headers=auth)

  assert preview.status_code == 409, preview.text
  assert preview.json()["detail"]["code"] == "local_history_not_linear"
  assert (source_dir / "index.jsx").read_text() == before
  assert not app_git.update_operation_in_progress(source_dir)


def test_update_candidate_preview_fetches_incoming_diff_without_mutation(
  client, auth, bypass_url_validation,
):
  """The pre-update review shows live source while leaving the app untouched."""
  base = "https://candidate-preview.test/repo/"
  m = {**MANIFEST_NEWS, "id": "candidate-preview"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "candidate-preview" / "index.jsx"
  before = jsx_file.read_text()

  jsx_v2 = JSX_MULTI.replace("ORIGINAL FOOTER", "INCOMING FOOTER")
  next_manifest = {**m, "version": "2.0.0"}
  responses = {
    base + "mobius.json": (200, json.dumps(next_manifest).encode()),
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v2 prompt"),
    base + "fetch.sh": (200, b""),
  }
  with patch(
    "app.routes.apps._fetch_update_candidate",
    return_value=_git_candidate(next_manifest, jsx_v2),
  ):
    preview = client.get(
      f"/api/apps/{app_id}/update-candidate-preview", headers=auth,
    )

  assert preview.status_code == 200, preview.text
  payload = preview.json()
  assert payload["upstream_version"] == "2.0.0"
  assert "INCOMING FOOTER" in payload["upstream_diff"]
  assert "ORIGINAL FOOTER" in payload["upstream_diff"]
  assert "a/index.jsx" in payload["upstream_diff"]
  assert "b/index.jsx" in payload["upstream_diff"]
  assert len(payload["source_digest"]) == 64
  assert jsx_file.read_text() == before

  changed_after_review = jsx_v2.replace("INCOMING FOOTER", "MOVED AGAIN")
  moved_responses = {
    **responses,
    base + "index.jsx": (200, changed_after_review.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(moved_responses),
  ):
    rejected = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
      "reviewed_source_digest": payload["source_digest"],
    })
  assert rejected.status_code == 409, rejected.text
  assert rejected.json()["detail"]["code"] == "update_changed"
  assert jsx_file.read_text() == before
  listed = client.get("/api/apps/", headers=auth).json()
  row = next(app for app in listed if app["id"] == app_id)
  assert row["version"] == "1.0.0"


def test_update_candidate_preview_uses_selected_same_repository_ref(
  client, auth, db, bypass_url_validation,
):
  """An explicit review may move a pin within the same GitHub repository."""
  from app import models

  base = "https://candidate-selected.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "selected-candidate"}
  installed = _install_v1(client, auth, base, manifest, JSX_MULTI)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]

  pinned_identity = (
    "https://raw.githubusercontent.com/hamzamerzic/app-selected/"
    "0123456789abcdef0123456789abcdef01234567"
    "#manifest-id=selected-candidate"
  )
  row = db.query(models.App).filter(models.App.id == app_id).one()
  row.manifest_url = pinned_identity
  db.commit()

  selected_url = (
    "https://raw.githubusercontent.com/hamzamerzic/"
    "app-selected/main/mobius.json"
  )
  next_manifest = {**manifest, "version": "2.0.0"}
  incoming = JSX_MULTI.replace("ORIGINAL FOOTER", "SELECTED REF FOOTER")
  with patch(
    "app.routes.apps._fetch_update_candidate",
    return_value=_git_candidate(next_manifest, incoming),
  ):
    preview = client.get(
      f"/api/apps/{app_id}/update-candidate-preview",
      headers=auth,
      params={"manifest_url": selected_url},
    )

  assert preview.status_code == 200, preview.text
  payload = preview.json()
  assert payload["upstream_version"] == "2.0.0"
  assert "SELECTED REF FOOTER" in payload["upstream_diff"]
  assert "ORIGINAL FOOTER" in payload["upstream_diff"]


def test_update_candidate_preview_rejects_a_different_catalog_app(
  client, auth, db, bypass_url_validation,
):
  """A manager cannot bind an installed row to another trusted package."""
  from app import models

  base = "https://candidate-mismatch.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "candidate-mismatch"}
  installed = _install_v1(client, auth, base, manifest, JSX)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]
  row = db.query(models.App).filter(models.App.id == app_id).one()
  row.manifest_url = (
    "https://raw.githubusercontent.com/mobius-os/app-one/main"
    "#manifest-id=candidate-mismatch"
  )
  db.commit()

  other_url = (
    "https://raw.githubusercontent.com/mobius-os/app-two/main/mobius.json"
  )
  responses = {
    other_url: (200, json.dumps(manifest).encode()),
    other_url.rsplit("/", 1)[0] + "/index.jsx": (200, JSX.encode()),
    other_url.rsplit("/", 1)[0] + "/icon.png": (200, _png_bytes()),
    other_url.rsplit("/", 1)[0] + "/prompt.md": (200, PROMPT.encode()),
    other_url.rsplit("/", 1)[0] + "/fetch.sh": (200, b""),
  }
  with patch(
    "app.routes.apps._fetch_update_candidate",
    return_value=_git_candidate(manifest, JSX),
  ):
    preview = client.get(
      f"/api/apps/{app_id}/update-candidate-preview",
      headers=auth,
      params={"manifest_url": other_url},
    )
  assert preview.status_code == 409, preview.text
  assert "does not match" in preview.json()["detail"]


def test_deferred_replay_reports_changed_candidate_as_structured_recovery(
  db, bypass_url_validation,
):
  """Explicit replay distinguishes a stale receipt from a compile failure."""
  from fastapi import HTTPException
  from app import install

  base = "https://pending-candidate.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "pending-candidate"}
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b"#!/bin/sh\n"),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), pytest.raises(HTTPException) as caught:
    asyncio.run(install.install_from_manifest(
      db,
      manifest_url=None,
      manifest=manifest,
      raw_base=base,
      source="store",
      expected_app_id=42,
      expected_upstream_commit="upstream-sha",
      expected_candidate_digest="0" * 64,
    ))

  assert caught.value.status_code == 409
  assert caught.value.detail == {
    "code": "pending_update_changed",
    "message": (
      "The pending update changed upstream. "
      "Review the latest update and start again."
    ),
  }


def test_update_preview_accepts_app_token_with_manage_apps_for_other_app(
  client, db, auth, bypass_url_validation,
):
  """The App Store can review update previews for apps it manages."""
  from app.auth import create_access_token
  base = "https://preview-manager.test/repo/"
  m = {**MANIFEST_NEWS, "id": "preview-manager-target"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  target_app_id = r1.json()["id"]

  jsx_v2 = JSX_MULTI.replace("ORIGINAL FOOTER", "MANAGED UPDATE FOOTER")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text

  manager_app_id = _seed_app_with_perms(
    db, perms_cross_write="none", manage_apps=True,
  )
  db.commit()
  token = create_access_token({
    "sub": "test", "scope": "app", "app_id": manager_app_id,
  })

  preview = client.get(
    f"/api/apps/{target_app_id}/update-preview",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert preview.status_code == 200, preview.text
  payload = preview.json()
  assert payload["app_id"] == target_app_id
  assert payload["upstream_version"] == "2.0.0"


def test_update_preview_rejects_ordinary_app_token_for_other_app(
  client, db, auth, bypass_url_validation,
):
  """App tokens without manage_apps cannot read another app's source preview."""
  from app.auth import create_access_token
  base = "https://preview-denied.test/repo/"
  m = {**MANIFEST_NEWS, "id": "preview-denied-target"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  target_app_id = r1.json()["id"]

  caller_app_id = _seed_app_with_perms(
    db, perms_cross_write="none", manage_apps=False,
  )
  db.commit()
  token = create_access_token({
    "sub": "test", "scope": "app", "app_id": caller_app_id,
  })

  preview = client.get(
    f"/api/apps/{target_app_id}/update-preview",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert preview.status_code == 403, preview.text
  assert "manage_apps" in preview.json()["detail"]


def test_update_preview_conflict_returns_real_markers_without_live_mutation(
  client, auth, bypass_url_validation,
):
  """Preview materializes conflict markers in a throwaway worktree only."""
  base = "https://preview-conflict.test/repo/"
  m = {**MANIFEST_NEWS, "id": "preview-conflict"}
  r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "preview-conflict" / "index.jsx"

  local = JSX_MULTI.replace("ORIGINAL TITLE", "AGENT TITLE")
  jsx_file.write_text(local)
  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "conflict"

  # The conflict update did not materialize markers in the LIVE tree. The preview
  # reads from a throwaway worktree and must not mutate the live tree either.
  before_preview = jsx_file.read_text()
  assert before_preview == local
  assert "<<<<<<<" not in before_preview
  assert not (jsx_file.parent / ".git" / "MERGE_HEAD").exists()

  preview = client.get(f"/api/apps/{app_id}/update-preview", headers=auth)
  assert preview.status_code == 200, preview.text
  payload = preview.json()
  assert payload["status"] == "conflict"
  assert payload["upstream_version"] == "2.0.0"
  assert payload["conflict_paths"] == ["index.jsx"]
  assert payload["upstream_commit"]
  assert "UPSTREAM TITLE" in payload["upstream_diff"]
  assert payload["conflicts"][0]["path"] == "index.jsx"
  markers = payload["conflicts"][0]["merged_with_markers"]
  assert "<<<<<<<" in markers
  assert "=======" in markers
  assert ">>>>>>>" in markers
  assert "AGENT TITLE" in markers
  assert "UPSTREAM TITLE" in markers
  assert jsx_file.read_text() == before_preview
  assert not (jsx_file.parent / ".git" / "MERGE_HEAD").exists()


# --------------------------------------------------------------------------
# Predecessor adoption — a renamed app (or a baked predecessor installed
# without a manifest_url) UPDATES the existing row instead of duplicating it.
# --------------------------------------------------------------------------


def _simple_manifest(
  app_id, version="1.0.0", previous_id=None, previous_manifest_url=None,
):
  """A minimal installable manifest (no schedule/icon/seeds) for the
  adoption tests, so the response map only needs mobius.json + index.jsx."""
  m = {
    "id": app_id,
    "name": app_id.replace("-", " ").title(),
    "version": version,
    "description": f"{app_id} app",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  if previous_id is not None:
    m["previous_id"] = previous_id
  if previous_manifest_url is not None:
    m["previous_manifest_url"] = previous_manifest_url
  return m


def _install_simple(client, auth, base, manifest, jsx=JSX):
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, jsx.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


def test_install_response_includes_capability_flags(
  client, auth, db, bypass_url_validation,
):
  """The immediate install response must match the persisted app capabilities."""
  base = "https://caps.test/repo/"
  manifest = _simple_manifest("capability-app")
  manifest["embeds_agent"] = True
  manifest["permissions"]["manage_apps"] = True
  manifest["permissions"]["github_access"] = True
  manifest["permissions"]["manage_skills"] = True
  manifest["permissions"]["github_connect"] = True
  manifest["permissions"]["filesystem_access"] = True
  manifest["permissions"]["connect_manage"] = True

  r = _install_simple(client, auth, base, manifest)

  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["embeds_agent"] is True
  assert payload["manage_apps"] is True
  assert payload["github_access"] is True
  assert payload["manage_skills"] is True
  assert payload["github_connect"] is True
  assert payload["filesystem_access"] is True

  listed = client.get("/api/apps/", headers=auth).json()
  row = next(app for app in listed if app["id"] == payload["id"])
  assert row["embeds_agent"] is True
  assert row["manage_apps"] is True
  assert row["github_access"] is True
  assert row["manage_skills"] is True
  assert row["github_connect"] is True
  assert row["filesystem_access"] is True
  persisted = db.query(models.App).filter(models.App.id == payload["id"]).one()
  assert persisted.connect_manage is True


def test_install_rejects_non_boolean_filesystem_capability(
  client, auth, bypass_url_validation,
):
  manifest = _simple_manifest("bad-filesystem-capability")
  manifest["permissions"]["filesystem_access"] = "yes"
  response = _install_simple(client, auth, "https://bad-fs-cap.test/repo/", manifest)
  assert response.status_code == 400
  assert "filesystem_access" in response.text


def test_install_rejects_non_boolean_github_connect_capability(
  client, auth, bypass_url_validation,
):
  manifest = _simple_manifest("bad-github-connect-capability")
  manifest["permissions"]["github_connect"] = "yes"
  response = _install_simple(
    client,
    auth,
    "https://bad-github-connect-cap.test/repo/",
    manifest,
  )
  assert response.status_code == 400
  assert "github_connect" in response.text


def test_install_validates_previous_id_field(client, auth, bypass_url_validation):
  """`previous_id` is held to the same slug rules as `id`, and may not equal
  `id` (a self-pointer would be a no-op that only confuses the migration)."""
  base = "https://prev-bad.test/repo/"
  # Purely numeric — reserved for the storage path, same as `id`.
  bad_numeric = _simple_manifest("renamed", previous_id="123")
  r = _install_simple(client, auth, base, bad_numeric)
  assert r.status_code == 400, r.text
  assert "previous_id" in r.text

  # Equal to id.
  base2 = "https://prev-self.test/repo/"
  self_ref = _simple_manifest("renamed", previous_id="renamed")
  r2 = _install_simple(client, auth, base2, self_ref)
  assert r2.status_code == 400, r2.text
  assert "previous_id" in r2.text

  # A repository predecessor is meaningful only as part of an id rename.
  missing_id = _simple_manifest(
    "renamed", previous_manifest_url="https://example.test/old/mobius.json",
  )
  r3 = _install_simple(client, auth, "https://prev-missing.test/repo/", missing_id)
  assert r3.status_code == 400, r3.text
  assert "requires `previous_id`" in r3.text

  # Credentials, mutable query parameters, and non-HTTPS predecessors are not
  # durable package identities.
  bad_url = _simple_manifest(
    "renamed", previous_id="old",
    previous_manifest_url="http://user:pass@example.test/old?ref=main",
  )
  r4 = _install_simple(client, auth, "https://prev-url.test/repo/", bad_url)
  assert r4.status_code == 400, r4.text
  assert "absolute HTTPS URL" in r4.text


def test_permanent_identity_manifest_requires_stable_service_id(
  client, auth, bypass_url_validation,
):
  manifest = _simple_manifest("social")
  manifest["package_id"] = "app.mobius.social"
  manifest["source_files"] = ["service.py"]
  manifest["service"] = {"entry": "service.py", "access": "public"}

  response = _install_simple(
    client, auth, "https://identity.test/social/", manifest,
  )

  assert response.status_code == 400
  assert "service.id" in response.text


def test_service_identity_collision_is_a_clear_install_conflict(
  client, auth, bypass_url_validation,
):
  service_source = b'import json, sys\njson.dump({"status": 200}, sys.stdout)\n'

  def install(app_id: str, package_id: str):
    base = f"https://identity.test/{app_id}/"
    manifest = _simple_manifest(app_id)
    manifest.update({
      "package_id": package_id,
      "source_files": ["service.py"],
      "service": {"id": "shared-api", "entry": "service.py"},
    })
    responses = {
      base + "mobius.json": (200, json.dumps(manifest).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "service.py": (200, service_source),
    }
    with patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ):
      return client.post(
        "/api/apps/install", headers=auth,
        json={"manifest_url": base + "mobius.json"},
      )

  first = install("first-service", "app.example.first")
  second = install("second-service", "app.example.second")

  assert first.status_code == 201, first.text
  assert second.status_code == 409
  assert "service identity is already installed" in second.text


def test_implicit_service_identity_follows_unique_slug_on_install_and_update(
  client, auth, db, bypass_url_validation,
):
  service_source = b'import json, sys\njson.dump({"status": 200}, sys.stdout)\n'

  def install(base: str, version: str):
    manifest = _simple_manifest("shared-service", version=version)
    manifest.update({
      "source_files": ["service.py"],
      "service": {"entry": "service.py"},
    })
    responses = {
      base + "mobius.json": (200, json.dumps(manifest).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "service.py": (200, service_source),
    }
    with patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ):
      return client.post(
        "/api/apps/install", headers=auth,
        json={"manifest_url": base + "mobius.json"},
      )

  first = install("https://implicit-one.test/repo/", "1.0.0")
  second = install("https://implicit-two.test/repo/", "1.0.0")
  updated = install("https://implicit-two.test/repo/", "2.0.0")

  assert first.status_code == 201, first.text
  assert second.status_code == 201, second.text
  assert updated.status_code == 201, updated.text
  apps = db.query(models.App).filter(
    models.App.slug.like("shared-service%"),
  ).order_by(models.App.id).all()
  assert [(app.slug, app.service_id) for app in apps] == [
    ("shared-service", "shared-service"),
    ("shared-service-2", "shared-service-2"),
  ]
  assert [app.capability_contract["service"]["id"] for app in apps] == [
    "shared-service", "shared-service-2",
  ]


def test_service_rename_requires_and_routes_one_reviewed_transition_alias(
  client, auth, db, bypass_url_validation,
):
  base = "https://service-rename.test/repo/"
  service_source = (
    b'import json, sys\n'
    b'json.load(sys.stdin)\n'
    b'json.dump({"status": 200, "body": {"ok": True}}, sys.stdout)\n'
  )

  def install(version: str, service: dict):
    manifest = _simple_manifest("social", version=version)
    manifest.update({
      "package_id": "app.example.social",
      "source_files": ["service.py"],
      "service": {**service, "entry": "service.py", "access": "public"},
    })
    responses = {
      base + "mobius.json": (200, json.dumps(manifest).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "service.py": (200, service_source),
    }
    with patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ):
      return client.post(
        "/api/apps/install", headers=auth,
        json={"manifest_url": base + "mobius.json"},
      )

  initial = install("1.0.0", {"id": "common"})
  unsafe = install("2.0.0", {"id": "social"})
  transition = install(
    "2.0.0", {"id": "social", "aliases": ["common"]},
  )

  assert initial.status_code == 201, initial.text
  assert unsafe.status_code == 409
  assert "previous identity" in unsafe.text
  assert transition.status_code == 201, transition.text
  assert client.get("/api/app-services/social/status").status_code == 200
  assert client.get("/api/app-services/common/status").status_code == 200
  app = db.query(models.App).filter_by(package_id="app.example.social").one()
  assert app.service_id == "social"
  assert db.get(models.AppServiceAlias, "common").app_id == app.id

  retired = install("3.0.0", {"id": "social"})
  assert retired.status_code == 201, retired.text
  assert client.get("/api/app-services/social/status").status_code == 200
  assert client.get("/api/app-services/common/status").status_code == 404
  assert db.get(models.AppServiceAlias, "common") is None


def test_package_identity_adopts_a_github_owner_transfer_by_repository_id(
  client, auth, bypass_url_validation,
):
  old_base = "https://raw.githubusercontent.com/alice/app-social/main/"
  new_base = "https://raw.githubusercontent.com/acme/app-social/main/"
  first = _simple_manifest("common")
  installed = _install_simple(client, auth, old_base, first)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]

  moved = _simple_manifest(
    "social",
    version="2.0.0",
    previous_id="common",
    previous_manifest_url=old_base + "mobius.json",
  )
  moved["package_id"] = "urn:uuid:2e824551-03cd-5166-b52a-8530f0c02c50"
  metadata = json.dumps({
    "id": 12345, "full_name": "acme/app-social",
  }).encode()
  responses = {
    new_base + "mobius.json": (200, json.dumps(moved).encode()),
    new_base + "index.jsx": (200, JSX.encode()),
    "https://api.github.com/repos/acme/app-social": (200, metadata),
    "https://api.github.com/repos/alice/app-social": (200, metadata),
  }
  with patch(
    "app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses),
  ):
    adopted = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": new_base + "mobius.json"},
    )

  assert adopted.status_code == 201, adopted.text
  assert adopted.json()["mode"] == "update", adopted.json()
  assert adopted.json()["id"] == app_id
  assert adopted.json()["slug"] == "social"
  assert adopted.json()["package_id"] == moved["package_id"]


def test_package_identity_rejects_a_different_repository_without_old_source_handoff(
  client, auth, bypass_url_validation,
):
  old_base = "https://raw.githubusercontent.com/alice/app-kanban/main/"
  new_base = "https://raw.githubusercontent.com/acme/app-kanban/main/"
  package_id = "urn:uuid:9e136d55-9631-585a-aa75-a745a5dc8f2e"
  old = _simple_manifest("kanban")
  old["package_id"] = package_id
  old_responses = {
    old_base + "mobius.json": (200, json.dumps(old).encode()),
    old_base + "index.jsx": (200, JSX.encode()),
    "https://api.github.com/repos/alice/app-kanban": (
      200, json.dumps({"id": 100, "full_name": "alice/app-kanban"}).encode(),
    ),
  }
  with patch(
    "app.install.httpx.AsyncClient", side_effect=_fake_async_client(old_responses),
  ):
    installed = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": old_base + "mobius.json"},
    )
  assert installed.status_code == 201, installed.text

  new = _simple_manifest("kanban", version="2.0.0")
  new["package_id"] = package_id
  new_responses = {
    new_base + "mobius.json": (200, json.dumps(new).encode()),
    new_base + "index.jsx": (200, JSX.encode()),
    old_base + "mobius.json": (200, json.dumps(old).encode()),
    "https://api.github.com/repos/acme/app-kanban": (
      200, json.dumps({"id": 200, "full_name": "acme/app-kanban"}).encode(),
    ),
  }
  with patch(
    "app.install.httpx.AsyncClient", side_effect=_fake_async_client(new_responses),
  ):
    refused = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": new_base + "mobius.json"},
    )

  assert refused.status_code == 409
  assert "trusted handoff" in refused.text


def test_package_identity_accepts_a_different_repository_named_by_old_source(
  client, auth, bypass_url_validation,
):
  old_base = "https://raw.githubusercontent.com/alice/app-kanban/main/"
  new_base = "https://raw.githubusercontent.com/acme/app-kanban/main/"
  package_id = "urn:uuid:9e136d55-9631-585a-aa75-a745a5dc8f2e"
  old = _simple_manifest("kanban")
  old.update({
    "package_id": package_id,
    "moved_to": {"manifest_url": new_base + "mobius.json"},
  })
  old_api = json.dumps({
    "id": 100, "full_name": "alice/app-kanban",
  }).encode()
  new_api = json.dumps({
    "id": 200, "full_name": "acme/app-kanban",
  }).encode()
  first_responses = {
    old_base + "mobius.json": (200, json.dumps(old).encode()),
    old_base + "index.jsx": (200, JSX.encode()),
    "https://api.github.com/repos/alice/app-kanban": (200, old_api),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(first_responses),
  ):
    installed = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": old_base + "mobius.json"},
    )
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]

  new = _simple_manifest("kanban", version="2.0.0")
  new["package_id"] = package_id
  moved_responses = {
    new_base + "mobius.json": (200, json.dumps(new).encode()),
    new_base + "index.jsx": (200, JSX.encode()),
    old_base + "mobius.json": (200, json.dumps(old).encode()),
    "https://api.github.com/repos/acme/app-kanban": (200, new_api),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(moved_responses),
  ):
    moved = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": new_base + "mobius.json"},
    )

  assert moved.status_code == 201, moved.text
  assert moved.json()["id"] == app_id
  assert moved.json()["package_id"] == package_id
  assert moved.json()["manifest_url"].startswith(new_base.rstrip("/"))


def test_rename_adopts_predecessor_across_same_owner_repository_move(
  client, auth, bypass_url_validation,
):
  """A package and repository rename preserves the row, storage, and data."""
  old_base = "https://raw.githubusercontent.com/acme/app-old/main/"
  new_base = "https://raw.githubusercontent.com/acme/app-new/main/"

  first = _install_simple(client, auth, old_base, _simple_manifest("old"))
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]
  old_source = Path(get_settings().data_dir) / "apps" / "old"
  storage_file = (
    Path(get_settings().data_dir) / "apps" / str(app_id) / "data.json"
  )
  storage_file.parent.mkdir(parents=True, exist_ok=True)
  storage_file.write_text('{"kept": true}')

  renamed = _install_simple(
    client,
    auth,
    new_base,
    _simple_manifest(
      "new",
      version="2.0.0",
      previous_id="old",
      previous_manifest_url=old_base + "mobius.json",
    ),
  )

  assert renamed.status_code == 201, renamed.text
  assert renamed.json()["mode"] == "update"
  assert renamed.json()["id"] == app_id
  assert renamed.json()["slug"] == "new"
  assert storage_file.read_text() == '{"kept": true}'
  new_source = Path(get_settings().data_dir) / "apps" / "new"
  assert not old_source.exists()
  assert app_git.origin_url(new_source) == "https://github.com/acme/app-new.git"


def test_repository_move_cannot_adopt_package_from_another_owner(
  client, auth, bypass_url_validation,
):
  old_base = "https://raw.githubusercontent.com/alice/app-old/main/"
  new_base = "https://raw.githubusercontent.com/bob/app-new/main/"
  first = _install_simple(client, auth, old_base, _simple_manifest("old"))
  assert first.status_code == 201, first.text
  refused = _install_simple(
    client,
    auth,
    new_base,
    _simple_manifest(
      "new",
      previous_id="old",
      previous_manifest_url=old_base + "mobius.json",
    ),
  )

  assert refused.status_code == 400, refused.text
  assert "owned by the same account" in refused.text


def test_rename_adopts_predecessor_row_and_moves_source_dir(
  client, auth, bypass_url_validation,
):
  """(a) install id=gym, then install id=workout + previous_id=gym from the
  SAME base. The second install ADOPTS the gym row: same numeric id (no new
  row), final slug == 'workout', source_dir moved to .../apps/workout, the old
  gym dir is gone, and the id-keyed storage tree is preserved across the move."""
  base = "https://rename.test/repo/"
  data_dir = Path(get_settings().data_dir)

  r1 = _install_simple(client, auth, base, _simple_manifest("gym"))
  assert r1.status_code == 201, r1.text
  gym_id = r1.json()["id"]
  assert r1.json()["slug"] == "gym"

  # App data lives under the id-keyed storage tree; seed a file to prove it
  # survives the rename (the move never touches /data/apps/<id>).
  storage_file = data_dir / "apps" / str(gym_id) / "log.json"
  storage_file.parent.mkdir(parents=True, exist_ok=True)
  storage_file.write_text('{"workouts": 3}')
  assert (data_dir / "apps" / "gym" / "index.jsx").exists()

  r2 = _install_simple(
    client, auth, base,
    _simple_manifest("workout", version="2.0.0", previous_id="gym"),
  )
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "update"
  assert payload["id"] == gym_id          # SAME row — no duplicate
  assert payload["slug"] == "workout"
  assert payload["version"] == "2.0.0"

  # Only one app row total.
  listed = client.get("/api/apps/", headers=auth).json()
  assert len([a for a in listed if a["id"] == gym_id]) == 1
  assert len(listed) == 1

  # Source dir moved; old gym dir gone.
  assert (data_dir / "apps" / "workout" / "index.jsx").exists()
  assert not (data_dir / "apps" / "gym").exists()
  # Storage (id-keyed) preserved untouched.
  assert storage_file.read_text() == '{"workouts": 3}'

  # The identity is re-stamped: re-installing id=workout now hits the canonical
  # match (update), not adoption, and still doesn't duplicate.
  r3 = _install_simple(
    client, auth, base, _simple_manifest("workout", version="3.0.0"),
  )
  assert r3.status_code == 201, r3.text
  assert r3.json()["mode"] == "update"
  assert r3.json()["id"] == gym_id


def test_catalog_rename_refuses_unrelated_git_history_without_proof(
  client, auth, bypass_url_validation,
):
  """A catalog rename cannot invent ancestry for an independent history."""
  base = "https://raw.githubusercontent.com/mobius-os/app-social/main/"
  old_manifest = _simple_manifest("common")
  old_manifest.update({
    "source_files": ["service.py"],
    "service": {"entry": "service.py", "access": "public"},
  })
  new_manifest = _simple_manifest(
    "social", version="2.0.0", previous_id="common",
  )
  new_manifest.update({
    "package_id": "urn:uuid:33fae9ec-b2fb-4351-8a4e-85bcc362b546",
    "source_files": ["service.py"],
    "service": {
      "id": "social", "entry": "service.py", "access": "public",
      "aliases": ["common"],
    },
  })
  new_index = JSX.replace("Hello", "Social")
  service_source = b'import json, sys\njson.dump({"status": 200}, sys.stdout)\n'
  src = Path(get_settings().data_dir) / "apps" / "common"
  legacy = create_local_app(
    client,
    auth,
    name="Common",
    description="Local predecessor",
    jsx_source=JSX,
    source_dir=src,
  )
  (src / "service.py").write_bytes(service_source)
  (src / "mobius.json").write_text(json.dumps(old_manifest))
  applied = client.post(
    "/api/apps/apply", headers=auth, json={"source_dir": str(src)},
  )
  assert applied.status_code == 200, applied.text
  app_id = legacy["id"]

  app_git._run(
    src, "remote", "add", "origin",
    "https://github.com/mobius-os/app-social.git",
  )
  # Model a legacy checkout whose complete accepted tree already matches the
  # catalog rename, but whose database identity has not yet been adopted.
  (src / "mobius.json").write_text(json.dumps(new_manifest))
  (src / "index.jsx").write_text(new_index)
  app_git._run(src, "add", "--", "index.jsx", "mobius.json", "service.py")
  tree = app_git._run(src, "write-tree").stdout.strip()
  unrelated_main = app_git._run(
    src, "commit-tree", tree, "-m", "independent accepted source",
  ).stdout.strip()
  app_git._run(src, "update-ref", "refs/heads/main", unrelated_main)
  app_git._run(src, "reset", "--hard", unrelated_main)

  from app.database import SessionLocal
  from app.models import App
  db = SessionLocal()
  try:
    row = db.query(App).filter(App.id == app_id).one()
    row.manifest_url = None
    row.upstream_commit = None
    db.commit()
  finally:
    db.close()

  storage = Path(get_settings().data_dir) / "apps" / str(app_id) / "kept.json"
  storage.parent.mkdir(parents=True, exist_ok=True)
  storage.write_text('{"kept": true}')
  updated_responses = {
    base + "mobius.json": (200, json.dumps(new_manifest).encode()),
    base + "index.jsx": (200, new_index.encode()),
    base + "service.py": (200, service_source),
    "https://api.github.com/repos/mobius-os/app-social": (
      200,
      json.dumps({
        "id": 12345, "full_name": "mobius-os/app-social",
      }).encode(),
    ),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(updated_responses),
  ):
    updated = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert updated.status_code == 409, updated.text
  assert src.exists()
  assert (src / "index.jsx").read_text() == new_index
  assert storage.read_text() == '{"kept": true}'
  from app.database import SessionLocal
  from app.models import App
  db = SessionLocal()
  try:
    row = db.query(App).filter(App.id == app_id).one()
    assert row.slug == "common"
  finally:
    db.close()


def test_rename_restamps_identity_when_source_is_already_at_target(
  client, auth, db, bypass_url_validation,
):
  """A partially completed rename converges instead of staying on its alias."""
  from app import models

  base = "https://rename-resume.test/repo/"
  data_dir = Path(get_settings().data_dir)

  first = _install_simple(client, auth, base, _simple_manifest("gym"))
  assert first.status_code == 201, first.text
  app_id = first.json()["id"]

  old_source = data_dir / "apps" / "gym"
  target_source = data_dir / "apps" / "workout"
  os.rename(old_source, target_source)
  row = db.query(models.App).filter_by(id=app_id).one()
  row.slug = "workout"
  row.source_dir = str(target_source)
  db.commit()

  resumed = _install_simple(
    client, auth, base,
    _simple_manifest("workout", version="2.0.0", previous_id="gym"),
  )

  assert resumed.status_code == 201, resumed.text
  assert resumed.json()["id"] == app_id
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert row.slug == "workout"
  assert row.source_dir == str(target_source)
  assert row.manifest_url == base.rstrip("/") + "#manifest-id=workout"
  assert not old_source.exists()

  canonical = _install_simple(
    client, auth, base, _simple_manifest("workout", version="3.0.0"),
  )
  assert canonical.status_code == 201, canonical.text
  assert canonical.json()["id"] == app_id


def test_previous_id_matching_nothing_is_a_fresh_install(
  client, auth, bypass_url_validation,
):
  """(c) a previous_id that matches no installed app falls through to a
  normal fresh install (new row, mode='install')."""
  base = "https://no-pred.test/repo/"
  r = _install_simple(
    client, auth, base,
    _simple_manifest("brandnew", previous_id="never-existed"),
  )
  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "install"
  assert payload["slug"] == "brandnew"
  assert len(client.get("/api/apps/", headers=auth).json()) == 1


def test_previous_id_ignored_when_canonical_match_exists(
  client, auth, bypass_url_validation,
):
  """(d) when a workout row already exists (manifest_url match), previous_id is
  ignored: it's a normal update of workout and the gym row is left untouched."""
  base = "https://both.test/repo/"
  data_dir = Path(get_settings().data_dir)

  # Pre-existing gym app from a DIFFERENT base (so its canonical url differs).
  gym_base = "https://both-gym.test/repo/"
  rg = _install_simple(client, auth, gym_base, _simple_manifest("gym"))
  assert rg.status_code == 201, rg.text
  gym_id = rg.json()["id"]

  # First install of workout (fresh) from `base`.
  rw1 = _install_simple(
    client, auth, base,
    _simple_manifest("workout", previous_id="gym"),
  )
  assert rw1.status_code == 201, rw1.text
  assert rw1.json()["mode"] == "install"
  workout_id = rw1.json()["id"]
  assert workout_id != gym_id

  # Second install of workout (canonical match exists) — previous_id is
  # ignored, gym is NOT adopted/moved.
  rw2 = _install_simple(
    client, auth, base,
    _simple_manifest("workout", version="2.0.0", previous_id="gym"),
  )
  assert rw2.status_code == 201, rw2.text
  assert rw2.json()["mode"] == "update"
  assert rw2.json()["id"] == workout_id

  # gym row untouched: still present, still at its own slug + source dir.
  listed = client.get("/api/apps/", headers=auth).json()
  gym_row = next(a for a in listed if a["id"] == gym_id)
  assert gym_row["slug"] == "gym"
  assert (data_dir / "apps" / "gym" / "index.jsx").exists()
  assert len(listed) == 2


def test_rename_keeps_old_slug_when_target_taken(
  client, auth, bypass_url_validation,
):
  """(e) rename when the target slug is already claimed by ANOTHER app: keep the
  old slug, emit the 'could not rename' warning, and still adopt the same row
  (no duplicate)."""
  base = "https://rename-taken.test/repo/"
  data_dir = Path(get_settings().data_dir)

  # The predecessor we'll try to rename.
  r1 = _install_simple(client, auth, base, _simple_manifest("gym"))
  assert r1.status_code == 201, r1.text
  gym_id = r1.json()["id"]

  # Another app already occupies the target slug 'workout' (different base).
  other_base = "https://rename-other.test/repo/"
  r_other = _install_simple(
    client, auth, other_base, _simple_manifest("workout"),
  )
  assert r_other.status_code == 201, r_other.text
  other_id = r_other.json()["id"]
  assert other_id != gym_id

  # Rename gym -> workout. The target dir is taken, so the move is skipped.
  r2 = _install_simple(
    client, auth, base,
    _simple_manifest("workout", version="2.0.0", previous_id="gym"),
  )
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "update"
  assert payload["id"] == gym_id          # adopted the same row
  assert payload["slug"] == "gym"          # slug NOT changed
  assert any(
    "could not rename slug gym->workout" in w for w in payload["warnings"]
  )

  # Both apps still exist; neither was duplicated, the other app is intact.
  listed = client.get("/api/apps/", headers=auth).json()
  assert len(listed) == 2
  assert (data_dir / "apps" / "gym" / "index.jsx").exists()
  assert (data_dir / "apps" / "workout" / "index.jsx").exists()


# --- Multi-file mini-apps: index.jsx + sibling modules ---------------
#
# A multi-file app's `index.jsx` imports sibling modules (`cards.js`, …)
# declared in the manifest's `source_files`. Install must fetch them,
# write them next to the entry, and compile with Rolldown bundling the
# import graph. An update merges the whole tree so locally edited siblings
# survive. The JSX below imports a sibling so the compiled bundle proves
# Rolldown resolved + inlined it; the sibling spacing mirrors JSX_MULTI so
# git's line-based 3-way merge can interleave disjoint edits cleanly.

JSX_IMPORTS_CARDS = (
  "import { CARD_LABEL } from './cards.js'\n"
  "export default function App() {\n"
  "  return <div>{CARD_LABEL}</div>\n"
  "}\n"
)

CARDS_V1 = (
  "export const CARD_LABEL = 'CARDS_ORIGINAL'\n"
  "export const PAD_A = 1\n"
  "export const PAD_B = 2\n"
  "export const PAD_C = 3\n"
  "export const PAD_D = 4\n"
  "export const PAD_E = 5\n"
  "export const FOOTER = 'FOOTER_ORIGINAL'\n"
)

MANIFEST_MULTI = {
  "id": "multi-app",
  "name": "Multi App",
  "version": "1.0.0",
  "description": "Multi-file app",
  "entry": "index.jsx",
  "source_files": ["cards.js"],
  "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
}


def _install_multi(client, auth, base, manifest, jsx, cards):
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, jsx.encode()),
    base + "cards.js": (200, cards.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


CLONE_INDEX_V1 = (
  "import { CARD_LABEL, FOOTER } from './cards.js'\n"
  "export default function App() {\n"
  "  const title = 'TITLE_V1'\n"
  "  return <div>{title}{CARD_LABEL}{FOOTER}</div>\n"
  "}\n"
)

CLONE_CARDS_V1 = (
  "export const CARD_LABEL = 'CARD_V1'\n"
  "export const FOOTER = 'FOOTER_V1'\n"
)


def _fixture_commit(repo: Path, msg: str) -> str:
  subprocess.run(
    [
      "git",
      "-c", "user.name=Test",
      "-c", "user.email=test@example.invalid",
      "-C", str(repo),
      "add", ".",
    ],
    check=True,
    env=app_git._git_env(repo),
  )
  subprocess.run(
    [
      "git",
      "-c", "user.name=Test",
      "-c", "user.email=test@example.invalid",
      "-C", str(repo),
      "commit", "-q", "-m", msg,
    ],
    check=True,
    env=app_git._git_env(repo),
  )
  return subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True, env=app_git._git_env(repo),
  ).stdout.strip()


def _make_clone_fixture(tmp_path, index: str, cards: str):
  work = tmp_path / "catalog-work"
  bare = tmp_path / "catalog.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
  (work / "index.jsx").write_text(index, encoding="utf-8")
  (work / "cards.js").write_text(cards, encoding="utf-8")
  head = _fixture_commit(work, "v1")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(work), str(bare)],
    check=True,
    env=app_git._git_env(work),
  )
  return work, bare, head


def _push_clone_fixture(work: Path, bare: Path, index: str, cards: str) -> str:
  (work / "index.jsx").write_text(index, encoding="utf-8")
  (work / "cards.js").write_text(cards, encoding="utf-8")
  head = _fixture_commit(work, "update")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True,
    env=app_git._git_env(work),
  )
  return head


def _install_clone_fixture(
  client, auth, base, manifest, index, cards, bare, *,
  work,
  include_source_file=False,
):
  (work / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  if app_git._run(work, "status", "--porcelain", read_only=True).stdout.strip():
    _fixture_commit(work, "commit package manifest")
    subprocess.run(
      ["git", "-C", str(work), "push", "-q", str(bare), "main"],
      check=True, env=app_git._git_env(work),
    )
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, index.encode()),
  }
  if include_source_file:
    responses[base + "cards.js"] = (200, cards.encode())
  with patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ), patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


def test_plain_install_does_not_abort_active_conflict_resolution(
  client, auth, db, tmp_path, bypass_url_validation,
):
  """A second Store request cannot erase a resolver's in-progress work."""
  base = "https://raw.githubusercontent.com/acme/resolver-active/main/"
  manifest = {
    "id": "resolver-active", "name": "Resolver active", "version": "1.0.0",
    "description": "review fixture", "entry": "index.jsx", "permissions": {},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  first = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1,
    bare, work=work,
  )
  assert first.status_code == 201, first.text
  row = db.get(models.App, first.json()["id"])
  repo = Path(row.source_dir)
  before = app_git.head_sha(repo, app_git.LOCAL_BRANCH)
  git_dir = Path(app_git._run(
    repo, "rev-parse", "--absolute-git-dir", read_only=True,
  ).stdout.strip())
  (git_dir / "rebase-merge").mkdir()

  second = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    CLONE_INDEX_V1.replace("V1", "V2"), CLONE_CARDS_V1,
    bare, work=work,
  )

  assert second.status_code == 409, second.text
  assert second.json()["detail"]["code"] == "update_resolution_required"
  assert (git_dir / "rebase-merge").is_dir()
  assert app_git.head_sha(repo, app_git.LOCAL_BRANCH) == before


def test_clone_update_fast_forward_uses_full_origin_tree(
  client, auth, tmp_path, bypass_url_validation,
):
  """A cloned no-local-edit update reads the full fetched git tree.

  The manifest intentionally does NOT declare cards.js. The HTTP update fetch
  therefore only has index.jsx; cards.js can update only if the clone fetch path
  replaces source_tree with read_ref_tree("upstream").
  """
  base = "https://raw.githubusercontent.com/acme/clone-ff/main/"
  manifest = {
    "id": "clone-ff",
    "name": "Clone FF",
    "version": "1.0.0",
    "description": "Clone fast-forward",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  r1 = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1, bare,
    work=work,
  )
  assert r1.status_code == 201, r1.text

  index_v2 = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_V2")
  cards_v2 = CLONE_CARDS_V1.replace("CARD_V1", "CARD_V2")
  _push_clone_fixture(work, bare, index_v2, cards_v2)
  r2 = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    index_v2, CLONE_CARDS_V1, bare, work=work,
  )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "fast_forward"
  src = Path(get_settings().data_dir) / "apps" / "clone-ff"
  assert (src / "index.jsx").read_text() == index_v2
  assert (src / "cards.js").read_text() == cards_v2
  new_head = app_git.head_sha(src, app_git.UPSTREAM_BRANCH)
  assert app_git.head_sha(src, app_git.UPSTREAM_BRANCH) == new_head
  origin_head = app_git._run(src, "rev-parse", "origin/main").stdout.strip()
  assert origin_head == new_head
  assert app_git.local_diverged_from(src, new_head) is False


def test_clone_update_diverged_clean_merge_carries_local_and_origin(
  client, auth, tmp_path, bypass_url_validation,
):
  """A cloned diverged update cleanly merges local edits with origin changes."""
  base = "https://raw.githubusercontent.com/acme/clone-clean/main/"
  manifest = {
    "id": "clone-clean",
    "name": "Clone Clean",
    "version": "1.0.0",
    "description": "Clone clean merge",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  r1 = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1, bare,
    work=work,
  )
  assert r1.status_code == 201, r1.text
  src = Path(get_settings().data_dir) / "apps" / "clone-clean"
  (src / "index.jsx").write_text(
    CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_LOCAL"),
    encoding="utf-8",
  )

  cards_v2 = CLONE_CARDS_V1.replace("FOOTER_V1", "FOOTER_V2")
  _push_clone_fixture(work, bare, CLONE_INDEX_V1, cards_v2)
  r2 = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    CLONE_INDEX_V1, CLONE_CARDS_V1, bare, work=work,
  )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "clean_merge"
  assert "TITLE_LOCAL" in (src / "index.jsx").read_text()
  assert (src / "cards.js").read_text() == cards_v2
  assert "<<<<<<<" not in (src / "index.jsx").read_text()


def test_clone_update_rebases_each_local_commit_without_squashing(
  client, auth, tmp_path, bypass_url_validation,
):
  base = "https://raw.githubusercontent.com/acme/clone-history/main/"
  manifest = {
    "id": "clone-history",
    "name": "Clone History",
    "version": "1.0.0",
    "description": "Preserve local Git history",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  installed = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1, bare,
    work=work,
  )
  assert installed.status_code == 201, installed.text
  src = Path(get_settings().data_dir) / "apps" / "clone-history"
  (src / "local-one.js").write_text("one\n")
  _fixture_commit(src, "owner change one")
  (src / "local-two.js").write_text("two\n")
  _fixture_commit(src, "owner change two")

  cards_v2 = CLONE_CARDS_V1.replace("FOOTER_V1", "FOOTER_V2")
  _push_clone_fixture(work, bare, CLONE_INDEX_V1, cards_v2)
  updated = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    CLONE_INDEX_V1, CLONE_CARDS_V1, bare, work=work,
  )

  assert updated.status_code == 201, updated.text
  assert updated.json()["divergence"] == "clean_merge"
  upstream = app_git.head_sha(src, app_git.UPSTREAM_BRANCH)
  assert app_git.ref_is_ancestor(src, upstream, "main") is True
  assert app_git._run(
    src, "log", "--format=%s", "--reverse", f"{upstream}..main",
  ).stdout.splitlines() == ["owner change one", "owner change two"]


def test_clone_update_keeps_owner_replacement_of_upstream_deleted_path(
  client, auth, tmp_path, bypass_url_validation,
):
  """Publishing a replay must not prune a path retained by that replay."""
  base = "https://raw.githubusercontent.com/acme/clone-retained/main/"
  manifest = {
    "id": "clone-retained",
    "name": "Clone retained",
    "version": "1.0.0",
    "description": "Retain owner source",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  (work / "notes.txt").write_text("upstream notes\n", encoding="utf-8")
  _fixture_commit(work, "add upstream notes")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True,
    env=app_git._git_env(work),
  )
  installed = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1, bare,
    work=work,
  )
  assert installed.status_code == 201, installed.text

  src = Path(get_settings().data_dir) / "apps" / "clone-retained"
  (src / "notes.txt").unlink()
  _fixture_commit(src, "owner removes upstream notes")
  (src / "notes.txt").write_text("owner replacement\n", encoding="utf-8")
  _fixture_commit(src, "owner restores notes")

  (work / "notes.txt").unlink()
  updated = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    CLONE_INDEX_V1, CLONE_CARDS_V1, bare, work=work,
  )

  assert updated.status_code == 201, updated.text
  assert updated.json()["divergence"] == "clean_merge"
  assert (src / "notes.txt").read_text() == "owner replacement\n"
  head = app_git.head_sha(src, app_git.LOCAL_BRANCH)
  assert app_git.read_blob(src, head, "notes.txt") == b"owner replacement\n"
  assert app_git._run(
    src, "status", "--porcelain", read_only=True,
  ).stdout == ""
  assert app_git.commit_local(src, "capture source") is None


def test_clone_update_conflict_keeps_served_old_source(
  client, auth, tmp_path, bypass_url_validation,
):
  """A cloned same-line local/origin edit returns conflict and leaves the DB
  source on the previously served version."""
  base = "https://raw.githubusercontent.com/acme/clone-conflict/main/"
  manifest = {
    "id": "clone-conflict",
    "name": "Clone Conflict",
    "version": "1.0.0",
    "description": "Clone conflict",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  r1 = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1, bare,
    work=work,
  )
  assert r1.status_code == 201, r1.text
  src = Path(get_settings().data_dir) / "apps" / "clone-conflict"
  local_index = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_LOCAL")
  (src / "index.jsx").write_text(local_index, encoding="utf-8")

  upstream_index = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_UPSTREAM")
  _push_clone_fixture(work, bare, upstream_index, CLONE_CARDS_V1)
  r2 = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    upstream_index, CLONE_CARDS_V1, bare, work=work,
  )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "conflict"
  assert "index.jsx" in r2.json()["conflict_paths"]
  worktree_source = (src / "index.jsx").read_text()
  assert worktree_source == local_index
  assert "<<<<<<<" not in worktree_source and ">>>>>>>" not in worktree_source
  assert not (src / ".git" / "MERGE_HEAD").exists()
  from app.database import SessionLocal
  from app.models import App
  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "clone-conflict").first()
    assert app.jsx_source == CLONE_INDEX_V1
    assert app.version == "1.0.0"
  finally:
    db.close()


def test_clone_conflict_resolution_keeps_rebased_commit_ids_on_finalize(
  client, auth, tmp_path, bypass_url_validation,
):
  base = "https://raw.githubusercontent.com/acme/clone-resolve/main/"
  manifest = {
    "id": "clone-resolve",
    "name": "Clone Resolve",
    "version": "1.0.0",
    "description": "Resolve with a real rebase",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  installed = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1, bare,
    work=work,
  )
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]
  src = Path(get_settings().data_dir) / "apps" / "clone-resolve"
  (src / "index.jsx").write_text(
    CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_LOCAL"),
  )
  _fixture_commit(src, "owner conflict choice")
  (src / "local.js").write_text("owner helper\n")
  _fixture_commit(src, "owner helper")

  upstream_index = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_UPSTREAM")
  _push_clone_fixture(
    work, bare, upstream_index, CLONE_CARDS_V1,
  )
  conflicted = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    upstream_index, CLONE_CARDS_V1, bare, work=work,
  )
  assert conflicted.status_code == 201, conflicted.text
  assert conflicted.json()["mode"] == "conflict"
  upstream = app_git.head_sha(src, app_git.UPSTREAM_BRANCH)

  resolver = client.post(
    f"/api/apps/{app_id}/conflict-resolver-chat",
    headers=auth,
    json={"resolution_policy": "preserve_local"},
  )
  assert resolver.status_code == 200, resolver.text
  assert app_git.rebase_in_progress(src)
  (src / "index.jsx").write_text(
    CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_RESOLVED"),
  )
  app_git._run(src, "add", "index.jsx")
  with patch.dict(os.environ, {"GIT_EDITOR": "true"}):
    continued = app_git._run(src, "rebase", "--continue", check=False)
  assert continued.returncode == 0, continued.stderr
  assert not app_git.rebase_in_progress(src)

  reviewed = client.post(
    "/api/apps/resolve-update/review",
    headers=auth,
    json={"source_dir": str(src)},
  )
  assert reviewed.status_code == 200, reviewed.text
  rebased_tip = app_git.head_sha(src, "main")
  rebased_commits = app_git._run(
    src, "log", "--format=%H%x00%s", "--reverse", f"{upstream}..main",
  ).stdout.splitlines()

  finalized = client.post(
    "/api/apps/resolve-update",
    headers=auth,
    json={
      "source_dir": str(src),
      "reviewed_tree_oid": reviewed.json()["tree_oid"],
    },
  )
  assert finalized.status_code == 200, finalized.text
  assert finalized.json()["mode"] == "updated"
  assert app_git.head_sha(src, "main") == rebased_tip
  assert app_git._run(
    src, "log", "--format=%H%x00%s", "--reverse", f"{upstream}..main",
  ).stdout.splitlines() == rebased_commits
  assert [line.split("\x00", 1)[1] for line in rebased_commits] == [
    "owner conflict choice", "owner helper",
  ]
  assert not (src / ".git" / "mobius-pending-update" / "receipt.json").exists()


def test_clone_update_fetch_failure_preserves_installed_git_revision(
  client, auth, tmp_path, bypass_url_validation,
):
  """A failed origin fetch never creates a parallel synthetic update."""
  base = "https://raw.githubusercontent.com/acme/clone-fallback/main/"
  manifest = {
    "id": "clone-fallback",
    "name": "Clone Fallback",
    "version": "1.0.0",
    "description": "Clone fallback",
    "entry": "index.jsx",
    "source_files": ["cards.js"],
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  r1 = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1, bare,
    work=work, include_source_file=True,
  )
  assert r1.status_code == 201, r1.text

  index_v2 = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_HTTP_V2")
  cards_v2 = CLONE_CARDS_V1.replace("CARD_V1", "CARD_HTTP_V2")
  _push_clone_fixture(work, bare, index_v2, cards_v2)
  with patch("app.app_git.fetch_upstream", side_effect=RuntimeError("offline")):
    r2 = _install_clone_fixture(
      client, auth, base, {**manifest, "version": "2.0.0"},
      index_v2, cards_v2, bare, work=work, include_source_file=True,
    )

  assert r2.status_code == 409, r2.text
  assert r2.json()["detail"]["code"] == "git_update_unavailable"
  src = Path(get_settings().data_dir) / "apps" / "clone-fallback"
  assert (src / "index.jsx").read_text() == CLONE_INDEX_V1
  assert (src / "cards.js").read_text() == CLONE_CARDS_V1


@pytest.mark.parametrize("drift_kind", ["source", "permissions"])
def test_real_git_review_drift_is_rejected_without_downgrade_and_retry_preserves_local_work(
  client, auth, tmp_path, bypass_url_validation, drift_kind,
):
  """A reviewed HTTP package must match the exact Git ref later fetched.

  The first remote tip deliberately drifts either executable source or a
  permission declaration after review. Rejection leaves managed refs and the
  row at v1, while the owner's local draft remains in the worktree. A later
  matching tip can be retried without losing that draft.
  """
  from app import install

  base = f"https://raw.githubusercontent.com/acme/review-drift-{drift_kind}/main/"
  manifest_v1 = {
    "id": f"review-drift-{drift_kind}",
    "name": "Review Drift",
    "version": "1.0.0",
    "description": "Git review drift",
    "entry": "index.jsx",
    "permissions": {"manage_apps": False},
  }
  index_v1 = JSX_MULTI
  work = tmp_path / f"drift-{drift_kind}-work"
  bare = tmp_path / f"drift-{drift_kind}.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
  (work / "mobius.json").write_text(json.dumps(manifest_v1), encoding="utf-8")
  (work / "index.jsx").write_text(index_v1, encoding="utf-8")
  first = _fixture_commit(work, "v1")
  subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)

  def http_responses(manifest, index):
    return {
      base + "mobius.json": (200, json.dumps(manifest).encode()),
      base + "index.jsx": (200, index.encode()),
    }

  with patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ), patch(
    "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(http_responses(manifest_v1, index_v1)),
  ):
    installed = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert installed.status_code == 201, installed.text

  source_dir = Path(get_settings().data_dir) / "apps" / manifest_v1["id"]
  local = index_v1.replace("ORIGINAL TITLE", "TITLE_LOCAL")
  (source_dir / "index.jsx").write_text(local, encoding="utf-8")
  manifest_v2 = {
    **manifest_v1,
    "version": "2.0.0",
    "permissions": {"manage_apps": True},
  }
  reviewed_index = index_v1
  (work / "mobius.json").write_text(
    json.dumps(manifest_v2), encoding="utf-8",
  )
  (work / "index.jsx").write_text(reviewed_index, encoding="utf-8")
  reviewed_head = _fixture_commit(work, "reviewed candidate")
  drift_manifest = (
    manifest_v2 if drift_kind == "source" else {
      **manifest_v2, "permissions": {"manage_apps": False},
    }
  )
  drift_index = (
    index_v1.replace("ORIGINAL TITLE", "TITLE_GIT_DRIFT")
    if drift_kind == "source" else index_v1
  )
  (work / "mobius.json").write_text(
    json.dumps(drift_manifest), encoding="utf-8",
  )
  (work / "index.jsx").write_text(drift_index, encoding="utf-8")
  drift_head = _fixture_commit(work, "drift")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  reviewed_digest = install._source_review_digest(
    manifest=manifest_v2,
    entry_bytes=reviewed_index.encode(),
    bundled_job=None,
    source_files={},
    upstream_commit=reviewed_head,
  )
  with patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ), patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(http_responses(manifest_v2, reviewed_index)),
  ):
    rejected = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
      "reviewed_source_digest": reviewed_digest,
    })
  assert rejected.status_code == 409, rejected.text
  assert rejected.json()["detail"]["code"] == (
    "update_changed" if drift_kind == "source" else "git_manifest_changed"
  )
  assert app_git.head_sha(source_dir, "main") == first
  assert app_git.head_sha(source_dir, "upstream") == first
  assert (source_dir / "index.jsx").read_text() == local

  # The remote now publishes exactly what the reviewed HTTP candidate named.
  (work / "mobius.json").write_text(
    json.dumps(manifest_v2), encoding="utf-8",
  )
  (work / "index.jsx").write_text(reviewed_index, encoding="utf-8")
  matching_head = _fixture_commit(work, "matching")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  matching_digest = install._source_review_digest(
    manifest=manifest_v2,
    entry_bytes=reviewed_index.encode(),
    bundled_job=None,
    source_files={},
    upstream_commit=matching_head,
  )
  with patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ), patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(http_responses(manifest_v2, reviewed_index)),
  ):
    retried = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
      "reviewed_source_digest": matching_digest,
    })
  assert retried.status_code == 201, retried.text
  assert app_git.head_sha(source_dir, "upstream") == matching_head
  assert "TITLE_LOCAL" in (source_dir / "index.jsx").read_text()


def test_untrusted_legacy_origin_fails_without_rewriting_history(
  client, auth, tmp_path, bypass_url_validation,
):
  """An older installer-history app may have picked up an origin later.

  If a failed cloned-update attempt moved its installer-owned upstream ref onto
  unrelated origin history, the next update restores the DB-recorded baseline
  and stops. It must not manufacture another upstream from downloaded files.
  """
  base = "https://synthetic-origin.test/repo/"
  manifest = {
    "id": "synthetic-origin",
    "name": "Synthetic Origin",
    "version": "1.0.0",
    "description": "Synthetic app with stray origin",
    "entry": "index.jsx",
    "source_files": ["cards.js"],
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  index_v1 = "import './cards.js'\nexport default () => <div>HTTP_V1</div>\n"
  cards_v1 = "export const card = 'HTTP_CARD_V1'\n"
  created = _seed_legacy_catalog_app(
    client, auth, base, manifest, index_v1,
    source_files={"cards.js": cards_v1},
  )

  src = Path(get_settings().data_dir) / "apps" / "synthetic-origin"
  from app.database import SessionLocal
  from app.models import App
  db = SessionLocal()
  try:
    app = db.get(App, created["id"])
    db_upstream = app.upstream_commit
  finally:
    db.close()
  assert db_upstream == app_git.head_sha(src, app_git.UPSTREAM_BRANCH)

  # Add a real origin that is unrelated to the legacy installer history, then
  # simulate the exact failed-attempt residue: upstream was moved to origin/main
  # while the DB row still points at the synthetic commit.
  work, bare, real_head = _make_clone_fixture(
    tmp_path,
    "import './cards.js'\nexport default () => <div>REAL_REPO</div>\n",
    "export const card = 'REAL_CARD'\n",
  )
  (work / "mobius.json").write_text(
    json.dumps({**manifest, "version": "2.0.0"}), encoding="utf-8",
  )
  real_head = _fixture_commit(work, "add committed package manifest")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True, env=app_git._git_env(work),
  )
  app_git._run(src, "remote", "add", "origin", bare.as_uri())
  app_git._run(src, "fetch", "--depth", "1", "origin", "main")
  app_git._run(src, "branch", "-f", app_git.UPSTREAM_BRANCH, "origin/main")
  assert app_git.head_sha(src, app_git.UPSTREAM_BRANCH) == real_head

  index_v2 = index_v1.replace("HTTP_V1", "HTTP_V2")
  cards_v2 = cards_v1.replace("HTTP_CARD_V1", "HTTP_CARD_V2")
  responses_v2 = {
    base + "mobius.json": (200, json.dumps({
      **manifest, "version": "2.0.0",
    }).encode()),
    base + "index.jsx": (200, index_v2.encode()),
    base + "cards.js": (200, cards_v2.encode()),
  }
  with patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ), patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })

  assert r2.status_code == 409, r2.text
  assert r2.json()["detail"]["code"] == "git_update_unavailable"
  assert (src / "index.jsx").read_text() == index_v1
  assert (src / "cards.js").read_text() == cards_v1
  assert app_git.head_sha(src, app_git.UPSTREAM_BRANCH) == db_upstream


def test_catalog_app_rebinds_equal_local_tree_from_legacy_history(
  client, auth, tmp_path, bypass_url_validation,
):
  """A legacy catalog app may gain its real origin during migration.

  When its complete local tree already equals the canonical origin tip, that
      equality is sufficient to repair the unrelated installer lineage without
      discarding a byte or falling back to any non-Git update path.
  """
  base = (
    "https://raw.githubusercontent.com/mobius-os/"
    "app-catalog-rebind/main/"
  )
  manifest = {
    "id": "catalog-rebind",
    "name": "Catalog rebind",
    "version": "1.0.0",
    "description": "Catalog app with legacy installer history",
    "entry": "index.jsx",
    "source_files": ["cards.js"],
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  index_v1 = "import './cards.js'\nexport default () => <div>V1</div>\n"
  cards_v1 = "export const card = 'V1'\n"
  _seed_legacy_catalog_app(
    client, auth, base, manifest, index_v1,
    source_files={"cards.js": cards_v1},
  )

  src = Path(get_settings().data_dir) / "apps" / "catalog-rebind"
  legacy_upstream = app_git.record_upstream(
    src,
    {"index.jsx": index_v1.encode(), "cards.js": cards_v1.encode()},
    base,
    "1.0.0",
  )
  app_git.align_local_to_upstream(src)
  from app.database import SessionLocal
  from app.models import App
  db = SessionLocal()
  try:
    row = db.query(App).filter(App.slug == "catalog-rebind").one()
    row.upstream_commit = legacy_upstream
    db.commit()
  finally:
    db.close()
  index_v2 = index_v1.replace("V1", "V2")
  cards_v2 = cards_v1.replace("V1", "V2")
  (src / "index.jsx").write_text(index_v2)
  (src / "cards.js").write_text(cards_v2)
  (src / "mobius.json").write_text(json.dumps({
    **manifest, "version": "2.0.0",
  }))
  app_git.commit_local(src, "apply accepted source")

  work, bare, real_head = _make_clone_fixture(
    tmp_path, index_v2, cards_v2,
  )
  # Managed app repositories include the platform-owned ignore file. Mirror
  # that complete tree in the real origin so this exercises the same exact-tree
  # proof used to repair an installed catalog app's lineage.
  (work / ".gitignore").write_bytes((src / ".gitignore").read_bytes())
  (work / "mobius.json").write_text(
    json.dumps({**manifest, "version": "2.0.0"}), encoding="utf-8",
  )
  real_head = _fixture_commit(work, "match managed app tree")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True,
    env=app_git._git_env(work),
  )
  app_git._run(src, "remote", "add", "origin", bare.as_uri())
  responses_v2 = {
    base + "mobius.json": (200, json.dumps({
      **manifest, "version": "2.0.0",
    }).encode()),
    base + "index.jsx": (200, index_v2.encode()),
    base + "cards.js": (200, cards_v2.encode()),
  }
  canonical_origin = (
    "https://github.com/mobius-os/app-catalog-rebind.git"
  )
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ), patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ), patch(
    "app.install.app_git.origin_url", return_value=canonical_origin,
  ):
    updated = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )

  assert updated.status_code == 201, updated.text
  assert updated.json()["mode"] == "update"
  assert (src / "index.jsx").read_text() == index_v2
  assert (src / "cards.js").read_text() == cards_v2
  assert json.loads((src / "mobius.json").read_text())["version"] == "2.0.0"
  assert app_git.head_sha(src, app_git.UPSTREAM_BRANCH) == real_head
  assert real_head != legacy_upstream
  assert app_git._run(
    src, "merge-base", "--is-ancestor", real_head, app_git.LOCAL_BRANCH,
    check=False,
  ).returncode == 0


def test_multifile_install_writes_siblings_and_bundles(
  client, auth, bypass_url_validation,
):
  """A fresh multi-file install writes index.jsx + the declared sibling to
  the source dir and the compiled bundle inlines the sibling's export — proof
  Rolldown resolved the `./cards.js` import from the on-disk source tree."""
  base = "https://multi.test/repo/"
  r = _install_multi(
    client, auth, base, MANIFEST_MULTI, JSX_IMPORTS_CARDS, CARDS_V1,
  )
  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "install"
  app_id = payload["id"]

  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "multi-app"
  assert (src / "index.jsx").read_text() == JSX_IMPORTS_CARDS
  assert (src / "cards.js").read_text() == CARDS_V1

  bundle = Path(payload["compiled_path"])
  assert bundle.exists()
  bundle_text = bundle.read_text()
  assert len(bundle_text) > 0
  # The sibling was bundled in, not left as an unresolved import.
  assert "CARDS_ORIGINAL" in bundle_text
  assert "./cards.js" not in bundle_text


# index.jsx imports a sibling the manifest's source_files never declares. The
# synthetic-fetch path won't fetch it, so the install ships a tree that can't
# resolve the import — the exact shape the Editor launch bug had. The
# source-completeness check must reject it BEFORE Rolldown, with its own 422.
JSX_IMPORTS_UNDECLARED = (
  "import { CARD_LABEL } from './cards.js'\n"
  "import { EXTRA } from './extra.js'\n"
  "export default function App() {\n"
  "  return <div>{CARD_LABEL}{EXTRA}</div>\n"
  "}\n"
)

MANIFEST_MULTI_INCOMPLETE = {
  **MANIFEST_MULTI,
  "id": "multi-incomplete",
  # ./extra.js is imported but omitted here — the defect under test.
  "source_files": ["cards.js"],
}


def test_multifile_install_rejects_incomplete_source_files(
  client, auth, bypass_url_validation,
):
  """An undeclared sibling is rejected without deleting the Git checkout."""
  base = "https://multi-incomplete.test/repo/"
  r = _install_multi(
    client, auth, base, MANIFEST_MULTI_INCOMPLETE,
    JSX_IMPORTS_UNDECLARED, CARDS_V1,
  )
  assert r.status_code == 422, r.text
  detail = r.json()["detail"]
  assert "manifest" in detail and "source file" in detail
  assert "extra.js" in detail
  data_dir = Path(get_settings().data_dir)
  source = data_dir / "apps" / "multi-incomplete"
  assert not source.exists()
  retained = source.parent / ".multi-incomplete.mobius-failed.bak"
  assert app_git.is_repo(retained)
  assert not (retained / "extra.js").exists()


def test_multifile_update_delivers_new_sibling_bytes(
  client, auth, bypass_url_validation,
):
  """An update that bumps a sibling (no local edits) delivers the new sibling
  bytes to disk via a clean fast-forward — no spurious conflict — and the new
  bundle reflects the bumped export."""
  base = "https://multi2.test/repo/"
  r1 = _install_multi(
    client, auth, base, MANIFEST_MULTI, JSX_IMPORTS_CARDS, CARDS_V1,
  )
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]
  bundle_v1 = Path(r1.json()["compiled_path"])
  assert bundle_v1.is_file()
  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "multi-app"

  cards_v2 = CARDS_V1.replace("CARDS_ORIGINAL", "CARDS_V2")
  r2 = _install_multi(
    client, auth, base,
    {**MANIFEST_MULTI, "version": "2.0.0"}, JSX_IMPORTS_CARDS, cards_v2,
  )
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "update"
  # No local edits → upstream wins outright, not a three-way merge conflict.
  assert payload["divergence"] == "fast_forward"
  assert (src / "cards.js").read_text() == cards_v2
  bundle_v2 = Path(payload["compiled_path"])
  assert bundle_v2 != bundle_v1
  assert bundle_v2.is_file()
  assert not bundle_v1.exists()
  assert "CARDS_V2" in bundle_v2.read_text()


def test_multifile_update_merges_local_sibling_edit(
  client, auth, bypass_url_validation,
):
  """A local edit to a sibling + a DISJOINT upstream edit to the same sibling
  merges cleanly: the merged tree (read via read_merged_tree) carries BOTH
  changes to disk, the same way the entry file's clean merge does."""
  base = "https://multi3.test/repo/"
  r1 = _install_multi(
    client, auth, base, MANIFEST_MULTI, JSX_IMPORTS_CARDS, CARDS_V1,
  )
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  cards_file = data_dir / "apps" / "multi-app" / "cards.js"

  # Agent edits the label region of the sibling locally.
  cards_file.write_text(CARDS_V1.replace("CARDS_ORIGINAL", "AGENT_LABEL"))

  # Upstream v2 edits the disjoint footer region of the SAME sibling.
  cards_v2 = CARDS_V1.replace("FOOTER_ORIGINAL", "FOOTER_UPSTREAM")
  r2 = _install_multi(
    client, auth, base,
    {**MANIFEST_MULTI, "version": "2.0.0"}, JSX_IMPORTS_CARDS, cards_v2,
  )
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "clean_merge"
  merged = cards_file.read_text()
  assert "AGENT_LABEL" in merged       # local sibling edit carried forward
  assert "FOOTER_UPSTREAM" in merged   # upstream sibling change applied
  assert "<<<<<<<" not in merged


def test_multifile_update_validates_published_tree_not_local_extensions(
  client, auth, bypass_url_validation,
):
  """A clean update merge may retain a locally applied sibling module.

  The upstream manifest cannot declare that local-only module. Source package
  completeness therefore belongs to the exact fetched release tree, not the
  later reconciled tree that is compiled and served.
  """
  base = "https://multi-local-extension.test/repo/"
  r1 = _install_multi(
    client, auth, base, MANIFEST_MULTI, JSX_IMPORTS_CARDS, CARDS_V1,
  )
  assert r1.status_code == 201, r1.text

  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "multi-app"
  local_index = (
    "import { LOCAL_LABEL } from './local.js'\n" + JSX_IMPORTS_CARDS
  ).replace(
    "<div>{CARD_LABEL}</div>",
    "<div>{CARD_LABEL}{LOCAL_LABEL}</div>",
  )
  (src / "index.jsx").write_text(local_index, encoding="utf-8")
  (src / "local.js").write_text(
    "export const LOCAL_LABEL = 'LOCAL_ONLY'\n", encoding="utf-8",
  )

  cards_v2 = CARDS_V1.replace("FOOTER_ORIGINAL", "FOOTER_UPSTREAM")
  r2 = _install_multi(
    client, auth, base,
    {**MANIFEST_MULTI, "version": "2.0.0"}, JSX_IMPORTS_CARDS, cards_v2,
  )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "clean_merge"
  assert (src / "index.jsx").read_text() == local_index
  assert (src / "local.js").read_text() == (
    "export const LOCAL_LABEL = 'LOCAL_ONLY'\n"
  )
  bundle = Path(r2.json()["compiled_path"]).read_text()
  assert "LOCAL_ONLY" in bundle
  assert "FOOTER_UPSTREAM" in (src / "cards.js").read_text()


def test_singlefile_install_unchanged_without_source_files(
  client, auth, bypass_url_validation,
):
  """Regression: a manifest with no `source_files` installs exactly as before
  — only index.jsx in the source dir, no stray sibling, bundle non-empty."""
  base = "https://single.test/repo/"
  manifest = {
    "id": "single-app",
    "name": "Single App",
    "version": "1.0.0",
    "description": "Single-file app",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]
  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "single-app"
  assert (src / "index.jsx").read_text() == JSX
  # Only the entry (+ managed .git) — no sibling source files materialized.
  jsx_siblings = [
    p.name for p in src.iterdir()
    if p.is_file() and p.suffix in (".js", ".jsx") and p.name != "index.jsx"
  ]
  assert jsx_siblings == []
  bundle = Path(r.json()["compiled_path"])
  assert bundle.exists() and bundle.stat().st_size > 0


def test_multifile_manifest_rejects_unsafe_source_file(
  client, auth, bypass_url_validation,
):
  """Schema guard: a `source_files` entry that escapes the repo, names the
  entry / managed .gitignore, or collides with an install-managed path (static/,
  dist/, the cron/job scripts, .bak snapshots, the numeric storage tree) is a
  clean 400, not a surprising fetch or a write that fights another phase."""
  base = "https://multibad.test/repo/"
  bad_paths = (
    "../escape.js", "index.jsx", ".gitignore", "/abs.js",
    "static/x.js", "dist/x.js", ".build/x.js", "node_modules/x.js",
    "init-cron.sh", "cards.js.bak", "5/data.js", "fetch.sh",
  )
  for bad in bad_paths:
    # fetch.sh is the declared job; the rest collide with managed prefixes.
    manifest = {**MANIFEST_MULTI, "source_files": [bad],
                "schedule": {"job": "fetch.sh"}}
    responses = {base + "mobius.json": (200, json.dumps(manifest).encode())}
    with patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ):
      r = client.post("/api/apps/install", headers=auth, json={
        "manifest_url": base + "mobius.json",
      })
    assert r.status_code == 400, (bad, r.text)
    assert "source_files" in r.json()["detail"], (bad, r.text)


def test_assert_within_rejects_symlinked_parent_escape(tmp_path):
  """The realpath guard rejects a write that resolves outside the source dir
  through a symlinked parent — the catastrophic case lexical validation misses
  (a nested `lib/cards.js` where `lib` symlinks to a dir outside the app)."""
  from app.install import _assert_within
  from fastapi import HTTPException

  src = tmp_path / "apps" / "victim"
  src.mkdir(parents=True)
  outside = tmp_path / "shared"
  outside.mkdir()
  # `lib` inside the source dir is actually a symlink to /shared.
  (src / "lib").symlink_to(outside)

  # A file under the symlinked parent resolves outside the source dir.
  with pytest.raises(HTTPException) as exc:
    _assert_within(src, src / "lib" / "cards.js", "source_files lib/cards.js")
  assert exc.value.status_code == 400

  # A genuine in-tree path is accepted.
  _assert_within(src, src / "real" / "cards.js", "source_files real/cards.js")


def test_multifile_update_deletes_dropped_sibling(
  client, auth, bypass_url_validation,
):
  """A v2 that drops a sibling the v1 shipped removes it from disk AND from git
  tracking, with no spurious divergence on a later update — the worktree is
  reconciled to the new tree, not left with a stale sibling that git re-records
  onto `main` as permanent local divergence."""
  base = "https://multidrop.test/repo/"
  # v1 ships index.jsx + cards.js, where index.jsx imports the sibling.
  r1 = _install_multi(
    client, auth, base, MANIFEST_MULTI, JSX_IMPORTS_CARDS, CARDS_V1,
  )
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "multi-app"
  assert (src / "cards.js").exists()

  # v2 drops the sibling: a self-contained index.jsx, no source_files.
  jsx_v2 = "export default function App() { return <div>standalone</div> }"
  m2 = {
    "id": "multi-app", "name": "Multi App", "version": "2.0.0",
    "description": "Multi-file app", "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  responses = {
    base + "mobius.json": (200, json.dumps(m2).encode()),
    base + "index.jsx": (200, jsx_v2.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  # The dropped sibling is gone from disk AND from git tracking.
  assert not (src / "cards.js").exists(), "dropped sibling lingered on disk"
  tracked = subprocess.run(
    ["git", "-C", str(src), "ls-files"],
    capture_output=True, text=True, check=True,
  ).stdout.split()
  assert "cards.js" not in tracked, f"dropped sibling still tracked: {tracked}"

  # A subsequent no-op re-install of v2 must NOT report a spurious conflict —
  # the worktree already matches the recorded tree (no lingering divergence).
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r3 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r3.status_code == 201, r3.text
  assert r3.json()["mode"] == "update"
  assert r3.json()["divergence"] != "conflict"
  assert r3.json()["conflict_paths"] == []


# --------------------------------------------------------------------------
# GET /{app_id}/update-check — read-only, git-native update detection.
#
# Content-compares the CURRENT upstream source (fetched the same way install
# does) against the pristine `upstream` branch the last install recorded, so a
# push that changed code WITHOUT bumping the version still reads as an update.
# --------------------------------------------------------------------------


def _install_with_sources(client, auth, base, manifest, jsx, sources):
  """Install a manifest whose `source_files` need extra fetched bytes.

  `_install_v1` only maps the entry/icon/seed/job set; a multi-file manifest
  also has to serve each declared source_files sibling, which this adds."""
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, jsx.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b""),
  }
  for rel, data in sources.items():
    responses[base + rel] = (200, data if isinstance(data, bytes) else data.encode())
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })


def _check_responses(base, manifest, jsx, sources=None, job=b""):
  """Response map for the upstream fetch a GET /update-check performs.

  update-check fetches ONLY tracked source — the manifest, the entry, declared
  source_files, and the job — never the icon or storage seeds, so only those
  are mapped (a stray unmapped fetch would 404 → degrade to unknown)."""
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, jsx.encode()),
  }
  sched = manifest.get("schedule")
  if isinstance(sched, dict) and sched.get("job"):
    responses[base + sched["job"]] = (200, job)
  for rel, data in (sources or {}).items():
    responses[base + rel] = (200, data if isinstance(data, bytes) else data.encode())
  return responses


def _git_candidate(
  manifest, jsx, sources=None, job=b"#!/bin/sh\n", *, commit=None,
):
  """One immutable Git package projection returned by the route helper."""
  from app import install
  from app.routes.apps import AppUpdateCandidate

  source_files = {
    rel: data if isinstance(data, bytes) else data.encode()
    for rel, data in (sources or {}).items()
  }
  tree = {"index.jsx": jsx.encode(), **source_files}
  bundled_job = None
  schedule = manifest.get("schedule") if isinstance(manifest, dict) else None
  if isinstance(schedule, dict) and schedule.get("job"):
    bundled_job = job
    tree[schedule["job"]] = job
  digest = install._source_review_digest(
    manifest=manifest,
    entry_bytes=jsx.encode(),
    bundled_job=bundled_job,
    source_files=source_files,
    upstream_commit=commit or "f" * 40,
  )
  return AppUpdateCandidate(
    manifest=manifest,
    source_tree=tree,
    executable_paths=frozenset(),
    commit=commit or "f" * 40,
    source_digest=digest,
  )


def _candidate_commit_for_app(
  app_id, manifest, jsx, sources=None, job=b"#!/bin/sh\n",
):
  from app.database import SessionLocal

  db = SessionLocal()
  try:
    row = db.get(models.App, app_id)
    assert row is not None
    repo = Path(row.source_dir)
  finally:
    db.close()
  parent = app_git.head_sha(repo, app_git.UPSTREAM_BRANCH)
  parent_tree = app_git.read_ref_tree(repo, parent)
  candidate_tree = dict(parent_tree)
  try:
    previous_manifest = json.loads(parent_tree["mobius.json"])
  except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
    previous_manifest = {}

  def declared_paths(value):
    from app import install

    paths = {value.get("entry", "index.jsx")}
    paths.update(value.get("source_files") or [])
    if value.get("icon"):
      paths.add(value["icon"])
    schedule_value = value.get("schedule")
    if isinstance(schedule_value, dict) and schedule_value.get("job"):
      paths.add(schedule_value["job"])
    paths.update(install.static_asset_entries(
      value.get("static_assets") or {},
    ).values())
    for seed in (value.get("storage_seeds") or {}).values():
      if not install._seed_value_is_inline(seed):
        paths.add(seed)
    return paths

  for rel in declared_paths(previous_manifest) - declared_paths(manifest):
    candidate_tree.pop(rel, None)
  candidate_tree.update({
    "mobius.json": json.dumps(manifest).encode(),
    manifest.get("entry", "index.jsx"): jsx.encode(),
    **{
      rel: data if isinstance(data, bytes) else data.encode()
      for rel, data in (sources or {}).items()
    },
  })
  schedule = manifest.get("schedule") if isinstance(manifest, dict) else None
  job_name = schedule.get("job") if isinstance(schedule, dict) else None
  if job_name:
    candidate_tree[job_name] = job
  if app_git.read_ref_tree(repo, parent) == candidate_tree:
    return parent
  with tempfile.TemporaryDirectory(
    prefix="mobius-update-check-", dir=repo.parent,
  ) as tmp:
    worktree = Path(tmp) / "candidate"
    app_git._run(repo, "worktree", "add", "--detach", str(worktree), parent)
    try:
      for child in worktree.iterdir():
        if child.name == ".git":
          continue
        if child.is_dir():
          shutil.rmtree(child)
        else:
          child.unlink()
      for rel, content in candidate_tree.items():
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if rel == job_name:
          target.chmod(0o755)
      app_git._run(worktree, "add", "-A")
      app_git._run(
        worktree, "commit", "-q", "--allow-empty",
        "-m", "Test update candidate",
      )
      return app_git.head_sha(worktree, "HEAD")
    finally:
      app_git._run(
        repo, "worktree", "remove", "--force", str(worktree), check=False,
      )


def _update_check(
  client, headers, base, app_id, manifest, jsx, sources=None,
  job=b"#!/bin/sh\n",
  candidate_manifest_url=None,
  candidate_commit=None,
):
  if candidate_commit is None:
    candidate_commit = _candidate_commit_for_app(
      app_id, manifest, jsx, sources=sources, job=job,
    )
  with patch(
    "app.routes.apps._fetch_update_candidate",
    return_value=_git_candidate(
      manifest, jsx, sources=sources, job=job, commit=candidate_commit,
    ),
  ):
    return client.get(
      f"/api/apps/{app_id}/update-check",
      headers=headers,
      params=(
        {"manifest_url": candidate_manifest_url}
        if candidate_manifest_url else None
      ),
    )



def test_plain_https_package_without_git_upstream_is_rejected(
  client, auth,
):
  """HTTPS may locate a package, but never replaces a real Git upstream."""
  base = "https://packages.test/history/"
  manifest = {
    "id": "https-history", "name": "HTTPS History", "version": "1.0.0",
    "description": "No real Git source", "entry": "index.jsx",
  }
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX_MULTI.encode()),
  }
  with (
    patch(
      "app.install._validate_url_safe",
      lambda url: (url, urlparse(url).netloc, urlparse(url).hostname),
    ),
    patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ),
  ):
    installed = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert installed.status_code == 400, installed.text
  assert installed.json()["detail"]["code"] == "git_source_required"
  assert not (
    Path(get_settings().data_dir) / "apps" / manifest["id"]
  ).exists()



def test_permission_only_same_version_update_is_discovered_and_reviewed(
  client, auth, db, bypass_url_validation,
):
  """Direct access review must be reachable even without a code/version bump."""
  base = "https://packages.test/access/"
  manifest = {
    "id": "access-only", "name": "Access Only", "version": "1.0.0",
    "description": "Permission-only release", "entry": "index.jsx",
    "permissions": {"manage_apps": False},
  }
  def responses(m):
    return {
      base + "mobius.json": (200, json.dumps(m).encode()),
      base + "index.jsx": (200, JSX.encode()),
    }
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses(manifest))):
    installed = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]
  repo = Path(get_settings().data_dir) / "apps" / "access-only"
  next_manifest = {**manifest, "permissions": {"manage_apps": True}}
  # An owner draft requesting the same future permission isn't the baseline.
  (repo / "mobius.json").write_text(json.dumps(next_manifest))
  original = app_git.head_sha(repo, "main")
  unchanged = _update_check(client, auth, base, app_id, manifest, JSX)
  assert unchanged.json()["update_available"] is False
  candidate_commit = _candidate_commit_for_app(
    app_id, next_manifest, JSX,
  )
  with patch(
    "app.routes.apps._fetch_update_candidate",
    return_value=_git_candidate(
      next_manifest, JSX, commit=candidate_commit,
    ),
  ):
    checked = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
    reviewed = client.get(f"/api/apps/{app_id}/update-candidate-preview", headers=auth)
  assert checked.status_code == 200, checked.text
  assert checked.json()["update_available"] is True
  assert reviewed.status_code == 200, reviewed.text
  preview = reviewed.json()["capability_preview"]
  assert preview["manifest"] == next_manifest
  assert "data.manage_apps" in preview["capability_diff"]["added"]
  assert preview["capability_contract"]["data"]["manage_apps"] is True
  assert preview["installed_contract"]["data"]["manage_apps"] is False
  assert app_git.head_sha(repo, "main") == original
  row = db.query(models.App).populate_existing().filter_by(id=app_id).one()
  assert not row.manage_apps
  assert not row.capability_contract["data"]["manage_apps"]

def test_known_origin_check_never_falls_back_to_http(tmp_path):
  """An origin outage cannot silently change the source being reviewed."""
  from app.routes.apps import _fetch_update_candidate
  app_git.ensure_repo(tmp_path)
  app_git._run(tmp_path, "remote", "add", "origin", "https://github.com/acme/source.git")
  with patch(
    "app.routes.apps._fetch_git_update_candidate",
    side_effect=RuntimeError("offline"),
  ):
    with pytest.raises(RuntimeError, match="offline"):
      asyncio.run(_fetch_update_candidate(
        tmp_path, "https://raw.githubusercontent.com/acme/source/main/mobius.json", strict=True,
      ))


def test_git_update_candidate_reads_one_commit_without_advancing_managed_refs(
  tmp_path,
):
  from app.routes.apps import (
    _diff_preview_trees, _fetch_git_update_candidate,
    _normalized_git_source_tree,
  )

  work = tmp_path / "candidate-work"
  bare = tmp_path / "candidate.git"
  installed = tmp_path / "installed"
  subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
  manifest = {
    "id": "git-candidate",
    "name": "Git Candidate",
    "version": "1.0.0",
    "description": "One-commit package",
    "entry": "index.jsx",
    "source_files": ["cards.js"],
    "schedule": {"job": "fetch.sh"},
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  (work / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  (work / "index.jsx").write_text(JSX, encoding="utf-8")
  (work / "cards.js").write_text("export const cards = ['v1']", encoding="utf-8")
  (work / "helper.js").write_text("export const helper = 'v1'\n", encoding="utf-8")
  (work / "settings.json").write_text("runtime-v1\n", encoding="utf-8")
  (work / "fetch.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
  first = _fixture_commit(work, "v1")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(work), str(bare)], check=True,
  )
  assert app_git.clone_upstream(installed, bare.as_uri(), "main") == first

  manifest["version"] = "2.0.0"
  (work / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  (work / "index.jsx").write_text(
    JSX.replace("ok", "from one git commit"), encoding="utf-8",
  )
  (work / "cards.js").write_text("export const cards = ['v2']", encoding="utf-8")
  (work / "helper.js").write_text("export const helper = 'v2'\n", encoding="utf-8")
  (work / "settings.json").write_text("runtime-v2\n", encoding="utf-8")
  (work / "fetch.sh").write_text("#!/bin/sh\necho v2\n", encoding="utf-8")
  second = _fixture_commit(work, "v2")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"], check=True,
  )

  with patch(
    "app.install._derive_repo_ref", return_value=(bare.as_uri(), "main"),
  ):
    candidate = _fetch_git_update_candidate(
      installed, "https://example.invalid/mobius.json", strict=True,
    )

  assert candidate.commit == second
  assert candidate.manifest["version"] == "2.0.0"
  assert candidate.source_tree == {
    "mobius.json": json.dumps(manifest).encode(),
    "index.jsx": JSX.replace("ok", "from one git commit").encode(),
    "cards.js": b"export const cards = ['v2']",
    "helper.js": b"export const helper = 'v2'\n",
    "fetch.sh": b"#!/bin/sh\necho v2\n",
  }
  previous, previous_executable = _normalized_git_source_tree(installed, first)
  preview = _diff_preview_trees(
    previous,
    candidate.source_tree,
    previous_executable=previous_executable,
    candidate_executable=candidate.executable_paths,
  )
  assert "helper.js" in preview
  assert "helper = 'v1'" in preview
  assert "helper = 'v2'" in preview
  assert "settings.json" not in preview
  assert app_git.head_sha(installed, "main") == first
  assert app_git.head_sha(installed, "upstream") == first


def test_git_update_preview_shows_executable_mode_only_change(tmp_path):
  from app.routes.apps import _diff_preview_trees, _normalized_git_source_tree

  repo = tmp_path / "mode-update"
  repo.mkdir()
  app_git.ensure_repo(repo)
  script = repo / "run.sh"
  script.write_text("#!/bin/sh\necho ok\n")
  app_git._run(repo, "add", "run.sh")
  app_git._run(repo, "commit", "-m", "non-executable")
  previous = app_git.head_sha(repo, "main")
  script.chmod(0o755)
  app_git._run(repo, "add", "run.sh")
  app_git._run(repo, "commit", "-m", "make executable")
  candidate = app_git.head_sha(repo, "main")

  previous_tree, previous_exec = _normalized_git_source_tree(repo, previous)
  candidate_tree, candidate_exec = _normalized_git_source_tree(repo, candidate)
  preview = _diff_preview_trees(
    previous_tree,
    candidate_tree,
    previous_executable=previous_exec,
    candidate_executable=candidate_exec,
  )

  assert "old mode 100644" in preview
  assert "new mode 100755" in preview


def test_update_check_unchanged_upstream_is_false(
  client, auth, bypass_url_validation,
):
  """Byte-identical upstream → update_available is a real False, not null."""
  base = "https://uc-unchanged.test/repo/"
  m = {**MANIFEST_NEWS, "id": "uc-unchanged"}
  r1 = _install_v1(client, auth, base, m, JSX)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  res = _update_check(client, auth, base, app_id, m, JSX)
  assert res.status_code == 200, res.text
  payload = res.json()
  assert payload["update_available"] is False
  assert payload["pending_update_state"] == "none"
  assert payload["needs_resolution"] is False
  assert payload["upstream_version"] == "1.0.0"
  assert payload["local_version"] == "1.0.0"
  assert payload["installed_source_revision"]
  assert len(payload["candidate_source_digest"]) == 64
  assert payload["checked_at"]


def test_update_check_uses_identity_matched_live_candidate_for_pinned_install(
  client, auth, bypass_url_validation,
):
  """A pinned provenance URL must not hide a newer catalog candidate."""
  pinned_base = (
    "https://raw.githubusercontent.com/mobius-os/app-pinned/"
    "1111111111111111111111111111111111111111/"
  )
  live_base = (
    "https://raw.githubusercontent.com/mobius-os/app-pinned/main/"
  )
  manifest_v1 = {**MANIFEST_NEWS, "id": "uc-pinned", "version": "1.0.0"}
  installed = _seed_legacy_catalog_app(
    client, auth, pinned_base, manifest_v1, JSX,
  )

  manifest_v2 = {**manifest_v1, "version": "2.0.0"}
  source_dir = Path(get_settings().data_dir) / "apps" / "uc-pinned"
  installed_upstream = app_git.head_sha(source_dir, app_git.UPSTREAM_BRANCH)
  candidate_commit = app_git.record_upstream(
    source_dir,
    {
      "mobius.json": json.dumps(manifest_v2).encode(),
      "index.jsx": JSX.replace("ok", "NEW RELEASE").encode(),
    },
    live_base,
    "2.0.0",
  )
  app_git.restore_upstream_ref(source_dir, installed_upstream)
  response = _update_check(
    client,
    auth,
    live_base,
    installed["id"],
    manifest_v2,
    JSX.replace("ok", "NEW RELEASE"),
    candidate_manifest_url=live_base + "mobius.json",
    candidate_commit=candidate_commit,
  )

  assert response.status_code == 200, response.text
  assert response.json()["update_available"] is True
  assert response.json()["upstream_version"] == "2.0.0"


def test_update_check_ignores_candidate_from_different_package(
  client, auth, bypass_url_validation,
):
  pinned_base = (
    "https://raw.githubusercontent.com/mobius-os/app-pinned/"
    "1111111111111111111111111111111111111111/"
  )
  other_base = "https://raw.githubusercontent.com/mobius-os/app-other/main/"
  manifest = {**MANIFEST_NEWS, "id": "uc-pinned"}
  installed = _seed_legacy_catalog_app(
    client, auth, pinned_base, manifest, JSX,
  )

  response = _update_check(
    client,
    auth,
    other_base,
    installed["id"],
    manifest,
    JSX,
    candidate_manifest_url=other_base + "mobius.json",
  )

  assert response.status_code == 200, response.text
  assert response.json()["update_available"] is None
  assert response.json()["upstream_version"] is None


def test_update_check_degrades_cross_owner_predecessor_to_unknown(
  client, auth, bypass_url_validation,
):
  pinned_base = (
    "https://raw.githubusercontent.com/mobius-os/app-pinned/"
    "1111111111111111111111111111111111111111/"
  )
  live_base = "https://raw.githubusercontent.com/mobius-os/app-renamed/main/"
  manifest = {**MANIFEST_NEWS, "id": "uc-pinned"}
  installed = _seed_legacy_catalog_app(
    client, auth, pinned_base, manifest, JSX,
  )

  candidate = {
    **manifest,
    "id": "uc-renamed",
    "previous_id": "uc-pinned",
    "previous_manifest_url": (
      "https://raw.githubusercontent.com/other-owner/app-pinned/"
      "main/mobius.json"
    ),
  }
  response = _update_check(
    client,
    auth,
    live_base,
    installed["id"],
    candidate,
    JSX,
    candidate_manifest_url=live_base + "mobius.json",
  )

  assert response.status_code == 200, response.text
  assert response.json()["update_available"] is None
  assert response.json()["upstream_version"] is None


def test_update_check_final_fence_preserves_concurrent_pending_conflict(
  client, auth, bypass_url_validation, monkeypatch,
):
  """A conflict receipt created during fetch wins over the stale comparison.

  The DB snapshot intentionally remains on v1 while the simulated concurrent
  installer advances the locked repo ref to v2 and journals its receipt. Without
  the final identity fence, comparing the just-fetched v2 bytes to that v2 ref
  returns False and overwrites the App Store's already-observed blocked state.
  """
  from app import install

  base = "https://uc-race.test/repo/"
  manifest_v1 = {**MANIFEST_NEWS, "id": "uc-race"}
  installed = _install_v1(client, auth, base, manifest_v1, JSX_MULTI)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]
  repo = Path(get_settings().data_dir) / "apps" / "uc-race"

  local = JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL TITLE")
  (repo / "index.jsx").write_text(local)
  assert app_git.commit_local(repo, "local edit before raced update")
  upstream_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM TITLE")
  manifest_v2 = {**manifest_v1, "version": "2.0.0"}

  async def advance_during_fetch(_repo, _manifest_url, *, strict=True):
    current_upstream = app_git.record_upstream(
      repo,
      {"index.jsx": upstream_v2.encode()},
      base + "mobius.json",
      "2.0.0",
    )
    install.stage_pending_conflict_update(
      repo,
      app_id=app_id,
      upstream_commit=current_upstream,
      manifest=manifest_v2,
      raw_base=base,
      capability_digest="test-capability-digest",
      candidate_digest="0" * 64,
    )
    return _git_candidate(manifest_v2, upstream_v2)

  monkeypatch.setattr(
    "app.routes.apps._fetch_update_candidate", advance_during_fetch,
  )
  res = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
  assert res.status_code == 200, res.text
  payload = res.json()
  assert payload["update_available"] is True
  assert payload["pending_update_state"] == "needs_resolution"
  assert payload["needs_resolution"] is True
  assert payload["upstream_version"] == "2.0.0"


def test_update_check_ignores_invalid_preview_metadata_but_install_rejects_it(
  client, auth, bypass_url_validation,
):
  """Unused preview metadata cannot hide source changes or permit installs."""
  base = "https://uc-nit.test/repo/"
  m = {**MANIFEST_NEWS, "id": "uc-nit"}
  r1 = _install_v1(client, auth, base, m, JSX)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  # Upstream now carries a project-template preview with no source/builder.
  # This fails the full manifest contract but is otherwise fetch-shaped.
  bad_preview_manifest = {
    **m,
    "project_templates": [{
      "id": "doc",
      "name": "Doc",
      "previews": [{"id": "p", "name": "Preview"}],
    }],
  }
  # A new Git commit is still visible even when its executable bytes match;
  # strict Apply validation remains the gate for malformed metadata.
  same = _update_check(client, auth, base, app_id, bad_preview_manifest, JSX)
  assert same.status_code == 200, same.text
  assert same.json()["update_available"] is True
  assert same.json()["upstream_version"] == "1.0.0"

  # Changed entry → a real True; the comparison ran despite the manifest nit.
  changed = _update_check(
    client, auth, base, app_id, bad_preview_manifest, JSX.replace("ok", "NEW"),
  )
  assert changed.status_code == 200, changed.text
  assert changed.json()["update_available"] is True

  from fastapi import HTTPException

  responses = _check_responses(base, bad_preview_manifest, JSX)
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ), patch(
    "app.routes.apps._fetch_update_candidate",
    side_effect=HTTPException(400, "previews[0].source is required"),
  ):
    preview = client.get(
      f"/api/apps/{app_id}/update-candidate-preview", headers=auth,
    )
    install = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert preview.status_code == 400, preview.text
  assert install.status_code == 400, install.text
  assert "previews[0].source" in preview.json()["detail"]
  assert "previews[0].source" in install.json()["detail"]


@pytest.mark.parametrize("invalid", [
  None, [], "manifest", 1,
  {"id": None}, {"id": []}, {"version": {}},
  {"entry": "other.jsx"}, {"entry": "../index.jsx"},
  {"source_files": "utils.js"}, {"source_files": [None]},
  {"source_files": ["https://other.test/utils.js"]},
  {"source_files": ["../utils.js"]},
  {"source_files": ["%2e%2e/utils.js"]},
  {"source_files": ["lib%2futils.js"]},
  {"source_files": ["index.jsx"]},
  {"source_files": [".gitignore"]},
  {"source_files": ["static/private.js"]},
  {"source_files": ["utils.js"] * 51},
  {"source_files": ["job.py"], "schedule": {"job": "job.py"}},
  {"schedule": []}, {"schedule": {"job": []}},
  {"schedule": {"job": "../job.py"}},
  {"previous_id": []}, {"previous_manifest_url": []},
  {"previous_id": "old-id", "previous_manifest_url": "http://other.test/"},
  {"package_id": {}}, {"moved_to": {}},
])
def test_discovery_rejects_malformed_identity_and_source_before_fetch(invalid):
  from fastapi import HTTPException
  from app import install

  manifest = {**MANIFEST_NEWS, **invalid} if isinstance(invalid, dict) else invalid
  with pytest.raises(HTTPException) as exc:
    install._validate_discovery_manifest(manifest)
  assert exc.value.status_code == 400


@pytest.mark.parametrize("invalid", [
  [], {"id": None}, {"version": {}}, {"source_files": ["../private.js"]},
  {"previous_id": "old-id", "previous_manifest_url": []},
])
def test_update_check_malformed_candidate_degrades_to_unknown(
  client, auth, bypass_url_validation, invalid,
):
  base = "https://invalid-candidate.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "uc-malformed"}
  installed = _install_v1(client, auth, base, manifest, JSX)
  assert installed.status_code == 201, installed.text
  candidate = {**manifest, **invalid} if isinstance(invalid, dict) else invalid
  with patch(
    "app.routes.apps._fetch_update_candidate",
    side_effect=ValueError(f"invalid candidate: {candidate!r}"),
  ):
    response = client.get(
      f"/api/apps/{installed.json()['id']}/update-check",
      headers=auth,
      params={"manifest_url": base + "mobius.json"},
    )
  assert response.status_code == 200, response.text
  assert response.json()["update_available"] is None
  assert response.json()["upstream_version"] is None


def test_update_check_shallow_history_failure_degrades_to_unknown(
  client, auth, bypass_url_validation,
):
  base = "https://shallow-history.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "uc-shallow-history"}
  installed = _install_v1(client, auth, base, manifest, JSX)
  assert installed.status_code == 201, installed.text

  with patch(
    "app.routes.apps.app_git.restore_shallow_history_if_needed",
    side_effect=RuntimeError("synthetic unshallow failure"),
  ):
    response = _update_check(
      client, auth, base, installed.json()["id"],
      {**manifest, "version": "2.0.0"}, JSX.replace("ok", "NEW"),
    )

  assert response.status_code == 200, response.text
  assert response.json()["update_available"] is None
  assert response.json()["upstream_version"] is None


def test_update_check_changed_file_is_true(
  client, auth, bypass_url_validation,
):
  """A changed entry byte upstream → update_available True."""
  base = "https://uc-changed.test/repo/"
  m = {**MANIFEST_NEWS, "id": "uc-changed"}
  r1 = _install_v1(client, auth, base, m, JSX)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  res = _update_check(
    client, auth, base, app_id, m, JSX.replace("ok", "CHANGED"),
  )
  assert res.status_code == 200, res.text
  assert res.json()["update_available"] is True


def test_update_check_version_unchanged_content_changed_is_true(
  client, auth, bypass_url_validation,
):
  """The whole point: version string identical, content changed → True."""
  base = "https://uc-samever.test/repo/"
  m = {**MANIFEST_NEWS, "id": "uc-samever"}
  r1 = _install_v1(client, auth, base, m, JSX)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  # Same manifest (same version "1.0.0"), different entry bytes.
  res = _update_check(
    client, auth, base, app_id, m, JSX.replace("ok", "SILENT PUSH"),
  )
  assert res.status_code == 200, res.text
  payload = res.json()
  assert payload["update_available"] is True
  assert payload["upstream_version"] == payload["local_version"] == "1.0.0"


def test_update_check_added_file_is_true(
  client, auth, bypass_url_validation,
):
  """A new source_files sibling upstream → update_available True."""
  base = "https://uc-added.test/repo/"
  m = {**MANIFEST_NEWS, "id": "uc-added"}
  r1 = _install_v1(client, auth, base, m, JSX)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  m2 = {**m, "source_files": ["helper.js"]}
  res = _update_check(
    client, auth, base, app_id, m2, JSX,
    sources={"helper.js": b"export const x = 1\n"},
  )
  assert res.status_code == 200, res.text
  assert res.json()["update_available"] is True


def test_update_check_removed_file_is_true(
  client, auth, bypass_url_validation,
):
  """A source file dropped from the manifest upstream → update_available True."""
  base = "https://uc-removed.test/repo/"
  m1 = {**MANIFEST_NEWS, "id": "uc-removed", "source_files": ["helper.js"]}
  r1 = _install_with_sources(
    client, auth, base, m1, JSX, {"helper.js": b"export const x = 1\n"},
  )
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  # Upstream drops the sibling — same entry bytes, no more source_files.
  m2 = {**MANIFEST_NEWS, "id": "uc-removed"}
  res = _update_check(client, auth, base, app_id, m2, JSX)
  assert res.status_code == 200, res.text
  assert res.json()["update_available"] is True


def test_update_check_no_manifest_url_is_null(client, auth, db, tmp_path):
  """An app with no manifest_url can't be checked git-natively → null."""
  from app import models
  app = models.App(
    name="uc-nomani", description="", jsx_source="export default () => null",
    source_dir=str(tmp_path / "nomani"), slug="uc-nomani",
    manifest_url=None, version="3.0.0",
    cross_app_access="none", share_with_apps="none",
    offline_capable=False, manage_apps=False,
  )
  db.add(app)
  db.commit()

  res = client.get(f"/api/apps/{app.id}/update-check", headers=auth)
  assert res.status_code == 200, res.text
  payload = res.json()
  assert payload["update_available"] is None
  # Version still flows through as a display label, but cannot become a
  # substitute update signal when the source is unverifiable.
  assert payload["local_version"] == "3.0.0"
  assert payload["upstream_version"] is None
  assert payload["installed_source_revision"] is None
  assert payload["candidate_source_digest"] is None


def test_update_check_no_git_repo_is_null(client, auth, db, tmp_path):
  """A manifest_url'd app whose source_dir isn't a git repo → null."""
  from app import models
  plain = tmp_path / "norepo"
  plain.mkdir()
  app = models.App(
    name="uc-norepo", description="", jsx_source="export default () => null",
    source_dir=str(plain), slug="uc-norepo",
    manifest_url="https://uc-norepo.test/repo#manifest-id=uc-norepo",
    version="1.0.0",
    cross_app_access="none", share_with_apps="none",
    offline_capable=False, manage_apps=False,
  )
  db.add(app)
  db.commit()

  res = client.get(f"/api/apps/{app.id}/update-check", headers=auth)
  assert res.status_code == 200, res.text
  assert res.json()["update_available"] is None


def test_update_check_no_upstream_branch_is_null(client, auth, db, tmp_path):
  """A git repo with no recorded `upstream` branch → null (nothing to diff)."""
  from app import models
  repo = tmp_path / "bare-repo"
  repo.mkdir()
  subprocess.run(["git", "init", "-q", str(repo)], check=True)
  app = models.App(
    name="uc-noups", description="", jsx_source="export default () => null",
    source_dir=str(repo), slug="uc-noups",
    manifest_url="https://uc-noups.test/repo#manifest-id=uc-noups",
    version="1.0.0",
    cross_app_access="none", share_with_apps="none",
    offline_capable=False, manage_apps=False,
  )
  db.add(app)
  db.commit()

  res = client.get(f"/api/apps/{app.id}/update-check", headers=auth)
  assert res.status_code == 200, res.text
  assert res.json()["update_available"] is None


def test_update_check_network_failure_degrades_to_null(
  client, auth, bypass_url_validation,
):
  """An unreachable upstream is a 200 + null (store open must degrade)."""
  base = "https://uc-netfail.test/repo/"
  m = {**MANIFEST_NEWS, "id": "uc-netfail"}
  r1 = _install_v1(client, auth, base, m, JSX)
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]

  # A failed Git fetch degrades to unknown rather than breaking Store refresh.
  with patch(
    "app.routes.apps._fetch_update_candidate",
    side_effect=RuntimeError("synthetic origin outage"),
  ):
    res = client.get(f"/api/apps/{app_id}/update-check", headers=auth)
  assert res.status_code == 200, res.text
  assert res.json()["update_available"] is None


def test_update_check_releases_db_connection_before_remote_fetch(
  client, auth, bypass_url_validation,
):
  """A slow fan-out check must not pin one pooled connection per request."""
  from fastapi import HTTPException
  from app.database import checked_out_connections

  base = "https://uc-pool.test/repo/"
  manifest = {**MANIFEST_NEWS, "id": "uc-pool"}
  installed = _install_v1(client, auth, base, manifest, JSX)
  assert installed.status_code == 201, installed.text
  app_id = installed.json()["id"]

  baseline = checked_out_connections()

  async def _slow_remote_fetch(_repo, _url, *, strict=True):
    assert checked_out_connections() <= baseline, (
      "update-check kept its request DB connection checked out while "
      "starting remote work"
    )
    raise HTTPException(status_code=502, detail="synthetic upstream outage")

  with patch(
    "app.routes.apps._fetch_update_candidate", new=_slow_remote_fetch,
  ):
    res = client.get(f"/api/apps/{app_id}/update-check", headers=auth)

  assert res.status_code == 200, res.text
  assert res.json()["update_available"] is None


def test_update_check_unknown_app_id_is_404(client, auth):
  """A genuinely invalid request keeps its normal HTTP error — not a degrade."""
  res = client.get("/api/apps/9999999/update-check", headers=auth)
  assert res.status_code == 404, res.text


def test_update_check_rejects_ordinary_app_token_for_other_app(
  client, db, auth, bypass_url_validation,
):
  """App tokens without manage_apps cannot check another app — mirrors preview."""
  from app.auth import create_access_token
  base = "https://uc-denied.test/repo/"
  m = {**MANIFEST_NEWS, "id": "uc-denied-target"}
  r1 = _install_v1(client, auth, base, m, JSX)
  assert r1.status_code == 201, r1.text
  target_app_id = r1.json()["id"]

  caller_app_id = _seed_app_with_perms(
    db, perms_cross_write="none", manage_apps=False,
  )
  db.commit()
  token = create_access_token({
    "sub": "test", "scope": "app", "app_id": caller_app_id,
  })

  res = client.get(
    f"/api/apps/{target_app_id}/update-check",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert res.status_code == 403, res.text
  assert "manage_apps" in res.json()["detail"]


def test_update_check_accepts_app_token_with_manage_apps_for_other_app(
  client, db, auth, bypass_url_validation,
):
  """A manage_apps token (the App Store) may check apps it manages."""
  from app.auth import create_access_token
  base = "https://uc-manager.test/repo/"
  m = {**MANIFEST_NEWS, "id": "uc-manager-target"}
  r1 = _install_v1(client, auth, base, m, JSX)
  assert r1.status_code == 201, r1.text
  target_app_id = r1.json()["id"]

  manager_app_id = _seed_app_with_perms(
    db, perms_cross_write="none", manage_apps=True,
  )
  db.commit()
  token = create_access_token({
    "sub": "test", "scope": "app", "app_id": manager_app_id,
  })

  res = _update_check(
    client, {"Authorization": f"Bearer {token}"}, base, target_app_id, m, JSX,
  )
  assert res.status_code == 200, res.text
  payload = res.json()
  assert payload["update_available"] is False
  assert payload["upstream_version"] == "1.0.0"


def test_static_only_store_reinstall_publishes_a_distinct_runtime(client, auth, db, bypass_url_validation):
  """Generated assets have their own publication identity, not the source SHA."""
  from app.applied_app_runtime import runtime_root

  base = "https://packages.test/x/runtime-static/main/"
  manifest = {
    "id": "runtime-static", "name": "Runtime static", "version": "1.0.0",
    "description": "Generated assets", "entry": "index.jsx", "permissions": {},
    "static_assets": {"asset.txt": "build/asset.txt"},
  }

  def install(content):
    responses = {
      base + "mobius.json": (200, json.dumps(manifest).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "build/asset.txt": (200, content),
    }
    with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses)):
      result = client.post("/api/apps/install", headers=auth,
                           json={"manifest_url": base + "mobius.json"})
    assert result.status_code == 201, result.text
    return result.json()["id"]

  app_id = install(b"first assets")
  row = db.get(models.App, app_id)
  first_runtime = runtime_root(row)
  first_source_commit = row.source_commit
  install(b"second assets")
  db.refresh(row)
  assert row.source_commit != first_source_commit
  assert runtime_root(row) != first_runtime
  assert (first_runtime / "static" / "asset.txt").read_bytes() == b"first assets"
  response = client.get(f"/app-assets/by-id/{app_id}/asset.txt")
  assert response.status_code == 200
  assert response.content == b"second assets"


def test_ordinary_store_source_apply_preserves_package_assets_and_runtime_manifest(client, auth, db, bypass_url_validation):
  from app.applied_app_runtime import runtime_root

  base = "https://packages.test/x/runtime-store-apply/main/"
  manifest = {
    "id": "runtime-store-apply", "name": "Runtime store apply", "version": "1.0.0",
    "description": "Generated assets", "entry": "index.jsx", "permissions": {},
    "static_assets": {"asset.txt": "build/asset.txt"},
  }
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "build/asset.txt": (200, b"accepted static"),
  }
  with patch("app.install.httpx.AsyncClient", side_effect=_fake_async_client(responses)):
    installed = client.post("/api/apps/install", headers=auth,
                            json={"manifest_url": base + "mobius.json"})
  assert installed.status_code == 201, installed.text
  row = db.get(models.App, installed.json()["id"])
  old_manifest = (runtime_root(row) / "mobius.json").read_bytes()
  source = Path(row.source_dir)
  (source / "index.jsx").write_text("export default () => <div>new code</div>")
  (source / "mobius.json").write_text(json.dumps({**manifest, "name": "Unaccepted metadata"}))
  (source / "static" / "asset.txt").write_text("draft static output")
  applied = client.post("/api/apps/apply", headers=auth, json={"source_dir": str(source)})
  assert applied.status_code == 200, applied.text
  db.refresh(row)
  assert (runtime_root(row) / "mobius.json").read_bytes() == old_manifest
  assert client.get(f"/app-assets/by-id/{row.id}/asset.txt").content == b"accepted static"


def test_failed_fresh_git_install_can_retry_a_fixed_head(
  client, auth, tmp_path, bypass_url_validation,
):
  base = "https://raw.githubusercontent.com/acme/retry-install/main/"
  manifest = {
    "id": "retry-install", "name": "Retry install", "version": "1.0.0",
    "description": "review fixture", "entry": "index.jsx", "permissions": {},
  }
  broken = "export default () => <div>broken\n"
  work, bare, _ = _make_clone_fixture(
    tmp_path, broken, CLONE_CARDS_V1,
  )
  first = _install_clone_fixture(
    client, auth, base, manifest, broken, CLONE_CARDS_V1, bare, work=work,
  )
  assert first.status_code == 422, first.text

  fixed = "export default () => <div>fixed</div>\n"
  _push_clone_fixture(work, bare, fixed, CLONE_CARDS_V1)
  retried = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "1.0.1"},
    fixed, CLONE_CARDS_V1, bare, work=work,
  )
  assert retried.status_code == 201, retried.text


def test_unchanged_source_publication_does_not_create_recovery_files(tmp_path):
  from app.install import _write_source_file

  target = tmp_path / "index.jsx"
  target.write_bytes(b"unchanged\n")
  for _ in range(4):
    rollback_actions = []
    commit_actions = []
    _write_source_file(
      target,
      b"unchanged\n",
      rollback_actions,
      commit_actions,
      executable=False,
      expected_previous=b"unchanged\n",
      expected_previous_executable=False,
    )
    for action in commit_actions:
      action()
  assert list(tmp_path.glob("*.bak")) == []


def test_real_git_update_applies_upstream_gitignore(
  client, auth, tmp_path, bypass_url_validation,
):
  base = "https://raw.githubusercontent.com/acme/ignore-update/main/"
  manifest = {
    "id": "ignore-update", "name": "Ignore update", "version": "1.0.0",
    "description": "review fixture", "entry": "index.jsx", "permissions": {},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  (work / ".gitignore").write_text("local-old.txt\n")
  installed = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1,
    bare, work=work,
  )
  assert installed.status_code == 201, installed.text

  (work / ".gitignore").write_text("local-old.txt\nprivate-new.txt\n")
  updated = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    CLONE_INDEX_V1, CLONE_CARDS_V1, bare, work=work,
  )
  assert updated.status_code == 201, updated.text
  source = Path(get_settings().data_dir) / "apps" / "ignore-update"
  assert (source / ".gitignore").read_text() == (
    "local-old.txt\nprivate-new.txt\n"
  )


def test_real_git_update_excludes_tracked_runtime_files_from_frozen_source(
  client, auth, db, tmp_path, bypass_url_validation,
):
  from app.applied_app_runtime import runtime_root

  base = "https://raw.githubusercontent.com/acme/runtime-residue/main/"
  manifest = {
    "id": "runtime-residue", "name": "Runtime residue", "version": "1.0.0",
    "description": "review fixture", "entry": "index.jsx", "permissions": {},
  }
  work, bare, _ = _make_clone_fixture(
    tmp_path, CLONE_INDEX_V1, CLONE_CARDS_V1,
  )
  first = _install_clone_fixture(
    client, auth, base, manifest, CLONE_INDEX_V1, CLONE_CARDS_V1,
    bare, work=work,
  )
  assert first.status_code == 201, first.text

  (work / "settings.json").write_text('{"private": true}')
  second = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    CLONE_INDEX_V1, CLONE_CARDS_V1, bare, work=work,
  )
  assert second.status_code == 201, second.text
  row = db.query(models.App).populate_existing().filter_by(
    id=first.json()["id"],
  ).one()
  assert not (runtime_root(row) / "settings.json").exists()
  assert "settings.json" not in app_git.read_ref_tree(
    Path(row.source_dir), row.source_commit,
  )


def test_migrated_manifest_source_adopts_real_git(
  client, auth, db, tmp_path, monkeypatch,
):
  from app import install, schema_migrations
  from app.database import engine

  base = "https://raw.githubusercontent.com/mobius-os/app-legacy-adopt/main/"
  remote_url = "https://github.com/mobius-os/app-legacy-adopt.git"
  manifest = {
    "id": "legacy-adopt", "name": "Legacy adopt", "version": "1.0.0",
    "description": "review fixture", "entry": "index.jsx", "permissions": {},
  }
  jsx = "export default () => <div>version one</div>\n"
  source = Path(get_settings().data_dir) / "apps" / "legacy-adopt"
  source.mkdir(parents=True)
  (source / "index.jsx").write_text(jsx)
  (source / "mobius.json").write_text(json.dumps(manifest))
  row = models.App(
    name="Legacy adopt", description="", jsx_source=jsx, compiled_path="",
    slug="legacy-adopt", source_dir=str(source),
    manifest_url=base.rstrip("/") + "#manifest-id=legacy-adopt",
  )
  db.add(row)
  db.commit()
  app_id = row.id
  schema_migrations._require_git_app_sources(engine)
  migration_base = app_git.migration_baseline(source)
  assert migration_base == app_git.head_sha(
    source, app_git.UPSTREAM_BRANCH,
  )
  local_before = app_git.head_sha(source, app_git.LOCAL_BRANCH)

  work, bare, _ = _make_clone_fixture(
    tmp_path, jsx.replace("one", "two"), CLONE_CARDS_V1,
  )
  manifest_v2 = {**manifest, "version": "2.0.0"}
  (work / "mobius.json").write_text(json.dumps(manifest_v2))
  _fixture_commit(work, "new manifest")
  app_git._run(work, "push", "-q", str(bare), "main")
  monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
  monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{bare.as_uri()}.insteadOf")
  monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote_url)
  with (
    patch(
      "app.install._validate_url_safe",
      lambda url: (url, urlparse(url).netloc, urlparse(url).hostname),
    ),
    patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client({
        base + "mobius.json": (200, json.dumps(manifest_v2).encode()),
      }),
    ),
  ):
    checked = client.get(
      f"/api/apps/{app_id}/update-check",
      headers=auth,
      params={"manifest_url": base + "mobius.json"},
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["update_available"] is True
    assert app_git.head_sha(source, app_git.LOCAL_BRANCH) == local_before
    assert app_git.head_sha(source, app_git.UPSTREAM_BRANCH) == migration_base
    assert app_git.migration_baseline(source) == migration_base

    updated = client.post(
      "/api/apps/install", headers=auth,
      json={"manifest_url": base + "mobius.json"},
    )
  assert updated.status_code == 201, updated.text
  # Discovery can cross the explicit finite migration bridge, but a genuine
  # source conflict still stops before promotion. Keep the bridge until the
  # reviewed resolution lands so a retry can prove the same baseline again.
  assert set(updated.json()["conflict_paths"]) == {"index.jsx", "mobius.json"}
  assert app_git.migration_baseline(source) == migration_base
  assert install.pending_conflict_update_receipt_present(source)


def test_binary_rebase_conflict_requires_resolution(tmp_path):
  from app.routes.apps import _pending_update_state

  repo = tmp_path / "binary"
  repo.mkdir()
  app_git.ensure_repo(repo)
  (repo / "asset.bin").write_bytes(b"base\0bytes")
  app_git.commit_local(repo, "base")
  base = app_git.head_sha(repo, "main")
  app_git._run(repo, "branch", "-f", "upstream", base)
  (repo / "asset.bin").write_bytes(b"owner\0bytes")
  app_git.commit_local(repo, "owner binary")
  app_git._run(repo, "checkout", "upstream")
  (repo / "asset.bin").write_bytes(b"new upstream\0bytes")
  app_git._run(repo, "add", "asset.bin")
  app_git._run(repo, "commit", "-m", "upstream binary")
  upstream = app_git.head_sha(repo, "upstream")
  app_git._run(repo, "checkout", "main")

  paths = app_git.start_overlay_rebase(repo, base=base, onto=upstream)

  assert paths == ["asset.bin"]
  assert app_git.rebase_in_progress(repo)
  assert _pending_update_state(repo, upstream) == "needs_resolution"
