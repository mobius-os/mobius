import test from 'node:test'
import assert from 'node:assert/strict'
import { collectDom } from '../perfProbe.js'

test('the interval performance probe never walks computed styles', () => {
  const previousDocument = globalThis.document
  let censusCalls = 0
  globalThis.document = {
    getElementsByTagName(selector) {
      censusCalls += 1
      assert.equal(selector, '*')
      return { length: 3588 }
    },
    querySelectorAll() {
      assert.fail('a passive sample must not walk the document twice')
    },
  }
  try {
    assert.deepEqual(collectDom(), { nodeCount: 3588 })
    assert.equal(censusCalls, 1)
  } finally {
    globalThis.document = previousDocument
  }
})
