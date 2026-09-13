import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  HISTORY_PREFETCH_VIEWPORTS,
  olderHistoryRetryShown,
  olderHistoryShouldLoad,
  paginationViewportCompensationAllowed,
} from '../scroll/policy.js'

function scrollEl({ scrollHeight, scrollTop, clientHeight }) {
  return { scrollHeight, scrollTop, clientHeight }
}

test('older history prefetches several viewports ahead and fills a short page', () => {
  assert.equal(olderHistoryShouldLoad(scrollEl({
    scrollHeight: 8000,
    scrollTop: 800 * HISTORY_PREFETCH_VIEWPORTS,
    clientHeight: 800,
  }), { userDriven: true }), true)
  assert.equal(olderHistoryShouldLoad(scrollEl({
    scrollHeight: 8000,
    scrollTop: 800 * HISTORY_PREFETCH_VIEWPORTS + 1,
    clientHeight: 800,
  }), { userDriven: true }), false)
  assert.equal(olderHistoryShouldLoad(scrollEl({
    scrollHeight: 800, scrollTop: 0, clientHeight: 800,
  })), true)
  assert.equal(olderHistoryShouldLoad(scrollEl({
    scrollHeight: 2000, scrollTop: 0, clientHeight: 800,
  })), false)
})

test('pagination compensation preserves the same reader generation under touch', () => {
  assert.equal(paginationViewportCompensationAllowed({
    capturedVersion: 7,
    currentVersion: 7,
  }), true)
  assert.equal(paginationViewportCompensationAllowed({
    capturedVersion: 7,
    currentVersion: 8,
  }), false)
})

test('failed pagination exposes retry only while older pages remain', () => {
  assert.equal(olderHistoryRetryShown(true, 20), true)
  assert.equal(olderHistoryRetryShown(false, 20), false)
  assert.equal(olderHistoryRetryShown(true, 0), false)
})
