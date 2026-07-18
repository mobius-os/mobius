import { readFileSync } from 'node:fs'
import test from 'node:test'
import assert from 'node:assert/strict'

const activityStretch = readFileSync(new URL('../ActivityStretch.jsx', import.meta.url), 'utf8')
const activityHeader = readFileSync(new URL('../ActivityLineHeader.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return chatCss.match(new RegExp(`${escaped}\\s*\\{[^}]*\\}`))?.[0] || ''
}

test('reasoning markdown has a scoped quiet typography lane', () => {
  const blocks = cssRule('.chat__reasoning-body .md-blocks')
  const headings = cssRule('.chat__reasoning-body .md-blocks .md-heading')
  const strong = cssRule('.chat__reasoning-body .md-blocks strong')

  assert.match(blocks, /gap:\s*var\(--activity-row-gap, 4px\)/,
    'reasoning blocks should use the activity row rhythm')
  assert.match(blocks, /color:\s*var\(--muted\)/,
    'reasoning should remain secondary to the answer')
  assert.match(blocks, /font-size:\s*13px/)
  assert.match(blocks, /line-height:\s*1\.45/)
  assert.match(headings, /font-size:\s*inherit/,
    'model-authored headings must stay at reasoning body size')
  assert.match(headings, /font-weight:\s*500/)
  assert.match(strong, /font-weight:\s*500/,
    'bold lead-ins should not dominate the activity timeline')
})

test('placeholder and real stretches render through the shared activity header', () => {
  const placeholderStart = chatView.indexOf('className="chat__tools chat__thinking"')
  const placeholderEnd = chatView.indexOf('</li>', placeholderStart)
  const placeholder = chatView.slice(placeholderStart, placeholderEnd)

  assert.match(chatView, /import ActivityLineHeader from '\.\/ActivityLineHeader\.jsx'/)
  assert.match(activityStretch, /import ActivityLineHeader from '\.\/ActivityLineHeader\.jsx'/)
  assert.match(activityStretch, /<ActivityLineHeader/,
    'the real stretch must RENDER the shared header, not merely import it')
  assert.ok(placeholderStart >= 0, 'placeholder should use the standard activity wrapper')
  assert.match(placeholder, /className="chat__activity chat__activity--running"/)
  assert.match(placeholder, /<ActivityLineHeader/)
  assert.doesNotMatch(placeholder, /chat__activity-icon|chat__activity-label/,
    'placeholder must not duplicate shared header internals')
  assert.doesNotMatch(chatCss, /\.chat__thinking\s*\{/,
    'the e2e presence hook must not carry layout compensation')
})

test('the stretch is the only disclosure — thinking renders inline as generated', () => {
  assert.match(activityHeader, /kind === 'reasoning'/,
    'the shared icon set should include a dedicated reasoning mark')
  assert.match(activityStretch, /const iconKind = thinkingOnly \? 'reasoning' : leadToolIcon/,
    'thinking-only stretches should select the reasoning glyph')
  assert.doesNotMatch(activityHeader + activityStretch, /chat__activity-icon--spacer/,
    'thinking is an activity type, not an empty icon column')
  // Opening the stretch preserves the reasoning exactly as generated — there is
  // no second "> Thinking" toggle to click (owner call).
  assert.doesNotMatch(activityStretch, /TimelineThought/,
    'an in-stretch thought must not be a nested disclosure')
  assert.doesNotMatch(activityStretch, /chat__activity-think-toggle/,
    'no nested thought toggle markup')
  assert.match(activityStretch, /<StandardMarkdown text=\{thinkingContentForDisplay\(item\.content\)\} \/>/,
    'reasoning renders inline in the expanded timeline')
  assert.match(activityStretch, /\{!thinkingOnly && \(/,
    'a mixed run keeps a plain duration label; a thinking-only header already named it')
})

test('activity spacing derives from one block gap and one row gap', () => {
  const message = cssRule('.chat__msg--assistant')
  const tools = cssRule('.chat__tools')
  const timeline = cssRule('.chat__activity-timeline')

  assert.match(message, /--activity-block-gap:\s*8px/)
  assert.match(message, /--activity-row-gap:\s*4px/)
  assert.match(tools, /gap:\s*var\(--activity-row-gap, 4px\)/)
  assert.match(timeline, /gap:\s*var\(--activity-row-gap, 4px\)/)
  assert.match(timeline, /margin-top:\s*var\(--activity-row-gap, 4px\)/)
  assert.doesNotMatch(chatCss, /\.chat__tools \+ \.chat__tools\s*\{\s*margin-top:\s*2px/,
    'adjacent activity blocks should not retain a one-off gap')
})
