/* One-tap message copy: messageCopyText owns what "copy this message" means,
 * MessageCopyButton is a plain tap target on the shared clipboard helper, and
 * MessageMetaRow keeps copy beside the timestamp behind the row's existing
 * tap-to-reveal interaction. Native long-press selection must stay untouched
 * (chatUiPolish.test locks the no-interception side). */
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { messageCopyText } from '../messageCopy.js'

const copyButton = readFileSync(new URL('../MessageCopyButton.jsx', import.meta.url), 'utf8')
const metaRow = readFileSync(new URL('../MessageMetaRow.jsx', import.meta.url), 'utf8')
const streamingMessage = readFileSync(new URL('../StreamingMessage.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

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
  assert.equal(messageCopyText({ role: 'assistant', kind: 'continuation', content: 'x' }), '')
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
  assert.match(streamingMessage, /isStreaming \? '' : messageCopyText\(msg\)/)
})

test('copy follows the timestamp inside one tap-revealed metadata row', () => {
  assert.ok(
    metaRow.indexOf('<time className="chat__ts">')
      < metaRow.indexOf('<MessageCopyButton text={copyText} />'),
    'copy must render immediately after the timestamp',
  )
  assert.match(chatView, /visible=\{visibleMessageMetaKey === dataKey\}/)

  const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  assert.match(css, /\.chat__msg-meta \{[\s\S]*visibility: hidden;/)
  assert.match(css, /\.chat__msg-meta--visible \{[\s\S]*visibility: visible;/)
  assert.match(css, /\.chat__msg-meta \{[\s\S]*height: 24px;[\s\S]*margin-bottom: -24px;/,
    'the row must use the message gap instead of centering controls in zero height')
})

test('message metadata stays visible for five seconds', () => {
  assert.match(chatView, /const MESSAGE_META_VISIBLE_MS = 5000/)
  assert.match(chatView, /\}, MESSAGE_META_VISIBLE_MS\)/)
})

test('the revealed action uses the shared shell copy icon without visible text', () => {
  assert.match(copyButton, /import \{ Check, Copy \} from '@openai\/apps-sdk-ui\/components\/Icon'/)
  assert.match(copyButton, /<Copy width=\{14\} height=\{14\} aria-hidden="true" \/>/)
  assert.doesNotMatch(copyButton, />Copy<\/span>/)
})
