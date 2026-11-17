/**
 * The pinned foot rows follow the reader's scroll direction like a mobile
 * browser's toolbar: scrolling up sinks them by the pixels moved, scrolling
 * down raises them by the same, at the bottom they are always shown, only the
 * reader's own scrolling moves them, and they never move the transcript.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useFootReveal, {
  FOOT_REVEAL_SHOWN,
  READER_SCROLL_WINDOW_MS,
  footRevealFraction,
  footRevealStep,
  layerTravel,
  readerScrollMotion,
} from '../useFootReveal.js'

const H = 142
// A reader in the history of a 5000px transcript in an 800px view.
const at = (scrollTop, extra = {}) => ({
  scrollTop, scrollHeight: 5000, clientHeight: 800, height: H, mode: 'ANCHOR_AT', readerScrolled: true, ...extra,
})
const run = (frames, start = FOOT_REVEAL_SHOWN) => frames.reduce(footRevealStep, start)
const BOTTOM = 4200

test('scrolling up sinks the rows by the pixels moved, clamped at their height', () => {
  const base = run([at(BOTTOM, { mode: 'FOLLOW_BOTTOM' })])
  assert.equal(base.offset, 0)
  assert.equal(run([at(BOTTOM - 30)], base).offset, 30)
  assert.equal(run([at(BOTTOM - 30), at(BOTTOM - 80)], base).offset, 80)
  assert.equal(run([at(BOTTOM - 200)], base).offset, H, 'gone at H, never beyond')
  assert.equal(footRevealFraction(run([at(BOTTOM - 71)], base)), 0.5)
})

test('scrolling down from anywhere in the history raises them progressively', () => {
  const hidden = run([at(3000), at(2000)])
  assert.equal(hidden.offset, H)
  const down30 = run([at(2030)], hidden)
  assert.equal(down30.offset, H - 30)
  const down80 = run([at(2080)], hidden)
  assert.equal(down80.offset, H - 80)
  assert.equal(run([at(2500)], hidden).offset, 0, 'fully back, never below 0')
  // Direction changes mid-way are followed exactly.
  assert.equal(run([at(2080), at(2040)], hidden).offset, H - 40)
})

test('at the physical bottom the rows are always fully shown', () => {
  const hidden = run([at(3000), at(2000)])
  // Even a programmatic arrival (scroll to bottom) shows them.
  assert.equal(run([at(BOTTOM, { readerScrolled: false })], hidden).offset, 0)
  assert.equal(run([at(BOTTOM - 1.5, { readerScrolled: false })], hidden).offset, 0, 'sub-pixel rest is the bottom')
  // Overscroll past the end too.
  assert.equal(run([at(BOTTOM + 40)], hidden).offset, 0)
})

test('programmatic scroll jumps do not count as the reader scrolling', () => {
  const half = run([at(3000), at(2930)])
  assert.equal(half.offset, 70)
  // An anchor repair, a restore or a follow write moves scrollTop with no reader input.
  assert.equal(run([at(1000, { readerScrolled: false })], half).offset, 70)
  assert.equal(run([at(3900, { readerScrolled: false })], half).offset, 70)
  // ...and the next reader frame measures from where the jump left the view.
  assert.equal(run([at(3900, { readerScrolled: false }), at(3890)], half).offset, 80)
})

test('a page prepended above a scrolling reader is not read as a jump back down', () => {
  // The reader scrolls up 20px while 3000px of older history lands above and
  // the controller compensates scrollTop by the same 3000px.
  const prev = run([at(800), at(700)])
  assert.equal(prev.offset, 100)
  const next = footRevealStep(prev, at(700 + 3000 - 20, { scrollHeight: 8000 }))
  assert.equal(next.offset, 120)
  // A reply growing below a scrolling reader does not inflate the motion either.
  assert.equal(readerScrollMotion({ scrollTop: 700, scrollHeight: 5000 }, { scrollTop: 680, scrollHeight: 5300 }), -20)
})

test('entering the live end shows them; following keeps them as they are', () => {
  const hidden = run([at(3000), at(2000)])
  // A send pins the new message, the jump to latest re-engages follow.
  assert.equal(run([at(2000, { mode: 'PIN_USER_MSG', readerScrolled: false })], hidden).offset, 0)
  assert.equal(run([at(3500, { mode: 'FOLLOW_BOTTOM', readerScrolled: false })], hidden).offset, 0)
  // A streamed reply runs ahead of the follow by up to ~150px: no movement.
  const following = run([at(BOTTOM, { mode: 'FOLLOW_BOTTOM' })])
  const lag = run([at(BOTTOM - 150, { mode: 'FOLLOW_BOTTOM', readerScrolled: false, scrollHeight: 5000 })], following)
  assert.equal(lag.offset, 0)
  // The reader's own scroll up during a follow (mode still FOLLOW_BOTTOM until
  // the gesture settles) hides them, and the settle to ANCHOR_AT keeps that.
  const up = run([at(BOTTOM - 90, { mode: 'FOLLOW_BOTTOM' })], following)
  assert.equal(up.offset, 90)
  assert.equal(run([at(BOTTOM - 90, { mode: 'ANCHOR_AT', readerScrolled: false })], up).offset, 90)
})

test('a change in the rows height keeps how hidden they are', () => {
  const half = run([at(3000), at(2929)])
  assert.equal(footRevealFraction(half), 0.5)
  const grown = footRevealStep(half, at(2929, { height: 2 * H, readerScrolled: false }))
  assert.equal(footRevealFraction(grown), 0.5)
  assert.equal(footRevealStep(half, at(2929, { height: 0 })).offset, 0, 'no rows, nothing to hide')
})

test('reduced motion snaps hidden or shown by direction, with no slide', () => {
  const rm = extra => at(extra.scrollTop, { reducedMotion: true, ...extra })
  const up = run([rm({ scrollTop: 3000 }), rm({ scrollTop: 2995 })])
  assert.equal(up.offset, H)
  assert.equal(run([rm({ scrollTop: 2996 })], up).offset, 0)
  assert.equal(run([rm({ scrollTop: 2995, readerScrolled: false })], up).offset, H)
})

function fakeEl() {
  const attrs = new Map()
  const vars = new Map()
  return {
    inert: false,
    offsetHeight: 136,
    offsetTop: 0,
    style: { setProperty: (name, value) => vars.set(name, value) },
    setAttribute: (name, value) => attrs.set(name, value),
    removeAttribute: name => attrs.delete(name),
    v: name => vars.get(name),
    hidden: () => attrs.has('data-hidden'),
  }
}

function fakeScroller({ scrollTop }) {
  const listeners = new Map()
  return {
    scrollHeight: 5000, clientHeight: 800, children: [], dataset: { scrollMode: 'FOLLOW_BOTTOM' },
    addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: type => listeners.delete(type),
    listening: () => listeners.size > 0,
    emit: (type, event = {}) => listeners.get(type)?.(event),
    // Reading position is all the hook may do: any write would throw.
    get scrollTop() { return scrollTop },
    set scrollTop(_) { throw new Error('the foot must never move the transcript') },
    scrollTo() { throw new Error('the foot must never move the transcript') },
    scrollBy() { throw new Error('the foot must never move the transcript') },
    move(top) { scrollTop = top; listeners.get('scroll')?.() },
  }
}

function withWindow(run) {
  const saved = { window: globalThis.window, now: performance.now }
  const frames = []
  const windowListeners = new Map()
  let clock = 1000
  performance.now = () => clock
  globalThis.window = {
    requestAnimationFrame: fn => frames.push(fn),
    cancelAnimationFrame: () => {},
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    getComputedStyle: () => ({ marginBottom: '6px' }),
    addEventListener: (type, fn) => windowListeners.set(type, fn),
    removeEventListener: type => windowListeners.delete(type),
  }
  try {
    return run({
      flush: () => { for (const fn of frames.splice(0)) fn() },
      advance: ms => { clock += ms },
      windowListening: () => windowListeners.size > 0,
    })
  } finally {
    globalThis.window = saved.window
    performance.now = saved.now
  }
}

test('the hook moves the rows per frame from reader scrolls only, and restores them on teardown', () => {
  withWindow(({ flush, advance, windowListening }) => {
    const foot = fakeEl()
    const layer = fakeEl() // 136px + 6px gap = 142px of travel
    const scroller = fakeScroller({ scrollTop: BOTTOM })
    const { unmount } = renderHook(() => useFootReveal({ current: scroller }, { current: foot }, { current: layer }, true))
    assert.equal(foot.v('--foot-hide'), '0px')

    scroller.emit('wheel')
    scroller.move(BOTTOM - 30)
    assert.equal(foot.v('--foot-hide'), '0px', 'nothing changes before the next frame')
    flush()
    assert.equal(foot.v('--foot-hide'), '30px')
    assert.equal(layer.inert, false)

    advance(100) // momentum: later scroll events keep the gesture open
    scroller.move(BOTTOM - 80)
    flush()
    assert.equal(foot.v('--foot-hide'), '80px')
    assert.equal(foot.v('--foot-hide-fraction'), '0.563')
    assert.equal(layer.inert, false, 'half-sunk rows still take taps on what shows')

    advance(100)
    scroller.move(BOTTOM - 110)
    flush()
    assert.equal(foot.v('--foot-hide-fraction'), '0.775')
    assert.equal(layer.inert, true, 'mostly hidden rows take no taps')
    assert.equal(layer.hidden(), true)

    // Past the quiet edge a scroll is programmatic and moves nothing.
    advance(READER_SCROLL_WINDOW_MS + 1)
    scroller.move(1200)
    flush()
    assert.equal(foot.v('--foot-hide'), '110px')

    // A finger on the glass makes it the reader's again; down raises them.
    scroller.emit('touchstart', { touches: [{}] })
    advance(5000)
    scroller.move(1230)
    flush()
    assert.equal(foot.v('--foot-hide'), '80px')
    assert.equal(layer.inert, false)
    scroller.emit('touchend', { touches: [] })

    // The controller re-engaging follow (the jump to latest) shows them.
    advance(5000)
    scroller.dataset.scrollMode = 'ANCHOR_AT'
    scroller.move(1100)
    flush()
    scroller.dataset.scrollMode = 'FOLLOW_BOTTOM'
    scroller.move(1100)
    flush()
    assert.equal(foot.v('--foot-hide'), '0px')

    unmount()
    assert.equal(scroller.listening(), false)
    assert.equal(windowListening(), false)
    assert.equal(foot.v('--foot-hide'), '0px', 'a torn-down hook never leaves rows hidden')
    assert.equal(layer.inert, false)
  })
})

test('with no transcript the rows show and nothing listens', () => {
  withWindow(() => {
    const foot = fakeEl()
    const layer = fakeEl()
    layer.inert = true
    const scroller = fakeScroller({ scrollTop: 0 })
    renderHook(() => useFootReveal({ current: scroller }, { current: foot }, { current: layer }, false))
    assert.equal(foot.v('--foot-hide'), '0px')
    assert.equal(layer.inert, false)
    assert.equal(scroller.listening(), false)
  })
})


test('rows travel behind a growing composer to the viewport edge; floating controls stop sooner', () => {
  withWindow(() => {
    const layer = fakeEl()
    const foot = fakeEl()
    layer.offsetHeight = 34
    layer.offsetTop = 8
    foot.offsetHeight = 102
    assert.equal(layerTravel(layer, foot), 94)
    assert.equal(foot.v('--foot-rows-travel'), '40px')
    foot.offsetHeight = 174
    assert.equal(layerTravel(layer, foot), 166, 'multiline input extends the path without moving input')
    window.getComputedStyle = () => ({ marginBottom: '6px', bottom: '24px' })
    assert.equal(layerTravel(layer, foot), 190, 'safe-area gap is part of the exit path')
    layer.offsetHeight = 0
    assert.equal(layerTravel(layer, foot), 0)
  })
})
