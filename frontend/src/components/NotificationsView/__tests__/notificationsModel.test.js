import assert from 'node:assert/strict'
import { test } from 'node:test'

import { formatRelativeTime } from '../notificationsModel.js'

const NOW = Date.parse('2026-07-27T02:54:00Z')

test('relative notification time honors an explicit UTC offset', () => {
  assert.equal(
    formatRelativeTime('2026-07-27T02:53:30+00:00', NOW),
    'now',
  )
})

test('relative time treats server datetimes without a zone as UTC', () => {
  const previousTimezone = process.env.TZ
  process.env.TZ = 'Pacific/Auckland'
  try {
    assert.equal(
      formatRelativeTime('2026-07-27T02:53:30.123456', NOW),
      'now',
    )
  } finally {
    if (previousTimezone === undefined) delete process.env.TZ
    else process.env.TZ = previousTimezone
  }
})

test('relative notification time keeps stable boundary labels', () => {
  assert.equal(formatRelativeTime('2026-07-27T02:53:00Z', NOW), '1m ago')
  assert.equal(formatRelativeTime('2026-07-27T01:54:00Z', NOW), '1h ago')
  assert.equal(formatRelativeTime('2026-07-26T02:54:00Z', NOW), '1d ago')
})

test('future clock skew and invalid values degrade safely', () => {
  assert.equal(formatRelativeTime('2026-07-27T03:54:00Z', NOW), 'now')
  assert.equal(formatRelativeTime('not-a-date', NOW), '')
})
