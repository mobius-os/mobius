import {
  useState,
  useRef,
  useEffect,
  useLayoutEffect,
  useCallback,
  useSyncExternalStore,
} from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { apiFetch, getAuthHeaders, BASE } from '../../api/client.js'
import { chatMessagesQueryKey } from '../../hooks/queries.js'
import useStreamConnection from './useStreamConnection.js'
import useScrollMode, {
  shouldPinSend,
} from './useScrollMode.js'
import useVoiceInput from './useVoiceInput.js'
import useFileUpload from './useFileUpload.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import useSystemEventStream from '../../hooks/useSystemEventStream.js'
import usePendingQueue from './hooks/usePendingQueue.js'
import useBridgePartial from './hooks/useBridgePartial.js'
import ChatInputBar from './ChatInputBar.jsx'
import AgentContextInspector from './AgentContextInspector.jsx'
import ChatSummaryViewer from './ChatSummaryViewer.jsx'
import ComposerPopover from './ComposerPopover.jsx'
import ConnectionStatus from './ConnectionStatus.jsx'
import StreamingMessage from './StreamingMessage.jsx'
import QueuedMessages from './QueuedMessages.jsx'
import MsgContent from './MsgContent.jsx'
import { formatResetTime } from './resetTime.js'
import {
  resetDeadlineDelay,
  resetDeadlineState,
  saveAutoResumePolicy,
} from './autoResumePolicy.js'
import {
  EMPTY_CHAT_RUN_SIGNAL,
  advanceChatRunSignal,
  chatRunSignalDelta,
} from '../../lib/chatRunSignal.js'
import {
  clearProviderSwitch,
  getProviderSwitchState,
  isProviderSwitchBlocking,
  subscribeProviderSwitch,
} from './providerSwitch.js'
import { questionKey } from './questionKey.js'
import { clearChatQuestionDrafts } from './questionDraft.js'
import { resolveStopResend } from './resolveStopResend.js'
import { focusComposerElement, shouldApplyComposerFocusRequest } from './composerFocusPolicy.js'
import { sameMessageList } from './chatMessageList.js'
import { copyableMessageText, copyPlainText } from './messageCopy.js'
import { sendFailureMessage } from './sendFailure.js'
import { assistantStreamCoversMessage, chooseActiveAssistantDataKey, chooseActiveAssistantMirrorIndex, chooseActiveAssistantSurface, findTrailingAssistantPartialIndex, promoteAssistantStream, streamItemsHaveRenderableContent, streamItemsToAssistantPayload } from './streamPromotion.js'
import {
  answerKeepsCurrentTurn,
  builtAppPulseDecision,
  canFastForwardQueue,
  cidOf,
  continuationRowsFromPromotedMessage,
  mergeRecentMessagesIntoLoadedWindow,
  openAppCtaViewModel,
  shouldRetryStopAfterConfirm,
  stopConfirmedIdle,
  stopRequestSucceeded,
  serverSnapshotBehindLocal,
  startedMessagesFromResponse,
  stripInternalUserMessageFields,
  systemEventForChat,
} from './chatRuntimeState.js'
import {
  cidForSendAttempt,
  sendDraftIdentity,
} from './sendAttemptIdentity.js'
import {
  clearFailedSendAttempt,
  loadFailedSendAttempt,
  saveFailedSendAttempt,
  sendAttemptIsDurable,
} from './sendAttemptRecovery.js'
import { persistComposerDraft } from './composerDraft.js'
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

function replaceOptimisticWithBatch(prev, cid, rows) {
  const base = cid == null
    ? prev
    : prev.filter(m => !(m?.role === 'user' && cidOf(m) === cid))
  return appendMessageBatch(base, rows)
}

function findUserIndexByCid(messages, cid) {
  if (cid == null) return -1
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (msg?.role === 'user' && cidOf(msg) === cid) return i
  }
  return -1
}

// Exported so sibling components (Shell, etc.) can clean up drafts when a
// chat is deleted.  Shell owns the deletion flow; it should call this after
// the chat row is removed from the list.
// NOTE: if deletion ever moves inside ChatView's own scope, call this inline
// instead of leaving the orphaned key behind.
export function deleteChatDraft(chatId) {
  try { sessionStorage.removeItem(`draft:${chatId}`) } catch { /* private browsing */ }
  clearFailedSendAttempt(chatId)
  clearChatQuestionDrafts(chatId)
}

function tailResumableBlock(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].hidden) continue
    const message = messages[i]
    if (message.role !== 'assistant' || !message.blocks?.length) return null
    const tail = message.blocks[message.blocks.length - 1]
    return tail.type === 'error' && tail.resumable ? tail : null
  }
  return null
}

const PENDING_DRAFT_KEY = 'pending-draft'
const PENDING_DRAFT_AUTOSEND_KEY = 'pending-draft-autosend'
const DRAFT_AUTOSEND_PREFIX = 'draft-autosend:'

function readInitialComposer(chatId) {
  try {
    const failedAttempt = loadFailedSendAttempt(chatId)
    const pending = sessionStorage.getItem(PENDING_DRAFT_KEY)
    if (pending && failedAttempt) clearFailedSendAttempt(chatId)
    const saved = sessionStorage.getItem(`draft:${chatId}`) || ''
    const input = pending || failedAttempt?.text || saved
    const autoSendDraft =
      sessionStorage.getItem(PENDING_DRAFT_AUTOSEND_KEY) ||
      sessionStorage.getItem(`${DRAFT_AUTOSEND_PREFIX}${chatId}`)
    return {
      input,
      autoSend: !!input && autoSendDraft === input,
      failedAttempt: pending ? null : failedAttempt,
      attachments: pending ? [] : (failedAttempt?.attachments || []),
    }
  } catch {
    return { input: '', autoSend: false, failedAttempt: null, attachments: [] }
  }
}

// Stable empty default so callers that pass no built apps (the embedded
// composer) don't hand ChatView a fresh array each render and re-fire its
// list-keyed effects.
const NO_BUILT_APPS = []

// One offscreen-visibility observer for a sticky footer nudge, used twice (the
// pending-question card and the resume card). Returns whether the found card is
// currently scrolled OUT of the scroll container's viewport, so the caller can
// show a "tap to jump to it" chip.
//
// `findCard` is a fresh closure every render (it reads scrollRef.current, a
// ref, so even a stale one queries live DOM), so it is deliberately kept OUT of
// the reactive dep array and read through a ref. The observer rebinds only on
// `active` flips and the explicit `rebindDeps` — the rendering-surface signals
// that can move the card's DOM node — exactly as the two hand-written effects
// did. Listing `findCard` in the deps would recreate the observer on every
// streaming re-render (a per-token IntersectionObserver thrash); the card's DOM
// node is stable across those, so it must not.
function useOffscreenNudge(scrollRef, active, findCard, rebindDeps) {
  const [offscreen, setOffscreen] = useState(false)
  const findCardRef = useRef(findCard)
  findCardRef.current = findCard
  useEffect(() => {
    if (!active) {
      setOffscreen(false)
      return undefined
    }
    const scrollEl = scrollRef.current
    const card = findCardRef.current()
    if (!scrollEl || !card || typeof IntersectionObserver === 'undefined') {
      setOffscreen(false)
      return undefined
    }
    const io = new IntersectionObserver(entries => {
      setOffscreen(!entries[0]?.isIntersecting)
    }, { root: scrollEl, threshold: 0 })
    io.observe(card)
    return () => io.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, scrollRef, ...rebindDeps])
  return offscreen
}

export default function ChatView({
  chatId,
  onStreamEnd,
  onFirstMessage,
  onSystemEvent,
  onChatMissing,
  builtApps = NO_BUILT_APPS,
  onOpenApp,
  onMessageStart,
  onQuestionAnswered,
  onVoiceListeningChange,
  showPicker = true,
  embedded = false,
  quickActions = null,
  getContext = null,
  composerFocusRequest = null,
  onComposerFocusHandled = null,
  externalRunSignal = EMPTY_CHAT_RUN_SIGNAL,
  onExternalRunEvent = null,
  // Multi-pane workspace (design §2): when this chat renders inside a tiled
  // pane, Shell passes the pane's projected CONTENT height (pane rect minus the
  // strip). A change means a committed geometry event (divider commit,
  // projection/mode flip, rotation, pane open/close) — forwarded to the scroll
  // controller's paneResized() below. Null for a single-pane chat (today's
  // behavior — the controller's own ResizeObserver owns resize there).
  paneContentHeight = null,
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
  const offsetRef = useRef(offset)
  offsetRef.current = offset
  const [loading, setLoading] = useState(!cached)
  // Warm cache content is useful immediately, but it is not authoritative for
  // entry layout. The first refresh decides whether an already-running turn's
  // catch-up must settle before the transcript can be revealed.
  const cachedEntryPhase = cached && !cached.running ? 'cached' : 'history'
  const [initialEntryPhase, setInitialEntryPhase] = useState(cachedEntryPhase)
  // On a failed initial /chats/{id} fetch, loadError flips in the catch so
  // the UI can render a retry message. Setting loading false alone would
  // render the empty-state UI ("What's on your mind?") as if the chat had no
  // history, hiding the real problem.
  const [loadError, setLoadError] = useState(false)
  // Bumped by the load-error Retry button to re-run the load effect in
  // place, instead of a hard window.location.reload (which would nuke the
  // Query cache, scroll positions, drafts, the app-iframe LRU, and the
  // back-stack — and contradicts the project's no-hard-reload principle).
  const [loadNonce, setLoadNonce] = useState(0)
  const [sending, setSending] = useState(() => !!cached?.running)
  // Terminal live-to-settled commits bump this sequence. The corresponding
  // layout effect settles an armed prompt pin against the committed DOM.
  const [pinnedSettleSeq, setPinnedSettleSeq] = useState(0)
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
  const [input, setInputState] = useState(() => initialComposerRef.current.input)
  function setComposerInput(nextInput) {
    // Navigation can unmount this component before React flushes passive
    // effects. Keep every composer transition durable at the state boundary,
    // whether it came from typing, voice, restoration, or send cleanup.
    persistComposerDraft(chatId, nextInput)
    setInputState(nextInput)
  }
  const [sendFailure, setSendFailure] = useState(() => (
    initialComposerRef.current.failedAttempt
      ? 'Möbius is checking whether your previous message reached the chat…'
      : null
  ))
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
  const [autoResumeSaving, setAutoResumeSaving] = useState(false)
  const [autoResumeError, setAutoResumeError] = useState('')
  const [autoResumeErrorSource, setAutoResumeErrorSource] = useState('')
  const [embeddedRunSignal, setEmbeddedRunSignal] = useState(
    EMPTY_CHAT_RUN_SIGNAL,
  )
  const [embeddedRunActive, setEmbeddedRunActive] = useState(false)
  // A counter is only a render wake-up; deadline elapsed is derived directly
  // from the current card's reset timestamp below, so a newly loaded card can
  // never render using the previous card's boolean state.
  const [, setLimitResetClockTick] = useState(0)
  const autoResumeSavingRef = useRef(false)
  const autoResumeRequestRef = useRef(0)
  const armedEmbeddedResetRef = useRef(null)
  // This external per-chat state survives ChatView's keyed unmount/remount.
  // Send handlers also read the store directly, closing the same-frame gap
  // before React paints disabled controls.
  const subscribeToProviderSwitch = useCallback(
    listener => subscribeProviderSwitch(chatId, listener),
    [chatId],
  )
  const readProviderSwitch = useCallback(
    () => getProviderSwitchState(chatId),
    [chatId],
  )
  const providerSwitchState = useSyncExternalStore(
    subscribeToProviderSwitch,
    readProviderSwitch,
    readProviderSwitch,
  )
  const providerSwitching = providerSwitchState.status === 'switching'
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
  // The pending-question and resume "tap to jump to it" nudges each track
  // whether their card is scrolled out of the viewport. Both use one shared
  // observer hook (useOffscreenNudge, below); their booleans are computed near
  // hasPendingQuestion / hasPendingResume where the card finders live.
  const [showInspector, setShowInspector] = useState(false)
  const [showSummary, setShowSummary] = useState(false)
  const [visibleTimestampKey, setVisibleTimestampKey] = useState(null)
  const [copyStatus, setCopyStatus] = useState('')
  const timestampTimerRef = useRef(null)
  const messageHoldRef = useRef(null)
  const suppressMessageClickRef = useRef(null)
  const copyStatusTimerRef = useRef(null)
  const [previewReadyStatus, setPreviewReadyStatus] = useState('')
  // The app id whose CTA is mid recompile-pulse (label swapped to "Preview
  // updated ✓" for ~2s), or null.
  const [pulsedAppId, setPulsedAppId] = useState(null)
  // Last-seen updated_at per built-app id, so the pulse/announce effect can tell
  // a first build (a new id) from a recompile (an existing id whose updated_at
  // advanced) without a separate app_built event — updated_at IS the monotonic
  // re-fire key. Per-ChatView-instance (fresh on remount), which is why the
  // pulse is naturally scoped to this chat.
  const lastSeenUpdatedAtRef = useRef(new Map())
  // Build-milestone rail: phases accumulated from chat-scoped `build_phase`
  // stream events (deduped by ts so catch-up replay rebuilds it), reset ONLY
  // when a new run starts for this chat (see buildPhaseRail.js for why a
  // mid-run reset is replay-incoherent). Rendered as a slim rail in the foot
  // near the open-app CTA; the announcement mirrors previewReadyStatus for
  // the polite live region.
  const [buildPhases, setBuildPhases] = useState(EMPTY_BUILD_PHASE_RAIL)
  const [buildPhaseStatus, setBuildPhaseStatus] = useState('')
  const lastAnnouncedPhaseRef = useRef(null)

  useEffect(() => () => {
    if (timestampTimerRef.current) clearTimeout(timestampTimerRef.current)
    if (messageHoldRef.current?.timer) clearTimeout(messageHoldRef.current.timer)
    if (copyStatusTimerRef.current) clearTimeout(copyStatusTimerRef.current)
  }, [])

  const cancelMessageHold = useCallback(() => {
    if (messageHoldRef.current?.timer) {
      clearTimeout(messageHoldRef.current.timer)
    }
    messageHoldRef.current = null
  }, [])

  const copyMessage = useCallback(async (message, key) => {
    const text = copyableMessageText(message)
    if (!text) return
    suppressMessageClickRef.current = key
    const copied = await copyPlainText(text)
    if (copied) {
      try { navigator.vibrate?.(8) } catch { /* haptics are optional */ }
    }
    setCopyStatus(copied ? 'Copied' : 'Couldn’t copy')
    if (copyStatusTimerRef.current) clearTimeout(copyStatusTimerRef.current)
    copyStatusTimerRef.current = setTimeout(() => {
      copyStatusTimerRef.current = null
      setCopyStatus('')
    }, 1800)
  }, [])

  const handleMessagePointerDown = useCallback((event, message, key) => {
    if (
      !_isTouchPrimary
      || event.pointerType !== 'touch'
      || event.button !== 0
      || event.target?.closest?.('button, a, input, textarea, summary, pre, code')
    ) return
    cancelMessageHold()
    const startX = event.clientX
    const startY = event.clientY
    const timer = setTimeout(() => {
      messageHoldRef.current = null
      void copyMessage(message, key)
    }, 520)
    messageHoldRef.current = { timer, startX, startY, key }
  }, [cancelMessageHold, copyMessage])

  const handleMessagePointerMove = useCallback((event) => {
    const hold = messageHoldRef.current
    if (!hold) return
    if (
      Math.abs(event.clientX - hold.startX) > 10
      || Math.abs(event.clientY - hold.startY) > 10
    ) cancelMessageHold()
  }, [cancelMessageHold])

  const showTimestamp = useCallback((event, key) => {
    if (suppressMessageClickRef.current === key) {
      suppressMessageClickRef.current = null
      return
    }
    if (window.getSelection?.()?.toString()) return
    if (timestampTimerRef.current) clearTimeout(timestampTimerRef.current)
    setVisibleTimestampKey(key)
    timestampTimerRef.current = setTimeout(() => {
      timestampTimerRef.current = null
      setVisibleTimestampKey(current => current === key ? null : current)
    }, 2200)
  }, [])
  useEffect(() => {
    autoResumeRequestRef.current += 1
    autoResumeSavingRef.current = false
    setAutoResumeSaving(false)
    setAutoResumeError('')
    setAutoResumeErrorSource('')
  }, [chatId])

  const handleAutoResumeChange = useCallback(async (next, source = 'card') => {
    if (autoResumeSavingRef.current) return
    autoResumeSavingRef.current = true
    const requestId = ++autoResumeRequestRef.current
    setAutoResumeSaving(true)
    setAutoResumeError('')
    setAutoResumeErrorSource(source)
    try {
      const result = await saveAutoResumePolicy({
        chatId,
        next,
        request: apiFetch,
      })
      if (requestId !== autoResumeRequestRef.current) return
      if (result.value !== null) {
        setChatInfo(prev => prev ? ({
          ...prev,
          auto_resume_on_limit: result.value,
        }) : prev)
      }
      setAutoResumeError(result.error)
    } finally {
      if (requestId === autoResumeRequestRef.current) {
        autoResumeSavingRef.current = false
        setAutoResumeSaving(false)
      }
    }
  }, [chatId])

  const handleAutoResumeSettingsChange = useCallback(
    next => handleAutoResumeChange(next, 'settings'),
    [handleAutoResumeChange],
  )

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
    if (nextOffset !== undefined) offsetRef.current = nextOffset
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
  const activeAssistantDataKeyRef = useRef(null)
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
  // If a POST's acknowledgement is lost, the composer is restored with the
  // same logical message identity. An unchanged retry reuses its cid so the
  // backend can acknowledge the durable row instead of starting a twin turn.
  const failedSendAttemptRef = useRef(initialComposerRef.current.failedAttempt)

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
  const onQuestionAnsweredRef = useRef(onQuestionAnswered)
  onQuestionAnsweredRef.current = onQuestionAnswered
  const onFirstMessageRef = useRef(onFirstMessage)
  onFirstMessageRef.current = onFirstMessage
  const onStreamEndRef = useRef(onStreamEnd)
  onStreamEndRef.current = onStreamEnd
  const onExternalRunEventRef = useRef(onExternalRunEvent)
  onExternalRunEventRef.current = onExternalRunEvent
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
  // useScrollMode owns the entire scroll state machine: semantic lifecycle
  // transitions, the automatic scroll-write funnel, geometry-based bottom
  // detection, ResizeObserver layout updates, user-gesture ownership, mobile
  // keyboard handling, diagnostics, and hide-then-reveal restore on mount.
  //
  // The hook returns:
  //   • modeRef               — read-only to ChatView for submit snapshots.
  //                             Lifecycle changes go through the returned
  //                             semantic controller methods below.
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
    anchorPagination,
    armSentMessage,
    closePreSendGestureWindow,
    freezeForegroundReturn,
    freezeQueuedSubmission,
    revealConversationTail,
    reapplyActiveMode,
    settleNonPin,
    settleStreamingPin,
    paneResized,
  } = useScrollMode({
    chatId,
    scrollRef,
    spacerRef,
    lastUserMsgRef,
    messages,
    messagesRef,
    pendingMessagesLength: pendingQueue.pendingMessages.length,
    loadingOlderRef: loadingOlder,
    turnRunning: sending || serverRunning,
    initialEntryCanReveal: initialEntryPhase === 'cached'
      || initialEntryPhase === 'ready',
    initialEntrySettled: initialEntryPhase === 'ready',
  })

  // Forward committed pane-geometry changes to the scroll controller. A new
  // projected height (divider commit, projection/mode flip, rotation) sets the
  // layout-derived floor and re-applies the active mode under the reader gate
  // (design §2). Skipped entirely for single-pane chats (paneContentHeight
  // null) so today's resize behavior is untouched.
  useEffect(() => {
    if (paneContentHeight != null) paneResized(paneContentHeight)
  }, [paneContentHeight, paneResized])

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

  function forgetQueuedPinIntent({ cid = null, cidList = null } = {}) {
    if (cid) queuedPinIntentByCidRef.current.delete(cid)
    if (Array.isArray(cidList)) {
      for (const value of cidList) queuedPinIntentByCidRef.current.delete(value)
    }
  }

  function takeQueuedPinIntent(cid) {
    if (!cid) return null
    const intent = queuedPinIntentByCidRef.current.get(cid) || null
    queuedPinIntentByCidRef.current.delete(cid)
    return intent
  }

  function forgetAllQueuedPinIntents() {
    queuedPinIntentByCidRef.current.clear()
    queuedContinuationPinIntentRef.current = null
    inlineSteerPinIntentRef.current = null
  }

  // The first-message exception is shared by every direct/promotion/steer
  // path. Stream promotion can render a user row one React commit before
  // messagesRef catches up, so state alone is not authoritative. A row is
  // first only when both the state mirror and rendered transcript are empty.
  function isFirstVisibleUserMessage() {
    const stateHasUser = messagesRef.current.some(
      m => m.role === 'user' && !m.hidden,
    )
    const domHasUser = !!scrollRef.current?.querySelector('.chat__msg--user')
    return !stateHasUser && !domHasUser
  }

  // Every send/steer/promote enters the scroll controller through this one
  // semantic event. ChatView resolves delayed-intent staleness; the controller
  // owns the actual PIN-vs-hold transition and the later automatic writes. The
  // pin targets the stable `cid` carried by the DOM row from mint.
  function pinSentMessage(cid, { willPin, intent } = {}) {
    armSentMessage({
      cid,
      willPin,
      intentCurrent: !intent || pinIntentStillCurrent(intent),
    })
  }

  // Re-fetch messages from the API. Called when the SSE stream reconnects
  // and gets a 204 (no active broadcast — the chat finished while the
  // user was offline or on poor connectivity). Replaces stale messages
  // with the current DB state.
  const fetchMessages = useCallback(async ({
    force = false,
    terminal204 = false,
    authoritative = false,
  } = {}) => {
    if (sendingRef.current && !force) return
    const gen = fetchGenRef.current
    try {
      const res = await apiFetch(`/chats/${chatId}?limit=20`, { timeoutMs: 15000 })
      if (!res.ok) throw new Error(`CHAT_FETCH_FAILED_${res.status}`)
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
        !authoritative
        && force
        && (sendingRef.current || isStreamingRef.current || serverRunningRef.current)
      const staleSnapshot =
        !terminal204
        && !preserveLocalTurn
        && serverSnapshotBehindLocal(msgs, messagesRef.current)
      if (preserveLocalTurn) {
        // A new local turn can begin while the mounted copy of the previous
        // assistant row is still a stale partial. Refresh the durable history,
        // but retain any optimistic user/queue rows newer than the server page.
        // Skipping the commit wholesale made the previous completed reply
        // disappear until a full remount.
        const refreshed = mergeRecentMessagesIntoLoadedWindow({
          loadedMessages: messagesRef.current,
          loadedOffset: offsetRef.current,
          recentMessages: msgs,
          recentOffset: data.offset || 0,
          preserveLocalSuffix: true,
        })
        commitMessages(refreshed.messages, refreshed.offset)
      } else if (!staleSnapshot) {
        const refreshed = mergeRecentMessagesIntoLoadedWindow({
          loadedMessages: messagesRef.current,
          loadedOffset: offsetRef.current,
          recentMessages: msgs,
          recentOffset: data.offset || 0,
        })
        commitMessages(refreshed.messages, refreshed.offset)
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
      return {
        running: !!data.running,
        pendingLimitResume: !!tailResumableBlock(msgs)?.pause?.resets_at,
      }
    } catch {
      // Network error — silent, user can retry. Callers that need to attach
      // to a newly announced run must distinguish this ambiguous result from
      // an authoritative idle verdict.
      return null
    }
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
      // (localAuthoritative, above) — the optimistic queue + confirmQueued
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

  useEffect(() => {
    if (
      providerSwitchState.status !== 'success'
      || !providerSwitchState.result
    ) return
    const data = providerSwitchState.result
    setChatInfo(prev => prev ? ({
      ...prev,
      agent_settings_json: data.agent_settings_json,
      provider: data.provider || prev.provider,
      effective: data.effective || prev.effective,
    }) : prev)
    handleCompactionStored()
    clearProviderSwitch(chatId)
  }, [
    chatId,
    handleCompactionStored,
    providerSwitchState.result,
    providerSwitchState.status,
  ])

  const {
    streamItems,
    latestItemsRef,
    isStreaming,
    isStreamingRef,
    connectionError,
    reconnecting,
    catchUpCommitSeq,
    sendMessage: streamSend,
    connectToStream,
    retry,
    disconnect,
    clearStreamItems,
    patchQuestionAnswers,
  } = useStreamConnection(chatId, {
    onStreamEnd: ({ continues, promotedMessage } = {}) => {
      if (embedded && continues === false) setEmbeddedRunActive(false)
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
          const contIsFirstUser = isFirstVisibleUserMessage()
          const pinCid = cidOf(promotedRows[0])
          const fallbackWillPin = () => shouldPinSend({
            scrollEl: scrollRef.current,
            mode: modeRef.current,
            isFirstUserMsg: contIsFirstUser,
            // The original submit-time intent is unavailable (for example
            // after a remount). Never infer a delayed pin from the reader's
            // later position; only the first-message exception remains.
            wasAtContentBottom: false,
          })
          const contWillPin = continuationPinIntent
            ? continuationPinIntent.willPin
            : fallbackWillPin()
          commitMessages(prev => appendMessageBatch(prev, promotedRows))
          promotedRef.current = false
          pinSentMessage(pinCid, {
            willPin: contWillPin,
            intent: continuationPinIntent,
          })
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
        setPinnedSettleSeq(seq => seq + 1)
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
      const consumedCids = message?._consumed_cids
      const serverRows = Array.isArray(message?._messages)
        ? message._messages.map(stripInternalUserMessageFields).filter(Boolean)
        : null
      const localPromoted = Array.isArray(consumedCids)
        ? pendingQueue.promoteManyByCid(consumedCids)
        : pendingQueue.promoteAll()
      queuedContinuationLocalPromotedRef.current =
        serverRows?.length ? serverRows : localPromoted
      // The pin intent was stamped at submit under the queued row's cid; the
      // backend echoes those cids back as _consumed_cids (or the promoted row
      // carries the head cid). Look it up by the head cid.
      const pinCid = cidOf(
        (serverRows && serverRows[0])
        || localPromoted
        || (Array.isArray(consumedCids) ? { cid: consumedCids[0] } : null),
      )
      queuedContinuationPinIntentRef.current = takeQueuedPinIntent(pinCid)
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
      // It still follows the one visible-row scroll rule. Automatic queue
      // promotion keeps the original submit snapshot; an explicit fast-forward
      // captures a fresh snapshot when pressed, because that is the deliberate
      // action making the row visible. Whether it pins or holds, the row gets
      // the same permanent bottom reservation as a normal send.
      //
      // Current backends carry a non-empty `messages` array, each row with its
      // stable cid (card-221: every row carries one). During rolling deploys an
      // older stream may still send only the legacy single-row `{ts, content}`
      // shape; render that too so a steered message is not dropped.
      const steeredSource = Array.isArray(steeredBatch) && steeredBatch.length > 0
        ? steeredBatch
        : (content ? [{ ts, content }] : [])
      const steeredMessages = steeredSource
        .map((m, i) => {
          const tsv = m?.ts ?? (ts != null ? ts + i : Date.now() + i)
          return {
            role: 'user',
            content: m?.content || '',
            ts: tsv,
            cid: m?.cid ?? null,
            ...(m?.attachments ? { attachments: m.attachments } : {}),
          }
        })
      const pinCid = cidOf(steeredMessages[0])
      const pinIntent = steerPinIntentRef.current
        || inlineSteerPinIntentRef.current
        || takeQueuedPinIntent(pinCid)
      inlineSteerPinIntentRef.current = null
      promoteStreamToMessages({ keepTurnOpen: true })
      const steeredIsFirstUser = isFirstVisibleUserMessage()
      const fallbackWillPin = () => shouldPinSend({
        scrollEl: scrollRef.current,
        mode: modeRef.current,
        isFirstUserMsg: steeredIsFirstUser,
        // A missing submit-time intent must degrade to hold, not infer a pin
        // from wherever the reader happens to be when the SSE event arrives.
        wasAtContentBottom: false,
      })
      const steerWillPin = pinIntent ? pinIntent.willPin : fallbackWillPin()
      // Arm the scroll mode BEFORE rendering the steered row. EventSource
      // callbacks are outside React's synthetic event layer, and query-cache
      // listeners can observe the transcript update immediately; setting the
      // mode first prevents a one-frame "row appears low, then snaps up" steer.
      pinSentMessage(pinCid, { willPin: steerWillPin, intent: pinIntent })
      // Dedup by ts so a reconnect's catch-up replay of the same event
      // can't double-insert the steered user message. Insert by transcript ts
      // instead of blindly appending: if a fetch/replay already committed the
      // post-steer assistant row, the steered user still belongs before it.
      commitMessages(prev => insertMessageBatchByTs(prev, steeredMessages))
      steerPinIntentRef.current = null
    },
  })

  // System run activity is a structured sequence, not a running boolean: it
  // preserves coalesced start+finish events. Reconciliation is single-flight
  // and drains the latest sequence without effect cleanup cancelling an older
  // GET. Only an authoritative/announced start attaches; a Stop-invalidated
  // `undefined` fetch result never can.
  const effectiveRunSignal = embedded ? embeddedRunSignal : externalRunSignal
  const externalSignalRef = useRef(effectiveRunSignal)
  externalSignalRef.current = effectiveRunSignal
  const processedExternalSignalRef = useRef(effectiveRunSignal)
  const externalReconcileInFlightRef = useRef(false)
  const externalClaimedRunRef = useRef(false)
  const reconcileExternalActivity = useCallback(async () => {
    if (externalReconcileInFlightRef.current) return
    externalReconcileInFlightRef.current = true
    try {
      while (
        processedExternalSignalRef.current.seq
        < externalSignalRef.current.seq
      ) {
        const previous = processedExternalSignalRef.current
        const target = externalSignalRef.current
        processedExternalSignalRef.current = target
        const delta = chatRunSignalDelta(previous, target)
        const locallyActive = (
          sendingRef.current || isStreamingRef.current
        ) && !externalClaimedRunRef.current
        if (locallyActive) {
          // The local optimistic turn remains authoritative for its suffix,
          // but completed history still needs server reconciliation. Without
          // this fetch, an under-promoted previous reply stays missing for the
          // lifetime of the open tab.
          await fetchMessages({ force: true })
          continue
        }

        if (delta.started && !delta.finished) {
          externalClaimedRunRef.current = true
          sendingRef.current = true
          setSending(true)
          setServerRunningState(true)
        } else if (delta.finished) {
          externalClaimedRunRef.current = false
          sendingRef.current = false
          setSending(false)
          setServerRunningState(false)
        }

        const snapshot = await fetchMessages({
          force: true,
          authoritative: true,
        })
        if (externalSignalRef.current.seq !== target.seq) continue
        const running = snapshot?.running
        if (running === false) {
          externalClaimedRunRef.current = false
          if (embedded) setEmbeddedRunActive(false)
          if (!snapshot.pendingLimitResume) {
            onExternalRunEventRef.current?.('chat_run_finished')
          }
        } else if (running === true && embedded) {
          setEmbeddedRunActive(true)
        }
        if (
          (running === true || (snapshot === null && delta.started))
          && !delta.finished
          && !isStreamingRef.current
        ) {
          await Promise.resolve(connectToStream(true)).catch(() => {})
        }
      }
    } finally {
      externalReconcileInFlightRef.current = false
      if (
        processedExternalSignalRef.current.seq
        < externalSignalRef.current.seq
      ) {
        queueMicrotask(reconcileExternalActivity)
      }
    }
  }, [connectToStream, embedded, fetchMessages, isStreamingRef])
  useEffect(() => {
    reconcileExternalActivity()
  }, [effectiveRunSignal.seq, reconcileExternalActivity])

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
  } = useFileUpload({
    chatId,
    initialFiles: initialComposerRef.current.attachments,
  })

  function clearFailedAttempt() {
    failedSendAttemptRef.current = null
    clearFailedSendAttempt(chatId)
  }

  function rememberFailedAttempt(attempt) {
    failedSendAttemptRef.current = attempt
    saveFailedSendAttempt(chatId, attempt)
  }

  // Reuse a failed attempt id only while the restored composer is genuinely
  // untouched. Once the owner edits text or attachments—even if they later
  // recreate the same visible draft—that is a new compose action and gets a
  // new cid on send.
  function handleComposerInputChange(nextInput) {
    clearFailedAttempt()
    setSendFailure(null)
    setComposerInput(nextInput)
  }

  function handleComposerAddFiles(fileList) {
    clearFailedAttempt()
    setSendFailure(null)
    return addFiles(fileList)
  }

  function handleComposerRemoveFile(fileId) {
    clearFailedAttempt()
    setSendFailure(null)
    return removeFile(fileId)
  }

  function restoreComposerText(
    text,
    { focus = false, preserveFailedAttempt = false } = {},
  ) {
    if (preserveFailedAttempt) setComposerInput(text)
    else handleComposerInputChange(text)
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

  const {
    listening,
    listeningRef,
    stopVoice,
    toggleVoice,
    acceptManualEdit,
  } = useVoiceInput({
    onTranscript: handleComposerInputChange,
    inputRef,
  })
  // Report only WHETHER dictation is live (the shell tracks a single boolean,
  // not which chat) — this ChatView is single-mount, so it is the sole source.
  useEffect(() => {
    onVoiceListeningChange?.(listening)
    return () => { onVoiceListeningChange?.(false) }
  }, [listening, onVoiceListeningChange])

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
    // Promotion ends this active row. A queued/steered continuation must seed
    // its own anchor instead of inheriting a bridged DB key.
    activeAssistantDataKeyRef.current = null
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
  // This remains as a safety net for programmatic input changes (restores,
  // voice transcription, send cleanup). Direct owner edits are saved
  // synchronously in handleComposerInputChange above.
  useEffect(() => {
    persistComposerDraft(chatId, input)
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

  // Announce a new build and flash a recompile, both derived from updated_at
  // deltas on the (server-derived) CTA list — no app_built event, no nonce. A
  // brand-new CTA id is a first build (announce "Live preview ready …" without
  // pulsing); an already-seen id whose updated_at advanced is a recompile
  // (flash "Preview updated ✓" for 2s + announce). builtAppPulseDecision owns
  // that pure distinction; this effect applies its verdict. Because `builtApps`
  // is referentially stable (Shell memoizes it on a content signature) this runs
  // only when THIS chat's derived list actually changes.
  useEffect(() => {
    if (builtApps.length === 0) {
      lastSeenUpdatedAtRef.current = new Map()
      setPreviewReadyStatus('')
      return
    }
    const { pulseId, announce, nextSeen } = builtAppPulseDecision(
      builtApps, lastSeenUpdatedAtRef.current,
    )
    lastSeenUpdatedAtRef.current = nextSeen
    if (announce) setPreviewReadyStatus(announce)
    if (pulseId == null) return
    setPulsedAppId(pulseId)
    const t = setTimeout(() => setPulsedAppId(null), 2000)
    return () => clearTimeout(t)
  }, [builtApps])

  // Fetch messages and connect to an in-progress stream if the agent is running.
  useEffect(() => {
    let cancelled = false
    chatIdStaleRef.current = false
    setLoadError(false)
    setInitialEntryPhase(cachedEntryPhase)

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
        const failedAttempt = failedSendAttemptRef.current
        if (failedAttempt) {
          if (sendAttemptIsDurable(failedAttempt, msgs, data.pending_messages)) {
            clearFailedAttempt()
            setComposerInput('')
            clearFiles()
            setSendFailure(null)
          } else {
            setSendFailure(
              'That message didn’t reach the chat. It’s ready in the composer—try again.',
            )
          }
        }
        // Snapshot the per-chat runtime config (provider/model/effort) BEFORE
        // the behind-local guard below. This is independent of the messages
        // snapshot, and the guard's early-return used to skip it — so after any
        // interaction (local optimistic state ahead of the server snapshot) the
        // `+` popover's model picker silently vanished, leaving only Attach +
        // "What the agent knows". Setting it here keeps the picker present
        // regardless of the messages fast-path.
        setChatInfo({
          provider: data.provider || 'claude',
          created_by_app_id: data.created_by_app_id ?? null,
          agent_settings_json: data.agent_settings_json || null,
          effective: data.effective_agent_settings || {},
          has_assistant_turns: !!data.has_assistant_turns,
          auto_resume_on_limit: !!data.auto_resume_on_limit,
        })
        if (serverSnapshotBehindLocal(msgs, messagesRef.current)) {
          setInitialEntryPhase(data.running ? 'catch-up' : 'ready')
          setLoading(false)
          return
        }

        // Keep the DB partial when the agent is still running. The user sees
        // the most recent persisted state immediately; SSE catch-up makes the
        // same active MsgContent swap to the live payload. On done,
        // promoteStreamToMessages replaces this partial with the final version.
        // Previously we stripped this and waited for SSE — caused the "message
        // disappears on choppy return" bug.

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

        const refreshed = mergeRecentMessagesIntoLoadedWindow({
          loadedMessages: messagesRef.current,
          loadedOffset: offsetRef.current,
          recentMessages: msgs,
          recentOffset: data.offset || 0,
        })
        commitMessages(refreshed.messages, refreshed.offset)
        setServerRunningState(!!data.running)
        hadMessagesRef.current = refreshed.messages.length > 0
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
        setInitialEntryPhase(data.running ? 'catch-up' : 'ready')
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
        setInitialEntryPhase('ready')
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
          anchorPagination(anchorKey, anchorOffset)
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
    // load. Only paginate while the shared controller says the reader owns
    // scrolling: from pointer/wheel/touch/key input through its first scroll,
    // then through the short momentum window.
    const userDriven = performance.now() < gestureWindowUntilRef.current
    if (!userDriven) return
    if (el.scrollTop < 5 && offset > 0) {
      loadOlderMessages()
    }
  }


  // `opts.pin` allows the shared submit-time rule to pin the message. Normal
  // user sends opt in, but still pin only when first-or-already-following at
  // the bottom. Pass `pin: false` from synthetic-send paths where pinning
  // would be surprising:
  //   - handleStop's queue-collapse: the user clicked Stop, not Send;
  //     pinning the auto-generated combined message would yank the
  //     viewport away from whatever the user was reading (the partial
  //     they just stopped) → original turn 1 user msg + partial get
  //     pushed above the viewport. Keep their current scroll mode
  //     instead — the new turn streams into view from where they were.
  const doSend = useCallback(async (text, opts = {}) => {
    if (isProviderSwitchBlocking(chatId)) return
    const pin = opts.pin !== false  // default true
    if (!text.trim()) return
    if (pendingFiles.some(c => c.status === 'uploading')) return
    setSendFailure(null)

    // Stop voice recognition so a late onresult doesn't refill input
    // after we clear it.
    if (listeningRef.current) stopVoiceRef.current?.()

    // Resolve the ONE direct/queued/steered pin rule BEFORE blurring the
    // textarea. The real-content geometry is authoritative; mode can lag a
    // gesture/layout by a frame. Mobile blur can resize/clamp the viewport, so
    // capture the complete decision before it.
    const isFirstUserMsgAtSubmit = isFirstVisibleUserMessage()
    const willPinAtSubmit = pin && shouldPinSend({
      scrollEl: scrollRef.current,
      mode: modeRef.current,
      isFirstUserMsg: isFirstUserMsgAtSubmit,
    })
    const sendPinIntent = makeSendPinIntent(willPinAtSubmit)
    // Sending is a newer explicit action than the wheel/touch gesture that
    // positioned the viewport for that send. Browsers update scrollTop
    // synchronously but may dispatch the matching `scroll` event later; if the
    // old gesture window stays open, that delayed event can land after this
    // snapshot and cancel the brand-new PIN before its spacer is measured.
    // Close only the PRE-SEND ownership window. Any wheel/touch/key input that
    // begins after this line opens a fresh window and still wins normally.
    closePreSendGestureWindow()

    const queuesBehindActiveTurn = !!(
      sendingRef.current
      || isStreamingRef.current
      || serverRunningRef.current
      || pendingQueue.pendingMessagesRef.current.length > 0
    )
    if (queuesBehindActiveTurn) {
      // Queueing changes the footer immediately (new chip, cleared composer,
      // mobile keyboard close) but adds no transcript row yet. Freeze the
      // exact visible message before any of those layout changes. The captured
      // submit intent above is kept for the later promotion/steer, while the
      // current in-flight answer stays where the reader left it now.
      freezeQueuedSubmission()
    }

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
      restoreComposerText(text, { preserveFailedAttempt: true })
      if (usesComposerFiles) restoreFiles(composerFileSnapshot)
    }

    // Mint the message's stable identity ONCE, before the queue-vs-fresh
    // branch, so both paths carry the same `cid` from optimistic render
    // through the wire and into persistence. If a prior POST failed after the
    // server may have accepted it, an unchanged restored draft reuses that cid
    // and lets the backend's durable identity gate answer the ambiguity.
    const draftIdentity = sendDraftIdentity(chatId, text, attachments)
    const cid = cidForSendAttempt({
      failedAttempt: failedSendAttemptRef.current,
      draftIdentity,
      mintCid: () => ((typeof crypto !== 'undefined' && crypto.randomUUID)
        ? crypto.randomUUID()
        : `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`),
    })

    // QUEUE PATH: agent is streaming or queue isn't empty. Optimistic
    // entry carrying the minted `cid` — the row identity is stable across
    // the optimistic→server-ts display update. Backend writes to
    // chat.pending_messages via POST /messages returning {status, ts, position}.
    //
    // Read from refs (not React state) so doSend stays closure-safe.
    // Callers like handleStop invoke doSend AFTER calling
    // setSending(false) — the captured `sending` state would still
    // be `true` in this render's closure, sending the message to the
    // queue path instead of the fresh-send path. Refs reflect the
    // latest commit and dodge that.
    if (queuesBehindActiveTurn) {
      const queuedMsg = { role: 'user', content: text, ts: Date.now(), cid, queued: true }
      if (attachments.length > 0) queuedMsg.attachments = attachments
      pendingQueue.add(queuedMsg, { inFlight: true })
      // The shared send decision was captured AT SEND TIME, before blur or the
      // POST. If this queued send is promoted into the active turn (the backend
      // returns started, either as `queued+started` or the `started` race),
      // it becomes a new visible user message and must follow the same pin
      // rule as a fresh send. The at-bottom / following decision and the
      // first-user check must reflect the moment of sending — reading them
      // AFTER `await streamSend(...)` lets a scroll during the POST flip the
      // decision. The user-scroll intent version lets us detect such a scroll
      // and yield to it (a user-driven scroll after send is the newer intent).
      const queuedWillPin = willPinAtSubmit
      const queuedPinIntent = sendPinIntent
      rememberQueuedPinIntent(cid, queuedPinIntent)
      inlineSteerPinIntentRef.current = queuedPinIntent
      setComposerInput('')
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
          { queueOnly: true, cid },
        )
        clearFailedAttempt()
        releaseComposerFilesAfterAccepted()
        if (result?.status === 'duplicate') {
          // A stale local queue decision can race an already-durable retry.
          // Remove only this send's optimistic tray row; an unrelated live
          // turn may still be streaming and must remain attached.
          pendingQueue.cancelByCid(queuedMsg.cid)
          forgetQueuedPinIntent({ cid: queuedMsg.cid })
          inlineSteerPinIntentRef.current = null
          const durableRows = startedMessagesFromResponse(result)
          if (durableRows) {
            commitMessages(prev => appendMessageBatch(prev, durableRows))
          }
          const continues = result.running === true
          if (!continues) {
            setSending(false)
            sendingRef.current = false
            setServerRunningState(false)
            onStreamEndRef.current?.({ continues: false })
          }
          fetchMessages({ force: true, authoritative: true })
          return
        }
        if (result?.status === 'queued') {
          const canonicalPending = result.pending_message || null
          // Update the DISPLAY ts + canonical content on the cid-matched row.
          // Identity (cid) never changes, so there is no swap — just a confirm.
          const ackTs = canonicalPending?.ts ?? result.ts
          pendingQueue.confirmQueued(cid, {
            ts: ackTs ?? queuedMsg.ts,
            position: result.position,
            serverMsg: canonicalPending,
          })
          if (!canonicalPending) {
            // Older backends acknowledge only {ts, position}. Hydrate
            // immediately so the queued row uses the server's canonical text
            // before the user taps fast-forward; otherwise upload/context
            // augmentation can make force-steer reject until a remount.
            fetchMessages({ force: true })
          }
          if (result.started) {
            if (Array.isArray(result.message?._consumed_cids)) {
              pendingQueue.promoteManyByCid(result.message._consumed_cids)
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
            // The queued send was promoted straight into the active turn, so
            // it's a new visible user message and follows the send rule just
            // like a fresh send. The pin targets the stable cid (the started
            // row carries the same cid the client minted).
            pinSentMessage(cid, {
              willPin: queuedWillPin,
              intent: queuedPinIntent,
            })
            forgetQueuedPinIntent({
              cid,
              cidList: result.message?._consumed_cids,
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
          if (Array.isArray(result.message?._consumed_cids)) {
            pendingQueue.promoteManyByCid(result.message._consumed_cids)
          }
          pendingQueue.cancelByCid(cid)
          onMessageStartRef.current?.()
          promotedRef.current = false
          // Same run-start semantics as the branch above: this send became
          // the first message of a NEW run, so the rail resets here too.
          setBuildPhases(railAtRunStart())
          // Apply the send rule before appending — see shouldPinSend and
          // the fresh-send path. A message that raced into a started turn
          // is still a new send becoming the active turn, so it pins only
          // when first-or-at-bottom. The decision was captured at send time.
          const startedMessages = startedMessagesFromResponse(result)
          commitMessages(prev => {
            if (startedMessages) return appendMessageBatch(prev, startedMessages)
            // Strip the queue-envelope fields but KEEP cid — the visible user
            // row needs it as its stable data-cid pin target.
            const { queued: _q, position: _p, ...msg } = queuedMsg
            return appendMessageBatch(prev, [msg])
          })
          setSending(true)
          setServerRunningState(true)
          // New visible user msg → pin the stable cid to the top when the rule
          // allows; otherwise the funnel retires any stale pin to the reader's
          // anchor and reservation stays available below.
          pinSentMessage(cid, {
            willPin: queuedWillPin,
            intent: queuedPinIntent,
          })
          forgetQueuedPinIntent({
            cid,
            cidList: result.message?._consumed_cids,
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
        // each clear it above (confirmQueued / cancelByCid). Any
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
        rememberFailedAttempt({
          cid,
          draftIdentity,
          text,
          attachments: composerFileSnapshot,
        })
        restoreComposerAfterFailedSend()
        setSendFailure(sendFailureMessage(err, { online }))
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

    // Direct sends use the same submit-time decision as queued/steered sends.
    // A legitimate pin changes FOLLOW_BOTTOM to PIN_USER_MSG, so reply growth
    // stays below the prompt until the user manually scrolls to the bottom.
    const willPin = willPinAtSubmit
    // The send-time pin intent, carried across the async POST so a user scroll
    // that lands during it can still win. The pinned row's identity is the
    // minted `cid`, which the optimistic row and the confirmed server row
    // share — so the pin never needs to be retargeted across a ts swap.
    const freshPinIntent = sendPinIntent

    const userMsg = { role: 'user', content: text, ts: Date.now(), cid, optimistic: true }
    if (attachments.length > 0) userMsg.attachments = attachments
    commitMessages(prev => [...prev, userMsg])
    setComposerInput('')
    clearComposerFilesForSend()
    if (inputRef.current) {
      inputRef.current.style.height = 'auto'
      // Drop the multi-line `.chat__pill--tall` class — see queue-path
      // comment above for the full rationale.
      inputRef.current.closest('.chat__pill')?.classList.remove('chat__pill--tall')
    }
    setSending(true)
    setServerRunningState(true)
    // Pin per the R2 send rule via the funnel: it arms the reservation spacer
    // on every send and, when not pinning, retires any stale PIN to the
    // reader's anchor so their viewport stays fixed. The row carries its final
    // cid from mint, so the pin lands on the first apply.
    pinSentMessage(cid, { willPin, intent: freshPinIntent })
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
      const result = await streamSend(
        sendText,
        attachments.length > 0 ? attachments : undefined,
        // The minted cid rides the POST so the durable row carries the same
        // identity the optimistic row (and its pin) already use — without it
        // the server row derives legacy-<ts> and the strict data-cid pin
        // selector goes blind after the ack re-render.
        { cid },
      )
      clearFailedAttempt()
      releaseComposerFilesAfterAccepted()
      if (result?.status === 'duplicate') {
        const durableRows = startedMessagesFromResponse(result)
        if (durableRows) {
          commitMessages(prev => replaceOptimisticWithBatch(prev, cid, durableRows))
        } else {
          commitMessages(prev => prev.filter(
            m => !(m?.role === 'user' && cidOf(m) === cid && m.optimistic),
          ))
        }
        const continues = result.running === true
        setSending(continues)
        sendingRef.current = continues
        setServerRunningState(continues)
        if (!continues) onStreamEndRef.current?.({ continues: false })
        fetchMessages({ force: true, authoritative: true })
        return
      }
      if (result?.status === 'queued') {
        const canonicalPending = result.pending_message || null
        commitMessages(prev => {
          const next = [...prev]
          const idx = findUserIndexByCid(next, cid)
          if (idx >= 0) next.splice(idx, 1)
          return next
        })
        // The queued row keeps the MINTED cid — its identity does not change
        // because the server was told to queue a fresh send. It is already
        // server-confirmed (the POST acked), so it is NOT in flight.
        pendingQueue.add({
          ...(canonicalPending || userMsg),
          ts: canonicalPending?.ts ?? result.ts ?? userMsg.ts,
          cid,
          queued: true,
          serverTs: !!canonicalPending || typeof result.ts === 'number',
          position: result.position,
        }, { inFlight: false })
        if (!canonicalPending) {
          // Same compatibility path as the queue-only branch: reconcile the
          // visible queued tray with the server's exact pending row before
          // fast-forward can compare against stale local text.
          fetchMessages({ force: true })
        }
        if (result.started) {
          if (Array.isArray(result.message?._consumed_cids)) {
            pendingQueue.promoteManyByCid(result.message._consumed_cids)
          }
          const startedMessages = startedMessagesFromResponse(result)
          pinSentMessage(cid, { willPin, intent: freshPinIntent })
          if (startedMessages) {
            commitMessages(prev => appendMessageBatch(prev, startedMessages))
          }
          return
        }
        if (!result.started) {
          const queuedPinStillValid = pinIntentStillCurrent(freshPinIntent)
          if (queuedPinStillValid) {
            settleNonPin({
              retireFollow: pin,
              event: 'send:not-started-hold',
            })
          }
          setSending(false)
          setServerRunningState(false)
        }
        return
      }
      const startedMessages = startedMessagesFromResponse(result)
      if (startedMessages) {
        // The started row carries the same cid the client minted, so the pin
        // targets that cid directly — no retarget from optimistic to canonical
        // ts, and no last-row fallback. The funnel owns arming + staleness.
        pinSentMessage(cid, { willPin, intent: freshPinIntent })
        commitMessages(prev => {
          return replaceOptimisticWithBatch(prev, cid, startedMessages)
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
      rememberFailedAttempt({
        cid,
        draftIdentity,
        text,
        attachments: composerFileSnapshot,
      })
      restoreComposerAfterFailedSend()
      // The POST never reached/finished on the server, so remove the optimistic
      // user bubble and keep the text in the composer. Otherwise a transient
      // "Failed to fetch" looks like the message was accepted locally but
      // silently disappears from the durable chat after refresh.
      commitMessages(prev => {
        const next = [...prev]
        const idx = findUserIndexByCid(next, cid)
        if (idx >= 0) next.splice(idx, 1)
        return next
      })
      setSendFailure(sendFailureMessage(err, { online }))
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
    chatId,
    streamSend,
    pendingFiles,
    commitMessages,
    fetchMessages,
    clearFiles,
    restoreFiles,
    releaseFiles,
    online,
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
    if (sendSilentInFlightRef.current) return false
    sendSilentInFlightRef.current = true
    if (!text.trim()) {
      sendSilentInFlightRef.current = false
      return false
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
      return false
    }
    sendingRef.current = true
    onMessageStartRef.current?.()
    promotedRef.current = false

    setSending(true)
    setServerRunningState(true)
    // Hidden answer is a continuation, NOT a new visible send. The
    // user may be reading somewhere else; don't yank them with a
    // PIN. The agent's response builds into the existing assistant
    // message; if the user was at FOLLOW_BOTTOM they'll see it
    // forming, if ANCHOR_AT they stay where they are.
    try {
      // Mint a cid for symmetry so the persisted hidden row carries a stable
      // identity for reload dedup. It is inert here — a hidden answer send
      // renders no visible user bubble and never pins.
      const silentCid = (typeof crypto !== 'undefined' && crypto.randomUUID)
        ? crypto.randomUUID()
        : `cid-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
      const response = await streamSend(text, undefined, {
        hidden: true,
        cid: silentCid,
        answers: resolvedAnswers,
        question_id: questionId,
      })
      // The 202 means the answer write committed. Settle the durable and live
      // card sources only now; an optimistic pre-request answer made transient
      // failures look final and erased the retryable per-tab question draft.
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
        // A mid-turn question may still live in streamItems rather than the
        // durable message list. Keep both render sources in agreement.
        patchQuestionAnswers(questionId, resolvedAnswers)
      }
      // `answer_delivered` resumes the SAME assistant turn. Keep its bridge
      // alive so terminal promotion replaces/extends the active row rather
      // than dropping the question and pre-answer output during the
      // live-to-durable source handoff. Only a recovered answer returns
      // `started`: the original runner is gone and the hidden continuation is
      // genuinely a new turn, so that path must append instead.
      // Unknown future modes also retire the bridge: preserving the completed
      // question row and appending is safer than overwriting it with output
      // from a turn whose ownership semantics this client does not know.
      if (!answerKeepsCurrentTurn(response)) {
        bridgeHook.markBridged()
        activeAssistantDataKeyRef.current = null
      }
      // The answer write has committed before the 202 response. Refreshing the
      // owner's chat list now makes this deliberate interaction visible in
      // drawer recency immediately, instead of waiting for the resumed turn to
      // finish and emit its terminal refresh.
      onQuestionAnsweredRef.current?.()
      if (questionId) setLiveQuestionId(prev => prev === questionId ? null : prev)
      return true
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
        throw err
      }
      commitMessages(prev => [
        ...prev,
        { role: 'assistant', content: `Error: ${err.message}`, blocks: [] },
      ])
      throw err
    } finally {
      sendSilentInFlightRef.current = false
    }
  }, [streamSend, commitMessages, fetchMessages])

  function handleSubmit(e) {
    e.preventDefault()
    if (isProviderSwitchBlocking(chatId)) return
    doSend(input.trim())
  }

  // Cancel a queued message via DELETE. Optimistic remove; reconcile
  // by re-fetching authoritative state on success or on error.
  const handleCancelPending = useCallback(async (cid) => {
    pendingQueue.cancelByCid(cid)
    forgetQueuedPinIntent({ cid })
    try {
      const res = await apiFetch(`/chats/${chatId}/pending/${encodeURIComponent(cid)}`, {
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
      // at all. pendingQueue.clear() updates pendingMessagesRef.current to
      // [] before this line returns (synchronous).
      fetchGenRef.current += 1
      forgetAllQueuedPinIntents()
      pendingQueue.clear()

      let stoppedCleanly = false
      // The backend reports which queued cids it actually removed. null = an
      // older backend without the field (→ fall back to resending all); an
      // array is the authoritative cleared set.
      let clearedPendingCids = null
      const requestStopOnce = async () => {
        const stopRes = await fetch(`${BASE}/api/chat/stop`, {
          method: 'POST',
          headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
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
          if (clearedPendingCids === null && Array.isArray(data?.cleared_pending_cids)) {
            clearedPendingCids = data.cleared_pending_cids
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
      // to ignore clearedPendingCids and re-send the full snapshot
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
        // narrowed by clearedPendingCids through the SHARED resolveResend
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
          resolveResend(clearedPendingCids)
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
        resolveResend(clearedPendingCids)

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
  // same queue and both POST a force_steer for the same cids → the second
  // POST's consume_pending_cids no longer matches pending (the first
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
      // Only server-confirmed entries can be force-steered: the backend
      // reconstructs the durable rows from chat.pending_messages, so an
      // optimistic-only entry whose queue-POST hasn't acked yet is not visible
      // there and its cid selects nothing. We take the simpler-correct option:
      // only steer when EVERY queued entry is serverTs-confirmed (usePendingQueue
      // sets that flag on the confirmQueued / hydrate paths). The button gate
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

      // The provider-facing steer text: the non-empty trimmed contents joined
      // by "\n\n", in pending order. The backend no longer byte-matches this
      // against the queue — it selects the durable rows by cid — so the join is
      // just the text delivered into the live turn. consume_pending_cids is
      // every snapshot entry's stable cid (the backend selects pending rows by
      // this set and rebuilds its own rows over them).
      const steerTexts = confirmedSnapshot
        .map(m => (m.content || '').trim())
        .filter(Boolean)
      const content = steerTexts.join('\n\n')
      const consumePendingCids = confirmedSnapshot.map(m => cidOf(m))
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

      let queueAfterOptimisticPromote = null
      function restoreOptimisticSteerQueue() {
        // If another path touched the queue while the POST was in flight
        // (notably the natural turn-end drain), every pendingQueue mutation
        // assigns a fresh array. In that case the other path won the race,
        // so restoring our stale snapshot would resurrect duplicate chips.
        if (
          queueAfterOptimisticPromote !== null
          && pendingQueue.pendingMessagesRef.current === queueAfterOptimisticPromote
        ) {
          pendingQueue.hydrate(confirmedSnapshot, { preserveMissing: true })
        }
      }

      try {
        const steerIsFirstUser = isFirstVisibleUserMessage()
        // Fast-forward is a deliberate visibility action, unlike automatic
        // queue drain. Capture the reader's ACTUAL position now: bottom pins,
        // reading elsewhere holds. A later real scroll during the POST still
        // invalidates this snapshot through the intent version.
        const steerWillPin = shouldPinSend({
          scrollEl: scrollRef.current,
          mode: modeRef.current,
          isFirstUserMsg: steerIsFirstUser,
        })
        steerPinIntentRef.current = makeSendPinIntent(steerWillPin)
        // The queued tray is part of the footer height. If it stays visible
        // until after the steered row is inserted, the scroll system pins with
        // one layout and then immediately reflows when the tray disappears — the
        // visible "down, then up" fast-forward jump. Hide only the confirmed
        // rows this request is steering; restore the snapshot below if the
        // backend says the turn was not steered.
        pendingQueue.promoteManyByCid(consumePendingCids)
        queueAfterOptimisticPromote = pendingQueue.pendingMessagesRef.current
        const result = await streamSend(content, attachments, {
          forceSteer: true,
          consumePendingCids,
          steeredMessages: confirmedSnapshot.map(m => ({
            ts: m.ts,
            cid: cidOf(m),
            content: m.content || '',
            ...(m.attachments ? { attachments: m.attachments } : {}),
          })),
        })
        if (result?.status === 'steered') {
          // The steered rows now render inline (onSteeredIntoTurn promotes
          // them from the SSE event + transcript). Drop them from the local
          // tray. Reconcile against the server's authoritative remaining
          // queue when present, else remove exactly the steered cids.
          if (Array.isArray(result.pending_messages)) {
            pendingQueue.hydrate(result.pending_messages)
          } else {
            for (const c of consumePendingCids) pendingQueue.cancelByCid(c)
          }
          forgetQueuedPinIntent({ cidList: consumePendingCids })
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
  // user was reading. A chat must return to exactly where it was — never to a
  // NEW tail that grew while hidden, even if it had been following before it
  // left. Returning freezes hold; only a later manual bottom gesture can
  // re-enter FOLLOW_BOTTOM. No-op when the turn isn't active or the tab is hidden.
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
      freezeForegroundReturn()
    }

    document.addEventListener('visibilitychange', freezeStreamingReturn)
    window.addEventListener('pageshow', freezeStreamingReturn)
    window.addEventListener('online', freezeStreamingReturn)
    return () => {
      document.removeEventListener('visibilitychange', freezeStreamingReturn)
      window.removeEventListener('pageshow', freezeStreamingReturn)
      window.removeEventListener('online', freezeStreamingReturn)
    }
  }, [freezeForegroundReturn, turnActive])

  // Cloak the first post-reconnect catch-up commit (contract v2 item 2, lever
  // 3). freezeStreamingReturn above already anchors the mode at the moment the
  // tab returns; the atomic catch-up commit lands async AFTER that, and even the
  // in-place reconcile (lever 2c) can re-settle heights. Re-hold the anchor the
  // instant the commit's DOM mutation lands — in a layout effect, before paint,
  // so a real reconnect (Path B) or a Path-A commit after the reveal cap never
  // blinks the reader's position. reapplyActiveMode no-ops before reveal, and a
  // quick-wake kept socket never reconnects (no commit → seq stays put), so a
  // glance at the notification shade cannot trigger it. The seq starts at 0 and
  // only a commit bumps it, so this skips the initial mount.
  useLayoutEffect(() => {
    if (catchUpCommitSeq === 0) return
    setInitialEntryPhase(phase => phase === 'catch-up' ? 'ready' : phase)
    reapplyActiveMode()
  }, [catchUpCommitSeq, reapplyActiveMode])

  // A stale `running` history snapshot can be followed by an authoritative
  // terminal response without a catch-up commit. Release the bounded entry
  // gate instead of waiting for its safety deadline.
  useEffect(() => {
    if (initialEntryPhase === 'catch-up' && !turnActive) {
      setInitialEntryPhase('ready')
    }
  }, [initialEntryPhase, turnActive])

  // Promotion and this sequence update share one React batch, so the terminal
  // pin decision runs after the settled assistant DOM is committed and before
  // paint. This avoids racing a concurrent commit from the stream callback.
  useLayoutEffect(() => {
    if (pinnedSettleSeq === 0) return
    settleStreamingPin()
  }, [pinnedSettleSeq, settleStreamingPin])

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
  // The captured bridge partial enters the active row before catch-up emits a
  // single item. That is the load-bearing part of Lever 1: when SSE becomes the
  // selected source, React updates MsgContent props instead of replacing the
  // DB row with a different renderer subtree.
  const bridgeMsgIdx = turnActive
    ? bridgeHook.findBridgeIndex(messages)
    : -1
  const trailingAssistantPartialIdx = turnActive
    ? findTrailingAssistantPartialIndex(messages)
    : -1
  const hasLiveAssistantPayload = turnActive && streamItems.length > 0
  const bridgeMsg = bridgeMsgIdx >= 0 ? messages[bridgeMsgIdx] : null
  const bridgeFollowedByVisibleUser = bridgeMsgIdx >= 0 && messages
    .slice(bridgeMsgIdx + 1)
    .some(msg => msg?.role === 'user' && !msg.hidden)
  const trailingAssistantPartialMsg = trailingAssistantPartialIdx >= 0
    ? messages[trailingAssistantPartialIdx]
    : null
  // Select DATA, never a component tree. A mount-time bridge is only
  // authoritative while the live payload still proves it is the same answer.
  // A stale in-memory cache can otherwise nominate the completed PREVIOUS
  // answer just as a new turn starts, suppressing that whole reply and showing
  // only the question card that happened to be cached. If the bridge and live
  // surfaces are unrelated, fall through to the real trailing DB partial.
  const bridgeAssistantSurface = chooseActiveAssistantSurface(bridgeMsg, streamItems)
  const trailingAssistantSurface = chooseActiveAssistantSurface(
    trailingAssistantPartialMsg,
    streamItems,
  )
  const activeMirrorMsgIdx = chooseActiveAssistantMirrorIndex({
    bridgeMsgIdx,
    trailingAssistantPartialIdx,
    bridgeFollowedByVisibleUser,
    hasLivePayload: hasLiveAssistantPayload,
    bridgeSurface: bridgeAssistantSurface,
    surface: trailingAssistantSurface,
  })
  const activeMirrorMsg = activeMirrorMsgIdx >= 0 ? messages[activeMirrorMsgIdx] : null
  const activeAssistantSurface = activeMirrorMsgIdx === bridgeMsgIdx
    ? bridgeAssistantSurface
    : (activeMirrorMsgIdx === trailingAssistantPartialIdx
        ? trailingAssistantSurface
        : { hideMessage: false, suppressStream: false })
  const useDbActivePayload = !!(
    activeMirrorMsg
    && (!hasLiveAssistantPayload || activeAssistantSurface.suppressStream)
  )
  const activeAssistantMsg = useDbActivePayload
    ? activeMirrorMsg
    : (hasLiveAssistantPayload
        ? {
            ...(activeMirrorMsg || {}),
            role: 'assistant',
            // Live rendering keeps running tool state and thinking clock
            // anchors; final promotion uses the converter's default finalize
            // mode and still seals running tools as done.
            ...streamItemsToAssistantPayload(streamItems, { finalize: false }),
          }
        : null)
  const showActiveAssistantSurface = !!activeAssistantMsg
  const activeAssistantIsStreaming = !!(activeAssistantMsg && !useDbActivePayload)

  // ── Sticky "needs your answer" affordance ──────────────────────────
  // A pending AskUserQuestion freezes the turn until the user answers,
  // but the card can sit outside the viewport (the user scrolled away,
  // or content streamed in around it) — the chat then just looks hung.
  // Detect a pending card in whichever surface currently renders it:
  // the live stream (a question item without answers) or the durable
  // tail-question invariant on the last visible assistant message (the
  // same rule MsgContent's blockAnswerable enforces; recovery preserves
  // that tail question even when the original process was interrupted).
  const pendingQuestionInStream = activeAssistantIsStreaming
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
  const pendingResumeBlock = tailResumableBlock(messages)
  const hasPendingResume = !!pendingResumeBlock
  const pendingLimitResetAt = pendingResumeBlock?.pause?.resets_at || null
  const autoResumeEnabled = !!chatInfo?.auto_resume_on_limit
  useEffect(() => {
    if (!embedded || !autoResumeEnabled || !pendingLimitResetAt) {
      if (!pendingLimitResetAt) armedEmbeddedResetRef.current = null
      return
    }
    if (armedEmbeddedResetRef.current === pendingLimitResetAt) return
    armedEmbeddedResetRef.current = pendingLimitResetAt
    // Arm the parent protocol once per durable park, before the automatic run
    // exists. If both system events are missed, the stream-open authoritative
    // idle handshake can still complete this new turn exactly once.
    onExternalRunEventRef.current?.('auto_resume_waiting')
  }, [autoResumeEnabled, embedded, pendingLimitResetAt])
  const handleEmbeddedRunEvent = useCallback((event) => {
    if (
      !embedded
      || String(event.chatId || '') !== String(chatId || '')
      || (event.type !== 'chat_run_started'
        && event.type !== 'chat_run_finished')
    ) return
    setEmbeddedRunSignal(previous => (
      advanceChatRunSignal(previous, event.type)
    ))
    setEmbeddedRunActive(event.type === 'chat_run_started')
    onExternalRunEventRef.current?.(event.type)
  }, [chatId, embedded])
  const handleEmbeddedStreamOpen = useCallback(() => {
    setEmbeddedRunSignal(previous => (
      advanceChatRunSignal(previous, 'chat_run_reconcile')
    ))
  }, [])
  // Embedded chats do not have Shell's process stream. Subscribe only while
  // an opted-in limit park is waiting (and through its observed run), rather
  // than holding one permanent SSE connection per retained app iframe.
  useSystemEventStream(handleEmbeddedRunEvent, {
    enabled: !!(
      embedded
      && ((autoResumeEnabled && pendingLimitResetAt) || embeddedRunActive)
    ),
    onOpen: handleEmbeddedStreamOpen,
  })
  const limitResetElapsed = resetDeadlineState(pendingLimitResetAt).elapsed
  const showAutoResumeControl = !!(
    !embedded
    && chatInfo !== null
    && pendingLimitResetAt
    // Once enabled, keep the persistent policy cancellable even if the
    // viewer's clock passes the advertised reset before the server resumes.
    && (!limitResetElapsed || autoResumeEnabled)
  )

  useEffect(() => {
    setAutoResumeError('')
    setAutoResumeErrorSource('')
    let timer = null
    let cancelled = false
    const schedule = () => {
      if (cancelled) return
      const delayMs = resetDeadlineDelay(pendingLimitResetAt)
      if (delayMs === null) return
      timer = setTimeout(() => {
        setLimitResetClockTick(tick => tick + 1)
        // Deadlines beyond the browser timer ceiling need another wait rather
        // than being treated as elapsed at the first capped wake-up.
        schedule()
      }, delayMs)
    }
    schedule()
    return () => {
      cancelled = true
      if (timer !== null) clearTimeout(timer)
    }
  }, [pendingLimitResetAt])

  // Visibility of that card is a pure viewport question — an
  // IntersectionObserver rooted at the scroll container is the signal,
  // no scroll math and no interaction with the spacer machinery. The
  // card's DOM node is stable across streaming ticks (keyed children),
  // so the observer only needs re-binding when the rendering surface
  // can change: pending-flag flips, stream↔messages promotion, or a
  // messages commit. Both nudges share useOffscreenNudge (above).
  // The LAST un-answered card is the pending one: it lives in the last
  // assistant message or the streaming <li>.
  const findPendingQuestionCard = () =>
    [...(scrollRef.current?.querySelectorAll('.qcard:not(.qcard--answered)') ?? [])].pop()
  const pendingCardOffscreen = useOffscreenNudge(
    scrollRef, hasPendingQuestion, findPendingQuestionCard,
    [showActiveAssistantSurface, messages],
  )

  // The resume card: only the tail resumable note renders `.chat__resume`
  // (MsgContent gates the button on isLastMsg), so observing that button is
  // enough to know the card's visibility; a tap on the nudge scrolls it in.
  const findResumeCard = () =>
    [...(scrollRef.current?.querySelectorAll('.chat__resume') ?? [])].pop()
  const resumeCardOffscreen = useOffscreenNudge(
    scrollRef, hasPendingResume, findResumeCard,
    [showActiveAssistantSurface, messages],
  )

  // The ONE active <li> carries this data-key for both DB and live payloads.
  // ANCHOR_AT resolves `[data-key]`, so source selection must never change it.
  // The first committed source owns the key: a DB-first bridge seeds the
  // partial's durable key, while a live-first answer keeps its synthetic key
  // even if a related DB partial arrives later.
  //
  // Fast-forward can insert a user row AFTER the mounted partial while the
  // stream remains live. Therefore bridge identity is ts-based across the
  // full message list, not "last message only." For multi-turn flow (no
  // bridge), the previous assistant is rendered alongside the streaming
  // <li> (different turns), so the streaming <li> gets its own synthetic key
  // rather than reusing a previous assistant row's key.
  const streamingDataKey = chooseActiveAssistantDataKey({
    latched: activeAssistantDataKeyRef.current,
    mirroredMsg: activeMirrorMsg,
    mirrorIndex: activeMirrorMsgIdx,
    hasLivePayload: hasLiveAssistantPayload,
    chatId,
  })
  useLayoutEffect(() => {
    if (!turnActive) {
      activeAssistantDataKeyRef.current = null
      return
    }
    if (showActiveAssistantSurface
        && activeAssistantDataKeyRef.current?.key !== streamingDataKey) {
      activeAssistantDataKeyRef.current = {
        key: streamingDataKey,
        mirrorKey: activeMirrorMsg
          ? (activeMirrorMsg.id
              || `${activeMirrorMsg.role}-${activeMirrorMsg.ts ?? activeMirrorMsgIdx}`)
          : null,
      }
    }
  }, [turnActive, showActiveAssistantSurface, streamingDataKey, activeMirrorMsg, activeMirrorMsgIdx])

  // Polite aria-live status: announced once per state transition, not per
  // token. Visually hidden via the sr-only utility in ChatView.css.
  // When the tail turn paused/parked and needs a Resume tap, announce the
  // recovery state — "Response ready." would be a lie (a paused turn isn't
  // ready, it's waiting on the owner), and a screen-reader user has no visual
  // Resume card to fall back on.
  const resumeStatus = (() => {
    if (!pendingResumeBlock) return null
    if (pendingResumeBlock.pause?.resets_at) {
      const label = formatResetTime(pendingResumeBlock.pause.resets_at)
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
      {!embedded && showInspector && (
        <AgentContextInspector
          chatId={chatId}
          onClose={() => setShowInspector(false)}
        />
      )}
      {!embedded && showSummary && (
        <ChatSummaryViewer
          chatId={chatId}
          onClose={() => setShowSummary(false)}
        />
      )}
      {copyStatus && (
        <div
          className={`chat__copy-toast${copyStatus === 'Copied' ? ' chat__copy-toast--success' : ''}`}
          role="status"
          aria-live="polite"
        >
          {copyStatus === 'Copied' && <Check size={15} strokeWidth={2.5} aria-hidden="true" />}
          {copyStatus}
        </div>
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
        {/* The reservation is a permanent geometry invariant for every
            non-empty chat, including after unmount/remount. Keep the list's
            elastic min-height out of the spacer formula at all times. */}
        <ul className="chat__list" style={{ minHeight: 0 }}>
          {hasMore && (
            <li className="chat__older">
              <button onClick={loadOlderMessages}>Load earlier messages</button>
            </li>
          )}

          {messages.map((msg, i) => {
            if (msg.hidden) return null
            const isLastMsg = i === messages.length - 1
              || messages.slice(i + 1).every(m => m.hidden)
            // The mirrored DB row is rendered below by the SAME active
            // MsgContent instance that consumes live payloads. Suppress only
            // that row; unrelated assistant history remains in this map.
            if (i === activeMirrorMsgIdx
                && msg.role === 'assistant'
                && showActiveAssistantSurface) {
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
            // fires, but the runner keeps waiting). It must NOT gate on the
            // live stream: `isStreaming` flips false on that `done`, which
            // would leave the card disabled forever. `liveQuestionId`, when
            // the live stream handed it to us, is an extra precision filter;
            // after a reload we may never have seen it, and then the
            // tail-unanswered invariant stands on its own.
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
            // User rows key + pin on the stable cid so the optimistic→confirm
            // display-ts update never remounts the row (which would drop the
            // pin target mid-swap). data-ts stays for the timestamp tooltip only.
            const userCid = msg.role === 'user' ? cidOf(msg) : null
            return (
            <li
              key={userCid || msg.id || msg.ts || `${msg.role}-${i}`}
              className={`chat__msg chat__msg--${msg.role}`}
              ref={i === lastUserIdx ? setLastUserMsgRef : null}
              data-key={dataKey}
              data-cid={userCid || undefined}
              data-ts={msg.role === 'user' && msg.ts ? String(msg.ts) : undefined}
              onPointerDown={(event) => handleMessagePointerDown(event, msg, dataKey)}
              onPointerMove={handleMessagePointerMove}
              onPointerUp={cancelMessageHold}
              onPointerCancel={cancelMessageHold}
              onContextMenu={_isTouchPrimary
                ? (event) => {
                    if (event.target?.closest?.('button, a, input, textarea, summary, pre, code')) return
                    event.preventDefault()
                    cancelMessageHold()
                    void copyMessage(msg, dataKey)
                  }
                : undefined}
              onClick={msg.ts && msg.role === 'user'
                ? (event) => showTimestamp(event, dataKey)
                : undefined}
            >
              <MsgContent
                msg={msg}
                chatId={chatId}
                onQuestionAnswer={doSendSilent}
                onResume={doSend}
                autoResumeEnabled={
                  isLastMsg && autoResumeEnabled
                }
                autoResumeAvailable={
                  isLastMsg && showAutoResumeControl
                }
                autoResumeSaving={isLastMsg && autoResumeSaving}
                autoResumeError={
                  isLastMsg && autoResumeErrorSource === 'card'
                    ? autoResumeError
                    : ''
                }
                onAutoResumeChange={
                  isLastMsg ? handleAutoResumeChange : undefined
                }
                submissionBlocked={providerSwitching}
                isLastMsg={isLastMsg}
                liveQuestionId={liveQuestionId}
                suppressedQuestionKeys={streamItemQuestionKeys}
              />
              {msg.ts && msg.role === 'user' && (
                <time className={`chat__ts${visibleTimestampKey === dataKey ? ' chat__ts--visible' : ''}`}>
                  {new Date(msg.ts).toLocaleString([], {
                    month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit',
                  })}
                </time>
              )}
            </li>
          )})}

          {showActiveAssistantSurface && (
            <StreamingMessage
              key={streamingDataKey}
              msg={activeAssistantMsg}
              dataKey={streamingDataKey}
              chatId={chatId}
              onAnswer={doSendSilent}
              onResume={activeAssistantIsStreaming ? undefined : doSend}
              autoResumeEnabled={autoResumeEnabled}
              autoResumeAvailable={showAutoResumeControl}
              autoResumeSaving={autoResumeSaving}
              autoResumeError={
                autoResumeErrorSource === 'card' ? autoResumeError : ''
              }
              onAutoResumeChange={handleAutoResumeChange}
              submissionBlocked={providerSwitching}
              liveQuestionId={liveQuestionId}
              isStreaming={activeAssistantIsStreaming}
            />
          )}

          {turnActive && streamItems.length === 0 && !loading && !showActiveAssistantSurface && (
            <li className="chat__msg chat__msg--assistant">
              <div className="chat__thinking"><span /><span /><span /></div>
            </li>
          )}
        </ul>

        <div className="spacer-dynamic" ref={spacerRef} aria-hidden="true" />
      </div>
      )}

      <div ref={footRef} className="chat__foot">
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
            onClick={revealConversationTail}
          >
            Möbius asked you something — tap to answer
          </button>
        )}
        {hasPendingResume && resumeCardOffscreen && (
          <button
            type="button"
            className="chat__resume-nudge"
            onClick={revealConversationTail}
          >
            {pendingResumeBlock?.pause?.resets_at
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
        <ChatInputBar
          input={input}
          onInputChange={handleComposerInputChange}
          onSubmit={handleSubmit}
          inputRef={inputRef}
          sending={composerBusy}
          listening={listening}
          listeningRef={listeningRef}
          onManualVoiceEdit={acceptManualEdit}
          onToggleVoice={toggleVoice}
          onStop={handleStop}
          onSteer={handleSteer}
          canSteer={canSteer}
          canRequestSteer={canRequestSteer}
          offline={!online}
          sendFailure={sendFailure}
          submissionBlocked={providerSwitching}
          pendingFiles={pendingFiles}
          onAddFiles={handleComposerAddFiles}
          onRemoveFile={handleComposerRemoveFile}
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
                   would skip the cross-provider handoff confirmation:
                   the user could flip Claude ↔ Codex mid-chat without
                   preparing the incoming provider's context. */
                hasAssistantTurns={
                  (chatInfo?.has_assistant_turns ?? false)
                  || messages.some(m => m.role === 'assistant')
                }
                autoResumeEnabled={autoResumeEnabled}
                autoResumeSaving={autoResumeSaving}
                autoResumeError={
                  autoResumeErrorSource === 'settings' ? autoResumeError : ''
                }
                onAutoResumeChange={
                  embedded ? undefined : handleAutoResumeSettingsChange
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
                providerSwitchState={providerSwitchState}
                onOpenInspector={() => setShowInspector(true)}
                onOpenSummary={() => setShowSummary(true)}
                embedded={embedded}
              />
            </>
          }
        />
      </div>
    </div>
  )
}
