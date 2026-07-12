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
 *   { kind: 'PIN_USER_MSG', cid }  — user msg at top (post-send), keyed
 *                                    on the stable client `cid` (data-cid)
 *   { kind: 'FOLLOW_BOTTOM' }     — sticky-bottom for streaming
 *   { kind: 'ANCHOR_AT', key, offset }  — anchored at a specific msg
 *
 * Bottom detection: the load-bearing at-bottom decision for
 * send-pinning is scrollHeight math (`shouldPinSend` →
 * `isNearContentBottom`, read from `scrollTop` before the append). The
 * dynamic pin spacer is reserved room, not message content: if the reader is
 * at the bottom of the real conversation, the next send should still pin.
 * The IntersectionObserver on a sentinel at the end of `.chat__scroll` is
 * used only for the gesture-driven mode transition (engaging
 * FOLLOW_BOTTOM when the user scrolls to the physical bottom), not for the
 * send-pin decision.
 *
 * User-gesture detection: pointerdown/wheel/touchstart/keydown open a
 * 250ms window in which scroll events are user-driven and can
 * transition the mode. Outside the window, scrolls come from our
 * applyMode or browser clamps and are ignored.
 *
 * See ARCHITECTURE.md "Chat scroll + steer contract" for the full design.
 */

import { useState, useRef, useLayoutEffect } from 'react'
import { cidOf } from './chatRuntimeState.js'


// Hide-then-reveal safety cap. Code-block-heavy chats with KaTeX and
// highlight.js settle in the 500-1200ms range; a too-tight cap would
// reveal before ANCHOR_AT-restored scroll positions get re-anchored to
// the post-settle target offsets. Live streaming never reaches the
// 50ms quiet window anyway, so the cap is mostly about giving lazy
// renderers room to land before the chat becomes visible.
const REVEAL_CAP_MS = 1500

// User gesture window — scroll events fired within this window of a
// pointerdown/wheel/touchstart/keydown are treated as user-driven.
// Outside the window, scrolls are our own applyMode or browser
// clamps and MUST NOT mutate mode.
const GESTURE_WINDOW_MS = 250

// IntersectionObserver debounce — momentum-scroll can flip the
// bottom-sentinel's isIntersecting state multiple times in rapid
// succession. Take the state at the end of a quiescent window.
const IO_DEBOUNCE_MS = 50

// Per-chat ScrollMode persistence in sessionStorage.
const _scrollModes = (() => {
  try { return JSON.parse(sessionStorage.getItem('chat-mode') || '{}') }
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
      // No-op when content doesn't overflow — otherwise we'd lock
      // scroll-up. The user can scroll freely on a short chat.
      if (scrollEl.scrollHeight > scrollEl.clientHeight + 4) {
        scrollEl.scrollTop = scrollEl.scrollHeight
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


/** Validates a saved ScrollMode against current state.
 *  Degrades to FOLLOW_BOTTOM if the anchor no longer exists. */
function _validateSavedMode(saved, messages, scrollEl) {
  if (!saved || !saved.kind) return { kind: 'FOLLOW_BOTTOM' }
  if (saved.kind === 'FOLLOW_BOTTOM') return saved
  if (saved.kind === 'PIN_USER_MSG') {
    // A legacy save carrying only {ts} (no cid, from a pre-cid session)
    // degrades to FOLLOW_BOTTOM: chat-mode lives in sessionStorage (per
    // tab), so such a save can only exist for one already-open tab spanning
    // the deploy, and the cost is one chat opening at the bottom instead of
    // restoring a pin — negligible, and it self-heals on the next send.
    if (saved.cid == null) return { kind: 'FOLLOW_BOTTOM' }
    const lastUserMsg = [...messages].reverse()
      .find(m => m.role === 'user' && !m.hidden)
    return cidOf(lastUserMsg) === saved.cid ? saved : { kind: 'FOLLOW_BOTTOM' }
  }
  if (saved.kind === 'ANCHOR_AT') {
    const sel = `[data-key="${(typeof CSS !== 'undefined' && CSS.escape)
      ? CSS.escape(saved.key) : saved.key}"]`
    return scrollEl?.querySelector(sel)
      ? saved : { kind: 'FOLLOW_BOTTOM' }
  }
  return { kind: 'FOLLOW_BOTTOM' }
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
 *  Reservation is intentionally independent from pinning. The send rule
 *  decides whether to move scrollTop (first message / already at bottom).
 *  This function always reserves enough bottom room for the latest visible
 *  user message, so keyboard open/close and later manual scrolls don't make
 *  that message lose its reachable "top of screen" position.
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


/** The height actually written to the spacer node. `_computeSpacerH` is the
 *  pure geometry; this wraps it with the one owner rule raw geometry cannot
 *  express: the reservation exists ONLY to serve a pin/send. A chat mounted
 *  idle (restored, nothing sent this session) must carry no phantom spacer —
 *  otherwise FOLLOW_BOTTOM's scroll-to-scrollHeight lands past the real
 *  content and a short completed chat sits entirely above the fold (the
 *  blank-screen-on-open bug). `active` is ChatView's spacerActive: armed by
 *  the first visible send of the session, never at mount.
 */
export function reservedSpacerH(scrollEl, listEl, lastUserMsgEl, fullViewH, {
  active = true,
} = {}) {
  if (!active) return 0
  return _computeSpacerH(scrollEl, listEl, lastUserMsgEl, fullViewH)
}


// "Near the bottom" tolerance for the send rule, matching the
// auto-follow engage threshold in CLAUDE.md "Chat UX" constraint #2. A
// reader within this many pixels of the bottom is treated as at-bottom.
const NEAR_BOTTOM_PX = 50

/** The send rule: a new user message moves to the top (PIN_USER_MSG)
 *  ONLY when it is the first message in the chat, or the user is
 *  already at the bottom (following the stream). When the user is
 *  scrolled up they are probably reading — possibly with something
 *  queued — so the send must leave their scroll position alone.
 *
 *  "At the bottom" is decided two ways, neither a raw IntersectionObserver
 *  read (appending the assistant shell hides the bottom sentinel before
 *  the first follow-write, so a sentinel read at send time would
 *  mis-classify an at-bottom reader):
 *
 *    1. The scroll position is within NEAR_BOTTOM_PX of the bottom of the
 *       real message content right now, measured from scrollTop BEFORE the
 *       new message appends. This is a position computation, not a sentinel
 *       read, and it covers a chat short enough to fit the viewport and a
 *       reader sitting at the visible tail without having made the bottom
 *       gesture. Reserved pin-spacer room is excluded for this purpose:
 *       empty room below the last message should not make the next send land
 *       mid-screen when the user is visually at the bottom of the chat.
 *
 *    2. FOLLOW_BOTTOM is only a fallback when the scroll element is not
 *       available. It is deliberately NOT authoritative when scrollEl
 *       exists: mobile browsers can move/clamp the viewport during keyboard
 *       and restore transitions without a user-gesture scroll event, leaving
 *       modeRef stale as FOLLOW_BOTTOM while the reader is visibly in the
 *       middle. The actual scroll position wins.
 */
export function shouldPinSend({
  scrollEl,
  mode,
  isFirstUserMsg,
  respectFollowMode = true,
  wasNearScrollBottom = null,
}) {
  if (isFirstUserMsg) return true
  if (typeof wasNearScrollBottom === 'boolean') return wasNearScrollBottom
  if (scrollEl) return isNearContentBottom(scrollEl)
  if (respectFollowMode && mode && mode.kind === 'FOLLOW_BOTTOM') return true
  return false
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
 *  reading gesture. If the reader was at the true scroll bottom before the
 *  keyboard moved, preserve that bottom intent. Otherwise, retire stale
 *  bottom/pin modes to the current anchor when possible so opening/closing the
 *  keyboard preserves the reader's chosen position inside reserved space. */
export function modeForViewportChange(mode, wasNearScrollBottom, anchorMode = null) {
  if (wasNearScrollBottom) return { kind: 'FOLLOW_BOTTOM' }
  if (
    anchorMode
    && (mode?.kind === 'FOLLOW_BOTTOM' || mode?.kind === 'PIN_USER_MSG')
  ) {
    return anchorMode
  }
  return mode
}


/** Foreground return (visibilitychange/pageshow/online) is not a reading
 *  gesture, so preserve the physical position the browser is already showing.
 *
 *  Owner rule: if a turn is STREAMING when the app backgrounds, the reader must
 *  come back to exactly where they were — never re-glued to a NEW tail that
 *  grew while the tab was hidden. So when `streaming`, freeze the topmost
 *  visible message as an ANCHOR_AT even at the tail: keeping FOLLOW_BOTTOM there
 *  would let the next applyMode snap to the grown bottom and yank the reader.
 *  Keep-follow-at-tail is only correct for an IDLE chat, where nothing grew
 *  while backgrounded. */
export function modeForForegroundReturn(scrollEl, { streaming = false } = {}) {
  if (!scrollEl) return null
  if (streaming) return anchorModeFromScroll(scrollEl)
  if (isNearScrollBottom(scrollEl)) return { kind: 'FOLLOW_BOTTOM' }
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
    || (isNearScrollBottom(scrollEl) ? { kind: 'FOLLOW_BOTTOM' } : null)
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
 *   {kind: 'PIN_USER_MSG', cid: string}
 *     Pin the user message with the given stable `cid` (matched via
 *     `data-cid`) to the top of the viewport (PIN_OFFSET=4 px of
 *     breathing room). Set on user send; applyMode scrolls to
 *     `userMsgEl.offsetTop - PIN_OFFSET`.
 *
 *   {kind: 'FOLLOW_BOTTOM'}
 *     Sticky-bottom for streaming. applyMode sets scrollTop =
 *     scrollHeight (only if content actually overflows). Engaged
 *     when the user scrolls to within the bottom sentinel; lost
 *     when the user scrolls up.
 *
 *   {kind: 'ANCHOR_AT', key: string, offset: number}
 *     Anchored at a specific message (`data-key="<key>"`) with
 *     `offset` pixels above the viewport top. Set when the user
 *     scrolls to a non-bottom position; degrades to FOLLOW_BOTTOM
 *     on _validateSavedMode if the anchor message no longer exists.
 *
 * The caller is expected to:
 *   - Mutate `modeRef.current = {...}` on lifecycle events:
 *     * PIN_USER_MSG{cid} when the user sends a new visible message
 *     * Reset to INITIAL / FOLLOW_BOTTOM as needed
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
 *
 * @returns {{
 *   modeRef: React.MutableRefObject<
 *     | {kind: 'INITIAL'}
 *     | {kind: 'PIN_USER_MSG', cid: string}
 *     | {kind: 'FOLLOW_BOTTOM'}
 *     | {kind: 'ANCHOR_AT', key: string, offset: number}
 *   >,
 *   gestureWindowUntilRef: React.MutableRefObject<number>,
 *   userScrollIntentVersionRef: React.MutableRefObject<number>,
 *   revealed: boolean,
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
  // True once a visible message has been sent in this chat this session. The
  // spacer reservation serves a pin/send, so a chat mounted idle (no send)
  // reserves nothing — otherwise FOLLOW_BOTTOM scrolls past the real content.
  spacerActive = false,
}) {
  const [revealed, setRevealed] = useState(false)
  const modeRef = useRef({ kind: 'INITIAL' })
  const modeChatIdRef = useRef(null)
  const bottomVisibleRef = useRef(false)
  const gestureWindowUntilRef = useRef(0)
  // Monotonic counter for actual user scroll intent. Send/steer code captures
  // this at submit time and honors a delayed pin only if the user did not
  // scroll after submitting. Programmatic applyMode scrolls must not increment
  // it, so it is bumped from pointer/wheel/touch/key input on the scroll pane,
  // not from every scroll event.
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

  const persistMode = ({ freezeToCurrentPosition = false } = {}) => {
    try {
      const mode = freezeToCurrentPosition
        ? (modeForChatExit(scrollRef.current) || modeRef.current)
        : modeRef.current
      if (mode && mode.kind !== 'INITIAL') {
        if (freezeToCurrentPosition) modeRef.current = mode
        _scrollModes[chatId] = mode
        sessionStorage.setItem('chat-mode', JSON.stringify(_scrollModes))
      }
    } catch {}
  }

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
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      window.removeEventListener('pagehide', onPageLeaving)
      window.removeEventListener('beforeunload', onPageLeaving)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [chatId])

  // Single layout effect: spacer sizing, applyMode, IntersectionObserver
  // for bottom detection, ResizeObserver for layout updates, user-gesture
  // detection, scroll handler for mode transitions, mobile keyboard
  // tracking via visualViewport. Re-runs on messages / pendingMessages
  // / chatId changes.
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
      modeRef.current = { kind: 'INITIAL' }
    }

    // Restore mode for this chat if persisted (mount-restore path).
    if (modeRef.current.kind === 'INITIAL') {
      const saved = _scrollModes[chatId]
      modeRef.current = _validateSavedMode(saved, messagesRef.current, scrollEl)
    }

    // Synchronous sentinel-rect bootstrap — without it, there's a
    // 50ms window after mount where bottomVisibleRef defaults to
    // false and a user scroll could stamp ANCHOR_AT.
    const sentinelEl = scrollEl.querySelector('.chat__bottom-sentinel')
    if (sentinelEl) {
      const sRect = sentinelEl.getBoundingClientRect()
      const cRect = scrollEl.getBoundingClientRect()
      bottomVisibleRef.current = sRect.top < cRect.bottom && sRect.bottom > cRect.top
    }
    nearScrollBottomRef.current = isNearScrollBottom(scrollEl)

    // Identity check: apply the mode ONLY when modeRef.current
    // changed since the last apply. Callers always assign a fresh
    // object (`modeRef.current = { kind: ..., ts: ... }`) when they
    // intend a transition, so === identity is the right signal.
    // Steady-state streaming (mode unchanged) won't re-pin even as
    // the layout settles around tool-block status flips, KaTeX,
    // highlight.js, and markdown re-wrap — that's the bug from
    // May 2026 where scrollTop drifted with userMsg.offsetTop. The
    // last-applied identity lives on `lastAppliedModeRef` (declared
    // above) so it survives the layout effect re-running, including
    // React 19 StrictMode's dev-time double-invoke.

    function sizeSpacer() {
      // Keep fullViewHRef authoritative at EVERY spacer sizing, not just at
      // the layout-effect entry and the RO callback start (the other two grow
      // sites). The visualViewport keyboard handler reaches sizeSpacer via
      // syncLayout BEFORE either of those grow steps runs on a keyboard-close,
      // so without this the spacer would be sized from a stale-small fullViewH
      // (the keyboard-open height) even though clientHeight has already grown
      // back. That undersizes the spacer, the pin clamps below its target, and
      // the message lands mid-viewport instead of at the top — the R5 "sent
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
      const h = reservedSpacerH(
        scrollEl, listEl, lastUserEl, fullViewHRef.current,
        { active: spacerActive },
      )
      spacerEl.style.height = `${h}px`
    }

    function maybeApplyMode() {
      if (modeRef.current !== lastAppliedModeRef.current) {
        applyMode(scrollEl, modeRef.current)
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
      applyMode(scrollEl, modeRef.current)
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
      if (viewportChange) {
        const anchor = preserveBottom ? null : anchorModeFromScroll(scrollEl)
        modeRef.current = modeForViewportChange(
          modeRef.current, preserveBottom, anchor,
        )
        persistMode()
        applyMode(scrollEl, modeRef.current)
        lastAppliedModeRef.current = modeRef.current
        if (modeRef.current.kind === 'PIN_USER_MSG') {
          const el = _pinnedUserEl(scrollEl, modeRef.current.cid)
          lastPinTopRef.current = el ? el.offsetTop : null
        } else {
          lastPinTopRef.current = null
        }
      } else if (forceApply) {
        applyMode(scrollEl, modeRef.current)
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

    // IntersectionObserver on the bottom sentinel.
    let ioBounceTimer = 0
    const io = sentinelEl ? new IntersectionObserver(entries => {
      const v = entries[0]?.isIntersecting ?? false
      clearTimeout(ioBounceTimer)
      ioBounceTimer = setTimeout(() => {
        bottomVisibleRef.current = v
      }, IO_DEBOUNCE_MS)
    }, { root: scrollEl, threshold: 0 }) : null
    if (io && sentinelEl) io.observe(sentinelEl)

    // Quiet-RO reveal debouncer. The initial reveal (below) waits for
    // the layout to be stable for ~50ms — that catches the case where
    // late renderers (markdown lexer, KaTeX, highlight.js, or just a
    // question card whose initial measurement isn't its final height)
    // cause a visible scroll adjustment AFTER the chat reveals. Capped
    // at REVEAL_CAP_MS so a perpetually-changing layout (live
    // streaming) doesn't strand the chat hidden.
    let revealTimer = 0
    const requestRevealOnQuiet = () => {
      if (revealed || revealedOnce) return
      clearTimeout(revealTimer)
      revealTimer = setTimeout(() => {
        if (scrollRef.current === scrollEl) syncLayout()
        revealedOnce = true
        setRevealed(true)
      }, 50)
    }

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
    // `revealedOnce` mirrors `revealed` at effect-start so re-runs of
    // this effect on an already-revealed chat (messages change, etc.)
    // don't re-enter the during-reveal branch and cause mid-stream
    // jitter.
    let revealedOnce = revealed
    const ro = new ResizeObserver(() => {
      if (scrollEl.clientHeight > fullViewHRef.current) {
        fullViewHRef.current = scrollEl.clientHeight
      }
      sizeSpacer()
      const k = modeRef.current.kind
      if (k === 'FOLLOW_BOTTOM'
          || (k === 'ANCHOR_AT' && !revealedOnce)) {
        applyMode(scrollEl, modeRef.current)
        nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
      } else if (k === 'PIN_USER_MSG') {
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
        //       This is the clamp-fix obligation in CLAUDE.md "Chat UX"
        //       constraint #2 — already honored for FOLLOW_BOTTOM/ANCHOR_AT,
        //       missing here. We only act when the target is now reachable
        //       (scrollHeight grew back enough); re-applying then lands the
        //       message at the top instead of futilely re-clamping.
        //
        // Neither case fights the user: a manual scroll flips the mode away
        // from PIN_USER_MSG, and streaming content BELOW the message with a
        // tall-enough scrollHeight already keeps the pin satisfied (no
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

    // User-gesture detection.
    const onUserInput = () => {
      gestureWindowUntilRef.current = performance.now() + GESTURE_WINDOW_MS
      userScrollIntentVersionRef.current += 1
    }
    scrollEl.addEventListener('pointerdown', onUserInput, { passive: true })
    scrollEl.addEventListener('touchstart', onUserInput, { passive: true })
    scrollEl.addEventListener('wheel', onUserInput, { passive: true })
    scrollEl.addEventListener('keydown', onUserInput, { passive: true })

    // Scroll handler — only user-driven scrolls mutate mode.
    const onScroll = () => {
      nearScrollBottomRef.current = isNearScrollBottom(scrollEl)
      const userDriven = performance.now() < gestureWindowUntilRef.current
      if (!userDriven) return
      if (loadingOlderRef.current) return
      const overflows = scrollEl.scrollHeight > scrollEl.clientHeight + 4
      if (!overflows) return

      // Synchronous bottom check. bottomVisibleRef is updated by the
      // IntersectionObserver with a 50ms debounce — but a fast scroll
      // gesture (user flicks to the bottom and the scroll event fires
      // before the IO has had a chance to settle) would read the
      // PREVIOUS state and incorrectly stamp ANCHOR_AT. Doing one
      // getBoundingClientRect per scroll event closes that race.
      //
      // Also: cancel any pending IO debounce write — without this,
      // a queued debounce timer carrying the OLD value (from before
      // the user's gesture) could fire 50ms later and clobber the
      // sync ref we just wrote. The next IO entry will land its
      // fresh value through the normal path.
      let atBottom = bottomVisibleRef.current
      if (sentinelEl) {
        const sRect = sentinelEl.getBoundingClientRect()
        const cRect = scrollEl.getBoundingClientRect()
        atBottom = sRect.top < cRect.bottom && sRect.bottom > cRect.top
        // Sync the ref + cancel any in-flight stale IO write.
        bottomVisibleRef.current = atBottom
        clearTimeout(ioBounceTimer)
      }

      if (atBottom) {
        modeRef.current = { kind: 'FOLLOW_BOTTOM' }
      } else {
        const anchor = anchorModeFromScroll(scrollEl)
        if (anchor) modeRef.current = anchor
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
    // Capped at REVEAL_CAP_MS so a perpetually-mutating layout
    // (live streaming) can't strand the chat hidden indefinitely.
    let safetyReveal = 0
    if (!revealed) {
      requestRevealOnQuiet()
      safetyReveal = setTimeout(() => {
        revealedOnce = true
        setRevealed(true)
      }, REVEAL_CAP_MS)
    }

    return () => {
      clearTimeout(ioBounceTimer)
      clearTimeout(safetyReveal)
      clearTimeout(revealTimer)
      io?.disconnect()
      ro.disconnect()
      scrollEl.removeEventListener('scroll', onScroll)
      scrollEl.removeEventListener('pointerdown', onUserInput)
      scrollEl.removeEventListener('touchstart', onUserInput)
      scrollEl.removeEventListener('wheel', onUserInput)
      scrollEl.removeEventListener('keydown', onUserInput)
      if (vvHandler && typeof window !== 'undefined' && window.visualViewport) {
        window.visualViewport.removeEventListener('resize', vvHandler)
        window.visualViewport.removeEventListener('scroll', vvHandler)
      }
    }
    // spacerActive is a dep so the spacer re-sizes the moment the first send
    // of the session arms the reservation (the send also changes `messages`,
    // but the flip is an explicit trigger, not an inferred one).
  }, [messages, pendingMessagesLength, chatId, spacerActive])

  return {
    modeRef,
    gestureWindowUntilRef,
    userScrollIntentVersionRef,
    revealed,
  }
}
