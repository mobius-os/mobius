import { execFileSync } from 'node:child_process'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CONTAINER = process.env.MOBIUS_CONTAINER || 'mobius-test'

const WRITE_SOURCE = String.raw`
import json
from pathlib import Path
import sys

payload = json.load(sys.stdin)
root = Path("/data/apps") / payload["slug"]
root.mkdir(parents=True, exist_ok=True)
(root / "index.jsx").write_text(payload["jsx_source"], encoding="utf-8")
(root / "mobius.json").write_text(
    json.dumps(payload["manifest"], separators=(",", ":")),
    encoding="utf-8",
)
for relative, body in payload.get("files", {}).items():
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
`

function manifestFor({
  slug,
  name,
  description = 'Disposable browser-test app.',
  offlineCapable = false,
  capabilities = {},
  manifest = {},
}) {
  return {
    id: slug,
    name,
    version: '0.1.0',
    description,
    entry: 'index.jsx',
    offline_capable: offlineCapable,
    permissions: {},
    capabilities,
    source_files: [],
    ...manifest,
  }
}

export function writeAppSource({
  slug,
  name,
  jsxSource,
  description,
  offlineCapable,
  capabilities,
  manifest,
  files = {},
}) {
  const payload = {
    slug,
    jsx_source: jsxSource,
    manifest: manifestFor({
      slug,
      name,
      description,
      offlineCapable,
      capabilities,
      manifest,
    }),
    files,
  }
  execFileSync(
    'docker',
    [
      'exec', '-i', '--user', 'mobius', CONTAINER,
      'python3', '-c', WRITE_SOURCE,
    ],
    {
      input: JSON.stringify(payload),
      encoding: 'utf8',
      stdio: ['pipe', 'pipe', 'pipe'],
    },
  )
  return `/data/apps/${slug}`
}

export async function applyApp(request, token, options) {
  const sourceDir = writeAppSource(options)
  const { response, body } = await applySource(
    request,
    token,
    sourceDir,
    options.chatId ?? null,
  )
  if (!response.ok()) {
    throw new Error(
      `app apply failed (${response.status()}): ${JSON.stringify(body)}`,
    )
  }
  return { response, mode: body.mode, app: body.app, sourceDir }
}

const WRITE_AND_FORCE_COMMIT_STATIC = String.raw`
import json
import subprocess
import sys
from pathlib import Path

payload = json.load(sys.stdin)
root = Path(payload["source_dir"])
paths = []
for relative, body in payload["files"].items():
    target = root / "static" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    paths.append(str(target.relative_to(root)))

git = ["git", "-c", "user.name=Mobius", "-c", "user.email=mobius@localhost", "-C", str(root)]
subprocess.run([*git, "add", "-f", *paths], check=True)
subprocess.run([*git, "commit", "-m", "test: add static fixture files"], check=True)
`

/**
 * Seed raw files directly under an app's static/ tree and force-commit them.
 *
 * static/* is gitignored by design (app_git.py's managed .gitignore --
 * manifest-installed static assets are install-managed, not edited source),
 * so an ordinary write (even through the owner's ordinary /api/fs/write) is
 * invisible to every normal apply/commit path (snapshot_worktree,
 * commit_local): they all stage via plain `git add -A .`, which never
 * touches a gitignored path. The one real precedent for getting static/
 * content into a published runtime, backend/tests/test_app_assets_cache.py's
 * `_write_static`, force-adds with `git add -f` before committing -- this
 * mirrors that exactly, then the caller's own applySource(...) call
 * republishes the runtime from the now-updated HEAD.
 */
export function writeStaticAppFiles(sourceDir, files) {
  execFileSync(
    'docker',
    [
      'exec', '-i', '--user', 'mobius', CONTAINER,
      'python3', '-c', WRITE_AND_FORCE_COMMIT_STATIC,
    ],
    {
      input: JSON.stringify({ source_dir: sourceDir, files }),
      encoding: 'utf8',
      stdio: ['pipe', 'pipe', 'pipe'],
    },
  )
}

export async function applySource(request, token, sourceDir, chatId = null) {
  const response = await request.post(`${BASE}/api/apps/apply`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { source_dir: sourceDir, chat_id: chatId },
  })
  const body = await response.json().catch(() => null)
  return { response, body }
}
