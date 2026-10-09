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
