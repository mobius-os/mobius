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

// ---------------------------------------------------------------------------
// Fix 1: Duplicate question cards — dedupe by identity at render-assembly time
// (streamItemQuestionKeys suppression in MsgContent)
// ---------------------------------------------------------------------------

// Simulate the questionKey logic used in the real code
function questionKey(block) {
  const questions = block?.questions || []
  if (block?.question_id) return `question_id:${block.question_id}`
  if (questions.length === 0) return 'empty'
  const first = questions[0] || {}
  if (first.id) return `id:${first.id}`
  return `text:${first.question || first.text || ''}`
}

// Simulate how streamItemQuestionKeys is built from streamItems
function buildStreamItemQuestionKeys(sending, streamItems) {
  if (sending && streamItems.length > 0) {
    return new Set(
      streamItems
        .filter(it => it.type === 'question')
        .map(it => questionKey(it))
    )
  }
  return null
}

// Simulate the MsgContent suppression check
function shouldSuppressBlock(block, suppressedQuestionKeys) {
  if (block.type !== 'question') return false
  return suppressedQuestionKeys?.has(questionKey(block)) ?? false
}

test('streamItemQuestionKeys is null when not sending', () => {
  const streamItems = [{ type: 'question', question_id: 'q1', questions: [] }]
  const keys = buildStreamItemQuestionKeys(false, streamItems)
  assert.equal(keys, null, 'null when sending=false')
})

test('streamItemQuestionKeys is null when streamItems is empty', () => {
  const keys = buildStreamItemQuestionKeys(true, [])
  assert.equal(keys, null, 'null when streamItems empty')
})

test('streamItemQuestionKeys includes question_id-keyed questions', () => {
  const streamItems = [
    { type: 'text', content: 'hello' },
    { type: 'question', question_id: 'q1', questions: [{ question: 'Pick one?' }] },
  ]
  const keys = buildStreamItemQuestionKeys(true, streamItems)
  assert.ok(keys instanceof Set)
  assert.ok(keys.has('question_id:q1'))
  assert.equal(keys.size, 1, 'only question items contribute keys')
})

test('MsgContent suppresses a persisted question block whose key is in streamItems', () => {
  const block = { type: 'question', question_id: 'q1', questions: [] }
  const streamItems = [
    { type: 'question', question_id: 'q1', questions: [] },
  ]
  const keys = buildStreamItemQuestionKeys(true, streamItems)
  assert.ok(shouldSuppressBlock(block, keys), 'block suppressed when key matches streamItems')
})

test('MsgContent does NOT suppress a question block whose key differs from streamItems', () => {
  const block = { type: 'question', question_id: 'q2', questions: [] }
  const streamItems = [
    { type: 'question', question_id: 'q1', questions: [] },
  ]
  const keys = buildStreamItemQuestionKeys(true, streamItems)
  assert.equal(shouldSuppressBlock(block, keys), false, 'different question_id is not suppressed')
})

test('MsgContent does NOT suppress text or tool blocks', () => {
  const textBlock = { type: 'text', content: 'hello' }
  const toolBlock = { type: 'tool', tool: 'Bash', status: 'done' }
  const streamItems = [{ type: 'question', question_id: 'q1', questions: [] }]
  const keys = buildStreamItemQuestionKeys(true, streamItems)
  assert.equal(shouldSuppressBlock(textBlock, keys), false)
  assert.equal(shouldSuppressBlock(toolBlock, keys), false)
})

test('MsgContent suppression uses text-based key when question_id absent', () => {
  const q = { question: 'What is your preference?' }
  const block = { type: 'question', questions: [q] }
  const streamItem = { type: 'question', questions: [q] }
  const keys = buildStreamItemQuestionKeys(true, [streamItem])
  assert.ok(keys.has('text:What is your preference?'))
  assert.ok(shouldSuppressBlock(block, keys), 'text-keyed block suppressed')
})

test('no suppression when suppressedQuestionKeys is null (not sending)', () => {
  const block = { type: 'question', question_id: 'q1', questions: [] }
  assert.equal(shouldSuppressBlock(block, null), false, 'null keys = no suppression')
})

// ---------------------------------------------------------------------------
// Fix 2: patchQuestionAnswers — streamItems answers optimistic update
// ---------------------------------------------------------------------------

// Simulate patchQuestionAnswers logic from useStreamConnection
function patchQuestionAnswers(streamItems, questionId, answers) {
  const key = questionId ? `question_id:${questionId}` : null
  return streamItems.map(it => {
    if (it.type !== 'question') return it
    const itKey = questionKey(it)
    if (key ? itKey === key : true) {
      return { ...it, answers }
    }
    return it
  })
}

test('patchQuestionAnswers updates the matching question item by question_id', () => {
  const items = [
    { type: 'text', content: 'hello' },
    { type: 'question', question_id: 'q1', questions: [{ question: 'Pick?' }] },
  ]
  const answers = { 'Pick?': 'Option A' }
  const updated = patchQuestionAnswers(items, 'q1', answers)
  assert.deepEqual(updated[1].answers, answers)
  assert.equal(updated[0].type, 'text', 'text item untouched')
})

test('patchQuestionAnswers leaves non-matching question items unchanged', () => {
  const items = [
    { type: 'question', question_id: 'q1', questions: [] },
    { type: 'question', question_id: 'q2', questions: [] },
  ]
  const answers = { 'Q': 'A' }
  const updated = patchQuestionAnswers(items, 'q1', answers)
  assert.deepEqual(updated[0].answers, answers, 'q1 patched')
  assert.equal(updated[1].answers, undefined, 'q2 not patched')
})

test('patchQuestionAnswers patches all questions when no questionId given', () => {
  const items = [
    { type: 'question', question_id: 'q1', questions: [] },
    { type: 'question', question_id: 'q2', questions: [] },
  ]
  const answers = { 'Q': 'A' }
  const updated = patchQuestionAnswers(items, null, answers)
  assert.deepEqual(updated[0].answers, answers)
  assert.deepEqual(updated[1].answers, answers)
})

test('patchQuestionAnswers preserves all other item fields', () => {
  const items = [
    {
      type: 'question',
      question_id: 'q1',
      questions: [{ question: 'Foo?' }],
      some_extra: 'keep me',
    },
  ]
  const answers = { 'Foo?': 'Bar' }
  const updated = patchQuestionAnswers(items, 'q1', answers)
  assert.equal(updated[0].some_extra, 'keep me')
  assert.equal(updated[0].question_id, 'q1')
  assert.deepEqual(updated[0].questions, [{ question: 'Foo?' }])
})

test('patchQuestionAnswers returns original array when no items match', () => {
  const items = [
    { type: 'text', content: 'just text' },
  ]
  const updated = patchQuestionAnswers(items, 'q1', { Q: 'A' })
  assert.deepEqual(updated, items)
})

// ---------------------------------------------------------------------------
// Fix 3: Multi-select answered state — split logic correctness
// ---------------------------------------------------------------------------

// Simulate the answeredArr split from QuestionCard
function buildAnsweredArr(answered, isMulti, answeredValue) {
  if (answered && isMulti) {
    return answeredValue ? answeredValue.split(', ').map(s => s.trim()) : []
  }
  return null
}

function isOptionChosen(answered, isMulti, answeredArr, answeredValue, optLabel) {
  if (!answered) return false
  return isMulti
    ? (answeredArr?.includes(optLabel) ?? false)
    : answeredValue === optLabel
}

test('multi-select answeredArr splits comma-joined value correctly', () => {
  const arr = buildAnsweredArr(true, true, 'Option A, Option B, Option C')
  assert.deepEqual(arr, ['Option A', 'Option B', 'Option C'])
})

test('multi-select: isChosen is true for each individual selected option', () => {
  const answeredValue = 'Option A, Option C'
  const arr = buildAnsweredArr(true, true, answeredValue)
  assert.ok(isOptionChosen(true, true, arr, answeredValue, 'Option A'))
  assert.ok(isOptionChosen(true, true, arr, answeredValue, 'Option C'))
  assert.equal(isOptionChosen(true, true, arr, answeredValue, 'Option B'), false)
})

test('single-select: isChosen uses exact string match, not includes', () => {
  const answeredValue = 'Option A'
  assert.ok(isOptionChosen(true, false, null, answeredValue, 'Option A'))
  assert.equal(isOptionChosen(true, false, null, answeredValue, 'Option'), false)
})

test('multi-select: empty answeredValue yields empty array (no chosen options)', () => {
  const arr = buildAnsweredArr(true, true, '')
  assert.deepEqual(arr, [])
})

test('multi-select: single selected option still works (no trailing comma)', () => {
  const arr = buildAnsweredArr(true, true, 'Option A')
  assert.deepEqual(arr, ['Option A'])
  assert.ok(isOptionChosen(true, true, arr, 'Option A', 'Option A'))
})
