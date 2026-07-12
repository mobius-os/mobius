import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  _computeSpacerH,
  _pinReapplyNeeded,
  isNearContentBottom,
  isNearScrollBottom,
  modeForChatExit,
  modeForForegroundReturn,
  modeForViewportChange,
  shouldPinSend,
} from '../useScrollMode.js'

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
    mode: { kind: 'PIN_USER_MSG', ts: 123 },
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
    mode: { kind: 'PIN_USER_MSG', ts: 123 },
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
      if (selector === '.chat__msg--user[data-ts="123"]') {
        return { offsetTop: 1000 }
      }
      return null
    },
  }

  assert.equal(
    _pinReapplyNeeded(scrollEl, { kind: 'PIN_USER_MSG', ts: 123 }, 1000),
    true,
  )
})

test('pin reapply waits until the target is reachable to avoid stepwise pin jitter', () => {
  const scrollEl = {
    scrollHeight: 1500,
    scrollTop: 500,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.chat__msg--user[data-ts="123"]') {
        return { offsetTop: 1000 }
      }
      return null
    },
  }

  assert.equal(
    _pinReapplyNeeded(scrollEl, { kind: 'PIN_USER_MSG', ts: 123 }, 1000),
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
      if (selector === '.chat__msg--user[data-ts="123"]') {
        return { offsetTop: 1000 }
      }
      return null
    },
  }

  assert.equal(
    _pinReapplyNeeded(scrollEl, { kind: 'PIN_USER_MSG', ts: 123 }, 1000),
    true,
  )
})

test('pin reapply is idle when the pinned send is still at its target', () => {
  const scrollEl = {
    scrollHeight: 2000,
    scrollTop: 996,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.chat__msg--user[data-ts="123"]') {
        return { offsetTop: 1000 }
      }
      return null
    },
  }

  assert.equal(
    _pinReapplyNeeded(scrollEl, { kind: 'PIN_USER_MSG', ts: 123 }, 1000),
    false,
  )
})

test('viewport resize at physical bottom retires stale pin mode', () => {
  const stalePin = { kind: 'PIN_USER_MSG', ts: 123 }
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

  // Deficit-only: 600 + (700 − 4) − 900. No flat cushion — see the
  // _computeSpacerH docblock for why any additive bottom room breaks
  // the send-rule / spacer lock-in specs.
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

test('fits-viewport chat: no phantom room — the pin sits at the true content bottom', () => {
  // The geometry the overflow tests above never cover: a short chat whose
  // whole conversation (listH 191) fits the viewport (viewH 857) — a first
  // short send on a phone. A flat PIN_BOTTOM_ROOM cushion here reserved
  // 180px of phantom scroll room below the pinned row, so the true-bottom
  // gap read ≈ ROOM, isNearContentBottom classified the reader as away
  // from the tail, and the NEXT send refused to pin (the send-rule
  // "second send still pins" regression). Deficit-only sizing makes
  // maxScrollTop land exactly on the pin target: reachable, no void below.
  const scrollEl = makeSpacerScrollEl({ clientHeight: 857 })
  const listEl = { offsetHeight: 191 }
  const lastUserMsgEl = { offsetTop: 8 }

  const spacerH = _computeSpacerH(scrollEl, listEl, lastUserMsgEl, 857)
  assert.equal(spacerH, 670) // 857 + (8 − 4) − 191
  const maxScrollTop = (191 + spacerH) - 857
  const pinTarget = 8 - 4
  assert.equal(maxScrollTop, pinTarget,
    'pin target is exactly reachable with zero phantom room below')
  assert.ok(spacerH <= 857,
    'reserved spacer stays within one viewport on a fits chat')
})

test('spacer decays continuously to zero as content outgrows the viewport', () => {
  // Continuity guard across the overflow boundary: as content streams in,
  // the spacer shrinks 1:1 with listH growth and reaches EXACTLY 0 once
  // the content below the pin fills the viewport (spacer.spec case 5's
  // lock-in) — no residual and no scrollHeight step mid-stream.
  const scrollEl = makeSpacerScrollEl({ clientHeight: 600 })
  const lastUserMsgEl = { offsetTop: 100 }

  // listH == viewH: only the pin deficit remains.
  assert.equal(
    _computeSpacerH(scrollEl, { offsetHeight: 600 }, lastUserMsgEl, 600),
    96,
  )
  // 90px more content → 90px less spacer.
  assert.equal(
    _computeSpacerH(scrollEl, { offsetHeight: 690 }, lastUserMsgEl, 600),
    6,
  )
  // Content below the pin fills the viewport → spacer is exactly 0.
  assert.equal(
    _computeSpacerH(scrollEl, { offsetHeight: 900 }, lastUserMsgEl, 600),
    0,
  )
})

// R5 regression contract: a send while at the bottom must pin the new user
// message to the TOP, which requires the dynamic spacer to reserve enough
// bottom room that the pin target is actually REACHABLE (maxScrollTop >=
// pinTarget). When fullViewH is stale-SMALL (the keyboard-open height used
// after the keyboard has already closed and grown clientHeight), the spacer
// is undersized, the pin clamps short, and the message lands mid-viewport.
// The fix keeps fullViewHRef >= clientHeight at every sizeSpacer() call
// (grow guard), so this asserts the math the fix preserves.
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
  assert.equal(r.maxScrollTop, r.pinTarget,
    'deficit-only spacer lands maxScrollTop exactly on the pin target — no phantom room')
})

test('R5: a stale-small fullViewH undersizes the spacer and strands the pin mid-viewport (the bug)', () => {
  // The pre-fix path: visualViewport fired sizeSpacer with the keyboard-open
  // height (400) after clientHeight had already grown to 700.
  const r = pinReachable({ fullViewH: 400, clientHeight: 700, listH: 1040, lastUserTop: 1000 })
  assert.equal(r.reachable, false, 'stale-small fullViewH leaves the pin target unreachable')
  assert.ok(r.pinTarget - r.maxScrollTop > 80,
    'the message is still stranded far below the top — visually mid-viewport')
})
