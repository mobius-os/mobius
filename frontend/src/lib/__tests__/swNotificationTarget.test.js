import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const SOURCE = readFileSync(
  new URL('../../../public/sw-push.js', import.meta.url),
  'utf8',
)
const BADGE_PNG = readFileSync(
  new URL('../../../public/icons/notification-badge.png', import.meta.url),
)
const BADGE_SVG = readFileSync(
  new URL('../../../public/icons/notification-badge.svg', import.meta.url),
  'utf8',
)

// _safeTarget is the notification-click allowlist: it normalizes whatever a
// notification carried in `data.target` down to a shell route we are willing
// to open. It lives in the push worker (public/sw-push.js), which owns push
// and notificationclick. Extract it from source (same approach as the denylist
// test) rather than importing the worker, which needs a
// ServiceWorkerGlobalScope.
function loadSafeTarget() {
  const start = SOURCE.indexOf('function _safeTarget(raw) {')
  assert.ok(start !== -1, '_safeTarget exists in sw-push.js')
  let depth = 0
  let end = -1
  for (let i = SOURCE.indexOf('{', start); i < SOURCE.length; i += 1) {
    if (SOURCE[i] === '{') depth += 1
    else if (SOURCE[i] === '}') {
      depth -= 1
      if (depth === 0) { end = i + 1; break }
    }
  }
  assert.ok(end !== -1, '_safeTarget body is balanced')
  // _safeTarget compares absolute targets against the worker's own origin, so
  // give it one.
  return Function(
    'self',
    `"use strict"; ${SOURCE.slice(start, end)}; return _safeTarget`,
  )({ location: { origin: 'https://mobius.test' } })
}

const safeTarget = loadSafeTarget()

test('push worker uses a dedicated transparent 96px status-bar badge', () => {
  assert.match(SOURCE, /badge:\s*['"]\/icons\/notification-badge\.png['"]/)
  assert.deepEqual(
    [...BADGE_PNG.subarray(0, 8)],
    [137, 80, 78, 71, 13, 10, 26, 10],
  )
  assert.equal(BADGE_PNG.readUInt32BE(16), 96)
  assert.equal(BADGE_PNG.readUInt32BE(20), 96)
  assert.equal(BADGE_PNG[25], 6, 'badge PNG must retain its RGBA transparency')
  assert.match(BADGE_SVG, /viewBox="0\.00 0\.00 96\.00 96\.00"/)
})

test('an app deep-link keeps the intent naming which item to open', () => {
  // The agent links artifacts as /shell/?app=artifacts&intent=artifact:<id>,
  // and sends the SAME url as a notification target. Dropping the intent here
  // opened the app's index instead of the artifact the notification was about.
  assert.equal(
    safeTarget('/shell/?app=artifacts&intent=artifact:tip-calculator-7f3a'),
    '/shell/?app=artifacts&intent=artifact%3Atip-calculator-7f3a',
  )
})

test('intent rides a same-origin absolute target and a numeric app id', () => {
  assert.equal(
    safeTarget('https://mobius.test/shell/?app=88&intent=artifact:deck-01'),
    '/shell/?app=88&intent=artifact%3Adeck-01',
  )
  assert.equal(
    safeTarget('HTTPS://mobius.test/shell/?chat=abc'),
    '/shell/?chat=abc',
  )
})

test('a malformed intent is dropped but the app still opens', () => {
  // Fail soft: the app is still a safe destination, so only the unusable
  // intent is discarded.
  for (const bad of ['a b', '../../etc', 'x'.repeat(129), '<script>']) {
    assert.equal(
      safeTarget(`/shell/?app=artifacts&intent=${encodeURIComponent(bad)}`),
      '/shell/?app=artifacts',
    )
  }
})

test('targets without an intent are unchanged', () => {
  assert.equal(safeTarget('/shell/?app=artifacts'), '/shell/?app=artifacts')
  assert.equal(safeTarget('/shell/?chat=abc'), '/shell/?chat=abc')
  assert.equal(safeTarget('/shell/'), '/shell/')
})

test('an intent cannot smuggle in a different destination', () => {
  // chat wins only when there is no app; and an intent never widens the route.
  assert.equal(
    safeTarget('/shell/?chat=abc&intent=artifact:x'),
    '/shell/?chat=abc',
  )
  // The intent is encoded, so an injected separator cannot append a param.
  assert.equal(
    safeTarget('/shell/?app=artifacts&intent=' + encodeURIComponent('a&chat=evil')),
    '/shell/?app=artifacts',
  )
})

test('out-of-scope targets still fall back to root', () => {
  assert.equal(safeTarget('https://evil.test/phish?app=artifacts'), '/')
  // A cross-origin target is refused even when it mimics a valid shell route.
  assert.equal(
    safeTarget('https://evil.test/shell/?app=artifacts&intent=artifact:x'), '/',
  )
  assert.equal(safeTarget('/app/5'), '/')
  assert.equal(safeTarget('javascript:alert(1)'), '/')
})
