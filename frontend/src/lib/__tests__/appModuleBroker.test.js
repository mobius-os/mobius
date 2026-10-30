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

test('shared module reads cannot reuse an owner or prior guest SW cache key', async () => {
  const previous = globalThis.location
  globalThis.location = { pathname: '/shell/shared', origin: 'https://mobius.test' }
  try {
    const { appCodeCacheKey } = await import('../../sw-cache-policy.js')
    const baseUrl = 'https://mobius.test/api/apps/66/module'
    const first = appModuleRequestUrl(baseUrl, { token: 'guest-token', frameVersion: 'v1' })
    const second = appModuleRequestUrl(baseUrl, { token: 'guest-token', frameVersion: 'v1' })
    const owner = `${baseUrl}?token=owner-token&v=v1`
    assert.notEqual(appCodeCacheKey(first), appCodeCacheKey(second))
    assert.notEqual(appCodeCacheKey(first), appCodeCacheKey(owner))
  } finally {
    globalThis.location = previous
  }
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

test('opaque frames execute only bytes returned by the parent module broker', () => {
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

// Passive transcript block sessions: execute the frame runtime's own function
// declarations (found by name in the parsed module script) with mocked I/O.
const runtimeScript = [...frame.matchAll(/<script type="module">([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).find(source => source.includes('function loadModule('))
const runtimeAst = parse(runtimeScript, { ecmaVersion: 'latest', sourceType: 'module' })
function runtimeFunctions(...names) {
  return names.map(name => {
    const node = runtimeAst.body.find(item => item.type === 'FunctionDeclaration' && item.id.name === name)
    assert.ok(node, `${name} is a top-level frame function`)
    return runtimeScript.slice(node.start, node.end)
  }).join('\n')
}
function frameScope(names, scope, result) {
  return new Function('scope', `with (scope) { ${runtimeFunctions(...names)}\n return ${result} }`)(scope)
}

function blockSessionFrame({ supportsAppBlocks = true, session = { sessionId: 's', generation: 3, actions: [{ key: 'run:1' }] } } = {}) {
  const parent = { posted: [], postMessage(message, origin) { this.posted.push({ message, origin }) } }
  const scope = {
    supportsAppBlocks, currentBlockSession: null, blockSessionListeners: new Set(), console,
    window: { parent, location: { origin: 'https://mobius.test' } },
  }
  const { api, deliver } = frameScope(['blockSessionApi', 'deliverBlockSessionMessage'], scope,
    '{ api: blockSessionApi, deliver: deliverBlockSessionMessage }')
  scope.currentBlockSession = api(session)
  const appListener = []
  const send = (msg, source = parent, origin = 'https://mobius.test') => {
    const event = { source, origin, data: msg, stopped: false, stopImmediatePropagation() { this.stopped = true } }
    const handled = deliver(event, msg)
    // A later app listener on window sees the event unless the runtime stopped it.
    if (!event.stopped) appListener.push(msg)
    return { handled, stopped: event.stopped }
  }
  return { api: scope.currentBlockSession, makeApi: api, send, appListener, parent }
}

test('block-session messages reach app listeners only for this exact session, generation and parent', () => {
  const action = { type: 'moebius:app-block-action', sessionId: 's', generation: 3, key: 'run:1', event: 'confirm', nonce: 'n' }
  const off = blockSessionFrame({ supportsAppBlocks: false })
  assert.deepEqual(off.send(action), { handled: true, stopped: true })
  assert.deepEqual(off.appListener, [])

  const on = blockSessionFrame()
  const subscribed = []
  const unsubscribe = on.api.subscribe(msg => subscribed.push(msg))
  for (const foreign of [{ ...action, sessionId: 'other' }, { ...action, generation: 2 }, { ...action, generation: undefined },
    { type: 'moebius:app-block-init', sessionId: 'other', generation: 3, actions: [] }]) {
    assert.deepEqual(on.send(foreign), { handled: true, stopped: true })
  }
  assert.deepEqual(on.send(action, { not: 'the parent' }), { handled: true, stopped: true })
  assert.deepEqual(on.send(action, on.parent, 'https://evil.test'), { handled: true, stopped: true })
  assert.deepEqual(on.send({ type: 'moebius:app-intent', intent: 'x:1' }), { handled: false, stopped: false })
  // The exact message reaches both the subscription API and a direct window
  // listener such as `window.addEventListener('message', …)` in the app.
  assert.deepEqual(on.send(action), { handled: true, stopped: false })
  assert.deepEqual(subscribed, [action])
  assert.deepEqual(on.appListener.filter(msg => msg.type === 'moebius:app-block-action'), [action])
  unsubscribe()
  on.send(action)
  assert.equal(subscribed.length, 1)
})

test('the app block-session handle mirrors the init envelope and stamps its own state', () => {
  const on = blockSessionFrame({ session: { sessionId: 's', generation: 4, actions: [{ key: 'run:1' }],
    checkpoint: { id: 'c1', data: 'opaque' }, retain: true, recoveryError: null } })
  assert.equal(on.api.sessionId, 's')
  assert.equal(on.api.generation, 4)
  assert.deepEqual(on.api.checkpoint, { id: 'c1', data: 'opaque' })
  assert.deepEqual(on.api.items, [], 'no listed items: an empty array')
  assert.deepEqual(on.makeApi({ sessionId: 's', generation: 1, items: [{ intent: 'review:a' }] }).items, [{ intent: 'review:a' }])
  assert.equal(on.api.retain, true)
  assert.ok(Object.isFrozen(on.api))
  on.api.setState({ actions: [{ key: 'run:1', busy: true }], type: 'forged', sessionId: 'other' })
  assert.equal(on.parent.posted[0].message.type, 'moebius:app-block-state')
  assert.equal(on.parent.posted[0].message.sessionId, 's')
  assert.equal(on.parent.posted[0].message.generation, 4, 'the handle stamps its own document generation')
  assert.equal(on.parent.posted[0].origin, 'https://mobius.test')
  assert.equal(on.makeApi({ sessionId: 's' }), null, 'an envelope without a generation is not a session')
})

function renderAppMount({ supportsAppBlocks = true, currentBlockSession = null } = {}) {
  let tree
  const render = frameScope(['renderMountedComponent'], {
    mountedRoot: { render: value => { tree = value } }, mountedComponent: 'App', Fragment: 'Fragment',
    MountSignal: 'MountSignal', _FRAME_APP_ID: 7, currentToken: 'app-session', supportsAppBlocks, currentBlockSession,
    createElement: (type, props, ...children) => ({ type, props, children }),
  }, 'renderMountedComponent')
  render()
  return tree.children[1].props
}

test('only a supporting module receives props.blockSession; ordinary props are unchanged', () => {
  const session = { sessionId: 's' }
  assert.equal(renderAppMount({ currentBlockSession: session }).blockSession, session)
  assert.deepEqual(Object.keys(renderAppMount({ supportsAppBlocks: false, currentBlockSession: session })).sort(), ['appId', 'token'])
})

function passiveLoad({ requested = true, session = { sessionId: 'p', generation: 1 }, admitted, exports = {}, importError = null }) {
  const observed = { imports: 0, roots: 0, renders: 0, mounted: [], errors: [] }
  const scope = {
    passiveRequested: requested, currentBlockSession: session, supportsAppBlocks: false,
    _FRAME_PASSIVE_BLOCK_DIGEST: admitted ? 'a'.repeat(64) : null, _FRAME_APP_ID: '42',
    currentToken: 'token', currentCapabilityContract: null, currentNavLocation: null,
    waitForNavigationReady: false, frameVisible: true, COMPILED_RUNTIME_ABI: 1,
    globalThis: { __mobiusCompiledRuntime: { abi: 1, createRoot() { observed.roots++; return {} }, createElement() {}, Fragment: {} } },
    document: { getElementById() { return {} } }, window: { __frameMounted: false },
    async importBrokeredModule() { observed.imports++; if (importError) throw importError; return { default() {}, ...exports } },
    tokenAppInstanceId() { return 'fixture' }, runtimeToken() {}, handleUncaughtAppError() {},
    signalFrameMounted() { observed.mounted.push(scope.supportsAppBlocks) },
    showErr(...args) { observed.errors.push(args) }, async settleOwnedFontsBeforeMount() {},
    renderMountedComponent() { observed.renders++ },
    renegotiateExpiredModuleToken() { return false }, isBlobModuleLoadFailure() { return false },
  }
  const load = frameScope(['settlePassiveUnsupported', 'loadModule'], scope, 'loadModule')
  return load().then(() => observed)
}

test('a passive document runs app code only for an admitted, supporting module', async () => {
  const unadmitted = await passiveLoad({ admitted: false })
  assert.deepEqual([unadmitted.imports, unadmitted.roots, unadmitted.renders], [0, 0, 0])
  assert.deepEqual(unadmitted.mounted, [false])
  const malformed = await passiveLoad({ admitted: true, session: null })
  assert.deepEqual([malformed.imports, malformed.mounted], [0, [false]], 'a malformed envelope still fails closed')
  const unsupported = await passiveLoad({ admitted: true })
  assert.deepEqual([unsupported.imports, unsupported.roots, unsupported.renders], [1, 0, 0])
  assert.deepEqual(unsupported.mounted, [false])
  const supported = await passiveLoad({ admitted: true, exports: { appBlockSessions: true } })
  assert.deepEqual([supported.roots, supported.renders, supported.errors], [1, 1, []])
  const drift = await passiveLoad({ admitted: true, importError: Object.assign(new Error('drift'), { code: 'passive-not-admitted' }) })
  assert.deepEqual([drift.roots, drift.mounted, drift.errors], [0, [false], []])
  const ordinary = await passiveLoad({ requested: false, session: null, admitted: false })
  assert.deepEqual([ordinary.roots, ordinary.renders], [1, 1], 'ordinary frames are unaffected')
})

test('module bytes must match the admitted digest before any passive evaluation', async () => {
  const { webcrypto } = await import('node:crypto')
  const bytes = new TextEncoder().encode('globalThis.__passiveStartupCount++; export default 1;')
  const digest = Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex')
  const scope = {
    passiveRequested: true, _FRAME_PASSIVE_BLOCK_DIGEST: '0'.repeat(64), crypto: webcrypto,
    globalThis: { crypto: webcrypto }, Blob, async requestModuleBytes() { return bytes },
    URL: { createObjectURL() { return 'data:text/javascript;base64,' + Buffer.from(bytes).toString('base64') }, revokeObjectURL() {} },
  }
  const importModule = frameScope(['passiveNotAdmitted', 'importBrokeredModule'], scope, 'importBrokeredModule')
  globalThis.__passiveStartupCount = 0
  try {
    await assert.rejects(importModule(0), error => error.code === 'passive-not-admitted')
    assert.equal(globalThis.__passiveStartupCount, 0)
    scope._FRAME_PASSIVE_BLOCK_DIGEST = digest
    await importModule(0)
    assert.equal(globalThis.__passiveStartupCount, 1)
  } finally { delete globalThis.__passiveStartupCount }
})
