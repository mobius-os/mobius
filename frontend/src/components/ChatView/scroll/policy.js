/** Pure intent transitions and input classification for chat scroll. */

import {
  _topmostVisibleMsg,
  anchorModeFromScroll,
  contentHoldModeFromScroll,
  isQuestionSubmissionMode,
  physicalBottomAnchorModeFromScroll,
} from './geometry.js'

const PHYSICAL_BOTTOM_EPSILON_PX = 4
export const FOLLOW_STICK_BAND_PX = 70
export const HISTORY_PREFETCH_PX = 240

export function olderHistoryRetryShown(error, offset) {
  return Boolean(error) && Number(offset) > 0
}

export function olderHistoryShouldLoad(scrollEl, { userDriven = false } = {}) {
  if (!scrollEl) return false
  return scrollEl.scrollHeight <= scrollEl.clientHeight + 1
    || (userDriven && scrollEl.scrollTop <= HISTORY_PREFETCH_PX)
}

/** The single submit-time rule used by direct, queued, and steered user rows.
 *  A row moves to the top (PIN_USER_MSG) only when it was the first visible
 *  user message, or the reader was at the physical autoscroll tail when
 *  submitted.
 *
 *  Dynamic spacer remains part of this geometry. It is reserved reply room,
 *  but it is also the range through which a reader moves upward after leaving
 *  autoscroll. Subtracting it made a message sitting mid-screen look like a
 *  bottom send and yanked the reader back to the top. Exact physical geometry
 *  remains synchronous even while ScrollMode settlement trails by a frame;
 *  ScrollMode is a DOM-less fallback only.
 */
export function shouldPinSend({
  scrollEl,
  mode,
  isFirstUserMsg,
}) {
  if (isFirstUserMsg) return true
  if (scrollEl) return isNearPhysicalBottom(scrollEl)
  return mode?.kind === 'FOLLOW_BOTTOM'
}


/** Layout may change bottom geometry without changing reader intent. Preserve
 *  the queued decision within its generation; after real reader movement,
 *  Fast-forward's current geometry wins. */
export function delayedSendWillPin({
  previousIntent,
  readerIntentVersion,
  willPinNow,
}) {
  return previousIntent?.readerIntentVersion === readerIntentVersion
    ? previousIntent.willPin
    : willPinNow
}


/** Position-based check against the one physical tail. Reserved spacer stays
 * in the range: scrolling upward through it is an explicit exit from
 * autoscroll, not a second kind of bottom. */
export function isNearPhysicalBottom(
  scrollEl,
  threshold = PHYSICAL_BOTTOM_EPSILON_PX,
) {
  if (!scrollEl) return false
  const gap = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
  return gap < threshold
}


/** Advance an armed live pin to tail-follow exactly when its reserved reply
 * room is exhausted. Settled/restored pins omit `followWhenFilled`, so later
 * viewport or lazy-layout changes cannot manufacture follow intent. */
export function modeAfterSpacerResize(mode, spacerH) {
  if (mode?.kind !== 'PIN_USER_MSG' || !mode.followWhenFilled) return mode
  return spacerH <= 1 ? { kind: 'FOLLOW_BOTTOM' } : mode
}


/** A short stream ended before filling its reservation: retain the pin
 * identity but retire its live-only automatic-follow handoff. */
export function settledPinMode(mode) {
  if (mode?.kind !== 'PIN_USER_MSG' || !mode.followWhenFilled) return mode
  return { kind: 'PIN_USER_MSG', cid: mode.cid }
}

/** Terminal promotion may commit before the final buffered text has changed
 * DOM geometry. A positive spacer is conclusive only after the layout is
 * stable; zero is immediately conclusive and hands off to follow. */
export function modeAfterTerminalLayout(mode, spacerH, layoutStable) {
  if (mode?.kind !== 'PIN_USER_MSG' || !mode.followWhenFilled) return mode
  const advanced = modeAfterSpacerResize(mode, spacerH)
  if (advanced !== mode) return advanced
  return layoutStable ? settledPinMode(mode) : mode
}


/** Resolve the quiet edge of a real reader gesture, using use-stick-to-bottom's
 * escape-latch semantics rather than a pixel-exact bottom test.
 *
 * Stickiness is a latch broken only by an explicit scroll UP (`escaped`).
 *   - Already following: stay in FOLLOW_BOTTOM unless the reader escaped upward.
 *     Content growth or downward momentum that drifted the viewport out of the
 *     band never breaks follow (the layout owner re-glues the tail).
 *   - Not following: enter FOLLOW_BOTTOM only when the gesture reached the
 *     bottom band (`reachedNearBottom`) and did not end escaped — i.e. the
 *     reader scrolled down to the tail.
 * Anything else holds the exact reader position. */
export function modeAfterReaderGesture({
  escaped,
  reachedNearBottom,
  wasFollowing,
  holdMode,
}) {
  const stick = !escaped && (wasFollowing || reachedNearBottom)
  if (stick) return { kind: 'FOLLOW_BOTTOM' }
  return holdMode || { kind: 'INITIAL' }
}


/** The escape/re-engage direction a raw reader input implies, mirroring
 * use-stick-to-bottom's wheel + keyboard handling: scrolling UP escapes the
 * bottom lock, scrolling DOWN re-engages it. Returns null for inputs with no
 * vertical scroll intent (they neither escape nor re-engage the latch). Read
 * from the input event itself so a single wheel tick or arrow press flips the
 * latch immediately, with zero layout cost. */
export function readerInputEscapeDirection(
  type,
  { deltaY = 0, key = '', shiftKey = false } = {},
) {
  if (type === 'wheel') {
    if (deltaY < 0) return 'up'
    if (deltaY > 0) return 'down'
    return null
  }
  if (type === 'keydown') {
    if (['ArrowUp', 'PageUp', 'Home'].includes(key)
        || (shiftKey && [' ', 'Spacebar'].includes(key))) return 'up'
    if (['ArrowDown', 'PageDown', 'End'].includes(key)
        || (!shiftKey && [' ', 'Spacebar'].includes(key))) return 'down'
    return null
  }
  return null
}


/** An end-directed input at the physical tail is meaningful even when the
 * browser is already clamped and therefore cannot emit a `scroll` event.
 * Wheel/keyboard and touch all use this predicate before claiming FOLLOW_BOTTOM
 * so a repeated "keep going" gesture has one semantic meaning on every input
 * path. */
export function readerInputClaimsPhysicalTail(
  escapeDirection,
  distanceToBottom,
) {
  return escapeDirection === 'down'
    && Number.isFinite(distanceToBottom)
    && distanceToBottom < PHYSICAL_BOTTOM_EPSILON_PX
}


/** Infer the same escape/re-engage direction from an actual scroll position.
 * Wheel, keyboard, and touch inputs expose direction before scrolling, but a
 * mouse scrollbar drag does not. Comparing consecutive owned positions keeps
 * that native path inside the same latch instead of snapping an upward drag
 * back to FOLLOW_BOTTOM at settlement. */
export function readerScrollEscapeDirection(
  previousScrollTop,
  nextScrollTop,
  epsilon = 0.5,
) {
  if (!Number.isFinite(previousScrollTop) || !Number.isFinite(nextScrollTop)) {
    return null
  }
  if (nextScrollTop < previousScrollTop - epsilon) return 'up'
  if (nextScrollTop > previousScrollTop + epsilon) return 'down'
  return null
}


/** A primary composer press or direct edit at the physical tail is an explicit
 * request to keep the latest content visible while the keyboard or composer
 * changes the viewport. The edit case covers paste/typing while the textarea
 * is already focused, so there may be no new pointer event to retire an older
 * scroll gesture. Read the tail before the foot observer publishes the new
 * composer height; edits higher in the transcript preserve their anchor. */
export function composerTailIntentRequestsFollow(event, scrollEl) {
  const primaryPress = event?.type === 'pointerdown' && event?.button === 0
  const directEdit = event?.type === 'input'
  if ((!primaryPress && !directEdit)
      || !event?.target?.matches?.('textarea.chat__input')
      || !scrollEl) return false
  return isNearPhysicalBottom(scrollEl)
}


const FOLLOW_ENTRY_EVENTS = new Set([
  'reader:scroll-bottom',
  'reader:composer-bottom',
  'layout:reservation-filled',
  'terminal:reservation-filled',
])

/** Enforce the state machine's narrow entry authority.
 *
 * Only a send can create a pin. FOLLOW_BOTTOM can be entered only by explicit
 * tail intent (a real bottom gesture or a composer press already at the
 * physical tail), or by an already-armed pin consuming its reservation. Other
 * events may preserve the current mode, demote it to an anchor/initial hold,
 * or retire an armed pin, but cannot manufacture automatic scroll ownership.
 */
export function modeForScrollTransition(previousMode, proposedMode, event) {
  if (!proposedMode) return previousMode
  const restoresQuestionSubmissionBase = (
    event === 'stream:question-response-follow'
      && proposedMode?.kind === 'FOLLOW_BOTTOM'
  )
    && isQuestionSubmissionMode(previousMode)
    && previousMode.questionSubmitBaseMode === proposedMode
  if (restoresQuestionSubmissionBase) return proposedMode

  const samePin = previousMode?.kind === 'PIN_USER_MSG'
    && proposedMode.kind === 'PIN_USER_MSG'
    && previousMode.cid === proposedMode.cid

  if (proposedMode.kind === 'PIN_USER_MSG'
      && !samePin
      && event !== 'send:pin-user-message') {
    return previousMode
  }

  if (proposedMode.kind === 'FOLLOW_BOTTOM'
      && previousMode?.kind !== 'FOLLOW_BOTTOM') {
    if (!FOLLOW_ENTRY_EVENTS.has(event)) return previousMode
    if (!event.startsWith('reader:')
        && !(previousMode?.kind === 'PIN_USER_MSG'
          && previousMode.followWhenFilled)) {
      return previousMode
    }
  }

  return proposedMode
}


/** A layout plan may commit only while it owns time, generation, and touch.
 * Gesture timing blocks work during the active handoff; the monotonic reader
 * generation prevents work captured before a later gesture from regaining
 * authority when that timing gate eventually opens; live touch contact holds
 * ownership even after the timing gate closes (contract R5, v1.24).
 */
export function scrollAuthorityAllowsCommit({
  capturedVersion,
  currentVersion,
  gestureWindowUntil,
  now,
  touchContactActive = false,
}) {
  return capturedVersion === currentVersion
    && layoutMayOwnScroll(gestureWindowUntil, now, touchContactActive)
}


/** Reduce one scroll frame into the current input sequence's intent.
 * A sequence advances the monotonic generation once and latches physical-tail
 * arrival until settlement. A newer sequence starts a fresh tail decision even
 * when several nearby gestures share one quiet-edge layout pass.
 */
export function readerIntentAfterScroll({
  gestureSequence,
  claimedSequence,
  version,
  reachedBottom = false,
  atBottom = false,
}) {
  const sameSequence = gestureSequence === claimedSequence
  return {
    claimedSequence: gestureSequence,
    version: sameSequence ? version : version + 1,
    reachedBottom: atBottom || (sameSequence && reachedBottom),
  }
}


/** Terminal rAF work distinguishes a no-scroll input from actual reader
 * movement. A closed gesture gate or live touch contact with the same
 * generation means wait; only a newer actual-scroll generation makes the old
 * terminal plan permanently stale.
 */
export function terminalLayoutAuthority({
  capturedVersion,
  currentVersion,
  gestureWindowUntil,
  now,
  touchContactActive = false,
}) {
  if (capturedVersion !== currentVersion) return 'stale'
  return layoutMayOwnScroll(gestureWindowUntil, now, touchContactActive)
    ? 'commit'
    : 'wait'
}


/** Layout observers may own scrollTop only outside the gesture-intent window
 * AND while no touch pointer is physically on the transcript (contract R5,
 * v1.24). Input events precede the browser's first `scroll` event; without the
 * timing gate, a streaming ResizeObserver can re-pin/follow in that gap and
 * throw the reader back before onScroll has a chance to stamp ANCHOR_AT.
 * Without the contact gate, a finger resting on the glass mid-gesture — a
 * reading pause longer than the quiet edge or the no-scroll dead-man — let
 * the same observers write scrollTop under live touch. Contact is ownership
 * by itself: the browser's own `scrollend` never fires during contact, and
 * this predicate must not be less careful than the primitive it emulates. */
export function layoutMayOwnScroll(gestureWindowUntil, now, touchContactActive = false) {
  return !touchContactActive && now >= gestureWindowUntil
}


/** Return a retry delay only once reader ownership has a finite release point.
 * Infinity is the input/scroll/quiet-settle handoff, not a timer duration
 * (browsers clamp an infinite setTimeout unpredictably). */
export function gestureLayoutRetryDelay(gestureWindowUntil, now) {
  if (!Number.isFinite(gestureWindowUntil)) return null
  return Math.max(0, gestureWindowUntil - now) + 1
}


/** Only keys whose default action can move the chat begin reader ownership.
 * Text entry and activating controls inside a message must not freeze layout
 * until the no-scroll dead-man expires. */
export function readerInputMayScroll(type, key = '') {
  if (type !== 'keydown') return true
  return [
    'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', 'Tab', ' ',
    'Spacebar',
  ].includes(key)
}


/** Nested controls keep the keys and scroll range they can consume. Once an
 * explicitly marked nested scroller reaches its matching edge, native scroll
 * chaining may hand the same wheel/swipe to the transcript. */
export function nestedReaderTargetOwnsInput({
  type,
  key = '',
  target = null,
  scrollEl = null,
  direction = null,
} = {}) {
  if (type === 'keydown') {
    const editingControl = target?.closest?.(
      'textarea, input, select, [contenteditable]:not([contenteditable="false"]), '
      + '[role="textbox"], [role="searchbox"], [role="combobox"], '
      + '[role="listbox"], [role="spinbutton"], [role="slider"]',
    )
    if (editingControl) return true
    if ([' ', 'Spacebar'].includes(key)) {
      return !!target?.closest?.(
        'button, a[href], summary, [role="button"], [role="menuitem"], [role="option"]',
      )
    }
    return false
  }

  if (!['wheel', 'pointermove', 'touchmove'].includes(type)
      || !['up', 'down'].includes(direction)) return false
  const nested = target?.closest?.('[data-chat-scroll-region], .chat__scroll')
  if (!nested || nested === scrollEl) return false

  const scrollTop = Number(nested.scrollTop)
  const scrollHeight = Number(nested.scrollHeight)
  const clientHeight = Number(nested.clientHeight)
  if (![scrollTop, scrollHeight, clientHeight].every(Number.isFinite)) return false
  const maxScrollTop = Math.max(0, scrollHeight - clientHeight)
  if (maxScrollTop <= 0.5) return false
  return direction === 'up'
    ? scrollTop > 0.5
    : scrollTop < maxScrollTop - 0.5
}


/** A disclosure activation is a reading action even when it produces no native
 * scroll event. Snapshotting the current message anchor before its body changes
 * prevents a stale FOLLOW_BOTTOM mode from replaying after pointerup and
 * dragging a near-foot activity header down into the newly-opened timeline. */
export function readerInputActivatesDisclosure(
  type,
  key = '',
  target = null,
  pointerButton = 0,
) {
  const disclosure = target?.closest?.(
    'button.chat__activity-header, button.chat__activity-think-toggle, button.chat__tool-header, button.chat__marker-header',
  )
  if (!disclosure) return false
  return (type === 'pointerdown' && pointerButton === 0)
    || type === 'touchstart'
    || (type === 'keydown' && ['Enter', ' ', 'Spacebar'].includes(key))
}


/** Wheel and keyboard input have no pointer/touch release event. They get a
 * next-frame no-scroll release only when their requested direction is exactly
 * clamped at the corresponding edge (or cannot move this vertical scroller).
 * A proximity epsilon is not sufficient: browser-owned wheel and keyboard
 * scrolling can arrive after rAF. When movement is possible, the actual scroll
 * event owns the release.
 *
 * `readGeometry` is a THUNK, not a value, because only wheel and scroll-key
 * branches below ever read it.
 *
 * This function is called from the shared user-input handler. Passing an
 * eagerly-built object meant `scrollTop`/`scrollHeight`/`clientHeight` were read
 * on touch input and then discarded - and reading `scrollHeight` forces a
 * synchronous layout of the whole (unvirtualized) transcript. Wheel and the
 * comparatively rare scroll keys genuinely need the values; pointer input does
 * not.
 *
 * Deferring is deliberately done HERE rather than by guarding the call site.
 * Which input types need geometry is this function's own
 * rule; duplicating it at the caller would let the two drift apart silently.
 */
export function readerInputNeedsFrameRelease(
  type,
  readGeometry,
  key = '',
  shiftKey = false,
) {
  if (type !== 'wheel' && type !== 'keydown') return false

  let towardStart = false
  let towardEnd = false
  if (type === 'keydown') {
    towardStart = ['ArrowUp', 'PageUp', 'Home'].includes(key)
      || (shiftKey && [' ', 'Spacebar'].includes(key))
    towardEnd = ['ArrowDown', 'PageDown', 'End'].includes(key)
      || (!shiftKey && [' ', 'Spacebar'].includes(key))
    // Tab may reveal a newly-focused control, but has no stable direction to
    // prove against this scroller. Keep its old fast no-scroll release; focus
    // management owns any later reveal. It does not need a layout read.
    if (!towardStart && !towardEnd) return true
  }

  const {
    deltaY = 0,
    scrollTop = 0,
    scrollHeight = 0,
    clientHeight = 0,
  } = (typeof readGeometry === 'function' ? readGeometry() : readGeometry) || {}

  const maxScrollTop = Math.max(0, scrollHeight - clientHeight)

  if (type === 'keydown') {
    return towardStart ? scrollTop <= 0 : scrollTop >= maxScrollTop
  }

  if (!Number.isFinite(deltaY) || deltaY === 0) return true

  if (deltaY < 0) return scrollTop <= 0
  return scrollTop >= maxScrollTop
}


/** Foreground return (visibilitychange/pageshow/online) is not a reading
 *  gesture. Freeze the exact visible anchor even when the chat was following
 *  before it left: content may have grown while inactive, and returning must
 *  never jump to that newer tail. Manual scrolling to the bottom re-enables
 *  FOLLOW_BOTTOM afterward. */
export function modeForForegroundReturn(scrollEl) {
  if (!scrollEl) return null
  return contentHoldModeFromScroll(scrollEl)
}


/** Leaving a chat is different from actively watching its tail. Persist the
 *  exact visible reading position — even when that position is currently the
 *  physical bottom — so new content that arrives while the chat is inactive
 *  appears below the restored viewport instead of redefining "bottom" and
 *  yanking the reader to the latest tail. */
export function modeForChatExit(scrollEl) {
  if (!scrollEl) return null
  return contentHoldModeFromScroll(scrollEl)
}


/** A disclosure toggle obeys the existing reading mode instead of inventing a
 * second scroll policy. FOLLOW_BOTTOM stays live and follows the resized tail;
 * every non-follow mode freezes the exact visible message anchor before the
 * disclosure changes height. Repeating the same toggle therefore has the same
 * result until the reader explicitly changes scroll mode. */
export function modeForDisclosureToggle(scrollEl, currentMode) {
  if (currentMode?.kind === 'FOLLOW_BOTTOM') return currentMode
  return anchorModeFromScroll(scrollEl) || currentMode
}


/** Submitting an in-message question answer resumes output inside the same
 * assistant row and may replace the card's controls immediately. Freeze the
 * exact visible row/offset during that card-to-stream handoff. The overlay
 * remembers the mode that owned the unanswered card: an accepted same-turn
 * answer may restore its prior FOLLOW_BOTTOM, while a reading hold remains a
 * hold. Keyboard, toolbar, pane, and orientation changes preserve the overlay;
 * only visible response activity or newer reader intent releases it. */
export function modeForQuestionSubmission(scrollEl, currentMode) {
  if (!scrollEl) return currentMode
  const anchor = anchorModeFromScroll(scrollEl)
  if (!anchor) return currentMode
  // questionSubmitBaseMode records the reader's intent behind the unanswered
  // card so a continuation can restore it. A reader parked at the physical tail
  // is "watching the tail" whether the live mode is FOLLOW_BOTTOM or a settled
  // PIN / at-bottom anchor whose automatic follow-arm was already retired — the
  // common case where a short intro + question card ends the stream before its
  // reservation fills, settling the pin (settledPinMode) at the bottom. Only
  // FOLLOW_BOTTOM used to count as follow intent, so answering such a card left
  // the continuation stranded without autoscroll. Normalize an at-tail base to
  // FOLLOW_BOTTOM here (the layer that still has the live scroll position) so
  // the existing resume path re-follows; a hold up-thread stays a hold.
  const baseMode = currentMode?.questionSubmitBaseMode
    || (currentMode?.kind !== 'FOLLOW_BOTTOM' && isNearPhysicalBottom(scrollEl)
      ? { kind: 'FOLLOW_BOTTOM' }
      : currentMode)
  return {
    ...anchor,
    questionSubmitBaseMode: baseMode,
  }
}


/** Restore live following when the first post-answer activity starts only
 * when the submitted card was itself being followed and no newer reader
 * scroll or semantic location has replaced that temporary hold. Acceptance
 * alone leaves the card anchored: it is not yet a visible continuation. */
export function modeAfterQuestionResponseStart({
  currentMode,
  submission,
  currentReaderIntentVersion,
}) {
  const submittedMode = submission?.mode
  const baseMode = submittedMode?.questionSubmitBaseMode
  if (baseMode?.kind !== 'FOLLOW_BOTTOM'
      || submission?.readerIntentVersion !== currentReaderIntentVersion) {
    return currentMode
  }
  if (currentMode === baseMode) return currentMode
  return currentMode === submittedMode ? baseMode : currentMode
}


/** A queued send changes composer/footer layout but does not add a transcript
 * row. Freeze the visible anchor before that reflow; its separately-captured
 * submit intent still decides what happens when the row is later promoted. */
export function modeForQueuedSubmission(scrollEl, currentMode) {
  if (!scrollEl) return currentMode
  const visible = _topmostVisibleMsg(scrollEl)
  if (!visible) return currentMode

  // A live assistant row can be split by fast-forward: its rendered content
  // is sealed into history, the steered user row is inserted, and a new live
  // assistant row continues below it. The active shell therefore cannot own a
  // queue-time anchor even though its data-key is stable during ordinary
  // streaming. Anchor to the nearest preceding transcript row instead; that
  // row survives the split and its (possibly negative) visual offset preserves
  // the exact reading position.
  let anchor = visible
  if (visible.hasAttribute?.('data-active-assistant')) {
    const rows = [...scrollEl.querySelectorAll('.chat__msg[data-key]')]
    const index = rows.indexOf(visible)
    if (index > 0) anchor = rows[index - 1]
  }

  return anchor?.dataset?.key
    ? {
        kind: 'ANCHOR_AT',
        key: anchor.dataset.key,
        offset: anchor.offsetTop - scrollEl.scrollTop,
      }
    : currentMode
}
