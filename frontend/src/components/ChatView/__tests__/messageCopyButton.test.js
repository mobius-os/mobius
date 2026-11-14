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

test('the chat has one reply footer, on the reply at its very end', () => {
  const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  // Hover reveals only the footer's Branch and time, never a row.
  assert.doesNotMatch(css, /\.chat__reply:hover\s*>\s*\.chat__msg-meta\s*[,{]/,
    'hovering a reply must not reveal another row above the current reply')
  assert.doesNotMatch(css, /\.chat__msg:hover > \.chat__msg-meta/,
    'owner messages keep their tap-only reveal')
  assert.doesNotMatch(metaRow, /alwaysVisible|pinned/,
    'the row only renders the visibility its owner decides')
  assert.match(chatView, /const turnStarting = turnActive \|\| questionFollowHandoff != null/,
    'a submitted card answer starts the turn before the server acknowledges it')
  assert.match(chatView, /useLayoutEffect\(\(\) => \{\s*if \(!turnStarting\) settledFooterKeyRef\.current = settledFooterKey\s*\}, \[turnStarting, settledFooterKey\]\)/,
    'the settled footer is recorded from committed renders only')
  assert.match(chatView, /const footerState = !turnStarting \? 'settled'\s*: showActiveAssistantSurface && footerGroup\?\.rows\[0\]\.key !== settledFooterKeyRef\.current\s*\? 'working' : null/,
    'answering or sending clears the footer at once; only a new live reply reserves its line again')
  assert.match(chatView, /footer=\{group === footerGroup \? footerState : null\}/,
    'only the last reply holds the footer')
  assert.match(chatView, /stretchSourceRows=\{group === footerGroup && turnStarting \? null : stretchSourceRows\.get\(group\) \|\| null\}/,
    'the latest stretch’s References leave with its footer while the agent works')
  assert.match(chatView, /useLayoutEffect\(\(\) => \{\s*if \(messageMetaTimerRef\.current\) clearTimeout\(messageMetaTimerRef\.current\)\s*messageMetaTimerRef\.current = null\s*setVisibleMessageMetaKey\(null\)\s*\}, \[chatId, turnActive, activeAssistantMessageId, streamAssistantMessageId\]\)/,
    'a new turn or assistant message clears an old tap reveal before paint')
})

test('ChatView gives the footer the start of the agent’s stretch to copy from', () => {
  assert.match(chatView, /const first = stretches\.get\(footerGroup\)\?\.\[0\]/)
  assert.match(chatView, /stretchStartKey=\{group === footerGroup \? footerStretch\?\.startKey : undefined\}/)
})

test('every row reads time, then Copy, then Branch; the reply footer appears on hover', () => {
  assert.ok(metaRow.indexOf('{time}') < metaRow.indexOf('<MessageCopyButton read={copy} />')
    && metaRow.indexOf('<MessageCopyButton read={copy} />') < metaRow.indexOf('<MessageBranchButton branch={branch} />'),
  'time, Copy, Branch')
  const footerCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  assert.match(footerCss, /@media \(hover: hover\) and \(pointer: fine\) and \(not \(any-pointer: coarse\)\) \{\s*\.chat__reply > \.chat__msg-meta--start \{ visibility: inherit; \}/,
    'with a fine pointer the transparent footer stays focusable')
  assert.match(footerCss, /\.chat__reply:hover > \.chat__msg-meta--start,\s*\.chat__reply > \.chat__msg-meta--start:focus-within \{\s*opacity: 1;\s*pointer-events: auto;/,
    'hovering the reply or focusing a control reveals it')
  assert.match(chatView, /metaVisible=\{group === footerGroup && visibleMessageMetaKey === group\.rows\[0\]\.key\}/,
    'a tap reveals the footer for a moment, as on owner rows')
  assert.match(footerCss, /\.chat__msg-meta--start > :is\(\.chat__msg-copy, \.chat__msg-branch\):not\(\.chat__msg-copy--failed\) \{\s*width: 24px;\s*height: 24px;\s*padding: 0;/,
    'Copy and Branch are equal square buttons with centred icons')
  assert.match(chatView, /visible=\{visibleMessageMetaKey === dataKey\}/)

  const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  assert.match(css, /\.chat__msg-meta \{[\s\S]*visibility: hidden;/)
  const revealRule = css.match(/\.chat__msg-meta--visible \{([^}]*)\}/)[1]
  assert.match(revealRule, /visibility: inherit;/,
    'a revealed row follows its view: forcing visible paints it over the app when the shell hides a mounted chat')
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

test('replies in one agent stretch sit as close as rows within a reply', () => {
  // An answered card, wait or helper result between two replies is hidden, so
  // the replies are adjacent; the conversation gap belongs between speakers.
  const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  assert.match(css, /\.chat__list > :is\(\.chat__reply, \.chat__msg--peer\)\s*\+ :is\(\.chat__reply, \.chat__msg--peer, \.chat__msg--pending-reply\) \{\s*margin-block-start: calc\(8px - 24px\);/)
  // The Thinking row a new turn shows first sits where its reply will, so
  // the swap does not move the transcript.
  assert.match(chatView, /<li className="chat__msg chat__msg--assistant chat__msg--pending-reply">/)
  assert.match(css, /\.chat__reply-rows > \.chat__msg \+ \.chat__msg[^{]*\{\s*margin-top: 8px;/)
})
