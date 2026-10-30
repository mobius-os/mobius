import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
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
  assert.match(frame, /suspendedScrollFrame = requestAnimationFrame\(holdSuspendedScroll\)/)
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

function bootFrame() {
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
  document.dispatchEvent = (event) => { visibilityEvents.push(event.type) }
  document.querySelectorAll = () => attached
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

  let messageListener = null
  const parent = {}
  const window = {
    parent,
    location: { origin: SHELL_ORIGIN },
    AudioContext: FakeAudioContext,
    webkitAudioContext: FakeAudioContext,
    HTMLMediaElement: FakeMediaElement,
    addEventListener(type, callback) { if (type === 'message') messageListener = callback },
  }
  runInNewContext(source, {
    window, document, Document: FakeDocument, HTMLMediaElement: FakeMediaElement, Event,
  })

  return {
    window,
    document,
    attached,
    visibilityEvents,
    Media: FakeMediaElement,
    setNativeVisibility(value) { nativeVisibility = value },
    shell(data, origin = SHELL_ORIGIN, eventSource = parent) {
      messageListener({ source: eventSource, origin, data: { type: 'moebius:frame-visibility', ...data } })
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
