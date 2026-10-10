import test from 'node:test'
import assert from 'node:assert/strict'

import {
  PROJECT_CHANGES_ACTIVE_MS,
  PROJECT_CHANGES_FAILURE_MAX_MS,
  PROJECT_CHANGES_IDLE_MAX_MS,
  createProjectChangePollTimer,
  nextProjectChangesDelay,
  startProjectChangesPoll,
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

function eventHub() {
  const listeners = new Map()
  return {
    addEventListener(name, listener) {
      if (!listeners.has(name)) listeners.set(name, new Set())
      listeners.get(name).add(listener)
    },
    removeEventListener(name, listener) { listeners.get(name)?.delete(listener) },
    emit(name, detail) {
      for (const listener of listeners.get(name) || []) listener({ detail })
    },
    count(name) { return listeners.get(name)?.size || 0 },
  }
}

const settle = async () => {
  for (let i = 0; i < 5; i += 1) await Promise.resolve()
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

test('real poll lifecycle delivers cursor-only changes despite sustained pushes', async () => {
  const clock = fakeClock()
  const events = eventHub()
  const visibility = { ...eventHub(), hidden: false }
  const reads = []
  const seen = []
  const stop = startProjectChangesPoll({
    projectId: 7, clock, events, visibility,
    readChanges: async (cursor, { signal }) => {
      reads.push({ cursor, signal, at: clock.now() })
      if (reads.length === 3) return { cursor: 3, changes: [{ path: 'durable-only.txt' }] }
      return { cursor: reads.length, changes: [] }
    },
    handleChanges: async (changes, baseline) => {
      seen.push({ changes, baseline })
      return baseline || changes.length > 0
    },
  })
  await settle() // baseline, next poll due at active rate
  clock.advance(PROJECT_CHANGES_ACTIVE_MS)
  await settle() // idle read, next poll due later
  assert.equal(reads.length, 2)
  for (let i = 0; i < 10; i += 1) {
    clock.advance(200)
    events.emit('mobius:project-change', { projectId: 7, change: { path: 'push.txt' } })
  }
  clock.advance(700)
  await settle()
  assert.equal(reads.length, 3)
  assert.equal(reads[2].at, 5_200) // first push at 2,700 + active wait
  assert.deepEqual(seen.at(-1).changes, [{ path: 'durable-only.txt' }])
  assert.equal(clock.pending(), 1)
  stop()
})

test('hide/show waits for the aborted request and ignores its late response', async () => {
  const clock = fakeClock()
  const events = eventHub()
  const visibility = { ...eventHub(), hidden: false }
  const seen = []
  const reads = []
  let resolveFirst
  const stop = startProjectChangesPoll({
    projectId: 7, clock, events, visibility,
    readChanges: (cursor, { signal }) => {
      reads.push({ cursor, signal })
      if (reads.length === 1) return new Promise(resolve => { resolveFirst = resolve })
      return Promise.resolve({ cursor: 2, changes: [{ path: 'current.txt' }] })
    },
    handleChanges: async changes => { seen.push(changes); return changes.length > 0 },
  })
  assert.equal(reads.length, 1)
  visibility.hidden = true
  visibility.emit('visibilitychange')
  assert.equal(reads[0].signal.aborted, true)
  visibility.hidden = false
  visibility.emit('visibilitychange')
  clock.advance(0)
  assert.equal(reads.length, 1) // no parallel request while abort is settling
  assert.equal(clock.pending(), 0)
  resolveFirst({ cursor: 1, changes: [{ path: 'stale.txt' }] })
  await settle()
  assert.deepEqual(seen, [])
  clock.advance(0) // resume immediately once the old request settles
  await settle()
  assert.equal(reads.length, 2)
  assert.equal(reads[1].cursor, null) // stale aborted response did not advance cursor
  assert.deepEqual(seen.at(-1), [{ path: 'current.txt' }])
  stop()
  assert.equal(clock.pending(), 0)
  assert.equal(events.count('mobius:project-change'), 0)
  assert.equal(visibility.count('visibilitychange'), 0)
})

test('a push during an in-flight empty read keeps the next cursor poll active', async () => {
  const clock = fakeClock()
  const events = eventHub()
  const visibility = { ...eventHub(), hidden: false }
  let resolveSecond
  const reads = []
  const stop = startProjectChangesPoll({
    projectId: 7, clock, events, visibility,
    readChanges: cursor => {
      reads.push({ cursor, at: clock.now() })
      if (reads.length === 2) return new Promise(resolve => { resolveSecond = resolve })
      return Promise.resolve({ cursor: reads.length, changes: [] })
    },
    handleChanges: async (changes, baseline) => baseline || changes.length > 0,
  })
  await settle()
  clock.advance(PROJECT_CHANGES_ACTIVE_MS)
  assert.equal(reads.length, 2)
  events.emit('mobius:project-change', { projectId: 7, change: { path: 'push.txt' } })
  clock.advance(2_000)
  assert.equal(reads.length, 2) // still one in-flight cursor request
  resolveSecond({ cursor: 2, changes: [] })
  await settle()
  clock.advance(PROJECT_CHANGES_ACTIVE_MS)
  await settle()
  assert.equal(reads.length, 3)
  assert.equal(reads[2].at, 7_000)
  stop()
})

test('replaced project stops old cursor IO and accepts only new project pushes', async () => {
  const clock = fakeClock()
  const events = eventHub()
  const visibility = { ...eventHub(), hidden: false }
  const reads = []
  const seen = []
  const start = projectId => startProjectChangesPoll({
    projectId, clock, events, visibility,
    readChanges: async cursor => {
      reads.push({ projectId, cursor })
      return { cursor: 1, changes: [] }
    },
    handleChanges: async changes => { seen.push({ projectId, changes }); return true },
  })
  const stopOld = start(7)
  await settle()
  stopOld()
  const stopNew = start(8)
  await settle()
  events.emit('mobius:project-change', { projectId: 7, change: { path: 'old.txt' } })
  events.emit('mobius:project-change', { projectId: 8, change: { path: 'new.txt' } })
  assert.equal(seen.filter(item => item.changes[0]?.path === 'old.txt').length, 0)
  assert.equal(seen.filter(item => item.changes[0]?.path === 'new.txt').length, 1)
  clock.advance(PROJECT_CHANGES_ACTIVE_MS)
  await settle()
  assert.deepEqual(reads.map(read => read.projectId), [7, 8, 8])
  stopNew()
  clock.advance(PROJECT_CHANGES_IDLE_MAX_MS)
  assert.equal(reads.length, 3)
})
