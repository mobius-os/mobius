/**
 * useScrollMode — the entire scroll state machine for ChatView.
 *
 * One ref holds the current mode; one function (applyMode) is the
 * single funnel that turns a mode into a concrete `scrollTop`.
 * Layout changes (RO, content mutation, spacer recompute, keyboard)
 * re-apply the mode but never mutate it. Only user gestures and
 * explicit semantic events (send, composer intent, mount restore) mutate it.
 *
 * Modes:
 *   { kind: 'INITIAL' }           — pre-restore default; no-op
 *   { kind: 'PIN_USER_MSG', cid, followWhenFilled? }
 *                                  — user msg at top (post-send), keyed on
 *                                    the stable client `cid` (data-cid)
 *   { kind: 'FOLLOW_BOTTOM' }     — sticky-bottom for streaming
 *   { kind: 'ANCHOR_AT', key, offset, part?, questionSubmitViewportH?,
 *     questionSubmitBaseMode? }
 *                                  — anchored at a message and, when that row
 *                                    is taller than the viewport, an ordered
 *                                    child-index path within it; `offset` is
 *                                    measured from the addressed element's
 *                                    top. An in-message question may
 *                                    temporarily preserve its submit-time
 *                                    position at one viewport size
 *
 * Send pinning has one rule for direct, queued, and steered messages: the
 * first visible user message always pins; every later message pins only at the
 * physical bottom/autoscroll tail at submit time. Reserved reply room is real
 * scroll distance for this decision: once the reader moves upward into it,
 * their next send must hold the exact reading position. ScrollMode is only a
 * fallback when no scroll element exists.
 * A live pin leaves FOLLOW_BOTTOM while its dynamic spacer is being consumed,
 * then hands off to FOLLOW_BOTTOM exactly when that reservation reaches zero.
 * A short reply never reaches the handoff and remains pinned after settle.
 * Ordinary dynamic spacer room belongs to the latest user row and is derived
 * from tail geometry, not from whether a gesture has already scrolled that row
 * into view. That keeps scrollHeight stable while the reader approaches the
 * final turn: short replies keep the remaining room, while reply/tool
 * expansion consumes it and collapse restores it. The reservation uses the
 * active scroll-box height, so a keyboard first removes now-hidden blank room;
 * only content that no longer fits makes FOLLOW_BOTTOM move. The one explicit
 * exception is the transient
 * question-submit anchor: it reserves exactly enough tail room for a stable
 * same-viewport handoff. A keyboard resize restores the pre-submit mode before
 * sizing, so the answered card moves exactly as the unanswered card would.
 * Gesture-driven bottom detection reads the scroll container's geometry in
 * the scroll event itself. There is no second sentinel/observer authority
 * that can lag behind the reader and contradict the current viewport.
 *
 * User-gesture detection: pointerdown/wheel/keydown hold
 * reader ownership until the first scroll event actually arrives. Real scrolls
 * keep that ownership until scrolling settles. Native `scrollend` can finish
 * the handoff early; the same 250ms quiet edge guarantees it when browsers
 * advertise that event but fail to deliver it: the hot event path records
 * intent and exact physical-tail arrival only, then derives and persists the
 * final anchor and resizes reservation once after momentum stops. Wheel input
 * is released early only when its direction is exactly clamped at the matching
 * edge; elapsed frames cannot prove that an in-range gesture was a no-op under
 * renderer load. Outside that handoff/settle window, scrolls come from our
 * applyMode or browser clamps and are ignored.
 *
 * See ARCHITECTURE.md "Chat scroll + steer contract" for the full design.
 */

import { useState, useRef, useLayoutEffect, useCallback } from 'react'
import { cidOf } from './messageIdentity.js'
import { isOwnerUserMessage } from './chatRuntimeState.js'
import { BEFORE_SHELL_RELOAD_EVENT } from '../../lib/shellReloadEvents.js'
import { isPerfProbeEnabled, perfMark, perfTime } from '../../lib/perfProbe.js'
import { captureLayoutSpace, clientLengthToLayout } from '../../lib/layoutSpace.js'


// Hide-then-reveal safety cap. The ordinary path reveals after authoritative
// history and one quiet layout window; this is only the escape hatch for a
// stalled history/layout handshake. Image bytes are deliberately NOT part of
// readiness: inline media owns a reserved frame and ResizeObserver keeps the
// saved anchor stable as lazy previews arrive after reveal.
const REVEAL_CAP_MS = 1500
const PREPARING_REVEAL_CAP_MS = 5000

// Reader quiet-settle edge. Input and momentum retain Infinity ownership;
// native scrollend may finish early, while this trailing edge guarantees the
// same handoff on browsers that omit or unreliably deliver that event.
const GESTURE_SETTLE_MS = 250

// A tap or non-scrolling key must not suspend layout ownership forever. This
// cap is only a dead-man release for input that produces no scroll event; a
// delayed scroll caused by a busy main thread still wins because its timer
// cannot run until that same thread is available again.
const PENDING_GESTURE_CAP_MS = 2000

// Physical-bottom transitions and later-send pin eligibility use the same
// exact tail. Allow only subpixel/browser rounding at the scroll extent. This
// prevents reserved reply room from disguising an upward reader escape as
// autoscroll intent.
const PHYSICAL_BOTTOM_EPSILON_PX = 4

// Start the next bounded history read before the loaded-page boundary can
// enter the viewport. A non-scrollable page also needs one immediately because
// the browser cannot emit the scroll event that normally drives pagination.
export const HISTORY_PREFETCH_PX = 240


export function olderHistoryRetryShown(error, offset) {
  return Boolean(error) && Number(offset) > 0
}

export function olderHistoryShouldLoad(scrollEl, { userDriven = false } = {}) {
  if (!scrollEl) return false
  return scrollEl.scrollHeight <= scrollEl.clientHeight + 1
    || (userDriven && scrollEl.scrollTop <= HISTORY_PREFETCH_PX)
}

// Follow-stick band. Adopted from use-stick-to-bottom's STICK_TO_BOTTOM_OFFSET_PX
// (https://github.com/stackblitz-labs/use-stick-to-bottom): a reader counts as
// "at the bottom" while within this many pixels of the tail. This replaces the
// old pixel-exact test that made a fast stream shake the reader out of follow —
// momentum/content-growth drift of a few dozen pixels is still "the bottom".
// The reader leaves follow only by an explicit scroll UP (the escape latch),
// never by drifting inside this band. The jump-to-latest control uses the same
// band so there is no dead zone between "still following" and "button appears".
export const FOLLOW_STICK_BAND_PX = 70

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

// Durable per-chat reading positions.
//
// These lived in sessionStorage, which dies with the browsing session. The
// messages they address survive in IndexedDB for a day, so on every PWA
// relaunch a chat re-opened instantly with its history intact and NO reading
// position — every conversation landed at the bottom and had to be scrolled
// back by hand. A reading position must outlive the tab that created it, so it
// is stored with the same durability as the transcript it points into.
// The rename from the sessionStorage `chat-mode` map IS the migration: those
// entries carry no part path, so they cannot be migrated into a coordinate
// they never recorded, and this key has never held any other shape.
export const READING_POSITION_KEY = 'chat-reading-position'
// This instance has 800+ chats. Positions are tiny, but the map is bounded so
// it cannot grow without limit; least-recently-written entries drop first.
const READING_POSITION_LIMIT = 300

const _scrollModes = (() => {
  try {
    const parsed = JSON.parse(localStorage.getItem(READING_POSITION_KEY) || '{}')
    return (parsed && typeof parsed === 'object') ? parsed : {}
  }
  catch { return {} }
})()
// `clearReadingPositions` is a terminal owner-session boundary. React and the
// browser may still deliver cleanup/pagehide work before the ensuing reload;
// those late callbacks must not recreate storage the session just removed.
let _readingPositionWritesEnabled = true

/** The durable message row an activation must contain before reveal.
 * ChatView uses only the row identity; this module keeps ownership of the
 * nested part and exact pixel offset.
 */
export function savedReadingAnchorKey(chatId) {
  const mode = _scrollModes[String(chatId || '')]
  return mode?.kind === 'ANCHOR_AT' && typeof mode.key === 'string'
    ? mode.key
    : null
}

/** Nested part paths need committed DOM validation before cache reveal. */
export function savedReadingAnchorHasNestedPart(chatId) {
  const mode = _scrollModes[String(chatId || '')]
  return mode?.kind === 'ANCHOR_AT'
    && Array.isArray(mode.part)
    && mode.part.length > 0
}

/** Replace one saved alias with the server's canonical row key before the
 * ready-phase restore consumes it. */
export function remapSavedReadingAnchor(chatId, fromKey, toKey) {
  const id = String(chatId || '')
  const mode = _scrollModes[id]
  if (mode?.kind !== 'ANCHOR_AT'
      || mode.key !== fromKey
      || typeof toKey !== 'string'
      || !toKey) return false
  _scrollModes[id] = { ...mode, key: toKey, at: Date.now() }
  _persistScrollModes()
  return true
}

/** A server-confirmed missing row makes its old coordinate impossible. Retire
 * that address once so the authoritative recent snapshot can settle normally
 * instead of retrying the same unresolvable key forever. */
export function retireSavedReadingPosition(chatId) {
  const id = String(chatId || '')
  if (!(id in _scrollModes)) return false
  delete _scrollModes[id]
  _persistScrollModes()
  return true
}

/** Reading positions are owner-scoped: they leave with the owner's session.
 *  Clearing storage alone is not enough — logout reloads the shell, and a
 *  still-mounted ChatView's pagehide write would put the in-memory map
 *  straight back over the cleared key. */
export function clearReadingPositions() {
  _readingPositionWritesEnabled = false
  for (const key of Object.keys(_scrollModes)) delete _scrollModes[key]
  try { localStorage.removeItem(READING_POSITION_KEY) } catch {}
}

function _persistScrollModes() {
  if (!_readingPositionWritesEnabled) return
  try {
    const entries = Object.entries(_scrollModes)
    if (entries.length > READING_POSITION_LIMIT) {
      // Descending by write time, so the TAIL past the limit is what gets
      // evicted. `.slice(0, LIMIT)` here would delete the newest 300 instead.
      const drop = entries
        .sort((a, b) => (b[1]?.at || 0) - (a[1]?.at || 0))
        .slice(READING_POSITION_LIMIT)
      for (const [key] of drop) delete _scrollModes[key]
    }
    localStorage.setItem(READING_POSITION_KEY, JSON.stringify(_scrollModes))
  }
  // Private mode or a full quota must never break scrolling.
  catch { /* position is best-effort, never load-bearing */ }
}


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


function _captureScrollMeasurement(scrollEl) {
  return {
    space: captureLayoutSpace(scrollEl),
    borderClientTop: scrollEl.getBoundingClientRect().top,
  }
}


/** Position of `el` in the scroll container's coordinate space. */
function _scrollTopOf(scrollEl, el, measurement = null) {
  // Rects are the only measurement that is correct for BOTH a message row and
  // an arbitrarily nested part of one: a part's offsetParent is whatever
  // happens to be positioned around it, which is not reliably the row or the
  // scroll container, so an offsetTop walk silently produces a wrong (often
  // zero) part position. Both call sites already force layout.
  //
  // Every real element has a rect, so this is the only path that runs in a
  // browser. The `offsetTop` branch exists purely so plain object fixtures
  // (which have no rect) still measure row-level anchors the old way.
  if (typeof el?.getBoundingClientRect === 'function'
      && typeof scrollEl?.getBoundingClientRect === 'function') {
    const captured = measurement || _captureScrollMeasurement(scrollEl)
    const clientTopDelta = el.getBoundingClientRect().top
      - captured.borderClientTop
    return clientLengthToLayout(clientTopDelta, captured.space) + scrollEl.scrollTop
  }
  return el?.offsetTop || 0
}


/** Index of the row's topmost child intersecting the viewport, or null.
 *
 * WHY a message needs sub-message resolution at all: one settled Möbius
 * assistant turn is routinely tens of thousands of pixels tall — a measured
 * turn in the owner's "Fixing urgent Möbius tech debt" chat renders 73,721px,
 * 77 viewport heights, 82% of that entire conversation — because agentic turns
 * interleave a few hundred text/tool worklog parts into ONE message row.
 *
 * A whole-message anchor therefore has no resolution inside the message the
 * reader is actually reading: `offset` becomes a five-digit negative number
 * that only restores correctly if the row re-renders to a byte-identical
 * height. It does not. Collapsed-then-expanded tool blocks, asynchronous
 * syntax highlighting, KaTeX, swapped webfonts and the sliced cold render each
 * move it, and every pixel of that drift lands the reader somewhere else
 * entirely — the reported "random super high up position" that then takes tens
 * of screens of scrolling to escape.
 *
 * The parts are already discrete, ordered DOM children, so addressing the Nth
 * one needs no extra markup and bounds the restore error by that part's own
 * height (tens of pixels for a worklog line) instead of the whole turn's. */
function _partPathAt(scrollEl, row, scrollTop, measurement) {
  const viewportH = scrollEl.clientHeight || 0
  const bottom = scrollTop + viewportH
  const path = []
  let node = row
  // Descend while the addressed element is still taller than the viewport:
  // a top-level worklog part can itself be thousands of pixels, so one level
  // of resolution is not enough to say where the reader was. Stop as soon as
  // the target fits, which is when its own height bounds the restore error.
  while (node?.children?.length && node.offsetHeight > viewportH) {
    let next = null
    for (let index = 0; index < node.children.length; index += 1) {
      const kid = node.children[index]
      const kidTop = _scrollTopOf(scrollEl, kid, measurement)
      if (kidTop + (kid.offsetHeight || 0) > scrollTop && kidTop < bottom) {
        path.push(index)
        next = kid
        break
      }
    }
    if (!next) break
    node = next
  }
  return path.length ? path : null
}


/** The element a mode's `part` addresses within a row already in hand. */
function _rowPartTarget(row, mode) {
  if (!row) return null
  const path = Array.isArray(mode?.part) ? mode.part : null
  if (!path?.length) return row
  let node = row
  for (const index of path) {
    const next = node.children?.[index]
    // A path that only partially resolves is an UNRESOLVED location, not a
    // clamp: the caller's `offset` is measured from the addressed part, so
    // returning a shallower origin (at the top level, the row itself) would
    // teleport the reader to the top of a turn that can be tens of thousands
    // of pixels tall. Failing here lets the retention flag keep the stored
    // position instead of overwriting it with a bogus one.
    if (!next) return null
    node = next
  }
  return node
}


/** Build an ANCHOR_AT for `row` describing the viewport at `scrollTop`. */
function _anchorModeForRow(scrollEl, row, scrollTop, extra = null) {
  if (!row?.dataset?.key) return null
  // Real DOM anchors share one rect measurement. Plain row-level fixtures use
  // the offset fallback without having to imitate browser geometry.
  const measurement = typeof scrollEl?.getBoundingClientRect === 'function'
    ? _captureScrollMeasurement(scrollEl)
    : null
  const part = _partPathAt(scrollEl, row, scrollTop, measurement)
  const target = _rowPartTarget(row, { part })
  return {
    kind: 'ANCHOR_AT',
    key: row.dataset.key,
    offset: _scrollTopOf(scrollEl, target, measurement) - scrollTop,
    ...(part == null ? {} : { part }),
    ...(extra || {}),
  }
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
  return _anchorModeForRow(
    scrollEl,
    _topmostVisibleMsg(scrollEl),
    scrollEl.scrollTop,
  )
}


/** Lifecycle anchors must describe visible conversation content. Live scroll
 * handling may temporarily anchor reserved room, but foreground/chat restore
 * must never recreate that blank viewport. */
function _contentAnchorModeFromScroll(scrollEl) {
  if (!scrollEl) return null
  const row = _topmostVisibleMsg(scrollEl)
  const mode = _anchorModeForRow(scrollEl, row, scrollEl.scrollTop)
  if (!mode) return null
  // The row is already in hand — validate against it directly rather than
  // re-resolving through the scroll container.
  return _anchorModeIntersectsContent(
    _rowPartTarget(row, mode), mode, scrollEl?.clientHeight,
  ) ? mode : null
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
  return _anchorModeForRow(
    scrollEl,
    last,
    targetScrollTop,
    { defaultTail: true },
  )
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
  return _anchorModeForRow(scrollEl, last, targetScrollTop)
}


/** Freeze a viewport to real conversation content.
 *
 * A reader can begin moving through latest-user reservation. There may be no
 * exact visible row in that region, but the gesture must still retire live
 * follow. Settle at the latest real-content tail; spacer is then recomputed
 * from whether the held viewport still shows the latest user row. */
export function contentHoldModeFromScroll(scrollEl) {
  const visibleAnchor = _contentAnchorModeFromScroll(scrollEl)
  if (visibleAnchor) return visibleAnchor

  const spacerH = scrollEl?.querySelector?.('.spacer-dynamic')?.offsetHeight || 0
  const realContentBottom = Math.max(
    0,
    (scrollEl?.scrollHeight || 0) - spacerH - (scrollEl?.clientHeight || 0),
  )
  // A transient/unkeyed descendant can fill the viewport even though no
  // canonical row intersects it. While the reader is still inside real
  // content, preserve the exact scrollTop with the nearest keyed row's offset.
  // Only reserved blank space settles to the real-content tail.
  if ((scrollEl?.scrollTop || 0) <= realContentBottom + 1) {
    const positionalAnchor = anchorModeFromScroll(scrollEl)
    if (positionalAnchor) return positionalAnchor
  }
  return bottomAnchorModeFromScroll(scrollEl)
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
      // Follow the one physical tail. While reply reservation remains, real
      // output consumes it without changing this target; once it is exhausted,
      // the same target advances with the stream.
      scrollEl.scrollTop = Math.max(
        0,
        scrollEl.scrollHeight - scrollEl.clientHeight,
      )
      return
    case 'ANCHOR_AT': {
      const el = _anchorEl(scrollEl, mode)
      if (el) {
        scrollEl.scrollTop = Math.max(
          0, _scrollTopOf(scrollEl, el) - mode.offset,
        )
      }
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
  // Never repair an unchanged target by moving the viewport backward. A
  // scrollTop beyond the pin is indistinguishable from (and normally means) a
  // real downward reader gesture whose scroll event may still be queued behind
  // a busy renderer. Legitimate layout damage either moves the target itself
  // (offsetTop changed) or clamps the viewport short of it.
  return el.offsetTop !== lastPinTop || clampedShort
}

/** Resolve the row an ANCHOR_AT mode targets: the element whose `data-key`
 *  equals the mode's key. */
function _anchorRow(scrollEl, key) {
  if (!scrollEl || key == null) return null
  const esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(key) : key
  return scrollEl.querySelector(`[data-key="${esc}"]`)
    || scrollEl.querySelector(`[data-anchor-key="${esc}"]`)
    || scrollEl.querySelector(`[data-cid="${esc}"]`)
}


/** Resolve the exact element an ANCHOR_AT addresses: the message row, or the
 *  `part`-th child of that row when the anchor carries sub-message resolution.
 *  Null when the row is gone OR its part path no longer resolves; both are
 *  unresolved locations and neither may be applied. */
function _anchorEl(scrollEl, mode) {
  return _rowPartTarget(_anchorRow(scrollEl, mode?.key), mode)
}

/** The defining ANCHOR_AT invariant: its row intersects the viewport encoded
 * by `offset`. Negative offsets are valid while the row remains partially
 * visible; an offset beyond either edge describes layout reservation, not a
 * readable conversation location. */
export function _anchorModeIntersectsContent(target, mode, viewportHeight) {
  const offset = Number(mode?.offset)
  return !!target
    && Number.isFinite(offset)
    && Number.isFinite(viewportHeight)
    && viewportHeight > 0
    && offset < viewportHeight
    && offset > -target.offsetHeight
}


function _durableQuestionSubmissionMode(mode) {
  if (mode?.kind !== 'ANCHOR_AT') return mode
  if (!Object.hasOwn(mode, 'questionSubmitViewportH')
      && !Object.hasOwn(mode, 'questionSubmitBaseMode')) {
    return mode
  }
  const {
    questionSubmitViewportH: _transientViewport,
    questionSubmitBaseMode: _transientBaseMode,
    ...durable
  } = mode
  return durable
}


/** A question answer temporarily overlays the reader's existing scroll mode
 * only while the viewport size is unchanged. Keyboard movement belongs to the
 * pre-submit mode: release the overlay before spacer sizing so the answered
 * card receives the same resize behavior as the unanswered card. */
export function releaseQuestionSubmissionForViewport(mode, viewportHeight) {
  if (mode?.kind !== 'ANCHOR_AT'
      || !Number.isFinite(mode.questionSubmitViewportH)
      || !Number.isFinite(viewportHeight)
      || Math.abs(mode.questionSubmitViewportH - viewportHeight) <= 1) {
    return mode
  }
  return mode.questionSubmitBaseMode
    || _durableQuestionSubmissionMode(mode)
}


/** A focused custom Q&A answer gives the browser one narrow exception to the
 * ordinary viewport-resize rule: native caret reveal becomes the new exact
 * reading hold instead of being overwritten by the pre-keyboard anchor.
 * Stronger send pins, live following, and the question-submission overlay keep
 * their existing ownership contracts. */
export function modeForQuestionEditingViewportChange(mode, caretAnchor = null) {
  if (mode?.kind !== 'ANCHOR_AT'
      || Number.isFinite(mode.questionSubmitViewportH)
      || !caretAnchor) return mode
  if (mode.key === caretAnchor.key
      && Math.abs(mode.offset - caretAnchor.offset) <= 0.5) return mode
  return caretAnchor
}


/** The ANCHOR_AT twin of `_pinReapplyNeeded` — the SAME two-case repair. A
 *  settled anchor drifts off its reader-chosen position when either the anchor
 *  element's offsetTop SHIFTED (content grew above it) or scrollTop was CLAMPED
 *  short of the target and the target is now reachable again. Gating on
 *  those conditions (never "every layout tick") keeps steady-state streaming
 *  below the anchor a no-op, so the post-reveal repair this enables cannot
 *  reintroduce the May-2026 re-apply-every-RO-firing jitter. Background panes
 *  resize routinely once panes exist, which is why the anchor now needs the
 *  clamp-repair PIN already had (design §2 prerequisite). */
export function _anchorReapplyNeeded(scrollEl, mode, lastAnchorTop) {
  if (!scrollEl || mode?.kind !== 'ANCHOR_AT') return false
  const el = _anchorEl(scrollEl, mode)
  if (!el) return false
  const anchorTop = _scrollTopOf(scrollEl, el)
  const target = Math.max(0, anchorTop - mode.offset)
  const maxScrollTop = scrollEl.scrollHeight - scrollEl.clientHeight
  const targetReachable = maxScrollTop >= target - 1
  const clampedShort = scrollEl.scrollTop < target - 1 && targetReachable
  // As with a send pin, overshooting an unchanged anchor belongs to the
  // reader. Browser clamps only shorten scrollTop; target movement is already
  // represented by the anchor's container-space top changing.
  //
  // That top is rect-derived, so it is a float: compare with the same 0.5px
  // tolerance `writeMode` uses, or sub-pixel font-swap drift reads as a shift
  // and fires a repair write.
  // Deliberately only two cases: the anchor MOVED, or the browser clamped us
  // short of a target that is reachable again. An element merely changing its
  // own height is NOT a repair case — the reader's distance from that
  // element's top is what holds them, and re-deriving it on a height change
  // moves them off the content they were reading, which is the "it lands on
  // the right position and then scrolls to the wrong one" failure.
  // A null baseline means no recorded position to compare against, which is
  // itself a repair case (as it was under the previous strict !==).
  if (!Number.isFinite(lastAnchorTop)) return true
  return Math.abs(anchorTop - lastAnchorTop) >= 0.5 || clampedShort
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
    if (cidOf(lastUserMsg) !== saved.cid) return holdBottom()
    // PIN_USER_MSG is a live send action, not a durable reading location.
    // Restore its physical result as an ordinary anchor so mount/return cannot
    // recreate pin authority or its later pin→follow layout handoff.
    const row = _pinnedUserEl(scrollEl, saved.cid)
    return row?.dataset?.key
      ? { kind: 'ANCHOR_AT', key: row.dataset.key, offset: PIN_OFFSET }
      : holdBottom()
  }
  if (saved.kind === 'ANCHOR_AT') {
    // A resolvable row is not enough: an old build could persist that row with
    // a huge negative offset while the viewport sat wholly in spacer below it.
    // Enforce the same content-intersection invariant used by spacer sizing,
    // self-healing every off-content restore to the real tail.
    const durable = _durableQuestionSubmissionMode(saved)
    const target = _anchorEl(scrollEl, durable)
    return _anchorModeIntersectsContent(target, durable, scrollEl?.clientHeight)
      ? durable
      : holdBottom()
  }
  return holdBottom()
}


/** Decide how the entry (restore) gate should act for the current mode.
 *
 * The gate converts the neutral INITIAL mode into a concrete reading
 * coordinate exactly once per activation. `_validateSavedMode` only yields
 * INITIAL when there is no content row to address yet — its tail fallback
 * needs at least one `.chat__msg[data-key]` in the DOM. Committing that
 * INITIAL would resolve the coordinate to a no-op and reveal the transcript at
 * scrollTop 0 (the physical top) with no later re-resolution, which is the
 * reported "keep being taken to the top of a chat". So a not-yet-addressable
 * transcript returns `wait`: hold INITIAL and let a later paint (effect re-run,
 * ResizeObserver, or reveal) resolve it against real rows.
 *
 * Returns one of:
 *   { action: 'idle' }                     — not in a restore position
 *   { action: 'wait', resolved, savedPresent }
 *                                          — cannot resolve yet; keep waiting
 *   { action: 'commit', mode, resolved, savedPresent }
 *                                          — concrete restore coordinate
 */
export function entryRestoreDecision({ mode, saved, messages, scrollEl, phase }) {
  const savedPresent = !!saved
  const restorePhase = phase === 'cache-validating'
    || phase === 'cached'
    || phase === 'ready'
  if (mode?.kind !== 'INITIAL' || !restorePhase) {
    return { action: 'idle', resolved: false, savedPresent }
  }
  const restored = _validateSavedMode(saved, messages, scrollEl)
  // No addressable row yet — revealing now would strand the reader at the top.
  if (restored.kind === 'INITIAL') {
    return { action: 'wait', resolved: false, savedPresent }
  }
  const resolved = savedPresent && !restored.defaultTail
  // cache-validating reveals only on an authoritative saved coordinate; a
  // manufactured tail fallback must wait for the validated window.
  if (phase === 'cache-validating' && !resolved) {
    return { action: 'wait', resolved, savedPresent }
  }
  return { action: 'commit', mode: restored, resolved, savedPresent }
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


/** Spacer height needed so the latest user message can sit near the
 *  top of the viewport, with the PIN_OFFSET breathing room above it, or so a
 *  transient question-submit anchor remains reachable while the submit-time
 *  viewport size is unchanged.
 *
 *  Tail geometry is the defining invariant. Reservation exists before a
 *  downward gesture reaches the latest row, so scrollHeight cannot grow at the
 *  old physical bottom after momentum settles. Turn completion does not retire
 *  room. Content growth consumes the exact deficit and content collapse
 *  restores it.
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
 *  A question-submit anchor instead reserves only its exact reachability
 *  deficit for a same-viewport handoff. A keyboard resize restores the mode
 *  that owned the unanswered card before this function runs again.
 */
const PIN_OFFSET = 4
const PIN_BOTTOM_ROOM = 0
export function _computeSpacerH(
  scrollEl,
  listEl,
  lastUserMsgEl,
  mode = null,
) {
  if (!scrollEl || !listEl) return 0
  const viewH = scrollEl.clientHeight
  if (mode?.kind === 'ANCHOR_AT'
      && Number.isFinite(mode.questionSubmitViewportH)) {
    const anchorEl = _anchorEl(scrollEl, mode)
    if (!anchorEl) return 0
    const anchorTarget = Math.max(
      0, _scrollTopOf(scrollEl, anchorEl) - mode.offset,
    )
    return Math.max(0, viewH + anchorTarget - listEl.offsetHeight)
  }
  if (!lastUserMsgEl) return 0
  const pinTarget = Math.max(0, lastUserMsgEl.offsetTop - PIN_OFFSET)
  return Math.max(
    0,
    viewH + pinTarget - listEl.offsetHeight + PIN_BOTTOM_ROOM,
  )
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
  const restoresQuestionSubmissionBase =
    event === 'layout:question-viewport-release'
    && previousMode?.kind === 'ANCHOR_AT'
    && Number.isFinite(previousMode.questionSubmitViewportH)
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


/** A layout plan may commit only while it owns both time and generation.
 * Gesture timing blocks work during the active handoff; the monotonic reader
 * generation prevents work captured before a later gesture from regaining
 * authority when that timing gate eventually opens.
 */
export function scrollAuthorityAllowsCommit({
  capturedVersion,
  currentVersion,
  gestureWindowUntil,
  now,
}) {
  return capturedVersion === currentVersion
    && layoutMayOwnScroll(gestureWindowUntil, now)
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
 * movement. A closed gesture gate with the same generation means wait; only a
 * newer actual-scroll generation makes the old terminal plan permanently stale.
 */
export function terminalLayoutAuthority({
  capturedVersion,
  currentVersion,
  gestureWindowUntil,
  now,
}) {
  if (capturedVersion !== currentVersion) return 'stale'
  return layoutMayOwnScroll(gestureWindowUntil, now) ? 'commit' : 'wait'
}


/** Layout observers may own scrollTop only outside the gesture-intent window.
 * Input events precede the browser's first `scroll` event; without this gate,
 * a streaming ResizeObserver can re-pin/follow in that gap and throw the
 * reader back before onScroll has a chance to stamp ANCHOR_AT. */
export function layoutMayOwnScroll(gestureWindowUntil, now) {
  return now >= gestureWindowUntil
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
 * assistant row and may replace the card's controls immediately. It is not a
 * request to follow the live tail. Freeze the exact visible row/offset before
 * that card-to-stream handoff so neither the control reflow nor resumed output
 * moves the reader. The overlay remembers the mode that owned the unanswered
 * card and is scoped to the current viewport height; a keyboard resize returns
 * to that base mode before layout is recomputed. */
export function modeForQuestionSubmission(scrollEl, currentMode) {
  if (!scrollEl) return currentMode
  const anchor = anchorModeFromScroll(scrollEl)
  if (!anchor) return currentMode
  return {
    ...anchor,
    questionSubmitViewportH: scrollEl.clientHeight,
    questionSubmitBaseMode:
      currentMode?.questionSubmitBaseMode || currentMode,
  }
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


/**
 * Hook that owns the chat scroll subsystem.
 *
 * See the module docblock for the `modeRef.current` tagged union.
 *
 * The caller is expected to:
 *   - Treat `modeRef` as read-only snapshot state. Route send, queue,
 *     pagination, and foreground lifecycle events through the semantic
 *     controller methods returned by this hook.
 *   - Read `gestureWindowUntilRef.current` in any custom scroll
 *     handlers (e.g., pagination triggers) to gate on user intent
 *   - Combine `revealed` with the caller's authoritative-data gate before
 *     painting the scroll container.
 *
 * @param {object} args
 * @param {string} args.chatId
 * @param {React.RefObject<HTMLElement>} args.scrollRef
 *   The `.chat__scroll` container ref.
 * @param {React.RefObject<HTMLElement>} args.spacerRef
 *   The dynamic spacer at the bottom of `.chat__list`.
 * @param {React.RefObject<HTMLElement>} args.lastUserMsgRef
 *   The most recent visible user message element.
 * @param {React.RefObject<HTMLElement>} args.chatRef
 *   The `.chat` root whose composer-clearance CSS variable is scroll geometry.
 * @param {React.RefObject<HTMLElement>} args.footRef
 *   The overlaid composer element measured by the scroll owner.
 * @param {Array<object>} args.messages
 *   Persisted message list. Row-count changes reinstall the DOM owner; content
 *   growth stays on its ResizeObserver so streaming cannot settle a gesture.
 * @param {React.MutableRefObject<Array<object>>} args.messagesRef
 *   Synchronous mirror for restore-time anchor validation.
 * @param {React.MutableRefObject<boolean>} args.loadingOlderRef
 *   When true, scroll events from pagination shouldn't mutate mode.
 * @param {'history'|'cache-validating'|'cached'|'stream-catchup'|'preparing'|'ready'} args.initialEntryPhase
 *   History blocks reveal, cached is a caller-validated restoration window,
 *   cache-validating mounts a complete cached window behind the gate so its
 *   exact nested coordinate can be checked, stream-catchup holds a running
 *   transcript until replay commits, preparing is a hidden progressive
 *   cold render, and ready means authoritative history has settled.
 * @param {() => void} [args.onCachedCoordinateReady]
 *   Promotes a hidden validation cache after its exact saved part resolves.
 * @param {boolean} args.ownsReadingPosition
 *   True only for the physical ChatView participating in the active workspace
 *   handoff. Retained hidden owners may keep DOM geometry, but never consume
 *   or write the logical chat's one durable reading coordinate.
 */
export default function useScrollMode({
  chatId,
  scrollRef,
  spacerRef,
  lastUserMsgRef,
  chatRef,
  footRef,
  messages,
  messagesRef,
  loadingOlderRef,
  initialEntryPhase,
  onCachedCoordinateReady,
  ownsReadingPosition,
}) {
  const messageCount = messages.length
  const [revealed, setRevealed] = useState(false)
  // A tiny React mirror reruns the layout effect when a semantic transition
  // enters/leaves PIN_USER_MSG before message props necessarily change.
  // modeRef remains the synchronous source of truth; visibility still owns
  // spacer and this state is not a second mode machine.
  const [pinModeActive, setPinModeActive] = useState(false)
  // Reactive mirror of FOLLOW_BOTTOM, used only to render the jump-to-latest
  // control (contract R5a). modeRef stays the synchronous source of truth; this
  // is deliberately NOT in the main layout effect's deps, so toggling follow
  // never reinstalls the scroll controller mid-stream.
  const [following, setFollowing] = useState(false)
  // Synchronous mirror of `revealed` for reapplyActiveMode, which is called
  // from a ChatView layout effect (a closure that may pre-date the reveal
  // flip). Set inline at every setRevealed(true) so the read is never stale.
  const revealedRef = useRef(false)
  const modeRef = useRef({ kind: 'INITIAL' })
  const modeChatIdRef = useRef(null)
  // Standard and Builder may retain separate physical ChatViews for one chat.
  // This is the sole durable-position authority: only the active surface may
  // persist, and a hidden surface must re-enter through INITIAL before restore.
  const readingPositionOwnerRef = useRef(ownsReadingPosition)
  // False only when mount had no deliberate reader location and therefore
  // used the automatic latest-message fallback. Passive lifecycle/viewport
  // changes must not promote that fallback into a saved reading position.
  const readerLocationExplicitRef = useRef(false)
  // A drawer result is navigation, not a new saved reading location. Preserve
  // the owner's prior coordinate until they actively scroll or send from the
  // revealed row.
  const transientSearchRevealRef = useRef(false)
  // A saved location existed but this visit could not resolve it (its row or
  // part was not in the committed window yet). That is a RETRIEVAL failure,
  // not the reader choosing the tail — so the automatic tail fallback must not
  // be written over the stored location. Without this, one unresolvable visit
  // permanently destroyed the reader's position and every later return was
  // "broken" for good rather than for one visit.
  const savedLocationUnresolvedRef = useRef(false)
  const gestureWindowUntilRef = useRef(0)
  const pendingGestureTimerRef = useRef(0)
  const pendingGestureReleaseRafRef = useRef(0)
  const gestureSequenceRef = useRef(0)
  const resumeLayoutAfterGestureRef = useRef(null)
  // A newer semantic action (Send, attention nudge) supersedes any reader
  // settlement still waiting on the quiet edge. The effect publishes its
  // local cancel closure here so those actions cannot be overwritten later.
  const discardPendingReaderSettleRef = useRef(null)
  // Monotonic generation for actual reader scroll intent. Send/steer snapshots
  // and every deferred/automatic geometry commit use this same authority:
  // once a newer gesture lands, older work can never regain ownership merely
  // because the gesture timing window later closes.
  const readerIntentVersionRef = useRef(0)
  // The chat reacts to the size of its actual scroll viewport, after Shell has
  // reconciled any visual-viewport overlay. Keeping the last observed height
  // across effect re-runs makes ResizeObserver the one keyboard/layout signal
  // and removes the old race between two direct visualViewport listeners.
  const observedScrollViewportRef = useRef({ element: null, height: 0 })
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
  // Set inside the layout effect to the live pane-resize runner; the returned
  // paneResized() forwards to it (null when no scroll DOM is mounted). Mirrors
  // the forceRevealRef / resumeLayoutAfterGestureRef effect-bridge pattern.
  const paneResizeRunRef = useRef(null)
  // Footer height changes are scroll geometry: the controller observes the
  // footer and publishes its list clearance in the same guarded transaction.
  // Controlled composer edits must publish their draft before a PIN -> FOLLOW
  // transition can render. React's change handler invokes this effect-owned
  // bridge after accepting the new value.
  const composerEditRunRef = useRef(null)
  const initialEntryPhaseRef = useRef(initialEntryPhase)
  initialEntryPhaseRef.current = initialEntryPhase
  // A normal reveal ends entry stabilization. A forced safety-cap reveal keeps
  // the saved anchor under layout ownership until authoritative history and
  // transcript layout settle.
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
    let preparationDeadline = 0
    const deadline = setTimeout(() => {
      if (revealedRef.current) return
      // A pathological transcript can be deliberately preparing in bounded,
      // committed slices. Revealing at the ordinary stalled-request cap would
      // expose that incomplete prefix and let its later growth take over the
      // reader's scroll mode. Give this explicit phase its own absolute escape
      // hatch; a normal ready transition still reveals immediately through the
      // quiet-layout path below.
      if (initialEntryPhaseRef.current === 'preparing') {
        preparationDeadline = setTimeout(() => {
          if (revealedRef.current) return
          if (initialEntryPhaseRef.current !== 'ready') return
          if (forceRevealRef.current) forceRevealRef.current()
          else {
            revealedRef.current = true
            setRevealed(true)
          }
        }, PREPARING_REVEAL_CAP_MS - REVEAL_CAP_MS)
        return
      }
      // A deadline may release slow layout, never unvalidated data. `cached`
      // is assigned only after the caller proves saved-coordinate coverage.
      if (
        initialEntryPhaseRef.current !== 'cached'
        && initialEntryPhaseRef.current !== 'ready'
      ) return
      if (forceRevealRef.current) forceRevealRef.current()
      else {
        // Defensive fallback for a mount whose scroll DOM never materialized.
        revealedRef.current = true
        setRevealed(true)
      }
    }, REVEAL_CAP_MS)
    return () => {
      clearTimeout(deadline)
      clearTimeout(preparationDeadline)
    }
  }, [chatId])

  // Geometry capture reads scrollHeight/offsetHeight, which forces a synchronous
  // reflow of the unvirtualized transcript. That is acceptable for low-frequency
  // transition/write traces (they run right after a layout write anyway), but on
  // the reader-input path it would reflow on every touchstart and first scroll
  // frame — the exact gesture-start window that must stay cheap. Those call sites
  // opt out; geometry stays on by default everywhere else.
  const recordTrace = useCallback((bucket, event, {
    from = null,
    to = null,
    scrollEl = scrollRef.current,
    captureGeometry = true,
  } = {}) => {
    _appendScrollTrace(bucket, {
      at: Math.round(typeof performance !== 'undefined' ? performance.now() : 0),
      chatId: String(chatId),
      event,
      ...(from ? { from: _scrollModeForDiagnostics(from) } : {}),
      ...(to ? { to: _scrollModeForDiagnostics(to) } : {}),
      geometry: captureGeometry ? _scrollGeometryForDiagnostics(scrollEl) : null,
    })
  }, [chatId, scrollRef])

  // The sole mode-mutation funnel. ChatView emits semantic lifecycle events
  // through the methods returned by this hook; layout and reader paths below
  // use the same transition function, so mode ownership cannot drift across
  // a collection of direct `modeRef.current = ...` writes.
  const transitionMode = useCallback((proposedMode, event) => {
    const previousMode = modeRef.current
    const nextMode = modeForScrollTransition(previousMode, proposedMode, event)
    if (!nextMode) return previousMode
    if (nextMode === previousMode) return previousMode
    modeRef.current = nextMode
    const pinOwnedBefore = previousMode?.kind === 'PIN_USER_MSG'
    const pinOwnedAfter = nextMode?.kind === 'PIN_USER_MSG'
    if (pinOwnedBefore !== pinOwnedAfter) {
      setPinModeActive(pinOwnedAfter)
    }
    const followBefore = previousMode?.kind === 'FOLLOW_BOTTOM'
    const followAfter = nextMode?.kind === 'FOLLOW_BOTTOM'
    if (followBefore !== followAfter) {
      setFollowing(followAfter)
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
  const writeMode = useCallback((
    scrollEl,
    mode,
    event,
    authorityVersion = readerIntentVersionRef.current,
  ) => {
    if (!scrollEl || !mode) return false
    if (!scrollAuthorityAllowsCommit({
      capturedVersion: authorityVersion,
      currentVersion: readerIntentVersionRef.current,
      gestureWindowUntil: gestureWindowUntilRef.current,
      now: performance.now(),
    })) return false
    const before = scrollEl.scrollTop
    applyMode(scrollEl, mode)
    if (Math.abs(scrollEl.scrollTop - before) > 0.5) {
      recordTrace('writes', event, {
        from: mode,
        to: mode,
        scrollEl,
      })
    }
    return true
  }, [recordTrace])

  const persistMode = useCallback(({ freezeToCurrentPosition = false } = {}) => {
    try {
      if (!readingPositionOwnerRef.current) return
      if (transientSearchRevealRef.current) return
      if (!readerLocationExplicitRef.current) {
        // Keep a stored location this visit merely failed to resolve. Only an
        // absent location — never a retrieval failure — may be cleared here.
        // Activation can fail before the ready-phase restore gets a chance to
        // set the unresolved flag, so the still-present durable entry is also
        // direct evidence that cleanup must leave it alone.
        if (savedLocationUnresolvedRef.current
            || Object.hasOwn(_scrollModes, chatId)) return
        delete _scrollModes[chatId]
        _persistScrollModes()
        return
      }
      const candidate = freezeToCurrentPosition
        ? (modeForChatExit(scrollRef.current) || modeRef.current)
        : modeRef.current
      // One persistence gate for every lifecycle path. Invalid ANCHOR_AT
      // geometry is normalized before it reaches storage. Live
      // FOLLOW_BOTTOM/PIN_USER_MSG remains observable while mounted; the
      // restore gate settles those modes on the next mount.
      const mode = _modeForPersistence(
        candidate, messagesRef.current, scrollRef.current,
      )
      if (mode && mode.kind !== 'INITIAL') {
        if (freezeToCurrentPosition) {
          transitionMode(mode, 'lifecycle:chat-exit')
        }
        // `defaultTail` marks a manufactured fallback, never a stored
        // location: persisting it makes the next mount misread a good entry
        // as unresolved and freeze that chat's position until the reader
        // scrolls.
        const { defaultTail: _fallback, ...durable } = mode
        _scrollModes[chatId] = { ...durable, at: Date.now() }
      } else {
        delete _scrollModes[chatId]
      }
      _persistScrollModes()
    } catch {}
  }, [chatId, messagesRef, scrollRef, transitionMode])

  // Transfer the logical chat's one durable coordinate with visibility. The
  // outgoing owner persists before relinquishing authority; the incoming owner
  // restores through INITIAL. A retained surface that begins hidden does neither.
  useLayoutEffect(() => {
    const wasOwner = readingPositionOwnerRef.current
    if (wasOwner === ownsReadingPosition) return

    if (wasOwner) {
      if (modeRef.current.kind !== 'INITIAL'
          && !(savedLocationUnresolvedRef.current
            && !readerLocationExplicitRef.current)) {
        readerLocationExplicitRef.current = true
        // Main-effect cleanup has already settled pending input to ANCHOR_AT.
        // Preserve that address: re-measuring after a world reflow can lose its
        // nested part. Live FOLLOW/PIN modes still need one physical freeze.
        persistMode({
          freezeToCurrentPosition: modeRef.current.kind !== 'ANCHOR_AT',
        })
      }
      readingPositionOwnerRef.current = false
      return
    }

    readingPositionOwnerRef.current = true
    readerLocationExplicitRef.current = false
    savedLocationUnresolvedRef.current = false
    transitionMode({ kind: 'INITIAL' }, 'lifecycle:position-owner-enter')
  }, [ownsReadingPosition, persistMode, transitionMode])

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

  // A semantic action snapshots the geometry that the reader just chose, then
  // supersedes every unfinished part of the older gesture. Keep that ordering
  // inside the scroll owner: exposing the gesture timer + intent generation to
  // ChatView made each new send entrypoint responsible for reproducing the same
  // easy-to-miss sequence.
  const supersedePendingReaderGesture = useCallback(() => {
    discardPendingReaderSettleRef.current?.()
    gestureSequenceRef.current += 1
    gestureWindowUntilRef.current = 0
    clearTimeout(pendingGestureTimerRef.current)
    pendingGestureTimerRef.current = 0
    cancelAnimationFrame(pendingGestureReleaseRafRef.current)
    pendingGestureReleaseRafRef.current = 0
    resumeLayoutAfterGestureRef.current?.()
  }, [])

  const captureSendIntent = useCallback(({
    canPin = true,
    isFirstUserMsg = false,
    previousIntent = null,
  } = {}) => {
    transientSearchRevealRef.current = false
    const readerIntentVersion = readerIntentVersionRef.current
    const willPinNow = canPin && shouldPinSend({
      scrollEl: scrollRef.current,
      mode: modeRef.current,
      isFirstUserMsg,
    })
    const intent = {
      willPin: delayedSendWillPin({
        previousIntent: canPin ? previousIntent : null,
        readerIntentVersion,
        willPinNow,
      }),
      readerIntentVersion,
    }
    supersedePendingReaderGesture()
    return intent
  }, [scrollRef, supersedePendingReaderGesture])

  const sendIntentIsCurrent = useCallback(intent => (
    !!intent
    && intent.readerIntentVersion === readerIntentVersionRef.current
  ), [])

  const commitSendIntent = useCallback(({
    cid,
    intent,
    fallbackWillPin = false,
  }) => {
    readerLocationExplicitRef.current = true
    if (intent && !sendIntentIsCurrent(intent)) {
      return settleNonPin({
        retireFollow: true,
        event: 'send:reader-overrode-delayed-pin',
      })
    }
    const willPin = intent ? intent.willPin : fallbackWillPin
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
  }, [sendIntentIsCurrent, settleNonPin, transitionMode])

  const settleSendIntent = useCallback(({
    intent,
    retireFollow = false,
    event = 'send:hold-current',
  } = {}) => {
    if (intent && !sendIntentIsCurrent(intent)) return modeRef.current
    return settleNonPin({ retireFollow, event })
  }, [sendIntentIsCurrent, settleNonPin])

  const freezeQueuedSubmission = useCallback(() => {
    readerLocationExplicitRef.current = true
    return transitionMode(
      modeForQueuedSubmission(scrollRef.current, modeRef.current),
      'send:queue-freeze',
    )
  }, [scrollRef, transitionMode])

  const freezeQuestionSubmission = useCallback(() => {
    const nextMode = modeForQuestionSubmission(scrollRef.current, modeRef.current)
    // Submit is a newer semantic reading action. Its current-geometry snapshot
    // must not be replaced a few milliseconds later by the quiet settlement of
    // the scroll that positioned the question card.
    supersedePendingReaderGesture()
    readerLocationExplicitRef.current = true
    return transitionMode(
      nextMode,
      'send:question-freeze',
    )
  }, [scrollRef, supersedePendingReaderGesture, transitionMode])

  const anchorPagination = useCallback((key, offset) => {
    if (!key) return modeRef.current
    readerLocationExplicitRef.current = true
    return transitionMode(
      { kind: 'ANCHOR_AT', key, offset },
      'reader:paginate-anchor',
    )
  }, [transitionMode])

  /** One held search result reveal; unlike a reader gesture it is never
   * persisted over the owner's saved location. */
  const revealAnchor = useCallback((key, offset = 96, exactTarget = null) => {
    const scrollEl = scrollRef.current
    const row = _anchorRow(scrollEl, key)
    if (!scrollEl || !row) return false
    let anchorOffset = offset
    const targetRect = exactTarget?.getBoundingClientRect?.()
    const rowRect = row.getBoundingClientRect?.()
    if (Number.isFinite(targetRect?.top) && Number.isFinite(rowRect?.top)) {
      const targetDelta = clientLengthToLayout(
        targetRect.top - rowRect.top,
        captureLayoutSpace(scrollEl),
      )
      if (Number.isFinite(targetDelta)) anchorOffset -= targetDelta
    }
    supersedePendingReaderGesture()
    transientSearchRevealRef.current = true
    readerLocationExplicitRef.current = false
    const mode = transitionMode(
      { kind: 'ANCHOR_AT', key: row.dataset.key, offset: anchorOffset },
      'search:reveal-anchor',
    )
    const authorityVersion = readerIntentVersionRef.current
    writeMode(scrollEl, mode, 'search:reveal-anchor', authorityVersion)
    lastAppliedModeRef.current = mode
    return true
  }, [scrollRef, supersedePendingReaderGesture, transitionMode, writeMode])

  const freezeForegroundReturn = useCallback(() => {
    const nextMode = modeForForegroundReturn(scrollRef.current)
    return nextMode
      ? transitionMode(nextMode, 'lifecycle:foreground-return')
      : modeRef.current
  }, [scrollRef, transitionMode])

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
    discardPendingReaderSettleRef.current?.()
    gestureWindowUntilRef.current = 0
    readerIntentVersionRef.current += 1
    const authorityVersion = readerIntentVersionRef.current
    readerLocationExplicitRef.current = true
    transitionMode(nextMode, 'reader:attention-nudge-tail')
    writeMode(
      scrollEl,
      nextMode,
      'reader:attention-nudge-tail',
      authorityVersion,
    )
    lastAppliedModeRef.current = nextMode
    persistMode()
  }, [persistMode, scrollRef, transitionMode, writeMode])

  // The jump-to-latest control: unlike the question/resume nudges (which only
  // reveal a card as a settled hold), tapping "jump to latest" is an explicit
  // request to RESUME following, matching use-stick-to-bottom's scrollToBottom.
  // It re-enters FOLLOW_BOTTOM so a live stream keeps glued afterward; on an
  // idle chat it simply rests at the tail. Close any open gesture window so the
  // resulting programmatic scroll is not mistaken for a fresh human gesture.
  const followLatest = useCallback(() => {
    const scrollEl = scrollRef.current
    if (!scrollEl) return
    discardPendingReaderSettleRef.current?.()
    gestureWindowUntilRef.current = 0
    readerIntentVersionRef.current += 1
    const authorityVersion = readerIntentVersionRef.current
    readerLocationExplicitRef.current = true
    const mode = transitionMode({ kind: 'FOLLOW_BOTTOM' }, 'reader:scroll-bottom')
    writeMode(scrollEl, mode, 'reader:jump-to-latest', authorityVersion)
    lastAppliedModeRef.current = mode
    persistMode()
  }, [persistMode, scrollRef, transitionMode, writeMode])

  useLayoutEffect(() => () => {
    clearTimeout(pendingGestureTimerRef.current)
    cancelAnimationFrame(pendingGestureReleaseRafRef.current)
  }, [])

  // Persist mode on every chatId change so the next mount restores.
  // (Layout effect can't easily handle persistence because it runs
  // on every messages change; cleanup is only fired on chatId change.)
  useLayoutEffect(() => {
    return () => persistMode({ freezeToCurrentPosition: true })
  }, [persistMode])

  // A hard shell refresh/page background does not reliably run React's
  // cleanup after the human manually scrolls. Persist the current mode on the
  // browser lifecycle events too, so reload returns to the last reading
  // position rather than an older mode saved during the last message change.
  useLayoutEffect(() => {
    if (!ownsReadingPosition || typeof window === 'undefined') return
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
  }, [ownsReadingPosition, persistMode])

  // Single layout effect: spacer sizing, automatic scroll writes,
  // ResizeObserver layout updates (including the mobile keyboard after Shell
  // has resized), user-gesture detection, and geometry-based transitions.
  // Re-runs when transcript structure, queue presence, or chat identity changes.
  // Content-only streaming is already owned by ResizeObserver; reinstalling
  // here would settle and recreate the active gesture on every streamed chunk.
  useLayoutEffect(() => {
    // Empty chats intentionally render no scroll node. Initialize the chat
    // identity before that early return so the first send can arm its pin
    // without the newly mounted transcript being mistaken for a chat change.
    if (modeChatIdRef.current !== chatId) {
      modeChatIdRef.current = chatId
      readerIntentVersionRef.current += 1
      transitionMode({ kind: 'INITIAL' }, 'lifecycle:chat-change')
    }

    // Retained hidden workspace worlds keep their DOM but own no scroll
    // lifecycle, observers, or durable reading-position writes. Becoming the
    // active owner above re-enters through INITIAL and this effect then installs
    // the one live controller against fresh geometry.
    if (!readingPositionOwnerRef.current) return

    const scrollEl = scrollRef.current
    const spacerEl = spacerRef.current
    if (!scrollEl || !spacerEl) return
    const chatEl = chatRef.current
    const isQuestionEditor = target => target?.matches?.(
      'textarea.qcard__input',
    ) === true
    const questionEditorIsFocused = () => {
      if (typeof document === 'undefined') return false
      const target = document.activeElement
      return isQuestionEditor(target) && scrollEl.contains?.(target) === true
    }

    if (observedScrollViewportRef.current.element !== scrollEl) {
      observedScrollViewportRef.current = {
        element: scrollEl,
        height: scrollEl.clientHeight,
      }
    }

    const listEl = scrollEl.querySelector('.chat__list')
    if (!listEl) return

    // Restore mode only after either the cache proves it contains the saved row
    // address or authoritative activation repairs/retires that address. The
    // same gate keeps a progressive cold prefix from rejecting a valid
    // deep-in-row part path.
    // Resolve the entry coordinate from the live transcript. A neutral INITIAL
    // mode means "not yet restored"; this converts it into the saved location
    // (or the validated tail fallback) — but only once at least one content row
    // exists to address. When rows have not painted yet the decision is `wait`,
    // so INITIAL is held rather than committed: committing the no-op would
    // reveal the transcript at the physical top. Progressive hidden-slice fills
    // add rows without changing `messageCount`, so this effect body's single
    // pass is not enough; the ResizeObserver and the reveal commit below call
    // this again so every path that can paint rows also re-resolves.
    const attemptEntryRestore = () => {
      const decision = entryRestoreDecision({
        mode: modeRef.current,
        saved: _scrollModes[chatId],
        messages: messagesRef.current,
        scrollEl,
        phase: initialEntryPhaseRef.current,
      })
      if (decision.action === 'idle') return
      if (decision.action === 'wait') {
        readerLocationExplicitRef.current = false
        savedLocationUnresolvedRef.current = decision.savedPresent
        return
      }
      readerLocationExplicitRef.current = decision.resolved
      savedLocationUnresolvedRef.current = decision.savedPresent && !decision.resolved
      transitionMode(decision.mode, 'lifecycle:restore')
      if (initialEntryPhaseRef.current === 'cache-validating') {
        onCachedCoordinateReady?.()
      }
    }
    attemptEntryRestore()
    // Semantic transitions use a fresh mode object. Identity therefore keeps
    // steady streaming from rewriting scrollTop, while the ref survives effect
    // re-runs and development-time double invocation.

    const currentAuthority = () => readerIntentVersionRef.current
    const layoutOwnsScroll = (authorityVersion = currentAuthority()) => (
      scrollAuthorityAllowsCommit({
        capturedVersion: authorityVersion,
        currentVersion: readerIntentVersionRef.current,
        gestureWindowUntil: gestureWindowUntilRef.current,
        now: performance.now(),
      })
    )
    let deferredGestureLayoutTimer = 0
    let deferredGestureLayoutPending = false
    const deferLayoutUntilReaderYields = (
      authorityVersion = currentAuthority(),
    ) => {
      clearTimeout(deferredGestureLayoutTimer)
      deferredGestureLayoutPending = true
      const ownershipUntil = gestureWindowUntilRef.current
      // Infinity means input has arrived but its first scroll has not. There
      // is deliberately no guessed retry delay in that phase: onScroll, the
      // no-scroll release, or the next effect instance resumes this pass.
      const delay = gestureLayoutRetryDelay(ownershipUntil, performance.now())
      if (delay == null) return
      const scheduledAuthority = authorityVersion
      deferredGestureLayoutTimer = setTimeout(() => {
        deferredGestureLayoutTimer = 0
        if (!layoutOwnsScroll(scheduledAuthority)) return
        deferredGestureLayoutPending = false
        // The observer pass that noticed the geometry change deliberately
        // yielded to reader input. Once ownership returns, replay that missed
        // write even when the semantic mode object itself did not change (the
        // common case for FOLLOW_BOTTOM while new content arrives). Calling
        // the ordinary identity-gated path here would resize the spacer but
        // leave the viewport behind the live tail forever.
        if (scrollRef.current === scrollEl) {
          syncLayout({
            forceApply: true,
            authorityVersion: scheduledAuthority,
          })
        }
      }, delay)
    }
    const replayDeferredLayoutNow = () => {
      const authorityVersion = currentAuthority()
      if (!deferredGestureLayoutPending
          || !layoutOwnsScroll(authorityVersion)) return false
      clearTimeout(deferredGestureLayoutTimer)
      deferredGestureLayoutTimer = 0
      deferredGestureLayoutPending = false
      // The footer CSS variable, compensating spacer, and semantic mode write
      // must commit in ONE task. If CSS padding + spacer land first and the
      // HOLD/ANCHOR write waits for the next timer, browser scroll anchoring
      // can paint one frame 36px away before the controller corrects it.
      syncLayout({ forceApply: true, authorityVersion })
      return true
    }
    const resumeLayoutAfterGesture = () => {
      if (!deferredGestureLayoutPending) return
      deferLayoutUntilReaderYields(currentAuthority())
    }
    resumeLayoutAfterGestureRef.current = resumeLayoutAfterGesture

    function sizeSpacer(authorityVersion = currentAuthority()) {
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
      if (!layoutOwnsScroll(authorityVersion)) {
        deferLayoutUntilReaderYields(authorityVersion)
        return
      }
      const composerHeight = footRef.current?.offsetHeight
      if (chatRef.current && Number.isFinite(composerHeight)) {
        chatRef.current.style.setProperty('--composer-h', `${composerHeight}px`)
      }
      // A sent row remounts when its optimistic identity becomes canonical.
      // Read the mounted last-user row while its React ref reattaches so that
      // brief handoff cannot collapse the reservation to zero.
      const lastUserEl = _lastUserRowEl(scrollEl) || lastUserMsgRef.current
      const h = _computeSpacerH(
        scrollEl, listEl, lastUserEl, modeRef.current,
      )
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

    function rememberAppliedMode() {
      const mode = modeRef.current
      lastAppliedModeRef.current = mode
      const pinnedEl = mode.kind === 'PIN_USER_MSG'
        ? _pinnedUserEl(scrollEl, mode.cid)
        : null
      const anchorEl = mode.kind === 'ANCHOR_AT'
        ? _anchorEl(scrollEl, mode)
        : null
      lastPinTopRef.current = pinnedEl?.offsetTop ?? null
      lastAnchorTopRef.current = anchorEl
        ? _scrollTopOf(scrollEl, anchorEl)
        : null
    }

    function applyLayoutMode(event, authorityVersion) {
      if (!writeMode(scrollEl, modeRef.current, event, authorityVersion)) {
        return false
      }
      rememberAppliedMode()
      return true
    }

    function maybeApplyMode(authorityVersion) {
      if (modeRef.current !== lastAppliedModeRef.current) {
        applyLayoutMode('layout:mode-transition', authorityVersion)
      }
    }

    function settlePinnedMode(authorityVersion) {
      if (!_pinReapplyNeeded(scrollEl, modeRef.current, lastPinTopRef.current)) {
        return
      }
      applyLayoutMode('layout:repair-pin', authorityVersion)
    }

    // The ANCHOR_AT twin of settlePinnedMode — the post-reveal clamp-repair.
    // Re-applies only when _anchorReapplyNeeded says the anchor shifted or was
    // clamped (never every firing), so it does not fight the reader or jitter.
    function settleAnchoredMode(authorityVersion) {
      if (!_anchorReapplyNeeded(scrollEl, modeRef.current, lastAnchorTopRef.current)) {
        return
      }
      applyLayoutMode('layout:repair-anchor', authorityVersion)
    }

    // Full sync — size spacer and apply-if-changed. Used at mount, RO, reveal,
    // and actual scroll-viewport changes. Each call sizes the spacer (always
    // needed — the spacer math depends on changing content). Most callers only
    // touch scrollTop on a real mode transition; viewport and deferred-replay
    // branches reapply the current mode after geometry changes.
    function syncLayout({
      forceApply = false,
      viewportChange = false,
      authorityVersion = currentAuthority(),
    } = {}) {
      const questionSubmissionWasActive = viewportChange
        && modeRef.current?.kind === 'ANCHOR_AT'
        && Number.isFinite(modeRef.current.questionSubmitViewportH)
      // Question submission freezes the card-to-stream handoff, not the
      // keyboard. Restore the unanswered card's mode before sizing a changed
      // viewport so its ordinary reservation and clamp remain authoritative.
      if (viewportChange) {
        const released = releaseQuestionSubmissionForViewport(
          modeRef.current,
          scrollEl.clientHeight,
        )
        if (released !== modeRef.current) {
          transitionMode(released, 'layout:question-viewport-release')
          persistMode()
        }
      }
      // Releasing the submitted-question overlay is a semantic state change,
      // not a scroll write. It must happen even while the input gate retains
      // viewport ownership; the deferred layout pass below then applies the
      // restored mode after the reader yields.
      if (!layoutOwnsScroll(authorityVersion)) {
        deferLayoutUntilReaderYields(authorityVersion)
        return false
      }
      sizeSpacer(authorityVersion)
      if (viewportChange) {
        const caretAnchor = questionEditorIsFocused()
          && !questionSubmissionWasActive
          ? anchorModeFromScroll(scrollEl)
          : null
        const viewportMode = modeForQuestionEditingViewportChange(
          modeRef.current,
          caretAnchor,
        )
        if (viewportMode !== modeRef.current) {
          readerLocationExplicitRef.current = true
          transitionMode(viewportMode, 'layout:question-edit-viewport')
          persistMode()
        }
        // A viewport resize is geometry, not reading intent. Reapply the mode
        // that already owns the chat instead of deriving a different mode from
        // the browser's intermediate clamp. The focused Q&A exception above
        // adopts a native caret reveal before this write can undo it.
        applyLayoutMode('layout:viewport-change', authorityVersion)
      } else if (forceApply) {
        writeMode(
          scrollEl,
          modeRef.current,
          'layout:forced-reapply',
          authorityVersion,
        )
      } else {
        maybeApplyMode(authorityVersion)
      }
      // A send can set PIN_USER_MSG while the dynamic spacer is still at its
      // old height. `sizeSpacer()` above makes the target reachable; this
      // immediate settle pass applies the same pin again if the browser had
      // clamped the first write, instead of waiting for a later RO event that
      // may never fire for the spacer-only height change.
      settlePinnedMode(authorityVersion)
      return true
    }
    syncLayout({ authorityVersion: currentAuthority() })

    // Shell supplies committed pane geometry separately from viewport resize.
    // The signal distinguishes a deliberate workspace reflow from a transient
    // visual-viewport change; the committed DOM is the geometry authority.
    function runPaneResize() {
      const authorityVersion = currentAuthority()
      // This committed pane change has its own explicit owner. Adopt the DOM's
      // resulting scroll-box height now so the ResizeObserver delivery that
      // follows cannot process the same resize again as a keyboard change.
      observedScrollViewportRef.current = {
        element: scrollEl,
        height: scrollEl.clientHeight,
      }
      if (!layoutOwnsScroll(authorityVersion)) {
        // Keep spacer and mode in one replayable pass.
        deferLayoutUntilReaderYields(authorityVersion)
        return
      }
      sizeSpacer(authorityVersion)
      const k = modeRef.current.kind
      if (k === 'FOLLOW_BOTTOM') {
        writeMode(
          scrollEl,
          modeRef.current,
          'pane:resize-follow',
          authorityVersion,
        )
      } else if (k === 'PIN_USER_MSG') {
        settlePinnedMode(authorityVersion)
      } else if (k === 'ANCHOR_AT') {
        settleAnchoredMode(authorityVersion)
      }
    }
    paneResizeRunRef.current = runPaneResize

    // Reveal after authoritative history once transcript layout stays quiet
    // for 50ms. Lazy image previews
    // are not a reveal dependency: their frames already reserve space and the
    // same ResizeObserver keeps ANCHOR_AT stable if a measured ratio differs.
    // REVEAL_CAP_MS is a layout escape hatch only; data readiness remains owned
    // by the caller and cannot be bypassed by a timer.
    let revealTimer = 0
    let mountMutationObserver = null
    const entryReady = () => initialEntryPhaseRef.current === 'cached'
      || initialEntryPhaseRef.current === 'ready'
    const requestRevealOnQuiet = () => {
      clearTimeout(revealTimer)
      if (revealedRef.current && !mountStabilizingRef.current) return
      if (!entryReady()) return
      const authorityVersion = currentAuthority()
      revealTimer = setTimeout(() => {
        if (scrollRef.current !== scrollEl || !entryReady()) return
        // Authority-gate before resolving: if a reader gesture somehow owns the
        // scroll, skip both the coordinate resolve and the reveal and let a
        // later pass retry — a live gesture must never have its mode rewritten
        // underneath it. (syncLayout re-checks the same gate; this only moves
        // the skip ahead of the mode write, and the outcome when unowned — no
        // reveal — is unchanged.)
        if (!layoutOwnsScroll(authorityVersion)) return
        // Rows may have finished painting only now; resolve the entry
        // coordinate from them so reveal never commits at the physical top.
        attemptEntryRestore()
        if (!syncLayout({ authorityVersion })) return
        revealedRef.current = true
        mountStabilizingRef.current = initialEntryPhaseRef.current !== 'ready'
        setRevealed(true)
        if (!mountStabilizingRef.current) mountMutationObserver?.disconnect()
      }, 50)
    }
    const forceReveal = () => {
      if (revealedRef.current || scrollRef.current !== scrollEl) return
      if (!entryReady()) return
      // Even the safety-cap reveal resolves the coordinate first, so a forced
      // reveal with rows present still lands on the saved location, not the top.
      attemptEntryRestore()
      syncLayout({ authorityVersion: currentAuthority() })
      mountStabilizingRef.current = false
      revealedRef.current = true
      setRevealed(true)
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
      const authorityVersion = currentAuthority()
      // Streaming content can resize on every reveal frame while the reader is
      // actively scrolling. Defer the whole geometry transaction here: even a
      // read of scrollHeight/offsetHeight can synchronously lay out the full
      // transcript and steal a frame from native momentum. The trailing replay
      // performs one authoritative spacer + mode pass after reader ownership
      // ends.
      if (!layoutOwnsScroll(authorityVersion)) {
        deferLayoutUntilReaderYields(authorityVersion)
        requestRevealOnQuiet()
        return
      }
      const previousViewportH = observedScrollViewportRef.current.height
      const currentViewportH = scrollEl.clientHeight
      const viewportChanged = previousViewportH > 0
        && Math.abs(currentViewportH - previousViewportH) >= 1
      observedScrollViewportRef.current = {
        element: scrollEl,
        height: currentViewportH,
      }
      if (viewportChanged) {
        syncLayout({
          viewportChange: true,
          authorityVersion,
        })
        requestRevealOnQuiet()
        return
      }
      sizeSpacer(authorityVersion)
      // A still-neutral entry coordinate means rows painted after the initial
      // restore pass (progressive hidden slices leave `messageCount` unchanged,
      // so this effect never re-ran). Resolve it now that content exists, then
      // let the branches below apply the resulting anchor while still hidden.
      if (modeRef.current.kind === 'INITIAL') attemptEntryRestore()
      const k = modeRef.current.kind
      if (
        k === 'FOLLOW_BOTTOM'
        // Hidden transcripts may re-anchor freely. Once revealed, only the
        // conditional shift/clamp repair below may move an anchor; otherwise
        // every late renderer or font swap becomes a visible jump.
        || (k === 'ANCHOR_AT' && !revealedRef.current)
      ) {
        writeMode(
          scrollEl,
          modeRef.current,
          k === 'FOLLOW_BOTTOM'
            ? 'layout:follow-live-tail'
            : 'layout:restore-anchor',
          authorityVersion,
        )
      } else if (k === 'PIN_USER_MSG') {
        // Repair only a shifted row or a previously clamped target that has
        // become reachable. The predicate deliberately gates on the target,
        // preventing stepwise re-pins while output grows below the message.
        settlePinnedMode(authorityVersion)
      } else if (k === 'ANCHOR_AT') {
        // Post-reveal anchors use the same shift-or-clamp repair as pins;
        // steady output below an unchanged anchor remains a no-op.
        settleAnchoredMode(authorityVersion)
      }
      requestRevealOnQuiet()  // each RO firing pushes the reveal back
    })
    ro.observe(listEl)
    ro.observe(scrollEl)  // catches real scroll-viewport size changes
    // Composer height is list clearance, so it belongs to this same observer
    // transaction. Queue rows, file chips, and other footer content are all
    // descendants, so observing them separately would duplicate this signal.
    if (footRef.current) ro.observe(footRef.current)
    // ResizeObserver alone is not a sufficient reveal gate. It reports that a
    // box ALREADY changed size, so it cannot hold the reveal for asynchronous
    // renderers that have not started yet: KaTeX is loaded on demand and then
    // REPLACES raw TeX, the first syntax-highlight pass replaces code markup,
    // and webfonts swap in and rewrap. Each lands after the 50ms size-quiet
    // window has elapsed, so the chat is revealed and the reader then watches
    // the position being corrected under them — the reported "we land
    // somewhere and then it moves while the chat is open".
    //
    // Mount stabilization therefore also waits for DOM activity to stop, and
    // for in-flight media to settle (a reserved frame gains its image without
    // its own box changing). Both were present before the tech-debt refactor
    // and removing them is what made entry visibly unstable.
    if (mountStabilizingRef.current && typeof MutationObserver !== 'undefined') {
      mountMutationObserver = new MutationObserver(requestRevealOnQuiet)
      mountMutationObserver.observe(listEl, { childList: true, subtree: true })
    }
    scrollEl.addEventListener('load', requestRevealOnQuiet, true)
    scrollEl.addEventListener('error', requestRevealOnQuiet, true)

    // User-gesture detection. The scroll event itself stays intentionally
    // cheap: it records ownership/intent, then one shared settlement pass runs
    // at native scrollend or the guaranteed quiet edge, whichever comes first.
    // Anchor discovery, spacer measurement, mode transition, and persistence
    // run once at the trailing edge instead of on every compositor frame.
    let readerScrollDirty = false
    // Latches once the gesture reaches the follow-stick band (70px of the tail).
    let readerGestureReachedBottom = false
    // use-stick-to-bottom's escapedFromLock: set by an explicit scroll UP,
    // cleared by a scroll DOWN. The sole signal that breaks an engaged follow.
    let readerGestureEscaped = false
    // Baseline for directionless native scrolling, chiefly a mouse scrollbar
    // drag. Input captures the pre-scroll position; each owned scroll frame
    // advances it without doing any DOM traversal or layout measurement.
    let readerGestureLastScrollTop = null
    let readerGestureSequence = null
    let readerSettleTimer = 0
    let disclosureInputOwnsGesture = false

    const discardPendingReaderSettle = () => {
      clearTimeout(readerSettleTimer)
      readerSettleTimer = 0
      readerScrollDirty = false
      readerGestureReachedBottom = false
      readerGestureEscaped = false
      readerGestureLastScrollTop = null
      disclosureInputOwnsGesture = false
    }
    discardPendingReaderSettleRef.current = discardPendingReaderSettle

    const settleReaderScroll = () => {
      clearTimeout(readerSettleTimer)
      readerSettleTimer = 0
      if (!readerScrollDirty) return
      readerScrollDirty = false
      const settledReachedNearBottom = readerGestureReachedBottom
      const settledEscaped = readerGestureEscaped
      readerGestureReachedBottom = false
      readerGestureEscaped = false
      readerGestureLastScrollTop = null

      // The quiet edge is the gesture/layout ownership handoff. Compute the
      // final semantic location before replaying any deferred layout observer;
      // otherwise a stale FOLLOW_BOTTOM could write once just before the
      // reader's settled ANCHOR_AT lands.
      gestureWindowUntilRef.current = 0
      clearTimeout(pendingGestureTimerRef.current)
      pendingGestureTimerRef.current = 0
      cancelAnimationFrame(pendingGestureReleaseRafRef.current)
      pendingGestureReleaseRafRef.current = 0

      if (!loadingOlderRef.current) {
        // Snapshot the exact physical position for an ordinary hold. Reaching
        // the physical tail instead enters FOLLOW_BOTTOM, including while
        // latest-turn reservation remains.
        const holdMode = anchorModeFromScroll(scrollEl)
        const wasFollowing = modeRef.current.kind === 'FOLLOW_BOTTOM'
        const settledMode = modeAfterReaderGesture({
          escaped: settledEscaped,
          reachedNearBottom: settledReachedNearBottom,
          wasFollowing,
          holdMode,
        })
        transitionMode(
          settledMode,
          settledMode.kind === 'FOLLOW_BOTTOM'
            ? 'reader:scroll-bottom'
            : 'reader:hold-exact',
        )
        persistMode()
        // Tail geometry, not mode or viewport visibility, owns reservation.
        // Recompute once after momentum, never underneath the gesture itself.
        // When a footer resize was deferred, replay geometry + the newly
        // settled semantic mode atomically; a bare sizeSpacer here would let
        // native scroll anchoring paint an intermediate displaced frame.
        if (!replayDeferredLayoutNow()) {
          sizeSpacer(currentAuthority())
        }
      }

      recordTrace('events', 'reader:scroll-settled', { scrollEl })
      resumeLayoutAfterGestureRef.current?.()
      requestRevealOnQuiet()
    }

    const releasePendingGesture = (sequence) => {
      if (gestureSequenceRef.current !== sequence
          || gestureWindowUntilRef.current !== Number.POSITIVE_INFINITY) return
      disclosureInputOwnsGesture = false
      readerGestureLastScrollTop = null
      gestureWindowUntilRef.current = 0
      clearTimeout(pendingGestureTimerRef.current)
      pendingGestureTimerRef.current = 0
      recordTrace('events', 'reader:no-scroll-release', { scrollEl })
      resumeLayoutAfterGestureRef.current?.()
    }
    const scheduleNoScrollRelease = () => {
      if (gestureWindowUntilRef.current !== Number.POSITIVE_INFINITY) return
      // Once a real scroll has landed, quiet-settlement (not pointer/touch end)
      // owns the handoff so inertial momentum remains protected.
      if (readerScrollDirty) return
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
    const claimPhysicalTailFollow = () => {
      if (modeRef.current?.kind === 'FOLLOW_BOTTOM') return
      // This gesture expressed tail intent but produced no scroll. Keep the
      // generation captured by a queued send: only an actual reader scroll may
      // supersede that send-time decision.
      readerLocationExplicitRef.current = true
      transitionMode({ kind: 'FOLLOW_BOTTOM' }, 'reader:scroll-bottom')
      persistMode()
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
      const inputDirection = readerInputEscapeDirection(event?.type, {
        deltaY: event?.deltaY,
        key: event?.key,
        shiftKey: event?.shiftKey,
      })
      if (!activatesDisclosure && nestedReaderTargetOwnsInput({
        type: event?.type,
        key: event?.key,
        target: event?.target,
        scrollEl,
        direction: inputDirection,
      })) return
      if (activatesDisclosure && readerScrollDirty) {
        // A disclosure is a newer semantic reading action. First commit the
        // preceding gesture's actual location; otherwise a stale FOLLOW_BOTTOM
        // can be latched before the disclosure changes layout.
        settleReaderScroll()
      }
      // Wheel and scroll-key paths need the same geometry again for their
      // no-scroll release decision. Memoize it inside this one input so the
      // clamped-tail follow check does not force a second transcript layout.
      let inputGeometry = null
      const readInputGeometry = () => {
        if (inputGeometry) return inputGeometry
        const scrollTop = scrollEl.scrollTop
        const scrollHeight = scrollEl.scrollHeight
        const clientHeight = scrollEl.clientHeight
        inputGeometry = {
          deltaY: event?.deltaY,
          scrollTop,
          scrollHeight,
          clientHeight,
          distanceToBottom: scrollHeight - scrollTop - clientHeight,
        }
        return inputGeometry
      }
      const readerAlreadyOwns = !layoutMayOwnScroll(
        gestureWindowUntilRef.current,
        performance.now(),
      )
      // Escape latch (use-stick-to-bottom): a fresh gesture starts un-escaped,
      // then this input's own vertical direction sets it (scroll up) or clears
      // it (scroll down). Read straight from the event so a single wheel tick or
      // arrow press flips follow immediately, at zero layout cost. Disclosure
      // taps carry no scroll direction and must not disturb the latch.
      if (!activatesDisclosure) {
        if (!readerAlreadyOwns) readerGestureEscaped = false
        // Every input is a fresh pre-scroll sample. This is redundant for
        // wheel/keys (their event carries direction) but load-bearing for a
        // scrollbar pointerdown, whose later scroll event is the first place
        // the browser reveals which way the reader moved.
        readerGestureLastScrollTop = scrollEl.scrollTop
        if (inputDirection === 'up') readerGestureEscaped = true
        else if (inputDirection === 'down') readerGestureEscaped = false
        // A downward wheel/key press at the physical clamp cannot produce the
        // scroll event that normally commits FOLLOW_BOTTOM. Claim the same
        // semantic tail intent here instead of silently releasing it next frame.
        if (inputDirection === 'down' && readerInputClaimsPhysicalTail(
          inputDirection,
          readInputGeometry().distanceToBottom,
        )) {
          claimPhysicalTailFollow()
        }
      }
      if (!readerAlreadyOwns || activatesDisclosure) {
        recordTrace('events', `reader:input-${event?.type || 'unknown'}`, {
          captureGeometry: false,
        })
      }
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
      // close under a busy renderer. Keep layout ownership suspended until that
      // first event actually lands; real scrolling then owns the viewport until
      // its quiet-settle edge. A bounded fallback releases taps/keys that never
      // produce any scroll at all.
      const sequence = gestureSequenceRef.current + 1
      gestureSequenceRef.current = sequence
      disclosureInputOwnsGesture = activatesDisclosure
      gestureWindowUntilRef.current = Number.POSITIVE_INFINITY
      clearTimeout(pendingGestureTimerRef.current)
      cancelAnimationFrame(pendingGestureReleaseRafRef.current)
      pendingGestureReleaseRafRef.current = 0
      pendingGestureTimerRef.current = setTimeout(() => {
        releasePendingGesture(sequence)
      }, PENDING_GESTURE_CAP_MS)
      // Thunk, not an object literal: these property reads force a synchronous
      // layout, and only wheel/scroll-key branches consume them. See
      // readerInputNeedsFrameRelease.
      // Space/Enter on a disclosure button cannot natively scroll; preserve its
      // existing one-frame release rather than waiting for the safety cap.
      const keyboardDisclosure = activatesDisclosure && event?.type === 'keydown'
      if (keyboardDisclosure || readerInputNeedsFrameRelease(
          event?.type,
          readInputGeometry,
          event?.key,
          event?.shiftKey,
        )) {
        scheduleNoScrollRelease()
      }
    }
    // Reuse the exact input handler when the opt-in field probe is disabled;
    // the hot path then carries no timing closure or extra wrapper.
    const onWheelInput = isPerfProbeEnabled()
      ? (event) => perfTime('scroll.wheel', () => onUserInput(event))
      : onUserInput

    // Scroll-START latency, measured rather than inferred. Lag at the moment a
    // finger lands is a different failure from steady-state jank and has a
    // different cause: the work between touch pointerdown and the first frame
    // that actually moves. Stamped on pointerdown, consumed by that gesture's first
    // scroll event, so the recorded value is exactly the gap a reader feels.
    let pendingGestureStart = 0
    let touchStartY = null
    let touchStartTarget = null
    let touchEndChecked = false
    const onPointerDownInput = (event) => {
      if (event.pointerType === 'touch') {
        touchStartY = Number.isFinite(event.clientY) ? event.clientY : null
        touchStartTarget = event.target
        touchEndChecked = false
        if (isPerfProbeEnabled()) {
          pendingGestureStart = performance.now()
          perfTime('scroll.touchstart', () => onUserInput(event))
          return
        }
      }
      onUserInput(event)
    }
    // Pointerdown already owns an ordinary gesture. Client-coordinate math is
    // the only per-move work. Once per touch, a meaningful swipe toward the end
    // may claim FOLLOW_BOTTOM even when the browser is already clamped and can
    // emit no scroll event; this is how a pinned reader explicitly asks to
    // follow before the reserved reply room has been consumed.
    const onPointerMoveInput = (event) => {
      if (event.pointerType !== 'touch') return
      const fingerDelta = touchStartY == null ? 0 : touchStartY - event.clientY
      const touchDirection = fingerDelta >= 12
        ? 'down'
        : fingerDelta <= -12 ? 'up' : null
      if (touchDirection && nestedReaderTargetOwnsInput({
        type: 'pointermove',
        target: touchStartTarget || event.target,
        scrollEl,
        direction: touchDirection,
      })) return
      if (!touchEndChecked
          && !readerScrollDirty
          && touchStartY != null
          && touchDirection === 'down') {
        touchEndChecked = true
        const distanceToBottom = scrollEl.scrollHeight
          - scrollEl.scrollTop
          - scrollEl.clientHeight
        if (readerInputClaimsPhysicalTail('down', distanceToBottom)) {
          claimPhysicalTailFollow()
        }
      }
      // Re-arm the safety gate if a deliberately long (>2s) stationary touch
      // outlived the no-scroll dead-man before moving.
      if (gestureWindowUntilRef.current !== Number.POSITIVE_INFINITY
          && !readerScrollDirty) onUserInput(event)
      // Touch escape latch: a finger moving UP drags content toward the tail —
      // that is scrolling DOWN, which re-engages follow; a finger moving down is
      // scrolling UP and escapes. Applied last so the stationary-touch re-arm
      // above (a fresh onUserInput claim) cannot clobber the direction.
      if (touchDirection === 'down') readerGestureEscaped = false
      else if (touchDirection === 'up') readerGestureEscaped = true
    }
    const onPointerUpInput = () => {
      if (!readerScrollDirty) pendingGestureStart = 0
      touchStartY = null
      touchStartTarget = null
      touchEndChecked = false
      scheduleNoScrollRelease()
    }
    const onPointerCancelInput = () => {
      // Native panning cancels a disclosure press before its first scroll.
      // Keep the gesture gate, but let that scroll become reader-owned.
      disclosureInputOwnsGesture = false
    }
    const runComposerTailIntent = (event) => {
      if (!composerTailIntentRequestsFollow(event, scrollEl)) return
      // Once FOLLOW already owns layout, later characters carry no new scroll
      // intent. Keep this a one-time handoff instead of persisting and tracing
      // the same state on every input event. An unfinished older gesture still
      // comes through so writing can supersede it below.
      if (modeRef.current?.kind === 'FOLLOW_BOTTOM'
          && layoutMayOwnScroll(
            gestureWindowUntilRef.current,
            performance.now(),
          )) return
      // Composer focus is a newer semantic action than any scroll still
      // waiting on momentum/quiet settlement. A direct edit is the same intent
      // when the field was already focused and no new press occurred. Retire
      // that gesture before the keyboard/composer resize arrives so the
      // viewport observer can apply FOLLOW in its first committed layout pass
      // instead of waiting behind the old gate.
      supersedePendingReaderGesture()
      readerLocationExplicitRef.current = true
      transitionMode({ kind: 'FOLLOW_BOTTOM' }, 'reader:composer-bottom')
      persistMode()
      recordTrace('events', 'reader:composer-bottom', { scrollEl })
    }
    const onComposerPointerDown = (event) => runComposerTailIntent(event)
    composerEditRunRef.current = runComposerTailIntent

    let pendingInlineEditorAnchor = null
    let inlineEditorRaf = 0
    const captureInlineEditorAnchor = (event) => {
      if (!isQuestionEditor(event.target)) return
      // Capture before the browser changes the textarea/caret. Typing is a
      // newer semantic action than a half-settled reader gesture, but it is not
      // new scroll intent, so the exact current anchor becomes layout owner.
      const readerGesturePending = !layoutMayOwnScroll(
        gestureWindowUntilRef.current,
        performance.now(),
      )
      const needsFreshAnchor = !readerLocationExplicitRef.current
        || modeRef.current?.kind !== 'ANCHOR_AT'
        || readerGesturePending
      // Once typing has retired FOLLOW/PIN, the current anchor is already the
      // exact contract to replay. Avoid measuring the transcript again on
      // every character unless a newer reader gesture made it stale.
      const nextMode = needsFreshAnchor
        ? anchorModeFromScroll(scrollEl) || modeRef.current
        : modeRef.current
      supersedePendingReaderGesture()
      if (needsFreshAnchor) {
        readerLocationExplicitRef.current = true
        transitionMode(nextMode, 'reader:inline-editor-growth')
        persistMode()
      }
      pendingInlineEditorAnchor = {
        mode: nextMode,
        authorityVersion: currentAuthority(),
      }
    }
    const restoreInlineEditorAnchor = (event) => {
      if (!isQuestionEditor(event.target)) return
      const plan = pendingInlineEditorAnchor
      pendingInlineEditorAnchor = null
      if (!plan) return
      cancelAnimationFrame(inlineEditorRaf)
      inlineEditorRaf = requestAnimationFrame(() => {
        inlineEditorRaf = 0
        writeMode(
          scrollEl,
          plan.mode,
          'layout:inline-editor-growth',
          plan.authorityVersion,
        )
      })
    }
    const noteScrollStart = () => {
      if (!pendingGestureStart) return
      perfMark('scroll.startLatency', performance.now() - pendingGestureStart)
      pendingGestureStart = 0
    }

    scrollEl.addEventListener('pointerdown', onPointerDownInput, { passive: true })
    scrollEl.addEventListener('pointermove', onPointerMoveInput, { passive: true })
    scrollEl.addEventListener('wheel', onWheelInput, { passive: true })
    scrollEl.addEventListener('keydown', onUserInput, { passive: true })
    scrollEl.addEventListener('beforeinput', captureInlineEditorAnchor, { passive: true })
    scrollEl.addEventListener('input', restoreInlineEditorAnchor, { passive: true })
    scrollEl.addEventListener('pointerup', onPointerUpInput, { passive: true })
    scrollEl.addEventListener('pointercancel', onPointerCancelInput, { passive: true })
    chatEl?.addEventListener('pointerdown', onComposerPointerDown, { passive: true })

    // Scroll handler — user-driven scrolls only mark intent here. The expensive
    // semantic location/mode work runs once in settleReaderScroll.
    const onScroll = () => {
      // First scroll event of a touch gesture closes the start-latency window
      // opened on pointerdown. Placed before the early returns below so the
      // measurement reflects when content actually moved, not whether this
      // controller classified the movement as reader-driven.
      noteScrollStart()
      const userDriven = performance.now() < gestureWindowUntilRef.current
      if (!userDriven) {
        return
      }
      transientSearchRevealRef.current = false
      const distanceToBottom = scrollEl.scrollHeight
        - scrollEl.scrollTop
        - scrollEl.clientHeight
      // Disclosure expansion/collapse can produce a browser scroll while its
      // DOM changes. Its pre-toggle mode snapshot owns that movement; it is not
      // a fresh reader gesture and must not start a 250ms settlement that later
      // overrides FOLLOW_BOTTOM or the latched reading anchor.
      if (disclosureInputOwnsGesture) return
      if (loadingOlderRef.current) {
        // Pagination owns its prepend anchor. Do not leave the gesture gate at
        // Infinity when its reconciliation scroll lands during the load.
        gestureWindowUntilRef.current = 0
        clearTimeout(pendingGestureTimerRef.current)
        pendingGestureTimerRef.current = 0
        cancelAnimationFrame(pendingGestureReleaseRafRef.current)
        pendingGestureReleaseRafRef.current = 0
        readerGestureLastScrollTop = null
        resumeLayoutAfterGestureRef.current?.()
        return
      }
      const firstOwnedScroll = !readerScrollDirty
      if (firstOwnedScroll) {
        recordTrace('events', 'reader:scroll-start', { captureGeometry: false })
        clearTimeout(pendingGestureTimerRef.current)
        pendingGestureTimerRef.current = 0
        cancelAnimationFrame(pendingGestureReleaseRafRef.current)
        pendingGestureReleaseRafRef.current = 0
      }
      readerScrollDirty = true
      const scrollEscapeDir = readerScrollEscapeDirection(
        readerGestureLastScrollTop,
        scrollEl.scrollTop,
      )
      readerGestureLastScrollTop = scrollEl.scrollTop
      if (scrollEscapeDir === 'up') readerGestureEscaped = true
      else if (scrollEscapeDir === 'down') readerGestureEscaped = false
      const intent = readerIntentAfterScroll({
        gestureSequence: gestureSequenceRef.current,
        claimedSequence: readerGestureSequence,
        version: readerIntentVersionRef.current,
        reachedBottom: readerGestureReachedBottom,
        // Follow-stick band, not the pixel-exact clamp: reaching within 70px of
        // the tail during the gesture counts as "reached the bottom" so a fast
        // stream can't shake the reader out of engaging follow.
        atBottom: distanceToBottom <= FOLLOW_STICK_BAND_PX,
      })
      readerGestureSequence = intent.claimedSequence
      readerGestureReachedBottom = intent.reachedBottom
      readerIntentVersionRef.current = intent.version
      readerLocationExplicitRef.current = true
      // Exposing `onscrollend` does not guarantee a terminal event for every
      // interrupted or disclosure-adjacent scroll. Keep one idempotent owner
      // and let the quiet edge guarantee its call; a native event merely
      // completes the same path sooner.
      clearTimeout(readerSettleTimer)
      readerSettleTimer = setTimeout(settleReaderScroll, GESTURE_SETTLE_MS)
    }
    scrollEl.addEventListener('scroll', onScroll, { passive: true })
    scrollEl.addEventListener('scrollend', settleReaderScroll, { passive: true })

    // Hide-then-reveal: kick off the quiet-debounce path immediately
    // (reveals ~50ms after the last RO firing, smoothing out
    // late-settling renderers like markdown/KaTeX/question cards).
    // The absolute reveal deadline is owned by the chatId-only effect above,
    // so message and tool churn cannot reset it.
    if (!revealedRef.current || mountStabilizingRef.current) {
      requestRevealOnQuiet()
    }

    return () => {
      // A dependency change or chat exit can arrive inside the quiet window.
      // Commit the reader's final DOM location while this effect still owns the
      // mounted nodes rather than carrying a stale FOLLOW_BOTTOM/PIN mode into
      // the next instance.
      settleReaderScroll()
      clearTimeout(readerSettleTimer)
      if (discardPendingReaderSettleRef.current === discardPendingReaderSettle) {
        discardPendingReaderSettleRef.current = null
      }
      clearTimeout(revealTimer)
      clearTimeout(deferredGestureLayoutTimer)
      if (resumeLayoutAfterGestureRef.current === resumeLayoutAfterGesture) {
        resumeLayoutAfterGestureRef.current = null
      }
      mountMutationObserver?.disconnect()
      scrollEl.removeEventListener('load', requestRevealOnQuiet, true)
      scrollEl.removeEventListener('error', requestRevealOnQuiet, true)
      ro.disconnect()
      if (paneResizeRunRef.current === runPaneResize) paneResizeRunRef.current = null
      scrollEl.removeEventListener('scroll', onScroll)
      scrollEl.removeEventListener('scrollend', settleReaderScroll)
      scrollEl.removeEventListener('pointerdown', onPointerDownInput)
      scrollEl.removeEventListener('pointermove', onPointerMoveInput)
      scrollEl.removeEventListener('wheel', onWheelInput)
      scrollEl.removeEventListener('keydown', onUserInput)
      scrollEl.removeEventListener('beforeinput', captureInlineEditorAnchor)
      scrollEl.removeEventListener('input', restoreInlineEditorAnchor)
      scrollEl.removeEventListener('pointerup', onPointerUpInput)
      scrollEl.removeEventListener('pointercancel', onPointerCancelInput)
      chatEl?.removeEventListener('pointerdown', onComposerPointerDown)
      if (composerEditRunRef.current === runComposerTailIntent) {
        composerEditRunRef.current = null
      }
      cancelAnimationFrame(inlineEditorRaf)
      if (forceRevealRef.current === forceReveal) forceRevealRef.current = null
    }
  }, [
    messageCount,
    chatId,
    initialEntryPhase,
    onCachedCoordinateReady,
    ownsReadingPosition,
    pinModeActive,
    chatRef,
    footRef,
  ])

  // Re-hold the reading position after an atomic catch-up commit lands
  // post-reveal (contract v2 item 2, lever 3 — cloak the commit). The in-place
  // reconcile keeps DOM identity but can still re-settle heights, so a real
  // reconnect (Path B) or a Path-A commit after the reveal cap must not shift
  // what the reader was looking at. Before reveal, hide-then-reveal already owns
  // the position, so this no-ops; a quick-wake kept socket produces no commit,
  // so the caller never invokes it. FOLLOW_BOTTOM/ANCHOR_AT only —
  // PIN_USER_MSG settles via its own RO branch.
  const reapplyActiveMode = useCallback(() => {
    if (!revealedRef.current) return
    const scrollEl = scrollRef.current
    if (!scrollEl) return
    const authorityVersion = readerIntentVersionRef.current
    if (!scrollAuthorityAllowsCommit({
      capturedVersion: authorityVersion,
      currentVersion: readerIntentVersionRef.current,
      gestureWindowUntil: gestureWindowUntilRef.current,
      now: performance.now(),
    })) return
    const k = modeRef.current.kind
    if (k === 'FOLLOW_BOTTOM' || k === 'ANCHOR_AT') {
      writeMode(
        scrollEl,
        modeRef.current,
        'lifecycle:catch-up-reapply',
        authorityVersion,
      )
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
    const terminalAuthorityVersion = readerIntentVersionRef.current
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
      const authority = terminalLayoutAuthority({
        capturedVersion: terminalAuthorityVersion,
        currentVersion: readerIntentVersionRef.current,
        gestureWindowUntil: gestureWindowUntilRef.current,
        now: performance.now(),
      })
      if (authority === 'stale') return
      if (authority === 'wait') {
        // Input alone is not reader movement. Keep the pin armed while a tap
        // or the input-to-first-scroll handoff owns the viewport. A real scroll
        // advances the generation and makes this plan stale; a no-scroll input
        // releases the gate and lets the ordinary committed-layout decision run.
        requestAnimationFrame(inspectCommittedLayout)
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
        scrollEl, listEl, lastUserEl, mode,
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
      if (!scrollAuthorityAllowsCommit({
        capturedVersion: terminalAuthorityVersion,
        currentVersion: readerIntentVersionRef.current,
        gestureWindowUntil: gestureWindowUntilRef.current,
        now: performance.now(),
      })) return
      spacerEl.style.height = `${spacerH}px`
      transitionMode(nextMode, spacerH <= 1
        ? 'terminal:reservation-filled'
        : 'terminal:short-reply-settle')
      writeMode(
        scrollEl,
        modeRef.current,
        'terminal:settle-layout',
        terminalAuthorityVersion,
      )
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
  const paneResized = useCallback(() => {
    const run = paneResizeRunRef.current
    if (run) run()
  }, [])

  const composerEdited = useCallback((event) => {
    composerEditRunRef.current?.(event)
  }, [])

  return {
    gestureWindowUntilRef,
    revealed,
    following,
    followLatest,
    anchorPagination,
    captureSendIntent,
    commitSendIntent,
    freezeForegroundReturn,
    freezeQuestionSubmission,
    freezeQueuedSubmission,
    revealConversationTail,
    revealAnchor,
    reapplyActiveMode,
    settleSendIntent,
    settleStreamingPin,
    composerEdited,
    paneResized,
  }
}
