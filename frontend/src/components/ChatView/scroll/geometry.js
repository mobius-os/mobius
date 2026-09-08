/** DOM geometry and scroll commands for the chat scroll controller. */

import { PIN_BOTTOM_ROOM, PIN_OFFSET } from '../chatContract.js'
import { captureLayoutSpace, clientLengthToLayout } from '../../../lib/layoutSpace.js'

/** Returns the topmost intersecting message, or the last real row while the
 * viewport is inside the dynamic reservation below the transcript.
 *
 * That fallback is load-bearing for LIVE reader ownership: a gesture through
 * reserved room still needs an anchor so streaming/layout work cannot move the
 * viewport underneath the reader. Lifecycle save/restore validates that the
 * anchor intersects real content and normalizes this live-only negative offset
 * to the real transcript tail before persistence. */
export function _topmostVisibleMsg(scrollEl, scrollTop = scrollEl.scrollTop) {
  const items = scrollEl.querySelectorAll('.chat__msg[data-key]')
  const top = scrollTop
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
export function _scrollTopOf(scrollEl, el, measurement = null) {
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


/** Return the exact settled hold that makes a focused editor fully visible in
 * the usable chat viewport. The browser does not know that the composer is an
 * overlay, so its native caret reveal can stop underneath it. This derives the
 * required scrollTop without writing it; the controller still commits through
 * its ordinary mode funnel. If the editor is taller than the usable region,
 * its caret-bearing bottom edge wins. */
export function modeForInlineEditorReveal({
  scrollEl,
  editor,
  visibleTop,
  visibleBottom,
  gap = 8,
}) {
  if (!scrollEl || !editor
      || !Number.isFinite(visibleTop)
      || !Number.isFinite(visibleBottom)) return null
  const rect = editor.getBoundingClientRect?.()
  if (!Number.isFinite(rect?.top)
      || !Number.isFinite(rect?.bottom)
      || visibleBottom <= visibleTop) return null

  const topLimit = visibleTop + gap
  const bottomLimit = visibleBottom - gap
  const usableHeight = Math.max(0, bottomLimit - topLimit)
  const editorHeight = rect.bottom - rect.top
  let clientDelta = 0
  if (rect.bottom > bottomLimit) {
    clientDelta = rect.bottom - bottomLimit
  } else if (editorHeight <= usableHeight && rect.top < topLimit) {
    clientDelta = rect.top - topLimit
  }
  if (Math.abs(clientDelta) <= 0.5) return null

  const layoutDelta = clientLengthToLayout(
    clientDelta,
    captureLayoutSpace(scrollEl),
  )
  if (!Number.isFinite(layoutDelta)) return null
  const maxScrollTop = Math.max(
    0,
    scrollEl.scrollHeight - scrollEl.clientHeight,
  )
  const targetScrollTop = Math.min(
    maxScrollTop,
    Math.max(0, scrollEl.scrollTop + layoutDelta),
  )
  if (Math.abs(targetScrollTop - scrollEl.scrollTop) <= 0.5) return null
  return _anchorModeForRow(
    scrollEl,
    _topmostVisibleMsg(scrollEl, targetScrollTop),
    targetScrollTop,
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


/** Create a settled hold that reveals `targetEl` FROM ITS TOP: its top edge
 * rests `topGap` px below the usable viewport top, so a tapped question card
 * is read from the first word of its prompt rather than scrolled until only
 * its tail (the submit action) clears the composer — the physical-tail hold
 * above. When the card is taller than the viewport this deliberately leaves
 * its action below the fold, one scroll away; when it fits, the whole card
 * (prompt and action) is visible.
 *
 * The anchor addresses the keyed row that OWNS `targetEl`, folding the
 * target-vs-row offset into the ANCHOR_AT so an inner card is positioned
 * exactly. Mirrors revealAnchor's exact-target math; returns null when no
 * scroll element or owning keyed row exists so the caller can fall back. */
export function topRevealAnchorMode(scrollEl, targetEl, topGap = 0) {
  const row = targetEl?.closest?.('.chat__msg[data-key]')
  if (!scrollEl || !row?.dataset?.key) return null
  let offset = topGap
  const targetRect = targetEl.getBoundingClientRect?.()
  const rowRect = row.getBoundingClientRect?.()
  if (Number.isFinite(targetRect?.top) && Number.isFinite(rowRect?.top)) {
    const delta = clientLengthToLayout(
      targetRect.top - rowRect.top,
      captureLayoutSpace(scrollEl),
    )
    if (Number.isFinite(delta)) offset -= delta
  }
  return { kind: 'ANCHOR_AT', key: row.dataset.key, offset }
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
export function _pinnedUserEl(scrollEl, cid) {
  if (!scrollEl || cid == null) return null
  const esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(cid) : cid
  return scrollEl.querySelector(`.chat__msg--user[data-cid="${esc}"]`)
}

/** The LAST user row in the DOM — for the one spacer-geometry caller that
 *  legitimately wants "the newest user row" independent of pin identity (a
 *  transiently-null lastUserMsgRef during a render swap). Kept separate from
 *  `_pinnedUserEl` so the pin selector stays strict. */
export function _lastUserRowEl(scrollEl) {
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
export function _anchorRow(scrollEl, key) {
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
export function _anchorEl(scrollEl, mode) {
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


export function _durableQuestionSubmissionMode(mode) {
  if (!isQuestionSubmissionMode(mode)) return mode
  const {
    questionSubmitBaseMode: _transientBaseMode,
    ...durable
  } = mode
  return durable
}


/** A submitted in-message question temporarily overlays the reader's prior
 * mode. The overlay is semantic rather than viewport-sized: keyboard, toolbar,
 * pane, and orientation geometry must all preserve it until visible response
 * activity or newer reader intent explicitly releases it. */
export function isQuestionSubmissionMode(mode) {
  return mode?.kind === 'ANCHOR_AT'
    && Object.hasOwn(mode, 'questionSubmitBaseMode')
}


/** A focused custom Q&A answer gives the browser one narrow exception to the
 * ordinary viewport-resize rule: native caret reveal becomes the new exact
 * reading hold instead of being overwritten by the pre-keyboard anchor.
 * Stronger send pins, live following, and the question-submission overlay keep
 * their existing ownership contracts. */
export function modeForQuestionEditingViewportChange(mode, caretAnchor = null) {
  if (mode?.kind !== 'ANCHOR_AT'
      || isQuestionSubmissionMode(mode)
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



/** Spacer height needed so the latest user message can sit near the
 *  top of the viewport, with the PIN_OFFSET breathing room above it, or so a
 *  transient question-submit anchor remains reachable through responsive
 *  viewport changes.
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
 *  PIN_USER_MSG and the transient question-submit hold may calculate that
 *  exact deficit against a larger, already observed same-width viewport so
 *  software-keyboard close cannot make the target unreachable for one paint.
 *  FOLLOW and ordinary anchors remain based on the active viewport.
 *
 *  A question-submit anchor still reserves only its exact reachability
 *  deficit. The same-width ceiling merely precomputes the imminent viewport;
 *  later changes recompute that deficit and reapply the same anchor, so
 *  submission remains visually fixed until response activity.
 */
export function _computeSpacerH(
  scrollEl,
  listEl,
  lastUserMsgEl,
  mode = null,
  { pinViewportHeight = 0 } = {},
) {
  if (!scrollEl || !listEl) return 0
  const activeViewH = scrollEl.clientHeight
  // A fresh mobile send dismisses the software keyboard. The browser can grow
  // the scroll box (and clamp scrollTop) one frame before ResizeObserver can
  // enlarge the dynamic spacer. While a pin owns the row, reserve against the
  // largest same-width viewport already observed so that imminent growth is
  // reachable before it happens. FOLLOW_BOTTOM and ordinary anchors keep using
  // the active box and therefore retain their responsive keyboard behavior.
  const viewH = mode?.kind === 'PIN_USER_MSG' || isQuestionSubmissionMode(mode)
    ? Math.max(activeViewH, Number(pinViewportHeight) || 0)
    : activeViewH
  if (isQuestionSubmissionMode(mode)) {
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


/** Keep the pin's reservation ceiling across a same-width keyboard resize.
 * Width changes and explicit pane commits establish a new layout, so they
 * reset rather than carrying portrait/pane geometry into the new frame. */
export function nextPinViewportHeight({
  previousHeight = 0,
  previousWidth = 0,
  currentHeight = 0,
  currentWidth = 0,
  committedResize = false,
} = {}) {
  if (!(Number.isFinite(currentHeight) && currentHeight > 0)) {
    return Number.isFinite(previousHeight) ? Math.max(0, previousHeight) : 0
  }
  const widthChanged = Number.isFinite(previousWidth)
    && previousWidth > 0
    && Number.isFinite(currentWidth)
    && currentWidth > 0
    && Math.abs(currentWidth - previousWidth) >= 1
  if (committedResize || widthChanged || !(previousHeight > 0)) {
    return currentHeight
  }
  return Math.max(previousHeight, currentHeight)
}
