/* Every shared date label reads a naive API timestamp as UTC, so a viewer
   outside UTC sees the same moment in their own local time. */
import assert from 'node:assert/strict'
import test from 'node:test'

test('shared date labels show a naive API timestamp in the local timezone', async () => {
  const previousTimezone = process.env.TZ
  process.env.TZ = 'Europe/Paris'
  try {
    // The shared formatters capture the local zone when first imported.
    const { formatDateTime, formatTime, formatRecentDateTime } = await import('../dateTimeFormat.js')
    // A chat search result's "Last active" tooltip passes this shape as-is.
    assert.equal(formatDateTime('2026-11-23T12:00:00.123456'), '23rd Nov 2026, 13:00')
    assert.equal(formatTime('2026-11-23T12:00:00'), '13:00')
    assert.equal(formatRecentDateTime('2026-11-23T12:00:00', Date.UTC(2026, 10, 25)), '23rd Nov 2026, 13:00')
    assert.equal(formatDateTime('2026-11-23T12:00:00+00:00'), '23rd Nov 2026, 13:00')
  } finally {
    if (previousTimezone === undefined) delete process.env.TZ
    else process.env.TZ = previousTimezone
  }
})
