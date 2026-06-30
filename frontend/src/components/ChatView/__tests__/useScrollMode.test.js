import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  _computeSpacerH,
  isNearContentBottom,
  isNearScrollBottom,
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

test('shouldPinSend ignores dynamic pin spacer when checking bottom gap', () => {
  // Raw gap is 440px, but 400px is phantom spacer left by a previous pin.
  // Real content gap is 40px, but the reader is in the middle of the reserved
  // spacer, not the true scroll bottom. Sending should NOT yank them.
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
  }), false)
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
// pinTarget). The reachability reduces to: fullViewH >= clientHeight. When
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
  assert.equal(r.maxScrollTop, r.pinTarget, 'spacer reserves exactly enough — no phantom overscroll')
})

test('R5: a stale-small fullViewH undersizes the spacer and strands the pin mid-viewport (the bug)', () => {
  // The pre-fix path: visualViewport fired sizeSpacer with the keyboard-open
  // height (400) after clientHeight had already grown to 700.
  const r = pinReachable({ fullViewH: 400, clientHeight: 700, listH: 1040, lastUserTop: 1000 })
  assert.equal(r.reachable, false, 'stale-small fullViewH leaves the pin target unreachable')
  assert.ok(r.pinTarget - r.maxScrollTop > 250,
    'the message is stranded hundreds of px below the top — visually mid-viewport')
})
