import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('optimistic steer restore only hydrates when no queue mutation won the race', () => {
  assert.match(
    source,
    /const queueAfterOptimisticPromote = pendingQueue\.pendingMessagesRef\.current/,
    'handleSteer should snapshot the queue array immediately after optimistic promote',
  )
  assert.match(
    source,
    /function restoreOptimisticSteerQueue|const restoreOptimisticSteerQueue = \(\) =>/,
    'handleSteer should route failed optimistic restores through a helper',
  )
  assert.match(
    source,
    /pendingQueue\.pendingMessagesRef\.current === queueAfterOptimisticPromote[\s\S]*?pendingQueue\.hydrate\(confirmedSnapshot, \{ preserveMissing: true \}\)/,
    'the restore helper must hydrate the stale snapshot only if the queue array identity is unchanged',
  )
})
