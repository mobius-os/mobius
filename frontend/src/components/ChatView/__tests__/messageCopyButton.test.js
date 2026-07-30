/* One-tap message copy: messageCopyText owns what "copy this message" means,
 * MessageCopyButton is a plain tap target on the shared clipboard helper, and
 * MsgContent only offers copy on settled prose. Native long-press selection
 * must stay untouched (chatUiPolish.test locks the no-interception side). */
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { messageCopyText } from '../messageCopy.js'

const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
const copyButton = readFileSync(new URL('../MessageCopyButton.jsx', import.meta.url), 'utf8')

test('messageCopyText joins prose blocks and skips activity chrome', () => {
  const msg = {
    role: 'assistant',
    blocks: [
      { type: 'thinking', content: 'private reasoning' },
      { type: 'text', content: 'First paragraph.' },
      { type: 'tool', name: 'Bash', output: 'noise' },
      { type: 'text', content: 'Second paragraph.' },
      { type: 'question', questions: [] },
    ],
  }
  assert.equal(messageCopyText(msg), 'First paragraph.\n\nSecond paragraph.')
})

test('messageCopyText strips hidden augmentation from user messages', () => {
  const msg = {
    role: 'user',
    content: 'hello <agent_experience>injected</agent_experience> world',
  }
  assert.equal(messageCopyText(msg), 'hello\n\nworld')
})

test('messageCopyText falls back to plain content; system rows copy nothing', () => {
  assert.equal(messageCopyText({ role: 'assistant', content: 'plain' }), 'plain')
  assert.equal(messageCopyText({ role: 'assistant', kind: 'compaction', content: 'x' }), '')
  assert.equal(messageCopyText({ role: 'assistant', kind: 'auto_continuation', content: 'x' }), '')
  assert.equal(messageCopyText({ role: 'assistant', blocks: [{ type: 'tool' }] }), '')
})

test('the copy button is a plain tap target on the shared clipboard helper', () => {
  assert.match(copyButton, /copyPlainText/,
    'reuse the shared clipboard helper (API + textarea fallback), not a bespoke path')
  assert.match(copyButton, /stopPropagation/,
    'a copy tap must not double as the user-row timestamp toggle')
  assert.doesNotMatch(copyButton, /onPointerDown|onTouchStart|onContextMenu/,
    'the copy affordance must never intercept press/hold — native selection stays intact')
})

test('only settled messages offer copy — a streaming answer is still changing', () => {
  assert.match(msgContent, /isStreaming \? '' : messageCopyText\(msg\)/)
})

test('every copy affordance shares the lower-right corner', () => {
  // Code blocks pin right (codeBlockCopy.test), user bubbles inherit their
  // column's flex-end; the response row must opt out of the assistant
  // column's flex-start or it strands the one copy button at the left.
  const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  assert.match(
    css,
    /\.chat__msg--assistant \.chat__msg-actions \{ align-self: flex-end; \}/,
    'response copy row aligns lower-right like code blocks and user bubbles',
  )
})
