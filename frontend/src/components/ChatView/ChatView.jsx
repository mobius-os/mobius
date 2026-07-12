import { useState, useRef, useEffect, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { apiFetch, getToken, BASE } from '../../api/client.js'
import { chatMessagesQueryKey } from '../../hooks/queries.js'
import useStreamConnection from './useStreamConnection.js'
import useScrollMode, {
  shouldPinSend,
  anchorModeFromScroll,
  isNearContentBottom,
  modeForForegroundReturn,
} from './useScrollMode.js'
import useVoiceInput from './useVoiceInput.js'
import useFileUpload from './useFileUpload.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import usePendingQueue from './hooks/usePendingQueue.js'
import useBridgePartial from './hooks/useBridgePartial.js'
import ChatInputBar from './ChatInputBar.jsx'
import AgentContextInspector from './AgentContextInspector.jsx'
import ComposerPopover from './ComposerPopover.jsx'
import ConnectionStatus from './ConnectionStatus.jsx'
import StreamingMessage from './StreamingMessage.jsx'
import QueuedMessages from './QueuedMessages.jsx'
import MsgContent from './MsgContent.jsx'
import { formatResetTime } from './resetTime.js'
import { questionKey } from './questionKey.js'
import { resolveStopResend } from './resolveStopResend.js'
import { focusComposerElement, shouldApplyComposerFocusRequest } from './composerFocusPolicy.js'
import { chooseActiveAssistantSurface, findTrailingAssistantPartialIndex, promoteAssistantStream, streamItemsHaveRenderableContent } from './streamPromotion.js'
import {
  canFastForwardQueue,
  continuationRowsFromPromotedMessage,
  openAppCtaViewModel,
  previewReadyAnnouncement,
  resolveFreshPinRetarget,
  resolveSteeredPinDecision,
  shouldRetryStopAfterConfirm,
  stopConfirmedIdle,
  stopRequestSucceeded,
  serverSnapshotBehindLocal,
  shouldShowOpenAppCta,
  startedMessagesFromResponse,
  systemEventForChat,
} from './chatRuntimeState.js'
import {
  EMPTY_BUILD_PHASE_RAIL,
  accumulateBuildPhase,
  buildPhaseRailViewModel,
  latestBuildPhaseAnnouncement,
  railAtRunStart,
} from './buildPhaseRail.js'
import './ChatView.css'


// Cache touch-primary detection. Updated dynamically if input devices change.
const _touchMql = typeof matchMedia === 'function'
  ? matchMedia('(hover: none) and (pointer: coarse)')
  : null
let _isTouchPrimary = _touchMql?.matches ?? false
_touchMql?.addEventListener('change', (e) => { _isTouchPrimary = e.matches })

const EMPTY_PROMPTS = [
  { label: 'Build an app', prompt: 'Build a simple app for tracking personal projects.' },
  { label: 'Plan a task', prompt: 'Help me break down a task I need to finish this week.' },
  { label: 'Analyze an idea', prompt: 'Help me think through an idea and find the sharpest next step.' },
]

const STOP_RETRY_DELAYS_MS = [0, 250, 700, 1200]

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

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
      && a.question_id === b.question_id
      // The error-card fields: a warm DB refresh can deliver a message that
      // differs ONLY in these (e.g. boot reconcile stamped resumable +
      // pause_kind onto an existing drain note, or a coalescing error event
      // rewrote message/park fields). Skipping them froze a stale red card
      // on screen until a remount.
      && a.message === b.message && a.resumable === b.resumable
      && a.parked_until === b.parked_until
      && a.park_reason === b.park_reason
      && a.pause_kind === b.pause_kind
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

function appendMessageBatch(prev, rows) {
  const batch = Array.isArray(rows) ? rows.filter(Boolean) : []
  if (batch.length === 0) return prev
  const seenTs = new Set(prev.map(m => m?.ts).filter(v => v != null))
  const nextRows = batch.filter(m => {
    if (m.ts == null) return true
    if (seenTs.has(m.ts)) return false
    seenTs.add(m.ts)
    return true
  })
  return nextRows.length ? [...prev, ...nextRows] : prev
}

function insertMessageBatchByTs(prev, rows) {
  const batch = Array.isArray(rows) ? rows.filter(Boolean) : []
  if (batch.length === 0) return prev
  const seenTs = new Set(prev.map(m => m?.ts).filter(v => v != null))
  const next = [...prev]
  let changed = false
  for (const row of batch) {
    if (row.ts != null) {
      if (seenTs.has(row.ts)) continue
      seenTs.add(row.ts)
      const insertAt = next.findIndex(m => m?.ts != null && m.ts > row.ts)
      if (insertAt >= 0) next.splice(insertAt, 0, row)
      else next.push(row)
    } else {
      next.push(row)
    }
    changed = true
  }
  return changed ? next : prev
}

function replaceOptimisticWithBatch(prev, optimisticTs, rows) {
  const base = optimisticTs == null
    ? prev
    : prev.filter(m => !(m?.role === 'user' && m.ts === optimisticTs))
  return appendMessageBatch(base, rows)
}

function findOptimisticUserIndex(messages, ts) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (msg?.role === 'user' && msg.ts === ts) return i
  }
  return -1
}

/** Settle the scroll mode for a send that did NOT pin (the reader is
 *  scrolled up, or a caller opted out via pin:false).
 *
 *  The mode that misbehaves on a non-pin send is a STALE PIN_USER_MSG left
 *  by an earlier send: re-applying it would move scrollTop even though the
 *  user is reading above the tail. Convert that stale pin into the reader's
 *  current scroll position (ANCHOR_AT). The bottom spacer is now independent
 *  from pinning, so it can still reserve room below the newest user message
 *  without moving the reader. By default FOLLOW_BOTTOM is left alone because
 *  pin:false synthetic sends (e.g. handleStop's queue-collapse) intentionally
 *  keep following. Real user sends/steers that choose not to pin pass
 *  retireFollow:true so a stale FOLLOW_BOTTOM cannot yank a scrolled-up reader
 *  when the delayed message becomes visible. */
function settleNonPinMode(modeRef, scrollEl, { retireFollow = false } = {}) {
  const kind = modeRef.current?.kind
  if (kind !== 'PIN_USER_MSG' && !(retireFollow && kind === 'FOLLOW_BOTTOM')) {
    return
  }
  const anchor = anchorModeFromScroll(scrollEl)
  if (anchor) modeRef.current = anchor
}

// Exported so sibling components (Shell, etc.) can clean up drafts when a
// chat is deleted.  Shell owns the deletion flow; it should call this after
// the chat row is removed from the list.
// NOTE: if deletion ever moves inside ChatView's own scope, call this inline
// instead of leaving the orphaned key behind.
export function deleteChatDraft(chatId) {
  try { sessionStorage.removeItem(`draft:${chatId}`) } catch { /* private browsing */ }
}

// Evict the oldest draft: key from sessionStorage so a new draft can land.
// Oldest = smallest numeric suffix after the colon; that's the chat that
// was least recently opened (chats get integer IDs assigned in order).
function evictOldestDraft() {
  try {
    const draftKeys = []
    for (let i = 0; i < sessionStorage.length; i++) {
      const key = sessionStorage.key(i)
      if (key?.startsWith('draft:')) draftKeys.push(key)
    }
    if (draftKeys.length === 0) return
    // Sort ascending by the numeric part; remove the oldest (lowest ID).
    draftKeys.sort((a, b) => {
      const na = parseInt(a.slice(6), 10) || 0
      const nb = parseInt(b.slice(6), 10) || 0
      return na - nb
    })
    sessionStorage.removeItem(draftKeys[0])
  } catch { /* ignore */ }
}

const PENDING_DRAFT_KEY = 'pending-draft'
const PENDING_DRAFT_AUTOSEND_KEY = 'pending-draft-autosend'
const DRAFT_AUTOSEND_PREFIX = 'draft-autosend:'

function readInitialComposer(chatId) {
  try {
    const pending = sessionStorage.getItem(PENDING_DRAFT_KEY)
    const saved = sessionStorage.getItem(`draft:${chatId}`) || ''
    const input = pending || saved
    const autoSendDraft =
      sessionStorage.getItem(PENDING_DRAFT_AUTOSEND_KEY) ||
      sessionStorage.getItem(`${DRAFT_AUTOSEND_PREFIX}${chatId}`)
    return {
      input,
      autoSend: !!input && autoSendDraft === input,
    }
  } catch {
    return { input: '', autoSend: false }
  }
}

// Stable empty default so callers that pass no built apps (the embedded
// composer) don't hand ChatView a fresh array each render and re-fire its
// list-keyed effects.
const NO_BUILT_APPS = []

export default function ChatView({
  chatId,
  onStreamEnd,
  onFirstMessage,
  onSystemEvent,
  onChatMissing,
  builtApps = NO_BUILT_APPS,
  recompilePulse = null,
  onOpenApp,
  onMessageStart,
  onVoiceListeningChange,
  showPicker = true,
  embedded = false,
  quickActions = null,
  getContext = null,
  composerFocusRequest = null,
  onComposerFocusHandled = null,
}) {
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
  // Bumped by the load-error Retry button to re-run the load effect in
  // place, instead of a hard window.location.reload (which would nuke the
  // Query cache, scroll positions, drafts, the app-iframe LRU, and the
  // back-stack — and contradicts the project's no-hard-reload principle).
  const [loadNonce, setLoadNonce] = useState(0)
  const [sending, setSending] = useState(() => !!cached?.running)
  // Server-hydrated running marker. `sending` is the local UI flag and
  // `isStreaming` belongs to the SSE hook; both can briefly be false across
  // app/chat remounts or reconnect windows even though the backend still has
  // an active run. Keep the durable server verdict separate so the composer
  // does not fall back to Mic while a turn is still running with queued work.
  const [serverRunning, setServerRunning] = useState(() => !!cached?.running)
  const serverRunningRef = useRef(!!cached?.running)
  function setServerRunningState(v) {
    const running = !!v
    serverRunningRef.current = running
    setServerRunning(running)
    queryClient.setQueryData(chatMessagesQueryKey(chatId), (existing) => existing
      ? { ...existing, running }
      : existing
    )
  }
  const initialComposerRef = useRef(null)
  if (!initialComposerRef.current) {
    initialComposerRef.current = readInitialComposer(chatId)
  }
  const [input, setInput] = useState(() => initialComposerRef.current.input)
  const [autoSendPendingDraft, setAutoSendPendingDraft] = useState(
    () => initialComposerRef.current.autoSend,
  )
  const autoSendAttemptedRef = useRef(false)

  useEffect(() => {
    const initial = initialComposerRef.current.input
    try {
      if (sessionStorage.getItem(PENDING_DRAFT_KEY) === initial) {
        sessionStorage.removeItem(PENDING_DRAFT_KEY)
      }
      if (sessionStorage.getItem(PENDING_DRAFT_AUTOSEND_KEY) === initial) {
        sessionStorage.removeItem(PENDING_DRAFT_AUTOSEND_KEY)
      }
    } catch { /* private browsing */ }
  }, [])

  // Per-chat agent runtime config (provider, agent_settings_json,
  // effective_agent_settings, has_assistant_turns). Resolved by the
  // initial /chats/{id} fetch and used to drive ChatSettingsPanel
  // (the model + effort picker inside the `+` popover). Stays null
  // until the fetch lands; the picker simply hides until then.
  const [chatInfo, setChatInfo] = useState(null)
  // The question_id of the AskUserQuestion the runner is currently parked
  // on, set from the live SSE `question` event (onLiveQuestion). It is a
  // FAST-PATH HINT only, never the sole gate: the backend does not persist
  // a `pending_question_id`, so on a fresh load / navigate-back it is null
  // (we never saw the live event), and answerability falls back to the
  // durable "tail unanswered question of the last assistant message"
  // invariant. See isQuestionAnswerable in the render. (The `cached`
  // read is forward-compat: harmlessly null today, it would pick up a
  // persisted pending_question_id if one is ever added.)
  const [liveQuestionId, setLiveQuestionId] = useState(() => cached?.pending_question_id ?? null)
  // True while a pending (unanswered) question card exists but is scrolled
  // out of the viewport — drives the footer "tap to answer" chip. Written
  // only by the IntersectionObserver effect below (near hasPendingQuestion).
  const [pendingCardOffscreen, setPendingCardOffscreen] = useState(false)
  // True while a resumable tail pause/park card exists but is scrolled out of
  // the viewport — drives the footer "tap to resume" chip. Written only by the
  // IntersectionObserver effect below (near hasPendingResume), mirroring the
  // pending-question nudge.
  const [resumeCardOffscreen, setResumeCardOffscreen] = useState(false)
  const [showInspector, setShowInspector] = useState(false)
  const [previewReadyStatus, setPreviewReadyStatus] = useState('')
  const lastAnnouncedPreviewRef = useRef(null)
  // The app id whose CTA is mid recompile-pulse (label swapped to "Preview
  // updated ✓" for ~2s), or null.
  const [pulsedAppId, setPulsedAppId] = useState(null)
  // Mirror of the built-app list so the pulse effect can look up the recompiled
  // app without taking builtApps as a dep (which would re-fire it on any list
  // change, not just a fresh pulse).
  const builtAppsRef = useRef(builtApps)
  useEffect(() => { builtAppsRef.current = builtApps }, [builtApps])
  // Build-milestone rail: phases accumulated from chat-scoped `build_phase`
  // stream events (deduped by ts so catch-up replay rebuilds it), reset ONLY
  // when a new run starts for this chat (see buildPhaseRail.js for why a
  // mid-run reset is replay-incoherent). Rendered as a slim rail in the foot
  // near the open-app CTA; the announcement mirrors previewReadyStatus for
  // the polite live region.
  const [buildPhases, setBuildPhases] = useState(EMPTY_BUILD_PHASE_RAIL)
  const [buildPhaseStatus, setBuildPhaseStatus] = useState('')
  const lastAnnouncedPhaseRef = useRef(null)

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
  const pendingQueue = usePendingQueue(cached?.pending_messages || [])
  const queuedContinuationLocalPromotedRef = useRef(null)
  const queuedContinuationPinIntentRef = useRef(null)
  const queuedPinIntentByCidRef = useRef(new Map())
  const queuedPinIntentByTsRef = useRef(new Map())
  const steerPinIntentRef = useRef(null)
  const inlineSteerPinIntentRef = useRef(null)
  const runtimeReconnectInFlightRef = useRef(false)
  const swReloadHoldTimerRef = useRef(null)

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
  const measureComposerHeight = useCallback(() => {
    const chatEl = chatRef.current
    const footEl = footRef.current
    if (!chatEl || !footEl) return
    chatEl.style.setProperty('--composer-h', `${footEl.offsetHeight}px`)
  }, [])

  // A drawer "New chat" tap should leave desktop-web users ready to type.
  // Keep this as an explicit one-shot request from Shell instead of a blanket
  // autofocus-on-mount: selecting existing chats should not steal focus, and
  // touch-primary devices should not pop the soft keyboard unexpectedly.
  useEffect(() => {
    const token = composerFocusRequest?.token
    if (token == null) return
    const requestChatId = composerFocusRequest?.chatId
    if (requestChatId == null || String(requestChatId) !== String(chatId)) return

    if (!shouldApplyComposerFocusRequest({
      focusRequest: composerFocusRequest,
      chatId,
      embedded,
      isTouchPrimary: _isTouchPrimary,
    })) {
      onComposerFocusHandled?.(token)
      return
    }

    let cancelled = false
    const raf = requestAnimationFrame(() => {
      if (cancelled) return
      focusComposerElement(inputRef.current)
      onComposerFocusHandled?.(token)
    })
    return () => {
      cancelled = true
      cancelAnimationFrame(raf)
    }
  }, [chatId, composerFocusRequest, embedded, onComposerFocusHandled])

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
  const [bridgeMountInputs, setBridgeMountInputs] = useState(() => ({
    runningAtMount: !!cached?.running,
    lastMsgAtMount: cached?.messages?.length
      ? cached.messages[cached.messages.length - 1]
      : null,
  }))
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
  // Re-entrancy guard for doSendSilent (answer submissions). sendingRef
  // alone cannot guard doSendSilent because answer sends are deliberately
  // allowed while sendingRef is true (the runner is parked waiting for
  // the answer). A dedicated flag flipped synchronously at entry protects
  // against a fast double-tap submitting the same answer twice.
  const sendSilentInFlightRef = useRef(false)

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
  const onStreamEndRef = useRef(onStreamEnd)
  onStreamEndRef.current = onStreamEnd
  // getContext: optional callback that returns a Promise<object|null> with
  // the current app state snapshot. Called on the fresh-send path only (not
  // the queue path, which is already mid-turn). The result is serialized as a
  // compact <app_state> block appended to the outgoing message content so the
  // backend agent receives it alongside the user's text.
  const getContextRef = useRef(getContext)
  getContextRef.current = getContext

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
  //   • userScrollIntentVersionRef
  //                           — bumped only by human scroll input; delayed
  //                             queued/steered sends honor their pin intent
  //                             only if this has not changed since submit.
  //   • revealed              — apply to .chat__scroll style for the
  //                             hide-then-reveal scroll restore.
  //
  // See useScrollMode.js + ARCHITECTURE.md "Chat scroll + steer
  // contract" for full design.
  const {
    modeRef,
    gestureWindowUntilRef,
    userScrollIntentVersionRef,
    revealed,
  } = useScrollMode({
    chatId,
    scrollRef,
    spacerRef,
    lastUserMsgRef,
    messages,
    messagesRef,
    pendingMessagesLength: pendingQueue.pendingMessages.length,
    loadingOlderRef: loadingOlder,
  })

  function makeSendPinIntent(willPin) {
    return {
      willPin: !!willPin,
      userScrollIntentVersion: userScrollIntentVersionRef.current,
    }
  }

  function pinIntentStillCurrent(intent) {
    return !!intent
      && intent.userScrollIntentVersion === userScrollIntentVersionRef.current
  }

  function rememberQueuedPinIntent(cid, intent) {
    if (!cid || !intent) return
    queuedPinIntentByCidRef.current.set(cid, intent)
  }

  function rememberQueuedPinIntentTs(cid, ts) {
    if (ts == null) return
    const intent = cid ? queuedPinIntentByCidRef.current.get(cid) : null
    if (intent) queuedPinIntentByTsRef.current.set(ts, intent)
  }

  function forgetQueuedPinIntent({ cid = null, ts = null, tsList = null } = {}) {
    if (cid) queuedPinIntentByCidRef.current.delete(cid)
    if (ts != null) queuedPinIntentByTsRef.current.delete(ts)
    if (Array.isArray(tsList)) {
      for (const value of tsList) queuedPinIntentByTsRef.current.delete(value)
    }
  }

  function takeQueuedPinIntent({ tsList = null, ts = null, localPromoted = null } = {}) {
    const keys = []
    if (Array.isArray(tsList)) keys.push(...tsList.filter(v => v != null))
    if (ts != null) keys.push(ts)
    let intent = null
    for (const key of keys) {
      if (!intent && queuedPinIntentByTsRef.current.has(key)) {
        intent = queuedPinIntentByTsRef.current.get(key)
      }
      queuedPinIntentByTsRef.current.delete(key)
    }
    const cid = localPromoted?.cid
    if (cid) {
      if (!intent && queuedPinIntentByCidRef.current.has(cid)) {
        intent = queuedPinIntentByCidRef.current.get(cid)
      }
      queuedPinIntentByCidRef.current.delete(cid)
    }
    return intent
  }

  function forgetAllQueuedPinIntents() {
    queuedPinIntentByCidRef.current.clear()
    queuedPinIntentByTsRef.current.clear()
    queuedContinuationPinIntentRef.current = null
    inlineSteerPinIntentRef.current = null
  }

  // Re-fetch messages from the API. Called when the SSE stream reconnects
  // and gets a 204 (no active broadcast — the chat finished while the
  // user was offline or on poor connectivity). Replaces stale messages
  // with the current DB state.
  const fetchMessages = useCallback(async ({ force = false, terminal204 = false } = {}) => {
    if (sendingRef.current && !force) return
    const gen = fetchGenRef.current
    try {
      const res = await apiFetch(`/chats/${chatId}?limit=20`, { timeoutMs: 15000 })
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
      const preserveLocalTurn =
        force && (sendingRef.current || isStreamingRef.current || serverRunningRef.current)
      const staleSnapshot =
        !terminal204
        && !preserveLocalTurn
        && serverSnapshotBehindLocal(msgs, messagesRef.current)
      if (!preserveLocalTurn && !staleSnapshot) {
        commitMessages(msgs, data.offset || 0)
      }
      if (data.running) {
        setSending(true)
      } else if (force && !preserveLocalTurn && !staleSnapshot) {
        setSending(false)
        sendingRef.current = false
      }
      if (data.running || (!preserveLocalTurn && !staleSnapshot)) {
        setServerRunningState(!!data.running)
      }
      setLiveQuestionId(data.pending_question_id || null)
      queryClient.setQueryData(chatMessagesQueryKey(chatId), (existing) => ({
        ...(existing || {}),
        running: !!data.running,
        pending_messages: data.pending_messages || [],
        pending_question_id: data.pending_question_id || null,
      }))
      // Reconcile pending queue against authoritative server state.
      // hydrate() already preserves truly optimistic/in-flight local rows
      // whose POST has not committed yet. Server-confirmed rows omitted from
      // pending_messages have been consumed/cancelled/steered and must be
      // dropped even while the agent turn is still running; preserving them
      // creates ghost queue chips that cannot be fast-forwarded.
      if (!preserveLocalTurn) {
        pendingQueue.hydrate(data.pending_messages || [])
      }
    } catch { /* network error — silent, user can retry */ }
  }, [chatId, commitMessages, pendingQueue.hydrate, queryClient])

  // Active-turn runtime reconciliation. The SSE stream is authoritative for
  // assistant output, but queued-message affordances depend on the durable
  // Chat.running + Chat.pending_messages fields. Mobile backgrounding, an
  // old service-worker client, or a queue POST that acks without canonical
  // pending_message can leave the mounted view showing a stale Stop button
  // until some unrelated local event (like focusing the composer) causes a
  // refresh. While a turn or visible queue exists, poll the small chat state
  // payload and hydrate only runtime fields — do not replace the transcript.
  const reconcileRuntimeState = useCallback(async () => {
    const gen = fetchGenRef.current
    try {
      const res = await apiFetch(`/chats/${chatId}?limit=1`, { timeoutMs: 15000 })
      const data = await res.json()
      if (chatIdStaleRef.current) return
      if (fetchGenRef.current !== gen) return
      const serverPending = data.pending_messages || []
      // The SSE stream is the source of truth for "a turn is live" — this poll
      // is only a fallback. While the stream is alive (isStreamingRef) or a Stop
      // is in flight, local optimistic state is authoritative: this background
      // poll must NOT tear down sending nor hydrate/clobber the queue (the poll
      // racing the optimistic queue was the steer + handleStop e2e flake). Only
      // when the stream is genuinely dead (a stale Stop with no real turn) does
      // the server snapshot win. Event-driven over polling — see
      // docs/architecture.md "determinism".
      const localAuthoritative =
        handlingStopRef.current || isStreamingRef.current
      if (data.running) {
        setSending(true)
      } else if (serverPending.length === 0 && !localAuthoritative) {
        // Stream is dead and the server is idle+empty: clear the stale Stop.
        setSending(false)
        sendingRef.current = false
      }
      setServerRunningState(!!data.running)
      setLiveQuestionId(data.pending_question_id || null)
      queryClient.setQueryData(chatMessagesQueryKey(chatId), (existing) => ({
        ...(existing || {}),
        running: !!data.running,
        pending_messages: serverPending,
        pending_question_id: data.pending_question_id || null,
      }))
      // Don't let the fallback poll add/clobber the queue while a turn is live
      // (localAuthoritative, above) — the optimistic queue + swapOptimisticTs
      // own it during a turn; hydrate only when the stream is dead.
      if (!localAuthoritative) {
        pendingQueue.hydrate(serverPending)
      }
    } catch { /* background reconciliation is best-effort */ }
  }, [chatId, pendingQueue.hydrate, pendingQueue.pendingMessagesRef, queryClient])

  const handleCompactionStored = useCallback(
    () => fetchMessages({ force: true }),
    [fetchMessages],
  )

  const {
    streamItems,
    latestItemsRef,
    isStreaming,
    isStreamingRef,
    connectionError,
    reconnecting,
    sendMessage: streamSend,
    connectToStream,
    retry,
    disconnect,
    clearStreamItems,
    patchQuestionAnswers,
  } = useStreamConnection(chatId, {
    onStreamEnd: ({ continues, promotedMessage } = {}) => {
      promoteStreamToMessages()
      if (continues) {
        // Backend auto-promoted queued follow-ups into the next turn. Newer
        // backend code persists the visible rows separately while sending
        // combined text to the provider; older code returned one combined row.
        // The local queue was already trimmed when the
        // queued_turn_starting event arrived, so a message queued after
        // that event cannot be accidentally folded into this turn here.
        const localPromoted = queuedContinuationLocalPromotedRef.current
        queuedContinuationLocalPromotedRef.current = null
        const continuationPinIntent = queuedContinuationPinIntentRef.current
        queuedContinuationPinIntentRef.current = null
        const promotedRows = continuationRowsFromPromotedMessage(
          promotedMessage,
          localPromoted,
        )
        if (promotedRows.length > 0) {
          // A queued continuation is still a user send becoming the active
          // turn, so it follows the same send rule (see shouldPinSend):
          // pin only when first-or-at-bottom. Read the first-user check
          // before the append. When not pinning, leave the reader where
          // the previous turn left them — the continuation just appears
          // below without moving the scroll.
          const contIsFirstUser = !messagesRef.current.some(
            m => m.role === 'user' && !m.hidden,
          )
          const pinTargetTs = promotedRows[0]?.ts
          const fallbackWillPin = () => shouldPinSend({
            scrollEl: scrollRef.current,
            mode: modeRef.current,
            isFirstUserMsg: contIsFirstUser,
            respectFollowMode: false,
          })
          const intentStillCurrent = continuationPinIntent
            ? pinIntentStillCurrent(continuationPinIntent)
            : true
          const contWillPin = pinTargetTs != null && intentStillCurrent && (
            continuationPinIntent
              ? continuationPinIntent.willPin
              : fallbackWillPin()
          )
          commitMessages(prev => appendMessageBatch(prev, promotedRows))
          promotedRef.current = false
          setSpacerActive(true)
          if (contWillPin) {
            if (spacerRef.current) spacerRef.current.style.height = '0px'
            modeRef.current = { kind: 'PIN_USER_MSG', ts: pinTargetTs }
          } else if (intentStillCurrent) {
            settleNonPinMode(modeRef, scrollRef.current, { retireFollow: true })
          }
        } else {
          // Server's promoted ts isn't in our local queue (cancel raced
          // with promote). Refetch authoritative state.
          promotedRef.current = false
          fetchMessages({ force: true })
        }
        setSending(true)
        setServerRunningState(true)
      } else {
        queuedContinuationLocalPromotedRef.current = null
        queuedContinuationPinIntentRef.current = null
        setSending(false)
        sendingRef.current = false
        setServerRunningState(false)
        // Stream ended without continuation. If we have local pending
        // entries, server may have cleared them (auth fail, error) —
        // refetch to reconcile. Skip when pending empty.
        if (pendingQueue.pendingMessagesRef.current.length > 0) {
          fetchMessages({ force: true })
        }
      }
      onStreamEnd?.({ continues })
    },
    onSystemEvent: event => {
      // A build_phase is chat-local: it only feeds this chat's milestone rail,
      // so accumulate it here (deduped by ts) instead of forwarding it to the
      // Shell, which has no handler for it.
      if (event?.type === 'build_phase') {
        setBuildPhases(prev => accumulateBuildPhase(prev, event))
        return
      }
      onSystemEvent?.(systemEventForChat(event, chatId))
    },
    onNeedsRefresh: fetchMessages,
    onQueuedTurnStarting: ({ ts, message } = {}) => {
      // A queued message is being promoted into its OWN run — the rail's
      // run-start boundary for queue drains. useStreamConnection fires this
      // callback during catch-up replay too, in event order, so a reconnect
      // that replays the old run's log applies this reset at the same
      // position the live stream did (old-run phases, then reset) and the
      // rail always lands on the run being displayed.
      setBuildPhases(railAtRunStart())
      const consumedTs = message?._consumed_ts
      const serverRows = Array.isArray(message?._messages)
        ? message._messages.map(stripInternalUserMessageFields).filter(Boolean)
        : null
      const localPromoted = Array.isArray(consumedTs)
        ? pendingQueue.promoteManyByTs(consumedTs)
        : pendingQueue.promoteAll(ts)
      queuedContinuationLocalPromotedRef.current =
        serverRows?.length ? serverRows : localPromoted
      queuedContinuationPinIntentRef.current = takeQueuedPinIntent({
        tsList: consumedTs,
        ts,
        localPromoted,
      })
    },
    onLiveQuestion: setLiveQuestionId,
    onSteeredIntoTurn: ({ ts, content, messages: steeredBatch }) => {
      // A send was injected mid-turn into a live turn (steering — fired for
      // both providers when Stop is pressed with a queued message). The
      // backend seals the assistant text streamed so far, persists the user
      // message, and then continues the assistant after that boundary. Mirror
      // that exact shape locally: first promote the current live stream
      // segment into `messages`, then append the steered user row, then let
      // future text deltas build a fresh streaming assistant block.
      //
      // It still follows the USER-SEND scroll rule: if the reader was at the
      // bottom when they tapped fast-forward, the steered message pins to the
      // top and gets the same bottom spacer as a normal send. If the reader
      // was scrolled up, leave them anchored. Earlier code treated steering
      // as generic content growth and never armed the spacer, so fast-forward
      // could work while the message stayed low in the viewport with no
      // reserved room below it.
      //
      const steeredMessages = Array.isArray(steeredBatch) && steeredBatch.length > 0
        ? steeredBatch.map((m, i) => ({
            role: 'user',
            content: m?.content || '',
            ts: m?.ts ?? (ts != null ? ts + i : Date.now() + i),
            ...(m?.attachments ? { attachments: m.attachments } : {}),
          }))
        : [{ role: 'user', content, ts: ts ?? Date.now() }]
      const pinTargetTs = steeredMessages[0]?.ts
      const steeredTsList = steeredMessages
        .map(m => m?.ts)
        .filter(v => v != null)
      const pinIntent = steerPinIntentRef.current
        || inlineSteerPinIntentRef.current
        || takeQueuedPinIntent({ tsList: steeredTsList, ts })
      inlineSteerPinIntentRef.current = null
      promoteStreamToMessages({ keepTurnOpen: true })
      const steeredIsFirstUser = !messagesRef.current.some(
        m => m.role === 'user' && !m.hidden,
      )
      const fallbackWillPin = () => shouldPinSend({
        scrollEl: scrollRef.current,
        mode: modeRef.current,
        isFirstUserMsg: steeredIsFirstUser,
        respectFollowMode: false,
      })
      const {
        intentStillCurrent: pinStillValid,
        shouldPin: shouldPinSteered,
      } = resolveSteeredPinDecision({
        pinTargetTs,
        pinIntent,
        pinIntentStillCurrent,
        fallbackWillPin,
      })
      // Dedup by ts so a reconnect's catch-up replay of the same event
      // can't double-insert the steered user message. Insert by transcript ts
      // instead of blindly appending: if a fetch/replay already committed the
      // post-steer assistant row, the steered user still belongs before it.
      // Arm the scroll mode BEFORE rendering the steered row. EventSource
      // callbacks are outside React's synthetic event layer, and query-cache
      // listeners can observe the transcript update immediately; setting the
      // mode first prevents a one-frame "row appears low, then snaps up" steer.
      setSpacerActive(true)
      if (shouldPinSteered) {
        if (spacerRef.current) spacerRef.current.style.height = '0px'
        modeRef.current = { kind: 'PIN_USER_MSG', ts: pinTargetTs }
      } else if (pinStillValid) {
        settleNonPinMode(modeRef, scrollRef.current, { retireFollow: true })
      }
      commitMessages(prev => insertMessageBatchByTs(prev, steeredMessages))
      steerPinIntentRef.current = null
    },
  })

  const ensureRuntimeStreamConnected = useCallback(() => {
    if (connectionError === 'disconnected') return
    if (!serverRunningRef.current) return
    if (isStreamingRef.current) return
    if (runtimeReconnectInFlightRef.current) return

    runtimeReconnectInFlightRef.current = true
    // The durable chat row can say "running" while this mounted mobile
    // client has no live SSE attached: Android can pause/kill the fetch
    // during app switch, network handoff, or a shell rebuild. Reconnect
    // from the server verdict instead of waiting for a full remount.
    Promise.resolve(connectToStream(true))
      .catch(() => {})
      .finally(() => {
        runtimeReconnectInFlightRef.current = false
      })
  }, [connectToStream, connectionError, isStreamingRef])

  const {
    files: pendingFiles,
    addFiles,
    removeFile,
    clearFiles,
    restoreFiles,
    releaseFiles,
  } = useFileUpload({ chatId })

  function restoreComposerText(text, { focus = false } = {}) {
    setInput(text)
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.style.height = 'auto'
      const h = Math.min(el.scrollHeight, 280)
      el.style.height = `${h}px`
      el.closest('.chat__pill')?.classList.toggle('chat__pill--tall', h > 45)
      if (focus) {
        try { el.focus({ preventScroll: true }) }
        catch { el.focus() }
      }
      const end = String(text).length
      try { el.setSelectionRange(end, end) } catch {}
      el.scrollTop = el.scrollHeight
    })
  }

  const { listening, listeningRef, stopVoice, toggleVoice } = useVoiceInput({
    onTranscript: (text) => setInput(text),
    inputRef,
  })
  useEffect(() => {
    onVoiceListeningChange?.(chatId, listening)
    return () => { onVoiceListeningChange?.(chatId, false) }
  }, [chatId, listening, onVoiceListeningChange])

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
  function promoteStreamToMessages({ keepTurnOpen = false } = {}) {
    if (promotedRef.current && !keepTurnOpen) return
    const items = latestItemsRef.current
    if (items.length === 0) return
    // A steer can cut over before the assistant emitted any real output — the
    // only buffered item is an empty/whitespace token. Sealing it would leave a
    // stray empty assistant bubble before the steered user row (the card-166
    // orphaned fragment). Drop the empty pre-steer segment: keep the turn open
    // (the live items already cleared below) so the post-steer continuation
    // becomes the turn's first assistant message, in the right place. A single
    // REAL token ("I ") is renderable and still seals — we only skip when there
    // is nothing worth keeping.
    if (keepTurnOpen && !streamItemsHaveRenderableContent(items)) {
      clearStreamItems?.()
      return
    }
    promotedRef.current = true

    // Decide REPLACE-vs-APPEND against the captured mounted partial.
    // Usually that partial is still the last message. Fast-forward is the
    // exception: it inserts a steered user row below the still-live partial,
    // and the active stream continues after that row. The bridge must still
    // replace the original partial by ts instead of appending duplicated
    // assistant text below the steered row.
    const bridgeIdx = bridgeHook.findBridgeIndex(messagesRef.current)
    const trailingIdx = bridgeIdx >= 0 ? -1 : findTrailingAssistantPartialIndex(messagesRef.current)
    const bridgeTs = bridgeIdx >= 0
      ? messagesRef.current[bridgeIdx]?.ts
      : (trailingIdx >= 0 && assistantStreamCoversMessage(messagesRef.current[trailingIdx], items)
          ? messagesRef.current[trailingIdx]?.ts
          : null)
    bridgeHook.markBridged()
    commitMessages(
      prev => promoteAssistantStream(prev, { items, bridgeTs }),
      undefined,
      { force: true },
    )
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
    if (keepTurnOpen) {
      // Steering is a semantic boundary INSIDE the still-running turn. The
      // pre-steer assistant segment has just been sealed, but the post-steer
      // continuation must still be promotable on the eventual `done` event.
      promotedRef.current = false
    }
  }

  // Persist draft so it survives leaving and re-entering the chat.
  useEffect(() => {
    try {
      if (input) sessionStorage.setItem(`draft:${chatId}`, input)
      else sessionStorage.removeItem(`draft:${chatId}`)
    } catch (e) {
      // QuotaExceededError: evict the oldest draft: key and retry once so the
      // current chat's draft is always fresh.
      if (e?.name === 'QuotaExceededError' || e?.code === 22) {
        evictOldestDraft()
        try {
          if (input) sessionStorage.setItem(`draft:${chatId}`, input)
        } catch { /* still no room — skip */ }
      }
      // Otherwise private browsing / storage disabled — silently skip.
    }
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
    const footEl = footRef.current
    if (!footEl) return

    let raf1 = 0
    let raf2 = 0
    const applySoon = () => {
      measureComposerHeight()
      if (raf1) cancelAnimationFrame(raf1)
      if (raf2) cancelAnimationFrame(raf2)
      raf1 = requestAnimationFrame(() => {
        measureComposerHeight()
        raf2 = requestAnimationFrame(measureComposerHeight)
      })
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') applySoon()
    }

    applySoon()
    const ro = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(applySoon)
      : null
    ro?.observe(footEl)
    window.addEventListener('resize', applySoon)
    window.addEventListener('pageshow', applySoon)
    window.visualViewport?.addEventListener('resize', applySoon)
    window.visualViewport?.addEventListener('scroll', applySoon)
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      if (raf1) cancelAnimationFrame(raf1)
      if (raf2) cancelAnimationFrame(raf2)
      ro?.disconnect()
      window.removeEventListener('resize', applySoon)
      window.removeEventListener('pageshow', applySoon)
      window.visualViewport?.removeEventListener('resize', applySoon)
      window.visualViewport?.removeEventListener('scroll', applySoon)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [measureComposerHeight])

  useEffect(() => {
    measureComposerHeight()
    const raf = requestAnimationFrame(measureComposerHeight)
    return () => cancelAnimationFrame(raf)
  }, [builtApps, sending, buildPhases, measureComposerHeight])

  useEffect(() => {
    const latest = buildPhases[buildPhases.length - 1]
    if (!latest) {
      lastAnnouncedPhaseRef.current = null
      setBuildPhaseStatus('')
      return
    }
    // Announce a phase once, keyed on its ts — an aria-live re-fire on every
    // rail change (e.g. an unrelated re-render) would re-read the same phrase.
    if (lastAnnouncedPhaseRef.current === latest.ts) return
    lastAnnouncedPhaseRef.current = latest.ts
    setBuildPhaseStatus(latestBuildPhaseAnnouncement(buildPhases))
  }, [buildPhases])

  // Announce the most recent built app (the newest CTA row) when it appears.
  useEffect(() => {
    const latest = builtApps.length > 0 ? builtApps[builtApps.length - 1] : null
    if (!shouldShowOpenAppCta(latest)) {
      lastAnnouncedPreviewRef.current = null
      setPreviewReadyStatus('')
      return
    }
    const key = `${latest.id}:${latest.name || ''}`
    if (lastAnnouncedPreviewRef.current === key) return
    lastAnnouncedPreviewRef.current = key
    setPreviewReadyStatus(previewReadyAnnouncement(latest))
  }, [builtApps])

  // Recompile pulse: Shell signals (with a nonce so repeats re-fire) that an
  // app just recompiled. If it's a live CTA here, flash it and announce the
  // update; the label swaps to "Preview updated ✓" for ~2s (see render).
  useEffect(() => {
    const appId = recompilePulse?.appId
    if (appId == null) return
    // Ignore a pulse meant for another chat — recompilePulse lingers in Shell
    // state, so a remount for a different chat could otherwise replay it.
    if (recompilePulse.chatId != null
        && String(recompilePulse.chatId) !== String(chatId)) return
    const app = builtAppsRef.current.find(a => Number(a.id) === Number(appId))
    if (!app) return
    setPulsedAppId(Number(appId))
    setPreviewReadyStatus(`Preview updated for ${app.name || 'app'}.`)
    const t = setTimeout(() => setPulsedAppId(null), 2000)
    return () => clearTimeout(t)
  }, [recompilePulse, chatId])

  // Fetch messages and connect to an in-progress stream if the agent is running.
  useEffect(() => {
    let cancelled = false
    chatIdStaleRef.current = false
    setLoadError(false)

    const gen = fetchGenRef.current
    apiFetch(`/chats/${chatId}?limit=20`)
      .then(r => {
        if (r.status === 404) throw new Error('CHAT_NOT_FOUND')
        if (!r.ok) throw new Error(`CHAT_LOAD_FAILED_${r.status}`)
        return r.json()
      })
      .then(data => {
        if (cancelled) return
        if (fetchGenRef.current !== gen) return
        const msgs = data.messages || []
        // Snapshot the per-chat runtime config (provider/model/effort) BEFORE
        // the behind-local guard below. This is independent of the messages
        // snapshot, and the guard's early-return used to skip it — so after any
        // interaction (local optimistic state ahead of the server snapshot) the
        // `+` popover's model picker silently vanished, leaving only Attach +
        // "What the agent knows". Setting it here keeps the picker present
        // regardless of the messages fast-path.
        setChatInfo({
          provider: data.provider || 'claude',
          agent_settings_json: data.agent_settings_json || null,
          effective: data.effective_agent_settings || {},
          has_assistant_turns: !!data.has_assistant_turns,
        })
        if (serverSnapshotBehindLocal(msgs, messagesRef.current)) {
          setLoading(false)
          return
        }

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
        setServerRunningState(!!data.running)
        hadMessagesRef.current = msgs.length > 0
        setLiveQuestionId(data.pending_question_id || null)
        queryClient.setQueryData(chatMessagesQueryKey(chatId), (existing) => ({
          ...(existing || {}),
          running: !!data.running,
          pending_messages: data.pending_messages || [],
          pending_question_id: data.pending_question_id || null,
        }))
        // (chatInfo — the model/effort picker config — is snapshotted above,
        // before the behind-local guard, so the picker survives interactions.)
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
        // doesn't drop the visible "queued" tray. The server list is
        // authoritative for confirmed rows; hydrate() still preserves
        // optimistic in-flight rows if a queue POST is racing this fetch.
        pendingQueue.hydrate(data.pending_messages || [])

        if (data.running) {
          setSending(true)
          connectToStream(false)
        } else {
          setSending(false)
          sendingRef.current = false
        }
      })
      .catch((err) => {
        if (cancelled) return
        setLoadError(true)
        setLoading(false)
        // A confirmed 404 means this chat is gone (deleted out-of-band, or an
        // off-list chat the restore probe had memoized as existing). Tell the
        // shell so it demotes to a live chat instead of stranding the user on a
        // dead chat's error screen. Network/other failures stay retryable.
        if (err && err.message === 'CHAT_NOT_FOUND') onChatMissing?.(chatId)
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
  }, [chatId, loadNonce])


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

    // Capture bottom intent BEFORE blurring the textarea. On mobile,
    // blur collapses the soft keyboard and can resize/clamp the visual
    // viewport before we compute the send rule; if we measure after
    // blur, a reader who was genuinely at the bottom can be misread as
    // scrolled up and the new user message won't pin to the top.
    const wasNearContentBottomAtSubmit = scrollRef.current
      ? isNearContentBottom(scrollRef.current)
      : null

    // On touch devices, blur to dismiss the soft keyboard. Desktop keeps
    // focus so the cursor stays ready for the next message.
    if (_isTouchPrimary) inputRef.current?.blur()

    // Callers can pre-supply attachments (e.g. handleStop collapsing
    // a queue that had files attached to queued items). When provided,
    // they replace the pendingFiles-derived list so data isn't lost.
    const usesComposerFiles = !Array.isArray(opts.attachments)
    const composerFileSnapshot = usesComposerFiles ? [...pendingFiles] : []
    const attachments = Array.isArray(opts.attachments)
      ? opts.attachments
      : pendingFiles
          .filter(f => f.status === 'done')
          .map(f => ({ name: f.name, size: f.size, mime_type: f.mime_type }))

    function clearComposerFilesForSend() {
      if (!usesComposerFiles) return
      // Hide the chips immediately for normal send UX, but do NOT revoke image
      // object URLs until the POST is accepted. A transient network failure must
      // restore the full composer state (text + staged files), not just text.
      clearFiles({ revoke: false })
    }
    function releaseComposerFilesAfterAccepted() {
      if (usesComposerFiles) releaseFiles(composerFileSnapshot)
    }
    function restoreComposerAfterFailedSend() {
      restoreComposerText(text)
      if (usesComposerFiles) restoreFiles(composerFileSnapshot)
    }

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
    if (
      sendingRef.current
      || isStreamingRef.current
      || serverRunningRef.current
      || pendingQueue.pendingMessagesRef.current.length > 0
    ) {
      const cid = (typeof crypto !== 'undefined' && crypto.randomUUID)
        ? crypto.randomUUID()
        : `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
      const queuedMsg = { role: 'user', content: text, ts: Date.now(), cid, queued: true }
      if (attachments.length > 0) queuedMsg.attachments = attachments
      pendingQueue.add(queuedMsg)
      // Capture the send rule's inputs AT SEND TIME, before the POST. If
      // this queued send is promoted into the active turn (the backend
      // returns started, either as `queued+started` or the `started` race),
      // it becomes a new visible user message and must follow the same pin
      // rule as a fresh send. The at-bottom / following decision and the
      // first-user check must reflect the moment of sending — reading them
      // AFTER `await streamSend(...)` lets a scroll during the POST flip the
      // decision. The user-scroll intent version lets us detect such a scroll
      // and yield to it (a user-driven scroll after send is the newer intent).
      const queuedIsFirstUser = !messagesRef.current.some(
        m => m.role === 'user' && !m.hidden,
      )
      const queuedWillPin = pin && shouldPinSend({
        scrollEl: scrollRef.current,
        mode: modeRef.current,
        isFirstUserMsg: queuedIsFirstUser,
        wasNearScrollBottom: wasNearContentBottomAtSubmit,
      })
      const queuedPinIntent = makeSendPinIntent(queuedWillPin)
      rememberQueuedPinIntent(cid, queuedPinIntent)
      inlineSteerPinIntentRef.current = queuedPinIntent
      setInput('')
      clearComposerFilesForSend()
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
        releaseComposerFilesAfterAccepted()
        if (result?.status === 'queued') {
          const canonicalPending = result.pending_message || null
          // Replace optimistic ts with server's (cid is stable).
          const ackTs = canonicalPending?.ts ?? result.ts
          rememberQueuedPinIntentTs(queuedMsg.cid, ackTs)
          pendingQueue.swapOptimisticTs(
            queuedMsg.cid,
            ackTs ?? queuedMsg.ts,
            result.position,
            canonicalPending,
            { confirmed: !!canonicalPending || typeof ackTs === 'number' },
          )
          if (!canonicalPending) {
            // Older backends acknowledge only {ts, position}. Hydrate
            // immediately so the queued row uses the server's canonical text
            // before the user taps fast-forward; otherwise upload/context
            // augmentation can make force-steer reject until a remount.
            fetchMessages({ force: true })
          }
          if (result.started) {
            if (Array.isArray(result.message?._consumed_ts)) {
              pendingQueue.promoteManyByTs(result.message._consumed_ts)
            }
            const startedMessages = startedMessagesFromResponse(result)
            if (startedMessages) {
              commitMessages(prev => appendMessageBatch(prev, startedMessages))
            }
            onMessageStartRef.current?.()
            promotedRef.current = false
            // started=true means this send began a NEW run (stale-pending
            // self-heal) rather than queueing behind one — a run start, so
            // the rail resets. A plain enqueue (started falsy) must NOT
            // touch the in-flight build's rail.
            setBuildPhases(railAtRunStart())
            setSending(true)
            setServerRunningState(true)
            // The queued send was promoted straight into the active turn,
            // so it's a new visible user message and follows the send rule
            // just like a fresh send. Use the at-send-time decision
            // (captured before the await) and yield to a user scroll that
            // changed the mode during the POST. Pin keys to the first SERVER
            // ts from the promoted batch, not the optimistic queuedMsg.ts.
            const pinStillValid = pinIntentStillCurrent(queuedPinIntent)
            const pinTargetTs = startedMessages?.[0]?.ts ?? queuedMsg.ts
            setSpacerActive(true)
            if (queuedWillPin && pinStillValid) {
              if (spacerRef.current) spacerRef.current.style.height = '0px'
              modeRef.current = {
                kind: 'PIN_USER_MSG',
                ts: pinTargetTs,
              }
            } else if (pinStillValid) {
              settleNonPinMode(modeRef, scrollRef.current, { retireFollow: pin })
            }
            forgetQueuedPinIntent({
              cid: queuedMsg.cid,
              ts: ackTs,
              tsList: result.message?._consumed_ts,
            })
            bridgeHook.markBridged()
          }
        }
        // Mid-turn steer: the backend delivered the send into the live
        // provider turn and persisted it in the transcript. The
        // `steered_into_turn` SSE event (handled in useStreamConnection's
        // onSteeredIntoTurn) renders the message inline, so drop the
        // optimistic queued-tray entry here — it never queued.
        if (result?.status === 'steered') {
          pendingQueue.cancelByCid(queuedMsg.cid)
          forgetQueuedPinIntent({ cid: queuedMsg.cid })
        }
        // Race: server said "started" though we expected queued.
        if (result?.status === 'started') {
          if (Array.isArray(result.message?._consumed_ts)) {
            pendingQueue.promoteManyByTs(result.message._consumed_ts)
          }
          pendingQueue.cancelByCid(queuedMsg.cid)
          onMessageStartRef.current?.()
          promotedRef.current = false
          // Same run-start semantics as the branch above: this send became
          // the first message of a NEW run, so the rail resets here too.
          setBuildPhases(railAtRunStart())
          // Apply the send rule before appending — see shouldPinSend and
          // the fresh-send path. A message that raced into a started turn
          // is still a new send becoming the active turn, so it pins only
          // when first-or-at-bottom. The decision was captured at send
          // time (before the await): reading scrollRef/modeRef HERE would
          // let a scroll during the POST flip it. `pinStillValid` yields to
          // a user-driven mode change that landed during the await.
          const startedMessages = startedMessagesFromResponse(result)
          commitMessages(prev => {
            if (startedMessages) return appendMessageBatch(prev, startedMessages)
            const { queued: _q, cid: _c, position: _p, ...msg } = queuedMsg
            return appendMessageBatch(prev, [msg])
          })
          setSending(true)
          setServerRunningState(true)
          // New visible user msg → pin to the top only when the rule
          // allows; otherwise convert any stale pin to the reader's
          // ANCHOR_AT (see settleNonPinMode) so the reader stays put with
          // reservation still available below. Pin keys to the first SERVER
          // ts when available; otherwise fall back to the optimistic ts.
          const startedPinStillValid = pinIntentStillCurrent(queuedPinIntent)
          const pinTargetTs = startedMessages?.[0]?.ts ?? queuedMsg.ts
          setSpacerActive(true)
          if (queuedWillPin && startedPinStillValid) {
            if (spacerRef.current) spacerRef.current.style.height = '0px'
            modeRef.current = {
              kind: 'PIN_USER_MSG',
              ts: pinTargetTs,
            }
          } else if (startedPinStillValid) {
            settleNonPinMode(modeRef, scrollRef.current, { retireFollow: pin })
          }
          forgetQueuedPinIntent({
            cid: queuedMsg.cid,
            tsList: result.message?._consumed_ts,
          })
          // This is a NEW turn (not the bridge turn from mount).
          // Retire the bridge gate so the upcoming promote appends a
          // fresh assistant instead of replacing whichever message
          // is currently last.
          bridgeHook.markBridged()
        }
        if (result?.status !== 'steered') {
          inlineSteerPinIntentRef.current = null
        }
        // Invariant: every observable queue-path status must resolve
        // the optimistic entry's in-flight flag. queued/steered/started
        // each clear it above (swapOptimisticTs / cancelByCid). Any
        // other status — e.g. streamSend's `not_steered` — leaves the
        // entry as an ordinary queued row, so clear the flag here or it
        // leaks forever and a later hydrate would wrongly preserve it.
        if (
          result?.status !== 'queued'
          && result?.status !== 'steered'
          && result?.status !== 'started'
        ) {
          pendingQueue.clearInFlight(queuedMsg.cid)
        }
      } catch (err) {
        // Roll back optimistic + restore input.
        pendingQueue.cancelByCid(queuedMsg.cid)
        forgetQueuedPinIntent({ cid: queuedMsg.cid })
        inlineSteerPinIntentRef.current = null
        restoreComposerAfterFailedSend()
        commitMessages(prev => [
          ...prev,
          {
            role: 'assistant',
            content: `Message didn’t send. I put it back in the composer so you can try again.`,
            blocks: [],
          },
        ])
      }
      return
    }

    // FRESH SEND PATH: no active turn, no queue.
    fetchGenRef.current += 1
    onMessageStartRef.current?.()
    promotedRef.current = false
    // A fresh send starts a NEW run — the rail's only reset seam besides
    // queued_turn_starting. Resetting on ENQUEUE instead (the queue path
    // above) wiped the in-flight build's rail, which the next catch-up
    // replay then silently repopulated (see buildPhaseRail.js).
    setBuildPhases(railAtRunStart())

    // The send rule (see shouldPinSend): pin the new message to the top
    // only when it is the first message OR the user is at the bottom.
    // Read both inputs BEFORE the append: `hasPriorVisibleUser` from the
    // synchronous messagesRef (commitMessages advances it eagerly), and
    // the at-bottom snapshot from the pre-append scrollHeight. `pin` is
    // the caller opt-out (handleStop's queue-collapse passes pin:false).
    const isFirstUserMsg = !messagesRef.current.some(
      m => m.role === 'user' && !m.hidden,
    )
    const willPin = pin && shouldPinSend({
      scrollEl: scrollRef.current,
      mode: modeRef.current,
      isFirstUserMsg,
      wasNearScrollBottom: wasNearContentBottomAtSubmit,
    })
    // The first rendered row uses this optimistic ts, but the backend often
    // returns a canonical server ts a moment later. Carry the send-time intent
    // across that swap so the "new sent message at the top" pin does not point
    // at a removed optimistic DOM node on fast second sends / start races.
    const freshPinIntent = makeSendPinIntent(willPin)

    const userMsg = { role: 'user', content: text, ts: Date.now(), optimistic: true }
    if (attachments.length > 0) userMsg.attachments = attachments
    commitMessages(prev => [...prev, userMsg])
    setInput('')
    clearComposerFilesForSend()
    if (inputRef.current) {
      inputRef.current.style.height = 'auto'
      // Drop the multi-line `.chat__pill--tall` class — see queue-path
      // comment above for the full rationale.
      inputRef.current.closest('.chat__pill')?.classList.remove('chat__pill--tall')
    }
    setSending(true)
    setServerRunningState(true)
    // Pin to top only when the rule allows. Reservation is separate:
    // every visible user send activates the dynamic bottom spacer, but
    // only first-or-at-bottom sends mutate scrollTop to PIN_USER_MSG.
    // When not pinning the user is reading (scrolled up), so convert any
    // stale PIN_USER_MSG from an earlier send into the reader's current
    // ANCHOR_AT and leave their viewport fixed. The optimistic userMsg.ts is
    // the rendered data-ts until the backend's canonical row arrives in the
    // fresh-start response.
    setSpacerActive(true)
    if (willPin) {
      if (spacerRef.current) spacerRef.current.style.height = '0px'
      modeRef.current = { kind: 'PIN_USER_MSG', ts: userMsg.ts }
    } else {
      settleNonPinMode(modeRef, scrollRef.current, { retireFollow: pin })
    }
    // Fresh turn — not a bridge from a mounted DB partial.
    bridgeHook.markBridged()

    // Append <app_state> context block if the embed provided a getContext
    // callback. The displayed message (`userMsg`) stays clean; only the
    // content sent to the backend carries the structured block.
    let sendText = text
    if (getContextRef.current) {
      try {
        const ctx = await getContextRef.current()
        if (ctx && typeof ctx === 'object') {
          // Serialize as a compact inline XML block. Keep it small — this
          // goes inline into the user's message, not a separate system block.
          const parts = Object.entries(ctx)
            .filter(([, v]) => v != null && String(v).trim() !== '')
            .map(([k, v]) => `  <${k}>${String(v).replace(/</g, '&lt;')}</${k}>`)
          if (parts.length > 0) {
            sendText = `${text}\n\n<app_state>\n${parts.join('\n')}\n</app_state>`
          }
        }
      } catch (e) {
        // Context fetch failed — send the original text unchanged.
      }
    }

    try {
      const result = await streamSend(sendText, attachments.length > 0 ? attachments : undefined)
      releaseComposerFilesAfterAccepted()
      if (result?.status === 'queued') {
        const canonicalPending = result.pending_message || null
        commitMessages(prev => {
          const next = [...prev]
          const idx = findOptimisticUserIndex(next, userMsg.ts)
          if (idx >= 0) next.splice(idx, 1)
          return next
        })
        pendingQueue.add({
          ...(canonicalPending || userMsg),
          ts: canonicalPending?.ts ?? result.ts ?? userMsg.ts,
          cid: canonicalPending
            ? `s-${canonicalPending.ts ?? result.ts ?? userMsg.ts}`
            : `q-${userMsg.ts}`,
          queued: true,
          serverTs: !!canonicalPending || typeof result.ts === 'number',
          position: result.position,
        })
        if (!canonicalPending) {
          // Same compatibility path as the queue-only branch: reconcile the
          // visible queued tray with the server's exact pending row before
          // fast-forward can compare against stale local text.
          fetchMessages({ force: true })
        }
        if (result.started) {
          if (Array.isArray(result.message?._consumed_ts)) {
            pendingQueue.promoteManyByTs(result.message._consumed_ts)
          }
          const startedMessages = startedMessagesFromResponse(result)
          const freshPin = resolveFreshPinRetarget({
            startedMessages,
            fallbackTs: userMsg.ts,
            willPin,
            pinIntent: freshPinIntent,
            pinIntentStillCurrent,
          })
          if (freshPin.shouldPin) {
            setSpacerActive(true)
            if (spacerRef.current) spacerRef.current.style.height = '0px'
            modeRef.current = { kind: 'PIN_USER_MSG', ts: freshPin.pinTargetTs }
          } else if (freshPin.intentStillCurrent) {
            settleNonPinMode(modeRef, scrollRef.current, { retireFollow: pin })
          }
          if (startedMessages) {
            commitMessages(prev => appendMessageBatch(prev, startedMessages))
          }
          return
        }
        if (!result.started) {
          const queuedPinStillValid = pinIntentStillCurrent(freshPinIntent)
          if (queuedPinStillValid) {
            settleNonPinMode(modeRef, scrollRef.current, { retireFollow: pin })
          }
          setSending(false)
          setServerRunningState(false)
        }
        return
      }
      const startedMessages = startedMessagesFromResponse(result)
      if (startedMessages) {
        const freshPin = resolveFreshPinRetarget({
          startedMessages,
          fallbackTs: userMsg.ts,
          willPin,
          pinIntent: freshPinIntent,
          pinIntentStillCurrent,
        })
        if (freshPin.shouldPin) {
          setSpacerActive(true)
          if (spacerRef.current) spacerRef.current.style.height = '0px'
          modeRef.current = { kind: 'PIN_USER_MSG', ts: freshPin.pinTargetTs }
        } else if (freshPin.intentStillCurrent) {
          settleNonPinMode(modeRef, scrollRef.current, { retireFollow: pin })
        }
        commitMessages(prev => {
          return replaceOptimisticWithBatch(prev, userMsg.ts, startedMessages)
        })
      }
      if (!hadMessagesRef.current) {
        hadMessagesRef.current = true
        onFirstMessageRef.current?.()
      }
    } catch (err) {
      setSending(false)
      sendingRef.current = false
      setServerRunningState(false)
      restoreComposerAfterFailedSend()
      // The POST never reached/finished on the server, so remove the optimistic
      // user bubble and keep the text in the composer. Otherwise a transient
      // "Failed to fetch" looks like the message was accepted locally but
      // silently disappears from the durable chat after refresh.
      commitMessages(prev => {
        const next = [...prev]
        const idx = findOptimisticUserIndex(next, userMsg.ts)
        if (idx >= 0) next.splice(idx, 1)
        next.push({
          role: 'assistant',
          content: `Message didn’t send. I put it back in the composer so you can try again.`,
          blocks: [],
        })
        return next
      })
      onStreamEndRef.current?.({ continues: false })
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
  }, [
    streamSend,
    pendingFiles,
    commitMessages,
    fetchMessages,
    clearFiles,
    restoreFiles,
    releaseFiles,
  ])

  useEffect(() => {
    if (!autoSendPendingDraft || autoSendAttemptedRef.current) return
    if (loading || loadError) return
    const text = input.trim()
    if (!text) {
      setAutoSendPendingDraft(false)
      return
    }
    autoSendAttemptedRef.current = true
    setAutoSendPendingDraft(false)
    try { sessionStorage.removeItem(`${DRAFT_AUTOSEND_PREFIX}${chatId}`) } catch {}
    doSend(text)
  }, [autoSendPendingDraft, loading, loadError, input, chatId, doSend])

  // Sends the answer without a visible user message bubble.
  // Sends the answer to an AskUserQuestion as a hidden user message.
  // Answers ride along in the SAME POST as the hidden message. The backend
  // either resolves the live parked future or, after a process restart,
  // records the answer and starts a recovered hidden continuation. The
  // previous flow had a separate
  // POST /question-answers that could race with the GET on a mid-
  // stream remount, causing answers to disappear on first return
  // and reappear on the second.
  const doSendSilent = useCallback(async (text, resolvedAnswers, questionId) => {
    // Synchronous re-entrancy guard: flip BEFORE any other logic so a
    // second concurrent call (fast double-tap) bails immediately. This
    // is separate from sendingRef because answer submissions are
    // deliberately allowed while sendingRef is true (the runner is
    // parked waiting for the answer), but we still need to prevent the
    // same answer from being submitted twice concurrently.
    if (sendSilentInFlightRef.current) return
    sendSilentInFlightRef.current = true
    if (!text.trim()) {
      sendSilentInFlightRef.current = false
      return
    }
    // Answer submissions (resolvedAnswers truthy) are allowed mid-turn:
    // the runner is paused on the AskUserQuestion future and is waiting
    // for exactly this POST. BOTH gates must relax — `sending` is set
    // by the originating user prompt and stays true through the whole
    // turn, `isStreaming` is true while the SSE stream is open. Without
    // both relaxations, Submit on a question card silently no-ops even
    // though the agent is parked indefinitely for the answer. QuestionCard's
    // own `submitted` state guards against double-clicks on the same card.
    if ((sendingRef.current || isStreamingRef.current) && !resolvedAnswers) {
      sendSilentInFlightRef.current = false
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
          msg.blocks = (msg.blocks || []).map(b => {
            if (b.type !== 'question') return b
            if (questionId && b.question_id !== questionId) return b
            return { ...b, answers: resolvedAnswers }
          })
          updated[lastIdx] = msg
        }
        return updated
      })
      // When the question is still live in streamItems (not yet promoted
      // to messages — the turn is mid-flight and messages[-1] is the user
      // message, not the assistant), the commitMessages patch above has no
      // target. Also update streamItems so the card visually transitions to
      // answered regardless of which source is currently rendering it.
      patchQuestionAnswers(questionId, resolvedAnswers)
    }

    setSending(true)
    setServerRunningState(true)
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
        question_id: questionId,
      })
      if (questionId) setLiveQuestionId(prev => prev === questionId ? null : prev)
    } catch (err) {
      setSending(false)
      setServerRunningState(false)
      if (err.message === 'HTTP 410') {
        // The backend refused this answer because the durable transcript no
        // longer has that open question (for example Stop cancelled it, or a
        // newer question superseded it). Refetch authoritative state rather
        // than keeping the optimistic answer locally.
        setLiveQuestionId(null)
        fetchMessages({ force: true })
        sendSilentInFlightRef.current = false
        return
      }
      commitMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
      ])
    } finally {
      sendSilentInFlightRef.current = false
    }
  }, [streamSend, commitMessages, fetchMessages])

  function handleSubmit(e) {
    e.preventDefault()
    doSend(input.trim())
  }

  // Cancel a queued message via DELETE. Optimistic remove; reconcile
  // by re-fetching authoritative state on success or on error.
  const handleCancelPending = useCallback(async (ts) => {
    pendingQueue.cancelByTs(ts)
    forgetQueuedPinIntent({ ts })
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
      // Snapshot the queue before doing anything destructive. Stop ALWAYS
      // interrupts the current turn and resends any queued messages as ONE
      // fresh follow-up turn — it never folds them into the still-running
      // turn. interrupt + new-turn is the only entry point that is
      // deterministic on both providers: the SDKs yield a structural
      // [Q1, A1-partial, Q2, A2] only via interrupt + re-query/new-turn
      // (Claude has no mid-turn inject at all; Codex turn.steer() leaves the
      // steered message's placement inside the live turn up to the
      // app-server). Force-steering queued text into the live turn on Stop
      // made the entry point fork on a timing race — steer-if-still-steerable
      // vs interrupt-if-just-closed — which is the "where did my queued
      // message go" bug. The opt-in mid-stream steer on a NORMAL send is
      // unchanged; this is only the Stop path. A second Stop with an empty
      // queue just halts; users still remove individual queued messages via
      // the X button while they're queued.
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
      forgetAllQueuedPinIntents()
      pendingQueue.clear()

      let stoppedCleanly = false
      // The backend reports which queued ts it actually removed. null = an
      // older backend without the field (→ fall back to resending all); an
      // array is the authoritative cleared set.
      let clearedPendingTs = null
      const requestStopOnce = async () => {
        const stopRes = await fetch(`${BASE}/api/chat/stop`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${getToken()}`,
          },
          body: JSON.stringify({ chat_id: chatId }),
        })
        let data = null
        if (stopRes.ok) {
          // stop_chat returns {stopped: false} when the SDK interrupt
          // timed out — the runner is still alive. We must NOT tear
          // down local state or re-send the collapsed queue, because
          // that would mean two concurrent runs of the same chat.
          // Leave the stream attached so the user can retry. Likewise,
          // a non-OK / missing response is NOT success: keeping Stop visible
          // is safer than pretending the turn halted while the backend runs on.
          try {
            data = await stopRes.json()
          } catch { /* non-JSON body — legacy success if HTTP itself was OK */ }
          // Resend only what Stop truly cleared: a queued message the
          // turn-end drain already promoted into a continuation (right as
          // Stop landed) is gone from the queue, so it's absent here and
          // must NOT be re-sent — that was the natural-finish-races-Stop
          // double-send (PM 115).
          if (clearedPendingTs === null && Array.isArray(data?.cleared_pending_ts)) {
            clearedPendingTs = data.cleared_pending_ts
          }
        }
        return stopRequestSucceeded({ responseOk: stopRes.ok, data })
      }
      const confirmStopIdle = async () => {
        try {
          const res = await apiFetch(`/chats/${chatId}?limit=1`, { timeoutMs: 5000 })
          if (!res.ok) return { failed: true, running: null }
          const data = await res.json()
          return { failed: false, running: data?.running }
        } catch {
          return { failed: true, running: null }
        }
      }
      for (const retryDelayMs of STOP_RETRY_DELAYS_MS) {
        if (retryDelayMs > 0) await delay(retryDelayMs)
        let requestSucceeded = false
        try {
          requestSucceeded = await requestStopOnce()
        } catch {
          requestSucceeded = stopRequestSucceeded({ fetchFailed: true })
        }
        if (!requestSucceeded) {
          stoppedCleanly = false
          break
        }
        const confirmation = await confirmStopIdle()
        stoppedCleanly = stopConfirmedIdle({
          stopSucceeded: requestSucceeded,
          confirmRunning: confirmation.running,
          confirmFailed: confirmation.failed,
        })
        if (stoppedCleanly) break
        if (!shouldRetryStopAfterConfirm({
          requestSucceeded,
          confirmRunning: confirmation.running,
          confirmFailed: confirmation.failed,
        })) {
          break
        }
      }

      // Resolve WHAT to resend from the queued snapshot + the set the
      // backend reports it actually cleared, via the SHARED, pure
      // resolveStopResend — ONE code path for both the clean-stop and
      // the timeout branch so they can't drift. The timeout branch used
      // to ignore clearedPendingTs and re-send the full snapshot
      // unconditionally, which double-sent a message the natural turn-end
      // drain had already consumed (cleared set []). The full contract +
      // its tests live in resolveStopResend.js.
      const resolveResend = (cleared) => resolveStopResend(
        queuedSnapshot, cleared,
        { text: combined, attachments: combinedAttachments },
      )

      if (!stoppedCleanly) {
        // The SDK interrupt timed out (handle.stop()'s 2s bound — see
        // claude_sdk_runner.stop): the runner is still alive and the
        // backend left the registry entry + broadcast intact for the
        // runner's own teardown. We must NOT disconnect or start a
        // second concurrent run. The backend ALREADY cleared persisted
        // chat.pending_messages, so a refetch returns [] and re-queueing
        // from authoritative server state would silently drop the queued
        // text (the "Stop ate my message" bug). Restore from the LOCAL
        // snapshot instead, via the same doSend re-send path — and
        // narrowed by clearedPendingTs through the SHARED resolveResend
        // (above) so a queued message the natural turn-end drain already
        // consumed (cleared set []) is NOT re-sent here: re-sending it
        // would duplicate the message and risk a duplicate follow-up run.
        // Because the stream is still attached (isStreamingRef true),
        // doSend takes its QUEUE PATH: it re-POSTs the combined turn into
        // the backend pending queue (re-persisting what Stop cleared) AND
        // re-shows it in the tray. No fresh run starts, no duplicate.
        //
        // Recovery contract (corrected): the re-persisted queue is NOT
        // auto-drained "on the next turn boundary". Stop already bumped
        // the run generation, so when the timed-out runner finally
        // finalizes, its terminal drain recomputes we_own_gen == false
        // and returns STALE_NO_ACTION — it promotes nothing and schedules
        // no continuation. The message sits in chat.pending_messages with
        // the run marker cleared and no live runner; it self-heals on the
        // NEXT user interaction (the not-is_chat_running stale-pending
        // drain) or a reconcile, not via the dying runner. The re-POST is
        // re-shown in the tray so the user sees it is still queued.
        //
        // The 2s timeout is NOT surfaced as a user-facing error; only a
        // genuine re-queue POST failure (doSend's catch) shows a block.
        const { text: resendText, attachments: resendAttachments } =
          resolveResend(clearedPendingTs)
        if (resendText) {
          doSend(resendText, {
            pin: false,
            attachments: resendAttachments.length > 0 ? resendAttachments : undefined,
          })
        }
        return
      }
      disconnect({ clearStreaming: true })
      promoteStreamToMessages()
      setSending(false)
      setServerRunningState(false)
      // Sync sendingRef to the just-committed state so the synchronous
      // doSend(resendText) call below reads the post-stop value.
      // setSending(false) queues a render — the next render will write
      // sendingRef via the top-of-component mirror, but until then the
      // ref still holds the pre-stop `true`. We need the value RIGHT
      // NOW for doSend's guard. (The peer isStreamingRef is the hook's
      // own ref; disconnect({clearStreaming: true}) above flipped it
      // synchronously already.)
      sendingRef.current = false
      // pending + fetchGen were cleared/bumped BEFORE the await above.
      onStreamEnd?.({ continues: false })

      // Resend the queued work as ONE fresh turn — but ONLY the messages
      // the backend confirms it cleared. Same SHARED resolveResend the
      // timeout branch uses, so the two paths can't drift: empty cleared
      // set → nothing re-sent (the natural-finish-races-Stop double-send,
      // PM 115), exact match → that subset, partial/legacy → full combined.
      const { text: resendText, attachments: resendAttachments } =
        resolveResend(clearedPendingTs)

      if (resendText) {
        // doSend's guard reads sendingRef/isStreamingRef (just synced to false
        // above) → fresh-send path. pin:false so the synthetic combined-from-
        // queue message doesn't yank the viewport to top, pushing the partial
        // the user just stopped (and the original turn-1 user msg) above the
        // viewport. Mode stays whatever the user had — they were reading the
        // partial, the new turn streams in continuing from there.
        doSend(resendText, {
          pin: false,
          attachments: resendAttachments.length > 0 ? resendAttachments : undefined,
        })
      }
    } finally {
      handlingStopRef.current = false
    }
  }

  // Re-entry guard for handleSteer, peer of handlingStopRef. Two rapid
  // taps on the fast-forward button would otherwise both snapshot the
  // same queue and both POST a force_steer for the same ts → the second
  // POST's consume_pending_ts no longer matches pending (the first
  // already consumed them) and comes back not_steered, but the optimistic
  // double-fire is still wasteful. Synchronous flip at entry, cleared in
  // finally.
  const handlingSteerRef = useRef(false)

  // STEER (fast-forward): inject the queued messages into the LIVE turn
  // at the next natural boundary, instead of hard-stopping (handleStop)
  // or waiting for turn-end (the default queue drain). Mirrors handleStop's
  // structure — re-entry guard, snapshot-before-await — but never
  // interrupts the running turn. The backend force-steers (bypassing the
  // steer_enabled opt-in) for BOTH providers; on success the steered
  // message lands in the transcript and renders inline via the
  // `steered_into_turn` SSE event (onSteeredIntoTurn above), so we just
  // drop those rows from the local tray.
  async function handleSteer() {
    if (handlingSteerRef.current) return
    handlingSteerRef.current = true
    try {
      const snapshot = pendingQueue.pendingMessagesRef.current
      // Only entries with a real numeric SERVER ts can be force-steered:
      // _force_steer_matches_pending compares consume_pending_ts against
      // chat.pending_messages[].ts, so an optimistic-only entry whose
      // queue-POST hasn't been server-acked (its ts is a client Date.now(),
      // not the server's) would make the match fail. We take the
      // simpler-correct option: only steer when EVERY queued entry is
      // serverTs-confirmed (usePendingQueue sets that flag on the
      // server-origin / swapOptimisticTs / hydrate paths). The button gate
      // (canSteer below) keeps the fast-forward hidden until then, so this
      // is belt-and-suspenders — if a stray optimistic entry slips in, bail
      // and leave the queue intact (it drains at turn-end as usual).
      // Before bailing, run one forced runtime reconcile: a mounted mobile
      // client can have visible queued rows whose serverTs flag is stale
      // until focus/new input wakes a fetch. If hydrate confirms them, retry
      // from the now-canonical snapshot in the same tap.
      if (snapshot.length > 0 && !snapshot.every(
        m => typeof m.ts === 'number' && m.serverTs === true,
      )) {
        await reconcileRuntimeState()
      }
      const confirmedSnapshot = pendingQueue.pendingMessagesRef.current
      const allServerConfirmed = confirmedSnapshot.length > 0 && confirmedSnapshot.every(
        m => typeof m.ts === 'number' && m.serverTs === true,
      )
      if (!allServerConfirmed) return

      // Build content EXACTLY as the backend expects: the non-empty
      // trimmed contents joined by "\n\n", in pending order. This must
      // byte-match _force_steer_matches_pending's `expected` or the
      // request is rejected (not_steered). consume_pending_ts is every
      // snapshot entry's ts (the backend selects pending rows by this set
      // and rebuilds the same join over them).
      const steerTexts = confirmedSnapshot
        .map(m => (m.content || '').trim())
        .filter(Boolean)
      const content = steerTexts.join('\n\n')
      const consumePendingTs = confirmedSnapshot.map(m => m.ts)
      // De-dupe attachments by name, exactly like handleStop/resolveStopResend.
      const seenNames = new Set()
      const attachments = []
      for (const m of confirmedSnapshot) {
        for (const a of (m.attachments || [])) {
          if (a && a.name && !seenNames.has(a.name)) {
            seenNames.add(a.name)
            attachments.push(a)
          }
        }
      }
      if (!content) return

      try {
        const wasNearContentBottomAtSteer = scrollRef.current
          ? isNearContentBottom(scrollRef.current)
          : null
        const steerIsFirstUser = !messagesRef.current.some(
          m => m.role === 'user' && !m.hidden,
        )
        const steerWillPin = shouldPinSend({
          scrollEl: scrollRef.current,
          mode: modeRef.current,
          isFirstUserMsg: steerIsFirstUser,
          respectFollowMode: false,
          wasNearScrollBottom: wasNearContentBottomAtSteer,
        })
        steerPinIntentRef.current = makeSendPinIntent(steerWillPin)
        // The queued tray is part of the footer height. If it stays visible
        // until after the steered row is inserted, the scroll system pins with
        // one layout and then immediately reflows when the tray disappears — the
        // visible "down, then up" fast-forward jump. Hide only the confirmed
        // rows this request is steering; restore the snapshot below if the
        // backend says the turn was not steered.
        pendingQueue.promoteManyByTs(consumePendingTs)
        const queueAfterOptimisticPromote = pendingQueue.pendingMessagesRef.current
        const restoreOptimisticSteerQueue = () => {
          // If another path touched the queue while the POST was in flight
          // (notably the natural turn-end drain), every pendingQueue mutation
          // assigns a fresh array. In that case the other path won the race,
          // so restoring our stale snapshot would resurrect duplicate chips.
          if (pendingQueue.pendingMessagesRef.current === queueAfterOptimisticPromote) {
            pendingQueue.hydrate(confirmedSnapshot, { preserveMissing: true })
          }
        }
        const result = await streamSend(content, attachments, {
          forceSteer: true,
          consumePendingTs,
          steeredMessages: confirmedSnapshot.map(m => ({
            ts: m.ts,
            content: m.content || '',
            ...(m.attachments ? { attachments: m.attachments } : {}),
          })),
        })
        if (result?.status === 'steered') {
          // The steered rows now render inline (onSteeredIntoTurn promotes
          // them from the SSE event + transcript). Drop them from the local
          // tray. Reconcile against the server's authoritative remaining
          // queue when present, else remove exactly the steered ts.
          if (Array.isArray(result.pending_messages)) {
            pendingQueue.hydrate(result.pending_messages)
          } else {
            for (const ts of consumePendingTs) pendingQueue.cancelByTs(ts)
          }
          forgetQueuedPinIntent({ tsList: consumePendingTs })
        }
        if (result?.status !== 'steered') {
          steerPinIntentRef.current = null
          restoreOptimisticSteerQueue()
        }
        // not_steered (the turn closed between the gate and the POST) or any
        // other status: restore the queue and let it drain at turn-end. The
        // tray may disappear briefly during the optimistic steer attempt, but
        // it never gets lost.
      } catch {
        steerPinIntentRef.current = null
        restoreOptimisticSteerQueue()
        // Network/POST error — restore the queue for the turn-end drain.
      }
    } finally {
      handlingSteerRef.current = false
    }
  }

  // Re-anchor the scroll mode when the tab returns to the foreground
  // (visibilitychange/pageshow/online) while a turn is active, so a
  // backgrounded-then-resumed streaming chat doesn't snap away from where the
  // user was reading. If the user is already at the tail, preserve
  // FOLLOW_BOTTOM so thinking/timer updates keep flowing at the bottom instead
  // of converting the tail into a fixed anchor. No-op when the turn isn't
  // active or the tab is hidden.
  // (The fast-forward affordance is computed separately at `canSteer` below.)
  const turnActive = sending || isStreaming || serverRunning
  useEffect(() => {
    function freezeStreamingReturn() {
      if (typeof document !== 'undefined'
          && document.visibilityState
          && document.visibilityState !== 'visible') {
        return
      }
      if (!turnActive) return
      const nextMode = modeForForegroundReturn(scrollRef.current)
      if (nextMode) modeRef.current = nextMode
    }

    document.addEventListener('visibilitychange', freezeStreamingReturn)
    window.addEventListener('pageshow', freezeStreamingReturn)
    window.addEventListener('online', freezeStreamingReturn)
    return () => {
      document.removeEventListener('visibilitychange', freezeStreamingReturn)
      window.removeEventListener('pageshow', freezeStreamingReturn)
      window.removeEventListener('online', freezeStreamingReturn)
    }
  }, [turnActive, modeRef])

  // Composer action state: queued work is also non-idle from the user's point
  // of view. Even if we momentarily don't have a live stream attached yet, a
  // visible queue must keep the primary action on Stop/Send-now, never Mic.
  // Fast-forward is stricter: it appears only when the click can actually
  // steer a live turn with server-confirmed pending rows. Optimistic rows stay
  // visible in the tray but do not expose an inert fast-forward button.
  const composerBusy = turnActive || pendingQueue.pendingMessages.length > 0
  const canSteer = canFastForwardQueue(pendingQueue.pendingMessages, turnActive)
  const canRequestSteer = turnActive && pendingQueue.pendingMessages.length > 0

  useEffect(() => {
    try {
      if (swReloadHoldTimerRef.current) {
        clearTimeout(swReloadHoldTimerRef.current)
        swReloadHoldTimerRef.current = null
      }
      sessionStorage.setItem('sw-auto-reloaded', '1')
      if (!turnActive) {
        swReloadHoldTimerRef.current = setTimeout(() => {
          try { sessionStorage.removeItem('sw-auto-reloaded') } catch {}
          swReloadHoldTimerRef.current = null
        }, 5000)
      }
    } catch {}
  }, [turnActive])

  useEffect(() => {
    const hasQueue = pendingQueue.pendingMessages.length > 0
    if (!turnActive && !hasQueue) return
    let cancelled = false
    let inFlight = false
    const run = () => {
      if (cancelled || inFlight) return
      // Single-flight: without this guard a slow/hung reconcile lets the next
      // interval tick fire another overlapping fetch, and they stack unbounded
      // against a wedged backend. Skip a tick while the prior one is in flight;
      // the fetch is time-boxed (apiFetch timeoutMs) so inFlight always clears.
      inFlight = true
      reconcileRuntimeState().finally(() => {
        inFlight = false
        if (!cancelled) ensureRuntimeStreamConnected()
      })
    }
    run()
    const intervalMs = hasQueue ? 1000 : 3000
    const timer = setInterval(run, intervalMs)
    const onVisible = () => {
      if (document.visibilityState === 'visible') run()
    }
    window.addEventListener('focus', run)
    window.addEventListener('pageshow', run)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      clearInterval(timer)
      window.removeEventListener('focus', run)
      window.removeEventListener('pageshow', run)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [
    ensureRuntimeStreamConnected,
    turnActive,
    pendingQueue.pendingMessages.length,
    reconcileRuntimeState,
  ])

  useEffect(() => {
    let cancelled = false
    const run = () => {
      if (cancelled) return
      reconcileRuntimeState().finally(() => {
        if (!cancelled) ensureRuntimeStreamConnected()
      })
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') run()
    }
    window.addEventListener('focus', run)
    window.addEventListener('pageshow', run)
    window.addEventListener('online', run)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      window.removeEventListener('focus', run)
      window.removeEventListener('pageshow', run)
      window.removeEventListener('online', run)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [ensureRuntimeStreamConnected, reconcileRuntimeState])

  const hasMore = offset > 0
  // Empty-state is the "I have nothing to show because nothing happened
  // yet" view. If the initial chat fetch errored, we have no idea
  // whether the chat is empty — surfacing that branch separately keeps
  // us from lying with "What's on your mind?" over a network failure.
  const showEmpty = !loadError && messages.length === 0 && !turnActive && !loading

  // Collect the question keys currently live in streamItems so MsgContent
  // can suppress any persisted question block that is already rendered by
  // the streaming <li>. Without this dedup, when doSendSilent retires the
  // bridge gate and the SSE catch-up burst fires a `question` event into
  // streamItems, BOTH the persisted message row AND the streaming <li>
  // render the card — the duplicate is impossible by construction when
  // MsgContent skips blocks whose key is already in streamItems.
  const streamItemQuestionKeys = (turnActive && streamItems.length > 0)
    ? new Set(
        streamItems
          .filter(it => it.type === 'question')
          .map(it => questionKey(it))
      )
    : null
  const showLoadError = loadError && messages.length === 0 && !loading && !turnActive
  const lastUserIdx = messages.reduce((acc, m, i) => (m.role === 'user' && !m.hidden) ? i : acc, -1)
  const bridgeMsgIdx = (turnActive && streamItems.length > 0)
    ? bridgeHook.findBridgeIndex(messages)
    : -1
  const trailingAssistantPartialIdx = (turnActive && streamItems.length > 0)
    ? findTrailingAssistantPartialIndex(messages)
    : -1
  const activePartialMsgIdx = bridgeMsgIdx >= 0 ? bridgeMsgIdx : trailingAssistantPartialIdx
  const activePartialMsg = activePartialMsgIdx >= 0 ? messages[activePartialMsgIdx] : null
  // Single-surface invariant for active assistant partials:
  // - if the live stream is at least as fresh as the saved DB partial, hide the
  //   DB row and render StreamingMessage; final promotion replaces that row.
  // - if the DB partial is richer than a stale cached stream snapshot (e.g.
  //   stray "I" after returning to chat), keep the DB row and suppress the
  //   stale stream until catch-up catches up.
  // - if both surfaces clearly belong to the same active answer but tool /
  //   thinking metadata is only partially replayed, still choose ONE surface.
  //   Rendering both was the transient "duplicated agent output" bug: reopening
  //   the chat fixed it because the durable transcript had only one copy.
  const activeAssistantSurface = chooseActiveAssistantSurface(activePartialMsg, streamItems)
  const liveMirrorMsgIdx = activeAssistantSurface.hideMessage ? activePartialMsgIdx : -1
  const suppressStreamingSurface = !!(activePartialMsg && activeAssistantSurface.suppressStream)
  const showStreamingSurface = turnActive && streamItems.length > 0 && !suppressStreamingSurface

  // ── Sticky "needs your answer" affordance ──────────────────────────
  // A pending AskUserQuestion freezes the turn until the user answers,
  // but the card can sit outside the viewport (the user scrolled away,
  // or content streamed in around it) — the chat then just looks hung.
  // Detect a pending card in whichever surface currently renders it:
  // the live stream (a question item without answers) or the durable
  // tail-question invariant on the last visible assistant message (the
  // same rule MsgContent's blockAnswerable enforces; recovery preserves
  // that tail question even when the original process was interrupted).
  const pendingQuestionInStream = showStreamingSurface
    && streamItems.some(it => it.type === 'question' && !it.answers)
  const pendingQuestionInMessages = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].hidden) continue
      const msg = messages[i]
      if (msg.role !== 'assistant' || !msg.blocks?.length) return false
      const tail = msg.blocks[msg.blocks.length - 1]
      return !!(tail.type === 'question' && !tail.answers
        && (!liveQuestionId || tail.question_id === liveQuestionId))
    }
    return false
  })()
  const hasPendingQuestion = pendingQuestionInStream || pendingQuestionInMessages

  // ── Sticky "tap to resume" affordance ──────────────────────────────
  // A turn paused by a drain-gated restart, a stall, or a provider-limit park
  // persists a resumable error block at the tail of the last assistant message
  // (the same tail invariant MsgContent's Resume gate enforces). Like a pending
  // question, that card can sit outside the viewport after a scroll — the chat
  // then just looks stopped. Detect the tail resumable block so the offscreen
  // nudge + SR status can name the recovery. A pause is terminal (the turn has
  // ended), so it only ever lives in `messages`, never in a live stream item.
  const pendingResumeBlock = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].hidden) continue
      const msg = messages[i]
      if (msg.role !== 'assistant' || !msg.blocks?.length) return null
      const tail = msg.blocks[msg.blocks.length - 1]
      return tail.type === 'error' && tail.resumable ? tail : null
    }
    return null
  })()
  const hasPendingResume = !!pendingResumeBlock

  // Visibility of that card is a pure viewport question — an
  // IntersectionObserver rooted at the scroll container is the signal,
  // no scroll math and no interaction with the spacer machinery. The
  // card's DOM node is stable across streaming ticks (keyed children),
  // so the observer only needs re-binding when the rendering surface
  // can change: pending-flag flips, stream↔messages promotion, or a
  // messages commit.
  // The LAST un-answered card is the pending one: it lives in the last
  // assistant message or the streaming <li>.
  const findPendingQuestionCard = () =>
    [...(scrollRef.current?.querySelectorAll('.qcard:not(.qcard--answered)') ?? [])].pop()

  useEffect(() => {
    if (!hasPendingQuestion) {
      setPendingCardOffscreen(false)
      return undefined
    }
    const scrollEl = scrollRef.current
    const card = findPendingQuestionCard()
    if (!scrollEl || !card || typeof IntersectionObserver === 'undefined') {
      setPendingCardOffscreen(false)
      return undefined
    }
    const io = new IntersectionObserver(entries => {
      setPendingCardOffscreen(!entries[0]?.isIntersecting)
    }, { root: scrollEl, threshold: 0 })
    io.observe(card)
    return () => io.disconnect()
  }, [hasPendingQuestion, showStreamingSurface, messages])

  // Same offscreen-detection machinery for the resume card. Only the tail
  // resumable note renders `.chat__resume` (MsgContent gates the button on
  // isLastMsg), so observing that button is enough to know the card's
  // visibility; a tap on the nudge scrolls it into view.
  const findResumeCard = () =>
    [...(scrollRef.current?.querySelectorAll('.chat__resume') ?? [])].pop()

  useEffect(() => {
    if (!hasPendingResume) {
      setResumeCardOffscreen(false)
      return undefined
    }
    const scrollEl = scrollRef.current
    const card = findResumeCard()
    if (!scrollEl || !card || typeof IntersectionObserver === 'undefined') {
      setResumeCardOffscreen(false)
      return undefined
    }
    const io = new IntersectionObserver(entries => {
      setResumeCardOffscreen(!entries[0]?.isIntersecting)
    }, { root: scrollEl, threshold: 0 })
    io.observe(card)
    return () => io.disconnect()
  }, [hasPendingResume, showStreamingSurface, messages])

  // The streaming <li> carries a stable data-key so the scroll state machine
  // can anchor inside an in-flight answer. Without this, returning to a
  // streaming chat while scrolled into the live bubble had no anchorable row,
  // so reconnect/catch-up could fall back to bottom-follow behavior. In the
  // BRIDGE case (we kept a DB partial on mount and the streaming <li> is the
  // visual replacement for that suppressed message), use the partial's key so
  // an existing ANCHOR_AT still resolves through the catch-up window.
  //
  // Fast-forward can insert a user row AFTER the mounted partial while the
  // stream remains live. Therefore bridge identity is ts-based across the
  // full message list, not "last message only." For multi-turn flow (no
  // bridge), the previous assistant is rendered alongside the streaming
  // <li> (different turns), so the streaming <li> gets its own synthetic key
  // rather than reusing a previous assistant row's key.
  const streamingDataKey = (() => {
    const bridged = liveMirrorMsgIdx >= 0 ? messages[liveMirrorMsgIdx] : null
    if (!bridged || bridged.role !== 'assistant' || bridged.hidden) {
      return `streaming-${chatId}`
    }
    return bridged.id || `${bridged.role}-${bridged.ts ?? bridgeMsgIdx}`
  })()

  // Polite aria-live status: announced once per state transition, not per
  // token. Visually hidden via the sr-only utility in ChatView.css.
  // When the tail turn paused/parked and needs a Resume tap, announce the
  // recovery state — "Response ready." would be a lie (a paused turn isn't
  // ready, it's waiting on the owner), and a screen-reader user has no visual
  // Resume card to fall back on.
  const resumeStatus = (() => {
    if (!pendingResumeBlock) return null
    if (pendingResumeBlock.parked_until) {
      const label = formatResetTime(pendingResumeBlock.parked_until)
      return label
        ? `Rate limit reached, resets ${label} — Resume available.`
        : 'Rate limit reached — Resume available.'
    }
    return 'Turn paused — Resume available.'
  })()
  const ariaStatus = turnActive
    ? 'Assistant is responding…'
    : (resumeStatus
        ?? (messages.length > 0
            && messages[messages.length - 1]?.role === 'assistant'
              ? 'Response ready.'
              : ''))
  // One CTA row per built app (most recent last). The view-model stays pure
  // and per-app; the pulse/label-swap is layered on in the render below.
  const openAppCtas = builtApps
    .map(app => ({ app, vm: openAppCtaViewModel(app, turnActive) }))
    .filter(entry => entry.vm)
  const buildPhaseRail = buildPhaseRailViewModel(buildPhases)

  return (
    <div
      ref={chatRef}
      className={`chat${showEmpty || showLoadError ? ' chat--empty' : ''}`}
    >
      {/* Single polite live region — announces state transitions only.
          aria-atomic keeps the full phrase together for NVDA/VoiceOver. */}
      <div
        className="chat__sr-status"
        aria-live="polite"
        aria-atomic="true"
        aria-relevant="text"
      >
        {ariaStatus}
      </div>
      <div
        className="chat__sr-status"
        aria-live="polite"
        aria-atomic="true"
        aria-relevant="text"
      >
        {previewReadyStatus}
      </div>
      <div
        className="chat__sr-status"
        aria-live="polite"
        aria-atomic="true"
        aria-relevant="text"
      >
        {buildPhaseStatus}
      </div>
      {showInspector && (
        <AgentContextInspector
          chatId={chatId}
          onClose={() => setShowInspector(false)}
        />
      )}
      {showEmpty && (
        <div className="chat__empty-wrap">
          {embedded ? (
            // App-embedded chats: render quick action chips when the app
            // provided them via opts.quickActions; otherwise a neutral hint.
            // Chips pre-fill the composer (never auto-send) — max 4 rendered.
            Array.isArray(quickActions) && quickActions.length > 0 ? (
              <div className="chat__empty chat__empty--embed chat__empty--chips">
                <div className="chat__quick-actions" role="list">
                  {quickActions.slice(0, 4).map((action, i) => (
                    <button
                      key={i}
                      type="button"
                      className="chat__quick-action-chip"
                      role="listitem"
                      onClick={() => restoreComposerText(action.prompt)}
                    >
                      {action.label}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              // Embedded chat with no quick-action chips: render nothing. The
              // embed peeks past the collapsed pill, and any greeting text
              // leaks into the app surface. The empty composer is enough.
              <div className="chat__empty chat__empty--embed" />
            )
          ) : (
            <div className="chat__empty">
              <img className="chat__empty-glyph" src={`${BASE}/moebius.png`} alt="" width="120" height="120" />
              <p className="chat__empty-title">What's on your mind?</p>
              <div className="chat__empty-prompts">
                {EMPTY_PROMPTS.map(prompt => (
                  <button
                    key={prompt.label}
                    type="button"
                    className="chat__empty-prompt"
                    onClick={() => restoreComposerText(prompt.prompt, { focus: true })}
                  >
                    {prompt.label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
      {showLoadError && (
        <div className="chat__empty-wrap">
          <div className="chat__empty">
            <p className="chat__empty-title">Couldn't load this chat.</p>
            <p className="chat__empty-sub">Check your connection and try again.</p>
            <button
              type="button"
              className="chat__empty-action"
              onClick={() => {
                setLoadError(false)
                setLoading(true)
                // Re-run the load effect in place (bump its nonce dep) —
                // no hard reload, so cache/scroll/drafts/back-stack survive.
                setLoadNonce(n => n + 1)
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
            if (i === liveMirrorMsgIdx
                && msg.role === 'assistant'
                && showStreamingSurface) {
              return null
            }
            // A question is answerable while the runner is parked on it,
            // waiting for the answer. The runner BLOCKS the turn on the
            // AskUserQuestion future until it is answered, so an unanswered
            // question that is still the TAIL of the last assistant message
            // means the runner is parked right there — nothing follows it
            // until the answer arrives.
            //
            // That invariant is fully DURABLE: it reads only the persisted
            // message blocks, so it survives a reload AND Möbius's
            // kill-on-question `done` (the SSE closes the moment a question
            // fires, but the runner keeps waiting). Gating on the live
            // stream instead — an earlier version required `isStreaming`,
            // which flips false on that `done` — is exactly what wedged the
            // card disabled-forever. `liveQuestionId`, when the live stream
            // handed it to us, is an extra precision filter; after a reload
            // we may never have seen it, and then the tail-unanswered
            // invariant stands on its own.
            //
            // MsgContent enforces the "tail block" half (the question is the
            // LAST block). Recovery may insert an interruption note before a
            // still-open question, but once the turn truly moves on and any
            // block follows the question, that older card becomes transcript
            // history. Double-submit is prevented by QuestionCard's own
            // `submitted` state + doSendSilent's synchronous sendingRef flip.
            //
            // isLastMsg + liveQuestionId are passed as stable scalars so
            // MsgContent's memo can skip non-last messages on every streaming
            // tick. The inline-arrow form (isQuestionAnswerable) created a
            // fresh function identity every render and defeated memo entirely.
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
                onQuestionAnswer={doSendSilent}
                onResume={doSend}
                isLastMsg={isLastMsg}
                liveQuestionId={liveQuestionId}
                suppressedQuestionKeys={streamItemQuestionKeys}
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

          {showStreamingSurface && (
            <StreamingMessage
              streamItems={streamItems}
              dataKey={streamingDataKey}
              onAnswer={doSendSilent}
            />
          )}

          {turnActive && streamItems.length === 0 && !loading && (
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

      <div ref={footRef} className="chat__foot">
        {buildPhaseRail.length > 0 && (
          <div className="chat__build-rail" role="group" aria-label="Build progress">
            {buildPhaseRail.map(phase => (
              <span
                key={phase.ts}
                className={`chat__build-phase${
                  phase.current ? ' chat__build-phase--current' : ''
                }`}
              >
                <span className="chat__build-phase-dot" aria-hidden="true" />
                <span className="chat__build-phase-label">{phase.label}</span>
              </span>
            ))}
          </div>
        )}
        {openAppCtas.length > 0 && (
          <div className="chat__open-app">
            {openAppCtas.map(({ app, vm }) => {
              const pulsing = pulsedAppId === Number(app.id)
              return (
                <button
                  key={app.id}
                  className={`chat__open-app-btn${pulsing ? ' chat__open-app-btn--pulse' : ''}`}
                  aria-label={pulsing ? `Preview updated for ${app.name || 'app'}` : vm.ariaLabel}
                  onClick={() => onOpenApp?.(app.id)}
                >
                  {pulsing ? 'Preview updated ✓' : `${vm.label} →`}
                </button>
              )
            })}
          </div>
        )}
        {hasPendingQuestion && pendingCardOffscreen && (
          <button
            type="button"
            className="chat__question-nudge"
            onClick={() => {
              // USER-initiated scroll — the no-auto-scroll contract only
              // forbids the app moving the viewport on its own; a tap on
              // this chip is the user asking to be taken to the card.
              findPendingQuestionCard()?.scrollIntoView({ block: 'nearest' })
            }}
          >
            Möbius asked you something — tap to answer
          </button>
        )}
        {hasPendingResume && resumeCardOffscreen && (
          <button
            type="button"
            className="chat__resume-nudge"
            onClick={() => {
              // USER-initiated scroll — same contract as the question nudge: a
              // tap is the user asking to be taken to the card, not the app
              // moving the viewport on its own.
              findResumeCard()?.scrollIntoView({ block: 'nearest' })
            }}
          >
            {pendingResumeBlock?.parked_until
              ? 'Rate limit reached — tap to resume'
              : 'Turn paused — tap to resume'}
          </button>
        )}
        <ConnectionStatus
          error={connectionError}
          reconnecting={reconnecting}
          onRetry={retry}
        />
        <QueuedMessages items={pendingQueue.pendingMessages} onCancel={handleCancelPending} />
        <ChatInputBar
          input={input}
          onInputChange={setInput}
          onSubmit={handleSubmit}
          inputRef={inputRef}
          sending={composerBusy}
          listening={listening}
          listeningRef={listeningRef}
          onToggleVoice={toggleVoice}
          onStop={handleStop}
          onSteer={handleSteer}
          canSteer={canSteer}
          canRequestSteer={canRequestSteer}
          offline={!online}
          pendingFiles={pendingFiles}
          onAddFiles={addFiles}
          onRemoveFile={removeFile}
          attachTriggerRef={attachTriggerRef}
          leftButtons={
            <>
              <ComposerPopover
                chatInfo={showPicker ? chatInfo : null}
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
                  // `provider` only changes when the user picked a new one —
                  // preserve the existing value otherwise so an unrelated
                  // PATCH doesn't wipe it.
                  setChatInfo(prev => prev ? ({
                    ...prev,
                    agent_settings_json: agent_settings_json,
                    provider: provider || prev.provider,
                    effective: effective || prev.effective,
                  }) : prev)
                }}
                onCompactionStored={handleCompactionStored}
                onOpenInspector={() => setShowInspector(true)}
              />
            </>
          }
        />
      </div>
    </div>
  )
}
