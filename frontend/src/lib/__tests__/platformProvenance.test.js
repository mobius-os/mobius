import test from 'node:test'
import assert from 'node:assert/strict'

import { formatUpstreamCommitDate } from '../platformProvenance.js'

test('upstream provenance presents the commit date rather than image build time', () => {
  assert.equal(
    formatUpstreamCommitDate('2026-08-26T04:42:48+00:00', 'en-GB'),
    '26 Aug 2026',
  )
  assert.equal(formatUpstreamCommitDate(null, 'en-GB'), '')
})
