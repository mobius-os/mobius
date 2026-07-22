import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  _anchorModeIntersectsContent,
  _anchorReapplyNeeded,
  _computeSpacerH,
  _modeForPersistence,
  _pinReapplyNeeded,
  _scrollModeForDiagnostics,
  _validateSavedMode,
  applyMode,
  bottomAnchorModeFromScroll,
  contentHoldModeFromScroll,
  gestureLayoutRetryDelay,
  isNearContentBottom,
  isNearScrollBottom,
  layoutMayOwnScroll,
  mountMediaSettled,
  modeForChatExit,
  modeForDisclosureToggle,
  modeForForegroundReturn,
  modeForQuestionSubmission,
  modeForQueuedSubmission,
  modeForViewportChange,
  modeAfterReaderReachesBottom,
  modeAfterSpacerResize,
  modeAfterTerminalLayout,
  readerInputActivatesDisclosure,
  readerInputMayScroll,
  readerInputNeedsFrameRelease,
  settledPinMode,
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

test('mount media readiness waits for token insertion and image decode', () => {
  const frame = img => ({ querySelector: selector => selector === 'img' ? img : null })
  const scrollEl = frames => ({
    querySelectorAll: selector => selector === '.md-image-frame' ? frames : [],
  })

  assert.equal(mountMediaSettled(scrollEl([])), true)
  assert.equal(mountMediaSettled(scrollEl([frame(null)])), false)
  assert.equal(mountMediaSettled(scrollEl([frame({ complete: false })])), false)
  assert.equal(mountMediaSettled(scrollEl([
    frame({ complete: true }),
    frame({ complete: true, naturalWidth: 0 }),
  ])), true, 'decoded and terminally-failed images both release the bounded gate')
})

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

test('shouldPinSend can use a complete pre-blur auto-scroll snapshot on mobile submit', () => {
  // Mobile send blurs the textarea, which can resize/clamp the viewport before
  // the pin decision runs. A true pre-blur bottom snapshot must win over the
  // post-blur geometry so send-at-bottom still pins the new user row.
  assert.equal(shouldPinSend({
    scrollEl: makeScrollEl({ scrollHeight: 2000, scrollTop: 0, clientHeight: 500 }),
    mode: { kind: 'ANCHOR_AT', key: 'old', offset: 0 },
    isFirstUserMsg: false,
    wasAtContentBottom: true,
  }), true)
})

test('shouldPinSend uses FOLLOW_BOTTOM only when no scroll element is available', () => {
  assert.equal(shouldPinSend({
    scrollEl: null,
    mode: { kind: 'FOLLOW_BOTTOM' },
    isFirstUserMsg: false,
  }), true)
})

test('shouldPinSend holds delayed insertion when submit-time intent is unavailable', () => {
  assert.equal(shouldPinSend({
    scrollEl: makeScrollEl({ scrollHeight: 2000, scrollTop: 0, clientHeight: 500 }),
    mode: { kind: 'FOLLOW_BOTTOM' },
    isFirstUserMsg: false,
    wasAtContentBottom: false,
  }), false)
})

test('shouldPinSend pins a following reader at the real-content bottom, ignoring dynamic spacer', () => {
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
    mode: { kind: 'FOLLOW_BOTTOM' },
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
    mode: { kind: 'FOLLOW_BOTTOM' },
    isFirstUserMsg: false,
  }), false)
})

test('shouldPinSend trusts bottom geometry even when mode is a stale hold', () => {
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

test('layout writes yield from the first input event until its gesture window closes', () => {
  assert.equal(layoutMayOwnScroll(Number.POSITIVE_INFINITY, 999_999), false,
    'a delayed first scroll keeps reader ownership without a 250ms race')
  assert.equal(layoutMayOwnScroll(1250, 1000), false)
  assert.equal(layoutMayOwnScroll(1250, 1249), false)
  assert.equal(layoutMayOwnScroll(1250, 1250), true)
})

test('deferred layout waits for the first scroll instead of timing Infinity', () => {
  assert.equal(gestureLayoutRetryDelay(Number.POSITIVE_INFINITY, 1000), null)
  assert.equal(gestureLayoutRetryDelay(1250, 1000), 251)
  assert.equal(gestureLayoutRetryDelay(999, 1000), 1)
})

test('only scrolling keys claim reader ownership', () => {
  assert.equal(readerInputMayScroll('keydown', 'a'), false)
  assert.equal(readerInputMayScroll('keydown', 'Enter'), false)
  assert.equal(readerInputMayScroll('keydown', 'PageDown'), true)
  assert.equal(readerInputMayScroll('keydown', 'ArrowUp'), true)
  assert.equal(readerInputMayScroll('keydown', 'Tab'), true)
  assert.equal(readerInputMayScroll('wheel'), true)
  assert.equal(readerInputMayScroll('touchmove'), true)
})

test('disclosure activation is recognized as an anchor-latching reading action', () => {
  const disclosureTarget = {
    closest: selector => selector.includes('button.chat__activity-header') ? {} : null,
  }
  const ordinaryTarget = { closest: () => null }
  const staticStatusTarget = {
    // A static status row has the base visual class but is a div, not a button.
    closest: selector => selector.startsWith('button.') ? null : {},
  }

  assert.equal(readerInputActivatesDisclosure(
    'pointerdown', '', disclosureTarget), true)
  assert.equal(readerInputActivatesDisclosure(
    'pointerdown', '', disclosureTarget, 2), false,
  'opening a context menu must not manufacture reading intent')
  assert.equal(readerInputActivatesDisclosure(
    'touchstart', '', disclosureTarget), true)
  assert.equal(readerInputActivatesDisclosure(
    'keydown', 'Enter', disclosureTarget), true)
  assert.equal(readerInputActivatesDisclosure(
    'keydown', ' ', disclosureTarget), true)
  assert.equal(readerInputActivatesDisclosure(
    'keydown', 'a', disclosureTarget), false)
  assert.equal(readerInputActivatesDisclosure(
    'wheel', '', disclosureTarget), false)
  assert.equal(readerInputActivatesDisclosure(
    'pointerdown', '', ordinaryTarget), false)
  assert.equal(readerInputActivatesDisclosure(
    'pointerdown', '', staticStatusTarget), false,
  'a non-interactive status row must not stop live follow')
})

test('disclosure toggles follow only in FOLLOW_BOTTOM and otherwise hold the reader anchor', () => {
  const row = {
    dataset: { key: 'assistant-1' },
    offsetTop: 420,
    offsetHeight: 300,
  }
  const scrollEl = {
    scrollTop: 500,
    clientHeight: 600,
    querySelectorAll: () => [row],
  }
  const follow = { kind: 'FOLLOW_BOTTOM' }
  assert.equal(modeForDisclosureToggle(scrollEl, follow), follow,
    'autoscroll remains the sole authority while following the tail')
  assert.deepEqual(
    modeForDisclosureToggle(scrollEl, { kind: 'PIN_USER_MSG', cid: 'c1' }),
    { kind: 'ANCHOR_AT', key: 'assistant-1', offset: -80 },
    'outside autoscroll the visible reading position is frozen before resize',
  )
})

test('only provably clamped wheel input gets a next-frame no-scroll release', () => {
  const middle = {
    scrollTop: 500,
    scrollHeight: 2000,
    clientHeight: 800,
  }
  assert.equal(readerInputNeedsFrameRelease('wheel', {
    ...middle,
    deltaY: 300,
  }), false, 'a downward wheel waits for its actual compositor scroll')
  assert.equal(readerInputNeedsFrameRelease('wheel', {
    ...middle,
    deltaY: -300,
  }), false, 'an upward wheel waits for its actual compositor scroll')
  assert.equal(readerInputNeedsFrameRelease('wheel', {
    ...middle,
    scrollTop: 1200,
    deltaY: 300,
  }), true, 'a downward wheel already at the bottom is a no-op')
  assert.equal(readerInputNeedsFrameRelease('wheel', {
    ...middle,
    scrollTop: 0,
    deltaY: -300,
  }), true, 'an upward wheel already at the top is a no-op')
  assert.equal(readerInputNeedsFrameRelease('wheel', {
    ...middle,
    scrollTop: 1199,
    deltaY: 300,
  }), false, 'a wheel one pixel from the bottom can still move')
  assert.equal(readerInputNeedsFrameRelease('wheel', {
    ...middle,
    scrollTop: 1,
    deltaY: -300,
  }), false, 'a wheel one pixel from the top can still move')
  assert.equal(readerInputNeedsFrameRelease('wheel', {
    ...middle,
    deltaY: 0,
  }), true, 'a horizontal-only wheel cannot move this vertical controller')
  assert.equal(readerInputNeedsFrameRelease('keydown'), true)
  assert.equal(readerInputNeedsFrameRelease('pointerdown'), false)
  assert.equal(readerInputNeedsFrameRelease('touchmove'), false)
})

test('scroll diagnostics expose behavior without message identity', () => {
  assert.deepEqual(_scrollModeForDiagnostics({
    kind: 'PIN_USER_MSG',
    cid: 'private-message-cid',
    followWhenFilled: true,
  }), {
    kind: 'PIN_USER_MSG',
    armed: true,
  })
  assert.deepEqual(_scrollModeForDiagnostics({
    kind: 'ANCHOR_AT',
    key: 'private-message-key',
    offset: 42,
  }), {
    kind: 'ANCHOR_AT',
  })
})

test('queued submission freezes the visible row before footer reflow', () => {
  const item = {
    offsetTop: 720,
    offsetHeight: 120,
    dataset: { key: 'assistant-live' },
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
    modeForQueuedSubmission(scrollEl, { kind: 'FOLLOW_BOTTOM' }),
    { kind: 'ANCHOR_AT', key: 'assistant-live', offset: 60 },
  )
})

test('question submission freezes the visible row before same-turn output resumes', () => {
  const item = {
    offsetTop: 720,
    offsetHeight: 420,
    dataset: { key: 'assistant-with-question' },
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
    modeForQuestionSubmission(scrollEl, { kind: 'FOLLOW_BOTTOM' }),
    { kind: 'ANCHOR_AT', key: 'assistant-with-question', offset: 60 },
  )
})

test('question submission keeps the current mode when there is no visible row', () => {
  const current = { kind: 'FOLLOW_BOTTOM' }
  const scrollEl = { querySelectorAll() { return [] } }
  assert.equal(modeForQuestionSubmission(scrollEl, current), current)
})

test('queued submission anchors before the active assistant shell that steer will split', () => {
  const user = {
    offsetTop: 8,
    offsetHeight: 50,
    dataset: { key: 'user-stable' },
    hasAttribute() { return false },
  }
  const activeAssistant = {
    offsetTop: 82,
    offsetHeight: 1600,
    dataset: { key: 'streaming-chat' },
    hasAttribute(name) { return name === 'data-active-assistant' },
  }
  const rows = [user, activeAssistant]
  const scrollEl = {
    scrollHeight: 1800,
    scrollTop: 800,
    clientHeight: 600,
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? rows : []
    },
  }

  assert.deepEqual(
    modeForQueuedSubmission(scrollEl, { kind: 'FOLLOW_BOTTOM' }),
    { kind: 'ANCHOR_AT', key: 'user-stable', offset: -792 },
  )
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

test('physical-bottom geometry uses only a rounding epsilon', () => {
  assert.equal(isNearScrollBottom(makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1497,
    clientHeight: 500,
  }), 4), true)
  assert.equal(isNearScrollBottom(makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1495,
    clientHeight: 500,
  }), 4), false)
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

// A scroll element whose ANCHOR_AT target row is resolvable by data-key.
function anchorScrollEl({ scrollHeight, scrollTop, clientHeight, offsetTop }) {
  return {
    scrollHeight,
    scrollTop,
    clientHeight,
    querySelector(selector) {
      return selector === '[data-key="k-1"]' ? { offsetTop } : null
    },
  }
}

test('anchor reapply fires when the anchor row shifted since the last apply', () => {
  // The anchor's offsetTop moved (content grew above it) — the reader would
  // otherwise drift off the message they were reading. This is the same
  // offsetTop-shift case PIN repairs.
  assert.equal(
    _anchorReapplyNeeded(
      anchorScrollEl({ scrollHeight: 2000, scrollTop: 300, clientHeight: 600, offsetTop: 360 }),
      { kind: 'ANCHOR_AT', key: 'k-1', offset: 60 },
      100, // last anchor top differs from the current 360
    ),
    true,
  )
})

test('anchor reapply fires when scrollTop was clamped short but the target is now reachable', () => {
  // target = 1000 - 40 = 960; maxScrollTop = 2000 - 700 = 1300 ≥ 960 reachable;
  // scrollTop 500 < 960 → clamped short.
  assert.equal(
    _anchorReapplyNeeded(
      { scrollHeight: 2000, scrollTop: 500, clientHeight: 700,
        querySelector: (s) => s === '[data-key="k-1"]' ? { offsetTop: 1000 } : null },
      { kind: 'ANCHOR_AT', key: 'k-1', offset: 40 },
      1000, // unchanged offsetTop, so only the clamp drives it
    ),
    true,
  )
})

test('anchor reapply waits until the target is reachable to avoid stepwise jitter', () => {
  // maxScrollTop = 1500 - 700 = 800 < target 960 → NOT reachable → no re-apply
  // (mirrors the pin: never re-clamp toward a still-growing layout).
  assert.equal(
    _anchorReapplyNeeded(
      { scrollHeight: 1500, scrollTop: 500, clientHeight: 700,
        querySelector: (s) => s === '[data-key="k-1"]' ? { offsetTop: 1000 } : null },
      { kind: 'ANCHOR_AT', key: 'k-1', offset: 40 },
      1000,
    ),
    false,
  )
})

test('anchor reapply is idle when the anchor is settled at its target', () => {
  // target = 1000 - 40 = 960; scrollTop = 960; offsetTop unchanged → no-op.
  assert.equal(
    _anchorReapplyNeeded(
      { scrollHeight: 2000, scrollTop: 960, clientHeight: 700,
        querySelector: (s) => s === '[data-key="k-1"]' ? { offsetTop: 1000 } : null },
      { kind: 'ANCHOR_AT', key: 'k-1', offset: 40 },
      1000,
    ),
    false,
  )
})

test('anchor reapply is inert for non-anchor modes and unresolved keys', () => {
  const el = anchorScrollEl({ scrollHeight: 2000, scrollTop: 300, clientHeight: 600, offsetTop: 360 })
  assert.equal(_anchorReapplyNeeded(el, { kind: 'FOLLOW_BOTTOM' }, 100), false)
  assert.equal(_anchorReapplyNeeded(null, { kind: 'ANCHOR_AT', key: 'k-1', offset: 0 }, 100), false)
  assert.equal(
    _anchorReapplyNeeded(
      { scrollHeight: 2000, scrollTop: 0, clientHeight: 600, querySelector: () => null },
      { kind: 'ANCHOR_AT', key: 'missing', offset: 0 }, 100,
    ),
    false, 'an unresolved anchor row never demands a re-apply',
  )
})

test('viewport resize never turns a pin into auto-scroll without a gesture', () => {
  const stalePin = { kind: 'PIN_USER_MSG', cid: 'c-123' }
  assert.equal(
    modeForViewportChange(stalePin, true),
    stalePin,
  )
})

test('an armed live pin holds until its exact spacer is filled, then follows', () => {
  const livePin = {
    kind: 'PIN_USER_MSG', cid: 'c-123', followWhenFilled: true,
  }
  assert.equal(modeAfterSpacerResize(livePin, 320), livePin)
  assert.equal(modeAfterSpacerResize(livePin, 2), livePin)
  assert.deepEqual(modeAfterSpacerResize(livePin, 1), { kind: 'FOLLOW_BOTTOM' })
  assert.deepEqual(modeAfterSpacerResize(livePin, 0), { kind: 'FOLLOW_BOTTOM' })
})

test('reader reaching the reserved physical bottom keeps the live pin armed', () => {
  const livePin = {
    kind: 'PIN_USER_MSG', cid: 'c-123', followWhenFilled: true,
  }
  assert.equal(modeAfterReaderReachesBottom({
    mode: livePin,
    spacerH: 320,
    turnRunning: true,
    lastUserCid: 'c-123',
  }), livePin, 'the existing pin identity stays intact')
})

test('reader reaching reserved bottom repairs a lost live pin instead of following immediately', () => {
  assert.deepEqual(modeAfterReaderReachesBottom({
    mode: { kind: 'FOLLOW_BOTTOM' },
    spacerH: 320,
    turnRunning: true,
    lastUserCid: 'c-123',
  }), {
    kind: 'PIN_USER_MSG', cid: 'c-123', followWhenFilled: true,
  })
})

test('reader reaches ordinary bottom only after reservation is exhausted', () => {
  assert.deepEqual(modeAfterReaderReachesBottom({
    mode: { kind: 'PIN_USER_MSG', cid: 'c-123', followWhenFilled: true },
    spacerH: 0,
    turnRunning: true,
    lastUserCid: 'c-123',
  }), { kind: 'FOLLOW_BOTTOM' })
})

test('idle reserved bottom is a settled pin and cannot manufacture follow', () => {
  assert.deepEqual(modeAfterReaderReachesBottom({
    mode: { kind: 'ANCHOR_AT', key: 'user-c-123', offset: 4 },
    spacerH: 320,
    turnRunning: false,
    lastUserCid: 'c-123',
  }), { kind: 'PIN_USER_MSG', cid: 'c-123' })
})

test('a bottom edge created by anchor reservation keeps the reader anchor', () => {
  const anchor = { kind: 'ANCHOR_AT', key: 'assistant-question', offset: 60 }
  assert.equal(modeAfterReaderReachesBottom({
    mode: anchor,
    spacerH: 180,
    anchorReservation: true,
    turnRunning: true,
    lastUserCid: 'c-123',
  }), anchor)
})

test('a short settled pin retires automatic follow but keeps its identity', () => {
  const livePin = {
    kind: 'PIN_USER_MSG', cid: 'c-123', followWhenFilled: true,
  }
  const settled = settledPinMode(livePin)
  assert.deepEqual(settled, { kind: 'PIN_USER_MSG', cid: 'c-123' })
  assert.equal(modeAfterSpacerResize(settled, 0), settled,
    'later layout changes cannot manufacture follow after stream settle')
})

test('terminal pin waits for stable committed geometry before disarming', () => {
  const livePin = {
    kind: 'PIN_USER_MSG', cid: 'c-123', followWhenFilled: true,
  }
  assert.equal(modeAfterTerminalLayout(livePin, 320, false), livePin)
  assert.deepEqual(
    modeAfterTerminalLayout(livePin, 320, true),
    { kind: 'PIN_USER_MSG', cid: 'c-123' },
  )
})

test('terminal pin follows immediately when final committed geometry fills the spacer', () => {
  const livePin = {
    kind: 'PIN_USER_MSG', cid: 'c-123', followWhenFilled: true,
  }
  assert.deepEqual(
    modeAfterTerminalLayout(livePin, 0, false),
    { kind: 'FOLLOW_BOTTOM' },
  )
})

test('keyboard close preserves pin identity even when keyboard-open geometry is away from the physical bottom', () => {
  const pin = { kind: 'PIN_USER_MSG', cid: 'c-123' }
  const temporaryAnchor = { kind: 'ANCHOR_AT', key: 'user-1', offset: 4 }

  assert.equal(
    modeForViewportChange(pin, false, temporaryAnchor),
    pin,
    'only a real reader scroll may retire PIN_USER_MSG',
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

test('no saved chat location opens at the latest real content without enabling follow', () => {
  const last = {
    offsetTop: 1500,
    offsetHeight: 220,
    dataset: { key: 'assistant-latest' },
  }
  const scrollEl = {
    scrollHeight: 2100,
    scrollTop: 0,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 200 }
      if (selector === '[data-key="assistant-latest"]') return last
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }

  const mode = _validateSavedMode(null, [], scrollEl)
  assert.deepEqual(mode, {
    kind: 'ANCHOR_AT',
    key: 'assistant-latest',
    offset: 300,
    defaultTail: true,
  })
  applyMode(scrollEl, mode)
  assert.equal(scrollEl.scrollTop, 1200,
    'the real content tail is visible and reserved spacer room is excluded')
})

test('attention nudge anchors the real-content tail without enabling follow', () => {
  const last = {
    offsetTop: 1500,
    offsetHeight: 220,
    dataset: { key: 'assistant-paused-tail' },
  }
  const scrollEl = {
    scrollHeight: 2100,
    scrollTop: 700,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 200 }
      if (selector === '[data-key="assistant-paused-tail"]') return last
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }

  const mode = bottomAnchorModeFromScroll(scrollEl)
  assert.deepEqual(mode, {
    kind: 'ANCHOR_AT',
    key: 'assistant-paused-tail',
    offset: 300,
    defaultTail: true,
  })
  applyMode(scrollEl, mode)
  assert.equal(scrollEl.scrollTop, 1200,
    'the nudge excludes blank reservation while retaining composer-cleared content')
  assert.notEqual(mode.kind, 'FOLLOW_BOTTOM',
    'revealing a question or Resume control must not create live-follow intent')
})

test('an unresolvable saved location falls back to a settled bottom anchor', () => {
  const last = {
    offsetTop: 900,
    offsetHeight: 180,
    dataset: { key: 'assistant-current-tail' },
  }
  const scrollEl = {
    scrollHeight: 1400,
    scrollTop: 0,
    clientHeight: 600,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 0 }
      if (selector === '[data-key="missing-old-row"]') return null
      if (selector === '[data-key="assistant-current-tail"]') return last
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }

  const mode = _validateSavedMode(
    { kind: 'ANCHOR_AT', key: 'missing-old-row', offset: 12 },
    [],
    scrollEl,
  )
  assert.equal(mode.kind, 'ANCHOR_AT')
  assert.equal(mode.key, 'assistant-current-tail')
  assert.equal(mode.defaultTail, true,
    'automatic fallback must not masquerade as a reader-chosen location')
  assert.notEqual(mode.kind, 'FOLLOW_BOTTOM')
})

test('a saved anchor wholly inside reserved blank space self-heals to real content', () => {
  const last = {
    offsetTop: 500,
    offsetHeight: 220,
    dataset: { key: 'assistant-question' },
  }
  const scrollEl = {
    scrollHeight: 1900,
    scrollTop: 0,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 1200 }
      if (selector === '[data-key="assistant-question"]') return last
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }

  const restored = _validateSavedMode(
    { kind: 'ANCHOR_AT', key: 'assistant-question', offset: -900 },
    [],
    scrollEl,
  )
  assert.deepEqual(restored, {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 500,
    defaultTail: true,
  })
})

test('live persistence preserves follow while restore settles it to real content', () => {
  const last = {
    offsetTop: 500,
    offsetHeight: 220,
    dataset: { key: 'assistant-tail' },
  }
  const scrollEl = {
    scrollHeight: 1900,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 1200 }
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }
  const follow = { kind: 'FOLLOW_BOTTOM' }

  assert.equal(_modeForPersistence(follow, [], scrollEl), follow,
    'ordinary live persistence must not erase the active follow state')
  assert.deepEqual(_validateSavedMode(follow, [], scrollEl), {
    kind: 'ANCHOR_AT',
    key: 'assistant-tail',
    offset: 500,
    defaultTail: true,
  }, 'mount restore still converts follow into a settled content hold')
})

test('a saved partially-visible anchor remains exact', () => {
  const row = {
    offsetTop: 500,
    offsetHeight: 220,
    dataset: { key: 'assistant-reading' },
  }
  const saved = {
    kind: 'ANCHOR_AT', key: 'assistant-reading', offset: -100,
  }
  const scrollEl = {
    clientHeight: 700,
    querySelector(selector) {
      return selector === '[data-key="assistant-reading"]' ? row : null
    },
  }
  assert.equal(_validateSavedMode(saved, [], scrollEl), saved,
    'an anchor whose row still intersects its restored viewport is preserved')
})

test('the anchor invariant distinguishes content from layout reservation', () => {
  const row = { offsetHeight: 220 }
  assert.equal(_anchorModeIntersectsContent(
    row, { offset: -100 }, 700,
  ), true, 'a partially visible row is a readable location')
  assert.equal(_anchorModeIntersectsContent(
    row, { offset: -900 }, 700,
  ), false, 'a row wholly above the viewport is blank reservation')
  assert.equal(_anchorModeIntersectsContent(
    row, { offset: 700 }, 700,
  ), false, 'a row beginning below the viewport is not visible content')
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

test('chat exit never infers follow mode when no message anchor exists', () => {
  const scrollEl = {
    scrollHeight: 1800,
    scrollTop: 1000,
    clientHeight: 800,
    querySelectorAll() { return [] },
  }

  assert.equal(modeForChatExit(scrollEl), null)
})

test('chat exit from blank reservation persists the real-content tail', () => {
  const last = {
    offsetTop: 500,
    offsetHeight: 220,
    dataset: { key: 'assistant-question' },
  }
  const scrollEl = {
    scrollHeight: 1900,
    scrollTop: 1200,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 1200 }
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }

  assert.deepEqual(modeForChatExit(scrollEl), {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 500,
    defaultTail: true,
  })
})

test('leaving the physical bottom inside blank reservation retires follow', () => {
  const last = {
    offsetTop: 500,
    offsetHeight: 220,
    dataset: { key: 'assistant-question' },
  }
  const scrollEl = {
    scrollHeight: 1900,
    scrollTop: 1200,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 1200 }
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }

  assert.deepEqual(contentHoldModeFromScroll(scrollEl), {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 500,
    defaultTail: true,
  })
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

test('viewport growth keeps a question-answer anchor reachable before output resumes', () => {
  const anchor = { offsetTop: 1320, offsetHeight: 220 }
  const scrollEl = {
    clientHeight: 960,
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? anchor : null
    },
  }
  const listEl = { offsetHeight: 1500 }
  const lastUserMsgEl = { offsetTop: 900 }
  const mode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 60,
  }

  const spacerH = _computeSpacerH(
    scrollEl, listEl, lastUserMsgEl, 960, mode,
  )
  const target = anchor.offsetTop - mode.offset
  const maxScrollTop = listEl.offsetHeight + spacerH - scrollEl.clientHeight

  assert.equal(maxScrollTop, target,
    'the frozen card position is reachable in the first grown-viewport frame')
})

test('anchor reservation disappears once real content makes the target reachable', () => {
  const anchor = { offsetTop: 1320, offsetHeight: 220 }
  const scrollEl = {
    clientHeight: 960,
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? anchor : null
    },
  }
  const mode = { kind: 'ANCHOR_AT', key: 'assistant-question', offset: 60 }

  assert.equal(
    _computeSpacerH(scrollEl, { offsetHeight: 2300 }, { offsetTop: 900 }, 960, mode),
    0,
  )
})

test('a live off-content anchor reserves its exact reader-owned position', () => {
  const anchor = { offsetTop: 500, offsetHeight: 220 }
  const scrollEl = {
    clientHeight: 700,
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? anchor : null
    },
  }
  const mode = {
    kind: 'ANCHOR_AT', key: 'assistant-question', offset: -900,
  }
  assert.equal(
    _computeSpacerH(scrollEl, { offsetHeight: 700 }, { offsetTop: 100 }, 700, mode),
    1400,
    'live reader ownership survives in reserved room; persistence rejects it',
  )
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
// F2 — spacer reservation survives remount. FOLLOW_BOTTOM must ignore that
// reservable room rather than deleting it to avoid an empty restored viewport.
// ---------------------------------------------------------------------------

test('F2: an idle-mounted chat still reserves exactly enough room for its last user row to reach the top', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 915 })
  const listEl = { offsetHeight: 260 }        // 2-message short chat, fits the viewport
  const lastUserMsgEl = { offsetTop: 200 }
  const spacerH = _computeSpacerH(scrollEl, listEl, lastUserMsgEl, 915)
  const maxScrollTop = listEl.offsetHeight + spacerH - scrollEl.clientHeight
  assert.equal(maxScrollTop, lastUserMsgEl.offsetTop - PIN_OFFSET,
    'remount keeps exactly enough room to lift the last user row, with no excess')
})

test('F2: FOLLOW_BOTTOM ignores permanent spacer room so a short restored chat stays on-screen', () => {
  const userTop = 8
  const shortList = 260
  const clientHeight = 915
  const lowUserTop = 200
  const spacerH = _computeSpacerH(
    { clientHeight }, { offsetHeight: shortList }, { offsetTop: lowUserTop }, clientHeight,
  )
  const restored = makePinnableScrollEl({ listH: shortList, spacerH, clientHeight, userTop: lowUserTop, cid: 'c-1' })
  applyMode(restored, { kind: 'FOLLOW_BOTTOM' })
  assert.equal(restored.scrollTop, 0,
    'short real content does not scroll merely because reservable room exists')
  assert.ok(userTop - restored.scrollTop >= 0, 'the restored conversation is on-screen')
})


// ---------------------------------------------------------------------------
// F4 — returning to the foreground freezes the reader where they were
// (anchor), even at the tail. Return never creates or restores FOLLOW_BOTTOM.
// ---------------------------------------------------------------------------

test('F4: foreground return freezes as an anchor even at the tail', () => {
  const tailItem = { offsetTop: 1200, offsetHeight: 200, dataset: { key: 'a-9' } }
  const scrollEl = {
    scrollHeight: 1400, scrollTop: 685, clientHeight: 700,   // near the tail
    querySelectorAll(sel) { return sel === '.chat__msg[data-key]' ? [tailItem] : [] },
  }
  assert.equal(isNearScrollBottom(scrollEl), true, 'precondition: at the tail')

  const restored = modeForForegroundReturn(scrollEl)
  assert.equal(restored.kind, 'ANCHOR_AT',
    'return freezes as an anchor, not the grown tail')
  assert.equal(restored.key, 'a-9')
})
