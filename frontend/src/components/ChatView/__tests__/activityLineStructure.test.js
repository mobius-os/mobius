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
  assert.match(activityStretch, /import ActivityLineHeader, \{ ActivityTypeIcon \} from '\.\/ActivityLineHeader\.jsx'/)
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

test('every thinking entry remains the same collapsed nested disclosure', () => {
  assert.match(activityHeader, /kind === 'reasoning'/,
    'the shared icon set should include a dedicated reasoning mark')
  assert.match(activityStretch, /const iconKind = thinkingOnly \? 'reasoning' : leadToolIcon/,
    'thinking-only stretches should select the reasoning glyph')
  assert.doesNotMatch(activityHeader + activityStretch, /chat__activity-icon--spacer/,
    'thinking is an activity type, not an empty icon column')
  assert.match(activityStretch, /function TimelineThought/,
    'a mixed thought owns its disclosure state')
  assert.match(activityStretch, /const \[open, setOpen\] = useState\(false\)/,
    'nested thinking starts collapsed')
  assert.match(activityStretch, /className="chat__activity-think-toggle"/)
  assert.doesNotMatch(activityStretch, /chat__activity-think-chevron/,
    'nested reasoning uses its icon and row affordance without a chevron')
  assert.match(activityStretch, /aria-expanded=\{open\}/,
    'the nested toggle exposes its state')
  assert.match(activityStretch, /aria-controls=\{bodyId\}/,
    'the nested toggle names the thought body it controls')
  assert.match(activityStretch, /id=\{bodyId\}[^>]*hidden=\{!open\}/,
    'the controlled thought shell remains addressable while its payload is unmounted')
  assert.match(activityStretch, /role="status" aria-live="polite"/,
    'deferred thought state changes should be announced')
  assert.match(activityStretch, /className="chat__lazy-retry" onClick=\{trace\.retry\}/,
    'a failed thought should retry without a close/reopen ritual')
  assert.match(activityStretch, /preserveTogglePosition\(headerRef\.current\)\s*setOpen\(o => !o\)/,
    'opening a long trace preserves the reader anchor')
  assert.doesNotMatch(activityStretch, /if \(thinkingOnly\) \{/,
    'a thinking-only entry must not swap component type when the first tool arrives')
  assert.match(activityStretch, /<StandardMarkdown text=\{content\} \/>/,
    'reasoning remains available inside the nested disclosure')
})

test('lazy tool details and touch targets keep their accessibility contract', () => {
  const toolBlock = readFileSync(new URL('../ToolBlock.jsx', import.meta.url), 'utf8')
  const coarse = [...chatCss.matchAll(/@media \(pointer: coarse\) \{[\s\S]*?\n\}/g)]
    .map(match => match[0])
    .join('\n')

  assert.match(toolBlock, /aria-controls=\{detailId\}/)
  assert.match(toolBlock, /id=\{detailId\}[\s\S]*hidden=\{!open\}/,
    'the controlled detail shell remains addressable while its payload is unmounted')
  assert.match(toolBlock, /role="region"/)
  assert.match(toolBlock, /aria-labelledby=\{headerId\}/)
  assert.match(toolBlock, /className="chat__lazy-retry"/)
  assert.match(toolBlock, /\? 'status' : undefined/)
  assert.match(toolBlock, /\? 'polite' : undefined/)
  assert.match(coarse, /\.chat__empty-prompt/)
  assert.match(coarse, /\.chat__empty-action/)
  assert.match(coarse, /\.chat__quick-action-chip/)
  assert.match(coarse, /\.chat__lazy-retry/)
  assert.match(coarse, /min-height:\s*44px/)
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
  assert.match(timeline, /margin-inline-start:\s*20px/,
    'child steps indent beneath the parent icon lane')
  assert.match(timeline, /padding-inline-start:\s*11px/)
  assert.match(timeline, /border-inline-start:\s*1px/,
    'a neutral one-pixel rail carries the child hierarchy')
  assert.doesNotMatch(chatCss, /\.chat__tools \+ \.chat__tools\s*\{\s*margin-top:\s*2px/,
    'adjacent activity blocks should not retain a one-off gap')
})
