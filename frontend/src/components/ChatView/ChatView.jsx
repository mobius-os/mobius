import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { apiFetch, getToken, BASE } from '../../api/client.js'
import { chatMessagesQueryKey } from '../../hooks/queries.js'
import { ProgressiveMarkdown } from './markdown/BlockRenderer.jsx'
import useStreamConnection from './useStreamConnection.js'
import useVoiceInput from './useVoiceInput.js'
import useFileUpload from './useFileUpload.js'
import ConnectionStatus from './ConnectionStatus.jsx'
import ToolBlock from './ToolBlock.jsx'
import QuestionCard from './QuestionCard.jsx'
import QueuedMessages from './QueuedMessages.jsx'
import MsgContent from './MsgContent.jsx'
import './ChatView.css'


/** Returns the element's margin-box height (offsetHeight + vertical
 *  margins). Needed for the queued tray: it sits in a flex column above
 *  .chat__form, so its bottom margin shrinks .chat__scroll just as
 *  much as its border-box does. offsetHeight alone misses that. */
function _trayMarginBox(el) {
  if (!el) return 0
  const cs = getComputedStyle(el)
  return el.offsetHeight
    + (parseFloat(cs.marginTop) || 0)
    + (parseFloat(cs.marginBottom) || 0)
}


// Cache touch-primary detection. Updated dynamically if input devices change.
const _touchMql = typeof matchMedia === 'function'
  ? matchMedia('(hover: none) and (pointer: coarse)')
  : null
let _isTouchPrimary = _touchMql?.matches ?? false
_touchMql?.addEventListener('change', (e) => { _isTouchPrimary = e.matches })

// Survive remounts: scroll and spacer state persisted in sessionStorage.
const _scrollPositions = (() => {
  try { return JSON.parse(sessionStorage.getItem('chat-scroll') || '{}') }
  catch { return {} }
})()
const _spacerHeights = (() => {
  try { return JSON.parse(sessionStorage.getItem('chat-spacer') || '{}') }
  catch { return {} }
})()

/** Cheap structural equality for chat-message arrays. Returns true when
 *  the lists have the same length AND the last message has the same
 *  role/content/blocks. Avoids re-renders when the background fetch
 *  returns the same data we just rendered from cache.
 *
 *  Conservative — false negatives (saying "different" when actually
 *  identical) just trigger a redundant re-render, which is the worst-
 *  case status quo. False positives (saying "same" when actually
 *  different) would cause stale rendering, so this comparison stays on
 *  the safe side: any structural difference in the last entry returns
 *  false. */
function sameBlock(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  return a.type === b.type && a.status === b.status
      && a.content === b.content && a.tool === b.tool
      && a.input === b.input && a.output === b.output
      && a.questions === b.questions && a.answers === b.answers
}

function sameMessageList(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  if (a.length !== b.length) return false
  if (a.length === 0) return true
  const la = a[a.length - 1]
  const lb = b[b.length - 1]
  if (la === lb) return true
  if (!la || !lb) return false
  if (la.role !== lb.role) return false
  if (la.content !== lb.content) return false
  const bla = la.blocks, blb = lb.blocks
  if ((bla?.length || 0) !== (blb?.length || 0)) return false
  if (bla && blb) {
    for (let i = 0; i < bla.length; i++) {
      if (!sameBlock(bla[i], blb[i])) return false
    }
  }
  return true
}


export default function ChatView({ chatId, onStreamEnd, onFirstMessage, onSystemEvent, builtApp, onOpenApp, onMessageStart }) {
  const queryClient = useQueryClient()
  // Read the query cache synchronously on mount. If we've viewed this
  // chat before, messages render immediately on remount — no empty
  // placeholder, no fetch wait, no flash. The query is then refreshed
  // in the background by the initial useEffect below.
  // Synchronous cache read on mount. If we've viewed this chat before
  // and the persister hydrated, useState starts populated → no flash.
  // The persister itself races with mount on cold load; PersistQuery-
  // ClientProvider's `onSuccess` flushes mid-flight render trees, so
  // for already-warm in-memory caches (same session) this is exact;
  // for IndexedDB-restored caches it's best-effort. The initial fetch
  // useEffect below always fires regardless and writes the fresh data
  // back via `commitMessages`, so any miss self-heals on next remount.
  const cached = queryClient.getQueryData(chatMessagesQueryKey(chatId))
  const [messages, setMessages] = useState(() => cached?.messages ?? [])
  const [pendingMessages, setPendingMessages] = useState([])
  const [offset, setOffset] = useState(() => cached?.offset ?? 0)
  const [loading, setLoading] = useState(!cached)
  const [sending, setSending] = useState(false)
  const [input, setInput] = useState(() => {
    try {
      const pending = sessionStorage.getItem('pending-draft')
      if (pending) { sessionStorage.removeItem('pending-draft'); return pending }
      return sessionStorage.getItem(`draft:${chatId}`) || ''
    } catch { return '' }
  })

  // Mirror `messages` in a ref so commitMessages can compute the next
  // value without putting a side-effect (setQueryData) inside a
  // setState updater. setState updaters must be pure; React may call
  // them multiple times during concurrent rendering. Reading from a
  // ref + calling setQueryData once outside the updater is correct.
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const pendingMessagesRef = useRef(pendingMessages)
  pendingMessagesRef.current = pendingMessages

  // Single setter that updates local state AND the query cache.
  //
  // ALWAYS writes the query cache (so even empty chats have an entry,
  // ensuring a cache hit on the next visit). Skips the React state
  // update when messages are structurally identical — that's the
  // path that was causing back-navigation jitter, because the
  // background fetch would re-set the same array reference and
  // trigger a redundant re-render of the spacer effect.
  const commitMessages = useCallback((updater, nextOffset) => {
    const prev = messagesRef.current
    const next = typeof updater === 'function' ? updater(prev) : updater
    queryClient.setQueryData(chatMessagesQueryKey(chatId), (existing) => ({
      ...(existing || {}),
      messages: next,
      offset: nextOffset !== undefined ? nextOffset : (existing?.offset ?? 0),
    }))
    if (sameMessageList(prev, next)) {
      // Offset may still have changed (older-messages pagination).
      if (nextOffset !== undefined) {
        setOffset(o => o === nextOffset ? o : nextOffset)
      }
      return
    }
    setMessages(next)
    if (nextOffset !== undefined) {
      setOffset(o => o === nextOffset ? o : nextOffset)
    }
  }, [chatId, queryClient])

  // DOM refs
  const scrollRef = useRef(null)
  const inputRef = useRef(null)
  const spacerRef = useRef(null)
  const fileInputRef = useRef(null)
  const lastUserMsgRef = useRef(null)

  // Lifecycle guards. With a cache hit, messages are populated on the
  // very first render, so:
  //   - `needsScrollRef` starts true so the *first* useLayoutEffect
  //     applies the saved scroll position synchronously, before paint.
  //     Without this, the user sees scrollTop=0 briefly, then the
  //     fetch resolves and the restoration teleports us to the saved
  //     position. That's the visible jitter on chat-back-nav.
  //   - `hadMessagesRef` reflects the cached length so `doSend`'s
  //     "first message" branch doesn't fire spuriously.
  const chatIdStaleRef = useRef(false)
  const hadMessagesRef = useRef((cached?.messages?.length ?? 0) > 0)
  const promotedRef = useRef(false)
  const needsScrollRef = useRef(!!cached)

  // Spacer state — see CLAUDE.md "Chat UX — non-negotiable constraints".
  // spacerActive: keeps min-height: 0 on the list while the spacer is active,
  //   preventing min-height: calc(100% + 1px) from inflating offsetHeight.
  const [spacerActive, setSpacerActive] = useState(false)

  // Reveal-after-scroll-settle gate. The scroll container is rendered
  // with `visibility: hidden` until scroll restoration has applied the
  // saved position AND lazy renderers (KaTeX, highlight.js) have
  // settled — so the user never sees an intermediate position. They
  // see the chat appear already at the right place, every time.
  // Reset on every fresh mount because Shell uses key={activeChatId},
  // so chat-switching is structurally unmount + mount.
  const [revealed, setRevealed] = useState(false)
  // Set in doSend, consumed by the spacer useLayoutEffect.
  const needsSpacerRef = useRef(false)
  // Persists scrollTarget across renders so promote can recalculate the spacer.
  const scrollTargetRef = useRef(null)
  // Full viewport height (keyboard closed). Set once on mount, used for spacer
  // sizing so keyboard open/close doesn't change scrollHeight.
  const fullViewHRef = useRef(0)
  // Tracks whether the user is near the bottom of the scroll area. Updated
  // by the passive scroll listener in the spacer effect. Used by
  // promoteStreamToMessages to decide whether to preserve scroll position
  // (user scrolled up to read) or allow re-anchoring (user was following).
  const nearBottomRef = useRef(false)
  // Ref mirror of `sending` for use in callbacks without adding it as a
  // dependency (avoids re-creating fetchMessages on every send).
  const sendingRef = useRef(false)
  sendingRef.current = sending

  // Bumped by handleStop (and any future hard-clear of local state)
  // so any in-flight fetchMessages can't resurrect cleared data.
  const fetchGenRef = useRef(0)

  // Re-fetch messages from the API. Called when the SSE stream reconnects
  // and gets a 204 (no active broadcast — the chat finished while the
  // user was offline or on poor connectivity). Replaces stale messages
  // with the current DB state.
  const fetchMessages = useCallback(async ({ force = false } = {}) => {
    if (sendingRef.current && !force) return
    const gen = fetchGenRef.current
    try {
      const res = await apiFetch(`/chats/${chatId}?limit=20`)
      const data = await res.json()
      if (chatIdStaleRef.current) return
      // Discard if a Stop (or other clear) bumped gen while we waited.
      if (fetchGenRef.current !== gen) return
      let msgs = data.messages || []
      for (const msg of msgs) {
        if (msg.blocks) {
          for (const blk of msg.blocks) {
            if (blk.type === 'tool' && blk.status === 'running') {
              blk.status = 'done'
            }
          }
        }
      }
      commitMessages(msgs, data.offset || 0)
      // Sync pending queue from server. Preserve any local cid if we
      // already had this entry (matched by server ts); otherwise derive
      // a stable cid from the server ts so QueuedMessages's expanded
      // state survives re-renders.
      const localByTs = new Map(
        (pendingMessagesRef.current || []).map(m => [m.ts, m.cid])
      )
      const serverPending = (data.pending_messages || []).map(m => ({
        ...m,
        cid: localByTs.get(m.ts) || `s-${m.ts}`,
        queued: true,
      }))
      pendingMessagesRef.current = serverPending
      setPendingMessages(serverPending)
    } catch { /* network error — silent, user can retry */ }
  }, [chatId, commitMessages])

  const {
    streamItems,
    latestItemsRef,
    isStreaming,
    connectionError,
    sendMessage: streamSend,
    connectToStream,
    retry,
    disconnect,
  } = useStreamConnection(chatId, {
    onStreamEnd: ({ continues, promotedTs } = {}) => {
      promoteStreamToMessages()
      if (continues) {
        // Backend auto-promoted the head of the pending queue into a
        // new turn. Mirror that locally: find the entry by ts (the
        // backend-authoritative id; the runner sends queued_turn_starting
        // with the promoted ts), append it to messages, and remove from
        // pendingMessages. Re-arm the spacer so the new user message
        // anchors at the top of the viewport.
        const queued = pendingMessagesRef.current
        const idx = promotedTs != null
          ? queued.findIndex(m => m.ts === promotedTs)
          : 0
        if (idx >= 0 && queued.length > 0) {
          const first = queued[idx]
          const rest = queued.filter((_, i) => i !== idx)
          const { queued: _q, cid: _c, position: _p, ...msg } = first
          commitMessages(prev => [...prev, msg])
          pendingMessagesRef.current = rest
          setPendingMessages(rest)
          promotedRef.current = false
          setSpacerActive(true)
          if (spacerRef.current) spacerRef.current.style.height = '0px'
          needsSpacerRef.current = true
        } else {
          // Server's promoted ts isn't in our local queue (cancel raced
          // with promote). Refetch authoritative state.
          promotedRef.current = false
          fetchMessages({ force: true })
        }
        setSending(true)
      } else {
        setSending(false)
        // Stream ended without continuation. If we have local pending
        // entries, server may have cleared them (auth fail, error) —
        // refetch to reconcile. Skip when pending empty.
        if (pendingMessagesRef.current.length > 0) {
          fetchMessages({ force: true })
        }
      }
      onStreamEnd?.()
    },
    onSystemEvent,
    onNeedsRefresh: fetchMessages,
    onQueuedTurnStarting: () => {},
  })

  // Mirror isStreaming as a ref so the ResizeObserver callback (which
  // never re-binds) can gate auto-follow on it. Tool-block toggles in
  // finished messages grow the list and were tripping auto-follow.
  const isStreamingRef = useRef(false)
  isStreamingRef.current = isStreaming

  const { files: pendingFiles, addFiles, removeFile, clearFiles } = useFileUpload({ chatId })

  const { listening, listeningRef, stopVoice, toggleVoice } = useVoiceInput({
    onTranscript: (text) => setInput(text),
    inputRef,
  })

  // Snapshot stream into a permanent message. Idempotent — both handleStop
  // and onStreamEnd may call this.
  function promoteStreamToMessages() {
    if (promotedRef.current) return
    const items = latestItemsRef.current
    if (items.length === 0) return
    promotedRef.current = true
    // Preserve scrollTargetRef so the spacer layout effect re-anchors
    // the user message at the same position after promote. Previously
    // this nulled scrollTargetRef when nearBottomRef was false, but
    // nearBottomRef is always false in the normal pinned state (user
    // message at top, response growing below). Nulling caused the
    // layout effect to skip re-anchoring, shifting scroll position.
    const blocks = items.map(item => {
      if (item.type === 'text') return { type: 'text', content: item.content }
      if (item.type === 'question') return { type: 'question', questions: item.questions }
      const status = item.status === 'running' ? 'done' : item.status
      return { type: 'tool', ...item, status }
    })
    const content = items
      .filter(i => i.type === 'text')
      .map(i => i.content)
      .join('')
    const newMsg = { role: 'assistant', content, blocks }
    commitMessages(prev => [...prev, newMsg])
  }

  // Persist draft so it survives leaving and re-entering the chat.
  useEffect(() => {
    try {
      if (input) sessionStorage.setItem(`draft:${chatId}`, input)
      else sessionStorage.removeItem(`draft:${chatId}`)
    } catch { /* quota exceeded or private browsing */ }
  }, [input, chatId])

  // Auto-size textarea when a draft is restored.
  useEffect(() => {
    const el = inputRef.current
    if (el && input) {
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 160) + 'px'
    }
  }, [chatId])

  // Fetch messages and connect to an in-progress stream if the agent is running.
  useEffect(() => {
    let cancelled = false
    chatIdStaleRef.current = false

    apiFetch(`/chats/${chatId}?limit=20`)
      .then(r => r.json())
      .then(data => {
        if (cancelled) return
        let msgs = data.messages || []

        // Drop partial assistant message when the agent is still running —
        // the SSE catch-up burst will replay those events.
        const stripped = data.running && msgs.length > 0
          && msgs[msgs.length - 1].role === 'assistant'
        if (stripped) {
          msgs = msgs.slice(0, -1)
        }

        // Normalize stale "running" tool blocks from interrupted sessions.
        for (const msg of msgs) {
          if (msg.blocks) {
            for (const blk of msg.blocks) {
              if (blk.type === 'tool' && blk.status === 'running') {
                blk.status = 'done'
              }
            }
          }
        }

        commitMessages(msgs, data.offset || 0)
        hadMessagesRef.current = msgs.length > 0
        setLoading(false)

        needsScrollRef.current = true

        // Hydrate pending queue from backend so a reload mid-queue
        // doesn't drop the visible "queued" tray. Derive a stable
        // cid from the server ts so QueuedMessages's expanded state
        // survives future re-renders.
        const serverPending = (data.pending_messages || []).map(m => ({
          ...m, cid: `s-${m.ts}`, queued: true,
        }))
        pendingMessagesRef.current = serverPending
        setPendingMessages(serverPending)

        if (data.running) {
          setSending(true)
          connectToStream(false)
        }
      })
      .catch(() => setLoading(false))

    return () => {
      try {
        // Capture current scroll + spacer from DOM at unmount, so chats
        // where the user never scrolled (no handleScroll firing) still
        // get their state persisted. Without this, a short response
        // chat that the user revisits has no saved spacer to restore
        // and the user message drops out of the top-of-viewport pin.
        const el = scrollRef.current
        if (el) {
          _scrollPositions[chatId] = el.scrollHeight - el.scrollTop
          const sp = el.querySelector('.spacer-dynamic')
          if (sp && sp.style.height) {
            _spacerHeights[chatId] = sp.style.height
          }
        }
        sessionStorage.setItem('chat-scroll', JSON.stringify(_scrollPositions))
        sessionStorage.setItem('chat-spacer', JSON.stringify(_spacerHeights))
      } catch {}
      cancelled = true
      chatIdStaleRef.current = true
      scrollTargetRef.current = null
      loadingOlder.current = false
      disconnect()
    }
  }, [chatId])

  // Restore scroll position before paint, then re-apply after 300ms to
  // correct drift from lazy renderers (KaTeX, highlight.js).
  useLayoutEffect(() => {
    // Capture full viewport height as soon as the scroll element mounts.
    // Must happen outside the needsScrollRef guard — the scroll element may
    // not exist on the initial render (empty state), so the first render
    // where it appears may not have needsScrollRef set.
    // Track the MAX clientHeight ever observed. On empty chats, the
    // .chat__scroll element doesn't exist until the first send — at
    // which point the user has been typing (keyboard open), so the
    // initial clientHeight is reduced by the keyboard. Subsequent
    // spacer math would use this too-small viewH and over-shrink the
    // spacer for every later message. By keeping the max, the value
    // snaps to the correct keyboard-closed height as soon as the
    // keyboard ever closes (e.g. after send via the blur on touch).
    const el = scrollRef.current
    if (el && el.clientHeight > fullViewHRef.current) {
      fullViewHRef.current = el.clientHeight
    }

    if (!needsScrollRef.current) {
      // Nothing to restore (e.g. brand-new chat) — reveal immediately.
      if (!revealed) setRevealed(true)
      return
    }
    needsScrollRef.current = false
    if (!el) {
      setRevealed(true)
      return
    }
    // Spacer height must be restored first — it affects scrollHeight.
    const savedSpacer = _spacerHeights[chatId]
    const sp = el.querySelector('.spacer-dynamic')
    if (savedSpacer && sp) {
      sp.style.height = savedSpacer
      setSpacerActive(true)
    }
    const saved = _scrollPositions[chatId]
    function applyScroll() {
      if (saved != null) {
        el.scrollTop = el.scrollHeight - saved
      } else {
        el.scrollTop = el.scrollHeight
      }
    }
    applyScroll()
    // Restore scrollTarget so the spacer effect sets up a ResizeObserver.
    // Without this, returning to a streaming chat leaves the spacer frozen
    // because scrollTargetRef was nulled on cleanup.  Use the last user
    // message's offsetTop (same as the send path) so the spacer formula
    // produces the correct height for keeping that message at the top.
    if (savedSpacer && parseInt(savedSpacer) > 0) {
      const userMsgs = el.querySelectorAll('.chat__msg--user')
      const lastUserEl = userMsgs[userMsgs.length - 1]
      scrollTargetRef.current = lastUserEl
        ? Math.max(0, lastUserEl.offsetTop - 4)
        : el.scrollTop
    }
    // Re-apply scroll once the list stops resizing (lazy renderers like
    // highlight.js / KaTeX expand code blocks asynchronously and shift
    // scrollHeight). Replaces a blind 300ms timeout with an event-driven
    // settle detector: re-apply after the list has been idle for 50ms,
    // then disconnect. Safety timeout caps the observation at 1500ms.
    const listEl = el.querySelector('.chat__list')
    let settleTimer = 0
    let ro
    if (listEl) {
      ro = new ResizeObserver(() => {
        clearTimeout(settleTimer)
        settleTimer = setTimeout(() => {
          applyScroll()
          ro.disconnect()
          setRevealed(true)
        }, 50)
      })
      ro.observe(listEl)
    } else {
      // No list to observe — reveal immediately.
      setRevealed(true)
    }
    // Safety: reveal even if RO never settles (lazy renderers still
    // working past 1.5s). Capped so the user is never stranded on a
    // hidden chat.
    const safety = setTimeout(() => {
      ro?.disconnect()
      setRevealed(true)
    }, 1500)
    return () => {
      clearTimeout(settleTimer)
      clearTimeout(safety)
      ro?.disconnect()
    }
  }, [messages])

  // ── Spacer: reserves space below the user's message ──────────────
  //
  // Formula: spacer = max(0, fullViewH + scrollTarget − listH)
  //
  // Three triggers (all via [messages] dependency):
  //   send    — set spacer, scroll to user message, start ResizeObserver
  //   promote — recalculate spacer only (don't touch scroll)
  //   mount   — skip (scroll-restore handles it)
  //
  // The spacer uses fullViewH (keyboard-closed viewport, captured on mount)
  // so that keyboard open/close doesn't change scrollHeight. Without this,
  // the browser resets scrollTop when the viewport resizes.
  //
  // The ResizeObserver only sets scrollTop in two cases:
  //   1. Content shrank (thinking dots removed) → browser clamped scrollTop
  //      in the async gap before the observer fired → correct it
  //   2. Content filled the viewport (spacer hit 0) → auto-follow if near bottom
  //
  // overflow-anchor: none is set on .chat__scroll in CSS to prevent Chrome's
  // scroll anchoring from fighting the spacer.
  //
  useLayoutEffect(() => {
    const isSend = needsSpacerRef.current
    needsSpacerRef.current = false

    // Mount/history — scroll-restore handles positioning.
    if (!isSend && scrollTargetRef.current == null) return

    const scrollEl = scrollRef.current
    const spacerEl = spacerRef.current
    if (!scrollEl || !spacerEl) return

    // Send — compute scroll target from the user message's position.
    if (isSend) {
      const userMsgEl = lastUserMsgRef.current
      if (!userMsgEl) return
      scrollTargetRef.current = Math.max(0, userMsgEl.offsetTop - 4)
    }

    // Recalculate spacer height (both send and promote paths).
    // Must happen before setting scrollTop — otherwise the browser clamps
    // scrollTop to a value that's too low.
    //
    // Subtract the queued tray's margin-box from viewH. The tray sits
    // in chat__foot (sibling of chat__scroll); when it appears, the
    // scroll area shrinks by tray.offsetHeight + vertical margins. Not
    // accounting for it leaves a dead-space below the response equal to
    // the tray height.
    const st = scrollTargetRef.current
    const listEl = scrollEl.querySelector('.chat__list')
    if (!listEl) return
    const queuedTrayEl = scrollEl.parentElement?.querySelector('.queued')
    const trayH = _trayMarginBox(queuedTrayEl)
    const viewH = (fullViewHRef.current || scrollEl.clientHeight) - trayH
    const listH = listEl.offsetHeight
    const spacerH = Math.max(0, viewH + st - listH)
    spacerEl.style.height = `${spacerH}px`

    if (isSend) scrollEl.scrollTop = st

    // ResizeObserver: keep the spacer in sync as content streams in,
    // tool blocks expand, or lazy renderers (highlight.js, KaTeX) resize.
    // Set up on every path (send, promote, reconnect) — not just send —
    // so the spacer stays correct after switching chats and returning.
    let prevH = spacerH
    let prevListH = listH
    let prevClientH = scrollEl.clientHeight
    // Track whether the user is near the bottom so auto-follow works
    // during streaming.  Updated on EVERY scroll event (including
    // programmatic snaps) so the ResizeObserver always has a fresh
    // reading — no stale-by-one-tick problem.
    // Start with auto-follow OFF. The send path scrolls to show the
    // user's message at the top of the viewport; the agent's response
    // appears just below it in the visible area. Auto-follow only
    // engages when the user actively scrolls to the bottom of content
    // that OVERFLOWS the viewport. Without the overflow check, the
    // first message (where content fits on screen → gap≈0) would
    // falsely engage auto-follow, yanking the user as the response
    // grows past the fold.
    let nearBottom = false
    const onScroll = () => {
      const g = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
      const overflows = scrollEl.scrollHeight > scrollEl.clientHeight + 50
      nearBottom = overflows && g < 50
      nearBottomRef.current = nearBottom
    }
    scrollEl.addEventListener('scroll', onScroll, { passive: true })

    const ro = new ResizeObserver(() => {
      // Snap fullViewHRef to keyboard-closed height when we observe a
      // larger clientHeight (e.g., keyboard dismissed after blur).
      if (scrollEl.clientHeight > fullViewHRef.current) {
        fullViewHRef.current = scrollEl.clientHeight
      }
      if (scrollTargetRef.current == null) return
      const target = scrollTargetRef.current
      const lH = listEl.offsetHeight
      // Re-read tray height every tick (expand/collapse, add/cancel).
      const trayHNow = _trayMarginBox(
        scrollEl.parentElement?.querySelector('.queued'),
      )
      const viewHNow = (fullViewHRef.current || scrollEl.clientHeight) - trayHNow
      const h = Math.max(0, viewHNow + target - lH)
      spacerEl.style.height = `${h}px`

      // Content shrank (spacer grew) — browser may have clamped scrollTop
      // in the gap before this callback. Correct it.
      if (h > prevH && scrollEl.scrollTop < target) {
        scrollEl.scrollTop = target
      }
      prevH = h

      // Auto-follow: only fires while a stream is actively writing
      // and the list actually grew. Two gates:
      //   isStreamingRef — user-initiated growth (tool-block expand,
      //     image lightbox close) on a finished message must not
      //     snap the scroll.
      //   lH > prevListH — re-measures from viewport resize (keyboard
      //     open/close) must not snap either.
      if (isStreamingRef.current && lH > prevListH) {
        const gap = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight
        if (nearBottom && gap > 1) {
          scrollEl.scrollTop = scrollEl.scrollHeight
        }
      }
      prevListH = lH
    })
    ro.observe(listEl)
    // Also observe the queued tray so expand/collapse of a row triggers
    // a spacer recalc (the tray's own height changes without listEl
    // changing).
    if (queuedTrayEl) ro.observe(queuedTrayEl)
    return () => {
      ro.disconnect()
      scrollEl.removeEventListener('scroll', onScroll)
    }
  }, [messages, pendingMessages.length])


  // Paginate older messages when scrolled to top or button clicked.
  const loadingOlder = useRef(false)
  function loadOlderMessages() {
    const el = scrollRef.current
    if (!el || loadingOlder.current || loading || offset <= 0) return
    loadingOlder.current = true
    const prevHeight = el.scrollHeight
    apiFetch(`/chats/${chatId}?limit=20&before=${offset}`)
      .then(r => r.json())
      .then(data => {
        if (chatIdStaleRef.current) return
        const older = data.messages || []
        for (const msg of older) {
          if (msg.blocks) {
            for (const blk of msg.blocks) {
              if (blk.type === 'tool' && blk.status === 'running') {
                blk.status = 'done'
              }
            }
          }
        }
        commitMessages(prev => [...older, ...prev], data.offset || 0)
        requestAnimationFrame(() => {
          const scrollEl = scrollRef.current
          if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight - prevHeight
          loadingOlder.current = false
        })
      })
      .catch(() => { loadingOlder.current = false })
  }

  function handleScroll() {
    const el = scrollRef.current
    if (!el || loadingOlder.current || loading) return
    _scrollPositions[chatId] = el.scrollHeight - el.scrollTop
    const sp = el.querySelector('.spacer-dynamic')
    if (sp) _spacerHeights[chatId] = sp.style.height
    if (el.scrollTop < 5 && offset > 0) {
      loadOlderMessages()
    }
  }


  function handleFileSelect(e) {
    const fileList = Array.from(e.target.files || [])
    if (!fileList.length) return
    e.target.value = ''
    addFiles(fileList)
  }

  const doSend = useCallback(async (text) => {
    if (!text.trim()) return
    if (pendingFiles.some(c => c.status === 'uploading')) return

    // Stop voice recognition so a late onresult doesn't refill input
    // after we clear it.
    if (listeningRef.current) stopVoice?.()

    // On touch devices, blur to dismiss the soft keyboard. Desktop keeps
    // focus so the cursor stays ready for the next message.
    if (_isTouchPrimary) inputRef.current?.blur()

    const attachments = pendingFiles
      .filter(f => f.status === 'done')
      .map(f => ({ name: f.name, size: f.size, mime_type: f.mime_type }))

    // QUEUE PATH: agent is streaming or queue isn't empty. Optimistic
    // entry with a stable client-side `cid` (UUID) that survives the
    // optimistic-ts → server-ts swap. Backend writes to chat.pending_messages
    // via POST /messages returning {status: "queued", ts, position}.
    if (sending || isStreaming) {
      const cid = (typeof crypto !== 'undefined' && crypto.randomUUID)
        ? crypto.randomUUID()
        : `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
      const queuedMsg = { role: 'user', content: text, ts: Date.now(), cid, queued: true }
      if (attachments.length > 0) queuedMsg.attachments = attachments
      setPendingMessages(prev => {
        const next = [...prev, { ...queuedMsg, position: prev.length + 1 }]
        pendingMessagesRef.current = next
        return next
      })
      setInput('')
      clearFiles()
      if (inputRef.current) inputRef.current.style.height = 'auto'
      try {
        const result = await streamSend(
          text,
          attachments.length > 0 ? attachments : undefined,
          { queueOnly: true },
        )
        if (result?.status === 'queued') {
          // Replace optimistic ts with server's (cid is stable).
          setPendingMessages(prev => {
            const next = prev.map(m =>
              m.cid === queuedMsg.cid
                ? { ...m, ts: result.ts ?? m.ts, position: result.position }
                : m
            )
            pendingMessagesRef.current = next
            return next
          })
        }
        // Race: server said "started" though we expected queued.
        if (result?.status === 'started') {
          setPendingMessages(prev => {
            const next = prev.filter(m => m.cid !== queuedMsg.cid)
            pendingMessagesRef.current = next
            return next
          })
          onMessageStart?.()
          promotedRef.current = false
          commitMessages(prev => {
            const { queued: _q, cid: _c, position: _p, ...msg } = queuedMsg
            return [...prev, msg]
          })
          setSending(true)
          setSpacerActive(true)
          if (spacerRef.current) spacerRef.current.style.height = '0px'
          needsSpacerRef.current = true
        }
      } catch (err) {
        // Roll back optimistic + restore input.
        setPendingMessages(prev => {
          const next = prev.filter(m => m.cid !== queuedMsg.cid)
          pendingMessagesRef.current = next
          return next
        })
        setInput(text)
        commitMessages(prev => [
          ...prev,
          { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
        ])
      }
      return
    }

    // FRESH SEND PATH: no active turn, no queue.
    onMessageStart?.()
    promotedRef.current = false

    const userMsg = { role: 'user', content: text, ts: Date.now() }
    if (attachments.length > 0) userMsg.attachments = attachments
    commitMessages(prev => [...prev, userMsg])
    setInput('')
    clearFiles()
    if (inputRef.current) inputRef.current.style.height = 'auto'
    setSending(true)
    setSpacerActive(true)
    if (spacerRef.current) spacerRef.current.style.height = '0px'
    needsSpacerRef.current = true

    try {
      await streamSend(text, attachments.length > 0 ? attachments : undefined)
      if (!hadMessagesRef.current) {
        hadMessagesRef.current = true
        onFirstMessage?.()
      }
    } catch (err) {
      setSending(false)
      commitMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
      ])
    }
  }, [sending, isStreaming, streamSend, pendingFiles, commitMessages, clearFiles])

  // Sends the answer without a visible user message bubble.
  // TODO: when migrating to Claude Agent SDK, replace this with the
  // canUseTool callback which returns answers as updatedInput — the
  // hidden message, answer persistence endpoint, and this function
  // all become unnecessary.
  const doSendSilent = useCallback(async (text, resolvedAnswers) => {
    if (!text.trim() || sending) return
    onMessageStart?.()
    promotedRef.current = false

    // Update the question block with answers so the completed state persists.
    if (resolvedAnswers) {
      commitMessages(prev => {
        const updated = [...prev]
        const lastIdx = updated.length - 1
        if (lastIdx >= 0 && updated[lastIdx].role === 'assistant') {
          const msg = { ...updated[lastIdx] }
          msg.blocks = (msg.blocks || []).map(b =>
            b.type === 'question' ? { ...b, answers: resolvedAnswers } : b
          )
          updated[lastIdx] = msg
        }
        return updated
      })
      // Persist answers to DB so the completed state survives reload.
      // TODO: replace with SDK canUseTool approach — answers would flow
      // back as updatedInput and this endpoint becomes unnecessary.
      apiFetch(`/chats/${chatId}/question-answers`, {
        method: 'POST',
        body: JSON.stringify({ answers: resolvedAnswers }),
      }).catch(() => {})
    }

    setSending(true)
    needsSpacerRef.current = true
    try {
      await streamSend(text, undefined, { hidden: true })
    } catch (err) {
      setSending(false)
      commitMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
      ])
    }
  }, [sending, streamSend, commitMessages])

  function handleSubmit(e) {
    e.preventDefault()
    doSend(input.trim())
  }

  // Cancel a queued message via DELETE. Optimistic remove; reconcile
  // by re-fetching authoritative state on success or on error.
  const handleCancelPending = useCallback(async (ts) => {
    const optimistic = pendingMessagesRef.current.filter(m => m.ts !== ts)
    pendingMessagesRef.current = optimistic
    setPendingMessages(optimistic)
    try {
      const res = await apiFetch(`/chats/${chatId}/pending/${ts}`, {
        method: 'DELETE',
      })
      const data = await res.json()
      const server = (data.pending_messages || []).map(m => ({
        ...m, cid: `s-${m.ts}`, queued: true,
      }))
      pendingMessagesRef.current = server
      setPendingMessages(server)
    } catch {
      // Refetch authoritative state.
      try {
        const res = await apiFetch(`/chats/${chatId}?limit=1`)
        const data = await res.json()
        const server = (data.pending_messages || []).map(m => ({
          ...m, cid: `s-${m.ts}`, queued: true,
        }))
        pendingMessagesRef.current = server
        setPendingMessages(server)
      } catch { /* offline; leave optimistic, user can retry */ }
    }
  }, [chatId])

  async function handleStop() {
    try {
      await fetch(`${BASE}/api/chat/stop`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${getToken()}`,
        },
        body: JSON.stringify({ chat_id: chatId }),
      })
    } catch { /* network error during stop is non-critical */ }
    promoteStreamToMessages()
    disconnect({ clearStreaming: true })
    setSending(false)
    // Backend clears pending on stop; mirror locally and bump fetch gen
    // so any in-flight refetch can't repopulate the tray.
    fetchGenRef.current += 1
    pendingMessagesRef.current = []
    setPendingMessages([])
    onStreamEnd?.()
  }

  const hasMore = offset > 0
  const showEmpty = messages.length === 0 && !isStreaming && !loading && !sending
  const lastUserIdx = messages.reduce((acc, m, i) => (m.role === 'user' && !m.hidden) ? i : acc, -1)

  return (
    <div className={`chat${showEmpty ? ' chat--empty' : ''}`}>
      {showEmpty && (
        <div className="chat__empty-wrap">
          <div className="chat__empty">
            <img className="chat__empty-glyph" src={`${BASE}/moebius.png`} alt="" width="120" height="120" />
            <p className="chat__empty-title">What's on your mind?</p>
            <p className="chat__empty-sub">Try: "What can you do?" or "Build me a weather app"<br />The agent learns from every session and gets better over time.</p>
          </div>
        </div>
      )}
      {!showEmpty && (
      <div
        className="chat__scroll"
        ref={scrollRef}
        onScroll={handleScroll}
        style={revealed ? undefined : { visibility: 'hidden' }}
      >
        <ul className="chat__list" style={spacerActive ? { minHeight: 0 } : undefined}>
          {hasMore && (
            <li className="chat__older">
              <button onClick={loadOlderMessages}>Load earlier messages</button>
            </li>
          )}

          {messages.map((msg, i) => {
            if (msg.hidden) return null
            const hasQuestion = msg.role === 'assistant'
              && msg.blocks?.some(b => b.type === 'question')
            const isLastMsg = i === messages.length - 1
              || messages.slice(i + 1).every(m => m.hidden)
            const questionBlock = hasQuestion
              ? msg.blocks.find(b => b.type === 'question') : null
            const questionAnswerable = hasQuestion && isLastMsg && !sending
              && !questionBlock?.answers
            return (
            <li
              key={msg.id || msg.ts || `${msg.role}-${i}`}
              className={`chat__msg chat__msg--${msg.role}`}
              ref={i === lastUserIdx ? lastUserMsgRef : undefined}
              onClick={msg.ts && msg.role === 'user'
                ? (e) => { e.currentTarget.querySelector('.chat__ts')?.classList.toggle('chat__ts--visible') }
                : undefined}
            >
              <MsgContent
                msg={msg}
                chatId={chatId}
                onQuestionAnswer={questionAnswerable ? doSendSilent : undefined}
                questionAnswers={questionBlock?.answers}
              />
              {msg.ts && msg.role === 'user' && (
                <time className="chat__ts">
                  {new Date(msg.ts).toLocaleString([], {
                    month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit',
                  })}
                </time>
              )}
            </li>
          )})}

          {sending && streamItems.length > 0 && (
            <li className="chat__msg chat__msg--assistant">
              {streamItems.map((item, i) => {
                if (item.type === 'tool') {
                  return (
                    <div key={`s-${i}`} className="chat__tools">
                      <ToolBlock t={item} />
                    </div>
                  )
                }
                if (item.type === 'question') {
                  return (
                    <div key={`s-${i}`}>
                      <QuestionCard
                        questions={item.questions}
                        onAnswer={doSendSilent}
                        disabled={isStreaming}
                      />
                    </div>
                  )
                }
                if (item.type === 'text') {
                  const isLast = i === streamItems.length - 1
                  return (
                    <div key={`s-${i}`} className="chat__text chat__text--assistant">
                      <ProgressiveMarkdown text={item.content} />
                      {isLast && <span className="chat__cursor" />}
                    </div>
                  )
                }
                return null
              })}
            </li>
          )}

          {sending && streamItems.length === 0 && !loading && (
            <li className="chat__msg chat__msg--assistant">
              <div className="chat__thinking"><span /><span /><span /></div>
            </li>
          )}
        </ul>

        <div className="spacer-dynamic" ref={spacerRef} aria-hidden="true" />
      </div>
      )}

      {builtApp && !sending && (
        <div className="chat__open-app">
          <button
            className="chat__open-app-btn"
            onClick={() => onOpenApp?.(builtApp.id)}
          >
            Open {builtApp.name || 'app'} →
          </button>
        </div>
      )}

      <ConnectionStatus error={connectionError} onRetry={retry} />

      <div className="chat__foot">
        <QueuedMessages items={pendingMessages} onCancel={handleCancelPending} />
        <form className="chat__form" onSubmit={handleSubmit}>
          {pendingFiles.length > 0 && (
            <div className="chat__chips">
              {pendingFiles.map(chip => (
                <div
                  key={chip.id}
                  className={`chat__chip${chip.status === 'error' ? ' chat__chip--error' : ''}${chip.objectUrl ? ' chat__chip--image' : ''}`}
                  title={chip.status === 'error' ? chip.error : chip.name}
                >
                  {chip.objectUrl && (
                    <img className="chat__chip-thumb" src={chip.objectUrl} alt="" />
                  )}
                  <span className="chat__chip-name">{chip.name}</span>
                  <span className="chat__chip-status">
                    {chip.status === 'uploading' ? 'uploading…' : chip.status === 'error' ? 'error' : `${Math.round(chip.size / 1024)}KB`}
                  </span>
                  <button
                    type="button"
                    className="chat__chip-remove"
                    onClick={() => removeFile(chip.id)}
                    aria-label={`Remove ${chip.name}`}
                  >×</button>
                </div>
              ))}
            </div>
          )}
          <div className="chat__input-row">
            <input
              type="file"
              multiple
              ref={fileInputRef}
              onChange={handleFileSelect}
              style={{ display: 'none' }}
            />
            <button
              type="button"
              className="chat__attach"
              onClick={() => fileInputRef.current?.click()}
              aria-label="Attach files"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
              </svg>
            </button>
            <textarea
              ref={inputRef}
              className="chat__input"
              value={input}
              onChange={(e) => {
                if (listeningRef.current) return
                setInput(e.target.value)
                e.target.style.height = 'auto'
                e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px'
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !_isTouchPrimary) {
                  e.preventDefault(); handleSubmit(e)
                }
              }}
              placeholder="Message the agent..."
              rows={1}
            />
            {(sending && !input.trim()) ? (
              <button className="chat__stop" type="button" onClick={handleStop} aria-label="Stop">
                <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
                  <rect width="12" height="12" rx="2" />
                </svg>
              </button>
            ) : (input.trim() && !listening) ? (
              <button
                className="chat__send"
                type="button"
                onTouchEnd={(e) => { e.preventDefault(); handleSubmit(e) }}
                onClick={handleSubmit}
                aria-label="Send"
                disabled={pendingFiles.some(c => c.status === 'uploading')}
              >
                <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
                  <path d="M6.5 11V2M2 6.5l4.5-4.5 4.5 4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
            ) : (
              <button
                className={`chat__mic ${listening ? 'chat__mic--active' : ''}`}
                type="button"
                onTouchEnd={(e) => { e.preventDefault(); toggleVoice() }}
                onClick={toggleVoice}
                aria-label={listening ? 'Stop recording' : 'Voice input'}
              >
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <rect x="4.5" y="1" width="5" height="8" rx="2.5" stroke="currentColor" strokeWidth="1.3"/>
                  <path d="M3 7a4 4 0 008 0" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
                  <path d="M7 11v2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
                </svg>
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  )
}
