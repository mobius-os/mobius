import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const inputBar = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const queuedMessages = readFileSync(new URL('../QueuedMessages.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('composer fast-forward dispatches immediately without an incidental blur', () => {
  const steerBlock = inputBar.match(
    /className="chat__action chat__steer"[\s\S]*?aria-label="Send queued message now"/,
  )?.[0] || ''
  assert.match(steerBlock, /onPointerDown=\{\(e\) => e\.preventDefault\(\)\}/)
  assert.match(
    steerBlock,
    /onTouchEnd=\{\(e\) => \{ e\.preventDefault\(\); onSteer\(\) \}\}/,
  )
  assert.match(steerBlock, /onClick=\{onSteer\}/)
})

test('Send, Steer, and Stop reuse one continuously visible primary action', () => {
  assert.match(
    inputBar,
    /if \(sending && !hasInput && showSteer\)[\s\S]*?key="primary"[\s\S]*?disabled=\{!steerReady\}/,
    'the semantic Steer identity must not wait for serverTs confirmation',
  )
  assert.match(
    chatView,
    /const showSteer = !hasPendingQuestion[\s\S]*?turnActive[\s\S]*?pendingQueue\.visiblePendingMessages\.length > 0/,
    'an optimistic visible queue row should choose Steer immediately',
  )
  assert.match(
    chatView,
    /const queueWrites = \[\.\.\.queuedSendRequestsRef\.current\.values\(\)\][\s\S]*?await Promise\.allSettled\(queueWrites\)[\s\S]*?const snapshot = pendingQueue\.getVisiblePendingMessages\(\)/,
    'an early Steer tap must await the queue write before reading steerable rows',
  )
  const steerBlock = inputBar.match(
    /if \(sending && !hasInput && showSteer\)[\s\S]*?<button[\s\S]*?<\/button>/,
  )?.[0] || ''
  const stopBlock = inputBar.match(
    /if \(sending && !hasInput\)[\s\S]*?<button[\s\S]*?<\/button>/,
  )?.[0] || ''
  const sendBlock = inputBar.match(
    /if \(hasInput && !listening\)[\s\S]*?<button[\s\S]*?<\/button>/,
  )?.[0] || ''

  assert.match(steerBlock, /key="primary"/)
  assert.match(stopBlock, /key="primary"/)
  assert.match(sendBlock, /key="primary"/)
})

test('per-row fast-forward appears with the optimistic row and dispatches on touchend', () => {
  assert.match(
    queuedMessages,
    /\{steerActive && \(/,
    'the row action should render before serverTs confirmation, alongside cancel',
  )
  assert.doesNotMatch(
    queuedMessages,
    /steerActive && msg\.serverTs === true/,
    'server confirmation must not delay the row action from appearing',
  )
  const steerBlock = queuedMessages.match(
    /className="queued__action queued__steer"[\s\S]*?aria-label="Send this queued message now"/,
  )?.[0] || ''
  assert.match(steerBlock, /onPointerDown=\{\(e\) => e\.preventDefault\(\)\}/)
  assert.match(steerBlock, /onTouchEnd=\{\(e\) => \{/)
  assert.match(steerBlock, /e\.preventDefault\(\)[\s\S]*?onSteerOne\?\.\(cidOf\(msg\)\)/)
  assert.match(
    chatView,
    /async function handleSteerOne\(cid\) \{[\s\S]*?const queueWrite = queuedSendRequestsRef\.current\.get\(cid\)[\s\S]*?if \(queueWrite\) await Promise\.allSettled\(\[queueWrite\]\)[\s\S]*?const findRow/,
    'an early row tap should await that row\'s exact queue write before steering',
  )
})

test('fast-forward preserves queue-time scroll intent through layout reflow', () => {
  const steerPath = chatView.match(
    /async function steerRowsImpl\(steerRowsList\) \{[\s\S]*?\n  \/\/ STEER \(fast-forward\): inject/,
  )?.[0] || ''
  assert.match(
    steerPath,
    /previousSendIntent = sendIntentByCidRef\.current\.get\(steerCid\) \|\| null[\s\S]*?captureSendIntent\(\{[\s\S]*?previousIntent: previousSendIntent[\s\S]*?rememberSendIntent\(steerCid, explicitSteerIntent\)/,
  )
})

test('touch steer dismisses the keyboard only after its committed row is positioned', () => {
  assert.match(
    chatView,
    /async function steerRowsImpl\(steerRowsList\) \{[\s\S]*?explicitSteerIntent = captureSendIntent\(\{[\s\S]*?rememberSendIntent\(steerCid, explicitSteerIntent\)[\s\S]*?steerKeyboardDismissRequestRef\.current = \{[\s\S]*?pendingQueue\.reserveForSteer\(consumePendingCids\)/,
    'the tap should retain focus while the provider is still settling the cut',
  )
  const requestToCut = chatView.match(
    /async function steerRowsImpl\(steerRowsList\) \{[\s\S]*?\n  \/\/ STEER \(fast-forward\): inject/,
  )?.[0] || ''
  assert.doesNotMatch(
    requestToCut,
    /inputRef\.current\?\.blur\(\)|inputEl\.blur\(\)/,
    'request-time keyboard dismissal recreates the pre-cut resize jitter',
  )
  assert.match(
    chatView,
    /onSteeredIntoTurn: \(\{[\s\S]*?landSentMessage\(pinCid,[\s\S]*?setCommittedSteerKeyboardDismiss\(keyboardDismissRequest\)[\s\S]*?commitMessages\(prev => insertMessageBatchByTs/,
    'the authoritative cut should schedule dismissal in the same batch as the row',
  )
  assert.match(
    chatView,
    /useLayoutEffect\(\(\) => \{[\s\S]*?committedSteerKeyboardDismiss[\s\S]*?querySelector\([\s\S]*?chat__msg--user\[data-cid=[\s\S]*?document\.activeElement === inputEl[\s\S]*?inputValueRef\.current === request\.draft[\s\S]*?inputEl\.blur\(\)/,
    'blur must wait for the committed row and preserve newer focus/draft intent',
  )
})
