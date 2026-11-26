/**
 * The runtime's edge swipe (src/runtime/edgeSwipeBack.js through makeNav).
 * Real touch events go through the runtime's own document listeners; the
 * document is a small EventTarget stand-in because the gesture needs only
 * listeners, one selector query and one indicator element.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { makeNav } from '../../../public/mobius-runtime.js'

const NATIVE_DIALOG = 'dialog[open]'
const ARIA_DIALOG = '[role="dialog"][aria-modal="true"]'

function makeTouchDocument() {
  const target = new EventTarget()
  const live = new Map() // type -> Set of listener options
  const open = new Set() // simple selectors an element currently matches
  const children = []
  return {
    open,
    children,
    body: { appendChild(el) { children.push(el) } },
    listening(type) { return [...(live.get(type) || [])] },
    addEventListener(type, cb, options) {
      if (!live.has(type)) live.set(type, new Set())
      live.get(type).add(options)
      target.addEventListener(type, cb, options)
    },
    removeEventListener(type, cb, options) {
      live.get(type)?.delete(options)
      target.removeEventListener(type, cb, options)
    },
    dispatchEvent: (event) => target.dispatchEvent(event),
    querySelector(selectors) {
      return selectors.split(',').some((s) => open.has(s.trim())) ? {} : null
    },
    createElement() {
      const el = {
        style: {},
        setAttribute() {},
        remove() { children.splice(children.indexOf(el), 1) },
      }
      return el
    },
  }
}

function touch(doc, type, x, y, timeStamp) {
  const event = new Event(type, { cancelable: true })
  const point = { clientX: x, clientY: y }
  const ended = type === 'touchend' || type === 'touchcancel'
  Object.defineProperty(event, 'touches', { value: ended ? [] : [point] })
  Object.defineProperty(event, 'changedTouches', { value: [point] })
  Object.defineProperty(event, 'timeStamp', { value: timeStamp })
  doc.dispatchEvent(event)
  return event
}

// A rightward drag from `x`, as a list of points over `ms` milliseconds.
function swipe(doc, { x = 10, y = 300, dx = 140, dy = 4, ms = 200 } = {}) {
  touch(doc, 'touchstart', x, y, 0)
  const moves = [0.25, 0.6, 1].map((f) => touch(doc, 'touchmove', x + dx * f, y + dy * f, ms * f))
  touch(doc, 'touchend', x + dx, y + dy, ms)
  return moves
}

async function withFrame(fn) {
  const previousWindow = globalThis.window
  const previousDocument = globalThis.document
  const listeners = new Set()
  const parent = {
    messages: [],
    postMessage(data) { this.messages.push(data) },
  }
  const window = {
    location: { origin: 'https://mobius.test' },
    parent,
    addEventListener(type, cb) { if (type === 'message') listeners.add(cb) },
    removeEventListener(type, cb) { if (type === 'message') listeners.delete(cb) },
    emit(data) {
      for (const cb of [...listeners]) cb({ data, origin: 'https://mobius.test', source: parent })
    },
  }
  const doc = makeTouchDocument()
  globalThis.window = window
  globalThis.document = doc
  try {
    const nav = makeNav()
    const backed = []
    const openLevel = async (label) => {
      const handle = nav.open(label, () => backed.push(label))
      window.emit({ type: 'moebius:nav-push-ack', requestId: parent.messages.at(-1).requestId })
      await handle.outcome
      return handle
    }
    const host = (edgeSwipeBack, visible = true) => {
      window.emit({ type: 'moebius:frame-visibility', visible, edgeSwipeBack })
    }
    const pops = () => parent.messages.filter((m) => m.type === 'moebius:nav-pop').length
    await fn({ window, doc, nav, backed, openLevel, host, pops })
  } finally {
    globalThis.window = previousWindow
    globalThis.document = previousDocument
  }
}

test('an edge swipe does what system back does for the newest nav level', async () => {
  await withFrame(async ({ window, doc, backed, openLevel, host, pops }) => {
    host(true)
    await openLevel('collection')
    await openLevel('detail')

    const moves = swipe(doc)
    assert.deepEqual(backed, ['detail'])
    assert.equal(pops(), 1, 'the shell retires the back entry the swipe consumed')
    assert.equal(moves.at(-1).defaultPrevented, true, 'a claimed swipe does not scroll the page')
    assert.equal(doc.children.length, 0, 'the arrow leaves with the finger')

    // System back now reaches the level beneath, so the two never disagree.
    window.emit({ type: 'moebius:nav-back' })
    assert.deepEqual(backed, ['detail', 'collection'])
    swipe(doc)
    assert.deepEqual(backed, ['detail', 'collection'], 'no level left, no swipe')
  })
})

test('a short flick goes back; a short slow drag, a vertical scroll or a mid-screen start do not', async () => {
  await withFrame(async ({ doc, backed, openLevel, host }) => {
    host(true)
    await openLevel('detail')

    swipe(doc, { dx: 40, ms: 400 })
    const scroll = swipe(doc, { dx: 30, dy: 200 })
    swipe(doc, { x: 120 })
    assert.deepEqual(backed, [])
    assert.equal(scroll.at(-1).defaultPrevented, false, 'a vertical drag stays a scroll')

    swipe(doc, { dx: 40, ms: 50 })
    assert.deepEqual(backed, ['detail'])
  })
})

test('an open modal dialog leaves the edge alone, including one opened mid-swipe', async () => {
  await withFrame(async ({ doc, backed, openLevel, host, pops }) => {
    host(true)
    await openLevel('detail')

    for (const dialog of [NATIVE_DIALOG, ARIA_DIALOG]) {
      doc.open.add(dialog)
      swipe(doc)
      doc.open.delete(dialog)
    }
    touch(doc, 'touchstart', 10, 300, 0)
    touch(doc, 'touchmove', 100, 300, 100)
    doc.open.add(NATIVE_DIALOG)
    touch(doc, 'touchend', 150, 300, 150)
    assert.deepEqual(backed, [])
    assert.equal(pops(), 0)
  })
})

test('the blocking touchmove listener exists only while an edge touch is down', async () => {
  await withFrame(async ({ doc, openLevel, host }) => {
    host(true)
    await openLevel('detail')
    assert.deepEqual(doc.listening('touchstart').map((o) => o.passive), [true])
    assert.deepEqual(doc.listening('touchmove'), [])

    touch(doc, 'touchstart', 120, 300, 0)
    assert.deepEqual(doc.listening('touchmove'), [], 'a mid-screen touch scrolls freely')
    touch(doc, 'touchend', 120, 300, 10)

    touch(doc, 'touchstart', 10, 300, 0)
    assert.deepEqual(doc.listening('touchmove').map((o) => o.passive), [false])
    touch(doc, 'touchcancel', 10, 300, 10)
    assert.deepEqual(doc.listening('touchmove'), [])
  })
})

test('the swipe is off until the host says there is no system back gesture', async () => {
  await withFrame(async ({ doc, backed, openLevel, host }) => {
    host(false)
    await openLevel('detail')
    assert.deepEqual(doc.listening('touchstart'), [])
    swipe(doc)
    assert.deepEqual(backed, [])

    host(true)
    host(true, false)
    assert.deepEqual(doc.listening('touchstart'), [], 'a hidden frame drops its listener')
    host(true)
    swipe(doc)
    assert.deepEqual(backed, ['detail'])
  })
})
