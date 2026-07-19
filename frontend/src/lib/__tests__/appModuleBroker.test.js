import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  APP_MODULE_MAX_BYTES,
  AppModuleBrokerError,
  appModuleRequestUrl,
  fetchAppModuleBytes,
} from '../appModuleBroker.js'

const frame = readFileSync(
  new URL('../../../public/app-frame.html', import.meta.url),
  'utf8',
)
const canvas = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.jsx', import.meta.url),
  'utf8',
)
const worker = readFileSync(new URL('../../sw.js', import.meta.url), 'utf8')
const caddy = readFileSync(
  new URL('../../../../Caddyfile', import.meta.url),
  'utf8',
)

test('module request keys keep the app version but discard the frame revision', () => {
  const url = new URL(appModuleRequestUrl(
    'https://mobius.test/api/apps/66/module',
    {
      token: 'scoped token',
      frameVersion: '2026-07-18T22:00:00-a1b2c3d4e5f67890',
      retry: 1,
    },
  ))
  assert.equal(url.pathname, '/api/apps/66/module')
  assert.equal(url.searchParams.get('token'), 'scoped token')
  assert.equal(url.searchParams.get('v'), '2026-07-18T22:00:00')
  assert.equal(url.searchParams.get('_'), '1')
})

test('module broker returns bounded bytes from the controlled parent fetch', async () => {
  let seen
  const bytes = await fetchAppModuleBytes({
    baseUrl: 'https://mobius.test/api/apps/7/module',
    token: 'app-token',
    frameVersion: 'v1',
    fetchImpl: async (url, init) => {
      seen = { url: new URL(url), init }
      return new Response('export default function App() {}', {
        headers: { 'Content-Type': 'application/javascript' },
      })
    },
  })
  assert.equal(new TextDecoder().decode(bytes), 'export default function App() {}')
  assert.equal(seen.url.searchParams.get('token'), 'app-token')
  assert.equal(seen.url.searchParams.get('v'), 'v1')
  assert.equal(seen.init.credentials, 'same-origin')
})

test('module broker classifies auth, network, HTTP, and size failures', async () => {
  const load = fetchImpl => fetchAppModuleBytes({
    baseUrl: 'https://mobius.test/api/apps/7/module',
    token: 'app-token',
    frameVersion: 'v1',
    fetchImpl,
  })
  await assert.rejects(
    load(async () => { throw new TypeError('offline') }),
    error => error instanceof AppModuleBrokerError && error.code === 'network',
  )
  await assert.rejects(
    load(async () => new Response('', { status: 401 })),
    error => error.code === 'token-expired' && error.status === 401,
  )
  await assert.rejects(
    load(async () => new Response('', { status: 500 })),
    error => error.code === 'http' && error.status === 500,
  )
  await assert.rejects(
    load(async () => new Response(new Uint8Array(APP_MODULE_MAX_BYTES + 1))),
    error => error.code === 'too-large',
  )
})

test('opaque frames request module bytes only after exact parent attribution', () => {
  const sourceGate = canvas.indexOf('if (srcVersion == null) return')
  const broker = canvas.indexOf("if (msg.type === 'moebius:module-request')")
  assert.ok(sourceGate >= 0 && broker > sourceGate)
  assert.match(canvas, /fetchAppModuleBytes\(/)
  assert.match(frame, /type: 'moebius:module-request'/)
  assert.match(frame, /new Blob\(\[bytes\], \{ type: 'text\/javascript' \}\)/)
  assert.match(frame, /URL\.revokeObjectURL\(blobUrl\)/)
  assert.doesNotMatch(frame, /await import\(moduleUrl\([01]\)\)/)
  assert.doesNotMatch(frame, /type="importmap"/)
  assert.doesNotMatch(frame, /await import\(['"]react['"]\)/)
  assert.doesNotMatch(frame, /await import\(['"]\/mobius-runtime\.js['"]\)/)
  assert.match(frame, /globalThis\.__mobiusRuntimeConfig/)
  assert.match(frame, /compiledRuntime\.abi !== COMPILED_RUNTIME_ABI/)
})

test('mounting an opaque frame explicitly warms its versioned document', () => {
  assert.match(canvas, /requestAppCodeWarm\(\{\s*frameUrl:/)
  assert.match(worker, /if \(!frameUrl && !moduleUrl\) return/)
  assert.match(worker, /url\.origin !== self\.location\.origin/)
  assert.match(worker, /if \(frameUrl\) await warmOne\(frameUrl, OFFLINE_APPS_CACHE\)/)
})

test('bundled app packages are not duplicated in the shell install precache', () => {
  for (const stale of [
    'VENDORED_REACT',
    'VENDORED_CODEMIRROR',
    'VENDORED_RECHARTS',
    'VENDORED_DATE_FNS',
    'VENDORED_ATLAS_NOTES',
  ]) {
    assert.doesNotMatch(worker, new RegExp(stale))
  }
  assert.match(worker, /VENDORED_MEMORY_GRAPH/)
  assert.match(worker, /RETAINED_RUNTIME_ASSETS/)
})

test('only app-frame responses admit brokered blob modules at the edge', () => {
  assert.match(caddy, /@appFrame path \/api\/apps\/\*\/frame/)
  const frameCsp = caddy.split('\n').find(
    line => line.includes('header @appFrame >Content-Security-Policy'),
  ) || ''
  const ordinaryCsp = caddy.split('\n').find(
    line => line.includes('header @notFrameableEmbed >Content-Security-Policy'),
  ) || ''
  assert.match(frameCsp, /sandbox allow-scripts/)
  assert.doesNotMatch(frameCsp, /allow-same-origin/)
  assert.match(frameCsp, /script-src[^;]*\bblob:/)
  assert.doesNotMatch(ordinaryCsp, /script-src[^;]*\bblob:/)
})
