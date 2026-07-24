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
 * A live pin leaves FOLLOW_BOTTOM while its dynamic spacer is being consumed,
 * then hands off to FOLLOW_BOTTOM exactly when that reservation reaches zero.
 * A short reply never reaches the handoff and remains pinned after settle.
 * The dynamic spacer belongs exclusively to the latest visible user row.
 * It is independent of turn completion: short replies keep the remaining
 * room, while reply/tool expansion consumes it and collapse restores it.
 * PIN_USER_MSG may reserve before its row lands so a fresh send can pin in
 * one frame; every other mode reserves only while that latest row is visible.
 * Gesture-driven bottom detection reads the scroll container's geometry in
 * the scroll event itself. There is no second sentinel/observer authority
 * that can lag behind the reader and contradict the current viewport.
 *
 * User-gesture detection: pointerdown/wheel/touchstart/touchmove/keydown hold
 * reader ownership until the first scroll event actually arrives, then keep a
 * 250ms momentum window in which scroll events are user-driven and can
 * transition the mode. Wheel input is released early only when its direction
 * is exactly clamped at the matching edge; elapsed frames cannot prove that an
 * in-range gesture was a no-op under renderer load. Outside that handoff/window,
 * scrolls come from our applyMode or browser clamps and are ignored.
 *
 * See ARCHITECTURE.md "Chat scroll + steer contract" for the full design.
 */

import { useState, useRef, useLayoutEffect, useCallback } from 'react'
import { cidOf, isOwnerUserMessage } from './chatRuntimeState.js'
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
    : { version: 1, transitions: [], writes: [], events: [] }
  const rows = Array.isArray(trace[bucket])
    ? trace[bucket]
    : (trace[bucket] = [])
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


/** Returns the topmost intersecting message, or the last real row while the
 * viewport is inside the dynamic reservation below the transcript.
 *
 * That fallback is load-bearing for LIVE reader ownership: a gesture through
 * reserved room still needs an anchor so streaming/layout work cannot move the
 * viewport underneath the reader. Lifecycle save/restore validates that the
 * anchor intersects real content and normalizes this live-only negative offset
 * to the real transcript tail before persistence. */
function _topmostVisibleMsg(scrollEl) {
  const items = scrollEl.querySelectorAll('.chat__msg[data-key]')
  const top = scrollEl.scrollTop
  const bottom = top + scrollEl.clientHeight
  for (const el of items) {
    const itemBottom = el.offsetTop + el.offsetHeight
    if (itemBottom > top && el.offsetTop < bottom) return el
  }
  return items[items.length - 1] || null
}


/** Snapshot the reader's current scroll position as an ANCHOR_AT mode
 *  (the same {key, offset} the gesture-gated scroll handler stamps when
 *  the user scrolls up). Returns null when there's no scroll element or
 *  no anchorable message.
 *
 *  Why this exists: a non-pinning send must not leave a stale PIN_USER_MSG
 *  behind. The send sites call this to convert a stale PIN into the reader's
 *  actual position. Reservation then follows whether that held viewport still
 *  shows the latest user row; mode alone neither grants nor retires it. */
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


/** Lifecycle anchors must describe visible conversation content. Live scroll
 * handling may temporarily anchor reserved room, but foreground/chat restore
 * must never recreate that blank viewport. */
function _contentAnchorModeFromScroll(scrollEl) {
  if (!scrollEl) return null
  const row = _topmostVisibleMsg(scrollEl)
  if (!row?.dataset?.key) return null
  const mode = {
    kind: 'ANCHOR_AT',
    key: row.dataset.key,
    offset: row.offsetTop - scrollEl.scrollTop,
  }
  return _anchorModeIntersectsContent(row, mode, scrollEl?.clientHeight)
    ? mode
    : null
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
  // Exclude reservation from the anchor calculation. Whether the resulting
  // held viewport qualifies for latest-user room is decided separately.
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


/** Create a settled hold anchor at the true physical scroll tail.
 *
 * This is deliberately different from `bottomAnchorModeFromScroll`, which
 * excludes the dynamic reservation for the automatic no-location restore.
 * An explicit attention-nudge tap asks to see everything after the question
 * or paused card too: composer clearance, any remaining reservation, and the
 * card's primary action. Keep that one-shot navigation as ANCHOR_AT rather
 * than FOLLOW_BOTTOM so revealing a control cannot manufacture live-follow
 * intent for a later answer or resume. Persistence independently rejects an
 * off-content physical anchor, so this live navigation cannot recreate a
 * blank viewport on reload.
 */
export function physicalBottomAnchorModeFromScroll(scrollEl) {
  if (!scrollEl) return null
  const items = scrollEl.querySelectorAll('.chat__msg[data-key]')
  const last = items[items.length - 1]
  const key = last?.dataset?.key
  if (!last || !key) return null
  const targetScrollTop = Math.max(
    0,
    scrollEl.scrollHeight - scrollEl.clientHeight,
  )
  return {
    kind: 'ANCHOR_AT',
    key,
    offset: last.offsetTop - targetScrollTop,
  }
}


/** Freeze a viewport to real conversation content.
 *
 * A reader can begin moving through latest-user reservation. There may be no
 * exact visible row in that region, but the gesture must still retire live
 * follow. Settle at the latest real-content tail; spacer is then recomputed
 * from whether the held viewport still shows the latest user row. */
export function contentHoldModeFromScroll(scrollEl) {
  return _contentAnchorModeFromScroll(scrollEl)
    || bottomAnchorModeFromScroll(scrollEl)
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
      // Mode never owns reservation. Follow the bottom of real content while
      // excluding any latest-user room; visibility sizing remains independent.
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

/** Resolve the row an ANCHOR_AT mode targets: the element whose `data-key`
 *  equals the mode's key. */
function _anchorEl(scrollEl, key) {
  if (!scrollEl || key == null) return null
  const esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(key) : key
  return scrollEl.querySelector(`[data-key="${esc}"]`)
}

/** The defining ANCHOR_AT invariant: its row intersects the viewport encoded
 * by `offset`. Negative offsets are valid while the row remains partially
 * visible; an offset beyond either edge describes layout reservation, not a
 * readable conversation location. */
export function _anchorModeIntersectsContent(row, mode, viewportHeight) {
  const offset = Number(mode?.offset)
  return !!row
    && Number.isFinite(offset)
    && Number.isFinite(viewportHeight)
    && viewportHeight > 0
    && offset < viewportHeight
    && offset > -row.offsetHeight
}

/** The ANCHOR_AT twin of `_pinReapplyNeeded` — the SAME two-case repair. A
 *  settled anchor drifts off its reader-chosen position when either the anchor
 *  element's offsetTop SHIFTED (content grew above it) or scrollTop was CLAMPED
 *  below / past the target and the target is now reachable again. Gating on
 *  those conditions (never "every layout tick") keeps steady-state streaming
 *  below the anchor a no-op, so the post-reveal repair this enables cannot
 *  reintroduce the May-2026 re-apply-every-RO-firing jitter. Background panes
 *  resize routinely once panes exist, which is why the anchor now needs the
 *  clamp-repair PIN already had (design §2 prerequisite). */
export function _anchorReapplyNeeded(scrollEl, mode, lastAnchorTop) {
  if (!scrollEl || mode?.kind !== 'ANCHOR_AT') return false
  const el = _anchorEl(scrollEl, mode.key)
  if (!el) return false
  const target = Math.max(0, el.offsetTop - mode.offset)
  const maxScrollTop = scrollEl.scrollHeight - scrollEl.clientHeight
  const targetReachable = maxScrollTop >= target - 1
  const clampedShort = scrollEl.scrollTop < target - 1 && targetReachable
  const driftedPastTarget = scrollEl.scrollTop > target + 1 && targetReachable
  return el.offsetTop !== lastAnchorTop || clampedShort || driftedPastTarget
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
      .find(isOwnerUserMessage)
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
    const row = scrollEl?.querySelector(sel)
    // A resolvable row is not enough: an old build could persist that row with
    // a huge negative offset while the viewport sat wholly in spacer below it.
    // Enforce the same content-intersection invariant used by spacer sizing,
    // self-healing every off-content restore to the real tail.
    return _anchorModeIntersectsContent(row, saved, scrollEl?.clientHeight)
      ? saved
      : holdBottom()
  }
  return holdBottom()
}


/** Normalize durable reader locations without collapsing live mode state.
 *
 * FOLLOW_BOTTOM and PIN_USER_MSG are useful while this mount is active and
 * are already converted to settled restore modes by `_validateSavedMode` on
 * the next mount. ANCHOR_AT can still carry legacy off-content geometry, so
 * validate that location before every write. */
export function _modeForPersistence(mode, messages, scrollEl) {
  return mode?.kind === 'ANCHOR_AT'
    ? _validateSavedMode(mode, messages, scrollEl)
    : mode
}


function _rowIntersectsViewport(rowEl, scrollTop, viewH) {
  if (!rowEl || !Number.isFinite(scrollTop) || !(viewH > 0)) return false
  const rowTop = rowEl.offsetTop
  const rowHeight = rowEl.offsetHeight
  if (!Number.isFinite(rowTop) || !Number.isFinite(rowHeight)) return false
  return rowTop + rowHeight > scrollTop && rowTop < scrollTop + viewH
}


/** Whether the latest user row owns reservation in the viewport represented by
 *  current geometry/mode. A matching PIN_USER_MSG is allowed to reserve before
 *  the browser can place the fresh row. Other modes must actually show the
 *  latest user row — either now or at the real-content target they are about to
 *  apply. Older user rows never participate because the caller passes only the
 *  DOM tail user row.
 */
function _latestUserOwnsSpacer(scrollEl, listEl, lastUserMsgEl, mode, viewH) {
  const rowCid = lastUserMsgEl?.dataset?.cid
  if (mode?.kind === 'PIN_USER_MSG'
      && rowCid != null
      && String(rowCid) === String(mode.cid)) {
    return true
  }

  let targetScrollTop = null
  if (mode?.kind === 'ANCHOR_AT') {
    const anchorEl = _anchorEl(scrollEl, mode.key)
    if (anchorEl) targetScrollTop = anchorEl.offsetTop - mode.offset
  } else if (mode?.kind === 'FOLLOW_BOTTOM') {
    targetScrollTop = listEl.offsetHeight - scrollEl.clientHeight
  }
  if (targetScrollTop == null) {
    return _rowIntersectsViewport(lastUserMsgEl, scrollEl.scrollTop, viewH)
  }

  // Project against real content only. Spacer cannot make its own owner
  // visible; it may only realize room for a row that the held viewport shows.
  const maxRealScrollTop = Math.max(
    0,
    listEl.offsetHeight - scrollEl.clientHeight,
  )
  const clampedTarget = Math.min(
    maxRealScrollTop,
    Math.max(0, targetScrollTop),
  )
  return _rowIntersectsViewport(lastUserMsgEl, clampedTarget, viewH)
}


/** Spacer height needed so the latest visible user message can sit near the
 *  top of the viewport, with the PIN_OFFSET breathing room above it.
 *
 *  Visibility is the defining invariant. The matching latest user pin may
 *  reserve before placement; every other mode gets room only while its real
 *  viewport contains that latest row. Turn completion does not retire room.
 *  Content growth consumes the exact deficit and content collapse restores it.
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
 *  Once the latest user row leaves the viewport, reservation collapses. An
 *  older visible user row never receives it.
 */
const PIN_OFFSET = 4
const PIN_BOTTOM_ROOM = 0
export function _computeSpacerH(
  scrollEl,
  listEl,
  lastUserMsgEl,
  fullViewH,
  mode = null,
) {
  if (!scrollEl || !listEl || !lastUserMsgEl) return 0
  const viewH = fullViewH || scrollEl.clientHeight
  if (!_latestUserOwnsSpacer(
    scrollEl,
    listEl,
    lastUserMsgEl,
    mode,
    scrollEl.clientHeight || viewH,
  )) return 0
  const pinTarget = Math.max(0, lastUserMsgEl.offsetTop - PIN_OFFSET)
  return Math.max(
    0,
    viewH + pinTarget - listEl.offsetHeight + PIN_BOTTOM_ROOM,
  )
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
 * Positive spacer means the latest user row is visible (or already pinned).
 * Reaching its reserved bottom makes that visible row the explicit pin;
 * an ordinary bottom with no reservation enters FOLLOW_BOTTOM.
 */
export function modeAfterReaderReachesBottom({
  mode,
  spacerH,
  turnRunning,
  lastUserCid,
}) {
  if (spacerH > 1 && lastUserCid != null) {
    if (mode?.kind === 'PIN_USER_MSG' && mode.cid === lastUserCid) {
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


/** Wheel and keyboard input have no pointer/touch release event. Keyboard
 * input keeps the next-frame no-scroll release. A wheel gets that fast release
 * only when its requested direction is exactly clamped at the corresponding
 * edge (or has no vertical delta). A proximity epsilon is not sufficient: a
 * wheel can still move through that final gap, and its compositor scroll can
 * arrive after rAF. For a wheel that can move, the actual scroll event owns the
 * release. */
export function readerInputNeedsFrameRelease(
  type,
  {
    deltaY = 0,
    scrollTop = 0,
    scrollHeight = 0,
    clientHeight = 0,
  } = {},
) {
  if (type === 'keydown') return true
  if (type !== 'wheel') return false
  if (!Number.isFinite(deltaY) || deltaY === 0) return true

  const maxScrollTop = Math.max(0, scrollHeight - clientHeight)
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
 * assistant row and may replace the card's controls immediately. It is not a
 * request to follow the live tail. Freeze the exact visible row/offset before
 * that card-to-stream handoff so neither the control reflow nor resumed output
 * moves the reader. */
export function modeForQuestionSubmission(scrollEl, currentMode) {
  if (!scrollEl) return currentMode
  return anchorModeFromScroll(scrollEl) || currentMode
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
 * @param {() => void} args.syncComposerGeometry
 *   Publishes the current overlaid composer height before the controller reads
 *   list/spacer geometry. Keeping this in the same pre-paint layout pass stops
 *   a later composer measurement from briefly clamping a newly pinned send.
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
 *   Whether a reader-owned move to the latest user's reserved bottom should
 *   arm the ordinary spacer-exhaustion handoff while output is still live.
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
 *   freezeChatExit: () => void,
 *   freezeForegroundReturn: () => void,
 *   freezeQuestionSubmission: () => void,
 *   freezeQueuedSubmission: () => void,
 *   revealConversationTail: () => void,
 *   settleNonPin: (event?: object) => void,
 *   settleStreamingPin: () => void,
 * }}
 */
export default function useScrollMode({
  chatId,
  scrollRef,
  spacerRef,
  lastUserMsgRef,
  syncComposerGeometry,
  messages,
  messagesRef,
  pendingMessagesLength,
  loadingOlderRef,
  turnRunning,
  initialEntryCanReveal,
  initialEntrySettled,
}) {
  const [revealed, setRevealed] = useState(false)
  // A tiny React mirror reruns the layout effect when a semantic transition
  // enters/leaves PIN_USER_MSG before message props necessarily change.
  // modeRef remains the synchronous source of truth; visibility still owns
  // spacer and this state is not a second mode machine.
  const [pinModeActive, setPinModeActive] = useState(false)
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
  // The ANCHOR_AT twin of lastPinTopRef — the anchor element's offsetTop at the
  // last apply, so the post-reveal anchor clamp-repair only fires on a real
  // shift/clamp (design §2). Null when no anchor is active.
  const lastAnchorTopRef = useRef(null)
  // A pane-geometry resize (divider commit, projection/mode flip, rotation)
  // hands its projected CONTENT height here; sizeSpacer consumes it under the
  // reader-ownership gate so the (possibly LOWER) floor rides the same deferred
  // pass as the spacer mutation. Null except in-flight.
  const pendingPaneHeightRef = useRef(null)
  // Set inside the layout effect to the live pane-resize runner; the returned
  // paneResized() forwards to it (null when no scroll DOM is mounted). Mirrors
  // the forceRevealRef / resumeLayoutAfterGestureRef effect-bridge pattern.
  const paneResizeRunRef = useRef(null)
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
    const pinOwnedBefore = previousMode?.kind === 'PIN_USER_MSG'
    const pinOwnedAfter = nextMode?.kind === 'PIN_USER_MSG'
    if (pinOwnedBefore !== pinOwnedAfter) {
      setPinModeActive(pinOwnedAfter)
    }
    const scrollEl = scrollRef.current
    if (scrollEl) scrollEl.dataset.scrollMode = nextMode.kind
    recordTrace('transitions', event, {
      from: previousMode,
      to: nextMode,
    })
    return nextMode
  }, [recordTrace, scrollRef])

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
      const candidate = freezeToCurrentPosition
        ? (modeForChatExit(scrollRef.current) || modeRef.current)
        : modeRef.current
      // One persistence gate for every lifecycle path. Invalid ANCHOR_AT
      // geometry is normalized before it reaches sessionStorage. Live
      // FOLLOW_BOTTOM/PIN_USER_MSG remains observable while mounted; the
      // restore gate settles those modes on the next mount.
      const mode = _modeForPersistence(
        candidate, messagesRef.current, scrollRef.current,
      )
      if (mode && mode.kind !== 'INITIAL') {
        if (freezeToCurrentPosition) {
          transitionMode(mode, 'lifecycle:chat-exit')
        }
        _scrollModes[chatId] = mode
      } else {
        delete _scrollModes[chatId]
      }
      sessionStorage.setItem('chat-mode', JSON.stringify(_scrollModes))
    } catch {}
  }, [chatId, messagesRef, scrollRef, transitionMode])

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
    const anchor = contentHoldModeFromScroll(scrollRef.current)
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

  const freezeQuestionSubmission = useCallback(() => {
    readerLocationExplicitRef.current = true
    return transitionMode(
      modeForQuestionSubmission(scrollRef.current, modeRef.current),
      'send:question-freeze',
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

  // A retained pane becoming hidden must cross the same scroll lifecycle
  // boundary as the old unmounting ChatView. Freeze and persist the exact
  // visible row before Settings (or another full-workspace overlay) can grow
  // the transcript in the background. Keeping this as a controller event
  // avoids exposing persistMode/modeRef mutation to ChatView.
  const freezeChatExit = useCallback(() => {
    persistMode({ freezeToCurrentPosition: true })
  }, [persistMode])

  // A sticky attention nudge (question or paused turn) is an explicit reader
  // request, but not a scroll gesture inside `.chat__scroll`. Route it through
  // the controller anyway:
  // the old `element.scrollIntoView({block:'nearest'})` stopped as soon as the
  // question/Resume card intersected the scroll viewport, even when the
  // absolutely positioned composer still covered its primary action. It also
  // left modeRef describing the old reading position. Anchor the true physical
  // tail in hold mode instead, and close any gesture window from the scroll
  // that exposed the nudge so the resulting programmatic scroll event cannot
  // be mistaken for a second human gesture that enables FOLLOW_BOTTOM.
  const revealConversationTail = useCallback(() => {
    const scrollEl = scrollRef.current
    const nextMode = physicalBottomAnchorModeFromScroll(scrollEl)
    if (!scrollEl || !nextMode) return
    gestureWindowUntilRef.current = 0
    readerLocationExplicitRef.current = true
    transitionMode(nextMode, 'reader:attention-nudge-tail')
    writeMode(scrollEl, nextMode, 'reader:attention-nudge-tail')
    lastAppliedModeRef.current = nextMode
    nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
    persistMode()
  }, [persistMode, scrollRef, transitionMode, writeMode])

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
      // The list's bottom padding is derived from the absolutely-positioned
      // composer height. React commits the emptied composer / new turn footer
      // in the same render as a sent row, but the foot's ResizeObserver runs
      // after paint. If spacer math reads the OLD padding first, the later
      // --composer-h update transiently shortens the scroll range, the browser
      // clamps the fresh pin, and the next controller pass visibly nudges the
      // row upward a second time. Publish the committed foot height here,
      // before ANY list/spacer reads, so reservation + scrollTop land from one
      // geometry snapshot. Respect reader ownership: the CSS-variable write is
      // scroll geometry too and must wait with the spacer during a gesture.
      if (layoutOwnsScroll()) syncComposerGeometry?.()
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
      //
      // The ONE sanctioned lowering of the grow-only floor: a committed pane
      // geometry change (divider/projection/rotation) delivers the layout-
      // derived pane height through pendingPaneHeightRef, which may be SMALLER
      // than the current floor (a narrower/shorter pane). Consume it here, under
      // the same reader-ownership gate as every other write in this pass, before
      // the grow-only clientHeight bump re-raises it if the real pane is taller.
      // The visualViewport keyboard path never sets this ref, so its floor stays
      // strictly grow-only (design §2).
      if (pendingPaneHeightRef.current != null && layoutOwnsScroll()) {
        const ph = pendingPaneHeightRef.current
        if (Number.isFinite(ph) && ph > 0) fullViewHRef.current = ph
        pendingPaneHeightRef.current = null
      }
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
      // exists. `_computeSpacerH` still requires the latest row to be visible
      // at the current/applied viewport, so this fallback cannot grant room to
      // an older row or an unrelated reading location.
      const lastUserEl = _lastUserRowEl(scrollEl) || lastUserMsgRef.current
      const h = _computeSpacerH(
        scrollEl, listEl, lastUserEl, fullViewHRef.current, modeRef.current,
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
        // Record the pin/anchor baseline (or clear both) so the RO's
        // re-apply-on-shift checks below have a reference offsetTop.
        if (modeRef.current.kind === 'PIN_USER_MSG') {
          const el = _pinnedUserEl(scrollEl, modeRef.current.cid)
          lastPinTopRef.current = el ? el.offsetTop : null
          lastAnchorTopRef.current = null
        } else if (modeRef.current.kind === 'ANCHOR_AT') {
          const el = _anchorEl(scrollEl, modeRef.current.key)
          lastAnchorTopRef.current = el ? el.offsetTop : null
          lastPinTopRef.current = null
        } else {
          lastPinTopRef.current = null
          lastAnchorTopRef.current = null
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

    // The ANCHOR_AT twin of settlePinnedMode — the post-reveal clamp-repair.
    // Re-applies only when _anchorReapplyNeeded says the anchor shifted or was
    // clamped (never every firing), so it does not fight the reader or jitter.
    function settleAnchoredMode() {
      if (!_anchorReapplyNeeded(scrollEl, modeRef.current, lastAnchorTopRef.current)) {
        return
      }
      writeMode(scrollEl, modeRef.current, 'layout:repair-anchor')
      const el = _anchorEl(scrollEl, modeRef.current.key)
      lastAnchorTopRef.current = el ? el.offsetTop : null
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

    // The scrollApi.paneResized(projectedHeightPx) contract (design §2,
    // constraint 1). Shell calls this on COMMITTED pane-geometry changes for a
    // MOUNTED chat — divider pointerup, projection/mode flip, pane open/close
    // affecting this chat, rotation — with the pane's new projected CONTENT
    // height (the LAYOUT-derived height, never scrollEl.clientHeight, so a
    // keyboard-shrunk viewport can not stick the floor). It lowers the grow-only
    // floor to that height and re-applies the active mode: FOLLOW_BOTTOM snaps
    // the tail, PIN_USER_MSG re-pins, ANCHOR_AT re-anchors. The whole pass
    // respects the reader-gesture ownership gate (R5): while a reader gesture
    // owns scroll, it DEFERS everything (the floor lowering is applied by
    // sizeSpacer only under the gate) and the reader-yield replay runs it. The
    // visualViewport keyboard path must never call this — divider/projection
    // shrink and keyboard shrink stay structurally separate.
    function runPaneResize(projectedHeightPx) {
      pendingPaneHeightRef.current =
        (Number.isFinite(projectedHeightPx) && projectedHeightPx > 0)
          ? projectedHeightPx
          : pendingPaneHeightRef.current
      if (!layoutOwnsScroll()) {
        // Defer the floor lowering, spacer mutation, and mode re-apply as one
        // replayable pass; the deferred syncLayout({forceApply}) consumes the
        // pending height and re-applies the current mode when ownership yields.
        deferLayoutUntilReaderYields()
        return
      }
      sizeSpacer()
      const k = modeRef.current.kind
      if (k === 'FOLLOW_BOTTOM') {
        writeMode(scrollEl, modeRef.current, 'pane:resize-follow')
        nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
      } else if (k === 'PIN_USER_MSG') {
        settlePinnedMode()
      } else if (k === 'ANCHOR_AT') {
        settleAnchoredMode()
      }
    }
    paneResizeRunRef.current = runPaneResize

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
    //   ANCHOR_AT     — during the reveal window, re-applied every firing
    //                   (lazy renderers — KaTeX, highlight.js, markdown
    //                   re-wrap — settle in the first ~1s and shift the
    //                   anchor's offsetTop; re-anchoring keeps the saved
    //                   position accurate on restore). AFTER reveal it gets the
    //                   same conditional two-case clamp-repair PIN has
    //                   (settleAnchoredMode) — a divider/projection resize can
    //                   clamp a background pane's anchor, so it must be repaired
    //                   — but only on a real shift/clamp, never every firing.
    //   PIN_USER_MSG  — conditional two-case repair only (settlePinnedMode);
    //                   never re-applied unconditionally (jitter risk).
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
      } else if (k === 'ANCHOR_AT' && mayWriteScroll) {
        // POST-REVEAL ANCHOR_AT repair (the pre-reveal case is handled by the
        // first branch above). Background panes resize routinely once panes
        // exist (design §2 prerequisite), and a settled anchor deserves the
        // SAME two-case repair PIN just got: re-apply only when the anchor's
        // offsetTop shifted or scrollTop was clamped and the target is reachable
        // again. _anchorReapplyNeeded gates it exactly like the pin, so
        // steady-state streaming below the anchor stays a no-op — no May-2026
        // jitter, no fight with the reader (whose first scroll flips the mode).
        settleAnchoredMode()
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
      recordTrace('events', 'reader:no-scroll-release', { scrollEl })
      resumeLayoutAfterGestureRef.current?.()
    }
    const scheduleNoScrollRelease = () => {
      if (gestureWindowUntilRef.current !== Number.POSITIVE_INFINITY) return
      // Geometry has already proved this input is clamped at its matching edge
      // (or it is an unknown focus-navigation key). Yield one frame so any
      // synchronous scroll can still claim the viewport before release.
      const sequence = gestureSequenceRef.current
      cancelAnimationFrame(pendingGestureReleaseRafRef.current)
      pendingGestureReleaseRafRef.current = requestAnimationFrame(() => {
        pendingGestureReleaseRafRef.current = 0
        releasePendingGesture(sequence)
      })
    }
    const onUserInput = (event) => {
      const activatesDisclosure = readerInputActivatesDisclosure(
        event?.type,
        event?.key,
        event?.target,
        event?.button,
      )
      if (!activatesDisclosure
          && !readerInputMayScroll(event?.type, event?.key)) return
      recordTrace('events', `reader:input-${event?.type || 'unknown'}`, {
        scrollEl,
      })
      if (activatesDisclosure) {
        // A disclosure tap obeys the mode the reader already chose. FOLLOW_BOTTOM
        // remains the sole tail authority; every other mode latches the visible
        // anchor BEFORE React changes body height. The gesture gate below defers
        // ResizeObserver writes until pointerup, then replays that same policy.
        const nextMode = modeForDisclosureToggle(scrollEl, modeRef.current)
        if (nextMode && nextMode !== modeRef.current) {
          readerLocationExplicitRef.current = true
          transitionMode(nextMode, 'reader:disclosure-toggle')
          persistMode()
        }
      }
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
      if (readerInputNeedsFrameRelease(event?.type, {
        deltaY: event?.deltaY,
        scrollTop: scrollEl.scrollTop,
        scrollHeight: scrollEl.scrollHeight,
        clientHeight: scrollEl.clientHeight,
      })) {
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
      if (!userDriven) {
        recordTrace('events', 'scroll:unowned', { scrollEl })
        return
      }
      recordTrace('events', 'scroll:owned', { scrollEl })
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
        const lastUserEl = _lastUserRowEl(scrollEl) || lastUserMsgRef.current
        const lastUserCid = lastUserEl?.dataset?.cid ?? null
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
        // Moving away from the physical bottom always retires live follow.
        // If the viewport is wholly inside reserved spacer there is no exact
        // row anchor, so settle on the real-content tail instead of leaving a
        // stale FOLLOW_BOTTOM armed for the next content resize.
        const anchor = contentHoldModeFromScroll(scrollEl)
        if (anchor) transitionMode(anchor, 'reader:hold-anchor')
      }
      persistMode()
      // Visibility, not mode, owns reservation. Recompute after the gesture
      // yields so scrolling the latest user row on/off screen changes spacer
      // without mutating geometry during the gesture itself.
      sizeSpacer()
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
      if (paneResizeRunRef.current === runPaneResize) paneResizeRunRef.current = null
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
    pinModeActive,
    syncComposerGeometry,
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
      const lastUserEl = _lastUserRowEl(scrollEl) || lastUserMsgRef.current
      if (!listEl || !spacerEl || !lastUserEl) {
        transitionMode(settledPinMode(mode), 'terminal:missing-layout-settle')
        persistMode()
        return
      }

      const spacerH = _computeSpacerH(
        scrollEl, listEl, lastUserEl, fullViewHRef.current, mode,
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

  // Shell calls this on committed pane-geometry changes for a mounted chat
  // (design §2). A stable identity is required — ChatView wires it to a
  // prop-change effect. Forwards to the live in-effect runner; no-op before the
  // scroll DOM mounts (single-pane chats never call it).
  const paneResized = useCallback((projectedHeightPx) => {
    const run = paneResizeRunRef.current
    if (run) run(projectedHeightPx)
  }, [])

  return {
    modeRef,
    gestureWindowUntilRef,
    userScrollIntentVersionRef,
    revealed,
    anchorPagination,
    armSentMessage,
    closePreSendGestureWindow,
    freezeChatExit,
    freezeForegroundReturn,
    freezeQuestionSubmission,
    freezeQueuedSubmission,
    revealConversationTail,
    reapplyActiveMode,
    settleNonPin,
    settleStreamingPin,
    paneResized,
  }
}
