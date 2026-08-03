import { test } from 'node:test'
import assert from 'node:assert/strict'

import { streamCatchUpOwnerMatches } from '../useStreamConnection.js'

test('a catch-up owner matches only its chat and connection generation', () => {
  const owner = { chatId: 'a', generation: 3 }
  assert.equal(streamCatchUpOwnerMatches(owner, { chatId: 'a', generation: 3 }), true)
  assert.equal(streamCatchUpOwnerMatches(owner, { chatId: 'b', generation: 3 }), false)
  assert.equal(streamCatchUpOwnerMatches(owner, { chatId: 'a', generation: 4 }), false)
})

test('a delayed refresh from chat A cannot settle chat B', async () => {
  const ownerA = { chatId: 'a', generation: 3 }
  let current = ownerA
  let releaseRefresh
  const refresh = new Promise(resolve => { releaseRefresh = resolve })
  let settled = 0

  const completion = refresh.then(() => {
    if (streamCatchUpOwnerMatches(ownerA, current)) settled += 1
  })
  current = { chatId: 'b', generation: 4 }
  releaseRefresh()
  await completion

  assert.equal(settled, 0)
})

test('the current owner settles after either refresh outcome', async () => {
  for (const refresh of [Promise.resolve(), Promise.reject(new Error('offline'))]) {
    const owner = { chatId: 'a', generation: 5 }
    let settled = 0
    const settle = () => {
      if (streamCatchUpOwnerMatches(owner, owner)) settled += 1
    }
    await refresh.then(settle, settle)
    assert.equal(settled, 1)
  }
})
