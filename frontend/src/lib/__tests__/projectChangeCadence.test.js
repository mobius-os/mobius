import test from 'node:test'
import assert from 'node:assert/strict'

import {
  PROJECT_CHANGES_ACTIVE_MS,
  PROJECT_CHANGES_FAILURE_MAX_MS,
  PROJECT_CHANGES_IDLE_MAX_MS,
  createProjectChangePollTimer,
  nextProjectChangesDelay,
} from '../projectChangeCadence.js'

function fakeClock() {
  let now = 0
  let nextId = 0
  const tasks = new Map()
  return {
    now: () => now,
    setTimeout(callback, wait) {
      const id = ++nextId
      tasks.set(id, { at: now + wait, callback })
      return id
    },
    clearTimeout: id => tasks.delete(id),
    advance(ms) {
      const until = now + ms
      while (true) {
        const next = [...tasks].sort((a, b) => a[1].at - b[1].at)[0]
        if (!next || next[1].at > until) break
        now = next[1].at
        tasks.delete(next[0])
        next[1].callback()
      }
      now = until
    },
    pending: () => tasks.size,
  }
}

test('an idle open project stretches its change poll to a bounded idle rate', () => {
  let delay = PROJECT_CHANGES_ACTIVE_MS
  const waits = []
  for (let i = 0; i < 10; i += 1) {
    delay = nextProjectChangesDelay(delay, 'unchanged')
    waits.push(delay)
  }
  assert.ok(waits.every((wait, i) => i === 0 || wait >= waits[i - 1]))
  assert.equal(waits.at(-1), PROJECT_CHANGES_IDLE_MAX_MS)
})

test('any observed change returns the poll to the active rate', () => {
  assert.equal(
    nextProjectChangesDelay(PROJECT_CHANGES_IDLE_MAX_MS, 'changed'),
    PROJECT_CHANGES_ACTIVE_MS,
  )
})

test('failures back off further than idle reads but stay bounded', () => {
  let delay = PROJECT_CHANGES_ACTIVE_MS
  for (let i = 0; i < 10; i += 1) delay = nextProjectChangesDelay(delay, 'failed')
  assert.equal(delay, PROJECT_CHANGES_FAILURE_MAX_MS)
  assert.equal(
    nextProjectChangesDelay(delay, 'unchanged'),
    PROJECT_CHANGES_IDLE_MAX_MS,
  )
})

test('repeated pushes never postpone a pending cursor poll', () => {
  const clock = fakeClock()
  const polls = []
  const timer = createProjectChangePollTimer(clock, () => polls.push(clock.now()))
  timer.schedule(PROJECT_CHANGES_ACTIVE_MS)
  for (let i = 0; i < 10; i += 1) {
    clock.advance(200)
    timer.schedule(PROJECT_CHANGES_ACTIVE_MS)
  }
  assert.equal(clock.pending(), 1)
  clock.advance(500)
  assert.deepEqual(polls, [PROJECT_CHANGES_ACTIVE_MS])
  assert.equal(clock.pending(), 0)
})

test('push wakes an idle poll early; visibility cleanup cancels it', () => {
  const clock = fakeClock()
  const polls = []
  const timer = createProjectChangePollTimer(clock, () => polls.push(clock.now()))
  timer.schedule(PROJECT_CHANGES_IDLE_MAX_MS)
  clock.advance(1_000)
  timer.schedule(PROJECT_CHANGES_ACTIVE_MS)
  clock.advance(PROJECT_CHANGES_ACTIVE_MS)
  assert.deepEqual(polls, [3_500])
  timer.schedule(PROJECT_CHANGES_IDLE_MAX_MS)
  timer.cancel() // hidden or unmounted
  clock.advance(PROJECT_CHANGES_IDLE_MAX_MS)
  assert.deepEqual(polls, [3_500])
  timer.schedule(0) // visible again
  clock.advance(0)
  assert.deepEqual(polls, [3_500, 23_500])
})

test('a completed request schedules once after its timer fires', () => {
  const clock = fakeClock()
  let polls = 0
  const timer = createProjectChangePollTimer(clock, () => {
    polls += 1
    timer.schedule(PROJECT_CHANGES_ACTIVE_MS)
  })
  timer.schedule(PROJECT_CHANGES_ACTIVE_MS)
  clock.advance(PROJECT_CHANGES_ACTIVE_MS * 3)
  assert.equal(polls, 3)
  assert.equal(clock.pending(), 1)
})
