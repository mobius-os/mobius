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
})
