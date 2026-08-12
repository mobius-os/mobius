import test from 'node:test'
import assert from 'node:assert/strict'
import {
  _anchorReapplyNeeded,
  _pinReapplyNeeded,
  anchorModeFromScroll,
  applyMode,
  modeAfterReaderGesture,
  modeForScrollTransition,
  readerIntentAfterScroll,
  scrollAuthorityAllowsCommit,
  terminalLayoutAuthority,
} from '../useScrollMode.js'

test('anchor repair never moves backward over an unchanged reader position', () => {
  // target = 960, but the viewport is now farther down at 1080. With an
  // unchanged row offset this is reader movement, not browser clamp damage.
  assert.equal(
    _anchorReapplyNeeded(
      {
        scrollHeight: 2000,
        scrollTop: 1080,
        clientHeight: 700,
        querySelector: selector => (
          selector === '[data-key="k-1"]' ? { offsetTop: 1000 } : null
        ),
      },
      { kind: 'ANCHOR_AT', key: 'k-1', offset: 40 },
      1000,
    ),
    false,
  )
})

test('pin repair never moves backward over an unchanged reader position', () => {
  const row = { offsetTop: 133 }
  const scrollEl = {
    scrollHeight: 1600,
    clientHeight: 700,
    scrollTop: 221,
    querySelector: selector => (
      selector === '[data-cid="c-111"]' ? row : null
    ),
  }
  // The target is 101px. Being at 221px is a downward reader move, not damage.
  assert.equal(
    _pinReapplyNeeded(
      scrollEl,
      { kind: 'PIN_USER_MSG', cid: 'c-111' },
      row.offsetTop,
    ),
    false,
  )
})

test('reserved-bottom reader settlement enters follow without moving backward', () => {
  const row = {
    offsetTop: 500,
    offsetHeight: 220,
    dataset: { key: 'assistant-tail' },
  }
  const spacer = { offsetHeight: 1200 }
  const scrollEl = {
    scrollHeight: 1900,
    scrollTop: 1200,
    clientHeight: 700,
    currentCSSZoom: 1,
    clientWidth: 1000,
    offsetWidth: 1000,
    offsetHeight: 700,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 700 }),
    querySelector(selector) {
      if (selector === '.spacer-dynamic') return spacer
      if (selector === '[data-key="assistant-tail"]') return row
      return null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg[data-key]' ? [row] : []
    },
  }

  const holdMode = anchorModeFromScroll(scrollEl)
  assert.deepEqual(holdMode, {
    kind: 'ANCHOR_AT',
    key: 'assistant-tail',
    offset: -700,
  })
  const settledMode = modeAfterReaderGesture({
    escaped: false,
    reachedNearBottom: true,
    wasFollowing: false,
    holdMode,
  })
  assert.deepEqual(settledMode, { kind: 'FOLLOW_BOTTOM' })

  applyMode(scrollEl, settledMode)
  assert.equal(scrollEl.scrollTop, 1200,
    'following the physical tail must not jump backward before reservation')
})

test('physical reader bottom creates follow without a reservation branch', () => {
  const hold = { kind: 'ANCHOR_AT', key: 'a-1', offset: -300 }
  assert.deepEqual(modeAfterReaderGesture({
    escaped: false,
    reachedNearBottom: true,
    wasFollowing: false,
    holdMode: hold,
  }), { kind: 'FOLLOW_BOTTOM' })
  assert.equal(modeAfterReaderGesture({
    escaped: false,
    reachedNearBottom: false,
    wasFollowing: false,
    holdMode: hold,
  }), hold)
})

test('transition entry authority prevents layout and reader paths creating pins', () => {
  const hold = { kind: 'ANCHOR_AT', key: 'a-1', offset: 30 }
  const pin = { kind: 'PIN_USER_MSG', cid: 'c-2', followWhenFilled: true }
  assert.equal(modeForScrollTransition(hold, pin, 'reader:hold-exact'), hold)
  assert.equal(modeForScrollTransition(hold, pin, 'layout:mode-transition'), hold)
  assert.equal(modeForScrollTransition(hold, pin, 'send:pin-user-message'), pin)
})

test('question viewport release restores only its captured base authority', () => {
  const follow = { kind: 'FOLLOW_BOTTOM' }
  const pin = { kind: 'PIN_USER_MSG', cid: 'c-2', followWhenFilled: true }
  const followOverlay = {
    kind: 'ANCHOR_AT',
    key: 'q-1',
    offset: 40,
    questionSubmitViewportH: 720,
    questionSubmitBaseMode: follow,
  }
  const pinOverlay = {
    ...followOverlay,
    questionSubmitBaseMode: pin,
  }

  assert.equal(modeForScrollTransition(
    followOverlay,
    follow,
    'layout:question-viewport-release',
  ), follow)
  assert.equal(modeForScrollTransition(
    pinOverlay,
    pin,
    'layout:question-viewport-release',
  ), pin)
  assert.equal(modeForScrollTransition(
    followOverlay,
    { kind: 'FOLLOW_BOTTOM' },
    'layout:question-viewport-release',
  ), followOverlay, 'an equivalent-looking mode is not the overlay\'s authority')
})

test('only explicit tail intent or an armed pin handoff can enter follow', () => {
  const hold = { kind: 'ANCHOR_AT', key: 'a-1', offset: 30 }
  const follow = { kind: 'FOLLOW_BOTTOM' }
  const armedPin = {
    kind: 'PIN_USER_MSG', cid: 'c-2', followWhenFilled: true,
  }
  const settledPin = { kind: 'PIN_USER_MSG', cid: 'c-2' }

  assert.equal(modeForScrollTransition(hold, follow, 'layout:mode-transition'), hold)
  assert.equal(
    modeForScrollTransition(settledPin, follow, 'layout:reservation-filled'),
    settledPin,
  )
  assert.equal(
    modeForScrollTransition(armedPin, follow, 'layout:reservation-filled'),
    follow,
  )
  assert.equal(
    modeForScrollTransition(hold, follow, 'reader:scroll-bottom'),
    follow,
  )
  assert.equal(
    modeForScrollTransition(hold, follow, 'reader:composer-bottom'),
    follow,
  )
})

test('a newer reader generation permanently rejects stale layout work', () => {
  assert.equal(scrollAuthorityAllowsCommit({
    capturedVersion: 4,
    currentVersion: 5,
    gestureWindowUntil: 0,
    now: 100,
  }), false, 'an expired timing gate cannot revive generation 4')
  assert.equal(scrollAuthorityAllowsCommit({
    capturedVersion: 5,
    currentVersion: 5,
    gestureWindowUntil: 120,
    now: 100,
  }), false, 'the current generation still yields during an active gesture')
  assert.equal(scrollAuthorityAllowsCommit({
    capturedVersion: 5,
    currentVersion: 5,
    gestureWindowUntil: 0,
    now: 100,
  }), true)
})

test('two gestures inside one quiet settlement advance two generations', () => {
  const firstGesture = readerIntentAfterScroll({
    gestureSequence: 11,
    claimedSequence: null,
    version: 4,
    atBottom: false,
  })
  assert.deepEqual(firstGesture, {
    claimedSequence: 11, version: 5, reachedBottom: false,
  })
  const reachedTail = readerIntentAfterScroll({
    gestureSequence: 11,
    claimedSequence: firstGesture.claimedSequence,
    version: firstGesture.version,
    reachedBottom: firstGesture.reachedBottom,
    atBottom: true,
  })
  assert.deepEqual(reachedTail, {
    claimedSequence: 11, version: 5, reachedBottom: true,
  }, 'one input sequence shares its generation and latches physical-tail arrival')
  assert.deepEqual(readerIntentAfterScroll({
    gestureSequence: 11,
    claimedSequence: reachedTail.claimedSequence,
    version: reachedTail.version,
    reachedBottom: reachedTail.reachedBottom,
    atBottom: false,
  }), reachedTail, 'lazy output cannot erase tail intent before settlement')

  const secondGesture = readerIntentAfterScroll({
    gestureSequence: 12,
    claimedSequence: reachedTail.claimedSequence,
    version: reachedTail.version,
    reachedBottom: reachedTail.reachedBottom,
    atBottom: false,
  })
  assert.deepEqual(secondGesture, {
    claimedSequence: 12, version: 6, reachedBottom: false,
  }, 'a newer input sequence advances and starts a fresh tail decision')
})

test('terminal settlement waits through taps but dies after actual reader movement', () => {
  assert.equal(terminalLayoutAuthority({
    capturedVersion: 7,
    currentVersion: 7,
    gestureWindowUntil: Number.POSITIVE_INFINITY,
    now: 100,
  }), 'wait', 'a tap/input gate alone cannot retire the armed pin')
  assert.equal(terminalLayoutAuthority({
    capturedVersion: 7,
    currentVersion: 7,
    gestureWindowUntil: 0,
    now: 101,
  }), 'commit', 'a no-scroll release resumes terminal layout settlement')
  assert.equal(terminalLayoutAuthority({
    capturedVersion: 7,
    currentVersion: 8,
    gestureWindowUntil: 0,
    now: 102,
  }), 'stale', 'actual reader movement permanently retires the older plan')
})
