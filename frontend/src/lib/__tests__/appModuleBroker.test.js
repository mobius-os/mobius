import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from 'acorn'

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

function dynamicImportTargetsFromHtml(html) {
  const targets = []
  for (const match of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/gi)) {
    const [, attributes, source] = match
    const sourceType = /\btype=["']module["']/i.test(attributes)
      ? 'module'
      : 'script'
    const ast = parse(source, { ecmaVersion: 'latest', sourceType })
    const visit = node => {
      if (!node || typeof node !== 'object') return
      if (node.type === 'ImportExpression') {
        targets.push(source.slice(node.source.start, node.source.end).replace(/\s+/g, ' '))
      }
      for (const value of Object.values(node)) {
        if (Array.isArray(value)) value.forEach(visit)
        else if (value && typeof value === 'object') visit(value)
      }
    }
    visit(ast)
  }
  return targets
}

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
  // The opaque frame has one module-execution route: the bytes fetched by the
  // exact parent broker are evaluated from its bounded blob. Pin the complete
  // dynamic-import target list rather than one old helper spelling — a renamed
  // direct URL fallback must not silently bypass attribution/version/offline
  // behavior or put the app bearer in a module URL.
  const dynamicImportTargets = dynamicImportTargetsFromHtml(frame)
  assert.deepEqual(dynamicImportTargets, ['blobUrl'])
  assert.doesNotMatch(frame, /\bimportDirectModule\b/)
  assert.doesNotMatch(frame, /type="importmap"/)
  assert.doesNotMatch(frame, /await import\(['"]react['"]\)/)
  assert.doesNotMatch(frame, /await import\(['"]\/mobius-runtime\.js['"]\)/)
  assert.match(frame, /globalThis\.__mobiusRuntimeConfig/)
  assert.match(frame, /compiledRuntime\.abi !== COMPILED_RUNTIME_ABI/)
})

test('dynamic import inventory includes multiline targets', () => {
  const fixture = `<script type="module">await import(\n  moduleUrl(0)\n)</script>`
  assert.deepEqual(dynamicImportTargetsFromHtml(fixture), ['moduleUrl(0)'])
})

test('the frame separates parent liveness from module transfer time', () => {
  // A single deadline over both charged a slow multi-megabyte download with
  // "the parent shell did not answer", breaking cold-cache opens on phone
  // networks. The ack must gate the switch between the two budgets.
  assert.match(frame, /const MODULE_ACK_TIMEOUT_MS = \d+/)
  assert.match(frame, /const MODULE_TRANSFER_TIMEOUT_MS = \d+/)
  const ack = Number(frame.match(/const MODULE_ACK_TIMEOUT_MS = (\d+)/)[1])
  const transfer = Number(frame.match(/const MODULE_TRANSFER_TIMEOUT_MS = (\d+)/)[1])
  assert.ok(transfer > ack, 'the transfer budget must outlast the liveness probe')
  // The liveness message keeps its wording; the transfer failure must NOT
  // blame the parent, which by then has demonstrably taken the request.
  assert.match(frame, /did not answer the module request/)
  assert.match(frame, /The app module download timed out/)
  // Both stay code:'network' so loadModule's single retry still covers them.
  assert.doesNotMatch(frame, /error\.code = 'timeout'/)
  assert.match(frame, /type === 'moebius:module-ack'/)

  // The parent must ack synchronously on receipt — before awaiting the fetch,
  // otherwise the frame's liveness deadline still races the download.
  const took = canvas.indexOf("type: 'moebius:module-ack'")
  const fetched = canvas.indexOf('await fetchAppModuleBytes(')
  assert.ok(took >= 0 && fetched > took, 'ack must precede the module fetch')
  const attributed = canvas.indexOf('if (srcVersion == null) return')
  assert.ok(attributed >= 0 && took > attributed, 'ack only after attribution')
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
    'VENDORED_MEMORY_GRAPH',
  ]) {
    assert.doesNotMatch(worker, new RegExp(stale))
  }
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
  assert.match(frameCsp, /allow-popups-to-escape-sandbox/)
  assert.doesNotMatch(frameCsp, /allow-same-origin/)
  assert.match(frameCsp, /script-src[^;]*\bblob:/)
  assert.doesNotMatch(ordinaryCsp, /script-src[^;]*\bblob:/)
})
