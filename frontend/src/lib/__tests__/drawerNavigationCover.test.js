import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  DRAWER_NAVIGATION_COVER_CAP_MS,
  createDrawerNavigationCoverCap,
} from '../drawerLifecycle.js'

// A hand-driven clock: the cap must be provably bounded without the test
// waiting real seconds for it.
function fakeClock() {
  const pending = new Map()
  let nextId = 1
  let now = 0
  return {
    schedule(callback, delay) {
      const id = nextId
      nextId += 1
      pending.set(id, { callback, at: now + delay })
      return id
    },
    cancel(id) {
      pending.delete(id)
    },
    advance(ms) {
      now += ms
      for (const [id, entry] of [...pending]) {
        if (entry.at > now) continue
        pending.delete(id)
        entry.callback()
      }
    },
    get scheduled() {
      return pending.size
    },
  }
}

function coverUnderTest({ capMs = DRAWER_NAVIGATION_COVER_CAP_MS } = {}) {
  const clock = fakeClock()
  const releases = []
  const cap = createDrawerNavigationCoverCap({
    release: () => releases.push(true),
    capMs,
    schedule: clock.schedule,
    cancel: clock.cancel,
  })
  return { cap, clock, releases }
}

test('a destination that never reports ready still releases the drawer cover', () => {
  // The regression: chat detail loading hangs, display-ready never fires, and
  // the interaction-locked drawer stayed over the workspace with no way out.
  const { cap, clock, releases } = coverUnderTest()
  cap.arm()
  assert.equal(cap.armed, true)
  clock.advance(DRAWER_NAVIGATION_COVER_CAP_MS - 1)
  assert.deepEqual(releases, [], 'the cover holds for the whole readiness window')
  clock.advance(1)
  assert.deepEqual(releases, [true], 'silence past the cap releases the cover')
  assert.equal(cap.armed, false)
})

test('destination readiness ends the cover without waiting for the cap', () => {
  const { cap, clock, releases } = coverUnderTest()
  cap.arm()
  clock.advance(120)
  cap.disarm()
  assert.equal(cap.armed, false)
  clock.advance(DRAWER_NAVIGATION_COVER_CAP_MS * 2)
  assert.deepEqual(releases, [], 'the ordinary path owns the close, not the cap')
  assert.equal(clock.scheduled, 0, 'a disarmed cap leaves no timer behind')
})

test('a second covered navigation restarts the bound from its own tap', () => {
  const { cap, clock, releases } = coverUnderTest()
  cap.arm()
  clock.advance(DRAWER_NAVIGATION_COVER_CAP_MS - 10)
  cap.arm()
  assert.equal(clock.scheduled, 1, 'the superseded bound is cancelled, never stacked')
  clock.advance(10)
  assert.deepEqual(releases, [], 'the first tap cannot release the second cover early')
  clock.advance(DRAWER_NAVIGATION_COVER_CAP_MS - 10)
  assert.deepEqual(releases, [true])
})

test('the cap releases exactly once', () => {
  const { cap, clock, releases } = coverUnderTest()
  cap.arm()
  clock.advance(DRAWER_NAVIGATION_COVER_CAP_MS)
  cap.disarm()
  clock.advance(DRAWER_NAVIGATION_COVER_CAP_MS)
  assert.deepEqual(releases, [true])
})

test('disarming an unarmed cap is safe on every path', () => {
  // Teardown, a plain navigation, and readiness for a chat that was never
  // covered all reach disarm without a live bound.
  const { cap, clock, releases } = coverUnderTest()
  cap.disarm()
  cap.disarm()
  assert.equal(cap.armed, false)
  clock.advance(DRAWER_NAVIGATION_COVER_CAP_MS)
  assert.deepEqual(releases, [])
})

test('the bound is long enough to outlast an ordinary destination handoff', () => {
  // It is a dead-man release, not a competing animation timer: it must never
  // cut a normal paint handoff short.
  assert.ok(DRAWER_NAVIGATION_COVER_CAP_MS >= 5000,
    'the cap must cover the readiness contract\'s longest absolute deadline')
})
