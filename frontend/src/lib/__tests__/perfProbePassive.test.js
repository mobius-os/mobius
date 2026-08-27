import test from 'node:test'
import assert from 'node:assert/strict'
import { collectAnimationsForSample, collectDom } from '../perfProbe.js'

test('automatic performance samples never enumerate live animations', () => {
  const previousDocument = globalThis.document
  globalThis.document = {
    getAnimations() {
      assert.fail('an automatic sample must not force an animation census')
    },
  }
  try {
    for (const reason of ['initial', 'interval', 'hidden']) {
      assert.equal(collectAnimationsForSample(reason), null)
    }
  } finally {
    globalThis.document = previousDocument
  }
})

test('an explicit manual sample can request the live animation census', () => {
  const previousDocument = globalThis.document
  globalThis.document = {
    getAnimations() {
      return [
        { playState: 'running', animationName: 'pulse' },
        { playState: 'paused', animationName: 'idle' },
      ]
    },
  }
  try {
    const census = collectAnimationsForSample('manual')
    assert.equal(census.runningCount, 1)
    assert.deepEqual({ ...census.byName }, { pulse: 1 })
  } finally {
    globalThis.document = previousDocument
  }
})

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
