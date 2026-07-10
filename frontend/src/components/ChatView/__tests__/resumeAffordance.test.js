import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

// The one-tap Resume affordance (design §2.2): a turn paused by a drain-gated
// restart (or interrupted by a crash) persists a `resumable` error note; the
// tail note renders a Resume button that re-sends a short "continue".
const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

test('MsgContent gates the Resume button on a resumable tail note', () => {
  assert.match(msgContent, /onResume/,
    'MsgContent must accept an onResume prop')
  assert.match(
    msgContent,
    /block\.resumable\s*&&\s*isLastMsg\s*&&\s*onResume/,
    'Resume must be gated on block.resumable AND isLastMsg AND onResume — so ' +
      'only the tail interrupt note (not scrolled-back history or a live ' +
      'provider error) shows the button',
  )
  assert.match(
    msgContent,
    /className="chat__resume"[\s\S]*?onClick=\{\(\)\s*=>\s*onResume\('continue'\)\}/,
    'the Resume button must re-send "continue" via onResume',
  )
})

test('MsgContent memo compares onResume so a stable ref skips re-render', () => {
  assert.match(msgContent, /prev\.onResume === next\.onResume/,
    'the memo comparator must include onResume')
})

test('ChatView wires MsgContent.onResume to the normal send', () => {
  assert.match(chatView, /<MsgContent[\s\S]*?onResume=\{doSend\}/,
    'ChatView must pass its stable doSend as onResume so tapping Resume ' +
      'performs a normal visible "continue" send')
})

test('Resume button has styling', () => {
  assert.match(css, /\.chat__resume\s*\{/,
    'a .chat__resume style must exist for the Resume button')
})
