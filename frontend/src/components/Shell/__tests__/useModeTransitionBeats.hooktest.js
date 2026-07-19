// Behavioral regression tests for the mode-transition beat state machine.
// Runs under the react-hook shim (see ChatView/hooks/__tests__/react-hook-shim)
// with an INJECTED mock clock so the timer-driven settle is deterministic.
//
// These lock the invariant that fixes the "rapid re-enter wedges / fights the
// deal" class: at most ONE beat is ever active, and a beat NEVER survives the
// next toggle. The wedge sequence (exit -> re-enter within the exit beat -> exit)
// is exercised directly.

import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import { useModeTransitionBeats } from '../useModeTransitionBeats.js'

const ENTER_MS = 380
const EXIT_MS = 250

function mockClock() {
  let seq = 1
  let now = 0
  const timers = new Map()
  return {
    set: (fn, ms) => { const id = seq++; timers.set(id, { at: now + ms, fn }); return id },
    clear: (id) => { timers.delete(id) },
    advance: (ms) => {
      now += ms
      const due = [...timers.entries()]
        .filter(([, t]) => t.at <= now)
        .sort((a, b) => a[1].at - b[1].at)
      for (const [id, t] of due) { timers.delete(id); t.fn() }
    },
    pending: () => timers.size,
  }
}

function mount() {
  const clock = mockClock()
  const cfg = { enterMs: ENTER_MS, exitMs: EXIT_MS, scheduler: { set: clock.set, clear: clock.clear } }
  const h = renderHook(() => useModeTransitionBeats(cfg))
  return { clock, get: () => h.result.current }
}

test('mounts with both beats off and no timer', () => {
  const { clock, get } = mount()
  assert.equal(get().builderExiting, false)
  assert.equal(get().builderEntering, false)
  assert.equal(clock.pending(), 0)
})

test("armBeat('exit') holds exiting, then self-clears on the exit deadline", () => {
  const { clock, get } = mount()
  get().armBeat('exit')
  assert.equal(get().builderExiting, true)
  assert.equal(get().builderEntering, false)
  clock.advance(EXIT_MS - 1)
  assert.equal(get().builderExiting, true, 'still held just before the deadline')
  clock.advance(1)
  assert.equal(get().builderExiting, false, 'cleared on the deadline')
  assert.equal(clock.pending(), 0)
})

test("armBeat('enter') holds entering, then self-clears on the enter deadline", () => {
  const { clock, get } = mount()
  get().armBeat('enter')
  assert.equal(get().builderEntering, true)
  assert.equal(get().builderExiting, false)
  clock.advance(ENTER_MS)
  assert.equal(get().builderEntering, false)
  assert.equal(clock.pending(), 0)
})

test('the beats are mutually exclusive — arming enter cancels a live exit beat', () => {
  const { clock, get } = mount()
  get().armBeat('exit')
  clock.advance(80) // still inside the 250ms exit beat
  assert.equal(get().builderExiting, true)
  get().armBeat('enter') // rapid re-enter
  assert.equal(get().builderExiting, false, 'exit deal is cancelled, not left fighting')
  assert.equal(get().builderEntering, true)
  assert.equal(clock.pending(), 1, 'only the enter timer remains — no orphaned exit timer')
  clock.advance(ENTER_MS)
  assert.equal(get().builderEntering, false)
})

test('armBeat(null) clears any live beat immediately (reduced motion / instant collapse)', () => {
  const { clock, get } = mount()
  get().armBeat('exit')
  assert.equal(get().builderExiting, true)
  get().armBeat(null)
  assert.equal(get().builderExiting, false)
  assert.equal(get().builderEntering, false)
  assert.equal(clock.pending(), 0)
})

test('WEDGE SEQUENCE: exit -> re-enter within the beat -> exit always settles, never both-on', () => {
  const { clock, get } = mount()
  const bothOn = () => get().builderExiting && get().builderEntering
  get().armBeat('exit')      // exit1
  assert.equal(bothOn(), false)
  clock.advance(100)         // still inside the exit beat
  get().armBeat('enter')     // re-enter within 250ms
  assert.equal(bothOn(), false)
  clock.advance(80)
  get().armBeat('exit')      // exit2, still overlapping the earlier timers
  assert.equal(bothOn(), false)
  clock.advance(EXIT_MS)     // let the last beat's deadline pass
  assert.equal(get().builderExiting, false, 'settles to no beat')
  assert.equal(get().builderEntering, false)
  assert.equal(clock.pending(), 0, 'no stranded timer can wedge a later render')
})

test('a long rapid toggle loop never strands a beat', () => {
  const { clock, get } = mount()
  const kinds = ['exit', 'enter', 'exit', 'enter', 'exit', null, 'enter', 'exit', 'enter']
  for (const k of kinds) {
    get().armBeat(k)
    assert.equal(get().builderExiting && get().builderEntering, false, 'never both on')
    clock.advance(30) // sub-beat gap
  }
  clock.advance(EXIT_MS + ENTER_MS) // drain
  assert.equal(get().builderExiting, false)
  assert.equal(get().builderEntering, false)
  assert.equal(clock.pending(), 0)
})
