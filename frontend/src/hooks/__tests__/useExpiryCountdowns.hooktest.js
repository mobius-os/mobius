import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import useExpiryCountdowns from '../useExpiryCountdowns.js'

const START = Date.parse('2026-10-27T12:00:00Z')
const MIN = 60 * 1000
const HOUR = 60 * MIN
const DAY = 24 * HOUR

// A row as the server sends it, with `left` milliseconds of its window remaining.
const row = (id, left) => ({ id, name: `App ${id}`, seconds_left: left / 1000 })
const labels = hook => hook.result.current.map(({ row: r, countdown }) => `${r.id}:${countdown.label}`)

test('rows that arrive between wakes are timed against the real clock', t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: START })
  const hook = renderHook(useExpiryCountdowns, [row(1, 3 * DAY + 5 * HOUR)], START)
  assert.deepEqual(labels(hook), ['1:3 days left'])

  // Two hours pass with no wake due, then a refetch brings a row with half an hour left.
  t.mock.timers.tick(2 * HOUR)
  hook.rerender([row(1, 3 * DAY + 3 * HOUR), row(2, 30 * MIN)], Date.now())
  t.mock.timers.tick(0)
  assert.deepEqual(labels(hook), ['1:3 days left', '2:~1 hr left'])

  t.mock.timers.tick(30 * MIN - 1)
  assert.deepEqual(labels(hook), ['1:3 days left', '2:~1 hr left'])
  t.mock.timers.tick(1)
  assert.deepEqual(labels(hook), ['1:3 days left'], 'the row is gone the moment its window closes')
  hook.unmount()
})

test('a list that starts empty does not keep its mount time for later rows', t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: START })
  const hook = renderHook(useExpiryCountdowns, [], START)
  assert.deepEqual(labels(hook), [])

  t.mock.timers.tick(3 * HOUR)
  hook.rerender([row(5, 10 * MIN)], Date.now())
  t.mock.timers.tick(0)
  assert.deepEqual(labels(hook), ['5:~1 hr left'])

  t.mock.timers.tick(10 * MIN)
  assert.deepEqual(labels(hook), [])
  hook.unmount()
})

test('each label changes exactly at its step', t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: START })
  const hook = renderHook(useExpiryCountdowns, [row(1, DAY + HOUR)], START)
  assert.deepEqual(labels(hook), ['1:1 day left'])
  t.mock.timers.tick(HOUR)
  assert.deepEqual(labels(hook), ['1:1 day left'])
  t.mock.timers.tick(1)
  assert.deepEqual(labels(hook), ['1:~12 hrs left'])
  t.mock.timers.tick(18 * HOUR - 1)
  assert.deepEqual(labels(hook), ['1:~6 hrs left'])
  t.mock.timers.tick(5 * HOUR)
  assert.deepEqual(labels(hook), ['1:~1 hr left'])
  t.mock.timers.tick(HOUR)
  assert.deepEqual(labels(hook), [])
  hook.unmount()
})
