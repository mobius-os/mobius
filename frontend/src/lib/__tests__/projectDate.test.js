// Project list dates use relative recency before switching to a compact date.
import test from 'node:test'
import assert from 'node:assert/strict'
import { formatProjectDate } from '../relativeTime.js'

test('project dates are relative for less than seven days and date-only afterwards', () => {
  const old = new Date(2026, 9, 20, 12)
  const sevenDays = 7 * 24 * 60 * 60 * 1000
  assert.equal(formatProjectDate(old.toISOString(), old.getTime() + sevenDays - 1), '6 days ago')
  assert.equal(formatProjectDate(old.toISOString(), old.getTime() + sevenDays), '20 Oct 2026')
})

test('project dates handle naive UTC timestamps and missing or invalid values', () => {
  assert.equal(formatProjectDate('2026-10-27T14:00:00', Date.parse('2026-10-27T15:00:00Z')), '1 hour ago')
  assert.equal(formatProjectDate(null), '')
  assert.equal(formatProjectDate('invalid'), '')
})
