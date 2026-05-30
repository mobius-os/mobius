import { useState, useRef, useEffect, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { apiFetch, getToken, BASE } from '../../api/client.js'
import { chatMessagesQueryKey } from '../../hooks/queries.js'
import { ProgressiveMarkdown, StandardMarkdown } from './markdown/BlockRenderer.jsx'
import useStreamConnection from './useStreamConnection.js'
import useScrollMode from './useScrollMode.js'
import useVoiceInput from './useVoiceInput.js'
import useFileUpload from './useFileUpload.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import usePendingQueue from './hooks/usePendingQueue.js'
import useBridgePartial from './hooks/useBridgePartial.js'
import ChatInputBar from './ChatInputBar.jsx'
import ComposerPopover from './ComposerPopover.jsx'
import ConnectionStatus from './ConnectionStatus.jsx'
import ToolBlock from './ToolBlock.jsx'
import QuestionCard from './QuestionCard.jsx'
import QueuedMessages from './QueuedMessages.jsx'
import MsgContent from './MsgContent.jsx'
import { questionKey } from './questionKey.js'
import './ChatView.css'


// Cache touch-primary detection. Updated dynamically if input devices change.
const _touchMql = typeof matchMedia === 'function'
  ? matchMedia('(hover: none) and (pointer: coarse)')
  : null
let _isTouchPrimary = _touchMql?.matches ?? false
_touchMql?.addEventListener('change', (e) => { _isTouchPrimary = e.matches })

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
  // Chat is online-only (it spawns a server-side agent). When offline
  // the composer disables send and says so, rather than failing into a
  // dead stream.
  const online = useOnlineStatus()
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
  const [offset, setOffset] = useState(() => cached?.offset ?? 0)
  const [loading, setLoading] = useState(!cached)
  // When the initial /chats/{id} fetch fails we used to silently
  // setLoading(false) — the empty-state UI ("What's on your mind?")
  // would then render as if the chat had no history, hiding the
  // real problem. loadError flips on the catch so we can render a
  // retry message instead of pretending the chat is empty.
  const [loadError, setLoadError] = useState(false)
  const [sending, setSending] = useState(false)
  const [input, setInput] = useState(() => {
    try {
      const pending = sessionStorage.getItem('pending-draft')
      if (pending) { sessionStorage.removeItem('pending-draft'); return pending }
      return sessionStorage.getItem(`draft:${chatId}`) || ''
    } catch { return '' }
  })

  // Per-chat agent runtime config (provider, agent_settings_json,
  // effective_agent_settings, has_assistant_turns). Resolved by the
  // initial /chats/{id} fetch and used to drive ChatSettingsPanel
  // (the model + effort picker inside the `+` popover). Stays null
  // until the fetch lands; the picker simply hides until then.
  const [chatInfo, setChatInfo] = useState(null)

  // Mirror `messages` in a ref so commitMessages can compute the next
  // value without putting a side-effect (setQueryData) inside a
  // setState updater. setState updaters must be pure; React may call
  // them multiple times during concurrent rendering. Reading from a
  // ref + calling setQueryData once outside the updater is correct.
  const messagesRef = useRef(messages)
  messagesRef.current = messages

  // Pending queue (the items shown in the queued-tray above the
  // composer) lives entirely inside usePendingQueue. Every mutation
  // goes through the hook's named ops; reads use pendingQueue.pendingMessages
  // for render and pendingQueue.pendingMessagesRef for closure-safe
  // synchronous access (handleStop's pre-await clear, fetchMessages'
  // cid preservation).
  const pendingQueue = usePendingQueue()

  // Single setter that updates local state AND the query cache.
  //
  // ALWAYS writes the query cache (so even empty chats have an entry,
  // ensuring a cache hit on the next visit). By default, skips the
  // React state update when messages are structurally identical
  // (sameMessageList) — that's the path that was causing back-
  // navigation jitter, because the background fetch would re-set the
  // same array reference and trigger a redundant re-render of the
  // spacer effect.
  //
  // The `force` option overrides that skip. Callers that originate
  // state-machine transitions (e.g., promoteStreamToMessages doing a
  // BRIDGE merge where catch-up content may match the DB partial
  // byte-for-byte) MUST pass force=true. Without it, sameMessageList
  // returns true on the structural match and setMessages is skipped
  // — local state lags behind the cache, the UI keeps rendering the
  // old version, and the only way to see the new one is to remount
  // (which re-reads from the cache via useState initializer).
  // Background-fetch callers leave force=false to keep the perf win.
  const commitMessages = useCallback((updater, nextOffset, opts) => {
    const force = opts?.force === true
    const prev = messagesRef.current
    const next = typeof updater === 'function' ? updater(prev) : updater
    // Advance messagesRef synchronously so back-to-back commitMessages
    // calls within the same React batch (e.g. handleStop's promote +
    // doSend's user-msg append) compose correctly. Without this, the
    // second call's updater reads the pre-batch prev and overwrites
    // the first call's result on setMessages.
    messagesRef.current = next
    queryClient.setQueryData(chatMessagesQueryKey(chatId), (existing) => ({
      ...(existing || {}),
      messages: next,
      offset: nextOffset !== undefined ? nextOffset : (existing?.offset ?? 0),
    }))
    if (!force && sameMessageList(prev, next)) {
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
  const lastUserMsgRef = useRef(null)
  // Stable callback ref attached to the last user message <div>. An
  // inline callback (or even an inline ternary returning `lastUserMsgRef`
  // vs `undefined`) creates fresh ref identities every render, which
  // React 19 treats as detach + reattach. During the detach window
  // `lastUserMsgRef.current = null` and any concurrent ResizeObserver
  // tick in useScrollMode (streaming tokens fire a lot of these) computes
  // pinTarget = 0, collapses the spacer, and the browser clamps scrollTop
  // — the chat visibly jumps. Capturing the callback once keeps the
  // attachment stable across re-renders.
  const setLastUserMsgRef = useCallback((node) => {
    lastUserMsgRef.current = node
  }, [])
  // ChatInputBar owns the hidden <input type="file"> but no longer
  // ships a paperclip button. ComposerPopover renders the "+" trigger
  // that opens the Attach-files row; on click it calls this ref's
  // current() to fire the bar's hidden picker. ChatInputBar's layout
  // effect installs the function.
  const attachTriggerRef = useRef(null)
  // Refs for the absolutely-positioned foot. A ResizeObserver
  // measures `.chat__foot` and publishes its height as `--composer-h`
  // on `.chat`, which `.chat__list` reads for its bottom padding so
  // chips/queue/multi-line growth keep the last message visible
  // above the pill.
  const chatRef = useRef(null)
  const footRef = useRef(null)

  // Lifecycle guards. `hadMessagesRef` reflects the cached length so
  // doSend's "first message" branch doesn't fire spuriously.
  const chatIdStaleRef = useRef(false)
  const hadMessagesRef = useRef((cached?.messages?.length ?? 0) > 0)
  const promotedRef = useRef(false)
  // Bridge-partial gating decides whether the next promote REPLACES
  // the kept DB partial (in-flight turn whose snapshot we mounted
  // on top of) or APPENDS a fresh assistant message. The captured
  // ts is sticky on first mount; markBridged() retires the gate
  // after the first promote so subsequent turns always append.
  // See hooks/useBridgePartial.js for the ts-based design.
  const [bridgeMountInputs, setBridgeMountInputs] = useState({
    runningAtMount: false,
    lastMsgAtMount: null,
  })
  const bridgeHook = useBridgePartial(bridgeMountInputs)

  // Spacer "active" CSS state — keeps min-height: 0 on the list while
  // the spacer is in play, preventing the elastic-overscroll
  // min-height: calc(100% + 1px) from inflating offsetHeight and
  // breaking the spacer formula.
  const [spacerActive, setSpacerActive] = useState(false)
  // Ref mirror of `sending`. Read by doSend's queue-vs-fresh-send
  // guard (and by fetchMessages). Reading state directly would
  // capture a render-time value in doSend's closure — stale when
  // doSend is invoked from a callback that crosses a render boundary
  // (e.g. handleStop calling doSend(combined) after setSending(false)).
  // The ref is updated every render so it always reflects the latest
  // commit. The peer ref for streaming state lives inside
  // useStreamConnection and is exposed below as `isStreamingRef`.
  const sendingRef = useRef(false)
  sendingRef.current = sending

  // Ref mirrors of prop callbacks. doSend / doSendSilent are
  // memoized via useCallback; if these props were listed in the
  // deps array, every parent re-render that passed a fresh function
  // identity would re-create both callbacks (and any consumers'
  // useEffect-on-doSend would re-fire). Keeping them out of deps
  // was an explicit choice (see the comment at doSend's deps
  // array below) — but reading the props directly from the closure
  // captured stale references the moment the parent dropped its
  // useCallback. Refs mirror the latest commit each render, so
  // doSend invokes whatever the parent passed THIS frame even when
  // the callback identity itself is frozen. stopVoice (from
  // useVoiceInput, not a prop) is mirrored below — its hook is
  // declared further down.
  const onMessageStartRef = useRef(onMessageStart)
  onMessageStartRef.current = onMessageStart
  const onFirstMessageRef = useRef(onFirstMessage)
  onFirstMessageRef.current = onFirstMessage

  // Re-entry guard for handleStop. Two rapid Stop clicks (e.g. during
  // the await on /chat/stop) would otherwise both snapshot the same
  // pending queue and both call doSend(combined) → duplicate sends.
  const handlingStopRef = useRef(false)

  // Bumped by handleStop (and any future hard-clear of local state)
  // so any in-flight fetchMessages can't resurrect cleared data.
  const fetchGenRef = useRef(0)

  // Pagination flag — gates loadOlderMessages from re-entering AND
  // gates the scroll-handler in useScrollMode from misclassifying
  // post-prepend scroll-clamps as user gestures.
  const loadingOlder = useRef(false)

  // ── Scroll subsystem ─────────────────────────────────────────────
  //
  // useScrollMode owns the entire scroll state machine: mode ref,
  // applyMode funnel, IntersectionObserver bottom sentinel,
  // ResizeObserver for layout updates, user-gesture detection,
  // mobile keyboard handling via visualViewport, and the
  // hide-then-reveal restore on mount.
  //
  // The hook returns:
  //   • modeRef               — mutate to set PIN_USER_MSG{ts} on send,
  //                             FOLLOW_BOTTOM on user scroll-to-bottom,
  //                             ANCHOR_AT{...} on pagination, etc.
  //   • gestureWindowUntilRef — read by handleScroll to gate pagination
  //                             on user-driven scrolls only.
  //   • revealed              — apply to .chat__scroll style for the
  //                             hide-then-reveal scroll restore.
  //
  // See useScrollMode.js + docs/chat-redesign.md for full design.
  const { modeRef, gestureWindowUntilRef, revealed } = useScrollMode({
    chatId,
    scrollRef,
    spacerRef,
    lastUserMsgRef,
    messages,
    messagesRef,
    pendingMessagesLength: pendingQueue.pendingMessages.length,
    loadingOlderRef: loadingOlder,
  })

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
      // Sync pending queue from server. usePendingQueue.hydrate
      // preserves the client-side cid for any entry whose ts already
      // exists locally (so QueuedMessages's expanded state doesn't
      // remount under a fresh `s-${ts}` key) and stamps a stable
      // s-prefixed cid on previously-unseen server entries.
      pendingQueue.hydrate(data.pending_messages || [])
    } catch { /* network error — silent, user can retry */ }
  }, [chatId, commitMessages, pendingQueue])

  const {
    streamItems,
    latestItemsRef,
    isStreaming,
    isStreamingRef,
    connectionError,
    sendMessage: streamSend,
    connectToStream,
    retry,
    disconnect,
    clearStreamItems,
  } = useStreamConnection(chatId, {
    onStreamEnd: ({ continues, promotedTs } = {}) => {
      promoteStreamToMessages()
      if (continues) {
        // Backend auto-promoted the head of the pending queue into a
        // new turn. Mirror that locally: the hook removes the entry
        // matching the backend-authoritative ts (from
        // queued_turn_starting) and returns it; we strip the queue-
        // only fields and append it to messages so the user msg is
        // visible alongside the incoming response.
        const promoted = pendingQueue.promoteByTs(promotedTs)
        if (promoted) {
          const { queued: _q, cid: _c, position: _p, ...msg } = promoted
          commitMessages(prev => [...prev, msg])
          promotedRef.current = false
          // Queued continuation is backend-initiated — the user may
          // be reading something else in the chat. Don't yank them
          // by re-pinning OR resizing the spacer to 0 (which causes
          // a visible jump when the spacer was previously sized for
          // the just-finished turn and then has to regrow as the
          // continuation streams). Keep whatever mode they were in
          // (FOLLOW_BOTTOM, ANCHOR_AT, or a previous PIN); the
          // existing spacer stays put until the user's next explicit
          // send re-arms it. Stop with queued messages does NOT hit
          // this path on the live UI — `handleStop` collapses the
          // queue client-side via doSend(combined, {pin: false}) and
          // clears the queue before /chat/stop fires, so no
          // queued_turn_starting event reaches us here.
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
        if (pendingQueue.pendingMessagesRef.current.length > 0) {
          fetchMessages({ force: true })
        }
      }
      onStreamEnd?.()
    },
    onSystemEvent,
    onNeedsRefresh: fetchMessages,
    onQueuedTurnStarting: () => {},
  })

  const { files: pendingFiles, addFiles, removeFile, clearFiles } = useFileUpload({ chatId })

  const { listening, listeningRef, stopVoice, toggleVoice } = useVoiceInput({
    onTranscript: (text) => setInput(text),
    inputRef,
  })

  // Ref mirror of stopVoice (peer of onMessageStartRef /
  // onFirstMessageRef above). useVoiceInput may not memoize its
  // return, so doSend's closure would capture a stale function
  // ref if we read stopVoice directly without including it in
  // deps. Mirror via ref to stay closure-safe without churning
  // doSend's identity.
  const stopVoiceRef = useRef(stopVoice)
  stopVoiceRef.current = stopVoice

  // Snapshot stream into a permanent message. Idempotent — both
  // handleStop and onStreamEnd may call this.
  //
  // REPLACE if the last message in `prev` is already an assistant
  // message — that's the DB partial we kept on mount when returning
  // mid-stream (see fetch effect). Promoting alongside the partial
  // would duplicate the in-flight content in the final transcript.
  // APPEND otherwise (the normal first-time send path: `prev` ends in
  // a user message, the assistant message hasn't been committed yet).
  function promoteStreamToMessages() {
    if (promotedRef.current) return
    const items = latestItemsRef.current
    if (items.length === 0) return
    promotedRef.current = true
    const blocks = items.map(item => {
      if (item.type === 'text') return { type: 'text', content: item.content }
      if (item.type === 'question') return { type: 'question', questions: item.questions }
      if (item.type === 'error') return { type: 'error', message: item.message }
      const status = item.status === 'running' ? 'done' : item.status
      return { type: 'tool', ...item, status }
    })
    const content = items
      .filter(i => i.type === 'text')
      .map(i => i.content)
      .join('')
    // Decide REPLACE-vs-APPEND on the live last message and retire
    // the bridge gate (one-shot — next turn always appends, even if
    // it streams through the same chat without remount).
    const isBridge = bridgeHook.shouldBridge(
      messagesRef.current[messagesRef.current.length - 1]
    )
    bridgeHook.markBridged()
    commitMessages(prev => {
      const last = prev[prev.length - 1]
      if (isBridge && last?.role === 'assistant') {
        // BRIDGE case: we mounted with a DB partial that we KEPT, and
        // this is the same in-flight turn finishing. Replace the
        // partial with the freshly-promoted version, preserving id/ts
        // so the <li>'s data-key stays stable.
        //
        // ANSWER PRESERVATION: question-block answers live INSIDE the
        // block (block.answers), not at the message level. The catch-
        // up streamItems re-emits the question event WITHOUT answers
        // (the backend doesn't include them in the SSE replay — they
        // live in chat.messages, not in the per-turn event stream).
        // If we blindly replace blocks, any answers the user submitted
        // before navigating away (which ARE persisted in the DB and
        // came back in our fetch) would be wiped on this promote.
        //
        // Match by question identity (shared questionKey helper —
        // mirrors backend/app/events.py:question_block_key), NOT by
        // block index. A duplicate-card hiccup mid-stream can shift
        // positions between live streamItems and the persisted
        // message; position-match would either drop answers or paste
        // them onto the wrong card. Identity-match survives any
        // intervening block reshuffles.
        const existingAnswersByKey = new Map()
        for (const ob of last.blocks || []) {
          if (ob?.type === 'question' && ob.answers) {
            existingAnswersByKey.set(questionKey(ob), ob.answers)
          }
        }
        const mergedBlocks = blocks.map(nb => {
          if (nb.type !== 'question' || nb.answers) return nb
          const carried = existingAnswersByKey.get(questionKey(nb))
          return carried ? { ...nb, answers: carried } : nb
        })
        const merged = { ...last, content, blocks: mergedBlocks }
        return [...prev.slice(0, -1), merged]
      }
      // Normal multi-turn flow: append a fresh assistant message.
      // No ts on this one — it'll get one from the DB on next fetch.
      return [...prev, { role: 'assistant', content, blocks }]
    }, undefined, { force: true })
    // force=true bypasses sameMessageList. In the BRIDGE merge path
    // the new (catch-up) blocks may be structurally identical to the
    // kept DB-partial blocks (backend's throttled save was recent +
    // catch-up replayed the same events). Without force, setMessages
    // is skipped, local state lags the cache, and the UI keeps
    // rendering the stale version — the partial only "appears" on
    // remount via the cache. Force is correct here because promote
    // is a state-machine commit, not a redundant background refetch.

    // Wipe the live streamItems now that they live in `messages`. The
    // conditional live `<li>` (rendered at the bottom of the list
    // when `sending && streamItems.length > 0`) would otherwise
    // double-render the just-promoted assistant message during the
    // ~150ms gap between this promote and the next reconnect that
    // would otherwise clear streamItems — the user sees a duplicate
    // flash on every queued-continuation turn.
    clearStreamItems?.()
  }

  // Persist draft so it survives leaving and re-entering the chat.
  useEffect(() => {
    try {
      if (input) sessionStorage.setItem(`draft:${chatId}`, input)
      else sessionStorage.removeItem(`draft:${chatId}`)
    } catch { /* quota exceeded or private browsing */ }
  }, [input, chatId])

  // Auto-size textarea when a draft is restored. Cap matches the
  // 280px max-height enforced by `handleTextareaChange` in
  // ChatInputBar; without keeping these in sync a tall draft would
  // restore visually truncated until the user types one more
  // character to trigger the live-grow path. Also mirror the
  // .chat__pill--tall class toggle so a restored multi-line draft
  // anchors the send/mic buttons to the bottom of the pill — the
  // toggle otherwise only fires on input keystrokes.
  useEffect(() => {
    const el = inputRef.current
    if (el && input) {
      el.style.height = 'auto'
      const h = Math.min(el.scrollHeight, 280)
      el.style.height = h + 'px'
      const pill = el.closest('.chat__pill')
      if (pill) pill.classList.toggle('chat__pill--tall', h > 45)
    }
  }, [chatId])

  // Publish `.chat__foot`'s rendered height as `--composer-h` on
  // `.chat`. `.chat__list` reads this var for its bottom padding so
  // the last message always clears the absolutely-positioned pill
  // — chips, queue tray, multi-line growth all push the clearance
  // in lockstep.
  useEffect(() => {
    const chatEl = chatRef.current
    const footEl = footRef.current
    if (!chatEl || !footEl || typeof ResizeObserver === 'undefined') return
    const apply = () => {
      chatEl.style.setProperty('--composer-h', `${footEl.offsetHeight}px`)
    }
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(footEl)
    return () => ro.disconnect()
  }, [])

  // Fetch messages and connect to an in-progress stream if the agent is running.
  useEffect(() => {
    let cancelled = false
    chatIdStaleRef.current = false
    setLoadError(false)

    apiFetch(`/chats/${chatId}?limit=20`)
      .then(r => r.json())
      .then(data => {
        if (cancelled) return
        const msgs = data.messages || []

        // Keep the DB partial when the agent is still running. The
        // user sees the most recent persisted state immediately; SSE
        // catch-up populates streamItems and the streaming <li> takes
        // over visually (see messages.map render — last assistant is
        // suppressed when sending && streamItems.length > 0). On done,
        // promoteStreamToMessages replaces this partial with the
        // final version. Previously we stripped this and waited for
        // SSE — caused the "message disappears on choppy return" bug.

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
        // Snapshot the per-chat runtime config so ChatSettingsPanel
        // (the model + effort picker in the `+` popover) renders
        // with the current effective model/effort. The initial
        // fetch is the only canonical source; the picker updates
        // this dict in place on each PATCH.
        setChatInfo({
          provider: data.provider || 'claude',
          agent_settings_json: data.agent_settings_json || null,
          effective: data.effective_agent_settings || {},
          has_assistant_turns: !!data.has_assistant_turns,
        })
        // Feed the bridge gate with the data.running + last-msg
        // snapshot. useBridgePartial captures the kept-partial ts
        // on first valid input and stays sticky from there — only
        // the very first turn after mount is a "bridge"; subsequent
        // turns always APPEND (markBridged retires the gate on the
        // first promote).
        setBridgeMountInputs({
          runningAtMount: !!data.running,
          lastMsgAtMount: msgs.length > 0 ? msgs[msgs.length - 1] : null,
        })
        setLoading(false)

        // Hydrate pending queue from backend so a reload mid-queue
        // doesn't drop the visible "queued" tray. hydrate stamps a
        // stable s-prefixed cid on each entry so QueuedMessages's
        // expanded state survives future re-renders.
        pendingQueue.hydrate(data.pending_messages || [])

        if (data.running) {
          setSending(true)
          connectToStream(false)
        }
      })
      .catch(() => {
        if (cancelled) return
        setLoadError(true)
        setLoading(false)
      })

    return () => {
      try {
        // (Scroll mode persistence has moved to useScrollMode's own
        // cleanup — runs on chatId change, before this effect's
        // cleanup, so modeRef is captured for the chat we're leaving.)
      } catch {}
      cancelled = true
      chatIdStaleRef.current = true
      loadingOlder.current = false
      disconnect()
    }
  }, [chatId])


  // Paginate older messages. Captures a pre-prepend anchor so we can
  // restore the user's reading position via applyMode after the
  // prepend grows scrollHeight upward. The anchor is the topmost
  // currently-rendered message; after prepend, it has the same
  // data-key but a new (larger) offsetTop. ANCHOR_AT{key, offset}
  // lands the user at the same visual position.
  // (loadingOlder ref is declared earlier alongside the useScrollMode
  // hook call — it's passed to the hook to gate the scroll handler.)
  function loadOlderMessages() {
    const el = scrollRef.current
    if (!el || loadingOlder.current || loading || offset <= 0) return
    loadingOlder.current = true
    // Snapshot the topmost rendered msg + its current offset for
    // post-prepend restore. The anchor key/offset is stable: after
    // the prepend, the SAME message has a larger offsetTop (older
    // messages are inserted above it), and ANCHOR_AT{key, offset}
    // resolves to the new offsetTop minus the original gap → no
    // visible jump.
    const topMsg = el.querySelector('.chat__msg[data-key]')
    const anchorKey = topMsg?.dataset?.key || null
    const anchorOffset = topMsg ? topMsg.offsetTop - el.scrollTop : 0
    // We deliberately do NOT save the previous mode to restore later.
    // The user paginated — their intent is now to read older content.
    // If the previous mode was FOLLOW_BOTTOM and we restored it,
    // the next layout event (e.g., a streaming token) would yank
    // them to the bottom, undoing the pagination. Pagination leaves
    // them at the new anchor; the next gesture (or send) writes a
    // fresh mode.
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
        // Set the temporary anchor mode BEFORE commitMessages so the
        // ensuing layout effect (triggered by [messages] change)
        // applies the anchor instead of intentMode. Otherwise the
        // layout effect runs first with intentMode (e.g., PIN at the
        // user msg's NEW offsetTop) → visible jump → then our rAF
        // would set the anchor → second jump.
        if (anchorKey) {
          modeRef.current = {
            kind: 'ANCHOR_AT', key: anchorKey, offset: anchorOffset,
          }
        }
        commitMessages(prev => [...older, ...prev], data.offset || 0)
        requestAnimationFrame(() => {
          // The layout effect has run with ANCHOR_AT — applyMode
          // landed the topmost-pre-prepend msg at the same visual
          // position. We deliberately DON'T restore the previous
          // mode: user paginated → their intent is to read older
          // content. The ANCHOR_AT mode keeps them there across
          // subsequent layout events (incoming tokens, etc). Their
          // next gesture (or send) writes a fresh mode.
          loadingOlder.current = false
        })
      })
      .catch(() => { loadingOlder.current = false })
  }

  function handleScroll() {
    const el = scrollRef.current
    if (!el || loadingOlder.current || loading) return
    // Gesture guard: applyMode's programmatic scrolls (e.g., PIN_USER_MSG
    // landing near scrollTop=0 when the user msg is high in the list,
    // or FOLLOW_BOTTOM after a pagination prepend) can satisfy
    // `scrollTop < 5 && offset > 0` and trigger an unwanted pagination
    // load. Only paginate when the scroll was user-driven (recent
    // pointer/wheel/touch/key in the 250ms window).
    const userDriven = performance.now() < gestureWindowUntilRef.current
    if (!userDriven) return
    if (el.scrollTop < 5 && offset > 0) {
      loadOlderMessages()
    }
  }


  // `opts.pin` controls whether the new user message pins to the top
  // of the viewport (the standard ChatGPT/Claude.ai send UX). Defaults
  // to true for normal user-initiated sends. Pass `pin: false` from
  // synthetic-send paths where pinning would be surprising:
  //   - handleStop's queue-collapse: the user clicked Stop, not Send;
  //     pinning the auto-generated combined message would yank the
  //     viewport away from whatever the user was reading (the partial
  //     they just stopped) → original turn 1 user msg + partial get
  //     pushed above the viewport. Keep their current scroll mode
  //     instead — the new turn streams into view from where they were.
  const doSend = useCallback(async (text, opts = {}) => {
    const pin = opts.pin !== false  // default true
    if (!text.trim()) return
    if (pendingFiles.some(c => c.status === 'uploading')) return

    // Stop voice recognition so a late onresult doesn't refill input
    // after we clear it.
    if (listeningRef.current) stopVoiceRef.current?.()

    // On touch devices, blur to dismiss the soft keyboard. Desktop keeps
    // focus so the cursor stays ready for the next message.
    if (_isTouchPrimary) inputRef.current?.blur()

    // Callers can pre-supply attachments (e.g. handleStop collapsing
    // a queue that had files attached to queued items). When provided,
    // they replace the pendingFiles-derived list so data isn't lost.
    const attachments = Array.isArray(opts.attachments)
      ? opts.attachments
      : pendingFiles
          .filter(f => f.status === 'done')
          .map(f => ({ name: f.name, size: f.size, mime_type: f.mime_type }))

    // QUEUE PATH: agent is streaming or queue isn't empty. Optimistic
    // entry with a stable client-side `cid` (UUID) that survives the
    // optimistic-ts → server-ts swap. Backend writes to chat.pending_messages
    // via POST /messages returning {status: "queued", ts, position}.
    //
    // Read from refs (not React state) so doSend stays closure-safe.
    // Callers like handleStop invoke doSend AFTER calling
    // setSending(false) — the captured `sending` state would still
    // be `true` in this render's closure, sending the message to the
    // queue path instead of the fresh-send path. Refs reflect the
    // latest commit and dodge that.
    if (sendingRef.current || isStreamingRef.current) {
      const cid = (typeof crypto !== 'undefined' && crypto.randomUUID)
        ? crypto.randomUUID()
        : `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
      const queuedMsg = { role: 'user', content: text, ts: Date.now(), cid, queued: true }
      if (attachments.length > 0) queuedMsg.attachments = attachments
      pendingQueue.add(queuedMsg)
      setInput('')
      clearFiles()
      if (inputRef.current) {
        inputRef.current.style.height = 'auto'
        // Drop the multi-line `.chat__pill--tall` class so send/mic
        // re-center vertically. Without this, the pill stays in
        // flex-end alignment after a send-from-tall and the freshly
        // empty textarea renders pinned to the bottom — text appears
        // off-center (lower than its resting position) until the
        // user types again. `handleTextareaChange` re-evaluates this
        // class on every keystroke, but send doesn't go through that
        // path. Tap-to-focus doesn't trigger a change event either,
        // so the visual stayed broken until the next keystroke.
        inputRef.current.closest('.chat__pill')?.classList.remove('chat__pill--tall')
      }
      try {
        const result = await streamSend(
          text,
          attachments.length > 0 ? attachments : undefined,
          { queueOnly: true },
        )
        if (result?.status === 'queued') {
          // Replace optimistic ts with server's (cid is stable).
          pendingQueue.swapOptimisticTs(
            queuedMsg.cid, result.ts ?? queuedMsg.ts, result.position,
          )
        }
        // Race: server said "started" though we expected queued.
        if (result?.status === 'started') {
          pendingQueue.cancelByCid(queuedMsg.cid)
          onMessageStartRef.current?.()
          promotedRef.current = false
          commitMessages(prev => {
            const { queued: _q, cid: _c, position: _p, ...msg } = queuedMsg
            return [...prev, msg]
          })
          setSending(true)
          setSpacerActive(true)
          if (spacerRef.current) spacerRef.current.style.height = '0px'
          // New visible user msg → pin it to the top of the viewport.
          modeRef.current = { kind: 'PIN_USER_MSG', ts: queuedMsg.ts }
          // This is a NEW turn (not the bridge turn from mount).
          // Retire the bridge gate so the upcoming promote appends a
          // fresh assistant instead of replacing whichever message
          // is currently last.
          bridgeHook.markBridged()
        }
      } catch (err) {
        // Roll back optimistic + restore input.
        pendingQueue.cancelByCid(queuedMsg.cid)
        setInput(text)
        commitMessages(prev => [
          ...prev,
          { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
        ])
      }
      return
    }

    // FRESH SEND PATH: no active turn, no queue.
    onMessageStartRef.current?.()
    promotedRef.current = false

    const userMsg = { role: 'user', content: text, ts: Date.now() }
    if (attachments.length > 0) userMsg.attachments = attachments
    commitMessages(prev => [...prev, userMsg])
    setInput('')
    clearFiles()
    if (inputRef.current) {
      inputRef.current.style.height = 'auto'
      // Drop the multi-line `.chat__pill--tall` class — see queue-path
      // comment above for the full rationale.
      inputRef.current.closest('.chat__pill')?.classList.remove('chat__pill--tall')
    }
    setSending(true)
    setSpacerActive(true)
    if (spacerRef.current) spacerRef.current.style.height = '0px'
    // User just sent — pin the new user message to viewport top.
    // Skip when caller asked us not to pin (e.g. handleStop's
    // queue-collapse, where the user was reading the partial and
    // shouldn't be yanked to a synthetic combined message).
    if (pin) {
      modeRef.current = { kind: 'PIN_USER_MSG', ts: userMsg.ts }
    }
    // Fresh turn — not a bridge from a mounted DB partial.
    bridgeHook.markBridged()

    try {
      await streamSend(text, attachments.length > 0 ? attachments : undefined)
      if (!hadMessagesRef.current) {
        hadMessagesRef.current = true
        onFirstMessageRef.current?.()
      }
    } catch (err) {
      setSending(false)
      commitMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
      ])
    }
    // doSend doesn't need `sending` / `isStreaming` in deps anymore —
    // the guard reads sendingRef/isStreamingRef, and refs are stable.
    // Same for the prop callbacks (onMessageStart, onFirstMessage,
    // stopVoice): doSend reads them via the ref mirrors declared near
    // the top of the component, so they don't need to be in deps and
    // doSend doesn't re-allocate when the parent passes fresh
    // identities. Dropping all of these from deps avoids needlessly
    // re-creating doSend on every stream tick (and avoids the
    // stale-closure trap for callers like handleStop).
  }, [streamSend, pendingFiles, commitMessages, clearFiles])

  // Sends the answer without a visible user message bubble.
  // Sends the answer to an AskUserQuestion as a hidden user message.
  // Answers ride along in the SAME POST as the hidden message —
  // backend writes them atomically into the existing question block
  // (see chats_stream.py:_apply_answers_to_last_question). One
  // transaction, no race. The previous flow had a separate
  // POST /question-answers that could race with the GET on a mid-
  // stream remount, causing answers to disappear on first return
  // and reappear on the second.
  const doSendSilent = useCallback(async (text, resolvedAnswers) => {
    // Guard on refs, not render-time `sending`. A fast double-click
    // fires two handlers in the same tick before React commits the
    // setSending(true) below — both closures see `sending === false`
    // and both submit the same answer. Flip sendingRef synchronously
    // right after the guard so the second click bails immediately.
    if (!text.trim()) return
    // Answer submissions (resolvedAnswers truthy) are allowed mid-turn:
    // the runner is paused on the AskUserQuestion future and is waiting
    // for exactly this POST. BOTH gates must relax — `sending` is set
    // by the originating user prompt and stays true through the whole
    // turn, `isStreaming` is true while the SSE stream is open. Without
    // both relaxations, Submit on a question card silently no-ops and
    // the Codex bridge times out after 10 minutes (the user sees "card
    // came back with no answer"). QuestionCard's own `submitted` state
    // guards against double-clicks on the same card.
    if ((sendingRef.current || isStreamingRef.current) && !resolvedAnswers) {
      return
    }
    sendingRef.current = true
    onMessageStartRef.current?.()
    promotedRef.current = false

    // Local optimistic update of the question block so the UI shows
    // the answered state immediately (before backend round-trip).
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
    }

    setSending(true)
    // doSendSilent starts a NEW hidden turn (the answer-followup).
    // The bridge gate may still be live if mount kept a DB partial
    // and the user submitted an answer before that partial's done
    // event arrived. The new turn is NOT a bridge — its promote
    // should append a fresh assistant message, not replace the
    // question-block message (which already has answers), so retire
    // the gate now.
    bridgeHook.markBridged()
    // Hidden answer is a continuation, NOT a new visible send. The
    // user may be reading somewhere else; don't yank them with a
    // PIN. The agent's response builds into the existing assistant
    // message; if the user was at FOLLOW_BOTTOM they'll see it
    // forming, if ANCHOR_AT they stay where they are.
    try {
      await streamSend(text, undefined, {
        hidden: true,
        answers: resolvedAnswers,
      })
    } catch (err) {
      setSending(false)
      commitMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
      ])
    }
  }, [streamSend, commitMessages])

  function handleSubmit(e) {
    e.preventDefault()
    doSend(input.trim())
  }

  // Cancel a queued message via DELETE. Optimistic remove; reconcile
  // by re-fetching authoritative state on success or on error.
  const handleCancelPending = useCallback(async (ts) => {
    pendingQueue.cancelByTs(ts)
    try {
      const res = await apiFetch(`/chats/${chatId}/pending/${ts}`, {
        method: 'DELETE',
      })
      const data = await res.json()
      pendingQueue.hydrate(data.pending_messages || [])
    } catch {
      // Refetch authoritative state.
      try {
        const res = await apiFetch(`/chats/${chatId}?limit=1`)
        const data = await res.json()
        pendingQueue.hydrate(data.pending_messages || [])
      } catch { /* offline; leave optimistic, user can retry */ }
    }
  }, [chatId, pendingQueue])

  async function handleStop() {
    // Re-entry guard. Without this, two rapid Stop clicks would both
    // snapshot the same pending queue (the snapshot happens BEFORE
    // the await on /chat/stop) and both call doSend(combined) →
    // duplicate combined send. Set the guard synchronously at entry
    // and clear it in a finally so transient errors don't strand it.
    if (handlingStopRef.current) return
    handlingStopRef.current = true
    try {
      // Snapshot the queue BEFORE stopping (the backend's stop
      // clears chat.pending_messages). If queued messages exist,
      // the first Stop press collapses them into a single combined
      // message and submits it as the next turn — matches Claude.ai /
      // Codex UX where Stop is "cancel current, send what I queued"
      // rather than "discard everything". A second Stop with no
      // queue actually halts. Users can still remove individual
      // queued messages via the X button while they're queued.
      //
      // Collapse queued messages into one combined turn. Attachments
      // are preserved by merging each queued item's `.attachments`
      // (de-duped by name) and passing them through doSend's opts —
      // data loss on Stop was a real bug (user adds files, agent's
      // mid-turn, user hits Stop, files vanish).
      const queuedSnapshot = pendingQueue.pendingMessagesRef.current
      const queuedTexts = queuedSnapshot
        .map(m => (m.content || '').trim())
        .filter(Boolean)
      const combined = queuedTexts.join('\n')
      const seenNames = new Set()
      const combinedAttachments = []
      for (const m of queuedSnapshot) {
        for (const a of (m.attachments || [])) {
          if (a && a.name && !seenNames.has(a.name)) {
            seenNames.add(a.name)
            combinedAttachments.push(a)
          }
        }
      }

      // Invalidate any in-flight refetch + clear pending BEFORE the
      // /chat/stop await. During that await, the SSE stream closes
      // (server kills proc + closes broadcast), which fires the
      // natural onStreamEnd path in useStreamConnection → ChatView's
      // onStreamEnd handler → if the queue has items it calls
      // fetchMessages({force:true}) → that fetch can land BEFORE
      // handleStop continues post-await, overwriting the just-
      // promoted partial + the soon-to-be-sent combined turn with
      // stale DB state. Bumping fetchGen NOW makes any such in-flight
      // fetch get discarded by its gen guard; clearing the queue NOW
      // also prevents the natural handler from triggering the fetch
      // at all. R1 in _034-design.md spells out the contract —
      // pendingQueue.clear() updates pendingMessagesRef.current to
      // [] before this line returns (synchronous).
      fetchGenRef.current += 1
      pendingQueue.clear()

      let stoppedCleanly = true
      try {
        const stopRes = await fetch(`${BASE}/api/chat/stop`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${getToken()}`,
          },
          body: JSON.stringify({ chat_id: chatId }),
        })
        if (stopRes.ok) {
          // stop_chat returns {stopped: false} when the SDK interrupt
          // timed out — the runner is still alive. We must NOT tear
          // down local state or re-send the collapsed queue, because
          // that would mean two concurrent runs of the same chat.
          // Leave the stream attached and surface the failure so the
          // user can retry. Network errors are treated as success-ish
          // (we have to assume the proc died on our end too).
          try {
            const data = await stopRes.json()
            if (data && data.stopped === false) stoppedCleanly = false
          } catch { /* non-JSON body — assume legacy success */ }
        }
      } catch { /* network error during stop is non-critical */ }
      if (!stoppedCleanly) {
        // Restore the queued messages we optimistically cleared, so
        // the user can hit Stop again or wait + retry without losing
        // their drafts. Don't disconnect — the runner may still be
        // streaming. We have `queuedTexts` + `combinedAttachments`
        // but their original `ts` ids; safest is just a refetch so
        // the user sees authoritative server state.
        if (combined) {
          try {
            const res = await apiFetch(`/chats/${chatId}?limit=1`)
            const data = await res.json()
            pendingQueue.hydrate(data.pending_messages || [])
          } catch { /* leave empty; user can resend */ }
        }
        return
      }
      disconnect({ clearStreaming: true })
      promoteStreamToMessages()
      setSending(false)
      // Sync sendingRef to the just-committed state so the synchronous
      // doSend(combined) call below reads the post-stop value.
      // setSending(false) queues a render — the next render will write
      // sendingRef via the top-of-component mirror, but until then the
      // ref still holds the pre-stop `true`. We need the value RIGHT
      // NOW for doSend's guard. (The peer isStreamingRef is the hook's
      // own ref; disconnect({clearStreaming: true}) above flipped it
      // synchronously already.)
      sendingRef.current = false
      // pending + fetchGen were cleared/bumped BEFORE the await above.
      onStreamEnd?.()

      if (combined) {
        // doSend's guard reads sendingRef/isStreamingRef (just synced
        // to false above) → fresh-send path. pin:false so the
        // synthetic combined-from-queue message doesn't yank the
        // viewport to top, pushing the partial the user just stopped
        // (and the original turn-1 user msg) above the viewport.
        // Mode stays whatever the user had — they were reading the
        // partial, the new turn streams in continuing from there.
        doSend(combined, {
          pin: false,
          attachments: combinedAttachments.length > 0
            ? combinedAttachments
            : undefined,
        })
      }
    } finally {
      handlingStopRef.current = false
    }
  }

  const hasMore = offset > 0
  // Empty-state is the "I have nothing to show because nothing happened
  // yet" view. If the initial chat fetch errored, we have no idea
  // whether the chat is empty — surfacing that branch separately keeps
  // us from lying with "What's on your mind?" over a network failure.
  const showEmpty = !loadError && messages.length === 0 && !isStreaming && !loading && !sending
  const showLoadError = loadError && messages.length === 0 && !loading && !sending
  const lastUserIdx = messages.reduce((acc, m, i) => (m.role === 'user' && !m.hidden) ? i : acc, -1)

  // The streaming <li> only carries a data-key in the BRIDGE case
  // (we kept a DB partial on mount and the streaming <li> is the
  // visual replacement for that suppressed message). Using the
  // partial's data-key keeps an ANCHOR_AT pointing at the partial
  // resolving correctly through the catch-up window.
  //
  // For multi-turn flow (no bridge), the previous turn's assistant
  // is rendered alongside the streaming <li> (different turns). If
  // we shared a data-key, applyMode's querySelector lookup would be
  // ambiguous (two elements with the same data-key). Better to leave
  // the streaming <li> with NO data-key — ANCHOR_AT during streaming
  // falls back to the topmost visible message (e.g., the user msg
  // above), which is the user's natural reading anchor anyway.
  const streamingDataKey = (() => {
    const last = messages[messages.length - 1]
    if (!bridgeHook.shouldBridge(last)) return undefined
    if (!last || last.role !== 'assistant' || last.hidden) return undefined
    return last.id || `${last.role}-${last.ts ?? messages.length - 1}`
  })()

  return (
    <div
      ref={chatRef}
      className={`chat${showEmpty || showLoadError ? ' chat--empty' : ''}`}
    >
      {showEmpty && (
        <div className="chat__empty-wrap">
          <div className="chat__empty">
            <img className="chat__empty-glyph" src={`${BASE}/moebius.png`} alt="" width="120" height="120" />
            <p className="chat__empty-title">What's on your mind?</p>
            <p className="chat__empty-sub">Ask questions, build and modify apps, schedule tasks.<br />Möbius improves the more you use it.</p>
          </div>
        </div>
      )}
      {showLoadError && (
        <div className="chat__empty-wrap">
          <div className="chat__empty">
            <p className="chat__empty-title">Couldn't load this chat.</p>
            <p className="chat__empty-sub">Check your connection and try again.</p>
            <button
              type="button"
              onClick={() => {
                setLoadError(false)
                setLoading(true)
                // Re-run the load-effect by toggling chatId-derived
                // state. Cheapest path is a soft reload of the route.
                window.location.reload()
              }}
            >
              Retry
            </button>
          </div>
        </div>
      )}
      {!showEmpty && !showLoadError && (
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
            const isLastMsg = i === messages.length - 1
              || messages.slice(i + 1).every(m => m.hidden)
            // Suppress the last assistant message ONLY when this is
            // the BRIDGE case (we kept a DB partial on mount and the
            // streaming <li> is about to render the same in-flight
            // turn). For normal multi-turn flow, the existing
            // assistant message and the streaming <li> represent
            // DIFFERENT turns and must BOTH render — otherwise a
            // user's answered-question card would hide whenever the
            // next turn streams.
            if (isLastMsg
                && bridgeHook.shouldBridge(msg)
                && msg.role === 'assistant'
                && sending && streamItems.length > 0) {
              return null
            }
            const hasQuestion = msg.role === 'assistant'
              && msg.blocks?.some(b => b.type === 'question')
            const questionBlock = hasQuestion
              ? msg.blocks.find(b => b.type === 'question') : null
            const questionAnswerable = hasQuestion && isLastMsg && !sending
              && !questionBlock?.answers
            // Stable per-message DOM key for the scroll state machine.
            // data-key is queried by applyMode when restoring an
            // ANCHOR_AT mode. msg.id (server-assigned UUID) is ideal;
            // fall back to role+ts which is also stable across renders.
            const dataKey = msg.id || `${msg.role}-${msg.ts ?? i}`
            return (
            <li
              key={msg.id || msg.ts || `${msg.role}-${i}`}
              className={`chat__msg chat__msg--${msg.role}`}
              ref={i === lastUserIdx ? setLastUserMsgRef : null}
              data-key={dataKey}
              data-ts={msg.role === 'user' && msg.ts ? String(msg.ts) : undefined}
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
            <li
              className="chat__msg chat__msg--assistant"
              data-key={streamingDataKey}
            >
              {streamItems.map((item, i) => {
                if (item.type === 'tool') {
                  return (
                    <div key={`s-${i}`} className="chat__tools">
                      <ToolBlock t={item} />
                    </div>
                  )
                }
                if (item.type === 'question') {
                  // QuestionCard tracks its own `submitted` state and
                  // disables itself after the user answers. The agent
                  // is paused on the AskUserQuestion future, so the
                  // user MUST be able to click these chips even while
                  // the turn is otherwise "streaming". No external
                  // disabled gate.
                  return (
                    <div key={`s-${i}`}>
                      <QuestionCard
                        questions={item.questions}
                        onAnswer={doSendSilent}
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
                if (item.type === 'error') {
                  // StandardMarkdown so URLs in provider errors
                  // (quota links, billing pages) become clickable.
                  // Same shape as the post-promote render in
                  // MsgContent so the user gets the same affordance
                  // before and after the streaming `<li>` is
                  // replaced by the persisted message.
                  return (
                    <div key={`s-${i}`} className="chat__text--error" role="alert">
                      <span className="chat__error-label">Error</span>
                      <StandardMarkdown
                        text={item.message || 'The agent ran into an issue.'}
                      />
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
        {/* Bottom sentinel — watched by IntersectionObserver. When
            it's in the viewport, the user is at the bottom of
            content (FOLLOW_BOTTOM intent). Zero size + aria-hidden
            so it's invisible to users and screen readers. */}
        <div className="chat__bottom-sentinel" aria-hidden="true" />
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

      <div ref={footRef} className="chat__foot">
        <QueuedMessages items={pendingQueue.pendingMessages} onCancel={handleCancelPending} />
        <ChatInputBar
          input={input}
          onInputChange={setInput}
          onSubmit={handleSubmit}
          inputRef={inputRef}
          sending={sending}
          listening={listening}
          listeningRef={listeningRef}
          onToggleVoice={toggleVoice}
          onStop={handleStop}
          offline={!online}
          pendingFiles={pendingFiles}
          onAddFiles={addFiles}
          onRemoveFile={removeFile}
          attachTriggerRef={attachTriggerRef}
          leftButtons={
            <ComposerPopover
              chatInfo={chatInfo}
              chatId={chatId}
              onAttachClick={() => attachTriggerRef.current?.()}
              /* Derive live — `chatInfo.has_assistant_turns` is set
                 once on mount via the API and never refreshed when
                 the running turn finishes. Without this OR, sending
                 a message and getting a reply in the same session
                 leaves the cross-provider lock disengaged: the user
                 can flip Claude ↔ Codex mid-chat and lose the
                 session, which neither SDK can recover from. */
              hasAssistantTurns={
                (chatInfo?.has_assistant_turns ?? false)
                || messages.some(m => m.role === 'assistant')
              }
              onChangeChatInfo={({ agent_settings_json, provider, effective }) => {
                // Merge into chatInfo so the next render reflects the
                // PATCH without a roundtrip. effective is authoritative
                // (backend re-merged on top of the current global file).
                // `provider` only changes when the user flipped the
                // provider radio — preserve the existing value
                // otherwise so a model-only PATCH doesn't wipe it.
                setChatInfo(prev => prev ? ({
                  ...prev,
                  agent_settings_json: agent_settings_json,
                  provider: provider || prev.provider,
                  effective: effective || prev.effective,
                }) : prev)
              }}
            />
          }
        />
      </div>
    </div>
  )
}
