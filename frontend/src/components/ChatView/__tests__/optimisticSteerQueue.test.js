import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('optimistic steer restore only hydrates when no queue mutation won the race', () => {
  assert.match(
    source,
    /queueAfterOptimisticPromote = pendingQueue\.pendingMessagesRef\.current/,
    'handleSteer should snapshot the queue array immediately after optimistic promote',
  )
  assert.match(
    source,
    /function restoreOptimisticSteerQueue|const restoreOptimisticSteerQueue = \(\) =>/,
    'handleSteer should route failed optimistic restores through a helper',
  )
  const restoreHelper = source.indexOf('function restoreOptimisticSteerQueue')
  // Indent-agnostic: the steer core's nesting depth changed when per-row
  // steer extracted it out of handleSteer; the contract is only that a try
  // block FOLLOWS the helper declaration (so catch can call it).
  const guardedRequest = source.slice(restoreHelper).search(/\n\s+try \{/)
  assert.ok(
    restoreHelper >= 0 && guardedRequest > 0,
    'the restore helper must be declared outside the request try block so catch can call it',
  )
  assert.match(
    source,
    // The snapshot identifier is whatever the steer core names it (it became
    // fullConfirmedSnapshot when per-row steer landed) — the contract is the
    // identity check gating a preserveMissing hydrate, not the name.
    /pendingQueue\.pendingMessagesRef\.current === queueAfterOptimisticPromote[\s\S]*?pendingQueue\.hydrate\(\w+, \{ preserveMissing: true \}\)/,
    'the restore helper must hydrate the stale snapshot only if the queue array identity is unchanged',
  )
})
