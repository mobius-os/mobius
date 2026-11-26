import assert from 'node:assert/strict'
import test from 'node:test'
import { apiDate, formatDateTime, formatTime, parseApiTimestamp } from '../dateTimeFormat.js'

test('shared Möbius dates use ordinal day, short month, year, and 24-hour local time', () => {
  assert.equal(formatDateTime(new Date(2026, 8, 11, 15, 7)), '11th Sep 2026, 15:07')
  assert.equal(formatDateTime(new Date(2026, 8, 1, 0, 4)), '1st Sep 2026, 00:04')
  assert.equal(formatDateTime(new Date(2026, 8, 12, 13, 0)), '12th Sep 2026, 13:00')
  assert.equal(formatDateTime(new Date(2026, 8, 13, 23, 59)), '13th Sep 2026, 23:59')
  assert.equal(formatTime(new Date(2026, 8, 11, 15, 7)), '15:07')
  assert.equal(formatDateTime('not a date'), '')
  assert.equal(formatDateTime(null), '')
  assert.equal(formatTime(undefined), '')
})

test('recent moments read as how long ago, older ones as their date', async () => {
  const { formatRecentDateTime, formatDateTime } = await import('../dateTimeFormat.js')
  const now = new Date(2026, 10, 8, 12, 0).getTime()
  assert.equal(formatRecentDateTime(now - 20_000, now), 'Just now')
  assert.equal(formatRecentDateTime(now - 23 * 60_000, now), '23 minutes ago')
  assert.equal(formatRecentDateTime(now - 60_000, now), '1 minute ago')
  assert.equal(formatRecentDateTime(now - 5 * 3_600_000 - 59 * 60_000, now), '5 hours ago')
  const dayOld = now - 86_400_000
  assert.equal(formatRecentDateTime(dayOld, now), formatDateTime(dayOld))
  assert.equal(formatRecentDateTime(now + 20_000, now), 'Just now', 'a server clock slightly ahead is still now')
  assert.equal(formatRecentDateTime(now + 120_000, now), formatDateTime(now + 120_000), 'a moment well in the future shows its date')
  assert.equal(formatRecentDateTime(null, now), '')
})

test('API timestamps without a zone are UTC, and an explicit zone is kept', () => {
  assert.equal(parseApiTimestamp('2026-09-09T12:00:00'), Date.UTC(2026, 8, 9, 12))
  assert.equal(parseApiTimestamp('2026-11-23T12:00:00.123456'), Date.UTC(2026, 10, 23, 12, 0, 0, 123))
  assert.equal(parseApiTimestamp('2026-09-09T12:00'), Date.UTC(2026, 8, 9, 12))
  assert.equal(parseApiTimestamp('2026-09-09T12:00:00+01:00'), Date.UTC(2026, 8, 9, 11))
  assert.equal(parseApiTimestamp('2026-09-09T12:00:00Z'), Date.UTC(2026, 8, 9, 12))
  assert.ok(Number.isNaN(parseApiTimestamp('')))
  assert.ok(Number.isNaN(parseApiTimestamp(undefined)))
  assert.equal(apiDate('2026-09-09T12:00:00').toISOString(), '2026-09-09T12:00:00.000Z')
  assert.equal(apiDate(Date.UTC(2026, 8, 9)).getTime(), Date.UTC(2026, 8, 9))
  assert.equal(apiDate('not a date'), null)
})
