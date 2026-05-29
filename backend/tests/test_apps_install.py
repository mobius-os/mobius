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


JSX = "export default function App() { return <div>ok</div> }"
PROMPT = "# default prompt\nDo the work.\n"


def _make_response(status: int, body: bytes):
  r = MagicMock()
  r.status_code = status
  r.content = body
  r.text = body.decode("utf-8", errors="replace")
  r.json = lambda: json.loads(body.decode("utf-8"))
  return r


def _fake_async_client(responses: dict):
  """`responses` maps URL → (status, bytes). Returns a context-manager
  factory matching `httpx.AsyncClient(...)` usage."""

  class _FakeClient:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc):
      return False

    async def get(self, url):
      if url not in responses:
        return _make_response(404, b"")
      status, body = responses[url]
      return _make_response(status, body)

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


def test_install_fresh_app_writes_everything(client, auth, tmp_path):
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


def test_install_validates_required_fields(client, auth):
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


def test_install_update_path_in_place(client, auth):
  """Second install of the same manifest.id PATCHes the existing app:
  same row, fresh jsx_source, preserved user data in seeds."""
  base = "https://x.test/v1/"
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

  base2 = "https://x.test/v2/"
  jsx_v2 = JSX.replace("ok", "ok v2")
  responses_v2 = {
    base2 + "mobius.json": (200, json.dumps({
      **MANIFEST_NEWS, "version": "1.2.0",
    }).encode()),
    base2 + "index.jsx": (200, jsx_v2.encode()),
    base2 + "icon.png": (200, _png_bytes()),
    base2 + "prompt.md": (200, b"v2 default prompt"),  # should NOT clobber
    base2 + "fetch.sh": (200, b""),
  }
  with patch(
    "app.install.httpx.AsyncClient",
    side_effect=_fake_async_client(responses_v2),
  ):
    r2 = client.post("/api/apps/install", headers=auth, json={
      "manifest_url": base2 + "mobius.json",
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


def test_install_rolls_back_on_compile_failure(client, auth):
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


def test_install_icon_404_is_warning_not_failure(client, auth):
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


def test_install_rejects_slug_with_path_traversal(client, auth):
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
