import { test } from 'node:test'
import assert from 'node:assert/strict'
import { formatResetTime } from '../resetTime.js'

// formatResetTime turns an explicit-UTC pause.resets_at into a viewer-local,
// day-qualified label that splices naturally after "Resets" / "resets".

// A local Date at noon on a given day offset. Noon is always the same calendar
// day it was constructed for, so the day bucket is deterministic no matter what
// wall-clock time the test runs at (unlike a "+2h" offset near midnight).
function localNoon(dayOffset) {
  const d = new Date()
  d.setDate(d.getDate() + dayOffset)
  d.setHours(12, 0, 0, 0)
  return d.toISOString()
}

test('null / unparseable input degrades to null (card shows just the message)', () => {
  assert.equal(formatResetTime(null), null)
  assert.equal(formatResetTime(''), null)
  assert.equal(formatResetTime('not-a-date'), null)
})

test('a same-day reset reads "at <time>" — splices to "Resets at …"', () => {
  const label = formatResetTime(localNoon(0))
  assert.match(label, /^at \d/, label)
  assert.doesNotMatch(label, /tomorrow|Mon|Tue|Wed|Thu|Fri|Sat|Sun/, label)
})

test('a next-day reset reads "tomorrow at <time>"', () => {
  const label = formatResetTime(localNoon(1))
  assert.match(label, /^tomorrow at \d/, label)
})

test('a reset several days out reads "<weekday> at <time>"', () => {
  const label = formatResetTime(localNoon(3))
  assert.match(label, /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) at \d/, label)
})

test('the label reads naturally after "Resets"', () => {
  const line = `Resets ${formatResetTime(localNoon(1))}`
  assert.equal(line.startsWith('Resets tomorrow at '), true, line)
})
