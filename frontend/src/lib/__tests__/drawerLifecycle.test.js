import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  DRAWER_CLOSE_WATCHDOG_BUFFER_MS,
  drawerCloseWatchdogMs,
  drawerWidthFromPointerDelta,
  isGeneratedTouchClick,
  isHorizontalDrawerSwipe,
  shouldRestoreDrawerFocus,
  shouldSuppressDrawerSwipeClick,
  shouldAutoRevealActiveChat,
  clearDrawerGestureStyles,
  drawerOpenBlockedByDrag,
  settleDrawerSwipe,
} from '../drawerLifecycle.js'

test('drawer close restores only while the drawer still owns focus', () => {
  const inside = { id: 'inside' }
  const outside = { id: 'outside' }
  const body = { id: 'body' }
  const drawer = { contains: element => element === inside }

  assert.equal(shouldRestoreDrawerFocus({ drawer, activeElement: inside, body }), true)
  assert.equal(shouldRestoreDrawerFocus({ drawer, activeElement: body, body }), true)
  assert.equal(shouldRestoreDrawerFocus({ drawer, activeElement: null, body }), true)
  assert.equal(shouldRestoreDrawerFocus({ drawer, activeElement: outside, body }), false)
})

test('only the persistent sidebar follows active chat selections', () => {
  const activeChat = {
    open: true,
    persistent: true,
    activeView: 'chat',
    activeChatId: 'chat-42',
  }

  assert.equal(shouldAutoRevealActiveChat(activeChat), true)
  assert.equal(shouldAutoRevealActiveChat({ ...activeChat, persistent: false }), false,
    'opening a modal drawer must preserve its manual scroll position')
  assert.equal(shouldAutoRevealActiveChat({ ...activeChat, open: false }), false)
  assert.equal(shouldAutoRevealActiveChat({ ...activeChat, activeView: 'canvas' }), false)
  assert.equal(shouldAutoRevealActiveChat({ ...activeChat, activeChatId: null }), false)
})

test('the drawer open path stands down only while a drag is live', () => {
  // A live drag blocks the open (a left-edge tab drag must split, not open the
  // drawer); every non-live state — including an unset/absent ref — allows it.
  assert.equal(drawerOpenBlockedByDrag(true), true)
  assert.equal(drawerOpenBlockedByDrag(false), false)
  assert.equal(drawerOpenBlockedByDrag(undefined), false)
  assert.equal(drawerOpenBlockedByDrag(null), false)
})

test('closed drawer cleanup removes an interrupted swipe transform', () => {
  const removed = []
  const element = {
    classList: { remove: (...names) => removed.push(...names) },
    style: { transform: 'translateX(-25px)' },
  }

  clearDrawerGestureStyles(element)

  assert.deepEqual(removed, ['drawer--dragging'])
  assert.equal(element.style.transform, '')
})

test('drawer cleanup is safe before the panel ref mounts', () => {
  assert.doesNotThrow(() => clearDrawerGestureStyles(null))
})

test('a swipe-close during a pending traversal leaves drawer state and panel consistent', () => {
  // The owner (useNavigation.closeDrawer) answers false while its previous
  // history traversal is still pending, so `open` stays true. The panel must
  // then rest where an open drawer rests — never parked off-screen under a
  // scrim that still guards it (the reported "stuck until I open and close").
  const owner = { traversalPending: true, open: true }
  owner.closeDrawer = () => {
    if (owner.traversalPending) return false
    owner.open = false
    return true
  }
  const dragged = () => ({
    classList: { remove: () => {} },
    style: { transform: 'translateX(-200px)' },
  })

  const refused = dragged()
  settleDrawerSwipe(refused, { closeAccepted: owner.closeDrawer() })
  assert.equal(owner.open, true)
  assert.equal(refused.style.transform, '', 'refused close snaps back like a cancel')

  owner.traversalPending = false
  const accepted = dragged()
  settleDrawerSwipe(accepted, { closeAccepted: owner.closeDrawer() })
  assert.equal(owner.open, false)
  assert.equal(accepted.style.transform, 'translateX(-100%)',
    'an accepted close parks the panel at the closed target it will read next render')

  // No owner at all (mode transition, `onClose` undefined) can never accept.
  const orphaned = dragged()
  settleDrawerSwipe(orphaned, { closeAccepted: undefined })
  assert.equal(orphaned.style.transform, '')
  assert.doesNotThrow(() => settleDrawerSwipe(null, { closeAccepted: true }))
})

test('close watchdog follows the computed transform transition', () => {
  assert.equal(drawerCloseWatchdogMs({
    transitionProperty: 'transform',
    transitionDuration: '100ms',
    transitionDelay: '0s',
  }), 100 + DRAWER_CLOSE_WATCHDOG_BUFFER_MS)

  assert.equal(drawerCloseWatchdogMs({
    transitionProperty: 'opacity, transform',
    transitionDuration: '50ms, 0.25s',
    transitionDelay: '0s, 20ms',
  }), 270 + DRAWER_CLOSE_WATCHDOG_BUFFER_MS)

  assert.equal(drawerCloseWatchdogMs({
    transitionProperty: 'transform',
    transitionDuration: '100ms, 5s',
    transitionDelay: '0s',
  }), 100 + DRAWER_CLOSE_WATCHDOG_BUFFER_MS)
})

test('close watchdog releases immediately when transform motion is disabled', () => {
  assert.equal(drawerCloseWatchdogMs({
    transitionProperty: 'transform',
    transitionDuration: '0s',
    transitionDelay: '0s',
  }), 0)
  // Chromium's reduced-motion rule computes as `none` plus a nominal 1ms
  // duration. The absent property wins: there is no transform transition to
  // wait for.
  assert.equal(drawerCloseWatchdogMs({
    transitionProperty: 'none',
    transitionDuration: '0.001s',
    transitionDelay: '0s',
  }), 0)
  assert.equal(drawerCloseWatchdogMs({
    transitionProperty: 'opacity',
    transitionDuration: '500ms',
    transitionDelay: '0s',
  }), 0)
})

test('drawer resize follows pointer delta from either panel edge', () => {
  assert.equal(drawerWidthFromPointerDelta({
    startWidth: 320,
    delta: 48,
  }), 368)
  assert.equal(drawerWidthFromPointerDelta({
    startWidth: 320,
    delta: -48,
    edgeDirection: -1,
  }), 368)
})

test('drawer swipe classification rejects vertical and ambiguous movement', () => {
  assert.equal(isHorizontalDrawerSwipe(-25, 4), true)
  assert.equal(isHorizontalDrawerSwipe(-15, -120), false)
  assert.equal(isHorizontalDrawerSwipe(-8, 0), false)
})

test('cancelled drawer gestures never own a future click', () => {
  assert.equal(shouldSuppressDrawerSwipeClick({
    sawHorizontalMove: true,
    cancelled: true,
    dx: -30,
    dy: 0,
  }), false)
})

test('diagonal noise does not turn a completed vertical scroll into a swipe', () => {
  assert.equal(shouldSuppressDrawerSwipeClick({
    sawHorizontalMove: true,
    dx: -15,
    dy: -130,
  }), false)
})

test('only a normally completed horizontal swipe owns its generated click', () => {
  assert.equal(shouldSuppressDrawerSwipeClick({
    sawHorizontalMove: true,
    dx: -30,
    dy: 2,
  }), true)
  assert.equal(shouldSuppressDrawerSwipeClick({
    sawHorizontalMove: false,
    dx: -30,
    dy: 2,
  }), false)
})

test('the click guard fails open for keyboard and assistive activation', () => {
  assert.equal(isGeneratedTouchClick({ detail: 0 }), false)
  assert.equal(isGeneratedTouchClick({
    detail: 1,
    sourceCapabilities: { firesTouchEvents: false },
  }), false)
  assert.equal(isGeneratedTouchClick({
    detail: 0,
    sourceCapabilities: { firesTouchEvents: true },
  }), true)
  assert.equal(isGeneratedTouchClick({ detail: 1 }), true)
})
