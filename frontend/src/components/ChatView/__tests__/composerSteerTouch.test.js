import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const inputBar = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const queuedMessages = readFileSync(new URL('../QueuedMessages.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('composer fast-forward dispatches immediately without an incidental blur', () => {
  const steerBlock = inputBar.match(
    /key="steer"[\s\S]*?aria-label="Send queued message now"/,
  )?.[0] || ''
  assert.match(steerBlock, /onPointerDown=\{\(e\) => e\.preventDefault\(\)\}/)
  assert.match(
    steerBlock,
    /onTouchEnd=\{\(e\) => \{ e\.preventDefault\(\); onSteer\(\) \}\}/,
  )
  assert.match(steerBlock, /onClick=\{onSteer\}/)
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
    /async function steerRowsImpl\(steerRowsList\) \{[\s\S]*?steerPinIntentRef\.current = makeSendPinIntent\(steerWillPin\)[\s\S]*?if \(_isTouchPrimary\) inputRef\.current\?\.blur\(\)[\s\S]*?pendingQueue\.promoteManyByCid\(consumePendingCids\)/,
  )
})
