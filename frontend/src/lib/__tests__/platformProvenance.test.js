import test from 'node:test'
import assert from 'node:assert/strict'

import {
  formatUpstreamCheckTime,
  formatUpstreamCommitDate,
} from '../platformProvenance.js'

test('upstream provenance presents the commit date rather than image build time', () => {
  assert.equal(
    formatUpstreamCommitDate('2026-08-26T04:42:48+00:00', 'en-GB'),
    '26 Aug 2026',
  )
  assert.equal(formatUpstreamCommitDate(null, 'en-GB'), '')
})

test('upstream provenance makes a fresh and cached check auditable', () => {
  const now = Date.parse('2026-08-26T12:40:00Z')
  assert.equal(
    formatUpstreamCheckTime('2026-08-26T12:39:45Z', now),
    'Last checked now',
  )
  assert.equal(
    formatUpstreamCheckTime('2026-08-26T12:10:00Z', now),
    'Last checked 30m ago',
  )
  assert.equal(formatUpstreamCheckTime(null, now), '')
})
