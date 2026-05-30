"""POST /api/apps/install — atomic install + update + rollback.

We exercise the endpoint against a mocked httpx.AsyncClient (no real
network) so tests run inside the existing pytest container without
external connectivity. The mocked layer returns canned (status, body)
tuples per URL so we can drive the install paths deterministically
and force failure modes.
"""

import io
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _bypass_cron_scaffold():
  """Force every test through the no-scaffold warning branch so the
  install endpoint doesn't shell out to init-cron-scaffold.sh (which
  hardcodes `/data/apps/...` and fails under DATA_DIR=/tmp/testdata)."""
  with patch("app.install.CRON_SCAFFOLD", Path("/nonexistent/scaffold.sh")):
    yield


@pytest.fixture
def bypass_url_validation():
  """Skip the SSRF URL-safety check so mocked-httpx tests using
  hostnames that don't resolve via DNS (`x.test`, etc.) still work.
  Tests that DO want to exercise URL validation request this fixture
  by NOT including it — see test_install_rejects_*."""
  with patch("app.install._validate_url_safe", lambda url: None):
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

    def stream(self, method, url):
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


def test_install_fresh_app_writes_everything(client, auth, tmp_path, bypass_url_validation):
  """Happy path: install creates DB row, compiles JSX, populates
  source_dir, seeds storage, processes icon, returns mode=install."""
  base = "https://raw.githubusercontent.com/x/app-test-news/main/"
  responses = {
    base + "mobius.json": (200, json.dumps(MANIFEST_NEWS).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
    base + "prompt.md": (200, PROMPT.encode()),
    base + "fetch.sh": (200, b"#!/bin/bash\necho hi\n"),
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
  assert payload["slug"] == "test-news"
  app_id = payload["id"]

  data_dir = Path(get_settings().data_dir)
  # source_dir/index.jsx written for the file watcher
  jsx_file = data_dir / "apps" / "test-news" / "index.jsx"
  assert jsx_file.read_text() == JSX
  # storage seeds live at /data/apps/<id>/ (storage API is id-keyed)
  assert (data_dir / "apps" / str(app_id) / "prompt.md").read_text() == PROMPT
  sched = json.loads((data_dir / "apps" / str(app_id) / "schedule.json").read_text())
  assert sched == {"hour": 10, "minute": 0}
  # warning expected: scaffold script isn't on PATH in the test image
  assert any("cron" in w for w in payload["warnings"])


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

    def stream(self, method, url):
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
    def stream(self, method, url):
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
  assert installed["manifest_url"] == base + "mobius.json"

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
  assert first["manifest_url"] == base + "mobius.json"

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
  assert second["manifest_url"] == base + "mobius.json"
