import { after, test } from 'node:test'
import assert from 'node:assert/strict'

import { api } from '../../api/client.js'

const previousFetch = globalThis.fetch

after(() => {
  globalThis.fetch = previousFetch
})

// A rebuild rewrites the same output path and the server sends no
// Cache-Control, so a heuristically cached read would keep showing the
// previous build in the viewer even after a page reload.
test('artifact output reads revalidate so a rebuilt document is never shown stale', async () => {
  const seen = []
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), init })
    return { ok: true, status: 200, headers: new Headers(), async arrayBuffer() { return new ArrayBuffer(0) } }
  }

  await api.projects.artifactOutput('project-1', 'artifact-1', 'document.pdf')

  assert.equal(seen.length, 1)
  assert.match(seen[0].url, /\/api\/projects\/project-1\/artifacts\/artifact-1\/output\/document\.pdf$/)
  assert.equal(seen[0].init.cache, 'no-cache')
})
