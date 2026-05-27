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
 *   { kind: 'PIN_USER_MSG', ts }  — user msg at top (post-send)
 *   { kind: 'FOLLOW_BOTTOM' }     — sticky-bottom for streaming
 *   { kind: 'ANCHOR_AT', key, offset }  — anchored at a specific msg
 *
 * Bottom detection uses an IntersectionObserver on a sentinel at the
 * end of `.chat__scroll`. No scrollHeight math — the browser tells us
 * if the user is at the bottom.
 *
 * User-gesture detection: pointerdown/wheel/touchstart/keydown open a
 * 250ms window in which scroll events are user-driven and can
 * transition the mode. Outside the window, scrolls come from our
 * applyMode or browser clamps and are ignored.
 *
 * See docs/chat-redesign.md for the full design.
 */

import { useState, useRef, useLayoutEffect } from 'react'


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


/** Element's margin-box height (offsetHeight + vertical margins).
 *  Needed for the queued tray: it sits in a flex column above
 *  .chat__form, so its bottom margin shrinks .chat__scroll just as
 *  much as its border-box does. offsetHeight alone misses that. */
function _trayMarginBox(el) {
  if (!el) return 0
  const cs = getComputedStyle(el)
  return el.offsetHeight
    + (parseFloat(cs.marginTop) || 0)
    + (parseFloat(cs.marginBottom) || 0)
}


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


/** Apply a scroll mode by setting scrollTop. Idempotent — call as
 *  often as layout changes happen. */
export function applyMode(scrollEl, mode) {
  if (!scrollEl || !mode) return
  switch (mode.kind) {
    case 'INITIAL':
      return
    case 'PIN_USER_MSG': {
      const el = scrollEl.querySelector(
        `.chat__msg--user[data-ts="${mode.ts}"]`,
      )
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


/** Validates a saved ScrollMode against current state.
 *  Degrades to FOLLOW_BOTTOM if the anchor no longer exists. */
function _validateSavedMode(saved, messages, scrollEl) {
  if (!saved || !saved.kind) return { kind: 'FOLLOW_BOTTOM' }
  if (saved.kind === 'FOLLOW_BOTTOM') return saved
  if (saved.kind === 'PIN_USER_MSG') {
    const lastUserMsg = [...messages].reverse()
      .find(m => m.role === 'user' && !m.hidden)
    return lastUserMsg?.ts === saved.ts ? saved : { kind: 'FOLLOW_BOTTOM' }
  }
  if (saved.kind === 'ANCHOR_AT') {
    const sel = `[data-key="${(typeof CSS !== 'undefined' && CSS.escape)
      ? CSS.escape(saved.key) : saved.key}"]`
    return scrollEl?.querySelector(sel)
      ? saved : { kind: 'FOLLOW_BOTTOM' }
  }
  return { kind: 'FOLLOW_BOTTOM' }
}


/** Spacer height needed so PIN_USER_MSG is achievable (the user
 *  message can scroll FLUSH to the top of the viewport, with the
 *  PIN_OFFSET breathing room above it). The spacer's only job — it
 *  does NOT touch scrollTop.
 *
 *  Formula: max(0, viewH + (lastUserMsgTop − PIN_OFFSET) − listH).
 *
 *  The (− PIN_OFFSET) must match applyMode's PIN_USER_MSG target so
 *  scrollTop-max equals the PIN target — otherwise the message
 *  lands PIN_OFFSET pixels below the top (visual "extra space" at
 *  the top + phantom over-scroll room at the bottom).
 */
const PIN_OFFSET = 4
function _computeSpacerH(scrollEl, listEl, lastUserMsgEl, fullViewH) {
  if (!scrollEl || !listEl) return 0
  const queuedTrayEl = scrollEl.parentElement?.querySelector('.queued')
  const trayH = _trayMarginBox(queuedTrayEl)
  const viewH = (fullViewH || scrollEl.clientHeight) - trayH
  const pinTarget = lastUserMsgEl
    ? Math.max(0, lastUserMsgEl.offsetTop - PIN_OFFSET) : 0
  return Math.max(0, viewH + pinTarget - listEl.offsetHeight)
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
 *   {kind: 'PIN_USER_MSG', ts: number}
 *     Pin the user message with the given ts to the top of the
 *     viewport (PIN_OFFSET=4 px of breathing room). Set on user
 *     send; applyMode scrolls to `userMsgEl.offsetTop - PIN_OFFSET`.
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
 *     * PIN_USER_MSG{ts} when the user sends a new visible message
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
 *     | {kind: 'PIN_USER_MSG', ts: number}
 *     | {kind: 'FOLLOW_BOTTOM'}
 *     | {kind: 'ANCHOR_AT', key: string, offset: number}
 *   >,
 *   gestureWindowUntilRef: React.MutableRefObject<number>,
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
}) {
  const [revealed, setRevealed] = useState(false)
  const modeRef = useRef({ kind: 'INITIAL' })
  const modeChatIdRef = useRef(null)
  const bottomVisibleRef = useRef(false)
  const gestureWindowUntilRef = useRef(0)
  const fullViewHRef = useRef(0)
  // Lives outside the layout effect so it survives StrictMode's
  // double-invoke in dev (and any future effect re-run). If this were
  // a local `let` inside the effect, the second invoke would reset it
  // to null and `maybeApplyMode()` would re-write scrollTop with the
  // same mode it already applied, visibly snapping the viewport.
  const lastAppliedModeRef = useRef(null)

  // Persist mode on every chatId change so the next mount restores.
  // (Layout effect can't easily handle persistence because it runs
  // on every messages change; cleanup is only fired on chatId change.)
  useLayoutEffect(() => {
    return () => {
      try {
        if (modeRef.current && modeRef.current.kind !== 'INITIAL') {
          _scrollModes[chatId] = modeRef.current
          sessionStorage.setItem('chat-mode', JSON.stringify(_scrollModes))
        }
      } catch {}
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
      const lastUserEl = lastUserMsgRef.current
      const h = _computeSpacerH(scrollEl, listEl, lastUserEl, fullViewHRef.current)
      spacerEl.style.height = `${h}px`
    }

    function maybeApplyMode() {
      if (modeRef.current !== lastAppliedModeRef.current) {
        applyMode(scrollEl, modeRef.current)
        lastAppliedModeRef.current = modeRef.current
      }
    }

    // Full sync — size spacer and apply-if-changed. Used at mount,
    // RO, vv, reveal. Each call sizes the spacer (always needed —
    // the spacer math depends on changing content) but only
    // touches scrollTop on a real mode transition.
    function syncLayout() {
      sizeSpacer()
      maybeApplyMode()
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
      }
      requestRevealOnQuiet()  // each RO firing pushes the reveal back
    })
    ro.observe(listEl)
    ro.observe(scrollEl)  // catches form-row growth (file chips, queue tray)
    const queuedTrayEl = scrollEl.parentElement?.querySelector('.queued')
    if (queuedTrayEl) ro.observe(queuedTrayEl)

    // User-gesture detection.
    const onUserInput = () => {
      gestureWindowUntilRef.current = performance.now() + GESTURE_WINDOW_MS
    }
    scrollEl.addEventListener('pointerdown', onUserInput, { passive: true })
    scrollEl.addEventListener('touchstart', onUserInput, { passive: true })
    scrollEl.addEventListener('wheel', onUserInput, { passive: true })
    scrollEl.addEventListener('keydown', onUserInput, { passive: true })

    // Scroll handler — only user-driven scrolls mutate mode.
    const onScroll = () => {
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
        const anchorEl = _topmostVisibleMsg(scrollEl)
        if (anchorEl?.dataset?.key) {
          modeRef.current = {
            kind: 'ANCHOR_AT',
            key: anchorEl.dataset.key,
            offset: anchorEl.offsetTop - scrollEl.scrollTop,
          }
        }
      }
    }
    scrollEl.addEventListener('scroll', onScroll, { passive: true })

    // Mobile keyboard via visualViewport.
    let vvHandler = null
    if (typeof window !== 'undefined' && window.visualViewport) {
      vvHandler = () => syncLayout()
      window.visualViewport.addEventListener('resize', vvHandler)
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
      }
    }
  }, [messages, pendingMessagesLength, chatId])

  return {
    modeRef,
    gestureWindowUntilRef,
    revealed,
  }
}
