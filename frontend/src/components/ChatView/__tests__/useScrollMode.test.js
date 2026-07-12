import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  _computeSpacerH,
  _pinReapplyNeeded,
  applyMode,
  isNearContentBottom,
  isNearScrollBottom,
  modeForChatExit,
  modeForForegroundReturn,
  modeForViewportChange,
  reservedSpacerH,
  shouldPinSend,
} from '../useScrollMode.js'
import {
  PIN_BOTTOM_ROOM,
  PIN_OFFSET,
  pinHeld,
  pinLanded,
  snapshotChatUX,
} from '../chatContract.js'

function makeScrollEl({ scrollHeight, scrollTop, clientHeight, spacerHeight = 0 }) {
  return {
    scrollHeight,
    scrollTop,
    clientHeight,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: spacerHeight }
      return null
    },
  }
}

test('shouldPinSend pins first visible user message regardless of scroll', () => {
  assert.equal(shouldPinSend({
    scrollEl: makeScrollEl({ scrollHeight: 2000, scrollTop: 0, clientHeight: 500 }),
    mode: { kind: 'ANCHOR_AT', key: 'old', offset: 0 },
    isFirstUserMsg: true,
  }), true)
})

test('shouldPinSend trusts actual scroll position over stale FOLLOW_BOTTOM mode', () => {
  assert.equal(shouldPinSend({
    scrollEl: makeScrollEl({ scrollHeight: 2000, scrollTop: 0, clientHeight: 500 }),
    mode: { kind: 'FOLLOW_BOTTOM' },
    isFirstUserMsg: false,
  }), false)
})

test('shouldPinSend can use a pre-blur bottom snapshot on mobile submit', () => {
  // Mobile send blurs the textarea, which can resize/clamp the viewport before
  // the pin decision runs. A true pre-blur bottom snapshot must win over the
  // post-blur geometry so send-at-bottom still pins the new user row.
  assert.equal(shouldPinSend({
    scrollEl: makeScrollEl({ scrollHeight: 2000, scrollTop: 0, clientHeight: 500 }),
    mode: { kind: 'ANCHOR_AT', key: 'old', offset: 0 },
    isFirstUserMsg: false,
    wasNearScrollBottom: true,
  }), true)
})

test('shouldPinSend uses FOLLOW_BOTTOM only when no scroll element is available', () => {
  assert.equal(shouldPinSend({
    scrollEl: null,
    mode: { kind: 'FOLLOW_BOTTOM' },
    isFirstUserMsg: false,
  }), true)
})

test('shouldPinSend can ignore stale follow mode for delayed queued insertion', () => {
  assert.equal(shouldPinSend({
    scrollEl: makeScrollEl({ scrollHeight: 2000, scrollTop: 0, clientHeight: 500 }),
    mode: { kind: 'FOLLOW_BOTTOM' },
    isFirstUserMsg: false,
    respectFollowMode: false,
  }), false)
})

test('shouldPinSend treats the bottom of real content as at-bottom, ignoring dynamic pin spacer', () => {
  // Raw gap is 440px, but 400px is phantom spacer left by a previous pin.
  // Real content gap is 40px: visually, the reader is at the conversation
  // tail. The next send should pin to the top even though the physical scroll
  // bottom includes empty reserved room below the messages.
  const scrollEl = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1000,
    clientHeight: 560,
    spacerHeight: 400,
  })
  assert.equal(shouldPinSend({
    scrollEl,
    mode: { kind: 'PIN_USER_MSG', cid: 'c-123' },
    isFirstUserMsg: false,
  }), true)
})

test('shouldPinSend still refuses to pin when real content gap is large', () => {
  const scrollEl = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 800,
    clientHeight: 560,
    spacerHeight: 400,
  })
  assert.equal(shouldPinSend({
    scrollEl,
    mode: { kind: 'PIN_USER_MSG', cid: 'c-123' },
    isFirstUserMsg: false,
  }), false)
})

test('isNearContentBottom uses the same phantom-spacer bottom contract', () => {
  const scrollEl = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1000,
    clientHeight: 560,
    spacerHeight: 400,
  })
  assert.equal(isNearContentBottom(scrollEl), true)
  assert.equal(isNearScrollBottom(scrollEl), false,
    'middle of reserved spacer is not true scroll bottom')
})

test('pin reapply is needed when the first pin was clamped but spacer now makes the target reachable', () => {
  const scrollEl = {
    scrollHeight: 2000,
    scrollTop: 500,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.chat__msg--user[data-cid="c-123"]') {
        return { offsetTop: 1000 }
      }
      return null
    },
  }

  assert.equal(
    _pinReapplyNeeded(scrollEl, { kind: 'PIN_USER_MSG', cid: 'c-123' }, 1000),
    true,
  )
})

test('pin reapply waits until the target is reachable to avoid stepwise pin jitter', () => {
  const scrollEl = {
    scrollHeight: 1500,
    scrollTop: 500,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.chat__msg--user[data-cid="c-123"]') {
        return { offsetTop: 1000 }
      }
      return null
    },
  }

  assert.equal(
    _pinReapplyNeeded(scrollEl, { kind: 'PIN_USER_MSG', cid: 'c-123' }, 1000),
    false,
  )
})

test('pin reapply holds a pinned send when streaming drags the viewport toward bottom', () => {
  const scrollEl = {
    scrollHeight: 2000,
    // Target is 996. This simulates browser/follow-bottom drift after content
    // streams below the pinned user row.
    scrollTop: 1200,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.chat__msg--user[data-cid="c-123"]') {
        return { offsetTop: 1000 }
      }
      return null
    },
  }

  assert.equal(
    _pinReapplyNeeded(scrollEl, { kind: 'PIN_USER_MSG', cid: 'c-123' }, 1000),
    true,
  )
})

test('pin reapply is idle when the pinned send is still at its target', () => {
  const scrollEl = {
    scrollHeight: 2000,
    scrollTop: 996,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.chat__msg--user[data-cid="c-123"]') {
        return { offsetTop: 1000 }
      }
      return null
    },
  }

  assert.equal(
    _pinReapplyNeeded(scrollEl, { kind: 'PIN_USER_MSG', cid: 'c-123' }, 1000),
    false,
  )
})

test('viewport resize at physical bottom retires stale pin mode', () => {
  const stalePin = { kind: 'PIN_USER_MSG', cid: 'c-123' }
  assert.deepEqual(
    modeForViewportChange(stalePin, true),
    { kind: 'FOLLOW_BOTTOM' },
  )
  assert.equal(
    modeForViewportChange(stalePin, false),
    stalePin,
  )
})

test('viewport resize in reserved spacer anchors instead of snapping to bottom', () => {
  const staleFollow = { kind: 'FOLLOW_BOTTOM' }
  const anchor = { kind: 'ANCHOR_AT', key: 'user-1', offset: -240 }

  assert.equal(
    modeForViewportChange(staleFollow, false, anchor),
    anchor,
  )
})

test('foreground return preserves FOLLOW_BOTTOM at the physical tail', () => {
  assert.deepEqual(
    modeForForegroundReturn(makeScrollEl({
      scrollHeight: 1600,
      scrollTop: 1000,
      clientHeight: 600,
    })),
    { kind: 'FOLLOW_BOTTOM' },
  )
})

test('foreground return anchors the current reading position when scrolled up', () => {
  const item = {
    offsetTop: 720,
    offsetHeight: 120,
    dataset: { key: 'assistant-7' },
  }
  const scrollEl = {
    scrollHeight: 1800,
    scrollTop: 660,
    clientHeight: 600,
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [item] : []
    },
  }

  assert.deepEqual(
    modeForForegroundReturn(scrollEl),
    { kind: 'ANCHOR_AT', key: 'assistant-7', offset: 60 },
  )
})

test('chat exit freezes the visible anchor even at the physical tail', () => {
  const item = {
    offsetTop: 1200,
    offsetHeight: 220,
    dataset: { key: 'assistant-tail' },
  }
  const scrollEl = {
    scrollHeight: 1800,
    scrollTop: 1000,
    clientHeight: 800,
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [item] : []
    },
  }

  assert.deepEqual(
    modeForChatExit(scrollEl),
    { kind: 'ANCHOR_AT', key: 'assistant-tail', offset: 200 },
  )
})

test('chat exit falls back to follow mode only when no message anchor exists', () => {
  const scrollEl = {
    scrollHeight: 1800,
    scrollTop: 1000,
    clientHeight: 800,
    querySelectorAll() { return [] },
  }

  assert.deepEqual(modeForChatExit(scrollEl), { kind: 'FOLLOW_BOTTOM' })
})

test('applyMode PIN is a no-op when the cid resolves no row (strict, no fallback)', () => {
  // The ts-swap that once forced a last-row fallback cannot happen: the row
  // carries its final cid from mint. An unresolved cid pins nothing (the
  // fallback limb + its diverged-ts _pinReapplyNeeded twin are deleted).
  const scrollEl = {
    scrollHeight: 3000,
    clientHeight: 800,
    scrollTop: 42,
    querySelector() { return null },
    querySelectorAll() {
      throw new Error('cid selector is strict — must never call querySelectorAll')
    },
  }
  applyMode(scrollEl, { kind: 'PIN_USER_MSG', cid: 'c-missing' })
  assert.equal(scrollEl.scrollTop, 42, 'scrollTop untouched when cid unresolved')
})

test('applyMode PIN resolves the row by its exact data-cid', () => {
  const scrollEl = {
    scrollHeight: 3000,
    clientHeight: 800,
    scrollTop: 0,
    querySelector(sel) {
      return sel === '.chat__msg--user[data-cid="c-123"]' ? { offsetTop: 500 } : null
    },
    querySelectorAll() {
      throw new Error('exact match present — must not fall back to last user row')
    },
  }
  applyMode(scrollEl, { kind: 'PIN_USER_MSG', cid: 'c-123' })
  assert.equal(scrollEl.scrollTop, 496)
})

function makeSpacerScrollEl({ clientHeight, queuedTray = null }) {
  return {
    clientHeight,
    parentElement: {
      querySelector(selector) {
        if (selector === '.queued') return queuedTray
        return null
      },
    },
  }
}

test('spacer reservation is independent from pin mode', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 600 })
  const listEl = { offsetHeight: 900 }
  const lastUserMsgEl = { offsetTop: 700 }

  assert.equal(
    _computeSpacerH(scrollEl, listEl, lastUserMsgEl, 600),
    396,
  )
})

test('spacer reservation returns zero before there is a user message', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 600 })
  const listEl = { offsetHeight: 200 }

  assert.equal(_computeSpacerH(scrollEl, listEl, null, 600), 0)
})

test('queued tray does not shorten spacer reservation', () => {
  // `.chat__list` bottom padding already includes the full measured footer
  // height (queue tray + composer). Subtracting the tray again makes the
  // latest user message unable to reach the top while queued rows are visible.
  const queuedTray = {
    offsetHeight: 120,
  }
  const scrollEl = makeSpacerScrollEl({ clientHeight: 600, queuedTray })
  const listEl = { offsetHeight: 900 }
  const lastUserMsgEl = { offsetTop: 700 }

  assert.equal(
    _computeSpacerH(scrollEl, listEl, lastUserMsgEl, 600),
    396,
  )
})

// R5 regression contract: a send while at the bottom must pin the new user
// message to the TOP, which requires the dynamic spacer to reserve enough
// bottom room that the pin target is actually REACHABLE (maxScrollTop >=
// pinTarget). By default it reserves EXACTLY that — no extra cushion — so
// maxScrollTop == pinTarget and the row rests flush at the top. When
// fullViewH is stale-SMALL (the keyboard-open height used after the keyboard
// has already closed and grown clientHeight), the spacer is undersized, the
// pin clamps short, and the message lands mid-viewport. The fix keeps
// fullViewHRef >= clientHeight at every sizeSpacer() call (grow guard), so
// this asserts the math the fix preserves.
function pinReachable({ fullViewH, clientHeight, listH, lastUserTop }) {
  const scrollEl = makeScrollEl({
    scrollHeight: 0, scrollTop: 0, clientHeight,
  })
  const listEl = { offsetHeight: listH }
  const lastUserMsgEl = { offsetTop: lastUserTop }
  const spacerH = _computeSpacerH(scrollEl, listEl, lastUserMsgEl, fullViewH)
  const scrollHeight = listH + spacerH
  const maxScrollTop = scrollHeight - clientHeight
  const pinTarget = Math.max(0, lastUserTop - 4) // PIN_OFFSET = 4
  return { spacerH, maxScrollTop, pinTarget, reachable: maxScrollTop >= pinTarget }
}

test('R5: spacer keeps the pin reachable when fullViewH tracks the (grown) clientHeight', () => {
  // Keyboard just closed: clientHeight grew back to 700. With the grow guard,
  // fullViewH is >= clientHeight, so the pin target is reachable → top pin.
  const r = pinReachable({ fullViewH: 700, clientHeight: 700, listH: 1040, lastUserTop: 1000 })
  assert.equal(r.reachable, true, 'message can reach the top when fullViewH >= clientHeight')
  assert.equal(r.maxScrollTop, r.pinTarget, 'spacer reserves exactly enough to reach the pin — no extra cushion')
  assert.equal(r.maxScrollTop - r.pinTarget, 0, 'no reservable blank below the pinned message by default')
})

test('R5: a stale-small fullViewH undersizes the spacer and strands the pin mid-viewport (the bug)', () => {
  // The pre-fix path: visualViewport fired sizeSpacer with the keyboard-open
  // height (400) after clientHeight had already grown to 700.
  const r = pinReachable({ fullViewH: 400, clientHeight: 700, listH: 1040, lastUserTop: 1000 })
  assert.equal(r.reachable, false, 'stale-small fullViewH leaves the pin target unreachable')
  assert.ok(r.pinTarget - r.maxScrollTop > 80,
    'the message is still stranded far below the top — visually mid-viewport')
})


// ---------------------------------------------------------------------------
// F1 — the 2nd-and-later direct send must keep pinning through the thinking
// pause. The ts-swap retarget used to collapse the spacer to 0px; that shrinks
// scrollHeight below the viewport and the browser CLAMPS scrollTop to 0. On a
// same-last-message commit sameMessageList skips the re-render, so no layout
// effect runs to restore the spacer, and the message strands at the top-of-
// content offset instead of the pin. Two invariants below:
//   (a) the fix (never collapse): a settled pin HOLDS through the pause; and
//   (b) the settle path: a clamped-but-now-reachable pin re-applies regardless
//       of the identity gate.
// ---------------------------------------------------------------------------

/** A minimal mutable scroll element: scrollHeight tracks listH + spacer, and
 *  scrollTop writes clamp to [0, maxScrollTop] exactly as a browser does when
 *  the spacer shrinks. Enough to drive applyMode + _pinReapplyNeeded. */
function makePinnableScrollEl({ listH, spacerH, clientHeight, userTop, cid }) {
  return {
    clientHeight,
    _spacer: spacerH,
    _top: 0,
    get scrollHeight() { return listH + this._spacer },
    get scrollTop() { return this._top },
    set scrollTop(v) {
      const max = Math.max(0, this.scrollHeight - this.clientHeight)
      this._top = Math.max(0, Math.min(v, max))
    },
    setSpacer(h) {
      this._spacer = h
      // The browser re-clamps scrollTop when scrollHeight shrinks below it.
      const max = Math.max(0, this.scrollHeight - this.clientHeight)
      if (this._top > max) this._top = max
    },
    querySelector(sel) {
      if (sel === '.spacer-dynamic') return { offsetHeight: this._spacer }
      if (sel === `.chat__msg--user[data-cid="${cid}"]`) return { offsetTop: userTop }
      return null
    },
  }
}

function snapOf(el, userTop) {
  return snapshotChatUX({ scrollEl: el, lastUserMsgEl: { offsetTop: userTop } })
}

test('F1: a settled pin HOLDS through the thinking pause when the retarget leaves the spacer alone', () => {
  const userTop = 133
  const el = makePinnableScrollEl({ listH: 400, spacerH: 824, clientHeight: 915, userTop, cid: 'c-111' })
  applyMode(el, { kind: 'PIN_USER_MSG', cid: 'c-111' })
  const before = snapOf(el, userTop)
  assert.ok(pinLanded(before).ok, 'optimistic pin lands flush at the top')
  assert.equal(before.pinGap, PIN_OFFSET)

  // The ts-swap retarget fires during the thinking pause. With the fix it does
  // NOT touch the spacer, so scrollHeight is unchanged and scrollTop is never
  // clamped — even though the same-last-message commit runs no layout effect.
  const after = snapOf(el, userTop)
  assert.ok(pinHeld(before, after).ok, 'the row is still at the top after the pause')
})

test('F1: a collapse-clamped pin is recovered by the settle once the spacer restores reachability', () => {
  const userTop = 133
  const el = makePinnableScrollEl({ listH: 400, spacerH: 824, clientHeight: 915, userTop, cid: 'c-111' })
  const mode = { kind: 'PIN_USER_MSG', cid: 'c-111' }
  applyMode(el, mode)
  const lastPinTop = userTop
  assert.equal(el.scrollTop, userTop - PIN_OFFSET)

  // The old retarget zeroed the spacer -> scrollHeight shrinks below the
  // viewport -> the browser clamps scrollTop to 0 (the stranded bug state).
  el.setSpacer(0)
  assert.equal(el.scrollTop, 0, 'spacer collapse clamps scrollTop to 0')
  assert.ok(!pinLanded(snapOf(el, userTop)).ok, 'the clamped state is a pin violation')
  assert.equal(_pinReapplyNeeded(el, mode, lastPinTop), false,
    'nothing to re-pin to while the target is unreachable')

  // The layout effect's sizeSpacer restores the reservation, making the target
  // reachable again. The settle MUST now fire regardless of the identity gate.
  el.setSpacer(824)
  assert.equal(_pinReapplyNeeded(el, mode, lastPinTop), true,
    'a clamped-but-now-reachable pin needs re-applying')
  applyMode(el, mode)
  assert.ok(pinLanded(snapOf(el, userTop)).ok, 'the settle re-pins flush at the top')
  assert.equal(el.scrollTop, userTop - PIN_OFFSET)
})


// ---------------------------------------------------------------------------
// F2 — a chat mounted idle (restored, no send this session) must reserve NO
// spacer. Otherwise FOLLOW_BOTTOM's scroll-to-scrollHeight includes the phantom
// spacer and a short completed chat mounts with every message above the fold.
// ---------------------------------------------------------------------------

test('F2: an idle-mounted chat (no send this session) reserves no spacer; an armed chat reserves room', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 915 })
  const listEl = { offsetHeight: 260 }        // 2-message short chat, fits the viewport
  const lastUserMsgEl = { offsetTop: 8 }
  assert.ok(
    reservedSpacerH(scrollEl, listEl, lastUserMsgEl, 915, { active: true }) > 500,
    'an armed chat reserves room to serve a pin',
  )
  assert.equal(
    reservedSpacerH(scrollEl, listEl, lastUserMsgEl, 915, { active: false }),
    0,
    'no send this session -> no phantom spacer',
  )
})

test('F2: with no spacer FOLLOW_BOTTOM leaves a short completed chat on-screen (the un-gated spacer pushed it above the fold)', () => {
  const userTop = 8
  const shortList = 260
  const clientHeight = 915

  // Fix: idle mount reserves 0 -> content does not overflow -> FOLLOW is a no-op.
  const fixed = makePinnableScrollEl({ listH: shortList, spacerH: 0, clientHeight, userTop, cid: 'c-1' })
  applyMode(fixed, { kind: 'FOLLOW_BOTTOM' })
  assert.equal(fixed.scrollTop, 0, 'no overflow -> no scroll -> messages at the top')
  assert.ok(userTop - fixed.scrollTop >= 0, 'the message is on-screen')

  // Bug: reserving the full spacer while idle overflows the viewport, so FOLLOW
  // scrolls to scrollHeight and the chat's earlier messages sit above the fold.
  // The last user row sits lower here (a short exchange, not a first message),
  // which is the shape that visibly blanked the screen on open.
  const lowUserTop = 200
  const buggySpacer = _computeSpacerH(
    { clientHeight }, { offsetHeight: shortList }, { offsetTop: lowUserTop }, clientHeight,
  )
  const buggy = makePinnableScrollEl({ listH: shortList, spacerH: buggySpacer, clientHeight, userTop: lowUserTop, cid: 'c-1' })
  applyMode(buggy, { kind: 'FOLLOW_BOTTOM' })
  const firstMsgTop = 8
  assert.ok(firstMsgTop - buggy.scrollTop < 0, 'the un-gated spacer scrolls earlier messages above the fold')
})


// ---------------------------------------------------------------------------
// F4 — returning to the foreground while a turn is STREAMING must freeze the
// reader where they were (anchor), even at the tail; keep-FOLLOW-at-tail is only
// right for an idle chat where nothing grew while backgrounded.
// ---------------------------------------------------------------------------

test('F4: foreground return freezes a STREAMING turn as an anchor even at the tail; idle keeps FOLLOW', () => {
  const tailItem = { offsetTop: 1200, offsetHeight: 200, dataset: { key: 'a-9' } }
  const scrollEl = {
    scrollHeight: 1400, scrollTop: 685, clientHeight: 700,   // near the tail
    querySelectorAll(sel) { return sel === '.chat__msg[data-key]' ? [tailItem] : [] },
  }
  assert.equal(isNearScrollBottom(scrollEl), true, 'precondition: at the tail')

  const streaming = modeForForegroundReturn(scrollEl, { streaming: true })
  assert.equal(streaming.kind, 'ANCHOR_AT',
    'streaming return freezes as an anchor, not the grown tail')
  assert.equal(streaming.key, 'a-9')

  const idle = modeForForegroundReturn(scrollEl, { streaming: false })
  assert.deepEqual(idle, { kind: 'FOLLOW_BOTTOM' },
    'an idle chat at the tail keeps following')
})
