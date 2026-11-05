import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  activationRetryDelay,
  chatEntryFrame,
  createActivationGate,
  heldAnswerStillOpen,
} from '../chatRuntimeState.js'

const READ_TIMEOUT_MS = 15000
const timeout = Object.assign(new Error('Request timed out'), { name: 'TimeoutError' })

test('activation retries only transient failures, a bounded number of times', () => {
  assert.equal(activationRetryDelay(new TypeError('Failed to fetch'), 0, READ_TIMEOUT_MS), 1000)
  assert.equal(activationRetryDelay(new TypeError('NetworkError when attempting to fetch resource.'), 0, READ_TIMEOUT_MS), 1000)
  assert.equal(activationRetryDelay(new TypeError('Cannot read properties of undefined'), 0, READ_TIMEOUT_MS), null)
  assert.equal(activationRetryDelay(new Error('CHAT_RUNTIME_FAILED_503'), 1, READ_TIMEOUT_MS), 3000)
  assert.equal(activationRetryDelay(new Error('CHAT_LOAD_FAILED_429'), 2, READ_TIMEOUT_MS), 8000)
  assert.equal(activationRetryDelay(new Error('CHAT_NOT_FOUND'), 0, READ_TIMEOUT_MS), null)
  assert.equal(activationRetryDelay(new Error('CHAT_RUNTIME_FAILED_403'), 0, READ_TIMEOUT_MS), null)
  assert.equal(activationRetryDelay(new Error('CHAT_RUNTIME_FAILED_503'), 5, READ_TIMEOUT_MS), null)
})

test('activation retries span a server restart', () => {
  const error = new Error('CHAT_LOAD_FAILED_503')
  let total = 0
  for (let attempt = 0; activationRetryDelay(error, attempt, READ_TIMEOUT_MS) != null; attempt += 1) {
    total += activationRetryDelay(error, attempt, READ_TIMEOUT_MS)
  }
  assert.ok(total >= 45000)
})

test('a timed-out read is never retried while it may still be running server-side', () => {
  for (let attempt = 0; activationRetryDelay(timeout, attempt, READ_TIMEOUT_MS) != null; attempt += 1) {
    assert.ok(activationRetryDelay(timeout, attempt, READ_TIMEOUT_MS) >= READ_TIMEOUT_MS)
  }
})

const frame = (overrides) => chatEntryFrame({
  messageCount: 0,
  loading: false,
  loadError: false,
  activationRetrying: false,
  turnActive: false,
  activationPhase: 'pending',
  activationSettled: false,
  transcriptPaintable: false,
  ...overrides,
})

test('an empty chat retrying its load presents a stable error frame, not the empty state', () => {
  // Shell holds the launch cover or the previous chat until display-ready, so
  // quiet retries must not keep it waiting for up to minutes.
  for (const activationPhase of ['error', 'pending', 'cold']) {
    for (const loading of [true, false]) {
      assert.deepEqual(
        frame({ activationRetrying: true, activationPhase, loading }),
        { showEmpty: false, showLoadError: true, displayReady: true },
        `${activationPhase}, loading=${loading}`,
      )
    }
  }
  assert.equal(frame({ activationRetrying: true, messageCount: 3 }).showLoadError, false,
    'rows a successful retry is preparing replace the retry frame')
})

test('a load error is stable at once and wins over a cached running marker', () => {
  assert.deepEqual(
    frame({ loadError: true, turnActive: true, activationPhase: 'error' }),
    { showEmpty: false, showLoadError: true, displayReady: true },
  )
  assert.equal(frame({ loadError: true, loading: true }).showLoadError, false,
    'a manual retry hides the error while it loads')
})

test('a transcript or empty frame waits for runtime truth; only cold or cached-fallback activation is early', () => {
  const transcript = { messageCount: 5, transcriptPaintable: true }
  assert.equal(frame({ ...transcript, activationPhase: 'pending' }).displayReady, false)
  assert.equal(frame({ ...transcript, activationPhase: 'cold' }).displayReady, true)
  assert.equal(frame({ ...transcript, activationPhase: 'error' }).displayReady, true)
  assert.equal(frame({ ...transcript, activationPhase: 'error', loading: true }).displayReady, false)
  assert.equal(frame({ ...transcript, activationSettled: true, loading: true }).displayReady, false)
  assert.equal(frame({ ...transcript, activationSettled: true }).displayReady, true)
  assert.deepEqual(frame({}), { showEmpty: true, showLoadError: false, displayReady: false })
  assert.deepEqual(
    frame({ activationSettled: true }),
    { showEmpty: true, showLoadError: false, displayReady: true },
  )
})

test('a saved transcript presents at once, idle or mid-reply, while runtime truth reconciles beneath it', () => {
  const cached = { messageCount: 5, transcriptPaintable: true, cachedFrame: true }
  assert.equal(frame({ ...cached, activationPhase: 'pending' }).displayReady, true,
    'reopening a chat must not wait on the network')
  assert.equal(frame({ ...cached, turnActive: true, activationPhase: 'pending' }).displayReady, true,
    'a chat whose cached window was mid-reply opens at once too')
  assert.equal(frame({ ...cached, loading: true }).displayReady, false,
    'a window still loading is not a saved frame')
  assert.equal(frame({ cachedFrame: true }).displayReady, false,
    'an empty cache never stands in for an unverified empty chat')
  assert.equal(
    frame({ messageCount: 5, transcriptPaintable: true, activationPhase: 'pending' }).displayReady,
    false,
    'an unvalidated reading coordinate still waits for runtime truth',
  )
})

const tick = () => new Promise(resolve => setImmediate(resolve))

test('a row control tapped on a saved frame is held, then runs once activation is ready', async () => {
  const gate = createActivationGate()
  assert.equal(await gate.wait('ready'), true, 'a settled chat acts at once')
  let answered = null
  const held = gate.wait('pending').then(ready => { answered = ready })
  await tick()
  assert.equal(answered, null, 'the tap is held, not dropped, while activation is pending')
  gate.settle(true)
  await held
  assert.equal(answered, true, 'it proceeds once activation is ready')
})

test('a held tap is cancelled when activation fails or the view leaves the chat', async () => {
  const gate = createActivationGate()
  const failed = gate.wait('pending')
  const cold = gate.wait('cold')
  gate.settle(false)
  assert.equal(await failed, false)
  assert.equal(await cold, false, 'a cold activation still holds and cancels the same way')
  const later = gate.wait('pending')
  gate.settle(true)
  assert.equal(await later, true, 'a settled gate leaves no stale waiter behind for the next tap')
})

test('a tap made after activation already failed resolves false at once instead of hanging', async () => {
  const gate = createActivationGate()
  let outcome = null
  gate.wait('error').then(ready => { outcome = ready })
  await tick()
  assert.equal(outcome, false, 'an answer tapped on a failed chat does not wait for a later recovery')
  gate.settle(true)
  await tick()
  assert.equal(outcome, false, 'a later recovery does not fire the stale tap')
})

test('a held control re-reads live state once activation lands and drops a moot tap', async () => {
  const gate = createActivationGate()
  let open = true
  const heldAnswer = gate.wait('pending', () => open)
  open = false
  gate.settle(true)
  assert.equal(await heldAnswer, false, 'an answer held for a question that closed meanwhile does nothing')

  const checked = []
  const immediate = gate.wait('ready', () => { checked.push('read'); return false })
  assert.equal(await immediate, true, 'a tap on a ready chat acts on what it saw; no re-check')
  assert.deepEqual(checked, [])
})

test('a held answer proceeds only while its card is still the open question', () => {
  const question = { type: 'question', question_id: 'q1' }
  const messages = [{ role: 'assistant', blocks: [question] }]
  assert.equal(heldAnswerStillOpen('q1', { answerableQuestionId: 'q1', messages }), true)
  assert.equal(heldAnswerStillOpen('q1', { answerableQuestionId: null, messages }), false,
    'runtime truth shows the question already closed, so the held answer is dropped')
  assert.equal(heldAnswerStillOpen('q1', { answerableQuestionId: 'q2', messages }), false,
    'a newer question replaced it')
  assert.equal(heldAnswerStillOpen(null, { answerableQuestionId: 'q1', messages }), false)

  const restart = (status, extra = {}) => ({
    type: 'question',
    question_id: 'r1',
    platform_action: { type: 'restart', version: 2, status },
    ...extra,
  })
  const offer = status => [{ role: 'assistant', blocks: [restart(status)] }]
  assert.equal(heldAnswerStillOpen('r1', { answerableQuestionId: null, messages: offer('awaiting_owner') }), true,
    'a durable Restart offer still awaiting the owner stays answerable without being the live question')
  assert.equal(heldAnswerStillOpen('r1', { answerableQuestionId: null, messages: offer('restarting') }), false)
  assert.equal(heldAnswerStillOpen('r1', {
    answerableQuestionId: null,
    messages: [{ role: 'assistant', blocks: [restart('awaiting_owner', { answers: { a: 'b' } })] }],
  }), false, 'an offer answered elsewhere is closed')
  assert.equal(heldAnswerStillOpen('r1', {
    answerableQuestionId: null,
    streamItems: [restart('awaiting_owner')],
  }), true, 'the live stream counts as well as the saved transcript')
})
