/**
 * useScrollMode — the entire scroll state machine for ChatView.
 *
 * One ref holds the current mode; one function (applyMode) is the
 * single funnel that turns a mode into a concrete `scrollTop`.
 * Layout changes (RO, content mutation, spacer recompute, keyboard)
 * re-apply the mode but never mutate it. Only user gestures and
 * explicit lifecycle events (send, mount restore) mutate it.
 *
 * Modes:
 *   { kind: 'INITIAL' }           — pre-restore default; no-op
 *   { kind: 'PIN_USER_MSG', cid, followWhenFilled? }
 *                                  — user msg at top (post-send), keyed on
 *                                    the stable client `cid` (data-cid)
 *   { kind: 'FOLLOW_BOTTOM' }     — sticky-bottom for streaming
 *   { kind: 'ANCHOR_AT', key, offset }  — anchored at a specific msg
 *
 * Send pinning has one rule for direct, queued, and steered messages: the
 * first visible user message always pins; every later message pins when the
 * reader is at the real-content tail at submit time. DOM geometry is the
 * authority; ScrollMode is only a fallback when no scroll element exists.
 * A live pin leaves FOLLOW_BOTTOM while its dynamic spacer is
 * being consumed, then hands off to FOLLOW_BOTTOM exactly when that
 * reservation reaches zero. A short reply never reaches the handoff and
 * remains pinned after settle. The dynamic pin spacer is reserved room, not
 * message content.
 * Gesture-driven bottom detection reads the scroll container's geometry in
 * the scroll event itself. There is no second sentinel/observer authority
 * that can lag behind the reader and contradict the current viewport.
 *
 * User-gesture detection: pointerdown/wheel/touchstart/touchmove/keydown hold
 * reader ownership until the first scroll event actually arrives, then keep a
 * 250ms momentum window in which scroll events are user-driven and can
 * transition the mode. Outside that handoff/window, scrolls come from our
 * applyMode or browser clamps and are ignored.
 *
 * See ARCHITECTURE.md "Chat scroll + steer contract" for the full design.
 */

import { useState, useRef, useLayoutEffect, useCallback } from 'react'
import { cidOf } from './chatRuntimeState.js'
import { BEFORE_SHELL_RELOAD_EVENT } from '../../lib/shellReloadEvents.js'


// Hide-then-reveal safety cap. Code-block-heavy chats with KaTeX and
// highlight.js settle in the 500-1200ms range; a too-tight cap would
// reveal before ANCHOR_AT-restored scroll positions get re-anchored to
// the post-settle target offsets. Live streaming never reaches the
// 50ms quiet window anyway, so the cap is mostly about giving lazy
// renderers room to land before the chat becomes visible.
const REVEAL_CAP_MS = 1500

// User gesture momentum window — once the first scroll event lands, further
// events inside this window are treated as part of the same reader gesture.
// Before that first event the ref is Infinity, closing the input→scroll race;
// outside both phases, scrolls are our own applyMode or browser clamps and
// MUST NOT mutate mode.
const GESTURE_WINDOW_MS = 250

// A tap or non-scrolling key must not suspend layout ownership forever. This
// cap is only a dead-man release for input that produces no scroll event; a
// delayed scroll caused by a busy main thread still wins because its timer
// cannot run until that same thread is available again.
const PENDING_GESTURE_CAP_MS = 2000

// Physical-bottom transitions are exact reader intent, not the broader
// "near real-content tail" send heuristic. Allow only subpixel/browser
// rounding at the scroll extent.
const PHYSICAL_BOTTOM_EPSILON_PX = 4

// Bounded, content-free diagnostics. Recurring scroll bugs used to require
// reconstructing races from screenshots and guesses; this keeps the last
// controller transitions and actual automatic writes without recording any
// message text, message keys, or pin cids.
const SCROLL_TRACE_LIMIT = 80

export function _scrollModeForDiagnostics(mode) {
  if (!mode?.kind) return { kind: 'NONE' }
  return {
    kind: mode.kind,
    ...(mode.kind === 'PIN_USER_MSG'
      ? { armed: !!mode.followWhenFilled }
      : {}),
  }
}

function _scrollGeometryForDiagnostics(scrollEl) {
  if (!scrollEl) return null
  const spacerH = scrollEl.querySelector?.('.spacer-dynamic')?.offsetHeight || 0
  return {
    top: Math.round(scrollEl.scrollTop || 0),
    height: Math.round(scrollEl.scrollHeight || 0),
    viewport: Math.round(scrollEl.clientHeight || 0),
    spacer: Math.round(spacerH),
  }
}

function _appendScrollTrace(bucket, entry) {
  if (typeof window === 'undefined') return
  const existing = window.__mobiusChatScrollTrace
  const trace = existing?.version === 1
    ? existing
    : { version: 1, transitions: [], writes: [] }
  const rows = trace[bucket]
  if (!Array.isArray(rows)) return
  rows.push(entry)
  if (rows.length > SCROLL_TRACE_LIMIT) {
    rows.splice(0, rows.length - SCROLL_TRACE_LIMIT)
  }
  window.__mobiusChatScrollTrace = trace
}

// Per-chat ScrollMode persistence in sessionStorage. Schema 3 retires modes
// written by the old missing-location fallback, which manufactured an
// ANCHOR_AT from the browser's initial scrollTop=0 and then persisted that
// accidental top-of-chat position as if the reader had chosen it.
const SCROLL_MODE_SCHEMA = '3'
const _scrollModes = (() => {
  try {
    if (sessionStorage.getItem('chat-mode-schema') !== SCROLL_MODE_SCHEMA) {
      sessionStorage.removeItem('chat-mode')
      sessionStorage.setItem('chat-mode-schema', SCROLL_MODE_SCHEMA)
      return {}
    }
    return JSON.parse(sessionStorage.getItem('chat-mode') || '{}')
  }
  catch { return {} }
})()


/** Returns the first message <li> whose bottom edge is past the
 *  viewport top — i.e. the topmost partially-visible message.
 *  Used to resolve a fresh ANCHOR_AT when the user scrolls. */
function _topmostVisibleMsg(scrollEl) {
  const items = scrollEl.querySelectorAll('.chat__msg[data-key]')
  const top = scrollEl.scrollTop
  for (const el of items) {
    const bottom = el.offsetTop + el.offsetHeight
    if (bottom > top) return el
  }
  return items[items.length - 1] || null
}


/** Snapshot the reader's current scroll position as an ANCHOR_AT mode
 *  (the same {key, offset} the gesture-gated scroll handler stamps when
 *  the user scrolls up). Returns null when there's no scroll element or
 *  no anchorable message.
 *
 *  Why this exists: a non-pinning send must not leave a stale PIN_USER_MSG
 *  behind. The bottom spacer is always reserved for the latest user message,
 *  but the scrollTop write is still mode-driven. The send sites call this to
 *  convert a stale PIN into the reader's actual position, so the reader stays
 *  exactly where they were while the new message still gets bottom room below
 *  it if they later scroll to the tail. */
export function anchorModeFromScroll(scrollEl) {
  if (!scrollEl) return null
  const anchorEl = _topmostVisibleMsg(scrollEl)
  if (!anchorEl?.dataset?.key) return null
  return {
    kind: 'ANCHOR_AT',
    key: anchorEl.dataset.key,
    offset: anchorEl.offsetTop - scrollEl.scrollTop,
  }
}


/** Create a settled anchor with the latest real conversation content at the
 * viewport bottom. This is a one-time restoration target, NOT FOLLOW_BOTTOM:
 * later streaming/layout growth cannot drag the reader after return. */
export function bottomAnchorModeFromScroll(scrollEl) {
  if (!scrollEl) return null
  const items = scrollEl.querySelectorAll('.chat__msg[data-key]')
  const last = items[items.length - 1]
  const key = last?.dataset?.key
  if (!last || !key) return null
  const spacerH = scrollEl.querySelector('.spacer-dynamic')?.offsetHeight || 0
  const realContentH = scrollEl.scrollHeight - spacerH
  const targetScrollTop = Math.max(0, realContentH - scrollEl.clientHeight)
  return {
    kind: 'ANCHOR_AT',
    key,
    offset: last.offsetTop - targetScrollTop,
    defaultTail: true,
  }
}


/** Resolve the DOM row a PIN_USER_MSG targets: the user row whose
 *  `data-cid` equals the mode's cid.
 *
 *  A strict exact match with NO last-row fallback. The pinned row carries its
 *  final `cid` from mint (the same value the optimistic row and the confirmed
 *  server row share), so the exact selector always resolves the just-sent row —
 *  the ts-swap that once broke the exact lookup (and forced a last-row
 *  fallback) cannot happen anymore. */
function _pinnedUserEl(scrollEl, cid) {
  if (!scrollEl || cid == null) return null
  const esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(cid) : cid
  return scrollEl.querySelector(`.chat__msg--user[data-cid="${esc}"]`)
}

/** The LAST user row in the DOM — for the one spacer-geometry caller that
 *  legitimately wants "the newest user row" independent of pin identity (a
 *  transiently-null lastUserMsgRef during a render swap). Kept separate from
 *  `_pinnedUserEl` so the pin selector stays strict. */
function _lastUserRowEl(scrollEl) {
  if (!scrollEl) return null
  const rows = scrollEl.querySelectorAll('.chat__msg--user[data-cid]')
  return rows.length ? rows[rows.length - 1] : null
}

/** Apply a scroll mode by setting scrollTop. Idempotent — call as
 *  often as layout changes happen. */
export function applyMode(scrollEl, mode) {
  if (!scrollEl || !mode) return
  switch (mode.kind) {
    case 'INITIAL':
      return
    case 'PIN_USER_MSG': {
      const el = _pinnedUserEl(scrollEl, mode.cid)
      if (el) scrollEl.scrollTop = Math.max(0, el.offsetTop - PIN_OFFSET)
      return
    }
    case 'FOLLOW_BOTTOM':
      // Follow the bottom of REAL conversation content, not the reservable
      // spacer below it. The spacer exists so the latest user row can be
      // lifted to the top; treating that blank reservation as content made a
      // short restored chat open on an empty viewport. Long content normally
      // has a zero-height spacer, so this is identical to the usual bottom
      // follow there.
      {
        const spacerH = scrollEl.querySelector('.spacer-dynamic')?.offsetHeight || 0
        const realContentH = scrollEl.scrollHeight - spacerH
        if (realContentH > scrollEl.clientHeight + 4) {
          scrollEl.scrollTop = Math.max(0, realContentH - scrollEl.clientHeight)
        }
      }
      return
    case 'ANCHOR_AT': {
      const sel = `[data-key="${(typeof CSS !== 'undefined' && CSS.escape)
        ? CSS.escape(mode.key) : mode.key}"]`
      const el = scrollEl.querySelector(sel)
      if (el) scrollEl.scrollTop = Math.max(0, el.offsetTop - mode.offset)
      return
    }
  }
}

export function _pinReapplyNeeded(scrollEl, mode, lastPinTop) {
  if (!scrollEl || mode?.kind !== 'PIN_USER_MSG') return false
  const el = _pinnedUserEl(scrollEl, mode.cid)
  if (!el) return false
  const target = Math.max(0, el.offsetTop - PIN_OFFSET)
  const maxScrollTop = scrollEl.scrollHeight - scrollEl.clientHeight
  const targetReachable = maxScrollTop >= target - 1
  const clampedShort = scrollEl.scrollTop < target - 1
    && targetReachable
  const driftedPastTarget = scrollEl.scrollTop > target + 1
    && targetReachable
  return el.offsetTop !== lastPinTop || clampedShort || driftedPastTarget
}


/** Validates a saved ScrollMode against current state. A valid reader anchor
 * is exact. With no resolvable location, show the latest real content once as
 * a settled ANCHOR_AT — never FOLLOW_BOTTOM. */
export function _validateSavedMode(saved, messages, scrollEl) {
  const holdBottom = () => bottomAnchorModeFromScroll(scrollEl) || { kind: 'INITIAL' }
  if (!saved || !saved.kind) return holdBottom()
  if (saved.kind === 'FOLLOW_BOTTOM') return holdBottom()
  if (saved.kind === 'PIN_USER_MSG') {
    // A save without a cid (malformed, or written by pre-cid code) can't
    // resolve a pin target — use the explicit no-location fallback.
    if (saved.cid == null) return holdBottom()
    const lastUserMsg = [...messages].reverse()
      .find(m => m.role === 'user' && !m.hidden)
    // Automatic pin→follow is live-turn state, never restoration state. Strip
    // the armed flag even if a pagehide captured it mid-stream; mount/return
    // must not manufacture tail-follow from saved geometry.
    return cidOf(lastUserMsg) === saved.cid
      ? settledPinMode(saved)
      : holdBottom()
  }
  if (saved.kind === 'ANCHOR_AT') {
    const sel = `[data-key="${(typeof CSS !== 'undefined' && CSS.escape)
      ? CSS.escape(saved.key) : saved.key}"]`
    return scrollEl?.querySelector(sel) ? saved : holdBottom()
  }
  return holdBottom()
}


/** Spacer height needed so the latest user message can sit near the
 *  top of the viewport, with the PIN_OFFSET breathing room above it.
 *  The spacer's only job is reserving bottom room — it does NOT touch
 *  scrollTop and it does NOT decide whether a send pins.
 *
 *  Formula:
 *    max(0, viewH + (lastUserMsgTop − PIN_OFFSET) − listH
 *           + PIN_BOTTOM_ROOM).
 *
 *  The (− PIN_OFFSET) must match applyMode's PIN_USER_MSG target so
 *  the target is reachable. PIN_BOTTOM_ROOM is extra reservable room BELOW
 *  the pin, ON TOP of what's needed to reach it. It defaults to 0: the
 *  spacer reserves *exactly* enough for the message to sit at the top, so
 *  maxScrollTop == pinTarget and the row rests with its top flush to the
 *  viewport top — "just enough for the message to be on top", with no extra
 *  blank the reader can scroll into below the last content. (This restores
 *  the pre-cushion behavior; a >0 value re-adds breathing room if the exact
 *  end-of-scroll rest ever feels cramped.)
 *
 *  Reservation is intentionally independent from pinning and component
 *  lifetime. This function always reserves enough bottom room for the latest
 *  visible user message, so leaving/reopening a chat, keyboard open/close, and
 *  later manual scrolls never make that message lose its reachable "top of
 *  screen" position.
 */
const PIN_OFFSET = 4
const PIN_BOTTOM_ROOM = 0
export function _computeSpacerH(scrollEl, listEl, lastUserMsgEl, fullViewH) {
  if (!scrollEl || !listEl) return 0
  if (!lastUserMsgEl) return 0
  const viewH = fullViewH || scrollEl.clientHeight
  const pinTarget = Math.max(0, lastUserMsgEl.offsetTop - PIN_OFFSET)
  return Math.max(0, viewH + pinTarget - listEl.offsetHeight + PIN_BOTTOM_ROOM)
}


// "Near the bottom" tolerance for the submit-time real-content snapshot.
const NEAR_BOTTOM_PX = 50

/** The single submit-time rule used by direct, queued, and steered user rows.
 *  A row moves to the top (PIN_USER_MSG) only when it was the first visible
 *  user message, or the reader was at the real-content tail when submitted.
 *
 *  The dynamic spacer is excluded from that geometry because it is reserved
 *  reply room, not content. This deliberately does not consult ScrollMode when
 *  the DOM exists: mode transitions lag input/layout by a frame, which made an
 *  identical bottom send pin only sometimes. The measured reader position is
 *  the single submit-time authority. ScrollMode is a DOM-less fallback only.
 */
export function shouldPinSend({
  scrollEl,
  mode,
  isFirstUserMsg,
  wasAtContentBottom = null,
}) {
  if (isFirstUserMsg) return true
  if (typeof wasAtContentBottom === 'boolean') return wasAtContentBottom
  if (scrollEl) return isNearContentBottom(scrollEl)
  return mode?.kind === 'FOLLOW_BOTTOM'
}


/** Position-based bottom check that treats the dynamic pin spacer as
 *  phantom room, not real content. Useful for reasoning about whether real
 *  message content is at the tail, but NOT for deciding whether to move the
 *  viewport: the reserved spacer is intentionally scrollable. */
export function isNearContentBottom(scrollEl, threshold = NEAR_BOTTOM_PX) {
  if (!scrollEl) return false
  const spacerH = scrollEl.querySelector('.spacer-dynamic')?.offsetHeight || 0
  const gap = scrollEl.scrollHeight - spacerH - scrollEl.scrollTop - scrollEl.clientHeight
  return gap < threshold
}


/** True scroll-bottom check: includes the dynamic spacer because the spacer is
 *  user-scrollable reserved room. Keyboard preservation and send pinning use
 *  this so "middle of the reserved space" remains a stable reading position
 *  instead of being treated as the bottom. */
export function isNearScrollBottom(scrollEl, threshold = NEAR_BOTTOM_PX) {
  if (!scrollEl) return false
  const gap = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
  return gap < threshold
}


/** visualViewport resize/scroll is a browser viewport clamp, not a user
 *  reading gesture, so it must never CREATE FOLLOW_BOTTOM or retire a valid
 *  PIN_USER_MSG. Existing follow intent may survive while it remains at the
 *  tail; an ordinary hold freezes to the current anchor when possible.
 *
 *  PIN_USER_MSG must keep its identity through the whole keyboard cycle. The
 *  open keyboard deliberately leaves the permanent full-height reservation in
 *  place, so the pinned scrollTop is no longer at the physical bottom while
 *  the viewport is short. If keyboard-close used that temporary geometry to
 *  demote PIN_USER_MSG to ANCHOR_AT, a short reply's terminal live-to-settled
 *  height change could clamp scrollTop with no pin left to restore it. Only the
 *  gesture-gated scroll handler may retire a pin: a real reader scroll stamps
 *  FOLLOW_BOTTOM or ANCHOR_AT before any viewport event sees the mode. */
export function modeForViewportChange(mode, wasNearScrollBottom, anchorMode = null) {
  if (mode?.kind === 'PIN_USER_MSG') return mode
  if (mode?.kind === 'FOLLOW_BOTTOM') {
    return wasNearScrollBottom ? mode : (anchorMode || mode)
  }
  return mode
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


/** Resolve a reader-owned scroll that reaches the physical bottom.
 *
 * The physical bottom sits AFTER the dynamic reservation. While that spacer
 * still exists, reaching it means the latest user row is at its exact pin
 * target; it does NOT mean the reader asked to follow the real-content tail
 * above the spacer. Conflating those two bottoms made the next ResizeObserver
 * tick jump back to the response and follow every token immediately.
 *
 * During a live turn, reaching the reserved bottom (re-)arms the ordinary
 * spacer-exhaustion handoff. In an idle chat it is a settled pin, so a later
 * image/font/layout change cannot manufacture live-follow intent.
 */
export function modeAfterReaderReachesBottom({
  mode,
  spacerH,
  turnRunning,
  lastUserCid,
}) {
  if (spacerH > 1 && lastUserCid != null) {
    if (mode?.kind === 'PIN_USER_MSG'
        && mode.cid === lastUserCid
        && (!!mode.followWhenFilled || !turnRunning)) {
      return mode
    }
    return {
      kind: 'PIN_USER_MSG',
      cid: lastUserCid,
      ...(turnRunning ? { followWhenFilled: true } : {}),
    }
  }
  return { kind: 'FOLLOW_BOTTOM' }
}


/** Layout observers may own scrollTop only outside the gesture-intent window.
 * Input events precede the browser's first `scroll` event; without this gate,
 * a streaming ResizeObserver can re-pin/follow in that gap and throw the
 * reader back before onScroll has a chance to stamp ANCHOR_AT. */
export function layoutMayOwnScroll(gestureWindowUntil, now) {
  return now >= gestureWindowUntil
}


/** Return a retry delay only after the first scroll has converted reader
 * ownership into a finite momentum window. Infinity is an event handoff, not
 * a timer duration (browsers clamp an infinite setTimeout unpredictably). */
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


/** Wheel and keyboard input have no pointer/touch release event. Their first
 * rendering frame is the deterministic no-scroll release: a real scroll is
 * dispatched before rAF and cancels it; an edge/no-op gesture resumes layout
 * immediately instead of holding a live chat for the two-second dead-man. */
export function readerInputNeedsFrameRelease(type) {
  return type === 'wheel' || type === 'keydown'
}


/** Foreground return (visibilitychange/pageshow/online) is not a reading
 *  gesture. Freeze the exact visible anchor even when the chat was following
 *  before it left: content may have grown while inactive, and returning must
 *  never jump to that newer tail. Manual scrolling to the bottom re-enables
 *  FOLLOW_BOTTOM afterward. */
export function modeForForegroundReturn(scrollEl) {
  if (!scrollEl) return null
  return anchorModeFromScroll(scrollEl)
}


/** Leaving a chat is different from actively watching its tail. Persist the
 *  exact visible reading position — even when that position is currently the
 *  physical bottom — so new content that arrives while the chat is inactive
 *  appears below the restored viewport instead of redefining "bottom" and
 *  yanking the reader to the latest tail. */
export function modeForChatExit(scrollEl) {
  if (!scrollEl) return null
  return anchorModeFromScroll(scrollEl)
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


/** True once every image frame present during entry has either decoded or
 * failed. The frame is rendered before its short-lived media URL resolves, so
 * a frame without an <img> is still pending. */
export function mountMediaSettled(scrollEl) {
  if (!scrollEl?.querySelectorAll) return true
  for (const frame of scrollEl.querySelectorAll('.md-image-frame')) {
    const img = frame.querySelector?.('img')
    if (!img || !img.complete) return false
  }
  return true
}


/**
 * Hook that owns the chat scroll subsystem.
 *
 * The `modeRef.current` value is a tagged union — every possible
 * shape:
 *
 *   {kind: 'INITIAL'}
 *     Pre-restore default. applyMode is a no-op in this state.
 *     Set once on mount before the saved mode is read from
 *     sessionStorage. Also re-entered when the layout effect sees
 *     a new chatId (defensive — key={chatId} normally remounts).
 *
 *   {kind: 'PIN_USER_MSG', cid: string, followWhenFilled?: boolean}
 *     Pin the user message with the given stable `cid` (matched via
 *     `data-cid`) to the top of the viewport (PIN_OFFSET=4 px of
 *     breathing room). Set only for the first visible user row or a
 *     later send submitted at the real-content tail; applyMode scrolls to
 *     `userMsgEl.offsetTop - PIN_OFFSET`. Pinning enters hold while the reply
 *     consumes the reservation; an armed live pin hands off to FOLLOW_BOTTOM
 *     only when that reservation reaches zero.
 *
 *   {kind: 'FOLLOW_BOTTOM'}
 *     Sticky-bottom for streaming. applyMode sets scrollTop =
 *     scrollHeight (only if content actually overflows). Engaged
 *     when the reader reaches the physical bottom, or when an armed live
 *     pin consumes its reservation; lost when the reader scrolls up.
 *
 *   {kind: 'ANCHOR_AT', key: string, offset: number}
 *     Anchored at a specific message (`data-key="<key>"`) with
 *     `offset` pixels above the viewport top. Set when the user
 *     scrolls to a non-bottom position and when lifecycle restoration
 *     freezes the current position. A missing saved anchor degrades to
 *     the current visible hold anchor, never FOLLOW_BOTTOM.
 *
 * The caller is expected to:
 *   - Treat `modeRef` as read-only snapshot state. Route send, queue,
 *     pagination, and foreground lifecycle events through the semantic
 *     controller methods returned by this hook.
 *   - Read `gestureWindowUntilRef.current` in any custom scroll
 *     handlers (e.g., pagination triggers) to gate on user intent
 *   - Apply `revealed` as `style={revealed ? undefined : {visibility:
 *     'hidden'}}` on the scroll container.
 *
 * @param {object} args
 * @param {string} args.chatId
 * @param {React.RefObject<HTMLElement>} args.scrollRef
 *   The `.chat__scroll` container ref.
 * @param {React.RefObject<HTMLElement>} args.spacerRef
 *   The dynamic spacer at the bottom of `.chat__list`.
 * @param {React.RefObject<HTMLElement>} args.lastUserMsgRef
 *   The most recent visible user message element.
 * @param {Array<object>} args.messages
 *   Persisted message list (drives effect re-runs).
 * @param {React.MutableRefObject<Array<object>>} args.messagesRef
 *   Synchronous mirror for restore-time anchor validation.
 * @param {number} args.pendingMessagesLength
 *   Count of queued messages (drives effect re-runs when the tray
 *   shows/hides because the tray's margin shrinks the spacer math).
 * @param {React.MutableRefObject<boolean>} args.loadingOlderRef
 *   When true, scroll events from pagination shouldn't mutate mode.
 * @param {boolean} args.turnRunning
 *   Whether a live turn can consume an existing reservation. Used only when
 *   the reader reaches the physical bottom while spacer room remains.
 * @param {boolean} args.initialEntryCanReveal
 *   Whether entry has a trustworthy idle cache or settled server history.
 * @param {boolean} args.initialEntrySettled
 *   Whether authoritative history and any first mount catch-up have settled.
 *
 * @returns {{
 *   modeRef: React.MutableRefObject<
 *     | {kind: 'INITIAL'}
 *     | {kind: 'PIN_USER_MSG', cid: string, followWhenFilled?: boolean}
 *     | {kind: 'FOLLOW_BOTTOM'}
 *     | {kind: 'ANCHOR_AT', key: string, offset: number}
 *   >,
 *   gestureWindowUntilRef: React.MutableRefObject<number>,
 *   userScrollIntentVersionRef: React.MutableRefObject<number>,
 *   revealed: boolean,
 *   anchorPagination: (key: string, offset: number) => void,
 *   armSentMessage: (event: object) => void,
 *   closePreSendGestureWindow: () => void,
 *   freezeForegroundReturn: () => void,
 *   freezeQueuedSubmission: () => void,
 *   settleNonPin: (event?: object) => void,
 *   settleStreamingPin: () => void,
 * }}
 */
export default function useScrollMode({
  chatId,
  scrollRef,
  spacerRef,
  lastUserMsgRef,
  messages,
  messagesRef,
  pendingMessagesLength,
  loadingOlderRef,
  turnRunning,
  initialEntryCanReveal,
  initialEntrySettled,
}) {
  const [revealed, setRevealed] = useState(false)
  // Synchronous mirror of `revealed` for reapplyActiveMode, which is called
  // from a ChatView layout effect (a closure that may pre-date the reveal
  // flip). Set inline at every setRevealed(true) so the read is never stale.
  const revealedRef = useRef(false)
  const modeRef = useRef({ kind: 'INITIAL' })
  const modeChatIdRef = useRef(null)
  // False only when mount had no deliberate reader location and therefore
  // used the automatic latest-message fallback. Passive lifecycle/viewport
  // changes must not promote that fallback into a saved reading position.
  const readerLocationExplicitRef = useRef(false)
  const gestureWindowUntilRef = useRef(0)
  const pendingGestureTimerRef = useRef(0)
  const pendingGestureReleaseRafRef = useRef(0)
  const gestureSequenceRef = useRef(0)
  const resumeLayoutAfterGestureRef = useRef(null)
  // Monotonic counter for actual user scroll intent. Send/steer code captures
  // this at submit time and honors a delayed pin only if the user did not
  // scroll after submitting. Programmatic applyMode scrolls must not increment
  // it. Input only opens the pre-scroll ownership gate; the counter is bumped
  // by an actual gesture-driven scroll event, so tapping a disclosure cannot
  // accidentally cancel a queued/steered send's pin intent.
  const userScrollIntentVersionRef = useRef(0)
  const fullViewHRef = useRef(0)
  // Lives outside the layout effect so it survives StrictMode's
  // double-invoke in dev (and any future effect re-run). If this were
  // a local `let` inside the effect, the second invoke would reset it
  // to null and `maybeApplyMode()` would re-write scrollTop with the
  // same mode it already applied, visibly snapping the viewport.
  const lastAppliedModeRef = useRef(null)
  // The pinned message's offsetTop at the last PIN_USER_MSG apply. The RO
  // re-pins when this shifts (content ABOVE the message grew — an image
  // finished loading, an error/question card rendered), which the identity
  // gate above otherwise misses. Stays null when no pin is active.
  const lastPinTopRef = useRef(null)
  // Tracks physical tail position independently from modeRef. A stale
  // PIN_USER_MSG can survive after the reader manually returns to the bottom;
  // when the keyboard opens, visualViewport fires AFTER the viewport has
  // already changed, so we need the last known pre-change tail snapshot.
  const nearScrollBottomRef = useRef(false)
  // Keep reader events wired to the latest run state without rebuilding every
  // observer/listener whenever the boolean changes.
  const turnRunningRef = useRef(!!turnRunning)
  turnRunningRef.current = !!turnRunning
  const initialEntryCanRevealRef = useRef(!!initialEntryCanReveal)
  initialEntryCanRevealRef.current = !!initialEntryCanReveal
  const initialEntrySettledRef = useRef(!!initialEntrySettled)
  initialEntrySettledRef.current = !!initialEntrySettled
  // A normal reveal ends entry stabilization. A forced safety-cap reveal keeps
  // the saved anchor under layout ownership until history/catch-up/media settle.
  const mountStabilizingRef = useRef(true)
  const forceRevealRef = useRef(null)

  // Absolute reveal deadline for this mounted chat. This deliberately lives
  // outside the messages-dependent layout effect below: tool-rich turns can
  // re-run that effect continuously, and clearing/restarting its local safety
  // timer kept the ENTIRE transcript visibility:hidden indefinitely.
  useLayoutEffect(() => {
    revealedRef.current = false
    mountStabilizingRef.current = true
    setRevealed(false)
    const deadline = setTimeout(() => {
      if (revealedRef.current) return
      if (forceRevealRef.current) forceRevealRef.current()
      else {
        // Defensive fallback for a mount whose scroll DOM never materialized.
        revealedRef.current = true
        setRevealed(true)
      }
    }, REVEAL_CAP_MS)
    return () => clearTimeout(deadline)
  }, [chatId])

  const recordTrace = useCallback((bucket, event, {
    from = null,
    to = null,
    scrollEl = scrollRef.current,
  } = {}) => {
    _appendScrollTrace(bucket, {
      at: Math.round(typeof performance !== 'undefined' ? performance.now() : 0),
      chatId: String(chatId),
      event,
      ...(from ? { from: _scrollModeForDiagnostics(from) } : {}),
      ...(to ? { to: _scrollModeForDiagnostics(to) } : {}),
      geometry: _scrollGeometryForDiagnostics(scrollEl),
    })
  }, [chatId, scrollRef])

  // The sole mode-mutation funnel. ChatView emits semantic lifecycle events
  // through the methods returned by this hook; layout and reader paths below
  // use the same transition function, so mode ownership cannot drift across
  // a collection of direct `modeRef.current = ...` writes.
  const transitionMode = useCallback((nextMode, event) => {
    if (!nextMode) return modeRef.current
    const previousMode = modeRef.current
    if (nextMode === previousMode) return previousMode
    modeRef.current = nextMode
    recordTrace('transitions', event, {
      from: previousMode,
      to: nextMode,
    })
    return nextMode
  }, [recordTrace])

  // The sole automatic scrollTop funnel inside the controller. `applyMode`
  // remains exported as a pure executor for unit tests, but live code routes
  // every mode-owned write through here and records only writes that actually
  // moved the viewport.
  const writeMode = useCallback((scrollEl, mode, event) => {
    if (!scrollEl || !mode) return
    const before = scrollEl.scrollTop
    applyMode(scrollEl, mode)
    if (Math.abs(scrollEl.scrollTop - before) > 0.5) {
      recordTrace('writes', event, {
        from: mode,
        to: mode,
        scrollEl,
      })
    }
  }, [recordTrace])

  const persistMode = useCallback(({ freezeToCurrentPosition = false } = {}) => {
    try {
      if (!readerLocationExplicitRef.current) {
        delete _scrollModes[chatId]
        sessionStorage.setItem('chat-mode', JSON.stringify(_scrollModes))
        return
      }
      const mode = freezeToCurrentPosition
        ? (modeForChatExit(scrollRef.current) || modeRef.current)
        : modeRef.current
      if (mode && mode.kind !== 'INITIAL') {
        if (freezeToCurrentPosition) {
          transitionMode(mode, 'lifecycle:chat-exit')
        }
        _scrollModes[chatId] = mode
        sessionStorage.setItem('chat-mode', JSON.stringify(_scrollModes))
      }
    } catch {}
  }, [chatId, scrollRef, transitionMode])

  const settleNonPin = useCallback(({
    retireFollow = false,
    event = 'send:hold-current',
  } = {}) => {
    readerLocationExplicitRef.current = true
    const kind = modeRef.current?.kind
    if (kind !== 'PIN_USER_MSG'
        && !(retireFollow && kind === 'FOLLOW_BOTTOM')) {
      return modeRef.current
    }
    const anchor = anchorModeFromScroll(scrollRef.current)
    return anchor ? transitionMode(anchor, event) : modeRef.current
  }, [scrollRef, transitionMode])

  const armSentMessage = useCallback(({
    cid,
    willPin,
    intentCurrent = true,
  }) => {
    readerLocationExplicitRef.current = true
    if (!intentCurrent) {
      return settleNonPin({
        retireFollow: true,
        event: 'send:reader-overrode-delayed-pin',
      })
    }
    if (willPin && cid != null) {
      return transitionMode({
        kind: 'PIN_USER_MSG',
        cid,
        followWhenFilled: true,
      }, 'send:pin-user-message')
    }
    return settleNonPin({
      retireFollow: true,
      event: 'send:hold-current',
    })
  }, [settleNonPin, transitionMode])

  const freezeQueuedSubmission = useCallback(() => {
    readerLocationExplicitRef.current = true
    return transitionMode(
      modeForQueuedSubmission(scrollRef.current, modeRef.current),
      'send:queue-freeze',
    )
  }, [scrollRef, transitionMode])

  const anchorPagination = useCallback((key, offset) => {
    if (!key) return modeRef.current
    readerLocationExplicitRef.current = true
    return transitionMode(
      { kind: 'ANCHOR_AT', key, offset },
      'reader:paginate-anchor',
    )
  }, [transitionMode])

  const freezeForegroundReturn = useCallback(() => {
    const nextMode = modeForForegroundReturn(scrollRef.current)
    return nextMode
      ? transitionMode(nextMode, 'lifecycle:foreground-return')
      : modeRef.current
  }, [scrollRef, transitionMode])

  const closePreSendGestureWindow = useCallback(() => {
    gestureSequenceRef.current += 1
    gestureWindowUntilRef.current = 0
    clearTimeout(pendingGestureTimerRef.current)
    pendingGestureTimerRef.current = 0
    cancelAnimationFrame(pendingGestureReleaseRafRef.current)
    pendingGestureReleaseRafRef.current = 0
  }, [])

  useLayoutEffect(() => () => {
    clearTimeout(pendingGestureTimerRef.current)
    cancelAnimationFrame(pendingGestureReleaseRafRef.current)
  }, [])

  // Persist mode on every chatId change so the next mount restores.
  // (Layout effect can't easily handle persistence because it runs
  // on every messages change; cleanup is only fired on chatId change.)
  useLayoutEffect(() => {
    return () => persistMode({ freezeToCurrentPosition: true })
  }, [chatId])

  // A hard shell refresh/page background does not reliably run React's
  // cleanup after the human manually scrolls. Persist the current mode on the
  // browser lifecycle events too, so reload returns to the last reading
  // position rather than an older mode saved during the last message change.
  useLayoutEffect(() => {
    if (typeof window === 'undefined') return
    const onPageLeaving = () => persistMode({ freezeToCurrentPosition: true })
    const onVisibilityChange = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        persistMode({ freezeToCurrentPosition: true })
      }
    }
    window.addEventListener('pagehide', onPageLeaving)
    window.addEventListener('beforeunload', onPageLeaving)
    window.addEventListener(BEFORE_SHELL_RELOAD_EVENT, onPageLeaving)
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      window.removeEventListener('pagehide', onPageLeaving)
      window.removeEventListener('beforeunload', onPageLeaving)
      window.removeEventListener(BEFORE_SHELL_RELOAD_EVENT, onPageLeaving)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [chatId])

  // Single layout effect: spacer sizing, automatic scroll writes,
  // ResizeObserver layout updates, user-gesture detection, geometry-based
  // scroll transitions, and mobile keyboard tracking via visualViewport.
  // Re-runs on messages / pendingMessages / chatId changes.
  useLayoutEffect(() => {
    const scrollEl = scrollRef.current
    const spacerEl = spacerRef.current
    if (!scrollEl || !spacerEl) return

    if (scrollEl.clientHeight > fullViewHRef.current) {
      fullViewHRef.current = scrollEl.clientHeight
    }

    const listEl = scrollEl.querySelector('.chat__list')
    if (!listEl) return

    // Reset modeRef when the layout effect sees a NEW chatId.
    // Defensive: today Shell uses key={chatId} so this only fires
    // on mount; if that key is ever removed, in-place chat switches
    // won't inherit stale modes from the previous chat.
    if (modeChatIdRef.current !== chatId) {
      modeChatIdRef.current = chatId
      transitionMode({ kind: 'INITIAL' }, 'lifecycle:chat-change')
    }

    // Restore mode for this chat if persisted (mount-restore path).
    if (modeRef.current.kind === 'INITIAL') {
      const saved = _scrollModes[chatId]
      const restored = _validateSavedMode(saved, messagesRef.current, scrollEl)
      readerLocationExplicitRef.current = !!saved
        && restored?.kind !== 'INITIAL'
        && !restored?.defaultTail
      transitionMode(
        restored,
        'lifecycle:restore',
      )
    }
    nearScrollBottomRef.current = isNearScrollBottom(scrollEl)

    // Identity check: apply the mode ONLY when transitionMode changed the
    // current object since the last apply. Semantic events always supply a
    // fresh object when they intend a transition, so === identity is the
    // right signal.
    // Steady-state streaming (mode unchanged) won't re-pin even as
    // the layout settles around tool-block status flips, KaTeX,
    // highlight.js, and markdown re-wrap — that's the bug from
    // May 2026 where scrollTop drifted with userMsg.offsetTop. The
    // last-applied identity lives on `lastAppliedModeRef` (declared
    // above) so it survives the layout effect re-running, including
    // React 19 StrictMode's dev-time double-invoke.

    const layoutOwnsScroll = () => layoutMayOwnScroll(
      gestureWindowUntilRef.current,
      performance.now(),
    )
    let deferredGestureLayoutTimer = 0
    let deferredGestureLayoutPending = false
    const deferLayoutUntilReaderYields = () => {
      clearTimeout(deferredGestureLayoutTimer)
      deferredGestureLayoutPending = true
      const ownershipUntil = gestureWindowUntilRef.current
      // Infinity means input has arrived but its first scroll has not. There
      // is deliberately no guessed retry delay in that phase: onScroll, the
      // no-scroll release, or the next effect instance resumes this pass.
      const delay = gestureLayoutRetryDelay(ownershipUntil, performance.now())
      if (delay == null) return
      deferredGestureLayoutTimer = setTimeout(() => {
        deferredGestureLayoutPending = false
        // The observer pass that noticed the geometry change deliberately
        // yielded to reader input. Once ownership returns, replay that missed
        // write even when the semantic mode object itself did not change (the
        // common case for FOLLOW_BOTTOM while new content arrives). Calling
        // the ordinary identity-gated path here would resize the spacer but
        // leave the viewport behind the live tail forever.
        if (scrollRef.current === scrollEl) syncLayout({ forceApply: true })
      }, delay)
    }
    const resumeLayoutAfterGesture = () => {
      if (!deferredGestureLayoutPending) return
      deferLayoutUntilReaderYields()
    }
    resumeLayoutAfterGestureRef.current = resumeLayoutAfterGesture

    function sizeSpacer() {
      // Keep fullViewHRef authoritative at EVERY spacer sizing, not just at
      // the layout-effect entry and the RO callback start (the other two grow
      // sites). The visualViewport keyboard handler reaches sizeSpacer via
      // syncLayout BEFORE either of those grow steps runs on a keyboard-close,
      // so without this the spacer would be sized from a stale-small fullViewH
      // (the keyboard-open height) even though clientHeight has already grown
      // back. That undersizes the spacer, the pin clamps below its target, and
      // the message lands mid-viewport instead of at the top — the "sent
      // while at the bottom, went to the middle not the top" bug. Grow-only: a
      // keyboard-OPEN shrink is ignored, so Chat-UX constraint #4 (keyboard
      // open/close must not resize the spacer) still holds.
      if (scrollEl.clientHeight > fullViewHRef.current) {
        fullViewHRef.current = scrollEl.clientHeight
      }
      // Fall back to the last user row in the DOM when the React ref is
      // transiently null. On a fresh send the just-sent row's optimistic node
      // is unmounted and its canonical node mounted (during a render swap),
      // and _computeSpacerH returns 0 for a null lastUserMsgEl — collapsing the
      // spacer to 0 mid-swap. Without a re-sizing ResizeObserver tick (there is
      // none until the assistant reply streams in), the spacer stays 0, the pin
      // clamps to scrollTop 0, and the sent message hangs mid-viewport until the
      // reply arrives ("subsequent messages don't get enough space"). The DOM's
      // last user row is the same element the ref points at once it re-attaches,
      // so reserving from it keeps the pin target reachable the instant the row
      // exists. Null only when there is genuinely no user message → spacer 0.
      const lastUserEl = lastUserMsgRef.current || _lastUserRowEl(scrollEl)
      const h = _computeSpacerH(
        scrollEl, listEl, lastUserEl, fullViewHRef.current,
      )
      if (!layoutOwnsScroll()) {
        // Spacer height is itself scroll geometry: shrinking it can make the
        // browser clamp scrollTop even though we never assign scrollTop. Hold
        // both direct and indirect scroll writes for the whole gesture, then
        // run one deterministic layout pass when ownership returns.
        deferLayoutUntilReaderYields()
        return spacerEl.offsetHeight || 0
      }
      spacerEl.style.height = `${h}px`
      // A wheel/touch/key gesture begins before the browser emits its first
      // scroll event. Do not let spacer geometry perform pin→follow in that
      // interval; the gesture-driven onScroll owns the next transition.
      const advanced = modeAfterSpacerResize(modeRef.current, h)
      if (advanced !== modeRef.current) {
        transitionMode(advanced, 'layout:reservation-filled')
        persistMode()
      }
    }

    function maybeApplyMode() {
      if (modeRef.current !== lastAppliedModeRef.current) {
        writeMode(scrollEl, modeRef.current, 'layout:mode-transition')
        lastAppliedModeRef.current = modeRef.current
        // Record the pin baseline (or clear it) so the RO's re-pin-on-shift
        // check below has a reference offsetTop for this pin.
        if (modeRef.current.kind === 'PIN_USER_MSG') {
          const el = _pinnedUserEl(scrollEl, modeRef.current.cid)
          lastPinTopRef.current = el ? el.offsetTop : null
        } else {
          lastPinTopRef.current = null
        }
      }
    }

    function settlePinnedMode() {
      if (!_pinReapplyNeeded(scrollEl, modeRef.current, lastPinTopRef.current)) {
        return
      }
      writeMode(scrollEl, modeRef.current, 'layout:repair-pin')
      const el = _pinnedUserEl(scrollEl, modeRef.current.cid)
      lastPinTopRef.current = el ? el.offsetTop : null
      lastAppliedModeRef.current = modeRef.current
      nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
    }

    // Full sync — size spacer and apply-if-changed. Used at mount, RO, reveal,
    // and visualViewport keyboard changes. Each call sizes the spacer (always
    // needed — the spacer math depends on changing content). Most callers only
    // touch scrollTop on a real mode transition; keyboard resize passes
    // forceApply so the current PIN/FOLLOW/ANCHOR survives the viewport clamp.
    function syncLayout({ forceApply = false, viewportChange = false } = {}) {
      const preserveBottom = viewportChange && nearScrollBottomRef.current
      sizeSpacer()
      // Input precedes the browser's first `scroll` event. Every layout entry
      // point shares this gate so streaming, footer reflow, keyboard resize,
      // and catch-up cannot race the reader and throw the viewport elsewhere.
      if (!layoutOwnsScroll()) {
        nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
        return
      }
      if (viewportChange) {
        const anchor = preserveBottom ? null : anchorModeFromScroll(scrollEl)
        transitionMode(
          modeForViewportChange(modeRef.current, preserveBottom, anchor),
          'layout:viewport-change',
        )
        persistMode()
        writeMode(scrollEl, modeRef.current, 'layout:viewport-change')
        lastAppliedModeRef.current = modeRef.current
        if (modeRef.current.kind === 'PIN_USER_MSG') {
          const el = _pinnedUserEl(scrollEl, modeRef.current.cid)
          lastPinTopRef.current = el ? el.offsetTop : null
        } else {
          lastPinTopRef.current = null
        }
      } else if (forceApply) {
        writeMode(scrollEl, modeRef.current, 'layout:forced-reapply')
      } else {
        maybeApplyMode()
      }
      // A send can set PIN_USER_MSG while the dynamic spacer is still at its
      // old height. `sizeSpacer()` above makes the target reachable; this
      // immediate settle pass applies the same pin again if the browser had
      // clamped the first write, instead of waiting for a later RO event that
      // may never fire for the spacer-only height change.
      settlePinnedMode()
      nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
    }
    syncLayout()

    // Reveal only from trusted idle cache or after authoritative history/first
    // catch-up, once current image frames settle and layout stays quiet for
    // 50ms. REVEAL_CAP_MS remains the escape hatch for a request that stalls.
    let revealTimer = 0
    let mountMutationObserver = null
    const entryReady = () => (
      initialEntryCanRevealRef.current && mountMediaSettled(scrollEl)
    )
    const requestRevealOnQuiet = () => {
      clearTimeout(revealTimer)
      if (revealedRef.current && !mountStabilizingRef.current) return
      if (!entryReady()) return
      revealTimer = setTimeout(() => {
        if (scrollRef.current !== scrollEl || !entryReady()) return
        syncLayout()
        revealedRef.current = true
        mountStabilizingRef.current = !initialEntrySettledRef.current
        setRevealed(true)
        if (!mountStabilizingRef.current) mountMutationObserver?.disconnect()
      }, 50)
    }
    const forceReveal = () => {
      if (revealedRef.current || scrollRef.current !== scrollEl) return
      syncLayout()
      mountStabilizingRef.current = true
      revealedRef.current = true
      setRevealed(true)
      requestRevealOnQuiet()
    }
    forceRevealRef.current = forceReveal

    // ResizeObserver — re-runs spacer sizing on content size changes.
    // Re-applies content-tracking modes:
    //   FOLLOW_BOTTOM — every firing, so streaming keeps the user
    //                   glued to the tail.
    //   ANCHOR_AT     — only during the reveal window (before the
    //                   chat becomes visible). Lazy renderers (KaTeX,
    //                   highlight.js, markdown re-wrap) settle in the
    //                   first ~1s and shift the anchor's offsetTop;
    //                   re-anchoring keeps the saved position accurate
    //                   on chat restore. After reveal, re-applying
    //                   ANCHOR_AT would cause the May 2026 mid-stream
    //                   jitter so we stop.
    //   PIN_USER_MSG  — never re-applied; same jitter risk.
    //
    const ro = new ResizeObserver(() => {
      if (scrollEl.clientHeight > fullViewHRef.current) {
        fullViewHRef.current = scrollEl.clientHeight
      }
      sizeSpacer()
      const k = modeRef.current.kind
      const mayWriteScroll = layoutOwnsScroll()
      if (mayWriteScroll && (
        k === 'FOLLOW_BOTTOM'
        || (k === 'ANCHOR_AT'
          && (!revealedRef.current || mountStabilizingRef.current))
      )) {
        writeMode(scrollEl, modeRef.current, k === 'FOLLOW_BOTTOM'
          ? 'layout:follow-live-tail'
          : 'layout:restore-anchor')
        nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
      } else if (k === 'PIN_USER_MSG' && mayWriteScroll) {
        // Re-pin in two cases, both of which leave the message off its
        // intended top position with no user action:
        //
        //   (a) offsetTop SHIFTED since the last apply — content ABOVE the
        //       message grew (a prior turn's image finished loading, an
        //       error/question card rendered). The pin target moved; the
        //       view didn't follow.
        //
        //   (b) scrollTop was CLAMPED below the pin target during a layout
        //       settle — the spacer shrank / scrollHeight dropped between
        //       the initial apply and now, so the browser clamped the
        //       scroll position down, leaving the message a chunk BELOW the
        //       top (the owner-reported "sent it and it only went halfway").
        //       This is the clamp-fix obligation in ARCHITECTURE.md's
        //       "Chat scroll + steer contract" — already honored for
        //       FOLLOW_BOTTOM/ANCHOR_AT, missing here. We only act when the
        //       target is now reachable
        //       (scrollHeight grew back enough); re-applying then lands the
        //       message at the top instead of futilely re-clamping.
        //
        // Neither case fights the user: reader input immediately suspends
        // layout-owned scroll writes, then its first scroll event flips the
        // mode away from PIN_USER_MSG. Streaming content BELOW the message
        // with a tall-enough scrollHeight already keeps the pin satisfied (no
        // clamp, no offsetTop shift) — so this stays a no-op there and does
        // NOT reintroduce the May-2026 re-pin-every-RO-firing jitter.
        // Reachability is measured against the TARGET, not "is there any
        // more room than now". If we gated on `maxScrollTop >= scrollTop`,
        // a layout still growing toward the target (scrollHeight climbing
        // as content streams in below) would re-pin stepwise on every RO
        // firing — clamp to the current max, fire again, clamp a little
        // higher — reintroducing the May-2026 stutter. Gating on the
        // target means we re-pin exactly once, when the settled layout
        // can actually hold the message at the top.
        settlePinnedMode()
      }
      nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
      requestRevealOnQuiet()  // each RO firing pushes the reveal back
    })
    ro.observe(listEl)
    ro.observe(scrollEl)  // catches form-row growth (file chips, queue tray)
    const queuedTrayEl = scrollEl.parentElement?.querySelector('.queued')
    if (queuedTrayEl) ro.observe(queuedTrayEl)
    if (mountStabilizingRef.current && typeof MutationObserver !== 'undefined') {
      mountMutationObserver = new MutationObserver(requestRevealOnQuiet)
      mountMutationObserver.observe(listEl, { childList: true, subtree: true })
    }
    // A frame can gain its token-resolved image without changing outer size.
    scrollEl.addEventListener('load', requestRevealOnQuiet, true)
    scrollEl.addEventListener('error', requestRevealOnQuiet, true)

    // User-gesture detection.
    const releasePendingGesture = (sequence) => {
      if (gestureSequenceRef.current !== sequence
          || gestureWindowUntilRef.current !== Number.POSITIVE_INFINITY) return
      gestureWindowUntilRef.current = 0
      clearTimeout(pendingGestureTimerRef.current)
      pendingGestureTimerRef.current = 0
      resumeLayoutAfterGestureRef.current?.()
    }
    const scheduleNoScrollRelease = () => {
      if (gestureWindowUntilRef.current !== Number.POSITIVE_INFINITY) return
      // Scroll events are delivered in the rendering step before rAF. Yield
      // one frame so a scroll already caused by this gesture can claim the
      // viewport before an input that changed nothing releases it.
      const sequence = gestureSequenceRef.current
      cancelAnimationFrame(pendingGestureReleaseRafRef.current)
      pendingGestureReleaseRafRef.current = requestAnimationFrame(() => {
        pendingGestureReleaseRafRef.current = 0
        releasePendingGesture(sequence)
      })
    }
    const onUserInput = (event) => {
      if (!readerInputMayScroll(event?.type, event?.key)) return
      // Input and its first scroll event are ordered, but not guaranteed to be
      // less than 250ms apart under a busy renderer. Keep layout ownership
      // suspended until that first event actually lands; after it, the normal
      // short window covers momentum/follow-up scroll events. A bounded
      // fallback releases taps/keys that never produce any scroll at all.
      const sequence = gestureSequenceRef.current + 1
      gestureSequenceRef.current = sequence
      gestureWindowUntilRef.current = Number.POSITIVE_INFINITY
      clearTimeout(pendingGestureTimerRef.current)
      cancelAnimationFrame(pendingGestureReleaseRafRef.current)
      pendingGestureReleaseRafRef.current = 0
      pendingGestureTimerRef.current = setTimeout(() => {
        releasePendingGesture(sequence)
      }, PENDING_GESTURE_CAP_MS)
      if (readerInputNeedsFrameRelease(event?.type)) {
        scheduleNoScrollRelease()
      }
    }
    const onGestureEndWithoutScroll = scheduleNoScrollRelease
    scrollEl.addEventListener('pointerdown', onUserInput, { passive: true })
    scrollEl.addEventListener('touchstart', onUserInput, { passive: true })
    // A long touch can pause beyond the initial 250ms before moving. Refresh
    // intent on touchmove so the pre-scroll race stays closed for the whole
    // gesture rather than only quick flicks.
    scrollEl.addEventListener('touchmove', onUserInput, { passive: true })
    scrollEl.addEventListener('wheel', onUserInput, { passive: true })
    scrollEl.addEventListener('keydown', onUserInput, { passive: true })
    scrollEl.addEventListener('pointerup', onGestureEndWithoutScroll, { passive: true })
    scrollEl.addEventListener('pointercancel', onGestureEndWithoutScroll, { passive: true })
    scrollEl.addEventListener('touchend', onGestureEndWithoutScroll, { passive: true })
    scrollEl.addEventListener('touchcancel', onGestureEndWithoutScroll, { passive: true })

    // Scroll handler — only user-driven scrolls mutate mode.
    const onScroll = () => {
      nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
      const userDriven = performance.now() < gestureWindowUntilRef.current
      if (!userDriven) return
      if (gestureWindowUntilRef.current === Number.POSITIVE_INFINITY) {
        clearTimeout(pendingGestureTimerRef.current)
        pendingGestureTimerRef.current = 0
        cancelAnimationFrame(pendingGestureReleaseRafRef.current)
        pendingGestureReleaseRafRef.current = 0
        gestureWindowUntilRef.current = performance.now() + GESTURE_WINDOW_MS
        resumeLayoutAfterGestureRef.current?.()
      }
      if (loadingOlderRef.current) return
      const overflows = scrollEl.scrollHeight > scrollEl.clientHeight + 4
      if (!overflows) return
      userScrollIntentVersionRef.current += 1
      readerLocationExplicitRef.current = true

      // The scroll event's own geometry is the only bottom authority. The old
      // sentinel + debounced IntersectionObserver could report the PREVIOUS
      // position and contradict the viewport during a fast gesture.
      const atBottom = isNearScrollBottom(
        scrollEl,
        PHYSICAL_BOTTOM_EPSILON_PX,
      )

      if (atBottom) {
        const spacerH = spacerEl.offsetHeight || 0
        const lastUserCid = _lastUserRowEl(scrollEl)?.dataset?.cid ?? null
        transitionMode(
          modeAfterReaderReachesBottom({
            mode: modeRef.current,
            spacerH,
            turnRunning: turnRunningRef.current,
            lastUserCid,
          }),
          'reader:physical-bottom',
        )
      } else {
        const anchor = anchorModeFromScroll(scrollEl)
        if (anchor) transitionMode(anchor, 'reader:hold-anchor')
      }
      persistMode()
    }
    scrollEl.addEventListener('scroll', onScroll, { passive: true })

    // Mobile keyboard via visualViewport.
    let vvHandler = null
    if (typeof window !== 'undefined' && window.visualViewport) {
      vvHandler = () => syncLayout({ forceApply: true, viewportChange: true })
      window.visualViewport.addEventListener('resize', vvHandler)
      window.visualViewport.addEventListener('scroll', vvHandler)
    }

    // Hide-then-reveal: kick off the quiet-debounce path immediately
    // (reveals ~50ms after the last RO firing, smoothing out
    // late-settling renderers like markdown/KaTeX/question cards).
    // The absolute reveal deadline is owned by the chatId-only effect above,
    // so message and tool churn cannot reset it.
    if (!revealedRef.current || mountStabilizingRef.current) {
      requestRevealOnQuiet()
    }

    return () => {
      clearTimeout(revealTimer)
      clearTimeout(deferredGestureLayoutTimer)
      if (resumeLayoutAfterGestureRef.current === resumeLayoutAfterGesture) {
        resumeLayoutAfterGestureRef.current = null
      }
      mountMutationObserver?.disconnect()
      ro.disconnect()
      scrollEl.removeEventListener('scroll', onScroll)
      scrollEl.removeEventListener('pointerdown', onUserInput)
      scrollEl.removeEventListener('touchstart', onUserInput)
      scrollEl.removeEventListener('touchmove', onUserInput)
      scrollEl.removeEventListener('wheel', onUserInput)
      scrollEl.removeEventListener('keydown', onUserInput)
      scrollEl.removeEventListener('pointerup', onGestureEndWithoutScroll)
      scrollEl.removeEventListener('pointercancel', onGestureEndWithoutScroll)
      scrollEl.removeEventListener('touchend', onGestureEndWithoutScroll)
      scrollEl.removeEventListener('touchcancel', onGestureEndWithoutScroll)
      scrollEl.removeEventListener('load', requestRevealOnQuiet, true)
      scrollEl.removeEventListener('error', requestRevealOnQuiet, true)
      if (forceRevealRef.current === forceReveal) forceRevealRef.current = null
      if (vvHandler && typeof window !== 'undefined' && window.visualViewport) {
        window.visualViewport.removeEventListener('resize', vvHandler)
        window.visualViewport.removeEventListener('scroll', vvHandler)
      }
    }
  }, [
    messages,
    pendingMessagesLength,
    chatId,
    initialEntryCanReveal,
    initialEntrySettled,
  ])

  // Re-hold the reading position after an atomic catch-up commit lands
  // post-reveal (contract v2 item 2, lever 3 — cloak the commit). The in-place
  // reconcile keeps DOM identity but can still re-settle heights, so a real
  // reconnect (Path B) or a Path-A commit after the reveal cap must not shift
  // what the reader was looking at. Before reveal, hide-then-reveal already owns
  // the position, so this no-ops; a quick-wake kept socket produces no commit,
  // so the caller never invokes it. Mirrors the RO's content-tracking re-apply
  // (FOLLOW_BOTTOM/ANCHOR_AT only — PIN_USER_MSG settles via its own RO branch).
  const reapplyActiveMode = useCallback(() => {
    if (!revealedRef.current) return
    const scrollEl = scrollRef.current
    if (!scrollEl) return
    if (!layoutMayOwnScroll(
      gestureWindowUntilRef.current,
      performance.now(),
    )) return
    const k = modeRef.current.kind
    if (k === 'FOLLOW_BOTTOM' || k === 'ANCHOR_AT') {
      writeMode(scrollEl, modeRef.current, 'lifecycle:catch-up-reapply')
    }
  }, [scrollRef, writeMode])

  // Terminal stream promotion and final buffered text can land in separate
  // React/browser phases. Observe committed geometry until two consecutive
  // animation frames agree; then either honor a filled reservation or disarm
  // a genuinely short reply. This is a layout-stability handshake, not a
  // guessed timeout. It replaces the one-rAF check that could retire
  // `followWhenFilled` just before the final text commit shrank the spacer to
  // zero, leaving a long completed reply stranded below a still-pinned prompt.
  const settleStreamingPin = useCallback(() => {
    const scrollEl = scrollRef.current
    const terminalMode = modeRef.current
    if (!scrollEl
        || terminalMode?.kind !== 'PIN_USER_MSG'
        || !terminalMode.followWhenFilled) {
      return
    }
    const terminalCid = terminalMode.cid
    let previousSignature = null
    let stableFrames = 0

    const inspectCommittedLayout = () => {
      if (scrollRef.current !== scrollEl) return
      const mode = modeRef.current
      // A newer send has its own pin lifecycle. A reader gesture may also have
      // retired this terminal pin; neither may be settled by the old turn.
      if (mode?.kind !== 'PIN_USER_MSG'
          || !mode.followWhenFilled
          || mode.cid !== terminalCid) {
        return
      }
      const mayWriteScroll = layoutMayOwnScroll(
        gestureWindowUntilRef.current,
        performance.now(),
      )
      if (!mayWriteScroll) {
        // Terminal promotion can land between input and the first scroll
        // event. The reader wins that race: freeze what is visible and retire
        // the live handoff without issuing a final scroll write.
        transitionMode(
          anchorModeFromScroll(scrollEl) || settledPinMode(mode),
          'terminal:reader-owns',
        )
        persistMode()
        return
      }

      const listEl = scrollEl.querySelector('.chat__list')
      const spacerEl = spacerRef.current
      const lastUserEl = lastUserMsgRef.current || _lastUserRowEl(scrollEl)
      if (!listEl || !spacerEl || !lastUserEl) {
        transitionMode(settledPinMode(mode), 'terminal:missing-layout-settle')
        persistMode()
        return
      }

      const spacerH = _computeSpacerH(
        scrollEl, listEl, lastUserEl, fullViewHRef.current,
      )
      const signature = [
        Math.round(listEl.offsetHeight),
        Math.round(lastUserEl.offsetTop),
        Math.round(scrollEl.clientHeight),
        Math.round(spacerH),
      ].join(':')
      stableFrames = signature === previousSignature ? stableFrames + 1 : 0
      previousSignature = signature

      const nextMode = modeAfterTerminalLayout(
        mode,
        spacerH,
        stableFrames >= 1,
      )
      if (nextMode === mode) {
        requestAnimationFrame(inspectCommittedLayout)
        return
      }

      // Keep styled geometry and the decision in the same frame. The main RO
      // normally wrote this value already; assigning the same value is a no-op.
      spacerEl.style.height = `${spacerH}px`
      transitionMode(nextMode, spacerH <= 1
        ? 'terminal:reservation-filled'
        : 'terminal:short-reply-settle')
      writeMode(scrollEl, modeRef.current, 'terminal:settle-layout')
      persistMode()
    }

    requestAnimationFrame(inspectCommittedLayout)
  }, [
    chatId,
    lastUserMsgRef,
    persistMode,
    scrollRef,
    spacerRef,
    transitionMode,
    writeMode,
  ])

  return {
    modeRef,
    gestureWindowUntilRef,
    userScrollIntentVersionRef,
    revealed,
    anchorPagination,
    armSentMessage,
    closePreSendGestureWindow,
    freezeForegroundReturn,
    freezeQueuedSubmission,
    reapplyActiveMode,
    settleNonPin,
    settleStreamingPin,
  }
}
