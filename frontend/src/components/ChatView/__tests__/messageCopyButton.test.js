/* Error-card copy retains its shared helper; message metadata offers only timestamps.
 * Native long-press selection must stay untouched. */
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const copyButton = readFileSync(new URL('../MessageCopyButton.jsx', import.meta.url), 'utf8')
const metaRow = readFileSync(new URL('../MessageMetaRow.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('the copy button is a plain tap target on the shared clipboard helper', () => {
  assert.match(copyButton, /copyPlainText/,
    'reuse the shared clipboard helper (API + textarea fallback), not a bespoke path')
  assert.match(copyButton, /stopPropagation/,
    'a copy tap must not double as the user-row timestamp toggle')
  assert.doesNotMatch(copyButton, /onPointerDown|onTouchStart|onContextMenu/,
    'the copy affordance must never intercept press/hold — native selection stays intact')
})

test('message rows no longer calculate whole-message copy payloads', () => {
  assert.doesNotMatch(chatView, /messageCopyText|copyText=/)
  assert.doesNotMatch(chatView, /speechText=|speechKey=|speechChatId=/)
  assert.doesNotMatch(chatView, /stopChatSpeech/)
})

test('tap-revealed metadata contains the timestamp but no copy action', () => {
  assert.match(metaRow, /<time className="chat__ts">/)
  assert.doesNotMatch(metaRow, /MessageCopyButton|copyText/)
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
