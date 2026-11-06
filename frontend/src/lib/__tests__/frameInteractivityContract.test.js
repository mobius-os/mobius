import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createContext, runInContext, runInNewContext } from 'node:vm'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const src = resolve(here, '../..')
const frame = readFileSync(resolve(src, '../public/app-frame.html'), 'utf8')
const canvas = readFileSync(resolve(src, 'components/AppCanvas/AppCanvas.jsx'), 'utf8')
const shell = readFileSync(resolve(src, 'components/Shell/Shell.jsx'), 'utf8')
const frameCacheModel = readFileSync(
  resolve(src, 'components/Shell/appFrameCache.js'),
  'utf8',
)

test('app content is selectable by default without exposing controls and drag handles to touch selection', () => {
  assert.match(frame, /body\s*\{\s*-webkit-user-select:\s*text;\s*user-select:\s*text;/)
  const controls = frame.match(/button, \[role="button"\][\s\S]*?\{([^}]+)\}/)
  assert.ok(controls)
  assert.match(controls[0], /input\[type="button"\]/)
  assert.match(controls[0], /input\[type="submit"\]/)
  assert.match(controls[0], /input\[type="reset"\]/)
  assert.match(controls[0], /\[draggable="true"\]/)
  assert.match(controls[0], /\[data-split-role="handle"\]/)
  assert.match(controls[1], /-webkit-user-select:\s*none;\s*user-select:\s*none;/)
  assert.match(frame, /input, textarea, \[contenteditable\], \[contenteditable\] \* \{[^}]*user-select:\s*text;/)
})

test('frame suspension reaches the live app before paint', () => {
  assert.match(canvas, /frameVisible = visible/)
  assert.match(canvas, /useEffect\(\(\) => \{[\s\S]*sendVisibility\(swap\.liveVersion, frameVisible\)/)
  assert.match(canvas, /useLayoutEffect\(\(\) => \{[\s\S]*sendInteractivity\(swap\.liveVersion, interactive, frameVisible\)/)
  assert.match(canvas, /suspendScrolling:\s*frameIsVisible\s*&&\s*!enabled/)
  assert.match(canvas, /moebius:frame-interactivity/)
})

test('hidden app-frame history stays device-bounded without limiting open tabs', () => {
  assert.match(frameCacheModel, /const BASE_APP_CACHE_MAX = 6/)
  assert.match(frameCacheModel, /const HIGH_MEMORY_APP_CACHE_MAX = 10/)
  assert.doesNotMatch(shell, /openTabs\.slice\(/)
})

test('iframe history retirement runs at the committed layout boundary, never during render', () => {
  assert.match(
    canvas,
    /useLayoutEffect\(\(\) => \{\s*if \(!appId\) return\s*return \(\) => \{ onNavReset\?\.\(appId\) \}/,
  )
  const cacheDerivation = frameCacheModel.slice(
    frameCacheModel.indexOf('export function deriveRenderedAppIds'),
  )
  assert.ok(cacheDerivation.length > 0)
  assert.doesNotMatch(cacheDerivation, /retireAppHistory/)
})

test('frame suspension cancels compositor momentum without changing the resting offset', () => {
  assert.match(frame, /function cancelScrollerMomentum\(element\)/)
  assert.match(frame, /element\.scrollTop = top < maxTop \? top \+ 1/)
  assert.match(frame, /element\.scrollTop = top;/)
  assert.match(frame, /data-mobius-frame-suspended/)
  assert.match(frame, /suspendedScrollFrame = window\.__mobiusNativeFrame\(holdSuspendedScroll\)/)
})

const SHELL_ORIGIN = 'https://mobius.test'
const searchShortcut = [{ actionId: 'search.open', binding: { key: 'k', mod: true } }]

function appFrameShortcuts() {
  const source = frame.match(/<script data-mobius-shell-shortcuts>([\s\S]*?)<\/script>/)?.[1]
  assert.ok(source, 'app-frame.html has the shell shortcut script')
  const listeners = new Map()
  const shellPosts = []
  const childPosts = []
  // Messages are built in the script's own realm; compare them as plain data.
  const plain = value => JSON.parse(JSON.stringify(value))
  const child = { postMessage(message) { childPosts.push(plain(message)) } }
  const otherChild = { postMessage() {} }
  const parent = { postMessage(message, origin) { shellPosts.push({ message: plain(message), origin }) } }
  const window = {
    parent,
    frames: [child, otherChild],
    location: { origin: SHELL_ORIGIN },
    addEventListener(type, callback) { listeners.set(type, callback) },
  }
  // Focus starts on the child's iframe inside a focused app document.
  const iframeOf = contentWindow => ({ contentWindow })
  let documentFocused = true
  const document = {
    activeElement: iframeOf(child),
    hasFocus() { return documentFocused },
    addEventListener(type, callback, capture) {
      assert.equal(capture, true)
      listeners.set(type, callback)
    },
  }
  runInNewContext(source, { window, document, Array, String, Boolean })
  return {
    child,
    otherChild,
    parent,
    focus(element, focused = true) {
      document.activeElement = element
      documentFocused = focused
    },
    iframeOf,
    shellPosts,
    childPosts,
    message(eventSource, data, origin = 'null') {
      listeners.get('message')({ source: eventSource, origin, data })
    },
    advertise(shortcuts) {
      listeners.get('message')({
        source: parent, origin: SHELL_ORIGIN, data: { type: 'moebius:frame-shortcuts', shortcuts },
      })
    },
    key(overrides = {}) {
      const event = {
        key: 'k', metaKey: true, prevented: false,
        preventDefault() { this.prevented = true },
        stopImmediatePropagation() {},
        ...overrides,
      }
      listeners.get('keydown')(event)
      return event.prevented
    },
  }
}

test('the app frame captures only the advertised shell chords, even in text fields', () => {
  const frameDoc = appFrameShortcuts()
  frameDoc.message(frameDoc.parent, { type: 'moebius:frame-shortcuts', shortcuts: searchShortcut }, 'https://evil.test')
  assert.equal(frameDoc.key(), false, 'a foreign embedder cannot advertise chords')

  frameDoc.advertise(searchShortcut)
  assert.equal(frameDoc.key({ target: { isContentEditable: true } }), true)
  assert.deepEqual(frameDoc.shellPosts.at(-1), {
    message: { type: 'moebius:shell-shortcut', actionId: 'search.open' },
    origin: SHELL_ORIGIN,
  })
  const altGraph = { getModifierState: state => state === 'AltGraph' }
  for (const other of [{ key: 'c' }, { metaKey: false }, { altKey: true }, { isComposing: true }, { repeat: true }, altGraph]) {
    assert.equal(frameDoc.key(other), false)
  }

  frameDoc.advertise([])
  assert.equal(frameDoc.key(), false, 'an opted-out app keeps every key')
})

test('the app frame shares shell chords with its direct child frames and relays their actions', () => {
  const frameDoc = appFrameShortcuts()
  frameDoc.advertise(searchShortcut)
  assert.deepEqual(frameDoc.childPosts.at(-1), { type: 'moebius:frame-shortcuts', shortcuts: searchShortcut })

  frameDoc.message(frameDoc.child, { type: 'moebius:frame-shortcuts-request' })
  assert.equal(frameDoc.childPosts.length, 2, 'a newly loaded child gets the current chords')

  frameDoc.message({ postMessage() { assert.fail('not a child frame') } }, { type: 'moebius:frame-shortcuts-request' })
  frameDoc.message({}, { type: 'moebius:shell-shortcut', actionId: 'search.open' })
  frameDoc.message(frameDoc.child, { type: 'moebius:shell-shortcut', actionId: 'chat.new' })
  assert.equal(frameDoc.shellPosts.length, 0, 'only a child frame, and only advertised actions')

  frameDoc.message(frameDoc.child, { type: 'moebius:shell-shortcut', actionId: 'search.open' })
  assert.deepEqual(frameDoc.shellPosts.at(-1).message, { type: 'moebius:shell-shortcut', actionId: 'search.open' })

  frameDoc.advertise([])
  assert.deepEqual(frameDoc.childPosts.at(-1).shortcuts, [], 'opting out reaches child frames too')
  frameDoc.message(frameDoc.child, { type: 'moebius:shell-shortcut', actionId: 'search.open' })
  assert.equal(frameDoc.shellPosts.length, 1)
})

test('the app frame relays a child frame\'s shell action only while that frame has keyboard focus', () => {
  const frameDoc = appFrameShortcuts()
  frameDoc.advertise(searchShortcut)
  const action = { type: 'moebius:shell-shortcut', actionId: 'search.open' }

  frameDoc.focus(frameDoc.iframeOf(frameDoc.otherChild))
  frameDoc.message(frameDoc.child, action)
  assert.equal(frameDoc.shellPosts.length, 0, 'not while a different child frame is focused')
  // Asking for the chord list stays open to every child frame, focused or not,
  // so a nested frame can capture chords before it ever receives focus.
  const listPosts = frameDoc.childPosts.length
  frameDoc.message(frameDoc.child, { type: 'moebius:frame-shortcuts-request' })
  assert.deepEqual(frameDoc.childPosts.slice(listPosts), [{ type: 'moebius:frame-shortcuts', shortcuts: searchShortcut }],
    'an unfocused child frame still receives the chord list')

  frameDoc.focus({ tagName: 'BODY' })
  frameDoc.message(frameDoc.child, action)
  frameDoc.focus(null)
  frameDoc.message(frameDoc.child, action)
  assert.equal(frameDoc.shellPosts.length, 0, 'not while no child frame is focused')

  frameDoc.focus(frameDoc.iframeOf(frameDoc.child), false)
  frameDoc.message(frameDoc.child, action)
  assert.equal(frameDoc.shellPosts.length, 0, 'not while the app document lacks focus')

  frameDoc.focus(frameDoc.iframeOf(frameDoc.child))
  frameDoc.message(frameDoc.child, action)
  assert.deepEqual(frameDoc.shellPosts.at(-1).message, action, 'relayed from the focused child frame')
})

function bootFrameVisibilityHost() {
  // The real bootstrap listener and runtime config literal from app-frame.html,
  // run without fetching or mounting an app.
  const listener = frame.match(
    /window\.addEventListener\('message', \(e\) => \{\n\s*if \(e\.source !== window\.parent \|\| e\.origin !== window\.location\.origin\) return;[\s\S]*?\n {4}\}\);/,
  )?.[0]
  const config = frame.match(/globalThis\.__mobiusRuntimeConfig = \{[\s\S]*?\n {8}\};/)?.[0]
  assert.ok(listener, 'frame bootstrap message listener exists')
  assert.ok(config, 'frame runtime config literal exists')
  const listeners = []
  const parent = {}
  const window = {
    parent,
    location: { origin: 'https://mobius.test' },
    addEventListener(type, cb) { if (type === 'message') listeners.push(cb) },
  }
  const context = {
    window,
    _FRAME_APP_ID: 7,
    currentToken: 'token',
    currentCapabilityContract: null,
    currentNavLocation: null,
    waitForNavigationReady: false,
    tokenAppInstanceId: () => null,
    runtimeToken: async () => 'token',
  }
  runInNewContext(
    `let frameVisible = null;\n${listener}\nglobalThis.startModuleLoad = () => { ${config} return globalThis.__mobiusRuntimeConfig; };`,
    context,
  )
  return {
    parent,
    startModuleLoad: context.startModuleLoad,
    deliver(data, source = parent) {
      for (const cb of listeners) cb({ data, source, origin: window.location.origin })
    },
  }
}

test('the frame keeps an early visibility verdict for the app runtime', () => {
  const host = bootFrameVisibilityHost()
  // AppCanvas posts this at frame load, before the app runtime listens.
  host.deliver({ type: 'moebius:frame-visibility', visible: false })
  host.deliver({ type: 'moebius:frame-visibility', visible: true }, {})
  host.deliver({ type: 'moebius:frame-visibility', visible: 'true' })
  const config = host.startModuleLoad()
  assert.equal(config.frameVisible, false)
  // A verdict that arrives while the module is still loading also counts,
  // because the runtime reads the config when it initializes.
  host.deliver({ type: 'moebius:frame-visibility', visible: true })
  assert.equal(config.frameVisible, true)
})

// A closed app stays loaded but hidden in the shell's warm-frame cache, and a
// hidden ancestor never changes the frame's native Page Visibility. These
// tests pin that the app frame itself turns the shell's verdict into silence.

// The app frame runs in an opaque-origin sandbox: window.origin reads "null"
// while location.origin is its URL's. A nested srcdoc document reads
// location.origin as "null" too.
function bootFrame({ locationOrigin = SHELL_ORIGIN, origin = 'null' } = {}) {
  const source = frame.match(/<script data-mobius-background-pause>([\s\S]*?)<\/script>/)?.[1]
  assert.ok(source, 'app-frame.html has the background pause script')

  let nativeVisibility = 'visible'
  class FakeDocument {
    get visibilityState() { return nativeVisibility }
    get hidden() { return nativeVisibility === 'hidden' }
  }
  const visibilityEvents = []
  const attached = []
  const captureListeners = new Map()
  const document = new FakeDocument()
  const childFrames = []
  document.currentScript = { textContent: source }
  // The root scroller, recording every overflow write and forced layout.
  const rootWrites = []
  const rootStyle = {
    props: new Map(),
    getPropertyValue(name) { return this.props.get(name)?.[0] ?? '' },
    getPropertyPriority(name) { return this.props.get(name)?.[1] ?? '' },
    setProperty(name, value, priority = '') { this.props.set(name, [value, priority]); rootWrites.push(value) },
    removeProperty(name) { this.props.delete(name); rootWrites.push('') },
  }
  document.scrollingElement = { style: rootStyle, get offsetHeight() { rootWrites.push('layout'); return 0 } }
  document.dispatchEvent = (event) => { visibilityEvents.push(event.type) }
  document.querySelectorAll = (selector) => (selector === 'iframe' ? childFrames : attached)
  document.addEventListener = (type, callback, capture) => {
    assert.equal(capture, true, 'frame playback listener is capture-phase')
    captureListeners.set(type, callback)
  }

  class FakeAudioContext {
    constructor() { this.state = 'running'; this._listeners = []; this._deferResume = false }
    addEventListener(type, fn) { if (type === 'statechange') this._listeners.push(fn) }
    _setState(s) { if (this.state === s) return; this.state = s; this._listeners.forEach(fn => fn()) }
    // When _deferResume is set, model a native resume still in flight: the
    // promise settles but the state only flips once completeResume() runs.
    resume() { if (!this._deferResume) this._setState('running'); return Promise.resolve() }
    completeResume() { this._deferResume = false; this._setState('running') }
    suspend() { this._setState('suspended'); return Promise.resolve() }
    close() { this._setState('closed'); return Promise.resolve() }
  }
  class FakeMediaElement {
    constructor() { this.paused = true; this.isConnected = false }
    play() { this.paused = false; return Promise.resolve() }
    pause() { this.paused = true }
  }

  class FakeElement {
    constructor() { this.attributes = {} }
    setAttribute(name, value) { this.attributes[name] = value }
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null }
  }
  class FakeIframe extends FakeElement {
    constructor() { super(); this.posted = []; this.contentWindow = { postMessage: (data) => this.posted.push(data) } }
    get srcdoc() { return this.attributes.srcdoc }
    set srcdoc(html) { this.attributes.srcdoc = html }
  }

  const messageListeners = []
  const messageListener = (event) => messageListeners.forEach((listener) => listener(event))
  const nativeFrames = new Map()
  let lastNativeFrame = 0
  const parent = {}
  const window = {
    parent,
    origin,
    location: { origin: locationOrigin },
    AudioContext: FakeAudioContext,
    webkitAudioContext: FakeAudioContext,
    HTMLMediaElement: FakeMediaElement,
    requestAnimationFrame(callback) { nativeFrames.set(++lastNativeFrame, callback); return lastNativeFrame },
    cancelAnimationFrame(id) { nativeFrames.delete(id) },
    addEventListener(type, callback) { if (type === 'message') messageListeners.push(callback) },
  }
  const context = createContext({
    window, document, Document: FakeDocument, HTMLMediaElement: FakeMediaElement, Event,
    Element: FakeElement, HTMLIFrameElement: FakeIframe,
  })
  runInContext(source, context)

  return {
    window,
    document,
    attached,
    childFrames,
    rootStyle,
    rootWrites,
    visibilityEvents,
    Media: FakeMediaElement,
    Iframe: FakeIframe,
    Element: FakeElement,
    source,
    // The same script reaching this document a second time.
    reinstall() { runInContext(source, context) },
    // Run code inside the frame, stopped if it blocks for longer than timeout.
    run(code, globals, timeout) {
      Object.assign(context, globals)
      return runInContext(code, context, { timeout })
    },
    // One browser frame: every callback the browser was given runs once.
    paint() {
      const due = [...nativeFrames.values()]
      nativeFrames.clear()
      due.forEach((callback) => callback(0))
      return due.length
    },
    childLoaded(element) { captureListeners.get('load')({ target: element }) },
    setNativeVisibility(value) { nativeVisibility = value },
    shell(data, origin = SHELL_ORIGIN, eventSource = parent) {
      messageListener({ source: eventSource, origin, data: { type: 'moebius:frame-visibility', ...data } })
    },
    // Load this document into a child iframe of `host`, as the browser would.
    nestIn(host, iframe) {
      iframe.contentWindow.postMessage = (data) => messageListener({ source: parent, origin: SHELL_ORIGIN, data })
      host.childFrames.push(iframe)
    },
    // The browser begins playback on its own (autoplay, or a clip that was
    // still loading): it starts playing, then dispatches a capture-phase play
    // event the frame's override never saw. With keepPaused, the element is
    // left paused to model a stale play event delivered after an explicit
    // pause() — play events are asynchronous, so one can arrive late.
    browserPlay(element, { keepPaused = false } = {}) {
      if (!keepPaused) element.paused = false
      const onPlay = captureListeners.get('play')
      assert.ok(onPlay, 'frame registers a capture-phase play listener')
      onPlay({ target: element })
    },
  }
}

const settle = () => new Promise(resolve => setImmediate(resolve))

test('a hidden app reads as a hidden page and hears visibilitychange both ways', () => {
  const app = bootFrame()
  assert.equal(app.document.hidden, false)
  app.shell({ visible: false })
  assert.equal(app.document.hidden, true)
  assert.equal(app.document.visibilityState, 'hidden')
  app.shell({ visible: false })
  app.shell({ visible: true })
  assert.equal(app.document.visibilityState, 'visible')
  assert.deepEqual(app.visibilityEvents, ['visibilitychange', 'visibilitychange'])

  // The browser's own tab-hide still counts while the shell shows the app.
  app.setNativeVisibility('hidden')
  assert.equal(app.document.hidden, true)
})

test('running audio stops while hidden and resumes on return; app-paused audio stays paused', async () => {
  const app = bootFrame()
  const music = new app.window.AudioContext()
  const muted = new app.window.webkitAudioContext()
  await muted.suspend()

  app.shell({ visible: false })
  await settle()
  assert.equal(music.state, 'suspended')

  app.shell({ visible: true })
  await settle()
  assert.equal(music.state, 'running')
  assert.equal(muted.state, 'suspended', 'never resumes what the app paused itself')
})

test('resume() while hidden is held until return, and an app suspend() cancels it', async () => {
  const app = bootFrame()
  const sfx = new app.window.AudioContext()
  const ambience = new app.window.AudioContext()
  app.shell({ visible: false })
  await settle()

  await sfx.resume()
  assert.equal(sfx.state, 'suspended', 'a game loop cannot restart sound in the background')
  await ambience.suspend()

  app.shell({ visible: true })
  await settle()
  assert.equal(sfx.state, 'running')
  assert.equal(ambience.state, 'suspended')
})

test('a native resume that completes after the hide message does not leave audio running', async () => {
  const app = bootFrame()
  const ctx = new app.window.AudioContext()
  await ctx.suspend()
  assert.equal(ctx.state, 'suspended')

  // The app requests resume while visible; the native resume is still in flight
  // (state stays 'suspended') when the hide arrives, so hold() skips it.
  ctx._deferResume = true
  ctx.resume()
  assert.equal(ctx.state, 'suspended')

  app.shell({ visible: false })
  await settle()
  assert.equal(ctx.state, 'suspended')

  // The native resume now completes; the statechange handler must re-suspend it.
  ctx.completeResume()
  assert.equal(ctx.state, 'suspended', 'a resume landing while hidden is caught and re-suspended')

  app.shell({ visible: true })
  await settle()
  assert.equal(ctx.state, 'running', 'and the playback intent is restored on return')
})

test('a context created while hidden waits silently for the foreground', async () => {
  const app = bootFrame()
  app.shell({ visible: false })
  const booted = new app.window.AudioContext()
  assert.ok(booted instanceof app.window.AudioContext)
  await settle()
  assert.equal(booted.state, 'suspended')
  app.shell({ visible: true })
  await settle()
  assert.equal(booted.state, 'running')
})

test('playing media pauses while hidden, play() is held, and app pauses are respected', async () => {
  const app = bootFrame()
  const detached = new app.Media()
  await detached.play()
  const inPage = new app.Media()
  inPage.paused = false // e.g. an autoplay element the app never called play() on
  app.attached.push(inPage)
  const stopped = new app.Media()
  app.attached.push(stopped)

  app.shell({ visible: false })
  assert.equal(detached.paused, true)
  assert.equal(inPage.paused, true)

  await stopped.play()
  assert.equal(stopped.paused, true, 'play() is held while hidden')
  inPage.pause()

  app.shell({ visible: true })
  await settle()
  assert.equal(detached.paused, false)
  assert.equal(stopped.paused, false)
  assert.equal(inPage.paused, true, 'the app paused it while hidden')
})

test('deliberate background playback keeps playing but still reads as hidden', async () => {
  const app = bootFrame()
  const reader = new app.window.AudioContext()
  app.shell({ visible: false, backgroundPlayback: true })
  await settle()
  assert.equal(reader.state, 'running')
  assert.equal(app.document.hidden, true)
})

test('only the shell parent can background the app', async () => {
  const app = bootFrame()
  const music = new app.window.AudioContext()
  app.shell({ visible: false }, 'https://evil.test')
  app.shell({ visible: false }, SHELL_ORIGIN, {})
  await settle()
  assert.equal(music.state, 'running')
  assert.equal(app.document.hidden, false)
})

test('media that starts playing on its own while hidden is paused and resumes on return', async () => {
  const app = bootFrame()
  const autoplay = new app.Media()
  autoplay.isConnected = true
  app.attached.push(autoplay) // present but not yet playing when the frame hides

  // A browser-initiated play while visible is left alone.
  app.browserPlay(autoplay)
  assert.equal(autoplay.paused, false)
  autoplay.pause()

  app.shell({ visible: false })
  await settle()
  assert.equal(autoplay.paused, true, 'nothing played at hide time')

  // Autoplay policy now lets it start, bypassing the play() override entirely.
  app.browserPlay(autoplay)
  assert.equal(autoplay.paused, true, 'caught in the capture phase and paused while hidden')

  app.shell({ visible: true })
  await settle()
  assert.equal(autoplay.paused, false, 'resumes on return like anything else held')
})

test('a stale play event for media the app already paused does not restart it on return', async () => {
  const app = bootFrame()
  const clip = new app.Media()
  clip.isConnected = true
  app.attached.push(clip)

  // The app plays then explicitly pauses while visible; the browser's play
  // event is still queued for later delivery.
  await clip.play()
  clip.pause()
  assert.equal(clip.paused, true)

  app.shell({ visible: false })
  await settle()

  // The queued play event is delivered while hidden, targeting a paused element.
  app.browserPlay(clip, { keepPaused: true })

  app.shell({ visible: true })
  await settle()
  assert.equal(clip.paused, true, 'the app\'s explicit pause wins over a stale play event')
})

test('an element the app removes while hidden is not restarted on return', async () => {
  const app = bootFrame()
  const clip = new app.Media()
  clip.isConnected = true
  clip.paused = false
  app.attached.push(clip)

  app.shell({ visible: false })
  await settle()
  assert.equal(clip.paused, true, 'held while hidden')

  // The app tears the element out of the page while hidden (scene change or
  // re-render); nobody could stop a detached element we restarted.
  clip.isConnected = false

  app.shell({ visible: true })
  await settle()
  assert.equal(clip.paused, true, 'a once-attached, now-detached element is left paused')
})

test('a hidden app keeps its animation frames for its return, as a really hidden page does', () => {
  const app = bootFrame()
  const ran = []
  app.window.requestAnimationFrame(() => ran.push('before'))
  app.shell({ visible: false })
  app.window.requestAnimationFrame(() => ran.push('held'))
  const cancelled = app.window.requestAnimationFrame(() => ran.push('cancelled'))
  app.window.cancelAnimationFrame(cancelled)
  assert.equal(app.paint(), 1, 'only the frame requested before the hide reaches the browser')
  assert.deepEqual(ran, ['before'])

  app.shell({ visible: true })
  app.paint()
  assert.deepEqual(ran, ['before', 'held'])
})

test('a held frame stays cancellable while held and after return, until it runs', () => {
  const app = bootFrame()
  const ran = []
  app.shell({ visible: false })
  const whileHeld = app.window.requestAnimationFrame(() => ran.push('while held'))
  const afterReturn = app.window.requestAnimationFrame(() => ran.push('after return'))
  app.window.requestAnimationFrame(() => ran.push('kept'))
  app.window.cancelAnimationFrame(whileHeld)
  app.shell({ visible: true })
  app.window.cancelAnimationFrame(afterReturn)
  // The return also schedules the platform's own scroller refresh.
  assert.equal(app.paint(), 2)
  assert.deepEqual(ran, ['kept'])
})

test('a held frame runs with the same receiver as one the browser runs directly', () => {
  const app = bootFrame()
  const receivers = []
  // Sloppy-mode callbacks, as most app loops are written, so `this` is observable.
  const record = runInNewContext('(function (sink) { return function () { sink.push(this) } })')(receivers)
  app.window.requestAnimationFrame(record)
  app.paint()
  app.shell({ visible: false })
  app.window.requestAnimationFrame(record)
  app.shell({ visible: true })
  app.paint()
  assert.equal(receivers.length, 2)
  assert.equal(receivers[1], receivers[0], 'releasing a held frame does not change what `this` is')
})

test('platform painting keeps running while the app is held', () => {
  const app = bootFrame()
  app.shell({ visible: false })
  let painted = false
  app.window.__mobiusNativeFrame(() => { painted = true })
  app.paint()
  assert.equal(painted, true)
})

test('the drawer holds an app beneath it without calling a painted page hidden', async () => {
  const app = bootFrame()
  const music = new app.window.AudioContext()
  const loop = []
  app.shell({ visible: true, covered: true })
  app.window.requestAnimationFrame(() => loop.push('frame'))
  await settle()
  assert.equal(music.state, 'suspended', 'the sound stops as soon as the owner reaches for the drawer')
  assert.equal(app.paint(), 0, 'the frame loop stops competing with the drawer')
  assert.equal(app.document.hidden, false)
  assert.deepEqual(app.visibilityEvents, [])

  app.shell({ visible: true, covered: false })
  await settle()
  app.paint()
  assert.equal(music.state, 'running', 'closing the drawer picks the app back up')
  assert.deepEqual(loop, ['frame'])
})

test('leaving through the drawer keeps the app held and now reads as hidden', async () => {
  const app = bootFrame()
  const music = new app.window.AudioContext()
  app.shell({ visible: true, covered: true })
  app.shell({ visible: false })
  await settle()
  assert.equal(music.state, 'suspended')
  assert.equal(app.document.hidden, true)
  assert.deepEqual(app.visibilityEvents, ['visibilitychange'])
})

test('deliberate background playback is not held beneath the drawer either', async () => {
  const app = bootFrame()
  const podcast = new app.window.AudioContext()
  app.shell({ visible: true, covered: true, backgroundPlayback: true })
  await settle()
  assert.equal(podcast.state, 'running')
})

test('every nested document an app writes runs the pause first', () => {
  const app = bootFrame()
  const viaProperty = new app.Iframe()
  viaProperty.srcdoc = '<!doctype html><html><body>game</body></html>'
  assert.match(viaProperty.attributes.srcdoc, /^<!doctype html><script data-mobius-background-pause>/)
  assert.ok(viaProperty.attributes.srcdoc.endsWith('</script><html><body>game</body></html>'))
  assert.ok(viaProperty.attributes.srcdoc.includes(app.source), 'nested documents reach their own children the same way')

  const viaAttribute = new app.Iframe()
  viaAttribute.setAttribute('SRCDOC', '<p>game</p>')
  assert.match(viaAttribute.attributes.SRCDOC, /^<script data-mobius-background-pause>/)
  assert.ok(viaAttribute.attributes.SRCDOC.endsWith('</script><p>game</p>'))
})

test('a nested document keeps its rendering mode behind leading comments', () => {
  const app = bootFrame()
  const iframe = new app.Iframe()
  iframe.srcdoc = '  <!-- built for play -->\n<!DOCTYPE html><p>game</p>'
  assert.match(iframe.attributes.srcdoc, /^ {2}<!-- built for play -->\n<!DOCTYPE html><script data-mobius-background-pause>/)
})

test('a nested document behind many comments and no doctype is wrapped at once', () => {
  const app = bootFrame()
  const iframe = new app.Iframe()
  const html = '<!-- part -->\n'.repeat(40) + '<p>game</p>'
  app.run('iframe.srcdoc = html', { iframe, html }, 1000)
  assert.match(iframe.attributes.srcdoc, /^<script data-mobius-background-pause>/)
  assert.ok(iframe.attributes.srcdoc.endsWith('</script>' + html))
})

test('an app reads back the srcdoc it set, so setting it again installs the pause once', () => {
  const app = bootFrame()
  const html = '<!doctype html><p>game</p>'
  const viaProperty = new app.Iframe()
  viaProperty.srcdoc = html
  assert.equal(viaProperty.srcdoc, html)
  assert.equal(viaProperty.getAttribute('srcdoc'), html)
  const written = viaProperty.attributes.srcdoc
  const readBack = viaProperty.srcdoc
  viaProperty.srcdoc = readBack
  assert.equal(viaProperty.attributes.srcdoc, written)

  const viaAttribute = new app.Iframe()
  viaAttribute.setAttribute('srcdoc', html)
  assert.equal(viaAttribute.srcdoc, html)
  assert.equal(viaAttribute.getAttribute('srcdoc'), html)
  viaAttribute.setAttribute('srcdoc', viaAttribute.getAttribute('srcdoc'))
  assert.equal(viaAttribute.attributes.srcdoc, written)
})

test('the pause installs once in a document however often it arrives', () => {
  const app = bootFrame()
  app.reinstall()
  const game = new app.Iframe()
  app.childFrames.push(game)
  app.shell({ visible: false })
  assert.equal(game.posted.length, 1)
  assert.deepEqual(app.visibilityEvents, ['visibilitychange'])
})

test('other attributes and other elements pass through untouched', () => {
  const app = bootFrame()
  const iframe = new app.Iframe()
  iframe.setAttribute('title', 'game')
  assert.equal(iframe.attributes.title, 'game')
  const other = new app.Element()
  other.setAttribute('srcdoc', 'text')
  assert.equal(other.attributes.srcdoc, 'text')
})

test('a re-shown document rebuilds its own root scroller, keeping the page\'s value', () => {
  // On macOS Chrome a nested page can stop scrolling after the shell hides and
  // re-shows its app, until the owner clicks inside it. Toggling the page's own
  // root overflow off and on (from inside that page) restores scrolling.
  const app = bootFrame()
  app.rootStyle.setProperty('overflow', 'auto')
  app.rootWrites.length = 0

  app.shell({ visible: true })
  app.paint(); app.paint()
  assert.deepEqual(app.rootWrites, [], 'an already-visible document is left alone')

  app.shell({ visible: false })
  app.shell({ visible: true })
  assert.deepEqual(app.rootWrites, ['hidden', 'layout', 'auto'], 'rebuilt at once')
  app.paint()
  app.paint()
  assert.deepEqual(app.rootWrites, ['hidden', 'layout', 'auto', 'hidden', 'layout', 'auto'], 'and again once shown')
  assert.equal(app.rootStyle.getPropertyValue('overflow'), 'auto')

  const plain = bootFrame()
  plain.shell({ visible: false })
  plain.shell({ visible: true })
  assert.equal(plain.rootStyle.getPropertyValue('overflow'), '', 'no inline value is left behind')
})

test('the frame script can never end its own element early', () => {
  // A literal comment-open followed by a script tag switches the HTML parser
  // into double-escaped script text, where the real closing tag is ignored.
  const source = frame.match(/<script data-mobius-background-pause>([\s\S]*?)<\/script>/)[1]
  assert.doesNotMatch(source, /<!--/)
  assert.doesNotMatch(source, /<script/i)
})

test('each verdict reaches child frames, and a child that loads later hears the last one', () => {
  const app = bootFrame()
  const game = new app.Iframe()
  app.childFrames.push(game)
  app.shell({ visible: false, covered: false, navigationReady: false })
  app.shell({ visible: true, covered: true })
  assert.deepEqual(game.posted.map(({ visible, covered }) => ({ visible, covered })), [
    { visible: false, covered: false },
    { visible: true, covered: true },
  ])
  assert.equal(game.posted[0].type, 'moebius:frame-visibility')
  assert.equal('navigationReady' in game.posted[0], false, 'children hear only the lifecycle verdict')

  const late = new app.Iframe()
  app.childLoaded(late)
  assert.deepEqual({ visible: late.posted[0].visible, covered: late.posted[0].covered }, { visible: true, covered: true })
})

test('a nested srcdoc document pauses on the verdict its parent forwards', async () => {
  const app = bootFrame()
  const game = bootFrame({ locationOrigin: 'null' })
  game.nestIn(app, new app.Iframe())
  const music = new game.window.AudioContext()
  app.shell({ visible: true, covered: true })
  await settle()
  assert.equal(music.state, 'suspended')
  app.shell({ visible: true, covered: false })
  await settle()
  assert.equal(music.state, 'running')
})

test('a sandboxed nested document trusts only the frame that embeds it', async () => {
  const app = bootFrame()
  const game = bootFrame({ locationOrigin: 'null', origin: 'null' })
  game.nestIn(app, new app.Iframe())
  const music = new game.window.AudioContext()
  game.shell({ visible: false }, SHELL_ORIGIN, {})
  await settle()
  assert.equal(music.state, 'running')
  app.shell({ visible: false })
  await settle()
  assert.equal(music.state, 'suspended')
})

test('a child frame that loads before any verdict is not told anything', () => {
  const app = bootFrame()
  const child = new app.Iframe()
  app.childLoaded(child)
  assert.deepEqual(child.posted, [])
})

test('the drawer holds the app from the opening tap, before its panel commits', () => {
  assert.match(shell, /covered=\{modalDrawerOpen \|\| drawerOpening\}/)
  assert.match(canvas, /type: 'moebius:frame-visibility',\s+visible,\s+covered,/)
  assert.match(canvas, /\[frameVisible, covered, visible, active, swap\.liveVersion, swap\.liveLoaded\]/)
})
