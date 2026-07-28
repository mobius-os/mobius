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

test('per-row fast-forward dispatches on touchend too', () => {
  const steerBlock = queuedMessages.match(
    /className="queued__steer"[\s\S]*?aria-label="Send this queued message now"/,
  )?.[0] || ''
  assert.match(steerBlock, /onPointerDown=\{\(e\) => e\.preventDefault\(\)\}/)
  assert.match(steerBlock, /onTouchEnd=\{\(e\) => \{/)
  assert.match(steerBlock, /e\.preventDefault\(\)[\s\S]*?onSteerOne\?\.\(cidOf\(msg\)\)/)
})

test('the shared steer path snapshots scroll before dismissing the mobile composer', () => {
  assert.match(
    chatView,
    /async function steerRowsImpl\(steerRowsList\) \{[\s\S]*?steerPinIntentRef\.current = makeSendPinIntent\(steerWillPin\)[\s\S]*?if \(_isTouchPrimary\) inputRef\.current\?\.blur\(\)[\s\S]*?pendingQueue\.reserveForSteer\(consumePendingCids\)/,
  )
})
