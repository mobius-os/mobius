import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  _anchorModeIntersectsContent,
  _anchorReapplyNeeded,
  _computeSpacerH,
  _pinReapplyNeeded,
  applyMode,
  anchorModeFromScroll,
  bottomAnchorModeFromScroll,
  contentHoldModeFromScroll,
  isQuestionSubmissionMode,
  nextPinViewportHeight,
  physicalBottomAnchorModeFromScroll,
  topRevealAnchorMode,
} from '../scroll/geometry.js'
import {
  _modeForPersistence,
  _validateSavedMode,
} from '../scroll/restore.js'
import {
  composerTailIntentRequestsFollow,
  delayedSendWillPin,
  gestureLayoutRetryDelay,
  isNearPhysicalBottom,
  layoutMayOwnScroll,
  modeForChatExit,
  modeForDisclosureToggle,
  modeForForegroundReturn,
  modeForQuestionSubmission,
  modeForQueuedSubmission,
  modeAfterQuestionResponseStart,
  modeAfterReaderGesture,
  modeAfterSpacerResize,
  modeAfterTerminalLayout,
  nestedReaderTargetOwnsInput,
  readerInputClaimsPhysicalTail,
  readerInputEscapeDirection,
  readerInputActivatesDisclosure,
  readerInputMayScroll,
  readerInputNeedsFrameRelease,
  readerScrollEscapeDirection,
  settledPinMode,
  shouldPinSend,
} from '../scroll/policy.js'
import { _scrollModeForDiagnostics } from '../useScrollMode.js'
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

test('shouldPinSend uses FOLLOW_BOTTOM only when no scroll element is available', () => {
  assert.equal(shouldPinSend({
    scrollEl: null,
    mode: { kind: 'FOLLOW_BOTTOM' },
    isFirstUserMsg: false,
  }), true)
})

test('shouldPinSend does not mistake reserved reply room for autoscroll after an upward escape', () => {
  // Raw physical gap is 440px. The old rule subtracted the 400px spacer,
  // misclassified this as a 40px bottom gap, and yanked the next message to the
  // top even though the reader had moved the latest user row into mid-screen.
  const scrollEl = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1000,
    clientHeight: 560,
    spacerHeight: 400,
  })
  assert.equal(shouldPinSend({
    scrollEl,
    mode: { kind: 'ANCHOR_AT', key: 'reader-row', offset: -280 },
    isFirstUserMsg: false,
  }), false)
})

test('shouldPinSend still refuses to pin when the physical bottom gap is large', () => {
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

test('shouldPinSend trusts the exact physical bottom even when mode is a stale hold', () => {
  const scrollEl = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1440,
    clientHeight: 560,
    spacerHeight: 400,
  })
  assert.equal(shouldPinSend({
    scrollEl,
    mode: { kind: 'PIN_USER_MSG', cid: 'c-123' },
    isFirstUserMsg: false,
  }), true)
})

test('delayed visibility preserves queue-time pin intent until the reader moves', () => {
  const bottomAtQueueTime = {
    willPin: true,
    readerIntentVersion: 7,
  }
  assert.equal(delayedSendWillPin({
    previousIntent: bottomAtQueueTime,
    readerIntentVersion: 7,
    // Opening the tray changed the viewport and made a later geometry snapshot
    // look away from the tail, but the reader did not move.
    willPinNow: false,
  }), true)

  assert.equal(delayedSendWillPin({
    previousIntent: { willPin: false, readerIntentVersion: 7 },
    readerIntentVersion: 7,
    // Tray collapse can also make old content appear near the tail; layout
    // alone must not manufacture a pin for somebody reading above it.
    willPinNow: true,
  }), false)

  assert.equal(delayedSendWillPin({
    previousIntent: bottomAtQueueTime,
    // A real scroll advanced the generation, so Fast-forward-time geometry is
    // now the newer intent and must win over the queued snapshot.
    readerIntentVersion: 8,
    willPinNow: false,
  }), false)
})

test('layout writes yield from first input through gesture settlement', () => {
  assert.equal(layoutMayOwnScroll(Number.POSITIVE_INFINITY, 999_999), false,
    'a delayed first scroll or active momentum keeps reader ownership')
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

test('nested controls keep their own keys and available scroll range', () => {
  const editor = {
    closest: selector => selector.startsWith('textarea,') ? editor : null,
  }
  const button = {
    closest: selector => selector.startsWith('button,') ? button : null,
  }
  assert.equal(nestedReaderTargetOwnsInput({
    type: 'keydown', key: 'End', target: editor,
  }), true, 'caret navigation in a textarea is not transcript intent')
  assert.equal(nestedReaderTargetOwnsInput({
    type: 'keydown', key: ' ', target: button,
  }), true, 'Space activates the focused control instead of scrolling the chat')
  assert.equal(nestedReaderTargetOwnsInput({
    type: 'keydown', key: 'End', target: button,
  }), false, 'an unconsumed scroll key may still belong to the transcript')

  const scrollEl = { contains: node => node === nested }
  const nested = {
    scrollTop: 20,
    scrollHeight: 200,
    clientHeight: 80,
    closest: selector => selector === '[data-chat-scroll-region], .chat__scroll'
      ? nested
      : null,
  }
  assert.equal(nestedReaderTargetOwnsInput({
    type: 'wheel', target: nested, scrollEl, direction: 'down',
  }), true)
  nested.scrollTop = 120
  assert.equal(nestedReaderTargetOwnsInput({
    type: 'wheel', target: nested, scrollEl, direction: 'down',
  }), false, 'a clamped nested surface may chain the gesture to the transcript')
  assert.equal(nestedReaderTargetOwnsInput({
    type: 'wheel', target: nested, scrollEl, direction: 'up',
  }), true)
})

test('a composer press or direct edit requests follow only at the physical tail', () => {
  const composer = { matches: selector => selector === 'textarea.chat__input' }
  const otherControl = { matches: () => false }
  const atTail = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1200,
    clientHeight: 800,
  })
  const aboveTail = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1000,
    clientHeight: 800,
  })

  assert.equal(composerTailIntentRequestsFollow({
    type: 'pointerdown', button: 0, target: composer,
  }, atTail), true)
  assert.equal(composerTailIntentRequestsFollow({
    type: 'input', target: composer,
  }, atTail), true, 'paste/typing can express tail intent without a new pointer event')
  assert.equal(composerTailIntentRequestsFollow({
    type: 'input', target: composer,
  }, aboveTail), false,
    'composer focus must preserve an older reading position')
  assert.equal(composerTailIntentRequestsFollow({
    type: 'pointerdown', button: 1, target: composer,
  }, atTail), false,
    'non-primary presses do not express writing intent')
  assert.equal(composerTailIntentRequestsFollow({
    type: 'input', target: otherControl,
  }, atTail), false)
  assert.equal(composerTailIntentRequestsFollow({
    type: 'change', target: composer,
  }, atTail), false, 'unrelated form events do not claim scroll ownership')
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

test('only provably clamped wheel and keyboard input gets a next-frame release', () => {
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
  assert.equal(readerInputNeedsFrameRelease('keydown', middle, 'PageUp'), false,
    'PageUp waits for the browser scroll before releasing reader ownership')
  assert.equal(readerInputNeedsFrameRelease('keydown', {
    ...middle,
    scrollTop: 0,
  }, 'PageUp'), true, 'PageUp at the top is already clamped')
  assert.equal(readerInputNeedsFrameRelease('keydown', middle, 'PageDown'), false)
  assert.equal(readerInputNeedsFrameRelease('keydown', {
    ...middle,
    scrollTop: 1200,
  }, 'PageDown'), true, 'PageDown at the bottom is already clamped')
  assert.equal(readerInputNeedsFrameRelease('keydown', middle, ' ', true), false,
    'Shift+Space owns upward movement')
  assert.equal(readerInputNeedsFrameRelease('keydown', {
    ...middle,
    scrollTop: 0,
  }, ' ', true), true, 'Shift+Space fast-releases at the top')
  assert.equal(readerInputNeedsFrameRelease('keydown', middle, 'Tab'), true,
    'focus traversal has no stable scroll direction')
  assert.equal(readerInputNeedsFrameRelease('pointerdown'), false)
  assert.equal(readerInputNeedsFrameRelease('touchmove'), false)
})

test('the no-scroll release classifier never reads touch geometry', () => {
  // The geometry thunk performs layout-forcing DOM reads (scrollHeight on an
  // unvirtualized transcript). Only the wheel branch consumes them, so any
  // input type that short-circuits before that branch must never invoke it.
  // Counting invocations - rather than asserting return values - is what pins
  // the COST rather than the behaviour: the previous eagerly-built argument
  // object returned identical answers while reading the scroller every time.
  let geometryReads = 0
  const readGeometry = () => {
    geometryReads += 1
    return { deltaY: 0, scrollTop: 500, scrollHeight: 2000, clientHeight: 800 }
  }

  readerInputNeedsFrameRelease('touchstart', readGeometry)
  readerInputNeedsFrameRelease('touchmove', readGeometry)
  readerInputNeedsFrameRelease('pointerdown', readGeometry)
  assert.equal(geometryReads, 0, 'touch and pointer input must not measure the scroller')

  // Scroll keys are rare and need the same exact-edge proof as wheel input.
  readerInputNeedsFrameRelease('keydown', readGeometry, 'PageUp')
  assert.equal(geometryReads, 1, 'keydown reads the scroller once')
  readerInputNeedsFrameRelease('keydown', readGeometry, 'Tab')
  assert.equal(geometryReads, 1, 'Tab fast-releases without measuring')

  // Wheel genuinely needs the values, so it must still read them - exactly once.
  readerInputNeedsFrameRelease('wheel', readGeometry)
  assert.equal(geometryReads, 2, 'wheel reads the scroller once')
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
    {
      kind: 'ANCHOR_AT',
      key: 'assistant-with-question',
      offset: 60,
      questionSubmitBaseMode: { kind: 'FOLLOW_BOTTOM' },
    },
  )
})

test('question submission treats a settled pin at the physical tail as follow intent', () => {
  // A short intro + question card ends the stream before its reservation fills,
  // so the live mode settles to a PIN_USER_MSG with its automatic follow-arm
  // retired while the reader is still parked at the bottom. Answering + resuming
  // must re-follow the new output, so the captured base normalizes to
  // FOLLOW_BOTTOM even though the live mode was a settled pin.
  const item = {
    offsetTop: 660,
    offsetHeight: 420,
    dataset: { key: 'assistant-with-question' },
  }
  const scrollEl = {
    scrollHeight: 1200,
    scrollTop: 600,
    clientHeight: 600, // gap = 0 → at the physical tail
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [item] : []
    },
  }
  const captured = modeForQuestionSubmission(
    scrollEl, { kind: 'PIN_USER_MSG', cid: 'latest' },
  )
  assert.equal(captured.kind, 'ANCHOR_AT')
  assert.deepEqual(captured.questionSubmitBaseMode, { kind: 'FOLLOW_BOTTOM' })
})

test('question submission keeps a reading hold when parked up-thread', () => {
  // Same settled-pin shape, but the reader has scrolled up: this is a genuine
  // reading hold, so the base must NOT be promoted to follow.
  const item = {
    offsetTop: 360,
    offsetHeight: 420,
    dataset: { key: 'assistant-with-question' },
  }
  const scrollEl = {
    scrollHeight: 1800,
    scrollTop: 300,
    clientHeight: 600, // gap = 900 → not at the tail
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [item] : []
    },
  }
  const readingHold = { kind: 'PIN_USER_MSG', cid: 'latest' }
  const captured = modeForQuestionSubmission(scrollEl, readingHold)
  assert.deepEqual(captured.questionSubmitBaseMode, readingHold)
})

test('first post-answer activity resumes only the follow mode that submitted it', () => {
  const follow = { kind: 'FOLLOW_BOTTOM' }
  const heldMode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-with-question',
    offset: 60,
    questionSubmitBaseMode: follow,
  }
  const submission = { mode: heldMode, readerIntentVersion: 7 }

  assert.equal(modeAfterQuestionResponseStart({
    currentMode: heldMode,
    submission,
    currentReaderIntentVersion: 7,
  }), follow)
  assert.equal(modeAfterQuestionResponseStart({
    currentMode: follow,
    submission,
    currentReaderIntentVersion: 7,
  }), follow, 'a viewport release that already restored follow remains stable')

  const readerHold = { kind: 'ANCHOR_AT', key: 'older-row', offset: 20 }
  assert.equal(modeAfterQuestionResponseStart({
    currentMode: readerHold,
    submission,
    currentReaderIntentVersion: 8,
  }), readerHold, 'a reader scroll during submission cancels follow restoration')
  assert.equal(modeAfterQuestionResponseStart({
    currentMode: readerHold,
    submission,
    currentReaderIntentVersion: 7,
  }), readerHold, 'a newer semantic location is never overwritten')
})

test('post-answer activity keeps a pre-submit reading hold', () => {
  const baseMode = { kind: 'ANCHOR_AT', key: 'older-row', offset: 20 }
  const submittedMode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-with-question',
    offset: 60,
    questionSubmitBaseMode: baseMode,
  }
  assert.equal(modeAfterQuestionResponseStart({
    currentMode: submittedMode,
    submission: { mode: submittedMode, readerIntentVersion: 3 },
    currentReaderIntentVersion: 3,
  }), submittedMode)
})

test('question submission is a viewport-independent transient anchor', () => {
  const baseMode = { kind: 'PIN_USER_MSG', cid: 'latest' }
  const heldMode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-with-question',
    offset: 60,
    questionSubmitBaseMode: baseMode,
  }

  assert.equal(isQuestionSubmissionMode(heldMode), true)
  assert.equal(isQuestionSubmissionMode(baseMode), false)
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

test('isNearPhysicalBottom keeps reserved room in the reader distance', () => {
  const scrollEl = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1000,
    clientHeight: 560,
    spacerHeight: 400,
  })
  assert.equal(isNearPhysicalBottom(scrollEl), false)
  scrollEl.scrollTop = 1440
  assert.equal(isNearPhysicalBottom(scrollEl), true)
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

test('pin repair never pulls backward over an unchanged target', () => {
  const scrollEl = {
    scrollHeight: 2000,
    // Target is 996. ScrollTop beyond it is indistinguishable from a reader
    // moving down while the scroll event waits behind a busy render.
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
    false,
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

test('a promoted assistant still resolves its live transcript-position alias', () => {
  const row = { offsetTop: 1000 }
  const scrollEl = {
    scrollHeight: 2000,
    scrollTop: 500,
    clientHeight: 700,
    querySelector(selector) {
      return selector === '[data-anchor-key="assistant-12"]' ? row : null
    },
  }

  assert.equal(_anchorReapplyNeeded(
    scrollEl,
    { kind: 'ANCHOR_AT', key: 'assistant-12', offset: 40 },
    1000,
  ), true, 'terminal layout can repair the held viewport through the alias')
  applyMode(scrollEl, { kind: 'ANCHOR_AT', key: 'assistant-12', offset: 40 })
  assert.equal(scrollEl.scrollTop, 960)
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

test('an armed live pin holds until its exact spacer is filled, then follows', () => {
  const livePin = {
    kind: 'PIN_USER_MSG', cid: 'c-123', followWhenFilled: true,
  }
  assert.equal(modeAfterSpacerResize(livePin, 320), livePin)
  assert.equal(modeAfterSpacerResize(livePin, 2), livePin)
  assert.deepEqual(modeAfterSpacerResize(livePin, 1), { kind: 'FOLLOW_BOTTOM' })
  assert.deepEqual(modeAfterSpacerResize(livePin, 0), { kind: 'FOLLOW_BOTTOM' })
})

test('reader settlement follows the physical tail even while reservation remains', () => {
  const exactHold = {
    kind: 'ANCHOR_AT', key: 'user-c-123', offset: -320,
  }
  // Not following, but the gesture reached the bottom band → enter follow.
  assert.deepEqual(modeAfterReaderGesture({
    escaped: false,
    reachedNearBottom: true,
    holdMode: exactHold,
  }), { kind: 'FOLLOW_BOTTOM' })
  // Not following and never reached the band → hold exactly where the reader is.
  assert.equal(modeAfterReaderGesture({
    escaped: false,
    reachedNearBottom: false,
    holdMode: exactHold,
  }), exactHold)
  // Prior follow state cannot override the gesture's actual off-bottom result.
  assert.equal(modeAfterReaderGesture({
    escaped: false,
    reachedNearBottom: false,
    holdMode: exactHold,
  }), exactHold)
  // An upward escape wins even if the gesture had touched the tail earlier.
  assert.equal(modeAfterReaderGesture({
    escaped: true,
    reachedNearBottom: true,
    holdMode: exactHold,
  }), exactHold)

  const scrollEl = makeScrollEl({
    scrollHeight: 2000,
    scrollTop: 1000,
    clientHeight: 560,
    spacerHeight: 400,
  })
  applyMode(scrollEl, { kind: 'FOLLOW_BOTTOM' })
  assert.equal(scrollEl.scrollTop, 1440,
    'follow owns the one physical tail instead of jumping back before reservation')
})

// Direction helpers are part of the same bottom-follow contract exercised above.
{
  // Wheel up escapes; wheel down points toward the tail but still needs bottom
  // geometry before it may follow. No vertical delta is neutral.
  assert.equal(readerInputEscapeDirection('wheel', { deltaY: -20 }), 'up')
  assert.equal(readerInputEscapeDirection('wheel', { deltaY: 20 }), 'down')
  assert.equal(readerInputEscapeDirection('wheel', { deltaY: 0 }), null)
  // Scroll keys carry a stable direction.
  assert.equal(readerInputEscapeDirection('keydown', { key: 'ArrowUp' }), 'up')
  assert.equal(readerInputEscapeDirection('keydown', { key: 'PageUp' }), 'up')
  assert.equal(readerInputEscapeDirection('keydown', { key: 'Home' }), 'up')
  assert.equal(readerInputEscapeDirection('keydown', { key: 'ArrowDown' }), 'down')
  assert.equal(readerInputEscapeDirection('keydown', { key: 'End' }), 'down')
  // Space pages down; Shift+Space pages up.
  assert.equal(readerInputEscapeDirection('keydown', { key: ' ' }), 'down')
  assert.equal(
    readerInputEscapeDirection('keydown', { key: ' ', shiftKey: true }), 'up',
  )
  // A non-directional key and pointer input never move the latch.
  assert.equal(readerInputEscapeDirection('keydown', { key: 'Tab' }), null)
  assert.equal(readerInputEscapeDirection('pointerdown', {}), null)
}

test('end-directed input at the physical clamp follows without a scroll event', () => {
  assert.equal(readerInputClaimsPhysicalTail('down', 0), true,
    'a downward input at the clamp is explicit follow intent')
  assert.equal(readerInputClaimsPhysicalTail('down', 3.5), true,
    'subpixel browser rounding at the clamp remains physical-tail intent')
  assert.equal(readerInputClaimsPhysicalTail('down', 4), false,
    'near the tail is not enough when no scroll event proves arrival')
  assert.equal(readerInputClaimsPhysicalTail('up', 0), false,
    'an upward input at the tail must escape rather than follow')
  assert.equal(readerInputClaimsPhysicalTail(null, 0), false,
    'directionless input cannot manufacture follow intent')
})

{
  assert.equal(readerScrollEscapeDirection(1400, 1200), 'up')
  assert.equal(readerScrollEscapeDirection(1200, 1400), 'down')
  assert.equal(readerScrollEscapeDirection(1200, 1200.4), null,
    'subpixel jitter must not flip the escape latch')
  assert.equal(readerScrollEscapeDirection(null, 1200), null,
    'a scroll without a pre-input position carries no invented direction')
}

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

test('attention nudge anchors the physical tail without enabling follow', () => {
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
      if (selector === '[data-key="assistant-paused-tail"]') return last
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }

  const mode = physicalBottomAnchorModeFromScroll(scrollEl)
  assert.deepEqual(mode, {
    kind: 'ANCHOR_AT',
    key: 'assistant-paused-tail',
    offset: 100,
  })
  applyMode(scrollEl, mode)
  assert.equal(scrollEl.scrollTop, 1400,
    'the nudge includes all composer clearance after the attention card')
  assert.notEqual(mode.kind, 'FOLLOW_BOTTOM',
    'revealing a question or Resume control must not create live-follow intent')
})

test('the question nudge reveals a card from its top, not its tail', () => {
  const scrollEl = {
    scrollTop: 300,
    clientHeight: 700,
    clientWidth: 400,
    offsetWidth: 400,
    getBoundingClientRect: () => ({ top: 0 }),
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? row : null
    },
  }
  const row = {
    offsetTop: 500,
    offsetHeight: 900,
    dataset: { key: 'assistant-question' },
    getBoundingClientRect: () => ({ top: 200 }),
  }
  // The card starts 60px below its owning row's top and is taller than the
  // usable viewport, so the physical tail would strand its prompt above.
  const card = {
    getBoundingClientRect: () => ({ top: 260 }),
    closest: (selector) => (selector === '.chat__msg[data-key]' ? row : null),
  }

  const mode = topRevealAnchorMode(scrollEl, card, 16)
  assert.deepEqual(mode, {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: -44,
  }, 'the anchor folds the card-vs-row offset into the owning row key')

  applyMode(scrollEl, mode)
  // The card sits at content position 560; landing scrollTop 544 places its
  // top 16px below the viewport top — the prompt is read from its first line.
  assert.equal(scrollEl.scrollTop, 544,
    'the card top rests just below the viewport top, its tail one scroll away')
})

test('a question card with no owning keyed row falls back to the tail reveal', () => {
  const scrollEl = { getBoundingClientRect: () => ({ top: 0 }) }
  const orphanCard = { closest: () => null }
  assert.equal(topRevealAnchorMode(scrollEl, orphanCard, 16), null,
    'an unresolvable card returns null so the caller can anchor the tail')
  assert.equal(topRevealAnchorMode(scrollEl, null, 16), null,
    'a missing card node returns null rather than throwing')
})

test('an off-content physical nudge stays live but persists the real-content tail', () => {
  const last = {
    offsetTop: 1500,
    offsetHeight: 220,
    dataset: { key: 'assistant-paused-tail' },
  }
  const scrollEl = {
    scrollHeight: 3100,
    scrollTop: 700,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 1200 }
      if (selector === '[data-key="assistant-paused-tail"]') return last
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [last] : []
    },
  }

  const liveMode = physicalBottomAnchorModeFromScroll(scrollEl)
  assert.deepEqual(liveMode, {
    kind: 'ANCHOR_AT',
    key: 'assistant-paused-tail',
    offset: -900,
  })
  applyMode(scrollEl, liveMode)
  assert.equal(scrollEl.scrollTop, 2400,
    'the explicit nudge reaches the true physical tail in the live mount')

  assert.deepEqual(_modeForPersistence(liveMode, [], scrollEl), {
    kind: 'ANCHOR_AT',
    key: 'assistant-paused-tail',
    offset: 300,
    defaultTail: true,
  }, 'durable state normalizes the off-content nudge to the real tail')
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

test('a product continuation marker cannot take ownership of a saved user pin', () => {
  const messages = [
    { role: 'user', cid: 'owner-cid', content: 'do work' },
    {
      role: 'user',
      cid: 'restart-resume-token',
      content: 'continue',
      kind: 'auto_continuation',
    },
  ]
  const ownerRow = {
    offsetTop: 420,
    dataset: { key: 'owner-user-row' },
  }
  const scrollEl = {
    querySelector(selector) {
      return selector === '.chat__msg--user[data-cid="owner-cid"]'
        ? ownerRow
        : null
    },
    querySelectorAll() { return [] },
  }

  assert.deepEqual(
    _validateSavedMode(
      { kind: 'PIN_USER_MSG', cid: 'owner-cid', followWhenFilled: true },
      messages,
      scrollEl,
    ),
    { kind: 'ANCHOR_AT', key: 'owner-user-row', offset: PIN_OFFSET },
    'the preceding owner row restores physically without recreating pin authority',
  )
  assert.deepEqual(
    _validateSavedMode(
      { kind: 'PIN_USER_MSG', cid: 'restart-resume-token' },
      messages,
      scrollEl,
    ),
    { kind: 'INITIAL' },
    'the provider-facing marker is never restored as an owner-authored pin',
  )
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
  const aliasedRow = {
    offsetTop: 500,
    offsetHeight: 220,
    dataset: { key: 'server-row', cid: 'client-row' },
  }
  const aliased = { kind: 'ANCHOR_AT', key: 'client-row', offset: -100 }
  const aliasedScrollEl = {
    clientHeight: 700,
    querySelector(selector) {
      return selector === '[data-cid="client-row"]' ? aliasedRow : null
    },
  }
  assert.equal(_validateSavedMode(aliased, [], aliasedScrollEl), aliased,
    'cached-phase restore resolves the cid before passive canonical remapping')
})

test('question-only viewport overlay is never restored as durable reader state', () => {
  const row = {
    offsetTop: 500,
    offsetHeight: 220,
    dataset: { key: 'assistant-question' },
  }
  const liveMode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 100,
    questionSubmitBaseMode: { kind: 'FOLLOW_BOTTOM' },
  }
  const scrollEl = {
    clientHeight: 700,
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? row : null
    },
  }

  assert.deepEqual(_modeForPersistence(liveMode, [], scrollEl), {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 100,
  })
  assert.deepEqual(_validateSavedMode(liveMode, [], scrollEl), {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 100,
  })
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

test('reader hold preserves exact position inside transient unkeyed content', () => {
  const lastKeyed = {
    offsetTop: 100,
    offsetHeight: 100,
    dataset: { key: 'user-before-transient-content' },
  }
  const scrollEl = {
    scrollHeight: 2000,
    scrollTop: 600,
    clientHeight: 700,
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: 0 }
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [lastKeyed] : []
    },
  }

  assert.deepEqual(contentHoldModeFromScroll(scrollEl), {
    kind: 'ANCHOR_AT',
    key: 'user-before-transient-content',
    offset: -500,
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
    scrollTop: 0,
    querySelector: () => null,
    parentElement: {
      querySelector(selector) {
        if (selector === '.queued') return queuedTray
        return null
      },
    },
  }
}

test('spacer reservation belongs to the latest user row in any mode', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 600 })
  scrollEl.scrollTop = 500
  const listEl = { offsetHeight: 900 }
  const lastUserMsgEl = {
    offsetTop: 700,
    offsetHeight: 80,
    dataset: { cid: 'c-1' },
  }

  assert.equal(
    _computeSpacerH(
      scrollEl,
      listEl,
      lastUserMsgEl,
      { kind: 'PIN_USER_MSG', cid: 'c-1' },
    ),
    396,
  )
  assert.equal(
    _computeSpacerH(
      scrollEl,
      listEl,
      lastUserMsgEl,
      { kind: 'ANCHOR_AT', key: 'a-1', offset: 0 },
    ),
    396,
    'mode does not retire the stable latest-turn room',
  )
  assert.equal(
    _computeSpacerH(
      scrollEl,
      listEl,
      lastUserMsgEl,
      { kind: 'PIN_USER_MSG', cid: 'different-row' },
    ),
    396,
    'stale mode identity cannot change the latest row reservation',
  )
})

test('off-screen latest user pre-reserves one stable downward scroll range', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 600 })
  scrollEl.scrollTop = 0
  const listEl = { offsetHeight: 1400 }
  const latestUserMsgEl = {
    offsetTop: 900,
    offsetHeight: 80,
    dataset: { cid: 'latest' },
  }

  assert.equal(
    _computeSpacerH(
      scrollEl,
      listEl,
      latestUserMsgEl,
      { kind: 'ANCHOR_AT', key: 'older-user', offset: 0 },
    ),
    96,
    'tail room exists before the gesture reaches the latest row',
  )
})

test('crossing the latest-user viewport boundary cannot create a second-stage bottom', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 600 })
  const listEl = { offsetHeight: 1400 }
  const latestUserMsgEl = {
    offsetTop: 900,
    offsetHeight: 80,
    dataset: { cid: 'latest' },
  }
  const mode = { kind: 'ANCHOR_AT', key: 'older-user', offset: 0 }

  scrollEl.scrollTop = 0
  const beforeApproach = _computeSpacerH(
    scrollEl, listEl, latestUserMsgEl, mode,
  )
  scrollEl.scrollTop = 700
  const afterLatestUserAppears = _computeSpacerH(
    scrollEl, listEl, latestUserMsgEl, mode,
  )

  assert.equal(beforeApproach, 96)
  assert.equal(afterLatestUserAppears, beforeApproach,
    'scrolling the latest row into view must not extend scrollHeight')
})

test('an older applied anchor does not make tail scrollHeight grow later', () => {
  const anchor = { offsetTop: 100, offsetHeight: 80 }
  const scrollEl = {
    clientHeight: 600,
    scrollTop: 500,
    querySelector(selector) {
      return selector === '[data-key="older-anchor"]' ? anchor : null
    },
  }
  const latestUserMsgEl = {
    offsetTop: 700,
    offsetHeight: 80,
    dataset: { cid: 'latest' },
  }

  assert.equal(
    _computeSpacerH(
      scrollEl,
      { offsetHeight: 1000 },
      latestUserMsgEl,
      { kind: 'ANCHOR_AT', key: 'older-anchor', offset: 0 },
    ),
    296,
    'the final range is present even while an older anchor is visible',
  )
})

test('applied anchor may reserve before current geometry reaches its visible latest row', () => {
  const anchor = { offsetTop: 600, offsetHeight: 80 }
  const scrollEl = {
    clientHeight: 600,
    scrollTop: 0,
    querySelector(selector) {
      return selector === '[data-key="latest-anchor"]' ? anchor : null
    },
  }
  const latestUserMsgEl = {
    offsetTop: 700,
    offsetHeight: 80,
    dataset: { cid: 'latest' },
  }

  assert.equal(
    _computeSpacerH(
      scrollEl,
      { offsetHeight: 900 },
      latestUserMsgEl,
      { kind: 'ANCHOR_AT', key: 'latest-anchor', offset: 100 },
    ),
    396,
    'mount can establish exact room before applying the saved visible-row anchor',
  )
})

test('keyboard height shrinks blank reservation before moving followed content', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 800 })
  const latestUserMsgEl = {
    offsetTop: 500,
    offsetHeight: 80,
    dataset: { cid: 'latest' },
  }
  const listEl = { offsetHeight: 700 }
  const mode = { kind: 'FOLLOW_BOTTOM' }
  const closedSpacer = _computeSpacerH(
    scrollEl, listEl, latestUserMsgEl, mode,
  )
  scrollEl.clientHeight = 500
  const openSpacer = _computeSpacerH(
    scrollEl, listEl, latestUserMsgEl, mode,
  )

  assert.equal(closedSpacer, 596)
  assert.equal(openSpacer, 296)
  assert.equal(
    listEl.offsetHeight + closedSpacer - 800,
    listEl.offsetHeight + openSpacer - 500,
    'removing 300px of visible height first removes 300px of blank spacer',
  )
})

test('a pin reserves the known keyboard-closed viewport before that resize paints', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 500 })
  const latestUserMsgEl = {
    offsetTop: 500,
    offsetHeight: 80,
    dataset: { cid: 'latest' },
  }
  const listEl = { offsetHeight: 700 }

  assert.equal(_computeSpacerH(
    scrollEl,
    listEl,
    latestUserMsgEl,
    { kind: 'PIN_USER_MSG', cid: 'latest' },
    { pinViewportHeight: 800 },
  ), 596)
  assert.equal(_computeSpacerH(
    scrollEl,
    listEl,
    latestUserMsgEl,
    { kind: 'FOLLOW_BOTTOM' },
    { pinViewportHeight: 800 },
  ), 296, 'follow remains responsive to the keyboard-open box')
})

test('pin viewport ceiling survives same-width keyboard motion and resets on layout changes', () => {
  assert.equal(nextPinViewportHeight({
    previousHeight: 800,
    previousWidth: 412,
    currentHeight: 500,
    currentWidth: 412,
  }), 800)
  assert.equal(nextPinViewportHeight({
    previousHeight: 500,
    previousWidth: 412,
    currentHeight: 800,
    currentWidth: 412,
  }), 800)
  assert.equal(nextPinViewportHeight({
    previousHeight: 800,
    previousWidth: 412,
    currentHeight: 500,
    currentWidth: 700,
  }), 500, 'orientation/width changes establish a new ceiling')
  assert.equal(nextPinViewportHeight({
    previousHeight: 800,
    previousWidth: 700,
    currentHeight: 620,
    currentWidth: 700,
    committedResize: true,
  }), 620, 'a committed pane resize establishes a new ceiling')
})

test('keyboard overflow lifts only content that no longer fits', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 800 })
  const latestUserMsgEl = {
    offsetTop: 500,
    offsetHeight: 80,
    dataset: { cid: 'latest' },
  }
  const listEl = { offsetHeight: 1050 }
  const mode = { kind: 'FOLLOW_BOTTOM' }
  const closedSpacer = _computeSpacerH(
    scrollEl, listEl, latestUserMsgEl, mode,
  )
  scrollEl.clientHeight = 500
  const openSpacer = _computeSpacerH(
    scrollEl, listEl, latestUserMsgEl, mode,
  )

  assert.equal(closedSpacer, 246)
  assert.equal(openSpacer, 0)
  assert.equal(
    (listEl.offsetHeight + openSpacer - 500)
      - (listEl.offsetHeight + closedSpacer - 800),
    54,
    'after reservation reaches zero, only the remaining overflow moves the tail',
  )
})

test('tool expansion consumes reservation and collapse restores the exact deficit', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 915 })
  const lastUserMsgEl = {
    offsetTop: 200,
    offsetHeight: 80,
    dataset: { cid: 'latest' },
  }
  const mode = { kind: 'ANCHOR_AT', key: 'latest-user', offset: 4 }

  const collapsed = _computeSpacerH(
    scrollEl, { offsetHeight: 500 }, lastUserMsgEl, mode,
  )
  const expanded = _computeSpacerH(
    scrollEl, { offsetHeight: 1300 }, lastUserMsgEl, mode,
  )
  const collapsedAgain = _computeSpacerH(
    scrollEl, { offsetHeight: 500 }, lastUserMsgEl, mode,
  )

  assert.equal(collapsed, 611)
  assert.equal(expanded, 0)
  assert.equal(collapsedAgain, collapsed)
})

test('spacer reservation returns zero before there is a user message', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 600 })
  const listEl = { offsetHeight: 200 }

  assert.equal(_computeSpacerH(scrollEl, listEl, null), 0)
})

test('ordinary question-answer anchor keeps the stable latest-turn tail range', () => {
  const anchor = { offsetTop: 60, offsetHeight: 220 }
  const scrollEl = {
    clientHeight: 960,
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? anchor : null
    },
  }
  const mode = { kind: 'ANCHOR_AT', key: 'assistant-question', offset: 60 }

  assert.equal(
    _computeSpacerH(
      scrollEl,
      { offsetHeight: 1500 },
      { offsetTop: 1100, offsetHeight: 80, dataset: { cid: 'c-1' } },
      mode,
    ),
    556,
    'ordinary anchor visibility cannot create a second-stage bottom',
  )
})

test('question submission reserves the exact room that keeps its anchor reachable', () => {
  const anchor = { offsetTop: 1200, offsetHeight: 220 }
  const scrollEl = {
    clientHeight: 600,
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? anchor : null
    },
  }
  const mode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 60,
    questionSubmitBaseMode: { kind: 'PIN_USER_MSG', cid: 'c-1' },
  }
  const listEl = { offsetHeight: 1400 }
  const latestUser = {
    offsetTop: 1100,
    offsetHeight: 80,
    dataset: { cid: 'c-1' },
  }

  assert.equal(
    _computeSpacerH(scrollEl, listEl, latestUser, mode),
    340,
  )
  scrollEl.clientHeight = 700
  assert.equal(
    _computeSpacerH(scrollEl, listEl, latestUser, mode),
    440,
    'the submission overlay keeps the exact anchor across responsive geometry',
  )
})

test('answered question keeps its exact submission anchor when the keyboard closes', () => {
  const anchor = { offsetTop: 1200, offsetHeight: 220 }
  const scrollEl = {
    clientHeight: 700,
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? anchor : null
    },
  }
  const baseMode = { kind: 'PIN_USER_MSG', cid: 'c-1' }
  const heldMode = {
    kind: 'ANCHOR_AT',
    key: 'assistant-question',
    offset: 60,
    questionSubmitBaseMode: baseMode,
  }
  const listEl = { offsetHeight: 1400 }
  const latestUser = {
    offsetTop: 1100,
    offsetHeight: 80,
    dataset: { cid: 'c-1' },
  }

  assert.equal(
    _computeSpacerH(scrollEl, listEl, latestUser, heldMode),
    440,
    'the larger viewport reserves the exact 1140px anchor target',
  )
  assert.equal(
    listEl.offsetHeight
      + _computeSpacerH(scrollEl, listEl, latestUser, heldMode)
      - scrollEl.clientHeight,
    1140,
    'keyboard close keeps the question at the same 60px viewport offset',
  )
})

test('an off-content legacy anchor clamps to content then reserves for its visible latest user', () => {
  const anchor = { offsetTop: 500, offsetHeight: 220 }
  const scrollEl = {
    clientHeight: 700,
    scrollTop: 1400,
    querySelector(selector) {
      return selector === '[data-key="assistant-question"]' ? anchor : null
    },
  }
  const mode = {
    kind: 'ANCHOR_AT', key: 'assistant-question', offset: -900,
  }
  assert.equal(
    _computeSpacerH(
      scrollEl,
      { offsetHeight: 700 },
      { offsetTop: 100, offsetHeight: 80, dataset: { cid: 'c-1' } },
      mode,
    ),
    96,
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
  const lastUserMsgEl = {
    offsetTop: 700,
    offsetHeight: 80,
    dataset: { cid: 'c-1' },
  }

  assert.equal(
    _computeSpacerH(
      scrollEl,
      listEl,
      lastUserMsgEl,
      { kind: 'PIN_USER_MSG', cid: 'c-1' },
    ),
    396,
  )
})

// R5 regression contract: spacer sizing reads the active scroll box directly,
// so callers cannot preserve a stale keyboard-open height after it grows.
function pinReachable({ clientHeight, listH, lastUserTop }) {
  const scrollEl = makeScrollEl({
    scrollHeight: 0, scrollTop: 0, clientHeight,
  })
  const listEl = { offsetHeight: listH }
  const lastUserMsgEl = {
    offsetTop: lastUserTop,
    dataset: { cid: 'pin-row' },
  }
  const spacerH = _computeSpacerH(
    scrollEl,
    listEl,
    lastUserMsgEl,
    { kind: 'PIN_USER_MSG', cid: 'pin-row' },
  )
  const scrollHeight = listH + spacerH
  const maxScrollTop = scrollHeight - clientHeight
  const pinTarget = Math.max(0, lastUserTop - 4) // PIN_OFFSET = 4
  return { spacerH, maxScrollTop, pinTarget, reachable: maxScrollTop >= pinTarget }
}

test('R5: the active viewport keeps the pin exactly reachable', () => {
  const r = pinReachable({ clientHeight: 700, listH: 1040, lastUserTop: 1000 })
  assert.equal(r.reachable, true, 'message can reach the top after keyboard close')
  assert.equal(r.maxScrollTop, r.pinTarget, 'spacer reserves exactly enough to reach the pin — no extra cushion')
  assert.equal(r.maxScrollTop - r.pinTarget, 0, 'no reservable blank below the pinned message by default')
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
// F2 — remount reservation follows whether the latest user row is visible.
// ---------------------------------------------------------------------------

test('F2: an idle-mounted short chat reserves for its visible latest user', () => {
  const scrollEl = makeSpacerScrollEl({ clientHeight: 915 })
  const listEl = { offsetHeight: 260 }        // 2-message short chat, fits the viewport
  const lastUserMsgEl = {
    offsetTop: 200,
    offsetHeight: 60,
    dataset: { cid: 'c-1' },
  }
  const spacerH = _computeSpacerH(
    scrollEl,
    listEl,
    lastUserMsgEl,
    { kind: 'ANCHOR_AT', key: 'a-1', offset: 0 },
  )
  assert.equal(spacerH, 851)
})

test('F2: a saved pin restores as the same physical ordinary anchor', () => {
  const shortList = 260
  const clientHeight = 915
  const lowUserTop = 200
  const spacerH = _computeSpacerH(
    { clientHeight },
    { offsetHeight: shortList },
    { offsetTop: lowUserTop, offsetHeight: 60, dataset: { cid: 'c-1' } },
    { kind: 'PIN_USER_MSG', cid: 'c-1' },
  )
  const restored = makePinnableScrollEl({ listH: shortList, spacerH, clientHeight, userTop: lowUserTop, cid: 'c-1' })
  restored.querySelector = (selector) => {
    if (selector === '.spacer-dynamic') return { offsetHeight: spacerH }
    if (selector === '[data-key="user-c-1"]') return { offsetTop: lowUserTop }
    return null
  }
  applyMode(restored, {
    kind: 'ANCHOR_AT', key: 'user-c-1', offset: PIN_OFFSET,
  })
  assert.equal(restored.scrollTop, lowUserTop - PIN_OFFSET)
  assert.equal(restored.scrollHeight - restored.clientHeight, restored.scrollTop,
    'the reservation ends at the same visible location without live pin state')
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
  assert.ok(
    scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 50,
    'precondition: at the tail',
  )

  const restored = modeForForegroundReturn(scrollEl)
  assert.equal(restored.kind, 'ANCHOR_AT',
    'return freezes as an anchor, not the grown tail')
  assert.equal(restored.key, 'a-9')
})

// --- Sub-message reading resolution (R4) -----------------------------------
// One settled agentic turn in the owner's chats renders 73,721px — 77 viewport
// heights in a single message row. A whole-message anchor therefore cannot say
// WHERE in that turn the reader was, and any re-render height change threw them
// thousands of pixels away. These lock the part-level address that fixes it.

function partedRow(key, top, partHeights) {
  const row = {
    offsetTop: top,
    offsetHeight: partHeights.reduce((sum, h) => sum + h, 0),
    dataset: { key },
  }
  let cursor = top
  row.children = partHeights.map(height => {
    const child = { offsetTop: cursor, offsetHeight: height }
    cursor += height
    return child
  })
  return row
}

function partedScrollEl(row, { scrollTop, clientHeight = 900, spacer = 0 }) {
  const scrollHeight = row.offsetTop + row.offsetHeight + spacer
  const scrollEl = {
    scrollTop,
    clientHeight,
    scrollHeight,
    getBoundingClientRect: () => ({ top: 0 }),
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return { offsetHeight: spacer }
      return selector.includes(row.dataset.key) ? row : null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [row] : []
    },
  }
  // Every real element has a rect, and rects are what the shipping code
  // measures with. Give the fixture the geometry a browser would report so
  // these tests exercise that arithmetic rather than a test-only fallback.
  const attachRect = node => {
    node.getBoundingClientRect = () => ({ top: node.offsetTop - scrollEl.scrollTop })
    node.children?.forEach(attachRect)
  }
  attachRect(row)
  return scrollEl
}

test('a reading position inside one enormous turn addresses the part, not the turn', () => {
  // 300 worklog parts of 240px = a 72,000px single message.
  const row = partedRow('assistant-huge', 0, Array(300).fill(240))
  const scrollEl = partedScrollEl(row, { scrollTop: 48_000 })

  const mode = anchorModeFromScroll(scrollEl)

  assert.equal(mode.kind, 'ANCHOR_AT')
  assert.equal(mode.key, 'assistant-huge')
  assert.deepEqual(mode.part, [200],
    'the addressed part is the one under the viewport top')
  assert.equal(mode.offset, 0)
})

test('restoring an enormous turn survives a height change elsewhere in that turn', () => {
  const saved = anchorModeFromScroll(
    partedScrollEl(partedRow('assistant-huge', 0, Array(300).fill(240)), {
      scrollTop: 48_000,
    }),
  )

  // The same turn re-renders with earlier parts taller (expanded tool output,
  // late syntax highlighting, swapped webfonts): every part below shifts down.
  const grown = partedRow('assistant-huge', 0, [
    ...Array(100).fill(600), ...Array(200).fill(240),
  ])
  const grownEl = partedScrollEl(grown, { scrollTop: 0 })
  applyMode(grownEl, saved)

  const target = grown.children[saved.part[0]]
  assert.equal(grownEl.scrollTop, target.offsetTop,
    'the reader lands on the same part they were reading, wherever it moved to')
  assert.equal(grownEl.scrollTop, 84_000)
})

test('an unresolvable saved location falls back to real content, not the top of the chat', () => {
  // The row is absent from this visit's committed window. That is a retrieval
  // failure; the automatic tail shown instead must not become the stored
  // location, or one bad return breaks every later return.
  const tail = partedRow('assistant-tail', 0, [400])
  const scrollEl = partedScrollEl(tail, { scrollTop: 0 })

  const restored = _validateSavedMode(
    { kind: 'ANCHOR_AT', key: 'assistant-absent', part: [12], offset: -30 },
    [],
    scrollEl,
  )

  assert.equal(restored.defaultTail, true,
    'the visit still shows real content rather than a blank viewport')
  assert.notEqual(restored.key, 'assistant-absent')
})

test('a part that is itself taller than the viewport resolves to a deeper path', () => {
  // The descent is not one level: a single worklog part can itself be
  // thousands of pixels, which is the whole reason `part` is a PATH.
  const row = partedRow('assistant-nested', 0, [1200, 3000, 1200])
  const middle = row.children[1]
  let cursor = middle.offsetTop
  middle.children = Array.from({ length: 10 }, () => {
    const kid = { offsetTop: cursor, offsetHeight: 300 }
    cursor += 300
    return kid
  })
  const scrollEl = partedScrollEl(row, { scrollTop: 1800 })

  const mode = anchorModeFromScroll(scrollEl)

  assert.deepEqual(mode.part, [1, 2],
    'descends into the oversized part, not just the oversized row')
  assert.equal(mode.offset, 0)

  applyMode(scrollEl, mode)
  assert.equal(scrollEl.scrollTop, 1800,
    'restores to the nested sub-part the reader was actually on')
})

test('a part path that no longer resolves fails the restore rather than jumping to the top of the turn', () => {
  const saved = anchorModeFromScroll(
    partedScrollEl(partedRow('assistant-huge', 0, Array(300).fill(240)), {
      scrollTop: 48_000,
    }),
  )
  assert.deepEqual(saved.part, [200])

  // The turn comes back with far fewer parts (a sliced cold render), so part
  // 200 does not exist. Degrading to the ROW would keep the part-relative
  // offset and drop the reader at the top of a 72,000px turn.
  const shrunk = partedRow('assistant-huge', 0, Array(30).fill(240))
  const scrollEl = partedScrollEl(shrunk, { scrollTop: 0 })

  const restored = _validateSavedMode(saved, [], scrollEl)

  assert.equal(restored.defaultTail, true,
    'a partially resolving path is an unresolved location, not a clamp')

  applyMode(scrollEl, restored)
  assert.equal(scrollEl.scrollTop, 6300,
    'lands on real content at the tail instead of scrollTop 0')
})
