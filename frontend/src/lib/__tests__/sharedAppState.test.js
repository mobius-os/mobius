import test from 'node:test'
import assert from 'node:assert/strict'

import { normalizeSharedAppSnapshot } from '../sharedAppState.js'


test('a late shared-state refresh cannot replace a newer mutation', () => {
  const current = {
    cursor: 12,
    values: { 'board.json': ['new'] },
    versions: { 'board.json': 'new-version' },
  }

  assert.equal(normalizeSharedAppSnapshot(current, {
    cursor: 11,
    values: { 'board.json': ['old'] },
    versions: { 'board.json': 'old-version' },
  }), null)
})


test('an equal or newer shared-state snapshot is normalized for adoption', () => {
  assert.deepEqual(normalizeSharedAppSnapshot({ cursor: 2 }, {
    cursor: 3,
    values: { 'board.json': ['current'] },
    versions: { 'board.json': 'v3' },
  }), {
    cursor: 3,
    values: { 'board.json': ['current'] },
    versions: { 'board.json': 'v3' },
  })
})
