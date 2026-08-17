import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const WORKER_SOURCE = readFileSync(
  new URL('../../../public/sw-push.js', import.meta.url),
  'utf8',
)
const BADGE_PNG = readFileSync(
  new URL('../../../public/icons/notification-badge.png', import.meta.url),
)

test('push worker uses a dedicated transparent 96px status-bar badge', () => {
  assert.match(
    WORKER_SOURCE,
    /badge:\s*['"]\/icons\/notification-badge\.png['"]/,
    'the monochrome status-bar badge must remain separate from the card icon',
  )
  assert.deepEqual(
    [...BADGE_PNG.subarray(0, 8)],
    [137, 80, 78, 71, 13, 10, 26, 10],
    'badge asset must remain a PNG',
  )
  assert.equal(BADGE_PNG.readUInt32BE(16), 96)
  assert.equal(BADGE_PNG.readUInt32BE(20), 96)
  assert.equal(BADGE_PNG[25], 6, 'badge PNG must retain its RGBA transparency')
})
