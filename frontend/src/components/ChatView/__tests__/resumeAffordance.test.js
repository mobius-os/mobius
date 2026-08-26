import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { sameMessageList } from '../chatMessageList.js'

// The one-tap Resume affordance (design §2.2): a turn paused by a drain-gated
// restart (or interrupted by a crash) persists a `resumable` error note; the
// tail note renders a Resume button that re-sends a short "continue".
const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

// Slice ONE JSX element's own text, from its tag to the matching close of that
// tag. A source assertion about an element's props is worthless without this:
// with an unbounded `[\s\S]*?` between the tag and the prop, a DIFFERENT
// element's props further down the file satisfy the match, so "both render
// paths pass the ref" can pass with one of them not passing it at all.
// Scan the opening tag that begins at `start` (source[start] === '<'), skipping
// over quoted strings ('...', "...", `...`) and balanced { } JSX expression
// containers so that a '>' inside an arrow prop (onClick={() => ...}), a
// comparison, or a comment does NOT prematurely end the tag. Returns the index
// of the tag-closing '>' and whether the tag is self-closing ('/>').
function scanOpeningTag(source, start) {
  for (let i = start + 1, brace = 0; i < source.length; i++) {
    const c = source[i]
    if (c === '"' || c === "'" || c === '`') {
      i++
      while (i < source.length && source[i] !== c) i++
      continue
    }
    if (c === '{') { brace++; continue }
    if (c === '}') { brace--; continue }
    if (brace > 0) continue
    if (c === '/' && source[i + 1] === '>') return { gt: i + 1, selfClosing: true }
    if (c === '>') return { gt: i, selfClosing: false }
  }
  return { gt: -1, selfClosing: false }
}

function sliceElement(source, openTag) {
  const from = source.indexOf(openTag)
  assert.ok(from >= 0, `expected to find ${openTag}`)
  // Recover the tag name from the opening tag (e.g. '<button' -> 'button',
  // '<div\n className=...' -> 'div') so we can find its real close.
  const nameMatch = /^<\s*([A-Za-z][\w.]*)/.exec(openTag)
  assert.ok(nameMatch, `openTag must start with a tag name: ${openTag}`)
  const tagName = nameMatch[1]

  const opening = scanOpeningTag(source, from)
  assert.notEqual(opening.gt, -1, `unterminated opening tag ${openTag}`)

  if (opening.selfClosing) {
    const slice = source.slice(from, opening.gt + 1)
    assert.ok(slice.endsWith('/>'),
      `self-closing slice for ${openTag} must end with '/>'`)
    return slice
  }

  // Not self-closing: walk forward, quote/brace aware, tracking nesting of
  // same-named tags, until the matching '</tagName>' close.
  const close = `</${tagName}`
  const nestedOpen = new RegExp(`^<\\s*${tagName}[\\s/>]`)
  let depth = 1
  for (let i = opening.gt + 1, brace = 0; i < source.length; i++) {
    const c = source[i]
    if (c === '"' || c === "'" || c === '`') {
      i++
      while (i < source.length && source[i] !== c) i++
      continue
    }
    if (c === '{') { brace++; continue }
    if (c === '}') { brace--; continue }
    if (brace > 0) continue
    if (c !== '<') continue
    if (source.startsWith(close, i)) {
      depth--
      if (depth === 0) {
        const gt = source.indexOf('>', i)
        assert.notEqual(gt, -1, `unterminated close for ${openTag}`)
        const slice = source.slice(from, gt + 1)
        const tail = slice.replace(/\s+/g, ' ')
        assert.ok(tail.endsWith(`${close}>`) || tail.endsWith(`${close} >`),
          `slice for ${openTag} must end with ${close}>`)
        return slice
      }
      continue
    }
    // A nested opening tag of the same name deepens the nesting — unless it is
    // self-closing, which needs no matching close.
    if (nestedOpen.test(source.slice(i))) {
      const nested = scanOpeningTag(source, i)
      if (nested.gt !== -1 && !nested.selfClosing) depth++
    }
  }
  assert.fail(`unterminated element ${openTag}`)
}

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
    /className="chat__resume"[\s\S]*?onClick=\{\(\)\s*=>\s*onResume\('continue',\s*\{[\s\S]*?continuation:\s*'manual'[\s\S]*?pin:\s*false/,
    'the Resume button must open a manual product-owned continuation',
  )
})

test('MsgContent memo compares onResume so a stable ref skips re-render', () => {
  assert.match(msgContent, /prev\.onResume === next\.onResume/,
    'the memo comparator must include onResume')
})

test('ChatView wires MsgContent.onResume to the normal send', () => {
  assert.match(chatView, /<MsgContent[\s\S]*?onResume=\{doSend\}/,
    'ChatView must pass its stable doSend as onResume so tapping Resume ' +
      'uses the ordinary durable send boundary without a visible user row')
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

test('ChatView routes both offscreen attention nudges through the controller', () => {
  assert.match(chatView, /hasPendingResume/,
    'ChatView detects a tail resumable pause/park block')
  assert.match(chatView, /const pendingResumeBlock = tailResumableBlock\(messages\)/,
    'the tail resumable block is found by walking the visible message tail')
  assert.match(chatView, /hasPendingResume && resumeCardOffscreen/,
    'the nudge shows only when the resume card is offscreen')
  assert.match(chatView, /Turn paused — tap to resume/,
    'the non-park nudge copy names the pause')
  assert.match(chatView, /Queued to continue/,
    'an automatic park nudge names the queued outcome')
  assert.match(chatView, /Usage available — tap to continue/,
    'an elapsed manual park names its now-available action')
  assert.match(
    chatView,
    /className="chat__question-nudge"\s+onClick=\{revealConversationTail\}/,
    'the question nudge routes through the scroll controller',
  )
  assert.match(
    chatView,
    /className="chat__resume-nudge"\s+onClick=\{revealConversationTail\}/,
    'the resume nudge routes through the scroll controller',
  )
  assert.doesNotMatch(chatView, /scrollIntoView/,
    'nearest-element scrolling can strand either primary action behind the composer')
  assert.match(css, /\.chat__resume-nudge/,
    'the resume nudge reuses the question-nudge visual style')
})

test('viewport-derived nudges never participate in footer geometry', () => {
  const layer = sliceElement(chatView, '<div className="chat__offscreen-nudges">')
  assert.match(layer, /className="chat__question-nudge"/,
    'the question cue belongs to the geometry-neutral layer')
  assert.match(layer, /className="chat__resume-nudge"/,
    'the resume cue shares the same geometry owner')

  const layerCss = css.match(/\.chat__offscreen-nudges\s*\{[\s\S]*?\}/)?.[0] ?? ''
  assert.doesNotMatch(layerCss, /position:\s*absolute|bottom:/,
    'the cue uses the shared floating stack instead of overlapping sibling cards')
  assert.match(layerCss, /width:\s*min\(100%,\s*720px\)/,
    'the cue stays inside the composer column')
  assert.match(layerCss, /pointer-events:\s*none/,
    'the overlay lane itself must not block transcript interaction')

  const stackCss = css.match(/\.chat__floating-actions\s*\{[\s\S]*?\}/)?.[0] ?? ''
  assert.match(stackCss, /position:\s*absolute/,
    'the shared parent keeps every cue outside measured footer geometry')
  assert.match(
    stackCss,
    /bottom:\s*calc\(100% \+ var\(--chat-foot-card-gap\)\)/,
    'the transient stack stays geometry-neutral and clear of the composer',
  )

  const nudgeCss = css.match(
    /\.chat__question-nudge,\s*\n\.chat__resume-nudge\s*\{[\s\S]*?\}/,
  )?.[0] ?? ''
  assert.match(nudgeCss, /margin:\s*0 auto/,
    'the absolute lane, not a flow margin, owns nudge placement')
  assert.doesNotMatch(nudgeCss, /margin:\s*0 auto 8px/,
    'a viewport-derived nudge cannot reserve footer height')
})

test('both attention nudges observe a node published by the card, not a lookup', () => {
  // The nudges track a card that changes DOM node mid-turn: the live streaming
  // surface renders the pending question while the turn runs, the durable
  // message row renders it once the turn parks. A querySelector taken when the
  // observer binds cannot see that swap, so the observer was left on a detached
  // node — and with the turn parked awaiting the answer nothing re-renders, so
  // the pill stuck forever. Node identity has to be the hook's input.
  assert.doesNotMatch(chatView, /querySelectorAll\('\.qcard|querySelectorAll\('\.chat__resume/,
    'neither nudge may locate its card by query')
  assert.match(chatView, /const \[pendingQuestionEl, pendingQuestionRef\] = useNudgeTargetRef\(\)/,
    'the pending question card publishes its node through a callback ref')
  assert.match(chatView, /useOffscreenNudge\(\s*scrollRef, hasPendingQuestion, pendingQuestionEl,/,
    'the question nudge observes the published node')
  assert.match(chatView, /const \[resumeCardEl, resumeCardRef\] = useNudgeTargetRef\(\)/,
    'the resume card publishes its node the same way — no parallel mechanism')
  assert.match(chatView, /useOffscreenNudge\(\s*scrollRef, hasPendingResume, resumeCardEl,/,
    'the resume nudge observes the published node')

  // BOTH render paths must publish through the SAME ref so the live→durable
  // handoff reaches the observer as an ordinary node swap. Each element is
  // sliced to its OWN text first: an unbounded wildcard between the tag and the
  // prop lets one call site satisfy both patterns, which makes deleting the
  // refs from the durable row — the reported bug, exactly — undetectable.
  for (const [label, openTag] of [
    ['durable message rows', '<MsgContent'],
    ['the live active surface', '<ActiveAssistantSurface'],
  ]) {
    const element = sliceElement(chatView, openTag)
    assert.match(element, /pendingQuestionRef=\{pendingQuestionRef\}/,
      `${label} must publish the question card through the shared ref`)
    assert.match(element, /resumeCardRef=\{resumeCardRef\}/,
      `${label} must publish the resume card through the shared ref`)
  }
  // The live surface reaches MsgContent through two more components, and a hop
  // that accepts the prop without forwarding it kills the cue for the whole
  // live half of the turn — silently, since the durable half still works.
  for (const [file, child] of [
    ['../ActiveAssistantSurface.jsx', '<StreamingMessage'],
    ['../StreamingMessage.jsx', '<MsgContent'],
  ]) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8')
    const element = sliceElement(source, child)
    for (const prop of ['pendingQuestionRef', 'resumeCardRef']) {
      assert.match(element, new RegExp(`${prop}=\\{${prop}\\}`),
        `${file} must forward ${prop} to ${child}`)
    }
  }
  // Only the card that actually blocks the turn registers: an answered question
  // or a scrolled-back history card is not somewhere to send the owner back to.
  assert.match(msgContent, /pendingCardRef=\{answerable \? pendingQuestionRef : undefined\}/,
    'only the answerable tail question publishes its node')
  // ONE publisher per ref. `resumable` gates on isLastMsg, not tail position, so
  // a last message with two resumable blocks renders two Resume buttons; a
  // shared single-slot ref would then be nulled by whichever unmounts first and
  // the cue would go dark with the real button still offscreen. The tail is also
  // what arms the cue (tailResumableBlock), so both must name the same block.
  assert.match(msgContent, /cardRef=\{resumable && i === lastEntryIdx \? resumeCardRef : undefined\}/,
    'only the TAIL resumable note publishes the complete recovery card')
  const questionCard = readFileSync(new URL('../QuestionCard.jsx', import.meta.url), 'utf8')
  const qcard = sliceElement(questionCard, '<div\n      className={`qcard')
  assert.match(qcard, /ref=\{answered \? (null|undefined) : pendingCardRef\}/,
    'a submitted card retires itself from the cue')
})

test('ariaStatus announces the recovery state instead of "Response ready."', () => {
  assert.match(chatView, /Turn paused — Resume available\./,
    'a paused turn announces the recovery state, not readiness')
  assert.match(chatView, /Usage limit reached\. Queued to continue \$\{label\}\./,
    'an automatic park announces the queued state')
  assert.match(chatView, /Usage is available again\. Continue available\./,
    'an elapsed manual park announces the available action')
  assert.match(chatView, /resumeStatus\s*\n?\s*\?\?/,
    'the recovery status takes precedence over the "Response ready." fallback')
})

test('message equality compares the error-card fields (stale-red-card guard)', () => {
  // A warm DB refresh can deliver a message differing ONLY in the error-card
  // fields (boot reconcile stamps resumable + a pause descriptor onto an
  // existing drain note). If equality ignores them, commitMessages skips
  // setMessages and a stale red card stays on screen until a remount. The
  // refreshed JSON object must therefore compare unequal.
  const oldRows = [{
    role: 'assistant', content: '',
    blocks: [{ type: 'error', message: 'Interrupted', resumable: false }],
  }]
  const recoveredRows = [{
    role: 'assistant', content: '',
    blocks: [{
      type: 'error', message: 'Paused', resumable: true,
      pause: { kind: 'rate_limit', resets_at: '2026-07-15T00:00:00Z' },
    }],
  }]
  assert.equal(sameMessageList(oldRows, recoveredRows), false)
})
