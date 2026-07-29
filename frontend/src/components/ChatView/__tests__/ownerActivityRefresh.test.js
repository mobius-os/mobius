import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const paneChatView = readFileSync(
  new URL('../../Shell/PaneChatView.jsx', import.meta.url),
  'utf8',
)

function slice(source, fromNeedle, toNeedle) {
  const from = source.indexOf(fromNeedle)
  assert.ok(from >= 0, `expected to find ${fromNeedle}`)
  const to = source.indexOf(toNeedle, from)
  assert.ok(to > from, `expected to find ${toNeedle} after ${fromNeedle}`)
  return source.slice(from, to)
}

test('the pane gives all committed owner activity one stable drawer refresh', () => {
  assert.match(paneChatView, /onOwnerActivity=\{refreshChats\}/)
  assert.doesNotMatch(paneChatView, /onQuestionAnswered/,
    'question answers must not keep a one-off parallel refresh callback')
  assert.match(chatView, /const onOwnerActivityRef = useRef\(onOwnerActivity\)/)
})

test('an accepted mid-turn queue or direct steer refreshes drawer recency', () => {
  const queuePath = slice(
    chatView,
    'const result = await queueRequest',
    '// Race: server said "started" though we expected queued.',
  )
  assert.match(
    queuePath,
    /if \(result\?\.status === 'queued' \|\| result\?\.status === 'steered'\) \{[\s\S]*?onOwnerActivityRef\.current\?\.\(\)/,
    'accepted owner activity behind an existing run must not wait for run-end',
  )
  const duplicate = slice(
    queuePath,
    "if (result?.status === 'duplicate') {",
    "if (result?.status === 'queued') {",
  )
  assert.doesNotMatch(duplicate, /onOwnerActivityRef/,
    'a duplicate acknowledgement must not manufacture new recency')
})

test('a deferred steer refreshes again at its authoritative transcript cut', () => {
  const cut = slice(
    chatView,
    'onSteeredIntoTurn: ({',
    '// System run activity is a structured sequence',
  )
  const commit = cut.indexOf('commitMessages(prev => insertMessageBatchByTs')
  const refresh = cut.indexOf('onOwnerActivityRef.current?.()')
  assert.ok(commit >= 0 && refresh > commit,
    'drawer refresh must follow the committed steer event, not predict its cut')
})

test('fast-forward exposes durable enqueue recency while the cut settles', () => {
  const fastForward = slice(
    chatView,
    'async function steerRowsImpl(steerRowsList) {',
    '// STEER (fast-forward): inject the queued messages into the LIVE turn',
  )
  const accepted = slice(
    fastForward,
    "if (result?.status === 'steered') {",
    "if (result?.status !== 'steered') {",
  )
  assert.match(accepted, /onOwnerActivityRef\.current\?\.\(\)/)
})

test('question answers share the same owner-activity refresh boundary', () => {
  const answerPath = slice(
    chatView,
    'const doSendSilent = useCallback',
    'function handleSubmit(e)',
  )
  const response = answerPath.indexOf('const response = await streamSend')
  const refresh = answerPath.indexOf('onOwnerActivityRef.current?.()')
  assert.ok(response >= 0 && refresh > response,
    'a question answer refresh belongs after its successful write')
})
