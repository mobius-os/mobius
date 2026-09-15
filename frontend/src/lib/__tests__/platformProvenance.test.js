import test from 'node:test'
import assert from 'node:assert/strict'

import { formatUpstreamCommitDate } from '../platformProvenance.js'

test('renders a commit timestamp/date as its day (August is ICU-stable)', () => {
  assert.equal(
    formatUpstreamCommitDate('2026-08-26T04:42:48+00:00', 'en-GB', 'UTC'),
    '26 Aug 2026',
  )
  assert.equal(formatUpstreamCommitDate('2026-08-14', 'en-GB', 'UTC'), '14 Aug 2026')
})

test('empty, null, or unparseable input yields an empty string', () => {
  assert.equal(formatUpstreamCommitDate(null, 'en-GB', 'UTC'), '')
  assert.equal(formatUpstreamCommitDate('', 'en-GB', 'UTC'), '')
  assert.equal(formatUpstreamCommitDate('not-a-date', 'en-GB', 'UTC'), '')
})

test('the committer offset in the string does not leak into the shown date', () => {
  // The same instant (2026-09-14T23:50:13Z) written with two different offsets,
  // plus the image's bare UTC build date for that same commit. A prefix-only
  // formatter showed 15 Sep for the +02:00 form and 14 Sep for the others;
  // rendering the real instant makes all three agree for a given zone.
  const plus2 = '2026-09-15T01:50:13+02:00'
  const utc = '2026-09-14T23:50:13+00:00'
  const bareBuildDate = '2026-09-14'
  const a = formatUpstreamCommitDate(plus2, 'en-GB', 'UTC')
  const b = formatUpstreamCommitDate(utc, 'en-GB', 'UTC')
  const c = formatUpstreamCommitDate(bareBuildDate, 'en-GB', 'UTC')
  assert.equal(a, b)
  assert.equal(a, c)
})

test('renders in the requested zone; viewer-local when omitted', () => {
  // Instant 2026-09-14T23:50Z is still 14 Sep in UTC but already 15 Sep in
  // London (BST). The chosen timeZone — not the string's offset — decides.
  const ts = '2026-09-15T01:50:13+02:00'
  assert.notEqual(
    formatUpstreamCommitDate(ts, 'en-GB', 'UTC'),
    formatUpstreamCommitDate(ts, 'en-GB', 'Europe/London'),
  )
  // Omitting timeZone uses the runtime's local zone (whatever the viewer's is).
  assert.equal(typeof formatUpstreamCommitDate(ts, 'en-GB'), 'string')
})
