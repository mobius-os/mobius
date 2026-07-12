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

test('Resume button clears the 44px touch floor with press feedback', () => {
  const block = css.match(/\.chat__resume\s*\{[\s\S]*?\}/)?.[0] ?? ''
  assert.match(block, /min-height:\s*44px/,
    'the Resume button must be at least 44px tall (touch floor)')
  assert.match(block, /var\(--accent\)/,
    'Resume carries an accent-tinted fill so it reads as the primary action')
  assert.match(css, /\.chat__resume:active\s*\{\s*transform:\s*scale\(0\.97\)/,
    'the Resume button has :active press feedback')
})

test('ChatView mirrors the offscreen nudge for a scrolled-away resume card', () => {
  assert.match(chatView, /hasPendingResume/,
    'ChatView detects a tail resumable pause/park block')
  assert.match(chatView, /const pendingResumeBlock = \(\(\) => \{/,
    'the tail resumable block is found by walking the visible message tail')
  assert.match(chatView, /hasPendingResume && resumeCardOffscreen/,
    'the nudge shows only when the resume card is offscreen')
  assert.match(chatView, /Turn paused — tap to resume/,
    'the non-park nudge copy names the pause')
  assert.match(chatView, /Rate limit reached — tap to resume/,
    'the park variant names the rate limit')
  assert.match(chatView, /findResumeCard\(\)\?\.scrollIntoView/,
    'tapping the nudge scrolls the resume card into view')
  assert.match(css, /\.chat__resume-nudge/,
    'the resume nudge reuses the question-nudge visual style')
})

test('ariaStatus announces the recovery state instead of "Response ready."', () => {
  assert.match(chatView, /Turn paused — Resume available\./,
    'a paused turn announces the recovery state, not readiness')
  assert.match(chatView, /Rate limit reached, resets \$\{label\} — Resume available\./,
    'a park announces the reset label and that Resume is available')
  assert.match(chatView, /resumeStatus\s*\n?\s*\?\?/,
    'the recovery status takes precedence over the "Response ready." fallback')
})

test('sameBlock compares the error-card fields (stale-red-card guard)', () => {
  // A warm DB refresh can deliver a message differing ONLY in the error-card
  // fields (boot reconcile stamps resumable + pause_kind onto an existing
  // drain note). If sameBlock ignores them, commitMessages skips setMessages
  // and a stale red card stays on screen until a remount.
  const fn = chatView.match(/function sameBlock[\s\S]*?\n\}/)?.[0] ?? ''
  assert.match(fn, /a\.message === b\.message/,
    'sameBlock must compare the error message text')
  assert.match(fn, /a\.resumable === b\.resumable/,
    'sameBlock must compare resumable')
  assert.match(fn, /a\.parked_until === b\.parked_until/,
    'sameBlock must compare parked_until')
  assert.match(fn, /a\.park_reason === b\.park_reason/,
    'sameBlock must compare park_reason')
  assert.match(fn, /a\.pause_kind === b\.pause_kind/,
    'sameBlock must compare pause_kind')
})
