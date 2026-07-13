import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from app import app_git
from app.config import get_settings


@pytest.fixture(autouse=True)
def _bypass_cron_scaffold():
  with patch("app.install.CRON_SCAFFOLD", Path("/nonexistent/scaffold.sh")):
    yield


@pytest.fixture(autouse=True)
def _stub_resolver_run_chat():
  async def _noop(*args, **kwargs):
    return None

  with patch("app.chat.run_chat", new=_noop):
    yield


@pytest.fixture
def bypass_url_validation():
  with patch(
    "app.install._validate_url_safe",
    lambda url: (url, urlparse(url).netloc, urlparse(url).hostname),
  ):
    yield


def _make_response(status: int, body: bytes, headers: dict | None = None):
  r = MagicMock()
  r.status_code = status
  r.content = body
  r.text = body.decode("utf-8", errors="replace")
  r.headers = headers or {}
  r.json = lambda: json.loads(body.decode("utf-8"))
  return r


class _StreamCtx:
  def __init__(self, status, body, headers=None):
    self._resp = _make_response(status, body, headers)
    self._chunks = [body]

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
  class _FakeClient:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc):
      return False

    def stream(self, method, url, **kwargs):
      if url not in responses:
        return _StreamCtx(404, b"")
      status, body = responses[url][:2]
      headers = responses[url][2] if len(responses[url]) > 2 else None
      return _StreamCtx(status, body, headers=headers)

  return lambda *a, **kw: _FakeClient()


def _manifest(app_id: str, name: str, version: str, source_files=None):
  manifest = {
    "id": app_id,
    "name": name,
    "version": version,
    "description": "Bug 2 regression app",
    "entry": "index.jsx",
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  if source_files is not None:
    manifest["source_files"] = source_files
  return manifest


def _install(
  client,
  auth,
  base: str,
  manifest: dict,
  files: dict[str, str],
  *,
  repo_ref: tuple[str, str] | None = None,
):
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
  }
  for rel, content in files.items():
    responses[base + rel] = (200, content.encode())

  patches = [
    patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    )
  ]
  if repo_ref is not None:
    patches.append(patch("app.install._derive_repo_ref", return_value=repo_ref))

  with patches[0]:
    if len(patches) == 1:
      return client.post(
        "/api/apps/install",
        headers=auth,
        json={"manifest_url": base + "mobius.json"},
      )
    with patches[1]:
      return client.post(
        "/api/apps/install",
        headers=auth,
        json={"manifest_url": base + "mobius.json"},
      )


def _data_dir() -> Path:
  return Path(get_settings().data_dir)


def _source_dir(slug: str) -> Path:
  return _data_dir() / "apps" / slug


def _bundle(app_id: int) -> Path:
  return _data_dir() / "compiled" / f"app-{app_id}.js"


def _tracked_files(repo: Path) -> list[str]:
  return app_git._run(repo, "ls-files").stdout.splitlines()


def _assert_no_drop_backup_leak(repo: Path):
  tracked = _tracked_files(repo)
  assert not [p for p in tracked if p.endswith(".mobius-drop-bak")], tracked
  assert list(repo.rglob("*.mobius-drop-bak")) == []


def _write_worktree(repo: Path, files: dict[str, str]):
  for child in repo.iterdir():
    if child.name == ".git":
      continue
    if child.is_dir():
      shutil.rmtree(child)
    else:
      child.unlink()
  for rel, content in files.items():
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture_commit(repo: Path, msg: str) -> str:
  subprocess.run(
    [
      "git",
      "-c", "user.name=Test",
      "-c", "user.email=test@example.invalid",
      "-C", str(repo),
      "add", "-A",
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
    capture_output=True,
    text=True,
    check=True,
    env=app_git._git_env(repo),
  ).stdout.strip()


def _make_origin(tmp_path: Path, files: dict[str, str]):
  work = tmp_path / "origin-work"
  bare = tmp_path / "origin.git"
  subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
  _write_worktree(work, files)
  _fixture_commit(work, "v1")
  subprocess.run(
    ["git", "clone", "-q", "--bare", str(work), str(bare)],
    check=True,
    env=app_git._git_env(work),
  )
  return work, bare


def _push_origin(work: Path, bare: Path, files: dict[str, str]):
  _write_worktree(work, files)
  _fixture_commit(work, "update")
  subprocess.run(
    ["git", "-C", str(work), "push", "-q", str(bare), "main"],
    check=True,
    env=app_git._git_env(work),
  )


def test_underdeclared_update_preserves_still_imported_sibling(
  client, auth, tmp_path, bypass_url_validation,
):
  base = "https://bug2.test/underdeclared/"
  manifest_v1 = _manifest(
    "bug2-underdeclared",
    "Bug2 Underdeclared",
    "1.0.0",
    source_files=["helper.js"],
  )
  index = (
    "import { HELPER_LABEL } from './helper.js'\n"
    "export default function App() {\n"
    "  return <div>{HELPER_LABEL}</div>\n"
    "}\n"
  )
  helper = "export const HELPER_LABEL = 'HELPER_FROM_V1'\n"
  work, bare = _make_origin(tmp_path, {"index.jsx": index, "helper.js": helper})
  repo_ref = (bare.as_uri(), "main")

  r1 = _install(
    client, auth, base, manifest_v1,
    {"index.jsx": index, "helper.js": helper},
    repo_ref=repo_ref,
  )
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]
  src = _source_dir("bug2-underdeclared")
  assert (src / "helper.js").read_text() == helper

  index_v2 = index.replace(
    "return <div>{HELPER_LABEL}</div>",
    "return <div data-version=\"2\">{HELPER_LABEL}</div>",
  )
  _push_origin(work, bare, {"index.jsx": index_v2, "helper.js": helper})

  # v2 is under-declared: the upstream git tree still carries helper.js and the
  # entry still imports it, but the manifest no longer lists that sibling in
  # source_files and HTTP only returns index.jsx.
  manifest_v2 = _manifest(
    "bug2-underdeclared",
    "Bug2 Underdeclared",
    "2.0.0",
  )
  r2 = _install(
    client, auth, base, manifest_v2, {"index.jsx": index_v2},
    repo_ref=repo_ref,
  )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert (src / "helper.js").read_text() == helper
  assert (src / "index.jsx").read_text() == index_v2
  bundle = _bundle(app_id).read_text()
  assert "HELPER_FROM_V1" in bundle
  assert "./helper.js" not in bundle
  _assert_no_drop_backup_leak(src)


def test_missing_import_compile_failure_returns_friendly_422(
  client, auth, bypass_url_validation,
):
  base = "https://bug2.test/compile-failure/"
  manifest_v1 = _manifest("bug2-compile", "Bug2 Compile", "1.0.0")
  good_index = "export default function App() { return <div>ok</div> }\n"
  r1 = _install(client, auth, base, manifest_v1, {"index.jsx": good_index})
  assert r1.status_code == 201, r1.text

  bad_index = (
    "import { MISSING } from './missing.js'\n"
    "export default function App() {\n"
    "  return <div>{MISSING}</div>\n"
    "}\n"
  )
  r2 = _install(
    client, auth, base,
    _manifest("bug2-compile", "Bug2 Compile", "2.0.0"),
    {"index.jsx": bad_index},
  )

  # The source-completeness check now intercepts this synthetic-fetch case
  # BEFORE esbuild — the entry imports a sibling that is neither declared in
  # source_files nor fetched, so the install would ship an incomplete tree.
  # The 422 stays friendly (names the app + the file, no raw compiler/ANSI
  # noise) and is more specific than the old "Could not resolve".
  assert r2.status_code == 422, r2.text
  detail = r2.json()["detail"]
  assert "Bug2 Compile" in detail
  assert "source_files" in detail
  assert "missing.js" in detail
  assert "CompileError" not in detail
  assert "Traceback" not in detail
  assert "\x1b[" not in detail
  assert "\\x1b[" not in detail


def test_genuinely_removed_upstream_source_file_is_pruned(
  client, auth, tmp_path, bypass_url_validation,
):
  base = "https://bug2.test/genuine-drop/"
  index = (
    "import { HELPER_LABEL } from './helper.js'\n"
    "export default function App() {\n"
    "  return <div>{HELPER_LABEL}</div>\n"
    "}\n"
  )
  helper_v1 = "export const HELPER_LABEL = 'HELPER_V1'\n"
  dropped_v1 = "export const DROPPED = 'DROP_ME'\n"
  work, bare = _make_origin(
    tmp_path,
    {
      "index.jsx": index,
      "helper.js": helper_v1,
      "dropped.js": dropped_v1,
    },
  )
  repo_ref = (bare.as_uri(), "main")
  manifest_v1 = _manifest(
    "bug2-genuine-drop",
    "Bug2 Genuine Drop",
    "1.0.0",
    source_files=["helper.js", "dropped.js"],
  )

  r1 = _install(
    client, auth, base, manifest_v1,
    {
      "index.jsx": index,
      "helper.js": helper_v1,
      "dropped.js": dropped_v1,
    },
    repo_ref=repo_ref,
  )
  assert r1.status_code == 201, r1.text
  app_id = r1.json()["id"]
  src = _source_dir("bug2-genuine-drop")
  assert (src / "helper.js").exists()
  assert (src / "dropped.js").exists()

  helper_v2 = "export const HELPER_LABEL = 'HELPER_V2'\n"
  _push_origin(work, bare, {"index.jsx": index, "helper.js": helper_v2})
  manifest_v2 = _manifest(
    "bug2-genuine-drop",
    "Bug2 Genuine Drop",
    "2.0.0",
    source_files=["helper.js"],
  )
  r2 = _install(
    client, auth, base, manifest_v2,
    {"index.jsx": index, "helper.js": helper_v2},
    repo_ref=repo_ref,
  )

  assert r2.status_code == 201, r2.text
  assert r2.json()["mode"] == "update"
  assert (src / "helper.js").read_text() == helper_v2
  assert not (src / "dropped.js").exists()
  assert "dropped.js" not in _tracked_files(src)
  bundle = _bundle(app_id).read_text()
  assert "HELPER_V2" in bundle
  assert "DROP_ME" not in bundle
  _assert_no_drop_backup_leak(src)
