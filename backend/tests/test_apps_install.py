"""POST /api/apps/install — atomic install + update + rollback.

We exercise the endpoint against a mocked httpx.AsyncClient (no real
network) so tests run inside the existing pytest container without
external connectivity. The mocked layer returns canned (status, body)
tuples per URL so we can drive the install paths deterministically
and force failure modes.
"""

import asyncio
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
from urllib.parse import urlparse

import pytest

from app import app_git
from app.config import get_settings


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
def bypass_url_validation():
  """Skip the SSRF URL-safety check so mocked-httpx tests using
  hostnames that don't resolve via DNS (`x.test`, etc.) still work.
  Tests that DO want to exercise URL validation request this fixture
  by NOT including it — see test_install_rejects_*."""
  # Return (url, host_header, sni_host) — the validate-and-pin shape — WITHOUT
  # pinning, so the mocked httpx still sees the ORIGINAL url (the response map
  # is keyed by it).
  with patch("app.install._validate_url_safe",
             lambda url: (url, urlparse(url).netloc, urlparse(url).hostname)):
    yield


JSX = "export default function App() { return <div>ok</div> }"
PROMPT = "# default prompt\nDo the work.\n"


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
  base = "https://raw.githubusercontent.com/x/app-test-news/main/"
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
  # source_dir/index.jsx written for the file watcher
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


def test_install_static_site_assets_route_css_fonts_and_chunks(
  client, auth, bypass_url_validation,
):
  """CubeRun-style static bundles keep HTML, CSS, chunks, and media together.

  The important regression here is path shape: CSS is served from
  /app-assets/<slug>/static/css/..., so relative font URLs must resolve to
  sibling static/media assets and missing app assets must stay a 404, never
  the Mobius shell HTML.
  """
  base = "https://raw.githubusercontent.com/x/cuberun-lite/main/"
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
  bundle = (
    Path(get_settings().data_dir)
    / "compiled"
    / f"app-{result.json()['id']}.js"
  )
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
  base = "https://raw.githubusercontent.com/x/static-prune/main/"
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
  base = "https://raw.githubusercontent.com/x/app-test-build/main/"
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
  here, so _register_cron only installs the crontab entry."""
  from app import install

  app_dir = tmp_path / "reflection"
  app_dir.mkdir()
  job_path = app_dir / "fetch.sh"
  fake_scaffold = tmp_path / "init-cron-scaffold.sh"
  fake_scaffold.write_text("#!/bin/bash\n")

  # The inner CRON_SCAFFOLD patch overrides the autouse bypass so we reach
  # the subprocess call; subprocess.run is mocked so nothing shells out.
  with patch("app.install.CRON_SCAFFOLD", fake_scaffold), \
       patch("app.install.subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stderr="")
    install._register_cron("reflection", "0 6 * * *", job_path, 42)

  # 5th arg is the app id — so a reusable fetch.sh that reads "$1" fires
  # from cron, not just from the run-job endpoint. Regression for news-2:
  # a bundled fetch.sh requires its id and exits 2 without it.
  assert mock_run.call_args.args[0] == [
    str(fake_scaffold), "reflection", "0 6 * * *", "fetch.sh", "42",
  ]


def test_register_cron_omits_app_id_when_none(tmp_path):
  """A self-contained job (hardcoded id) needs no app-id arg — the
  scaffold call stays 4 elements so the crontab command stays bare."""
  from app import install

  app_dir = tmp_path / "selfcontained"
  app_dir.mkdir()
  job_path = app_dir / "job.sh"
  fake_scaffold = tmp_path / "init-cron-scaffold.sh"
  fake_scaffold.write_text("#!/bin/bash\n")

  with patch("app.install.CRON_SCAFFOLD", fake_scaffold), \
       patch("app.install.subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stderr="")
    install._register_cron("selfcontained", "0 6 * * *", job_path)

  assert mock_run.call_args.args[0] == [
    str(fake_scaffold), "selfcontained", "0 6 * * *", "job.sh",
  ]


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


@pytest.mark.parametrize("legacy_shape", ["mobius.json", "bare", "trailing"])
def test_update_matches_legacy_manifest_url_shape_in_place(
    legacy_shape, client, db, auth, bypass_url_validation):
  """A row whose manifest_url is a LEGACY shape (the raw `<base>/mobius.json`
  install-core-apps / register_app wrote, or a very old bare `<base>` / `<base>/`)
  must still resolve as an UPDATE, not fork a duplicate install. Regression for
  the "app installed, not updated" core-app dup that shipped once core apps
  became store-updatable — covering every legacy shape in the fallback's `in_`."""
  from app import models
  # A trusted mobius-os base — the shape-drift fallback is gated on it, and every
  # affected legacy row (the core apps) is mobius-os.
  base = "https://raw.githubusercontent.com/mobius-os/app-legacytest/main/"
  responses = {
    base + "mobius.json": (200, json.dumps({**MANIFEST_NEWS, "version": "1.0.0"}).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"p"),
    base + "fetch.sh": (200, b""),
  }
  with patch("app.install.httpx.AsyncClient",
             side_effect=_fake_async_client(responses)):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201, r1.text
  v1_id = r1.json()["id"]

  # Drift the row's manifest_url to the legacy shape under test, exactly as a
  # platform-installed core app carries it (canonical exact-match would miss).
  legacy_manifest_url = {
    "mobius.json": base + "mobius.json",
    "bare": base.rstrip("/"),
    "trailing": base,
  }[legacy_shape]
  row = db.query(models.App).filter(models.App.id == v1_id).first()
  row.manifest_url = legacy_manifest_url
  db.commit()

  responses_v2 = {
    **responses,
    base + "mobius.json": (200, json.dumps({**MANIFEST_NEWS, "version": "2.0.0"}).encode()),
  }
  with patch("app.install.httpx.AsyncClient",
             side_effect=_fake_async_client(responses_v2)):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"      # matched the legacy-shape row
  assert r2.json()["id"] == v1_id           # same row, no duplicate
  assert r2.json()["version"] == "2.0.0"
  rows = db.query(models.App).filter(models.App.slug.like("test-news%")).all()
  assert len(rows) == 1, [r.slug for r in rows]
  db.refresh(row)
  assert row.manifest_url.endswith("#manifest-id=test-news")  # self-healed


def _seed_null_manifest_core(db, slug: str):
  """A baked core app the way install-core-apps registers it: a real slug and a
  real source tree under <data_dir>/apps/<slug>, but NO manifest_url (the shape
  the previous_id-adoption guard protects). Creating it under data_dir/apps also
  ensures that dir exists so a trusted adoption's source-tree rename can land."""
  from app import models
  import os
  source_dir = str(Path(get_settings().data_dir) / "apps" / slug)
  os.makedirs(source_dir, exist_ok=True)
  app = models.App(
    name=slug.title(),
    description="",
    jsx_source="export default function App() { return null }",
    source_dir=source_dir,
    slug=slug,
    manifest_url=None,
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()
  return app.id


def test_previous_id_cannot_adopt_reserved_slug_from_untrusted_source(
    client, db, auth, bypass_url_validation):
  """An owner-pasted (untrusted-host) manifest declaring previous_id=<core slug>
  must NOT take over the platform app — it installs as a SEPARATE app instead,
  leaving the core row untouched (card 172)."""
  from app import models
  core_id = _seed_null_manifest_core(db, "memory")

  base = "https://x.test/evil/"  # not the mobius-os catalog
  manifest = {**MANIFEST_NEWS, "id": "evil", "name": "Evil App",
              "previous_id": "memory"}
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"p"),
    base + "fetch.sh": (200, b""),
  }
  with patch("app.install.httpx.AsyncClient",
             side_effect=_fake_async_client(responses)):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  assert r.json()["mode"] == "install"   # a fresh app, NOT an adoption
  assert r.json()["id"] != core_id
  core = db.query(models.App).filter(models.App.id == core_id).first()
  assert core.slug == "memory"
  assert core.manifest_url is None
  assert core.deleted_at is None


def test_previous_id_cannot_adopt_beat_machine_from_untrusted_source(
    client, db, auth, bypass_url_validation):
  """New native core apps must be reserved too, including Beat Machine."""
  from app import models
  core_id = _seed_null_manifest_core(db, "beat-machine")

  base = "https://x.test/evil-beat/"
  manifest = {**MANIFEST_NEWS, "id": "evil-beat", "name": "Evil Beat",
              "previous_id": "beat-machine"}
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"p"),
    base + "fetch.sh": (200, b""),
  }
  with patch("app.install.httpx.AsyncClient",
             side_effect=_fake_async_client(responses)):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })

  assert r.status_code == 201, r.text
  assert r.json()["mode"] == "install"
  assert r.json()["id"] != core_id
  core = db.query(models.App).filter(models.App.id == core_id).first()
  assert core.slug == "beat-machine"
  assert core.manifest_url is None


def test_previous_id_may_adopt_reserved_slug_from_trusted_catalog(
    client, db, auth, bypass_url_validation):
  """The trusted mobius-os catalog CAN still supersede a baked core app via
  previous_id — the guard only blocks UNTRUSTED sources, so legitimate renames
  (e.g. Reflection adopting the old baked row) keep working."""
  from app import models
  core_id = _seed_null_manifest_core(db, "memory")

  base = "https://raw.githubusercontent.com/mobius-os/app-memory/main/"
  manifest = {**MANIFEST_NEWS, "id": "memory-catalog", "name": "Memory",
              "previous_id": "memory"}
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"p"),
    base + "fetch.sh": (200, b""),
  }
  with patch("app.install.httpx.AsyncClient",
             side_effect=_fake_async_client(responses)):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r.status_code == 201, r.text
  assert r.json()["mode"] == "update"   # adopted the baked core row
  assert r.json()["id"] == core_id


def test_untrusted_same_base_inline_install_does_not_overwrite_legacy_row(
    client, db, auth, bypass_url_validation):
  """An UNTRUSTED install (here the inline-manifest path, where the caller
  supplies both the id and the raw_base) that merely SHARES a base with an
  existing legacy-shape row — but declares a different manifest id — must NOT
  match/overwrite that row. The shape-drift fallback is gated on the trusted
  mobius-os catalog, so this installs a SEPARATE app. Regression for the Codex
  adversarial finding that base-only matching could flip a fresh install into an
  in-place overwrite of an existing (incl. core) row before any adoption check."""
  from app import models
  base = "https://x.test/shared/"  # untrusted host, shared base
  victim = models.App(
    name="Victim", description="",
    jsx_source="export default function App() { return null }",
    source_dir="/tmp/victim-app", slug="victim",
    manifest_url=base + "mobius.json",  # the legacy shape the fallback matches
    cross_app_access="none", share_with_apps="none", offline_capable=False,
  )
  db.add(victim)
  db.commit()
  victim_id = victim.id

  manifest = {**MANIFEST_NEWS, "id": "attacker", "name": "Attacker"}
  responses = {
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"p"),
    base + "fetch.sh": (200, b""),
  }
  with patch("app.install.httpx.AsyncClient",
             side_effect=_fake_async_client(responses)):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest": manifest,
      "raw_base": base,
    })
  assert r.status_code == 201, r.text
  assert r.json()["mode"] == "install"   # a separate app, NOT an overwrite
  assert r.json()["id"] != victim_id
  victim = db.query(models.App).filter(models.App.id == victim_id).first()
  assert victim.slug == "victim"
  assert victim.name == "Victim"
  assert victim.manifest_url == base + "mobius.json"  # untouched


def test_is_trusted_catalog_source_rejects_path_traversal():
  """`raw.githubusercontent.com/mobius-os/../evil/...` string-matches mobius-os
  but GitHub resolves it to the `evil` org — it must read as UNTRUSTED so the
  guard/fallback can't be bypassed (Codex adversarial finding)."""
  from app.install import _is_trusted_catalog_source as trusted
  assert trusted("https://raw.githubusercontent.com/mobius-os/app-x/main#manifest-id=x")
  assert not trusted("https://raw.githubusercontent.com/mobius-os/../evil/app-x/main#manifest-id=x")
  assert not trusted("https://raw.githubusercontent.com/mobius-os/%2e%2e/evil/x/main#manifest-id=x")
  assert not trusted("https://raw.githubusercontent.com.evil.com/mobius-os/x/main#manifest-id=x")
  assert not trusted("https://raw.githubusercontent.com/mobius-os#manifest-id=x")  # too shallow


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
  parse clone the wrong tree (repo root at a mis-read ref), so both must fall
  back to None (synthetic install), and a leading-dash ref is rejected so it
  can't reach git as an option."""
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


def test_update_of_legacy_no_source_dir_app_makes_no_stray_dir(
    client, auth, db, bypass_url_validation):
  """A legacy app with no source_dir must not get a /data/apps/<slug>/ on update.

  `drop_prior_cron` forced EVERY update into the cron block, whose
  `app_data_dir.mkdir` then materialized an empty /data/apps/<slug>/ even for a
  legacy row that never had a source dir or a crontab line to converge (card
  099, stray-dir follow-up). Guarding `drop_prior_cron` on `app.source_dir`
  mirrors the git block and keeps the cron block off a sourceless update.
  """
  from app import models

  # No `schedule` at all, so bundled_job/has_cron are both false on update —
  # the ONLY thing that can force the cron block (and its stray mkdir) is
  # drop_prior_cron. That isolates the guard under test.
  manifest_noschedule = {
    "id": "test-legacy",
    "name": "Test Legacy",
    "version": "1.0.0",
    "description": "Legacy app, no schedule.",
    "entry": "index.jsx",
    "icon": "icon.png",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
    "runtime": {"imports": ["react"], "esm_deps": []},
  }
  base = "https://x.test/repo-legacy/"
  responses_v1 = {
    base + "mobius.json": (200, json.dumps(manifest_noschedule).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
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

  data_dir = Path(get_settings().data_dir)
  source_dir = data_dir / "apps" / "test-legacy"
  # Simulate a legacy row predating the source_dir era: clear source_dir and
  # remove its on-disk tree so the next update takes the sourceless path. Match
  # by manifest_url is independent of source_dir, so this still resolves as an
  # update.
  row = db.query(models.App).filter(models.App.id == v1_id).first()
  row.source_dir = ""
  db.commit()
  if source_dir.exists():
    import shutil as _shutil
    _shutil.rmtree(source_dir)

  responses_v2 = {
    base + "mobius.json": (200, json.dumps({
      **manifest_noschedule, "version": "2.0.0",
    }).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
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
  assert r2.json()["id"] == v1_id
  # No stray empty /data/apps/<slug>/ materialized for the sourceless update.
  assert not source_dir.exists(), "update materialized a stray source dir"
  # And no cwd-relative write either: Path("") is PosixPath('.') (truthy), so
  # an empty source_dir once slipped into the materialize branch and wrote
  # index.jsx into the process cwd — backend/ under pytest, the uvicorn cwd
  # in production.
  assert not Path("index.jsx").exists(), (
    "sourceless update wrote index.jsx into the process cwd"
  )


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


def test_install_same_manifest_via_url_and_inline_matches(
  client, auth, bypass_url_validation,
):
  """Same app installed twice — once via `manifest_url` pointing at
  `.../mobius.json`, once via inline `manifest` + `raw_base` — must
  collapse onto a single App row. The two paths used to write
  visibly different strings into `App.manifest_url` (literal URL vs
  synthesized `<base>#manifest-id=<id>`), so the re-install lookup
  missed and produced a duplicate. The canonicaliser now folds both
  into the same identity key."""
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

  # Second install: inline form pointing at the same base. The
  # canonicaliser must recognise these as the same app.
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest": manifest,
      "raw_base": base,
    })
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "update", (
    "Re-installing the same manifest via inline + raw_base should "
    "update the existing row, not create a duplicate."
  )
  assert payload["id"] == first_id


def test_install_rolls_back_on_compile_failure(client, auth, bypass_url_validation):
  """Bad JSX → compile fails → no App row, no source_dir, no seeds."""
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
  # No app row, no source_dir
  data_dir = Path(get_settings().data_dir)
  assert not (data_dir / "apps" / "rollback-target" / "index.jsx").exists()
  list_r = client.get("/api/apps/", headers=auth)
  slugs = [a["slug"] for a in list_r.json()]
  assert "rollback-target" not in slugs


def test_install_inline_manifest_requires_raw_base(client, auth):
  """Inline `manifest` without `raw_base` → 400 (we don't know where
  to fetch entry JSX from)."""
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest": {**MANIFEST_NEWS, "id": "inline-test"},
  })
  assert r.status_code == 400
  assert "raw_base" in r.json()["detail"].lower()


def test_install_inline_raw_base_may_omit_trailing_slash(
  client, auth, bypass_url_validation,
):
  """Inline callers may pass either .../main or .../main/ as raw_base.

  The store passes the slash today, but this endpoint is public platform
  surface; normalizing here prevents a future caller from fetching
  `mainindex.jsx` by accident.
  """
  base = "https://raw.githubusercontent.com/x/app-inline-main"
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
  assert r.status_code == 201, r.text
  assert r.json()["manifest_url"] == (
    base + "#manifest-id=inline-noslash"
  )


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
  assert "raw_base" in r.json()["detail"]


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
  ({"static_assets": "build/index.html"}, "static_assets"),
])
def test_install_rejects_non_repo_relative_manifest_asset_paths(
  client, auth, field_patch, expected_field,
):
  """External manifests must point asset references inside their repo.

  This mirrors the public schema and keeps mistakes/hostile manifests as
  precise 400s rather than odd URL concatenations or late install 500s.
  """
  manifest = {**MANIFEST_NEWS, "id": "bad-asset-path", **field_patch}
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest": manifest,
    "raw_base": "https://raw.githubusercontent.com/x/app/main/",
  })
  assert r.status_code == 400
  assert expected_field in r.json()["detail"]


def test_storage_seeds_inline_content_400_teaches_the_contract(client, auth):
  """A string seed value that is really inline content fails the path check,
  and the 400 names the path-vs-inline-JSON contract — not just "must be a
  relative path" — so the author sees the wrong shape, not a phantom typo.
  This is the footgun that made Web Studio mis-encode its starter files."""
  inline_html = '<!DOCTYPE html>\n<a href="#features">hi</a>\n'
  manifest = {
    **MANIFEST_NEWS,
    "id": "seed-inline-content",
    "storage_seeds": {"files/index.html": inline_html},
  }
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest": manifest,
    "raw_base": "https://raw.githubusercontent.com/x/app/main/",
  })
  assert r.status_code == 400
  detail = r.json()["detail"]
  assert "storage_seeds.files/index.html" in detail
  assert "non-string" in detail and "installer fetches" in detail


def test_non_seed_path_rejection_omits_the_seed_hint(client, auth):
  """The seed-specific teaching hint attaches only to storage_seeds fields;
  entry/icon/static_assets strings are always paths, so their 400 stays
  generic and never mentions storage_seeds."""
  manifest = {**MANIFEST_NEWS, "id": "bad-entry-path", "entry": "../index.jsx"}
  r = client.post("/api/apps/install", headers=auth, json={
    "manifest": manifest,
    "raw_base": "https://raw.githubusercontent.com/x/app/main/",
  })
  assert r.status_code == 400
  detail = r.json()["detail"]
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
  assert "block" in r.json()["detail"].lower() or "resolve" in r.json()["detail"].lower()


def test_install_rejects_non_http_scheme(client, auth):
  """SSRF: file:// and other schemes are rejected."""
  for url in ("file:///etc/passwd", "ftp://x/y.json", "gopher://x/"):
    r = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": url,
    })
    assert r.status_code == 400, url
    assert "scheme" in r.json()["detail"].lower()


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
  compiled_path = data_dir / "compiled" / f"app-{app_id}.js"
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
  # The .bak file should be gone (either restored or never created).
  assert not compiled_path.with_suffix(".js.bak").exists()


# --- manifest_url is the new identity key (slug is routing only) ----


def test_install_with_same_slug_different_manifest_keeps_both(
  client, auth, bypass_url_validation,
):
  """A user-built app and a store-installed app may want the same
  slug stem. After the manifest_url refactor, identity is keyed on
  manifest_url, so the store install must NOT clobber the user app —
  it lands as a fresh row with slug='news-2' (or similar) instead."""
  # 1. User builds an app named "News" via the regular create path.
  r0 = client.post("/api/apps/", headers=auth, json={
    "name": "News",
    "description": "user-built news reader",
    "jsx_source": JSX,
  })
  assert r0.status_code == 201, r0.text
  user_app = r0.json()
  user_id = user_app["id"]
  assert user_app["slug"] == "news"
  assert user_app["manifest_url"] is None

  # 2. Store installs a manifest whose id is also "news".
  base = "https://raw.githubusercontent.com/x/app-news/main/"
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


def test_install_same_manifest_twice_updates(
  client, auth, bypass_url_validation,
):
  """Re-installing the same manifest_url updates the existing row
  in place (mode='update', same id) — identity now keyed on URL."""
  base = "https://raw.githubusercontent.com/x/app-same/main/"
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

  base = "https://raw.githubusercontent.com/x/app-installable/main/"
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
  assert r.headers["access-control-allow-origin"] == "null"


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
  # app's id as a string (matches the file-watcher's payload shape).
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


def test_delete_publishes_app_updated(client, auth):
  """Uninstall must also refresh the drawer — Shell.jsx's app_updated
  handler refetches /api/apps/, which then no longer contains the
  deleted row, so the entry disappears without a page reload."""
  r0 = client.post("/api/apps/", headers=auth, json={
    "name": "Doomed",
    "description": "",
    "jsx_source": JSX,
  })
  assert r0.status_code == 201, r0.text
  app_id = r0.json()["id"]
  with patch("app.routes.apps.get_system_broadcast") as mock_get_sb:
    fake_sb = MagicMock()
    mock_get_sb.return_value = fake_sb
    r = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert r.status_code == 204, r.text
  fake_sb.publish.assert_called_once_with({
    "type": "app_updated", "appId": str(app_id),
  })


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


def test_flag_on_install_creates_repo_and_records_upstream(
  client, auth, bypass_url_validation,
):
  """Flag ON: a fresh install inits the per-app repo and stamps the
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


def test_flag_on_clean_update_carries_local_edits_forward(
  client, auth, bypass_url_validation,
):
  """Flag ON: a local edit to one region + an upstream edit to a DISJOINT
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


def test_flag_on_repeated_updates_to_same_region_stay_clean(
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


def test_flag_on_clean_update_advances_merge_base(
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

  from app import app_git
  env = app_git._git_env(repo)
  proc = subprocess.run(
    ["git", "-C", str(repo), "merge-base", "--is-ancestor", "upstream", "main"],
    env=env, capture_output=True,
  )
  assert proc.returncode == 0, (
    "upstream tip must be an ancestor of main after a clean merge so the "
    "next update's base is the just-merged version"
  )


def test_flag_on_clean_update_without_local_edits_is_fast_forward(
  client, auth, bypass_url_validation,
):
  """Flag ON: when local main still matches the previous upstream, a
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
  # With no local edits the served source must be the new upstream verbatim.
  # The latent bug let a failed in-memory merge leave the OLD bytes on disk
  # while still bumping the version, so assert the new content actually
  # landed rather than trusting the divergence label alone.
  data_dir = Path(get_settings().data_dir)
  served = (data_dir / "apps" / "on-fast-forward" / "index.jsx").read_text()
  assert "UPSTREAM FOOTER" in served
  assert "ORIGINAL FOOTER" not in served


def test_flag_on_consecutive_no_edit_updates_advance_base(
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


def test_flag_on_static_asset_update_leaves_clean_app_repo(
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


def test_flag_on_conflicting_update_leaves_source_unchanged_until_resolve(
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
  assert update.json()["upstream_version"] == "2.0.0"

  bypass = client.patch(
    f"/api/apps/{payload['id']}",
    headers=auth,
    json={"jsx_source": "export default function App(){return <div>bypass</div>}"},
  )
  assert bypass.status_code == 409, bypass.text

  # One Resolve click materializes a real merge. Saving marker-free text (no
  # manual git add/commit) must run the complete installer replay exactly once.
  resolver = client.post(
    f"/api/apps/{payload['id']}/conflict-resolver-chat", headers=auth,
  )
  assert resolver.status_code == 200, resolver.text
  assert (app_dir / ".git" / "MERGE_HEAD").is_file()
  resolved = JSX_MULTI.replace("ORIGINAL TITLE", "RESOLVED TITLE")
  jsx_file.write_text(resolved)

  replay_responses = {
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"v2 prompt"),
    base + "fetch.sh": (200, b""),
  }

  async def _run_watcher():
    from app.app_watcher import _JsxHandler
    handler = _JsxHandler(asyncio.get_running_loop())
    await handler._recompile(str(jsx_file), force_rebuild=True)

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(replay_responses),
  ):
    asyncio.run(_run_watcher())

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


def test_version_only_conflict_auto_resolves_to_upstream(
  client, auth, bypass_url_validation,
):
  """A conflict CONFINED to the app's version identifier must NOT spawn a
  resolver: install auto-resolves it to the upstream version and returns
  mode='update'. This exercises the full wiring (install_from_manifest →
  app_git.resolve_version_only_conflict), not just the git helper."""
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
  assert payload["mode"] == "update", payload  # auto-resolved, no conflict chat
  assert 'APP_VERSION = "2.0.0"' in jsx_file.read_text()  # upstream version won


def test_resolved_conflict_changed_candidate_fails_closed_and_stays_retryable(
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
  )
  assert resolver.status_code == 200, resolver.text
  resolved = JSX_MULTI.replace("ORIGINAL TITLE", "RESOLVED TITLE")
  jsx_file.write_text(resolved)
  pending = app_dir / ".git" / "mobius-pending-update" / "receipt.json"
  bundle = data_dir / "compiled" / f"app-{app_id}.js"
  old_bundle = bundle.read_bytes()

  # Only a seed byte moved at the same URL/version. The replay must reject the
  # whole candidate before changing any DB/static/live-bundle state.
  changed_responses = {
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, b"release C changed this byte"),
    base + "fetch.sh": (200, b""),
  }

  async def _run_watcher():
    from app.app_watcher import _JsxHandler
    await _JsxHandler(asyncio.get_running_loop())._recompile(
      str(jsx_file), force_rebuild=True,
    )

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(changed_responses),
  ):
    asyncio.run(_run_watcher())

  db = SessionLocal()
  try:
    app = db.query(App).filter(App.id == app_id).first()
    assert app.version == "1.0.0"
    assert app.jsx_source == JSX_MULTI
  finally:
    db.close()
  assert bundle.read_bytes() == old_bundle
  assert pending.is_file(), "journal must survive for restart/user retry"
  assert not (app_dir / ".git" / "MERGE_HEAD").exists()
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
  )
  assert resolver.status_code == 200, resolver.text
  resolved = JSX_MULTI.replace("ORIGINAL TITLE", "RESOLVED TITLE")
  jsx_file.write_text(resolved)

  replay_responses = {
    base + "index.jsx": (200, jsx_v2.encode()),
    base + "build/new.js": (200, b"window.release='v2'"),
  }

  async def _run_watcher():
    from app.app_watcher import _JsxHandler
    await _JsxHandler(asyncio.get_running_loop())._recompile(
      str(jsx_file), force_rebuild=True,
    )

  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(replay_responses),
  ):
    asyncio.run(_run_watcher())

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


def test_conflict_resolver_click_materializes_merge_for_agent(
  client, auth, bypass_url_validation, monkeypatch,
):
  """The update attempt is read-only on conflict; the owner's resolver click is
  the moment a real git merge with markers is materialized for the agent."""
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
    return True

  monkeypatch.setattr(
    "app.routes.apps._start_conflict_resolver_turn",
    fake_start_turn,
  )
  r3 = client.post(
    f"/api/apps/{app_id}/conflict-resolver-chat",
    headers=auth,
  )
  assert r3.status_code == 200, r3.text
  payload = r3.json()
  assert payload["chat_id"]
  assert payload["created"] is True
  assert payload["started"] is True

  materialized = jsx_file.read_text()
  assert "<<<<<<<" in materialized and ">>>>>>>" in materialized
  assert "AGENT TITLE" in materialized and "UPSTREAM TITLE" in materialized
  assert (app_dir / ".git" / "MERGE_HEAD").exists()


def test_flag_on_conflict_does_not_apply_upstream_capabilities(
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


def test_core_app_store_self_update_overwrites_local_conflict(
  client, auth, bypass_url_validation,
):
  """The App Store must be able to update itself from the App Store.

  For normal apps, a same-hunk local/upstream conflict returns
  mode='conflict'. For the canonical mobius-os App Store, upstream wins
  so an old store cannot get permanently wedged behind its own local edit.
  """
  base = "https://raw.githubusercontent.com/mobius-os/app-store/main/"
  m = {
    **MANIFEST_NEWS,
    "id": "store",
    "name": "App Store",
    "version": "1.0.0",
  }
  with patch("app.install._derive_repo_ref", return_value=None):
    r1 = _install_v1(client, auth, base, m, JSX_MULTI)
  assert r1.status_code == 201, r1.text
  data_dir = Path(get_settings().data_dir)
  jsx_file = data_dir / "apps" / "store" / "index.jsx"

  local = JSX_MULTI.replace("ORIGINAL TITLE", "LOCAL STORE TITLE")
  jsx_file.write_text(local)

  jsx_v2 = JSX_MULTI.replace("ORIGINAL TITLE", "UPSTREAM STORE TITLE")
  with patch("app.install._derive_repo_ref", return_value=None):
    r2 = _update_v2(client, auth, base, {**m, "version": "2.0.0"}, jsx_v2)
  assert r2.status_code == 201, r2.text
  payload = r2.json()
  assert payload["mode"] == "update"
  assert payload["version"] == "2.0.0"
  assert payload["conflict_paths"] == []
  assert any("core App Store self-update" in w for w in payload["warnings"])

  served = jsx_file.read_text()
  assert served == jsx_v2
  assert "LOCAL STORE TITLE" not in served
  assert "<<<<<<<" not in served

  from app.models import App
  from app.database import SessionLocal
  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "store").first()
    assert app.version == "2.0.0"
    assert app.jsx_source == jsx_v2
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
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
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


def _simple_manifest(app_id, version="1.0.0", previous_id=None):
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
  client, auth, bypass_url_validation,
):
  """The immediate install response must match the persisted app capabilities."""
  base = "https://caps.test/repo/"
  manifest = _simple_manifest("capability-app")
  manifest["embeds_agent"] = True
  manifest["permissions"]["manage_apps"] = True
  manifest["permissions"]["github_access"] = True
  manifest["permissions"]["filesystem_access"] = True

  r = _install_simple(client, auth, base, manifest)

  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["embeds_agent"] is True
  assert payload["manage_apps"] is True
  assert payload["github_access"] is True
  assert payload["filesystem_access"] is True

  listed = client.get("/api/apps/", headers=auth).json()
  row = next(app for app in listed if app["id"] == payload["id"])
  assert row["embeds_agent"] is True
  assert row["manage_apps"] is True
  assert row["github_access"] is True
  assert row["filesystem_access"] is True


def test_install_rejects_non_boolean_filesystem_capability(
  client, auth, bypass_url_validation,
):
  manifest = _simple_manifest("bad-filesystem-capability")
  manifest["permissions"]["filesystem_access"] = "yes"
  response = _install_simple(client, auth, "https://bad-fs-cap.test/repo/", manifest)
  assert response.status_code == 400
  assert "filesystem_access" in response.text


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


def test_legacy_slug_adoption_of_no_manifest_url_row(
  client, auth, db, bypass_url_validation,
):
  """(b) a baked/register_app predecessor (slug='reflection-old',
  manifest_url=None) is ADOPTED by a catalog manifest id='reflection' that
  declares previous_id='reflection-old': same numeric id, manifest_url now set,
  source moved to the new slug, NO duplicate.

  Legacy adoption is opt-in via `previous_id` BY DESIGN. A baked predecessor
  created through register_app.py is byte-for-byte indistinguishable from a
  user-built app (both go through POST /api/apps/ → manifest_url=None,
  source_dir set), so adopting on slug-match ALONE would hijack a user's app —
  forbidden by test_install_with_same_slug_different_manifest_keeps_both. The
  manifest declaring previous_id is the author's explicit takeover intent."""
  from app import models
  data_dir = Path(get_settings().data_dir)
  src_dir = data_dir / "apps" / "reflection-old"
  src_dir.mkdir(parents=True, exist_ok=True)
  (src_dir / "index.jsx").write_text(JSX)
  app = models.App(
    name="Reflection",
    description="baked predecessor",
    jsx_source=JSX,
    source_dir=str(src_dir),
    slug="reflection-old",
    manifest_url=None,
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()
  baked_id = app.id

  base = "https://catalog.test/reflection/"
  r = _install_simple(
    client, auth, base,
    _simple_manifest("reflection", version="2.0.0", previous_id="reflection-old"),
  )
  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "update"
  assert payload["id"] == baked_id        # adopted the baked row
  assert payload["slug"] == "reflection"    # migrated to the new id's slug
  canonical = base.rstrip("/") + "#manifest-id=reflection"
  assert payload["manifest_url"] == canonical

  # Source moved to the new slug; the old baked dir is gone.
  assert (data_dir / "apps" / "reflection" / "index.jsx").exists()
  assert not (data_dir / "apps" / "reflection-old").exists()

  listed = client.get("/api/apps/", headers=auth).json()
  assert len(listed) == 1                  # no duplicate


def test_previous_id_cannot_adopt_legacy_platform_source_row(
  client, auth, db, bypass_url_validation,
):
  """A trusted rename must not adopt a legacy platform-source row.

  Historical platform rows migrate only when the catalog id is the SAME slug
  (memory -> memory, reflection -> reflection, beat-machine -> beat-machine).
  A previous_id rename shape still fails closed.
  """
  from app import models
  data_dir = Path(get_settings().data_dir)
  src_dir = data_dir / "platform" / "core-apps" / "memory"
  src_dir.mkdir(parents=True, exist_ok=True)
  (src_dir / "index.jsx").write_text(JSX)
  app = models.App(
    name="Memory",
    description="platform core",
    jsx_source=JSX,
    source_dir=str(src_dir),
    slug="memory",
    manifest_url=None,
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()

  base = "https://raw.githubusercontent.com/mobius-os/app-memory/main/"
  r = _install_simple(
    client, auth, base,
    _simple_manifest("memory-next", previous_id="memory"),
  )

  assert r.status_code == 409, r.text
  db.refresh(app)
  assert app.source_dir == str(src_dir)
  assert (src_dir / "index.jsx").exists()


def test_trusted_catalog_install_migrates_legacy_platform_row(
  client, auth, db, bypass_url_validation,
):
  """A trusted catalog install adopts the old platform row instead of creating
  memory-2, and moves editable source to /data/apps/memory."""
  from app import models
  data_dir = Path(get_settings().data_dir)
  src_dir = data_dir / "platform" / "core-apps" / "memory"
  src_dir.mkdir(parents=True, exist_ok=True)
  (src_dir / "index.jsx").write_text(JSX)
  app = models.App(
    name="Memory",
    description="platform core",
    jsx_source=JSX,
    source_dir=str(src_dir),
    slug="memory",
    manifest_url=None,
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()

  base = "https://raw.githubusercontent.com/mobius-os/app-memory/main/"
  r = _install_simple(client, auth, base, _simple_manifest("memory"))

  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "update"
  assert payload["id"] == app.id
  assert payload["slug"] == "memory"
  assert payload["source_dir"] == str(data_dir / "apps" / "memory")
  assert payload["manifest_url"] == base.rstrip("/") + "#manifest-id=memory"

  db.refresh(app)
  assert len(db.query(models.App).all()) == 1
  assert app.source_dir == str(data_dir / "apps" / "memory")
  assert app.manifest_url == base.rstrip("/") + "#manifest-id=memory"
  assert (data_dir / "apps" / "memory" / "index.jsx").exists()
  assert (src_dir / "index.jsx").exists()


def test_trusted_catalog_install_adopts_historical_data_apps_row(
  client, auth, db, bypass_url_validation,
):
  """Beat Machine shipped in a prod-era shape at /data/apps/<slug> with no
  manifest_url. Its trusted same-id catalog install must update that row in
  place, not create beat-machine-2."""
  from app import models
  data_dir = Path(get_settings().data_dir)
  legacy_id = _seed_null_manifest_core(db, "beat-machine")
  base = "https://raw.githubusercontent.com/mobius-os/app-beat-machine/main/"

  r = _install_simple(
    client, auth, base,
    _simple_manifest("beat-machine", version="1.0.5"),
  )

  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "update"
  assert payload["id"] == legacy_id
  assert payload["slug"] == "beat-machine"
  assert payload["source_dir"] == str(data_dir / "apps" / "beat-machine")
  assert payload["manifest_url"] == (
    base.rstrip("/") + "#manifest-id=beat-machine"
  )

  rows = (
    db.query(models.App)
    .filter(models.App.slug.like("beat-machine%"))
    .all()
  )
  assert len(rows) == 1, [row.slug for row in rows]
  assert (data_dir / "apps" / "beat-machine" / "index.jsx").read_text() == JSX


def test_historical_data_apps_adoption_preserves_owner_edits(
  client, auth, db, bypass_url_validation,
):
  """Adopting a pre-git-model app must NOT silently reset the owner's edits.

  The historical shape is editable source on disk + a DB jsx_source with NO
  per-app git repo and NO recorded upstream. Before the fix, adoption reached
  align_local_to_upstream (reset --hard) and the owner's on-disk edits ended up in
  ZERO git blobs — unrecoverable. Now the adoption captures the on-disk tree onto
  `main` first, then three-way merges: because the owner's index.jsx diverges from
  the catalog entry the merge verdicts a conflict, so the served source stays
  theirs until they resolve it AND the owner bytes live in a recoverable git blob.
  The identity still migrates once (canonical manifest_url stamped even on
  conflict) so the boot migration never re-fires.
  """
  from app import bootstrap, models
  data_dir = Path(get_settings().data_dir)
  slug = "beat-machine"
  src_dir = data_dir / "apps" / slug
  src_dir.mkdir(parents=True, exist_ok=True)
  owner_jsx = (
    "export default function App() { /* OWNER EDIT */ return <div>mine</div> }"
  )
  (src_dir / "index.jsx").write_text(owner_jsx)
  assert not app_git.is_repo(src_dir)

  app = models.App(
    name="Beat Machine",
    description="",
    jsx_source=owner_jsx,
    source_dir=str(src_dir),
    slug=slug,
    manifest_url=None,
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()
  app_id = app.id
  canonical = "https://raw.githubusercontent.com/mobius-os/app-beat-machine/main"
  base = canonical + "/"

  # The catalog entry (JSX) differs from the owner's edit — a real divergence.
  r = _install_simple(
    client, auth, base, _simple_manifest("beat-machine", version="1.0.5"),
  )
  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "conflict", payload
  assert "index.jsx" in payload["conflict_paths"]
  assert payload["id"] == app_id

  # The served source on disk is STILL the owner's, never the catalog bytes.
  assert (src_dir / "index.jsx").read_text() == owner_jsx
  assert (src_dir / "index.jsx").read_text() != JSX

  # The owner bytes survive in a git blob (recoverable): captured on `main`
  # before the catalog upstream was recorded.
  assert app_git.is_repo(src_dir)
  main_tree = app_git.read_ref_tree(src_dir, app_git.LOCAL_BRANCH)
  assert main_tree.get("index.jsx") == owner_jsx.encode()

  # Identity migrated (one-shot): the row now carries the canonical manifest_url,
  # so the boot predicate no longer treats it as an un-migrated historical row.
  db.refresh(app)
  assert app.manifest_url == canonical + "#manifest-id=beat-machine"
  assert bootstrap._is_legacy_platform_row(app) is False

  # A second catalog install (the next boot's Store update) resolves the row by
  # its now-canonical manifest_url — a normal update, NOT a re-triggered
  # migration — and still guards the unresolved owner edits behind a conflict,
  # forking no duplicate.
  r2 = _install_simple(
    client, auth, base, _simple_manifest("beat-machine", version="1.0.6"),
  )
  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "conflict"
  assert r2.json()["id"] == app_id
  assert (src_dir / "index.jsx").read_text() == owner_jsx
  rows = db.query(models.App).filter(models.App.slug.like("beat-machine%")).all()
  assert len(rows) == 1, [row.slug for row in rows]


@pytest.mark.parametrize("stored_manifest_url_shape", ["raw_mobius_json", "canonical"])
def test_trusted_catalog_update_migrates_legacy_platform_row_found_by_url(
  stored_manifest_url_shape, client, auth, db, bypass_url_validation,
):
  """Prod-era baked rows can carry an old catalog URL while still pointing at
  /data/platform/core-apps. Updating from the Store should repair that state in
  place instead of tripping the platform-owned-app guard."""
  from app import models
  data_dir = Path(get_settings().data_dir)
  src_dir = data_dir / "platform" / "core-apps" / "reflection"
  src_dir.mkdir(parents=True, exist_ok=True)
  (src_dir / "index.jsx").write_text(JSX)
  base = "https://raw.githubusercontent.com/mobius-os/app-reflection/main/"
  canonical = base.rstrip("/") + "#manifest-id=reflection"
  stored_manifest_url = {
    "raw_mobius_json": base + "mobius.json",
    "canonical": canonical,
  }[stored_manifest_url_shape]
  app = models.App(
    name="Reflection",
    description="platform core",
    jsx_source=JSX,
    source_dir=str(src_dir),
    slug="reflection",
    manifest_url=stored_manifest_url,
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()

  r = _install_simple(client, auth, base, _simple_manifest("reflection"))

  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "update"
  assert payload["id"] == app.id
  assert payload["slug"] == "reflection"
  assert payload["source_dir"] == str(data_dir / "apps" / "reflection")
  assert payload["manifest_url"] == canonical

  db.refresh(app)
  assert len(db.query(models.App).all()) == 1
  assert app.source_dir == str(data_dir / "apps" / "reflection")
  assert app.manifest_url == canonical
  assert (data_dir / "apps" / "reflection" / "index.jsx").exists()
  assert (src_dir / "index.jsx").exists()


def test_legacy_platform_migration_ignores_runtime_sidecar_repo(
  client, auth, db, bypass_url_validation,
):
  """The old runtime sidecar under /data/apps/<slug> is not editable source.

  Prod had Reflection source in /data/platform/core-apps/reflection, plus a
  stale repo at /data/apps/reflection used by scheduled runtime files. Migration
  must install the catalog source into /data/apps/reflection instead of merging
  that stale repo's deleted source files as local user edits.
  """
  from app import models
  data_dir = Path(get_settings().data_dir)
  platform_source = data_dir / "platform" / "core-apps" / "reflection"
  platform_source.mkdir(parents=True, exist_ok=True)
  old_jsx = "export default function App() { return <div>old</div> }"
  (platform_source / "index.jsx").write_text(old_jsx)

  runtime_dir = data_dir / "apps" / "reflection"
  base = "https://raw.githubusercontent.com/mobius-os/app-reflection/main/"
  old_upstream = app_git.record_upstream(
    runtime_dir,
    {"index.jsx": old_jsx.encode()},
    base.rstrip("/") + "#manifest-id=reflection",
    "0.9.0",
  )
  app_git.align_local_to_upstream(runtime_dir)
  (runtime_dir / ".gitignore").write_text(
    "# old managed ignore\n*.js\n*.bak\n[0-9]*/\n",
    encoding="utf-8",
  )
  (runtime_dir / "index.jsx").unlink()
  (runtime_dir / "inputs").mkdir()
  (runtime_dir / "inputs" / "activity.jsonl").write_text("keep\n")
  (runtime_dir / "last-run.json").write_text("{}\n")
  app_git._run(runtime_dir, "add", "-A", ".")
  app_git._run(runtime_dir, "commit", "-q", "-m", "runtime sidecar state")
  assert "inputs/activity.jsonl" in app_git._run(
    runtime_dir, "ls-files",
  ).stdout.split()

  app = models.App(
    name="Reflection",
    description="platform core",
    jsx_source=old_jsx,
    source_dir=str(platform_source),
    slug="reflection",
    manifest_url=None,
    version="0.9.0",
    upstream_commit=old_upstream,
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
  )
  db.add(app)
  db.commit()

  new_jsx = "export default function App() { return <div>catalog</div> }"
  r = _install_simple(
    client, auth, base, _simple_manifest("reflection", version="2.0.0"),
    jsx=new_jsx,
  )

  assert r.status_code == 201, r.text
  payload = r.json()
  assert payload["mode"] == "update"
  assert payload["conflict_paths"] == []
  assert payload["source_dir"] == str(runtime_dir)
  assert (runtime_dir / "index.jsx").read_text() == new_jsx
  assert (runtime_dir / "inputs" / "activity.jsonl").read_text() == "keep\n"
  assert (runtime_dir / "last-run.json").read_text() == "{}\n"
  tracked = set(app_git._run(runtime_dir, "ls-files").stdout.split())
  assert "inputs/activity.jsonl" not in tracked
  assert "last-run.json" not in tracked
  assert "init-cron.sh" not in tracked
  gitignore = (runtime_dir / ".gitignore").read_text(encoding="utf-8")
  assert "*.js" not in gitignore
  assert "inputs/" in gitignore

  db.refresh(app)
  assert app.version == "2.0.0"
  assert app.source_dir == str(runtime_dir)
  assert app.manifest_url == base.rstrip("/") + "#manifest-id=reflection"
  assert app_git.local_diverged_from(runtime_dir, app.upstream_commit) is False


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
# write them next to the entry, and compile with esbuild bundling the
# import graph. An update merges the whole tree so locally edited siblings
# survive. The JSX below imports a sibling so the compiled bundle proves
# esbuild resolved + inlined it; the sibling spacing mirrors JSX_MULTI so
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
  include_source_file=False,
):
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
  )
  assert r1.status_code == 201, r1.text

  index_v2 = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_V2")
  cards_v2 = CLONE_CARDS_V1.replace("CARD_V1", "CARD_V2")
  new_head = _push_clone_fixture(work, bare, index_v2, cards_v2)
  r2 = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    index_v2, CLONE_CARDS_V1, bare,
  )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "fast_forward"
  src = Path(get_settings().data_dir) / "apps" / "clone-ff"
  assert (src / "index.jsx").read_text() == index_v2
  assert (src / "cards.js").read_text() == cards_v2
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
    CLONE_INDEX_V1, CLONE_CARDS_V1, bare,
  )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "clean_merge"
  assert "TITLE_LOCAL" in (src / "index.jsx").read_text()
  assert (src / "cards.js").read_text() == cards_v2
  assert "<<<<<<<" not in (src / "index.jsx").read_text()


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
  )
  assert r1.status_code == 201, r1.text
  src = Path(get_settings().data_dir) / "apps" / "clone-conflict"
  local_index = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_LOCAL")
  (src / "index.jsx").write_text(local_index, encoding="utf-8")

  upstream_index = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_UPSTREAM")
  _push_clone_fixture(work, bare, upstream_index, CLONE_CARDS_V1)
  r2 = _install_clone_fixture(
    client, auth, base, {**manifest, "version": "2.0.0"},
    upstream_index, CLONE_CARDS_V1, bare,
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


def test_clone_update_fetch_failure_falls_back_to_record_upstream(
  client, auth, tmp_path, bypass_url_validation,
):
  """If origin fetch fails, cloned apps use the existing HTTP-fetched path."""
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
    include_source_file=True,
  )
  assert r1.status_code == 201, r1.text

  index_v2 = CLONE_INDEX_V1.replace("TITLE_V1", "TITLE_HTTP_V2")
  cards_v2 = CLONE_CARDS_V1.replace("CARD_V1", "CARD_HTTP_V2")
  _push_clone_fixture(work, bare, index_v2, cards_v2)
  with patch("app.app_git.fetch_upstream", side_effect=RuntimeError("offline")):
    r2 = _install_clone_fixture(
      client, auth, base, {**manifest, "version": "2.0.0"},
      index_v2, cards_v2, bare, include_source_file=True,
    )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["divergence"] == "fast_forward"
  src = Path(get_settings().data_dir) / "apps" / "clone-fallback"
  assert (src / "index.jsx").read_text() == index_v2
  assert (src / "cards.js").read_text() == cards_v2


def test_synthetic_app_with_accidental_origin_restores_ref_and_updates(
  client, auth, tmp_path, bypass_url_validation,
):
  """An older synthetic-history app may have picked up an origin remote later.

  If a failed cloned-update attempt also moved its installer-owned upstream ref
  onto that unrelated origin history, the next update must trust the DB-recorded
  upstream commit, restore the ref, and fall back to the manifest-fetched source
  path instead of crashing with "refusing to merge unrelated histories".
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
  responses_v1 = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, index_v1.encode()),
    base + "cards.js": (200, cards_v1.encode()),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v1),
  ):
    r1 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base + "mobius.json",
    })
  assert r1.status_code == 201, r1.text

  src = Path(get_settings().data_dir) / "apps" / "synthetic-origin"
  from app.database import SessionLocal
  from app.models import App
  db = SessionLocal()
  try:
    app = db.query(App).filter(App.slug == "synthetic-origin").first()
    db_upstream = app.upstream_commit
  finally:
    db.close()
  assert db_upstream == app_git.head_sha(src, app_git.UPSTREAM_BRANCH)

  # Add a real origin that is unrelated to the synthetic install history, then
  # simulate the exact failed-attempt residue: upstream was moved to origin/main
  # while the DB row still points at the synthetic commit.
  work, bare, real_head = _make_clone_fixture(
    tmp_path,
    "import './cards.js'\nexport default () => <div>REAL_REPO</div>\n",
    "export const card = 'REAL_CARD'\n",
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

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert r2.json()["version"] == "2.0.0"
  assert (src / "index.jsx").read_text() == index_v2
  assert (src / "cards.js").read_text() == cards_v2
  assert "REAL_REPO" not in (src / "index.jsx").read_text()
  new_upstream = app_git.head_sha(src, app_git.UPSTREAM_BRANCH)
  assert new_upstream != real_head
  assert app_git._run(
    src, "merge-base", "--is-ancestor", db_upstream, new_upstream,
    check=False,
  ).returncode == 0


def test_multifile_install_writes_siblings_and_bundles(
  client, auth, bypass_url_validation,
):
  """A fresh multi-file install writes index.jsx + the declared sibling to
  the source dir and the compiled bundle inlines the sibling's export — proof
  esbuild resolved the `./cards.js` import from the on-disk source tree."""
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

  bundle = data_dir / "compiled" / f"app-{app_id}.js"
  assert bundle.exists()
  bundle_text = bundle.read_text()
  assert len(bundle_text) > 0
  # The sibling was bundled in, not left as an unresolved import.
  assert "CARDS_ORIGINAL" in bundle_text
  assert "./cards.js" not in bundle_text


# index.jsx imports a sibling the manifest's source_files never declares. The
# synthetic-fetch path won't fetch it, so the install ships a tree that can't
# resolve the import — the exact shape the Editor launch bug had. The
# source-completeness check must reject it BEFORE esbuild, with its own 422.
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
  """A manifest whose entry imports an undeclared sibling is rejected with a
  422 that names the completeness gap, and the source dir is not left behind
  (the outer HTTPException handler rolls the writes back)."""
  base = "https://multi-incomplete.test/repo/"
  r = _install_multi(
    client, auth, base, MANIFEST_MULTI_INCOMPLETE,
    JSX_IMPORTS_UNDECLARED, CARDS_V1,
  )
  assert r.status_code == 422, r.text
  detail = r.json()["detail"]
  assert "source_files" in detail
  assert "extra.js" in detail
  data_dir = Path(get_settings().data_dir)
  assert not (data_dir / "apps" / "multi-incomplete").exists()


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
  bundle = (data_dir / "compiled" / f"app-{app_id}.js").read_text()
  assert "CARDS_V2" in bundle


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
  bundle = data_dir / "compiled" / f"app-{app_id}.js"
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


def _update_check(client, headers, base, app_id, manifest, jsx, sources=None, job=b""):
  responses = _check_responses(base, manifest, jsx, sources=sources, job=job)
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses),
  ):
    return client.get(f"/api/apps/{app_id}/update-check", headers=headers)


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
  assert payload["upstream_version"] == "1.0.0"
  assert payload["local_version"] == "1.0.0"
  assert payload["checked_at"]


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
  # Version still flows through so the caller can fall back to comparing it.
  assert payload["local_version"] == "3.0.0"
  assert payload["upstream_version"] is None


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

  # Empty response map → every fetch 404s → fetch_upstream_source raises →
  # the route swallows it and returns unknown rather than erroring.
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client({}),
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

  async def _slow_remote_fetch(_url):
    assert checked_out_connections() <= baseline, (
      "update-check kept its request DB connection checked out while "
      "starting remote work"
    )
    raise HTTPException(status_code=502, detail="synthetic upstream outage")

  with patch("app.install.fetch_upstream_source", new=_slow_remote_fetch):
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
