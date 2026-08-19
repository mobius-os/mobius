import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  olderHistoryRetryShown,
  olderHistoryShouldLoad,
} from '../scroll/policy.js'

function scrollEl({ scrollHeight, scrollTop, clientHeight }) {
  return { scrollHeight, scrollTop, clientHeight }
}

test('older history prefetches near its boundary and fills a short page', () => {
  assert.equal(olderHistoryShouldLoad(scrollEl({
    scrollHeight: 2000, scrollTop: 240, clientHeight: 800,
  }), { userDriven: true }), true)
  assert.equal(olderHistoryShouldLoad(scrollEl({
    scrollHeight: 2000, scrollTop: 600, clientHeight: 800,
  }), { userDriven: true }), false)
  assert.equal(olderHistoryShouldLoad(scrollEl({
    scrollHeight: 800, scrollTop: 0, clientHeight: 800,
  })), true)
  assert.equal(olderHistoryShouldLoad(scrollEl({
    scrollHeight: 2000, scrollTop: 0, clientHeight: 800,
  })), false)
})

test('failed pagination exposes retry only while older pages remain', () => {
  assert.equal(olderHistoryRetryShown(true, 20), true)
  assert.equal(olderHistoryRetryShown(false, 20), false)
  assert.equal(olderHistoryRetryShown(true, 0), false)
})
