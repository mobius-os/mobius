import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const canvas = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.jsx', import.meta.url),
  'utf8',
)
const canvasCss = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.css', import.meta.url),
  'utf8',
)
const frame = readFileSync(
  new URL('../../../public/app-frame.html', import.meta.url),
  'utf8',
)

test('a direct app intent stays covered until the exact live frame applies it', () => {
  assert.match(canvas, /const pendingIntentRef = useRef\(pendingIntent\)/)
  assert.match(canvas, /if \(srcVersion !== liveVersionRef\.current\) return[\s\S]*msg\.type === 'moebius:app-intent-applied'/)
  assert.match(canvas, /msg\.nonce !== pending\.nonce/)
  assert.match(canvas, /onIntentDelivered\?\.\(appId, pending\)/)
  assert.match(canvas, /\(!swap\.liveLoaded \|\| intentHandoffPending\)/)
  assert.match(canvas, /canvas--intent-pending/)
  assert.match(canvasCss, /\.canvas--intent-pending\s*\{[\s\S]*pointer-events:\s*none/)
  assert.match(canvasCss, /\.canvas-loading\s*\{[\s\S]*animation:\s*canvas-loading-in 80ms 120ms ease-out both/)
  assert.match(canvasCss, /\.canvas-loading--intent-handoff\s*\{[\s\S]*animation:\s*none/)
})

test('posting an intent no longer uncovers the app before its acknowledgement', () => {
  const delivery = canvas.slice(
    canvas.indexOf('// One-shot shell intent'),
    canvas.indexOf('// ── P1-A:'),
  )
  assert.match(delivery, /postToFrame\(swap\.liveVersion/)
  assert.doesNotMatch(delivery, /onIntentDelivered/)
})

test('the frame acknowledges after app handlers get two commit turns', () => {
  const acknowledgement = frame.slice(
    frame.indexOf('function acknowledgeAppIntent'),
    frame.indexOf('function requestModuleBytes'),
  )
  assert.match(acknowledgement, /requestAnimationFrame\(\(\) => \{\s*requestAnimationFrame/)
  assert.match(acknowledgement, /setTimeout\(send, 120\)/)
  assert.match(acknowledgement, /type: 'moebius:app-intent-applied'/)
  assert.match(frame, /msg\.type === 'moebius:app-intent'[\s\S]*acknowledgeAppIntent\(msg\.nonce\)/)
})

test('app-block capability is reported only after the module commits and live source is attributed', () => {
  assert.match(frame, /supportsAppBlocks = mod\.appBlockSessions === true/)
  assert.match(frame, /function signalFrameMounted\(node\)[\s\S]*type: 'moebius:frame-mounted', appId: _FRAME_APP_ID, supportsAppBlocks/)
  assert.match(canvas, /const srcVersion = attributedFrameVersion\(framesRef\.current, e\.source\)[\s\S]*if \(srcVersion == null\) return/)
  assert.match(canvas, /msg\.type === 'moebius:frame-mounted'[\s\S]*doc\.supported = msg\.supportsAppBlocks === true/)
  assert.match(canvas, /msg\.type === 'moebius:app-block-state'[\s\S]*srcVersion === liveVersionRef\.current && blockSessionRef\.current\?\.sessionId === msg\.sessionId/)
  const blockDelivery = canvas.slice(canvas.indexOf('Inline transcript sessions'), canvas.indexOf('// ── P1-A:'))
  assert.match(blockDelivery, /if \(!blockSession \|\| !swap\.liveLoaded\) return/)
  assert.match(blockDelivery, /type: 'moebius:app-block-init'/)
  assert.match(blockDelivery, /sentBlockEventRef\.current === blockEvent\.nonce/)
})

test('actual frame init and subsequent block init carry the checkpoint envelope', () => {
  const checkpoint = { id: 'attempt-1', data: 'opaque' }
  const blockSession = { sessionId: 'block', actions: [], checkpoint, retain: true,
    recoveryError: 'Open the app to check the result.' }
  const messages = []
  const scope = {
    loadedDocsRef: { current: new Set(['v1']) }, token: 'token',
    framesRef: { current: new Map([['v1', { contentWindow: { postMessage(message) { messages.push(message) } } }]]) },
    getEffectiveTheme() { return { css: '', bg: '#fff' } }, theme: null,
    readAppFrameStorage() { return {} }, appId: 'app', appSlug: 'app', capabilityContract: null,
    blockSessionRef: { current: blockSession }, swap: { liveVersion: 'v1', liveLoaded: true },
    blockSession, postToFrame(_version, message) { messages.push(message) }, useEffect(fn) { fn() },
  }
  const init = canvas.slice(canvas.indexOf('  function sendInit(v) {'), canvas.indexOf('  // Keep the swap state machine'))
  new Function('scope', `with(scope) { ${init}; sendInit('v1') }`)(scope)
  const delivery = canvas.slice(canvas.indexOf('  // Inline transcript sessions'),
    canvas.indexOf('  useEffect(() => {\n    if (!blockSession || !blockEvent'))
  new Function('scope', `with(scope) { ${delivery} }`)(scope)
  assert.equal(messages[0].type, 'moebius:frame-init')
  assert.deepEqual(messages[0].blockSession, blockSession)
  assert.deepEqual(messages[1], { type: 'moebius:app-block-init', sessionId: 'block',
    actions: [], initialAction: null, checkpoint, retain: true,
    recoveryError: 'Open the app to check the result.' })
  assert.match(frame, /currentBlockSession = msg\.blockSession \|\| null/)
  assert.match(frame, /blockSession: currentBlockSession/)
})

// Native React ordering is covered by scripts/test-inline-document-reload-browser.mjs.
// This narrower check executes the same onLoad/init path for its document scope.
function reloadCanvasLifecycle() {
  const posts = [], resets = [], flushes = []
  const session = { sessionId: 'block', actions: [], retain: true, checkpoint: null }
  const scope = {
    loadedDocsRef: { current: new Set() }, framesRef: { current: new Map([
      ['live', { contentWindow: { postMessage(message) { posts.push(message) } } }],
      ['next', { contentWindow: { postMessage(message) { posts.push(message) } } }],
    ]) },
    liveVersionRef: { current: 'live' }, blockDocumentsRef: { current: new Map() },
    reportedBlockDocumentRef: { current: null }, blockSessionRef: { current: session },
    accountLinkRef: { current: null }, capabilityHostRef: { current: { detachSource() {} } },
    storageHostRef: { current: { detachSource() {} } }, frameVisibleRef: { current: false },
    interactiveRef: { current: false }, token: 'fixture', appId: 'app', appSlug: 'app', theme: null,
    capabilityContract: null, getEffectiveTheme() { return { css: '', bg: '#fff' } },
    readAppFrameStorage() { return {} }, dispatchSwap() {}, retireFrameMediaSession() {},
    clearAccountLinkRegistration() {}, sendOnlineStatus() {}, sendInsets() {},
    sendImmersiveState() {}, sendShellShortcuts() {}, sendVisibility() {}, sendInteractivity() {},
    onBlockCapabilityRef: { current(supported, metadata) {
      resets.push({ supported, ...metadata })
      scope.queued = () => { scope.blockSessionRef.current = { ...session, retain: false } }
    } },
    flushSync(callback) {
      flushes.push(posts.length)
      callback()
      scope.queued?.()
      scope.queued = null
    },
  }
  const init = canvas.slice(canvas.indexOf('  function sendInit(v) {'), canvas.indexOf('  // Keep the swap state machine'))
  const load = canvas.slice(canvas.indexOf('  function handleFrameLoad(v) {'), canvas.indexOf('  // The frames to render:'))
  const onLoad = new Function('scope', `with(scope) { ${init}; ${load}; return handleFrameLoad }`)(scope)
  return { scope, posts, resets, flushes, onLoad }
}

test('actual live document reload commits the owner reset before its first init', () => {
  const host = reloadCanvasLifecycle()
  host.onLoad('live')
  host.scope.reportedBlockDocumentRef.current = host.scope.blockDocumentsRef.current.get('live')
  host.onLoad('live')
  assert.deepEqual(host.flushes, [1], 'the reload boundary flushes before any replacement init')
  assert.deepEqual(host.resets, [{ supported: null, version: 'live', reset: true }])
  assert.equal(host.posts[1].type, 'moebius:frame-init')
  assert.equal(host.posts[1].blockSession.retain, false)
  assert.equal(host.scope.reportedBlockDocumentRef.current, null)
  assert.equal(host.scope.blockDocumentsRef.current.size, 1, 'reload replaces its prior document entry')
})

test('initial load and hidden successor loads cannot reset the live document owner', () => {
  const host = reloadCanvasLifecycle()
  host.onLoad('live')
  host.scope.reportedBlockDocumentRef.current = host.scope.blockDocumentsRef.current.get('live')
  host.onLoad('next')
  host.onLoad('next')
  assert.deepEqual(host.flushes, [])
  assert.deepEqual(host.resets, [])
  assert.ok(host.posts.every(message => message.blockSession.retain))
  assert.equal(host.scope.blockDocumentsRef.current.size, 2, 'only the live and buffered frame have entries')
})

test('document reset captures an unacknowledged Confirm before deferred state evaluation clears the event ref', async () => {
  const { inlineBlockDocumentReset } = await import('../../components/ChatView/markdown/appBlock.js')
  const block = readFileSync(new URL('../../components/ChatView/markdown/AppBlock.jsx', import.meta.url), 'utf8')
  const start = block.indexOf('  const onBlockCapability = useCallback(')
  const callback = block.slice(start, block.indexOf('  // The open view', start))
  let update
  const scope = {
    blockEventRef: { current: { event: 'confirm', nonce: 'confirm:unacknowledged' } },
    inlineBlockDocumentReset, isSession: true, allowedKeys: new Set(),
    dispatchBlockEvent() {}, observeBlockCapability() {}, useCallback(fn) { return fn },
    setSessionState(updater) { update = updater },
    setBlockEvent() { scope.blockEventRef.current = null },
  }
  const reset = new Function('scope', `with(scope) { ${callback}; return onBlockCapability }`)(scope)
  reset(null, { reset: true })
  const state = update({ retain: true, actions: [{ confirming: true }], ackNonce: 'activate:previous' })
  assert.equal(state.retain, true, 'deferred evaluation cannot mistake an unacknowledged Confirm for idle UI')
  assert.equal(state.recoveryPending, true)
  assert.ok(state.recoveryError, 'without a checkpoint the operation remains failclosed')
  assert.equal(state.actions[0].confirming, false)
})


// Execute the actual frame entry functions with isolated, mocked transports.
function frameFunction(name, next) {
  const begin = frame.indexOf(`function ${name}(`)
  assert.ok(begin >= 0)
  return "async " + frame.slice(begin, frame.indexOf(next, begin)).trim()
}

const loadSource = frameFunction('loadModule', "window.addEventListener('message', (e) =>")
const importSource = frameFunction('importBrokeredModule', 'function isBlobModuleLoadFailure')

function passiveLoad({ admitted, supported = false, importError = null }) {
  const observed = { imports: 0, roots: 0, renders: 0, reports: [], errors: [] }
  const mod = { default() { observed.renders++ }, appBlockSessions: supported }
  const config = {
    observed, _FRAME_PASSIVE_BLOCK_DIGEST: admitted ? 'a'.repeat(64) : null,
    currentBlockSession: { sessionId: 'passive' }, currentToken: 'test-token',
    _FRAME_APP_ID: '42', currentCapabilityContract: null, supportsAppBlocks: false,
    globalThis: { __mobiusCompiledRuntime: {
      abi: 1, createRoot() { observed.roots++ }, createElement() {}, Fragment: {} } },
    COMPILED_RUNTIME_ABI: 1, document: { getElementById() { return {} } },
    window: { __frameMounted: false },
    async importBrokeredModule() { observed.imports++; if (importError) throw importError; return mod },
    tokenAppInstanceId() { return 'fixture' }, runtimeToken() {},
    signalFrameMounted() { observed.reports.push(this.supportsAppBlocks) },
    showErr(...args) { observed.errors.push(args) },
    async settleOwnedFontsBeforeMount() {},
    renderMountedComponent() { observed.renders++ },
    renegotiateExpiredModuleToken() { return false }, isBlobModuleLoadFailure() { return false },
  }
  return new Function('scope', `with(scope) { ${loadSource}; return loadModule(); }`)(config)
    .then(() => observed)
}

test('unknown passive app cannot execute module startup or ordinary default', async () => {
  const result = await passiveLoad({ admitted: false })
  assert.equal(result.imports, 0)
  assert.equal(result.roots, 0)
  assert.equal(result.renders, 0)
})

test('admitted but unsupported passive module never mounts its ordinary default', async () => {
  const result = await passiveLoad({ admitted: true })
  assert.equal(result.imports, 1)
  assert.equal(result.roots, 0)
  assert.equal(result.renders, 0)
  assert.equal(result.reports.length, 1)
})

test('opted-in supported passive module still hydrates the session', async () => {
  const result = await passiveLoad({ admitted: true, supported: true })
  assert.equal(result.imports, 1)
  assert.equal(result.roots, 1)
  assert.equal(result.renders, 1)
  assert.deepEqual(result.errors, [])
})

test('cached or racing module bytes cannot run startup under another passive admission', async () => {
  const { webcrypto } = await import('node:crypto')
  const bytes = new TextEncoder().encode('globalThis.__passiveStartupCount++; export default 1;')
  const digest = Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex')
  const execute = new Function('scope', `with(scope) { ${importSource}; return importBrokeredModule(0); }`)
  const scope = {
    currentBlockSession: { sessionId: 'test' }, _FRAME_PASSIVE_BLOCK_DIGEST: '0'.repeat(64),
    crypto: webcrypto, globalThis: { crypto: webcrypto },
    async requestModuleBytes() { return bytes }, Blob,
    URL: { createObjectURL() { return 'data:text/javascript;base64,' + Buffer.from(bytes).toString('base64') }, revokeObjectURL() {} },
  }
  globalThis.__passiveStartupCount = 0
  try {
    await assert.rejects(execute(scope), /revision changed/)
    assert.equal(globalThis.__passiveStartupCount, 0)
    scope._FRAME_PASSIVE_BLOCK_DIGEST = digest
    await execute(scope)
    assert.equal(globalThis.__passiveStartupCount, 1)
  } finally { delete globalThis.__passiveStartupCount }
})

function blockCanvasLifecycle() {
  const reports = [], posts = []
  const liveSource = {}, nextSource = {}
  const scope = {
    swap: null, framesRef: { current: new Map([
      ['old', { contentWindow: liveSource }], ['new', { contentWindow: nextSource }],
    ]) },
    liveVersionRef: { current: 'old' }, blockDocumentsRef: { current: new Map([
      ['old', { supported: null }], ['new', { supported: null }],
    ]) },
    reportedBlockDocumentRef: { current: null },
    onBlockCapabilityRef: { current: (supported, metadata) => reports.push({ supported, ...metadata }) },
    blockSessionRef: { current: { sessionId: 'block', retain: false } },
    blockSession: { sessionId: 'block', retain: false },
    blockDocumentRevision: 0, appId: '42',
    setBlockDocumentRevision() {},
    dispatchSwap(event) { scope.swap = scope.reduceSwap(scope.swap, event) },
    postToFrame(version, message) { posts.push({ version, message }) },
    blockEvent: { sessionId: 'block', nonce: 'confirm:once', action: 'confirm' },
    sentBlockEventRef: { current: null },
  }
  const publish = canvas.slice(canvas.indexOf('function publishBlockCapability('), canvas.indexOf('  const storageHost ='))
  scope.publishBlockCapability = new Function('scope', `with(scope) { ${publish}; return publishBlockCapability; }`)(scope)
  const mounted = canvas.slice(canvas.indexOf("if (msg.type === 'moebius:frame-mounted'"), canvas.indexOf('      // frame-error:'))
  const notify = new Function('scope', 'source', 'msg', `with(scope) {
    const srcVersion = attributedFrameVersion(framesRef.current, source);
    if (srcVersion == null) return;
    ${mounted}
  }`)
  const layout = canvas.slice(canvas.indexOf('  useLayoutEffect(() => {\n    const incoming = swap.incomingVersion'), canvas.indexOf('  // Inline transcript sessions'))
  const actionStart = canvas.indexOf('  useEffect(() => {\n    if (!blockSession || !blockEvent')
  const action = canvas.slice(actionStart, canvas.indexOf('\n\n  // ── P1-A:', actionStart))
  scope.useLayoutEffect = fn => fn()
  scope.useEffect = fn => fn()
  const promote = new Function('scope', `with(scope) { ${layout} }`)
  const deliver = new Function('scope', `with(scope) { ${action} }`)
  return {
    scope, reports, posts, liveSource, nextSource,
    mounted(source, supported) { notify(scope, source, { type: 'moebius:frame-mounted', appId: '42', supportsAppBlocks: supported }) },
    commit() { scope.liveVersionRef.current = scope.swap.liveVersion; promote(scope) },
    action() { deliver(scope) },
  }
}

for (const [oldSupport, newSupport] of [[false, true], [true, false]]) {
  test(`promoted exact document reports ${oldSupport} → ${newSupport} and cannot replay Confirm`, async () => {
    const { initSwapState, reduceSwap } = await import('../previewSwapState.js')
    const { attributedFrameVersion } = await import('../../components/AppCanvas/appFrameProtocol.js')
    const host = blockCanvasLifecycle()
    Object.assign(host.scope, { reduceSwap, attributedFrameVersion, swap: initSwapState('old') })
    host.mounted(host.liveSource, oldSupport)
    host.commit()
    host.action()
    host.scope.swap = reduceSwap(host.scope.swap, { type: 'version', version: 'new' })
    host.mounted(host.nextSource, newSupport)
    assert.deepEqual(host.reports.map(report => report.supported), [oldSupport])
    host.commit()
    assert.deepEqual(host.reports.map(report => report.supported), [oldSupport, newSupport])
    assert.equal(host.reports[1].version, 'new')
    assert.equal(host.reports[1].reset, true)
    host.action()
    assert.equal(host.posts.length, 1, 'old nonce is never re-delivered on promotion')
    host.scope.framesRef.current.delete('old')
    host.mounted(host.liveSource, oldSupport)
    assert.equal(host.reports.length, 2, 'removed old source cannot alter negotiation')
  })
}

test('active or uncertain publication pins its document until retention clears', async () => {
  const { initSwapState, reduceSwap } = await import('../previewSwapState.js')
  const { attributedFrameVersion } = await import('../../components/AppCanvas/appFrameProtocol.js')
  const host = blockCanvasLifecycle()
  Object.assign(host.scope, { reduceSwap, attributedFrameVersion, swap: initSwapState('old') })
  host.mounted(host.liveSource, true)
  host.commit()
  host.scope.swap = reduceSwap(host.scope.swap, { type: 'version', version: 'new' })
  host.scope.blockSession.retain = host.scope.blockSessionRef.current.retain = true
  host.mounted(host.nextSource, false)
  host.commit()
  assert.equal(host.scope.swap.liveVersion, 'old')
  assert.equal(host.scope.swap.incomingVersion, 'new')
  assert.equal(host.reports.length, 1)
  host.scope.blockSession.retain = host.scope.blockSessionRef.current.retain = false
  host.commit()
  host.commit()
  assert.equal(host.scope.swap.liveVersion, 'new')
  assert.deepEqual(host.reports.map(report => report.supported), [true, false])
  assert.equal(host.posts.length, 0)
})


test('late old-frame session state cannot overwrite the promoted document view', async () => {
  const { attributedFrameVersion } = await import('../../components/AppCanvas/appFrameProtocol.js')
  const current = {}, old = {}, states = []
  const branch = canvas.slice(canvas.indexOf("if (msg.type === 'moebius:app-block-state'"), canvas.indexOf("      if (msg.type === 'moebius:module-request'"))
  const scope = { liveVersionRef: { current: 'new' }, blockSessionRef: { current: { sessionId: 'block' } },
    onBlockStateRef: { current: message => states.push(message) } }
  const receive = new Function('scope', 'srcVersion', 'msg', `with(scope) { ${branch} }`)
  const frames = new Map([['old', { contentWindow: old }], ['new', { contentWindow: current }]])
  receive(scope, attributedFrameVersion(frames, old), { type: 'moebius:app-block-state', sessionId: 'block', summary: 'stale' })
  receive(scope, attributedFrameVersion(frames, current), { type: 'moebius:app-block-state', sessionId: 'wrong', summary: 'wrong session' })
  receive(scope, attributedFrameVersion(frames, current), { type: 'moebius:app-block-state', sessionId: 'block', summary: 'current' })
  assert.deepEqual(states.map(state => state.summary), ['current'])
})


test('passive module drift settles as unsupported without an ordinary mount', async () => {
  const error = Object.assign(new Error('drift'), { code: 'passive-not-admitted' })
  const result = await passiveLoad({ admitted: true, importError: error })
  assert.equal(result.roots, 0)
  assert.equal(result.renders, 0)
  assert.deepEqual(result.reports, [false])
  assert.deepEqual(result.errors, [])
})
