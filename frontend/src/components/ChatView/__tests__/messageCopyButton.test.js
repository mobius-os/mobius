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
  assert.match(copyButton, /writeClipboard\(payload\)/,
    'reuse the shared clipboard writer (API + textarea fallback), not a bespoke path')
  assert.match(copyButton, /stopPropagation/,
    'a copy tap must not double as the user-row timestamp toggle')
  assert.doesNotMatch(copyButton, /onPointerDown|onTouchStart|onContextMenu/,
    'the copy affordance must never intercept press/hold — native selection stays intact')
})

test('owner rows copy their own text; a reply copies only from its one end row', () => {
  // Per-row assistant copy (#1290) scattered buttons through every turn and
  // was reverted (#1342). Replies own a single action row instead
  // (replyFooter.test), so the transcript loop never derives assistant copy.
  assert.match(chatView, /const copyText = ownerUserMessage \? messageCopyText\(renderedMsg\) : ''/)
  const reply = readFileSync(new URL('../AssistantReply.jsx', import.meta.url), 'utf8')
  assert.equal(reply.split('<MessageMetaRow').length - 1, 1)
  assert.doesNotMatch(chatView, /speechText=|speechKey=|speechChatId=/)
  assert.doesNotMatch(chatView, /stopChatSpeech/)
})

test('a mouse hover reveals a reply’s row; only the latest reply keeps its row in view', () => {
  const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  const hover = css.match(/@media \(hover: hover\) and \(pointer: fine\) \{\s*\.chat__reply:hover > \.chat__msg-meta \{[^}]*visibility: visible;/)
  assert.ok(hover, 'hover reveal is limited to fine pointers so touch keeps tap-to-reveal')
  assert.doesNotMatch(css, /\.chat__msg:hover > \.chat__msg-meta/,
    'owner messages keep their tap-only reveal')
  assert.doesNotMatch(metaRow, /alwaysVisible|pinned/,
    'the row only renders the visibility its owner decides')
  assert.match(chatView, /metaVisible=\{last \|\| visibleMessageMetaKey === group\.rows\[0\]\.key\}/,
    'the latest reply keeps its actions in view; owner rows never pin their timestamp')
})

test('copy follows the timestamp inside one tap-revealed metadata row', () => {
  assert.ok(
    metaRow.indexOf('<time className="chat__ts">')
      < metaRow.indexOf('<MessageCopyButton read={copy} />'),
    'copy must render immediately after the timestamp',
  )
  assert.match(chatView, /visible=\{visibleMessageMetaKey === dataKey\}/)

  const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  assert.match(css, /\.chat__msg-meta \{[\s\S]*visibility: hidden;/)
  assert.match(css, /\.chat__msg-meta--visible \{[\s\S]*visibility: visible;/)
  const metaRule = css.match(/\.chat__msg-meta \{([^}]*)\}/)[1]
  const boxHeight = metaRule.match(/(?:^|\s)height: (\d+)px;/)?.[1]
  assert.ok(boxHeight, 'the row must use the message gap instead of centering controls in zero height')
  assert.match(metaRule, new RegExp(`margin-bottom: -${boxHeight}px;`),
    'the negative bottom margin cancels the whole box, so mounting the row never moves the transcript')
  assert.doesNotMatch(metaRule, /margin-top:/, 'no gap outside the cancelled box')
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

test('a copy that fails says so instead of silently doing nothing', () => {
  assert.match(copyButton, /failed: 'Couldn’t copy'/)
  assert.match(copyButton, /setCopyState\(copied \? 'copied' : 'failed'\)/)
  assert.match(copyButton, /role="status" aria-live="polite"/,
    'the outcome is announced, like tool-output copy')
})
