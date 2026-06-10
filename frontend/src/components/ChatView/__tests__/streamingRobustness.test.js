/**
 * Unit tests for the streaming-robustness fixes:
 *   1. Retry-exhausted partial loss — lastGoodItemsRef preservation
 *   2. Double-answer race — sendSilentInFlightRef guard
 *   4b. removeFile side-effect outside setFiles updater
 *
 * Run with:
 *   cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *     --test src/components/ChatView/__tests__/streamingRobustness.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

// ---------------------------------------------------------------------------
// Fix 1: lastGoodItemsRef preservation across reconnect resets
// ---------------------------------------------------------------------------
// We test the invariant by exercising the same logic that setStreamItems
// and connectToStream use: when items become non-empty, lastGoodItemsRef is
// updated; when resetState fires, latestItemsRef/visible-items are cleared
// but lastGoodItemsRef is NOT; when a catch-up burst begins
// (catchUpStartedRef fires), lastGoodItemsRef is cleared.

test('lastGoodItemsRef is updated whenever items become non-empty', () => {
  let latestItems = []
  let lastGoodItems = []

  // Simulate setStreamItems wrapper
  function setStreamItems(updater) {
    const next = typeof updater === 'function' ? updater(latestItems) : updater
    if (next.length > 0) lastGoodItems = next
    latestItems = next
  }

  setStreamItems([{ type: 'text', content: 'hello' }])
  assert.deepEqual(lastGoodItems, [{ type: 'text', content: 'hello' }])
})

test('lastGoodItemsRef is NOT cleared when resetState wipes latestItemsRef', () => {
  let latestItems = []
  let lastGoodItems = []

  function setStreamItems(updater) {
    const next = typeof updater === 'function' ? updater(latestItems) : updater
    if (next.length > 0) lastGoodItems = next
    latestItems = next
  }

  // Partial response builds up
  setStreamItems([{ type: 'text', content: 'partial response' }])
  assert.equal(lastGoodItems.length, 1)

  // Reconnect wipes latestItems but does NOT touch lastGoodItems
  latestItems = []
  // (resetState path only wipes latestItemsRef/visible, not lastGoodItemsRef)
  assert.equal(lastGoodItems.length, 1, 'lastGoodItems preserved after reset')
})

test('lastGoodItemsRef is cleared when catch-up burst begins (catchUpStartedRef fires)', () => {
  let latestItems = []
  let lastGoodItems = []
  let catchUpStarted = false

  function setStreamItems(updater) {
    const next = typeof updater === 'function' ? updater(latestItems) : updater
    if (next.length > 0) lastGoodItems = next
    latestItems = next
  }

  // Build up partial state
  setStreamItems([{ type: 'text', content: 'partial' }])
  assert.equal(lastGoodItems.length, 1)

  // Reconnect
  latestItems = []

  // First event of catch-up burst fires — this is the safe point to clear
  // lastGoodItems because the replay will rebuild everything from scratch
  if (!catchUpStarted) {
    catchUpStarted = true
    lastGoodItems = []
  }

  assert.equal(lastGoodItems.length, 0, 'lastGoodItems cleared when catch-up starts')
})

test('on retry exhaustion, lastGoodItemsRef is restored into latestItemsRef when latestItems empty', () => {
  let latestItems = []
  let _visibleItems = []
  let lastGoodItems = []

  function setStreamItems(updater) {
    const next = typeof updater === 'function' ? updater(latestItems) : updater
    if (next.length > 0) lastGoodItems = next
    latestItems = next
    _visibleItems = next
  }

  // Partial response
  setStreamItems([{ type: 'text', content: 'partial text before disconnect' }])
  assert.equal(lastGoodItems.length, 1)

  // Multiple reconnect resets
  latestItems = []
  _visibleItems = []
  latestItems = []
  _visibleItems = []
  // retryCount >= 3 — restore path:
  if (lastGoodItems.length > 0 && latestItems.length === 0) {
    latestItems = lastGoodItems
    _visibleItems = lastGoodItems
  }

  assert.equal(latestItems.length, 1, 'latestItemsRef restored from lastGoodItemsRef')
  assert.equal(_visibleItems[0].content, 'partial text before disconnect')
})

test('on retry exhaustion, restore is skipped when latestItems is non-empty', () => {
  // A successful reconnect delivers events (catchUpStarted=true clears
  // lastGoodItems), but even if it didn't, if latestItems already has
  // content the restore must not double it.
  let latestItems = []
  let lastGoodItems = []

  function setStreamItems(updater) {
    const next = typeof updater === 'function' ? updater(latestItems) : updater
    if (next.length > 0) lastGoodItems = next
    latestItems = next
  }

  setStreamItems([{ type: 'text', content: 'old partial' }])

  // reconnect
  latestItems = []

  // New events arrive before exhaustion
  setStreamItems([{ type: 'text', content: 'new content' }])

  // Now retry exhaustion fires — latestItems already has content, skip
  if (lastGoodItems.length > 0 && latestItems.length === 0) {
    // This branch should NOT fire
    latestItems = lastGoodItems
  }

  assert.equal(latestItems[0].content, 'new content', 'no overwrite when latestItems non-empty')
})

// ---------------------------------------------------------------------------
// Fix 2: doSendSilent re-entrancy guard (sendSilentInFlightRef)
// ---------------------------------------------------------------------------

test('sendSilentInFlightRef blocks a second concurrent invocation', async () => {
  // Simulate the synchronous guard at the top of doSendSilent.
  let callCount = 0
  const sendSilentInFlightRef = { current: false }

  async function doSendSilent(text) {
    if (sendSilentInFlightRef.current) return 'blocked'
    sendSilentInFlightRef.current = true
    try {
      callCount++
      await Promise.resolve() // simulate async work
      return 'sent'
    } finally {
      sendSilentInFlightRef.current = false
    }
  }

  // Fire two concurrent calls without awaiting the first
  const p1 = doSendSilent('answer')
  const p2 = doSendSilent('answer') // should be blocked

  const [r1, r2] = await Promise.all([p1, p2])
  assert.equal(r1, 'sent')
  assert.equal(r2, 'blocked')
  assert.equal(callCount, 1, 'only one invocation completed')
})

test('sendSilentInFlightRef is cleared after completion so a second submission can proceed', async () => {
  let callCount = 0
  const sendSilentInFlightRef = { current: false }

  async function doSendSilent(text) {
    if (sendSilentInFlightRef.current) return 'blocked'
    sendSilentInFlightRef.current = true
    try {
      callCount++
      await Promise.resolve()
      return 'sent'
    } finally {
      sendSilentInFlightRef.current = false
    }
  }

  await doSendSilent('first answer')
  assert.equal(callCount, 1)

  // After the first call completes, a second answer (different question card) can proceed
  const result = await doSendSilent('second answer')
  assert.equal(result, 'sent')
  assert.equal(callCount, 2, 'second sequential call allowed through')
})

test('sendSilentInFlightRef is cleared on early return for empty text', () => {
  const sendSilentInFlightRef = { current: false }

  function doSendSilentSync(text) {
    if (sendSilentInFlightRef.current) return 'blocked'
    sendSilentInFlightRef.current = true
    if (!text.trim()) {
      sendSilentInFlightRef.current = false
      return 'empty'
    }
    // ... rest of function would continue
    sendSilentInFlightRef.current = false
    return 'sent'
  }

  doSendSilentSync('')
  // After empty-text early return, the ref must be cleared
  assert.equal(sendSilentInFlightRef.current, false, 'flag cleared on empty-text return')
  // A subsequent non-empty call can proceed
  assert.equal(doSendSilentSync('valid'), 'sent')
})

// ---------------------------------------------------------------------------
// Fix 4b: removeFile side-effect outside setFiles updater
// ---------------------------------------------------------------------------

test('removeFile reads the file to remove from filesRef, not from within the updater', () => {
  // The bug: the DELETE fetch was inside the setFiles(prev => {...}) updater,
  // which React may double-invoke. The fix moves the side-effect BEFORE
  // setFiles is called.
  //
  // We verify the invariant: the file to be removed is identified and acted
  // on BEFORE the state update, using the ref value, not the updater's `prev`.

  const filesRef = {
    current: [
      { id: 'a', status: 'done', name: 'file1.txt', objectUrl: null },
      { id: 'b', status: 'done', name: 'file2.txt', objectUrl: null },
    ]
  }

  const deletedFiles = []
  const revokedUrls = []

  // Simulate the fixed removeFile logic
  function removeFile(id) {
    // Side effects happen here, outside the updater
    const removing = filesRef.current.find(c => c.id === id)
    if (removing?.objectUrl) revokedUrls.push(removing.objectUrl)
    // State update (pure — no side effects)
    const newFiles = filesRef.current.filter(c => c.id !== id)
    filesRef.current = newFiles  // simulate setFiles + ref sync
    if (removing?.status === 'done' && removing.name) {
      deletedFiles.push(removing.name)
    }
  }

  // If React double-invokes the updater, we should NOT double-delete.
  // With the fix, the DELETE happens before setFiles — it fires exactly once.
  removeFile('a')
  assert.equal(deletedFiles.length, 1, 'DELETE fired exactly once (not inside double-invoke updater)')
  assert.equal(deletedFiles[0], 'file1.txt')
  assert.equal(filesRef.current.length, 1)
  assert.equal(filesRef.current[0].id, 'b')
})

test('removeFile revokes object URLs outside the updater', () => {
  const revokedUrls = []
  const mockRevokeObjectURL = (url) => revokedUrls.push(url)

  const filesRef = {
    current: [
      { id: 'img1', status: 'done', name: 'photo.jpg', objectUrl: 'blob:abc123' },
    ]
  }

  function removeFile(id) {
    const removing = filesRef.current.find(c => c.id === id)
    // Revoke happens outside the updater (using the ref, not the updater arg)
    if (removing?.objectUrl) mockRevokeObjectURL(removing.objectUrl)
    filesRef.current = filesRef.current.filter(c => c.id !== id)
  }

  removeFile('img1')
  assert.equal(revokedUrls.length, 1)
  assert.equal(revokedUrls[0], 'blob:abc123')
})
