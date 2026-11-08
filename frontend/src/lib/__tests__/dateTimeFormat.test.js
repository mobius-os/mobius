import assert from 'node:assert/strict'
import test from 'node:test'
import { formatDateTime, formatTime } from '../dateTimeFormat.js'

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
  assert.equal(formatRecentDateTime(now + 120_000, now), formatDateTime(now + 120_000), 'a future clock skew shows its date')
  assert.equal(formatRecentDateTime(null, now), '')
})
