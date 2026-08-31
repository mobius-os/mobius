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
 *   { kind: 'ANCHOR_AT', key, offset, part?, questionSubmitBaseMode? }
 *                                  — anchored at a message and, when that row
 *                                    is taller than the viewport, an ordered
 *                                    child-index path within it; `offset` is
 *                                    measured from the addressed element's
 *                                    top. An in-message question may
 *                                    temporarily preserve its submit-time
 *                                    position across responsive geometry
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
 * only content that no longer fits makes FOLLOW_BOTTOM move. PIN_USER_MSG alone
 * keeps the largest already-observed same-width height reachable, preventing
 * keyboard close from clamping the sent row for a frame before ResizeObserver
 * can react. The other explicit exception is the transient
 * question-submit anchor: it reserves exactly enough tail room to preserve the
 * same visible card offset through keyboard, toolbar, pane, and orientation
 * changes. Only visible response activity or newer reader intent releases it.
 * Gesture-driven bottom detection reads the scroll container's geometry in
 * the scroll event itself. There is no second sentinel/observer authority
 * that can lag behind the reader and contradict the current viewport.
 *
 * User-gesture detection: pointerdown/wheel/keydown hold
 * reader ownership until the first scroll event actually arrives. Real scrolls
 * keep that ownership until scrolling settles. Physical touch contact is
 * ownership by itself (contract v1.23): while any finger is on the transcript
 * — tracked through touch events, since pointer delivery stops at the
 * `pointercancel` a native pan fires — the quiet edge cannot settle, the
 * no-scroll dead-man cannot release, and every gated layout commit waits; the
 * lift restarts the quiet edge so one contact episode settles exactly once.
 * Native `scrollend` can finish
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
import { BEFORE_SHELL_RELOAD_EVENT } from '../../lib/shellReloadEvents.js'
import { isPerfProbeEnabled, perfMark, perfTime } from '../../lib/perfProbe.js'
import { captureLayoutSpace, clientLengthToLayout } from '../../lib/layoutSpace.js'
import {
  _anchorEl,
  _anchorReapplyNeeded,
  _anchorRow,
  _computeSpacerH,
  _lastUserRowEl,
  _pinReapplyNeeded,
  _pinnedUserEl,
  _scrollTopOf,
  anchorModeFromScroll,
  applyMode,
  contentHoldModeFromScroll,
  isQuestionSubmissionMode,
  modeForInlineEditorReveal,
  modeForQuestionEditingViewportChange,
  nextPinViewportHeight,
  physicalBottomAnchorModeFromScroll,
  topRevealAnchorMode,
} from './scroll/geometry.js'
import {
  FOLLOW_STICK_BAND_PX,
  composerTailIntentRequestsFollow,
  delayedSendWillPin,
  gestureLayoutRetryDelay,
  layoutMayOwnScroll,
  modeAfterQuestionResponseStart,
  modeAfterReaderGesture,
  modeAfterSpacerResize,
  modeAfterTerminalLayout,
  modeForChatExit,
  modeForDisclosureToggle,
  modeForForegroundReturn,
  modeForQuestionSubmission,
  modeForQueuedSubmission,
  modeForScrollTransition,
  nestedReaderTargetOwnsInput,
  readerInputActivatesDisclosure,
  readerInputClaimsPhysicalTail,
  readerInputEscapeDirection,
  readerInputMayScroll,
  readerInputNeedsFrameRelease,
  readerIntentAfterScroll,
  readerScrollEscapeDirection,
  scrollAuthorityAllowsCommit,
  settledPinMode,
  shouldPinSend,
  terminalLayoutAuthority,
} from './scroll/policy.js'
import {
  _modeForPersistence,
  entryRestoreDecision,
} from './scroll/restore.js'
import {
  forgetReadingPosition,
  hasReadingPosition,
  readingPositionFor,
  writeReadingPosition,
} from './scroll/readingPositions.js'


// Mount/gesture timing belongs to the browser coordinator, not scroll policy.
const REVEAL_CAP_MS = 1500
const PREPARING_REVEAL_CAP_MS = 5000
const GESTURE_SETTLE_MS = 250
const PENDING_GESTURE_CAP_MS = 2000
// Breathing room above a question card's top edge when the question nudge
// reveals it, so its prompt sits just below the top fade rather than flush
// against the viewport edge.
const QUESTION_REVEAL_TOP_GAP = 16

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

// Full reset of the reader's gesture-intent record (chat change / surface
// replacement). Mutates in place so every closure holding the ref's object
// keeps observing the one live gesture.
function _resetReaderGesture(gesture) {
  gesture.dirty = false
  gesture.reachedBottom = false
  gesture.escaped = false
  gesture.lastScrollTop = null
  gesture.claimedSequence = null
  gesture.disclosureOwns = false
  gesture.touchStartY = null
  gesture.touchStartTarget = null
  gesture.touchEndChecked = false
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
 *   exact nested coordinate can be checked, stream-catchup is an authoritative
 *   running frame whose transport replay may still reconcile in place,
 *   preparing is a hidden progressive cold render, and ready means settled
 *   authoritative history.
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
  const settlePendingReaderGestureRef = useRef(null)
  // A newer semantic action (Send, attention nudge) supersedes any reader
  // settlement still waiting on the quiet edge. The effect publishes its
  // local cancel closure here so those actions cannot be overwritten later.
  const discardPendingReaderSettleRef = useRef(null)
  // Question Submit must snapshot before it retires a pending gesture, while
  // the gesture's dirty state lives inside the layout effect. This bridge reads
  // the exact hold only on that rare semantic boundary, never in the hot scroll
  // path.
  // Monotonic generation for actual reader scroll intent. Send/steer snapshots
  // and every deferred/automatic geometry commit use this same authority:
  // once a newer gesture lands, older work can never regain ownership merely
  // because the gesture timing window later closes.
  const readerIntentVersionRef = useRef(0)
  // Live touch contact with the transcript (contract R5, v1.23): while any
  // finger is physically on the glass, the gesture cannot settle, the
  // no-scroll dead-man cannot release, and no gated layout commit may run.
  // Tracked through touch events, not pointer events, because the browser
  // fires `pointercancel` and stops pointer delivery the moment native
  // scrolling claims the gesture, while touch events keep firing through the
  // pan and `touchend`/`touchcancel` reliably mark the lift. A ref, not
  // effect-local state: a transcript row committing mid-stream re-runs the
  // main layout effect without ending the reader's physical gesture.
  const touchContactCountRef = useRef(0)
  // Gesture-intent state survives effect re-runs for the same reason: the
  // gesture belongs to the reader, not to one effect instance. Reset only by
  // settlement, supersession, discard, or chat change.
  const readerGestureRef = useRef({
    dirty: false,
    reachedBottom: false,
    escaped: false,
    lastScrollTop: null,
    claimedSequence: null,
    disclosureOwns: false,
    touchStartY: null,
    touchStartTarget: null,
    touchEndChecked: false,
  })
  // The chat reacts to the size of its actual scroll viewport, after Shell has
  // reconciled any visual-viewport overlay. Keeping the last observed geometry
  // across effect re-runs makes ResizeObserver the one keyboard/layout signal
  // and removes the old race between two direct visualViewport listeners. The
  // pinHeight ceiling pre-reserves a same-width keyboard close so the browser
  // cannot paint its native clamp one frame ahead of that observer.
  const observedScrollViewportRef = useRef({
    element: null,
    height: 0,
    width: 0,
    pinHeight: 0,
  })
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
        && initialEntryPhaseRef.current !== 'stream-catchup'
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
      touchContactActive: touchContactCountRef.current > 0,
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
            || hasReadingPosition(chatId)) return
        forgetReadingPosition(chatId)
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
        writeReadingPosition(chatId, durable)
      } else {
        forgetReadingPosition(chatId)
      }
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
    // A real wheel/touch scroll can update the viewport and reader generation
    // before its 250ms quiet settlement publishes ANCHOR_AT. Commit that
    // reader-owned location before choosing the pre-submit base; discarding the
    // settlement first would preserve a stale FOLLOW_BOTTOM and yank the reader
    // back to the tail when the answer resumes.
    settlePendingReaderGestureRef.current?.()
    const nextMode = modeForQuestionSubmission(scrollRef.current, modeRef.current)
    // Submit is a newer semantic reading action. Its current-geometry snapshot
    // must not be replaced a few milliseconds later by the quiet settlement of
    // the scroll that positioned the question card.
    readerLocationExplicitRef.current = true
    supersedePendingReaderGesture()
    const mode = transitionMode(
      nextMode,
      'send:question-freeze',
    )
    return {
      mode,
      readerIntentVersion: readerIntentVersionRef.current,
    }
  }, [scrollRef, supersedePendingReaderGesture, transitionMode])

  const resumeQuestionSubmissionOnResponse = useCallback((submission) => {
    const nextMode = modeAfterQuestionResponseStart({
      currentMode: modeRef.current,
      submission,
      currentReaderIntentVersion: readerIntentVersionRef.current,
    })
    if (nextMode === modeRef.current) return modeRef.current
    const mode = transitionMode(nextMode, 'stream:question-response-follow')
    const scrollEl = scrollRef.current
    if (mode === nextMode && scrollEl) {
      writeMode(
        scrollEl,
        mode,
        'stream:question-response-follow',
        submission.readerIntentVersion,
      )
      lastAppliedModeRef.current = mode
      persistMode()
    }
    return mode
  }, [persistMode, scrollRef, transitionMode, writeMode])

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

  // The question nudge is a stronger promise than the resume nudge: the reader
  // wants to READ the question, so land them at the TOP of the card (its
  // prompt), not the physical tail where a tall card shows only its submit
  // action with the prompt scrolled off above. Anchor the card's top a small
  // gap below the usable viewport top; a card that fits stays fully visible,
  // and a taller one leaves its action one scroll down. Falls back to the tail
  // reveal when the card node or its owning row can't be resolved. Same
  // explicit-reader bookkeeping as revealConversationTail so the programmatic
  // scroll is never mistaken for a fresh gesture that enables FOLLOW_BOTTOM.
  const revealPendingQuestion = useCallback((cardEl) => {
    const scrollEl = scrollRef.current
    if (!scrollEl) return
    const nextMode = topRevealAnchorMode(scrollEl, cardEl, QUESTION_REVEAL_TOP_GAP)
    if (!nextMode) {
      revealConversationTail()
      return
    }
    discardPendingReaderSettleRef.current?.()
    gestureWindowUntilRef.current = 0
    readerIntentVersionRef.current += 1
    const authorityVersion = readerIntentVersionRef.current
    readerLocationExplicitRef.current = true
    transitionMode(nextMode, 'reader:attention-nudge-question-top')
    writeMode(
      scrollEl,
      nextMode,
      'reader:attention-nudge-question-top',
      authorityVersion,
    )
    lastAppliedModeRef.current = nextMode
    persistMode()
  }, [persistMode, revealConversationTail, scrollRef, transitionMode, writeMode])

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
      // A chat change replaces the physical transcript: stale contact or
      // gesture intent from the previous surface must not freeze or steer the
      // new one.
      touchContactCountRef.current = 0
      _resetReaderGesture(readerGestureRef.current)
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
    const isQuestionEditor = target => (
      target?.dataset?.chatInlineEditor === 'question-answer'
    )
    const focusedQuestionEditor = () => {
      if (typeof document === 'undefined') return null
      const target = document.activeElement
      return isQuestionEditor(target) && scrollEl.contains?.(target) === true
        ? target
        : null
    }

    if (observedScrollViewportRef.current.element !== scrollEl) {
      observedScrollViewportRef.current = {
        element: scrollEl,
        height: scrollEl.clientHeight,
        width: scrollEl.clientWidth,
        pinHeight: scrollEl.clientHeight,
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
        saved: readingPositionFor(chatId),
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
    const touchContactActive = () => touchContactCountRef.current > 0
    const layoutOwnsScroll = (authorityVersion = currentAuthority()) => (
      scrollAuthorityAllowsCommit({
        capturedVersion: authorityVersion,
        currentVersion: readerIntentVersionRef.current,
        gestureWindowUntil: gestureWindowUntilRef.current,
        now: performance.now(),
        touchContactActive: touchContactActive(),
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
        { pinViewportHeight: observedScrollViewportRef.current.pinHeight },
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

    /** Keep the focused answer fully visible above the overlaid composer, then
     * record that exact position as the ordinary reading hold. Keyboard resize,
     * native caret movement, and field growth all use this one operation. */
    function revealFocusedQuestionEditor(event, {
      editor = focusedQuestionEditor(),
      nativePositionAlreadyApplied = false,
      authorityVersion = currentAuthority(),
    } = {}) {
      if (!editor
          || focusedQuestionEditor() !== editor
          || !layoutOwnsScroll(authorityVersion)) return false
      const scrollRect = scrollEl.getBoundingClientRect?.()
      const footRect = footRef.current?.getBoundingClientRect?.()
      const visibleTop = scrollRect?.top
      const visibleBottom = Math.min(
        scrollRect?.bottom ?? Infinity,
        footRect?.top ?? Infinity,
      )
      const revealAnchor = modeForInlineEditorReveal({
        scrollEl,
        editor,
        visibleTop,
        visibleBottom,
      })
      const nextMode = modeForQuestionEditingViewportChange(
        modeRef.current,
        revealAnchor || anchorModeFromScroll(scrollEl),
      )
      if (nextMode === modeRef.current) {
        if (nativePositionAlreadyApplied
            && modeRef.current?.kind === 'ANCHOR_AT'
            && !isQuestionSubmissionMode(modeRef.current)) {
          rememberAppliedMode()
        }
        return false
      }
      readerLocationExplicitRef.current = true
      transitionMode(nextMode, event)
      persistMode()
      if (revealAnchor === nextMode) {
        applyLayoutMode(event, authorityVersion)
      } else if (nativePositionAlreadyApplied) {
        rememberAppliedMode()
      }
      return true
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
      // Responsive geometry never releases a submitted question. The same
      // semantic anchor is resized and reapplied below; only response activity
      // or newer reader intent may replace it.
      if (!layoutOwnsScroll(authorityVersion)) {
        deferLayoutUntilReaderYields(authorityVersion)
        return false
      }
      sizeSpacer(authorityVersion)
      if (viewportChange) {
        if (!isQuestionSubmissionMode(modeRef.current)) {
          revealFocusedQuestionEditor('layout:question-edit-viewport', {
            nativePositionAlreadyApplied: true,
            authorityVersion,
          })
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
        width: scrollEl.clientWidth,
        pinHeight: nextPinViewportHeight({
          currentHeight: scrollEl.clientHeight,
          currentWidth: scrollEl.clientWidth,
          committedResize: true,
        }),
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
      || initialEntryPhaseRef.current === 'stream-catchup'
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

    // Inline Q&A editing joins this same layout transaction only while focused.
    // The observer tells us when the field actually changed size; ordinary
    // characters read only scrollTop and therefore do not force layout.
    let pendingInlineEditorInput = null
    let pendingInlineEditorGrowth = null
    let inlineEditorRaf = 0
    let observedQuestionEditor = null

    const questionEditorResized = entries => entries.some(
      entry => entry.target === observedQuestionEditor,
    )

    const observeQuestionEditor = (editor) => {
      if (!isQuestionEditor(editor) || observedQuestionEditor === editor) return
      if (observedQuestionEditor) ro.unobserve?.(observedQuestionEditor)
      observedQuestionEditor = editor
      ro.observe(editor, { box: 'border-box' })
    }

    const stopObservingQuestionEditor = (editor) => {
      if (observedQuestionEditor !== editor) return
      ro.unobserve?.(editor)
      observedQuestionEditor = null
    }

    const captureInlineEditorInput = (event) => {
      if (!isQuestionEditor(event.target)) return
      observeQuestionEditor(event.target)
      // Typing supersedes an unfinished gesture settlement, but is not itself
      // reader scroll intent. Keep only the cheap pre-input scroll coordinate;
      // geometry is consulted later only if the browser actually moved.
      supersedePendingReaderGesture()
      pendingInlineEditorInput = {
        editor: event.target,
        scrollTop: scrollEl.scrollTop,
        authorityVersion: currentAuthority(),
      }
      pendingInlineEditorGrowth = {
        editor: event.target,
        mode: anchorModeFromScroll(scrollEl),
        authorityVersion: currentAuthority(),
      }
    }

    const settleInlineEditorInput = (event) => {
      if (!isQuestionEditor(event.target)) return
      const plan = pendingInlineEditorInput
      pendingInlineEditorInput = null
      if (!plan || plan.editor !== event.target) return
      cancelAnimationFrame(inlineEditorRaf)
      inlineEditorRaf = requestAnimationFrame(() => {
        // Let native caret reveal and any ResizeObserver transaction land. A
        // second frame is still layout-free unless scrollTop actually changed.
        inlineEditorRaf = requestAnimationFrame(() => {
          inlineEditorRaf = 0
          // React may commit textarea auto-sizing after this caret pass. Keep
          // the pre-input anchor until ResizeObserver consumes it, a newer
          // input replaces it, or blur retires it.
          if (Math.abs(scrollEl.scrollTop - plan.scrollTop) <= 0.5) return
          revealFocusedQuestionEditor('reader:inline-editor-caret', {
            editor: plan.editor,
            nativePositionAlreadyApplied: true,
            authorityVersion: plan.authorityVersion,
          })
        })
      })
    }
    const onInlineEditorFocus = (event) => observeQuestionEditor(event.target)
    const onInlineEditorBlur = (event) => {
      if (pendingInlineEditorGrowth?.editor === event.target) {
        pendingInlineEditorGrowth = null
      }
      stopObservingQuestionEditor(event.target)
    }

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
    const ro = new ResizeObserver((entries = []) => {
      const editorResized = questionEditorResized(entries)
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
      const previousViewportW = observedScrollViewportRef.current.width
      const currentViewportH = scrollEl.clientHeight
      const currentViewportW = scrollEl.clientWidth
      const viewportChanged = previousViewportH > 0
        && (Math.abs(currentViewportH - previousViewportH) >= 1
          || Math.abs(currentViewportW - previousViewportW) >= 1)
      observedScrollViewportRef.current = {
        element: scrollEl,
        height: currentViewportH,
        width: currentViewportW,
        pinHeight: nextPinViewportHeight({
          previousHeight: observedScrollViewportRef.current.pinHeight,
          previousWidth: previousViewportW,
          currentHeight: currentViewportH,
          currentWidth: currentViewportW,
        }),
      }
      if (viewportChanged) {
        syncLayout({
          viewportChange: true,
          authorityVersion,
        })
        if (editorResized) revealFocusedQuestionEditor(
          'layout:question-edit-resize',
          { nativePositionAlreadyApplied: true, authorityVersion },
        )
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
      if (editorResized) {
        const growth = pendingInlineEditorGrowth
        pendingInlineEditorGrowth = null
        if (growth?.editor === focusedQuestionEditor()
            && growth.mode
            && growth.authorityVersion === currentAuthority()) {
          // Textarea growth is content geometry, not a request to move the
          // conversation. Hold the card at its pre-input coordinate; once the
          // field reaches its cap, the field itself owns overflow. A genuine
          // keyboard/viewport resize takes the viewportChanged branch above
          // and still adopts the browser's caret-visible position.
          transitionMode(growth.mode, 'layout:question-edit-growth')
          applyLayoutMode('layout:question-edit-growth', authorityVersion)
        }
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
    // at native scrollend or the guaranteed quiet edge, whichever comes first —
    // and never while a finger is still on the glass (contract v1.23): the
    // touch-lift handler restarts the quiet edge, so one contact episode
    // (drag, pause, drag, lift, momentum) settles exactly once. Anchor
    // discovery, spacer measurement, mode transition, and persistence run
    // once at the trailing edge instead of on every compositor frame. The
    // gesture-intent record lives in readerGestureRef so a mid-stream effect
    // re-run (a transcript row committing) preserves the live gesture instead
    // of resetting its escape/tail latches.
    const gesture = readerGestureRef.current
    let readerSettleTimer = 0

    const discardPendingReaderSettle = () => {
      clearTimeout(readerSettleTimer)
      readerSettleTimer = 0
      gesture.dirty = false
      gesture.reachedBottom = false
      gesture.escaped = false
      gesture.lastScrollTop = null
      gesture.disclosureOwns = false
    }
    discardPendingReaderSettleRef.current = discardPendingReaderSettle
    const settleReaderScroll = () => {
      clearTimeout(readerSettleTimer)
      readerSettleTimer = 0
      // A finger on the glass holds the gesture open (contract v1.23).
      // Settling here would immediately hand layout the viewport under live
      // touch; the lift handler restarts this quiet edge instead.
      if (touchContactActive()) return
      if (!gesture.dirty) return
      gesture.dirty = false
      const settledReachedNearBottom = gesture.reachedBottom
      const settledEscaped = gesture.escaped
      gesture.reachedBottom = false
      gesture.escaped = false
      gesture.lastScrollTop = null

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
          // Spacer changes can trigger native scroll anchoring or a clamp in
          // the same frame. Re-apply the semantic mode after sizing so the
          // quiet-edge handoff preserves the exact reader coordinate it just
          // captured instead of publishing a hold and then moving underneath
          // it.
          syncLayout({ forceApply: true, authorityVersion: currentAuthority() })
        }
      }

      recordTrace('events', 'reader:scroll-settled', { scrollEl })
      resumeLayoutAfterGestureRef.current?.()
      requestRevealOnQuiet()
    }
    settlePendingReaderGestureRef.current = settleReaderScroll

    const releasePendingGesture = (sequence) => {
      if (gestureSequenceRef.current !== sequence
          || gestureWindowUntilRef.current !== Number.POSITIVE_INFINITY) return
      if (touchContactActive()) {
        // A resting finger is not an interrupted gesture (contract v1.23):
        // the dead-man may not fire through live contact. Re-arm the bounded
        // cap; lift/cancel evidence releases through the ordinary paths, and
        // the contact gate keeps layout blocked regardless.
        clearTimeout(pendingGestureTimerRef.current)
        pendingGestureTimerRef.current = setTimeout(() => {
          releasePendingGesture(sequence)
        }, PENDING_GESTURE_CAP_MS)
        return
      }
      gesture.disclosureOwns = false
      gesture.lastScrollTop = null
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
      if (gesture.dirty) return
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
      if (activatesDisclosure && gesture.dirty) {
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
        touchContactActive(),
      )
      // Escape latch (use-stick-to-bottom): a fresh gesture starts un-escaped,
      // then this input's own vertical direction sets it (scroll up) or clears
      // it (scroll down). Read straight from the event so a single wheel tick or
      // arrow press flips follow immediately, at zero layout cost. Disclosure
      // taps carry no scroll direction and must not disturb the latch.
      if (!activatesDisclosure) {
        if (!readerAlreadyOwns) gesture.escaped = false
        // Every input is a fresh pre-scroll sample. This is redundant for
        // wheel/keys (their event carries direction) but load-bearing for a
        // scrollbar pointerdown, whose later scroll event is the first place
        // the browser reveals which way the reader moved.
        gesture.lastScrollTop = scrollEl.scrollTop
        if (inputDirection === 'up') gesture.escaped = true
        else if (inputDirection === 'down') gesture.escaped = false
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
      gesture.disclosureOwns = activatesDisclosure
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
    const onPointerDownInput = (event) => {
      if (event.pointerType === 'touch') {
        gesture.touchStartY = Number.isFinite(event.clientY) ? event.clientY : null
        gesture.touchStartTarget = event.target
        gesture.touchEndChecked = false
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
      const fingerDelta = gesture.touchStartY == null
        ? 0
        : gesture.touchStartY - event.clientY
      const touchDirection = fingerDelta >= 12
        ? 'down'
        : fingerDelta <= -12 ? 'up' : null
      if (touchDirection && nestedReaderTargetOwnsInput({
        type: 'pointermove',
        target: gesture.touchStartTarget || event.target,
        scrollEl,
        direction: touchDirection,
      })) return
      if (!gesture.touchEndChecked
          && !gesture.dirty
          && gesture.touchStartY != null
          && touchDirection === 'down') {
        gesture.touchEndChecked = true
        const distanceToBottom = scrollEl.scrollHeight
          - scrollEl.scrollTop
          - scrollEl.clientHeight
        if (readerInputClaimsPhysicalTail('down', distanceToBottom)) {
          claimPhysicalTailFollow()
        }
      }
      // Re-arm the safety gate if the gesture window was superseded by a newer
      // semantic action (send, nudge) while this finger stayed on the glass.
      if (gestureWindowUntilRef.current !== Number.POSITIVE_INFINITY
          && !gesture.dirty) onUserInput(event)
      // Touch escape latch: a finger moving UP drags content toward the tail —
      // that is scrolling DOWN, which re-engages follow; a finger moving down is
      // scrolling UP and escapes. Applied last so the stationary-touch re-arm
      // above (a fresh onUserInput claim) cannot clobber the direction.
      if (touchDirection === 'down') gesture.escaped = false
      else if (touchDirection === 'up') gesture.escaped = true
    }
    const onPointerUpInput = () => {
      if (!gesture.dirty) pendingGestureStart = 0
      gesture.touchStartY = null
      gesture.touchStartTarget = null
      gesture.touchEndChecked = false
      scheduleNoScrollRelease()
    }
    const onPointerCancelInput = () => {
      // Native panning cancels a disclosure press before its first scroll.
      // Keep the gesture gate, but let that scroll become reader-owned.
      gesture.disclosureOwns = false
    }
    // Physical-contact ownership (contract v1.23). Touch events — not pointer
    // events — are the contact authority: the browser fires `pointercancel`
    // and stops pointer delivery once native scrolling claims a drag, while
    // touch events keep firing through the pan and `touchend`/`touchcancel`
    // reliably mark the lift. `touches.length` is the browser's own count of
    // fingers still on the surface, so a multi-touch episode ends exactly when
    // the last finger lifts.
    const onTouchContactChange = (event) => {
      const count = event.touches ? event.touches.length : 0
      const wasActive = touchContactCountRef.current > 0
      touchContactCountRef.current = count
      if (count > 0 || !wasActive) return
      // Last finger lifted: the trailing quiet edge begins NOW. A dirty
      // gesture settles GESTURE_SETTLE_MS after this lift; inertial momentum
      // keeps pushing that timer back through its ordinary scroll events.
      if (gesture.dirty) {
        clearTimeout(readerSettleTimer)
        readerSettleTimer = setTimeout(settleReaderScroll, GESTURE_SETTLE_MS)
        return
      }
      if (gestureWindowUntilRef.current === Number.POSITIVE_INFINITY) {
        // A no-scroll touch (tap or held finger) releases through the
        // ordinary frame-yield path now that contact has ended.
        scheduleNoScrollRelease()
        return
      }
      // Contact alone was blocking (the window was already superseded by a
      // newer semantic action): replay any layout work deferred under the
      // finger.
      resumeLayoutAfterGestureRef.current?.()
    }
    // Window-level lift/cancel delivery is the wedge guard: if the transcript
    // element is replaced mid-gesture, its own listeners never see the lift.
    const onWindowTouchContactEnd = (event) => {
      if (touchContactCountRef.current === 0) return
      onTouchContactChange(event)
    }
    // Backgrounding mid-touch loses the lift entirely; finger state is
    // unknowable on return. Clear contact and settle so the transcript cannot
    // stay frozen. There is deliberately NO timer-based contact escape: an
    // indefinitely resting finger is a legitimate reading hold, and the
    // browser's own scrollend honors it the same way.
    const onVisibilityHiddenClearContact = () => {
      if (typeof document === 'undefined'
          || document.visibilityState !== 'hidden') return
      if (touchContactCountRef.current === 0) return
      touchContactCountRef.current = 0
      settleReaderScroll()
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

    const noteScrollStart = () => {
      if (!pendingGestureStart) return
      perfMark('scroll.startLatency', performance.now() - pendingGestureStart)
      pendingGestureStart = 0
    }

    scrollEl.addEventListener('pointerdown', onPointerDownInput, { passive: true })
    scrollEl.addEventListener('pointermove', onPointerMoveInput, { passive: true })
    scrollEl.addEventListener('wheel', onWheelInput, { passive: true })
    scrollEl.addEventListener('keydown', onUserInput, { passive: true })
    scrollEl.addEventListener('focusin', onInlineEditorFocus, { passive: true })
    scrollEl.addEventListener('focusout', onInlineEditorBlur, { passive: true })
    scrollEl.addEventListener('beforeinput', captureInlineEditorInput, { passive: true })
    scrollEl.addEventListener('input', settleInlineEditorInput, { passive: true })
    scrollEl.addEventListener('pointerup', onPointerUpInput, { passive: true })
    scrollEl.addEventListener('pointercancel', onPointerCancelInput, { passive: true })
    scrollEl.addEventListener('touchstart', onTouchContactChange, { passive: true })
    scrollEl.addEventListener('touchend', onTouchContactChange, { passive: true })
    scrollEl.addEventListener('touchcancel', onTouchContactChange, { passive: true })
    window.addEventListener('touchend', onWindowTouchContactEnd, { passive: true })
    window.addEventListener('touchcancel', onWindowTouchContactEnd, { passive: true })
    document.addEventListener('visibilitychange', onVisibilityHiddenClearContact)
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
      if (gesture.disclosureOwns) return
      if (loadingOlderRef.current) {
        // Pagination owns its prepend anchor. Do not leave the gesture gate at
        // Infinity when its reconciliation scroll lands during the load.
        gestureWindowUntilRef.current = 0
        clearTimeout(pendingGestureTimerRef.current)
        pendingGestureTimerRef.current = 0
        cancelAnimationFrame(pendingGestureReleaseRafRef.current)
        pendingGestureReleaseRafRef.current = 0
        gesture.lastScrollTop = null
        resumeLayoutAfterGestureRef.current?.()
        return
      }
      const firstOwnedScroll = !gesture.dirty
      if (firstOwnedScroll) {
        recordTrace('events', 'reader:scroll-start', { captureGeometry: false })
        clearTimeout(pendingGestureTimerRef.current)
        pendingGestureTimerRef.current = 0
        cancelAnimationFrame(pendingGestureReleaseRafRef.current)
        pendingGestureReleaseRafRef.current = 0
      }
      gesture.dirty = true
      const scrollEscapeDir = readerScrollEscapeDirection(
        gesture.lastScrollTop,
        scrollEl.scrollTop,
      )
      gesture.lastScrollTop = scrollEl.scrollTop
      if (scrollEscapeDir === 'up') gesture.escaped = true
      else if (scrollEscapeDir === 'down') gesture.escaped = false
      const intent = readerIntentAfterScroll({
        gestureSequence: gestureSequenceRef.current,
        claimedSequence: gesture.claimedSequence,
        version: readerIntentVersionRef.current,
        reachedBottom: gesture.reachedBottom,
        // Follow-stick band, not the pixel-exact clamp: reaching within 70px of
        // the tail during the gesture counts as "reached the bottom" so a fast
        // stream can't shake the reader out of engaging follow.
        atBottom: distanceToBottom <= FOLLOW_STICK_BAND_PX,
      })
      gesture.claimedSequence = intent.claimedSequence
      gesture.reachedBottom = intent.reachedBottom
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

    // A reinstall inherits the previous instance's live gesture record. When
    // its quiet edge was pending without touch contact (a dep change landing
    // mid-momentum), re-arm the settle timer the cleanup cleared; under
    // contact, the lift handler owns the restart.
    if (gesture.dirty && !touchContactActive()) {
      readerSettleTimer = setTimeout(settleReaderScroll, GESTURE_SETTLE_MS)
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
      // A dependency change or chat exit can arrive inside the quiet window.
      // Commit the reader's final DOM location while this effect still owns
      // the mounted nodes rather than carrying a stale FOLLOW_BOTTOM/PIN mode
      // into the next instance. Under live touch contact this is a no-op by
      // design (contract v1.23): the gesture record survives in
      // readerGestureRef and the next instance continues it.
      settleReaderScroll()
      clearTimeout(readerSettleTimer)
      if (discardPendingReaderSettleRef.current === discardPendingReaderSettle) {
        discardPendingReaderSettleRef.current = null
      }
      if (settlePendingReaderGestureRef.current === settleReaderScroll) {
        settlePendingReaderGestureRef.current = null
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
      scrollEl.removeEventListener('focusin', onInlineEditorFocus)
      scrollEl.removeEventListener('focusout', onInlineEditorBlur)
      scrollEl.removeEventListener('beforeinput', captureInlineEditorInput)
      scrollEl.removeEventListener('input', settleInlineEditorInput)
      scrollEl.removeEventListener('pointerup', onPointerUpInput)
      scrollEl.removeEventListener('pointercancel', onPointerCancelInput)
      scrollEl.removeEventListener('touchstart', onTouchContactChange)
      scrollEl.removeEventListener('touchend', onTouchContactChange)
      scrollEl.removeEventListener('touchcancel', onTouchContactChange)
      window.removeEventListener('touchend', onWindowTouchContactEnd)
      window.removeEventListener('touchcancel', onWindowTouchContactEnd)
      document.removeEventListener('visibilitychange', onVisibilityHiddenClearContact)
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

  // Re-hold the reading position after an atomic transcript-source commit
  // lands post-reveal. Reconnect catch-up and authoritative compact reads can
  // both re-settle row heights without changing message count. Apply the mode
  // already owned by the controller before paint; ResizeObserver remains the
  // fallback for later intrinsic media/markdown growth. Before reveal,
  // hide-then-reveal already owns the position, so this no-ops.
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
      touchContactActive: touchContactCountRef.current > 0,
    })) return
    const k = modeRef.current.kind
    if (k === 'FOLLOW_BOTTOM' || k === 'ANCHOR_AT' || k === 'PIN_USER_MSG') {
      writeMode(
        scrollEl,
        modeRef.current,
        'lifecycle:transcript-reapply',
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
        touchContactActive: touchContactCountRef.current > 0,
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
        { pinViewportHeight: observedScrollViewportRef.current.pinHeight },
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
        touchContactActive: touchContactCountRef.current > 0,
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
    pinning: pinModeActive,
    following,
    followLatest,
    anchorPagination,
    captureSendIntent,
    commitSendIntent,
    freezeForegroundReturn,
    freezeQuestionSubmission,
    freezeQueuedSubmission,
    resumeQuestionSubmissionOnResponse,
    revealConversationTail,
    revealPendingQuestion,
    revealAnchor,
    reapplyActiveMode,
    settleSendIntent,
    settleStreamingPin,
    composerEdited,
    paneResized,
  }
}
