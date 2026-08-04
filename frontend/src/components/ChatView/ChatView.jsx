import {
  startTransition,
  useState,
  useRef,
  useEffect,
  useLayoutEffect,
  useCallback,
  useMemo,
} from 'react'
import { flushSync } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import Check from 'lucide-react/dist/esm/icons/check.mjs'
import ArrowDown from 'lucide-react/dist/esm/icons/arrow-down.mjs'
import { apiFetch, getAuthHeaders, jsonOrThrow, BASE } from '../../api/client.js'
import { chatMessagesQueryKey } from '../../hooks/queries.js'
import useStreamConnection from './useStreamConnection.js'
import useScrollMode, {
  isNearContentBottom,
  remapSavedReadingAnchor,
  retireSavedReadingPosition,
  savedReadingAnchorHasNestedPart,
  savedReadingAnchorKey,
} from './useScrollMode.js'
import useVoiceInput from './useVoiceInput.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import { getOnlineSnapshot } from '../../lib/connectivityStore.js'
import useSystemEventStream from '../../hooks/useSystemEventStream.js'
import usePendingQueue from './hooks/usePendingQueue.js'
import useBridgePartial from './hooks/useBridgePartial.js'
import useTranscriptState from './hooks/useTranscriptState.js'
import useComposerDraftState from './hooks/useComposerDraftState.js'
import useChatRuntimePolicy from './hooks/useChatRuntimePolicy.js'
import useOffscreenNudge, { useNudgeTargetRef } from './hooks/useOffscreenNudge.js'
import ChatInputBar from './ChatInputBar.jsx'
import { hasSendablePayload } from './composerSubmission.js'
import AgentContextInspector from './AgentContextInspector.jsx'
import ChatSummaryViewer from './ChatSummaryViewer.jsx'
import ComposerPopover from './ComposerPopover.jsx'
import ConnectionStatus from './ConnectionStatus.jsx'
import ProgressRail from './ProgressRail.jsx'
import ActiveAssistantSurface from './ActiveAssistantSurface.jsx'
import QueuedMessages from './QueuedMessages.jsx'
import ContributionReviewCard from './ContributionReviewCard.jsx'
import MsgContent from './MsgContent.jsx'
import MessageMetaRow from './MessageMetaRow.jsx'
import ActivityLineHeader from './ActivityLineHeader.jsx'
import { messageCopyText } from './messageCopy.js'
import { formatResetTime } from './resetTime.js'
import {
  resetDeadlineDelay,
  resetDeadlineState,
} from './autoResumePolicy.js'
import {
  EMPTY_CHAT_RUN_SIGNAL,
  advanceChatRunSignal,
  chatRunSignalDelta,
} from '../../lib/chatRunSignal.js'
import {
  isProviderSwitchBlocking,
} from './providerSwitch.js'
import { questionKey } from './questionKey.js'
import { clearChatQuestionDrafts } from './questionDraft.js'
import { captureLayoutSpace, clientLengthToLayout } from '../../lib/layoutSpace.js'
import { resolveStopResend } from './resolveStopResend.js'
import { focusComposerElement, shouldApplyComposerFocusRequest } from './composerFocusPolicy.js'
import { shouldDismissComposerKeyboardOnSubmit } from './composerKeyboardPolicy.js'
import { updateChatRuntimeCache } from './chatRuntimeCache.js'
import {
  assistantAnchorKey,
  chatCacheEntryState,
  chatDetailCacheValue,
  chatSnapshotMatchesRuntime,
  mergeRecentMessagesIntoLoadedWindow,
  messageKey,
  messageMatchesKey,
  optimisticHandoffWindow,
} from '../../lib/chatDetailCache.js'
import {
  chatSearchRevealFor,
  clearChatSearchReveal,
  consumeChatSearchActivation,
  reconcileChatSearchActivation,
  subscribeChatSearchReveal,
} from '../../lib/chatSearchReveal.js'
import {
  highlightSearchTerms,
} from '../../lib/searchTermHighlight.js'
import { composerHistoryFromMessages } from './composerHistory.js'
import { stopChatSpeech } from './chatSpeechPlayer.js'
import { sendFailureMessage } from './sendFailure.js'
import { assistantStreamCoversMessage, chooseActiveAssistantDataKey, findTrailingAssistantPartialIndex, streamItemsHaveRenderableContent } from './streamPromotion.js'
import {
  commitAssistantPromotion,
  deriveActiveAssistantSelection,
} from './activeAssistantSelection.js'
import {
  answerKeepsCurrentTurn,
  builtAppPulseDecision,
  canFastForwardQueue,
  coldTranscriptRenderFrames,
  continuationRowsFromPromotedMessage,
  isContinuationMessage,
  isOwnerUserMessage,
  jumpToLatestShown,
  openAppCtaViewModel,
  shouldAttachRunningStream,
  shouldRetryStopAfterConfirm,
  stopConfirmedIdle,
  stopRequestSucceeded,
  serverSnapshotBehindLocal,
  shouldFreezeStreamingReturn,
  startedMessagesFromResponse,
  stripInternalUserMessageFields,
  systemEventForChat,
} from './chatRuntimeState.js'
import { cidOf } from './messageIdentity.js'
import {
  cidForSendAttempt,
  sendDraftIdentity,
} from './sendAttemptIdentity.js'
import {
  clearFailedSendAttempt,
  sendAttemptIsDurable,
} from './sendAttemptRecovery.js'
import {
  clearComposerDraft,
  consumeComposerHandoff,
} from './composerDraft.js'
import {
  composerRoom,
  reconcileComposerTextarea,
  resetComposerTextarea,
} from './composerTextareaSizing.js'
import {
  EMPTY_BUILD_PHASE_RAIL,
  accumulateBuildPhase,
  buildPhaseRailViewModel,
  latestBuildPhaseAnnouncement,
  railAtRunStart,
} from './buildPhaseRail.js'
import {
  goalObjectiveAtRunStart,
  goalObjectiveFromRuntime,
  latestGoalObjective,
  progressRailViewModel,
} from './goalProgress.js'
import './ChatView.css'


// Cache touch-primary detection. Updated dynamically if input devices change.
const _touchMql = typeof matchMedia === 'function'
  ? matchMedia('(hover: none) and (pointer: coarse)')
  : null
let _isTouchPrimary = _touchMql?.matches ?? false
_touchMql?.addEventListener('change', (e) => { _isTouchPrimary = e.matches })

const STOP_RETRY_DELAYS_MS = [0, 250, 700, 1200]
const CHAT_FETCH_TIMEOUT_MS = 15000
const MESSAGE_META_VISIBLE_MS = 5000
// The floating jump-to-latest control appears once the reader holds a position
// this far above the CONTENT tail (reserved spacer room is phantom, per the
// send-snapshot bottom rule). Deliberately wider than the 50px near-bottom
// band: settling a line or two up must not summon a control, a real upward
// scroll should.
const JUMP_TO_LATEST_GAP_PX = 200

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function yieldToMainThread() {
  // scheduler.yield() continuations retain enough priority for React to batch
  // every prefix update into one final commit in Chromium. A fresh timer task
  // gives React a real commit/paint/input boundary without imposing a whole
  // animation-frame delay on every hidden slice.
  return new Promise(resolve => setTimeout(resolve, 0))
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
  clearComposerDraft(chatId)
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
  onOpenApp,
  onInternalNav,
  onMessageStart,
  onOwnerActivity,
  onVoiceListeningChange,
  showPicker = true,
  embedded = false,
  guidance = null,
  quickActions = null,
  getContext = null,
  composerRequest = null,
  onComposerRequestHandled = null,
  externalRunSignal = EMPTY_CHAT_RUN_SIGNAL,
  onExternalRunEvent = null,
  // Multi-pane workspace (design §2): when this chat renders inside a tiled
  // pane, Shell passes the pane's projected CONTENT height (pane rect minus the
  // strip). A change means a committed geometry event (divider commit,
  // projection/mode flip, rotation, pane open/close) — forwarded as a signal to
  // the scroll controller's paneResized() below. Null for a single-pane chat (today's
  // behavior — the controller's own ResizeObserver owns resize there).
  paneContentHeight = null,
  // True when this mounted chat is hidden behind the full-workspace Settings
  // overlay (design §2). Before path-unification the single ChatView UNMOUNTED on
  // Settings, which aborted the mic; now it stays mounted, so we must stop voice
  // capture explicitly or the microphone would keep listening off-screen.
  hidden = false,
  // A chat-to-chat handoff keeps the outgoing hidden runtime as the visual
  // cover until its replacement is ready. It must relinquish runtime work
  // without also blanking the already-rendered transcript.
  keepTranscriptPainted = false,
  // Shell chat-to-chat handoff: fired from a layout effect once this mounted
  // chat has a stable frame to paint. Empty/error chats are already stable;
  // transcript chats wait for useScrollMode's existing hide-then-reveal gate.
  onDisplayReady = null,
}) {
  const queryClient = useQueryClient()
  useEffect(() => () => stopChatSpeech({ chatId }), [chatId])
  const hiddenRef = useRef(hidden)
  hiddenRef.current = hidden
  // A drawer search may target a ChatView that is already mounted. Subscribe
  // to its tiny transient intent store so same-chat searches do not require a
  // route/remount to run the anchor reveal.
  const [, setSearchRevealVersion] = useState(0)
  const searchActivationRef = useRef(null)
  searchActivationRef.current = reconcileChatSearchActivation(
    searchActivationRef.current,
    chatId,
    chatSearchRevealFor(chatId),
  )
  useEffect(() => subscribeChatSearchReveal(chatId, () => {
    const next = reconcileChatSearchActivation(
      searchActivationRef.current,
      chatId,
      chatSearchRevealFor(chatId),
    )
    if (next === searchActivationRef.current) return
    searchActivationRef.current = next
    setSearchRevealVersion(version => version + 1)
  }), [chatId])
  const searchReveal = searchActivationRef.current.reveal
  const searchRevealConsumed = searchActivationRef.current.consumedId === searchReveal?.id
  const searchRevealCleanupRef = useRef(() => {})
  useEffect(() => () => searchRevealCleanupRef.current(), [])
  const inputRef = useRef(null)
  const handleInternalNav = useCallback((url) => {
    onInternalNav?.(url)
  }, [onInternalNav])
  const internalNav = onInternalNav ? handleInternalNav : undefined
  // Chat is online-only (it spawns a server-side agent). When offline
  // the composer disables send and says so, rather than failing into a
  // dead stream.
  const online = useOnlineStatus()
  // Read the query cache synchronously on mount. If we've viewed this chat
  // before, its complete transcript window builds the hidden restoration DOM
  // immediately. A complete cache that covers the saved reading coordinate may
  // paint while the version handshake runs; every other cache supplies hidden
  // restoration geometry without becoming freshness authority.
  // If the persister hydrated, useState starts populated; if it races with a
  // cold mount, the authoritative activation read self-heals the miss.
  // The persister itself races with mount on cold load; PersistQuery-
  // ClientProvider's `onSuccess` flushes mid-flight render trees, so
  // for already-warm in-memory caches (same session) this is exact;
  // for IndexedDB-restored caches it's best-effort. The initial fetch
  // useEffect below always fires regardless and writes the fresh data
  // back through the versioned activation handoff, so any miss self-heals on
  // the first authoritative detail read.
  const cached = queryClient.getQueryData(chatMessagesQueryKey(chatId))
  const transcriptCacheKey = useMemo(() => chatMessagesQueryKey(chatId), [chatId])
  const {
    messages,
    messagesRef,
    offset,
    offsetRef,
    applyMessagesToView,
    commitMessages,
  } = useTranscriptState({ cacheKey: transcriptCacheKey, cached, queryClient })
  const messageHistory = useMemo(
    () => composerHistoryFromMessages(messages),
    [messages],
  )
  // A canonical cache is useful restoration geometry, not freshness authority.
  // It may prepare beneath the outgoing cover only when it contains the reader's
  // saved coordinate; an incomplete restoration window stays hidden until the
  // anchor-addressed read repairs or retires that coordinate.
  const initialSavedAnchorKey = savedReadingAnchorKey(chatId)
  const initialActivationAnchorKey = searchReveal?.anchorKey || initialSavedAnchorKey
  const initialCacheEntryState = chatCacheEntryState(
    cached,
    initialActivationAnchorKey,
    !searchReveal && savedReadingAnchorHasNestedPart(chatId),
  )
  const [loading, setLoading] = useState(initialCacheEntryState === 'missing')
  const [initialEntryPhase, setInitialEntryPhase] = useState(
    initialCacheEntryState === 'paintable'
      ? 'cached'
      : initialCacheEntryState === 'validating'
        ? 'cache-validating'
        : initialCacheEntryState,
  )
  // Cached rows are safe restoration geometry, but their persisted liveness can
  // be stale. Do not publish a chat-to-chat handoff until this activation's
  // runtime/detail verdict has arrived: otherwise an apparently idle cache can
  // be promoted, then disappear when the server reports a running turn and the
  // stream catch-up gate closes one frame later.
  const [activationSettled, setActivationSettled] = useState(false)
  const acceptCachedReadingCoordinate = useCallback(() => {
    // The scroll owner has proved the exact nested part against committed DOM.
    setInitialEntryPhase(current => (
      current === 'cache-validating' ? 'cached' : current
    ))
    setLoading(false)
  }, [])
  const acceptInitialStreamCatchUp = useCallback(() => {
    setInitialEntryPhase(current => (
      current === 'stream-catchup' ? 'ready' : current
    ))
  }, [])
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
  // A touch fast-forward dismisses the software keyboard only after the
  // authoritative steer cut has rendered its user row. Closing it at request
  // time resized the chat while that row did not exist yet, so the old tail
  // re-anchored once and the steered row pinned in a second visible movement.
  const steerKeyboardDismissRequestRef = useRef(null)
  const [committedSteerKeyboardDismiss, setCommittedSteerKeyboardDismiss] =
    useState(null)
  // Server-hydrated running marker. `sending` is the local UI flag and
  // `isStreaming` belongs to the SSE hook; both can briefly be false across
  // app/chat remounts or reconnect windows even though the backend still has
  // an active run. Keep the durable server verdict separate so the composer
  // does not fall back to Mic while a turn is still running with queued work.
  const [serverRunning, setServerRunning] = useState(() => !!cached?.running)
  const serverRunningRef = useRef(!!cached?.running)
  function setServerRunningLocalState(v) {
    const running = !!v
    serverRunningRef.current = running
    setServerRunning(running)
  }
  function setServerRunningState(v) {
    const running = !!v
    setServerRunningLocalState(running)
    updateChatRuntimeCache(
      queryClient,
      chatMessagesQueryKey(chatId),
      { running },
    )
  }
  const {
    input,
    inputValueRef,
    setComposerInput,
    sendFailure,
    setSendFailure,
    pendingComposerSubmit,
    setPendingComposerSubmit,
    submittedComposerRequestTokenRef,
    failedSendAttemptRef,
    clearFailedAttempt,
    rememberFailedAttempt,
    pendingFiles,
    clearFiles,
    restoreFiles,
    releaseFiles,
    handleComposerInputChange,
    handleComposerAddFiles,
    handleComposerRemoveFile,
    restoreComposerText,
    restoreDurableDraft,
  } = useComposerDraftState({ chatId, hidden, inputRef })

  const [embeddedRunSignal, setEmbeddedRunSignal] = useState(
    EMPTY_CHAT_RUN_SIGNAL,
  )
  const [embeddedRunActive, setEmbeddedRunActive] = useState(false)
  // A counter is only a render wake-up; deadline elapsed is derived directly
  // from the current card's reset timestamp below, so a newly loaded card can
  // never render using the previous card's boolean state.
  const [, setLimitResetClockTick] = useState(0)
  const armedEmbeddedResetRef = useRef(null)
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
  // hasPendingQuestion / hasPendingResume, alongside the callback refs the
  // cards themselves use to publish the node being observed.
  const [showInspector, setShowInspector] = useState(false)
  const [showSummary, setShowSummary] = useState(false)
  const [visibleMessageMetaKey, setVisibleMessageMetaKey] = useState(null)
  const messageMetaTimerRef = useRef(null)
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
  // The active goal is run-scoped, just like build phases. It is set at every
  // real run-start seam and left intact across mid-turn steers; a fresh mount
  // can recover it from the visible run-start message once liveness is known.
  const [activeGoalObjective, setActiveGoalObjective] = useState(
    () => cached?.running ? (cached?.activeGoalObjective ?? '') : '',
  )
  const setActiveGoalState = useCallback((objective) => {
    setActiveGoalObjective(objective)
    updateChatRuntimeCache(
      queryClient,
      chatMessagesQueryKey(chatId),
      { activeGoalObjective: objective },
    )
  }, [chatId, queryClient])

  useEffect(() => () => {
    if (messageMetaTimerRef.current) clearTimeout(messageMetaTimerRef.current)
  }, [])

  const showMessageMeta = useCallback((event, key) => {
    if (event.defaultPrevented) return
    if (event.target.closest?.('button, a, input, textarea, select, [role="button"]')) return
    if (window.getSelection?.()?.toString()) return
    if (messageMetaTimerRef.current) clearTimeout(messageMetaTimerRef.current)
    setVisibleMessageMetaKey(key)
    messageMetaTimerRef.current = setTimeout(() => {
      messageMetaTimerRef.current = null
      setVisibleMessageMetaKey(current => current === key ? null : current)
    }, MESSAGE_META_VISIBLE_MS)
  }, [])
  useEffect(() => {
    const runtime = queryClient.getQueryData(chatMessagesQueryKey(chatId))
    setActiveGoalObjective(runtime?.running ? (runtime.activeGoalObjective ?? '') : '')
  }, [chatId, queryClient])

  // Pending queue (the items shown in the queued-tray above the
  // composer) lives entirely inside usePendingQueue. Every mutation
  // goes through the hook's named ops; reads use pendingQueue.pendingMessages
  // for render and pendingQueue.pendingMessagesRef for closure-safe
  // synchronous access (handleStop's pre-await clear, fetchMessages'
  // cid preservation).
  const pendingQueue = usePendingQueue(cached?.pending_messages || [])
  // Every delayed visible user row carries one opaque scroll intent under the
  // same stable cid that owns its queue/transcript identity. A queued
  // continuation temporarily moves its rows + intent into one envelope while
  // the previous assistant turn finishes.
  const queuedContinuationRef = useRef(null)
  const sendIntentByCidRef = useRef(new Map())
  const runtimeReconnectInFlightRef = useRef(false)
  const swReloadHoldTimerRef = useRef(null)

  // DOM refs
  const scrollRef = useRef(null)
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
  // The model/effort picker can accept another choice before the prior save
  // settles. Keep its serialized write tail at ChatView scope so both a closed
  // + popover and an immediate Send still observe the same ordering boundary.
  const settingsSaveTailRef = useRef(Promise.resolve())
  // Refs for the absolutely-positioned foot. Its ResizeObserver notifies the
  // scroll controller, which owns publishing composer clearance together with
  // every other indirect scroll-geometry write.
  const chatRef = useRef(null)
  const footRef = useRef(null)
  // `--composer-room` — how much of this chat the reader can actually SEE,
  // read by the composer's growth cap (`.chat__input` max-height).
  //
  // This is deliberately NOT part of the pass above. `--composer-h` is scroll
  // geometry: it feeds the list's bottom padding and has to be sequenced with
  // spacer math, so the scroll controller owns when it runs. The room owns
  // nothing of the sort — it describes the pane and the visible viewport, and
  // an empty chat has both. Publishing it from the scroll controller's pass
  // made it inherit that pass's early return for chats that render no scroll
  // node, so on a NEW chat the var was never set at all, the cap fell back to
  // its `100dvh` default, and — because iOS does not shrink `dvh` for the soft
  // keyboard — the composer could still cover the conversation in the exact
  // flow most likely to hit it. It publishes on its own terms now.
  const composerRoomRef = useRef(0)
  const publishComposerRoom = useCallback(() => {
    const chatEl = chatRef.current
    if (!chatEl) return
    const viewportHeight = clientLengthToLayout(
      window.visualViewport?.height || window.innerHeight,
      captureLayoutSpace(chatEl),
    )
    const room = composerRoom({
      paneHeight: chatEl.clientHeight,
      viewportHeight,
    })
    // Write only on a real change: this also runs from a ResizeObserver on
    // `.chat`, and an unconditional style write there is an easy feedback loop.
    if (room <= 0 || room === composerRoomRef.current) return
    composerRoomRef.current = room
    chatEl.style.setProperty('--composer-room', `${room}px`)
  }, [])
  // One explicit Shell-to-composer handoff owns both New-chat focus and drafts
  // supplied by app navigation. Storage restores unmounted chats; applying the
  // same request here is what updates a retained ChatView without sacrificing
  // its transcript, scroll, or stream identity through a forced remount.
  useEffect(() => {
    const token = composerRequest?.token
    if (token == null) return
    const requestChatId = composerRequest?.chatId
    if (requestChatId == null || String(requestChatId) !== String(chatId)) return

    if (typeof composerRequest.draft === 'string'
        && inputValueRef.current !== composerRequest.draft) {
      handleComposerInputChange(composerRequest.draft)
    }

    if (!shouldApplyComposerFocusRequest({
      focusRequest: composerRequest,
      chatId,
      embedded,
    })) {
      onComposerRequestHandled?.(token)
      return
    }

    let cancelled = false
    const raf = requestAnimationFrame(() => {
      if (cancelled) return
      focusComposerElement(inputRef.current)
      onComposerRequestHandled?.(token)
    })
    return () => {
      cancelled = true
      cancelAnimationFrame(raf)
    }
  }, [chatId, composerRequest, embedded, onComposerRequestHandled])

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
  const onOwnerActivityRef = useRef(onOwnerActivity)
  onOwnerActivityRef.current = onOwnerActivity
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
  //   • gestureWindowUntilRef — read by handleScroll to gate pagination
  //                             on user-driven scrolls only.
  //   • capture/commit/settle send intent
  //                           — the only boundary for submit geometry,
  //                             stale-gesture cancellation, and delayed pins.
  //   • revealed              — apply to .chat__scroll style for the
  //                             hide-then-reveal scroll restore.
  //
  // See useScrollMode.js + ARCHITECTURE.md "Chat scroll + steer
  // contract" for full design.
  // Promotion may publish the durable assistant row through the query cache
  // before React paints clearStreamItems. Remember the exact currently-painted
  // array at that boundary so selection can retire only that surface, never a
  // later continuation that merely happens to lag latestItemsRef by one render.
  const retiredAssistantItemsRef = useRef(null)
  const {
    gestureWindowUntilRef,
    revealed,
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
  } = useScrollMode({
    chatId,
    scrollRef,
    spacerRef,
    lastUserMsgRef,
    chatRef,
    footRef,
    messages,
    messagesRef,
    loadingOlderRef: loadingOlder,
    initialEntryPhase,
    onCachedCoordinateReady: acceptCachedReadingCoordinate,
    ownsReadingPosition: !hidden,
  })

  // Forward committed pane-geometry changes to the scroll controller. A new
  // projected height (divider commit, projection/mode flip, rotation) signals
  // that the committed DOM geometry should re-apply the active mode under the
  // reader gate (design §2). Skipped entirely for single-pane chats
  // (paneContentHeight null) so today's resize behavior is untouched.
  //
  // Must be a layout effect: this is the only automatic scroll write in the
  // controller that would otherwise run after paint. Every other one is
  // pre-paint (syncLayout in a layout effect, the tail follow in a
  // ResizeObserver callback, settleStreamingPin in rAF), and running this one
  // post-paint shows the reader a frame at the old scroll position before the
  // correction lands — visible as a jump when pane geometry changes.
  useLayoutEffect(() => {
    if (paneContentHeight != null) paneResized()
  }, [paneContentHeight, paneResized])

  // A hidden retained owner is not an active runtime. The scroll controller
  // owns the visible -> hidden reading-position handoff; this layer only arms
  // freshness so the surface cannot paint stale history when it returns.
  useLayoutEffect(() => {
    if (!hidden) return
    if (keepTranscriptPainted) return
    // Arm the freshness + restoration gate while this surface is still
    // physically hidden. A retained ChatView must not reappear with the
    // transcript from its previous visible lifetime for even one frame.
    setInitialEntryPhase('history')
    setLoading(true)
  }, [hidden, keepTranscriptPainted])

  function rememberSendIntent(cid, intent) {
    if (!cid || !intent) return
    sendIntentByCidRef.current.set(cid, intent)
  }

  function forgetSendIntent({ cid = null, cidList = null } = {}) {
    if (cid) sendIntentByCidRef.current.delete(cid)
    if (Array.isArray(cidList)) {
      for (const value of cidList) sendIntentByCidRef.current.delete(value)
    }
  }

  function takeSendIntent(cid) {
    if (!cid) return null
    const intent = sendIntentByCidRef.current.get(cid) || null
    sendIntentByCidRef.current.delete(cid)
    return intent
  }

  function restoreReplacedSendIntent(cid, replacement, previous) {
    if (!cid || sendIntentByCidRef.current.get(cid) !== replacement) return
    if (previous) sendIntentByCidRef.current.set(cid, previous)
    else sendIntentByCidRef.current.delete(cid)
  }

  function forgetAllSendIntents() {
    sendIntentByCidRef.current.clear()
    queuedContinuationRef.current = null
  }

  // The first-message exception is shared by every direct/promotion/steer
  // path. Stream promotion can render a user row one React commit before
  // messagesRef catches up, so state alone is not authoritative. A row is
  // first only when both the state mirror and rendered transcript are empty.
  function isFirstVisibleUserMessage() {
    const stateHasUser = messagesRef.current.some(isOwnerUserMessage)
    const domHasUser = !!scrollRef.current?.querySelector('.chat__msg--user')
    return !stateHasUser && !domHasUser
  }

  // Every send/steer/promote lands through one semantic controller event. The
  // intent stays opaque here; the controller alone decides whether a later real
  // reader scroll invalidated it.
  function landSentMessage(cid, { intent, fallbackWillPin = false } = {}) {
    commitSendIntent({ cid, intent, fallbackWillPin })
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
      const res = await apiFetch(
        `/chats/${chatId}?limit=20&compact=1`,
        { timeoutMs: CHAT_FETCH_TIMEOUT_MS },
      )
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
        setServerRunningLocalState(!!data.running)
      }
      const runtimeGoalObjective = goalObjectiveFromRuntime(
        data, latestGoalObjective(msgs),
      )
      setActiveGoalObjective(runtimeGoalObjective)
      setLiveQuestionId(data.pending_question_id || null)
      updateChatRuntimeCache(queryClient, chatMessagesQueryKey(chatId), {
        running: !!data.running,
        activeGoalObjective: runtimeGoalObjective,
        pending_messages: data.pending_messages || [],
        pending_question_id: data.pending_question_id || null,
      })
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
        pendingQuestionId: data.pending_question_id || null,
        pendingLimitResume: !!tailResumableBlock(msgs)?.pause?.resets_at,
      }
    } catch {
      // Network error — silent, user can retry. Callers that need to attach
      // to a newly announced run must distinguish this ambiguous result from
      // an authoritative idle verdict.
      return null
    }
  }, [
    chatId,
    commitMessages,
    pendingQueue.hydrate,
    queryClient,
  ])

  // Active-turn runtime reconciliation. The SSE stream is authoritative for
  // assistant output, but queued-message affordances depend on the durable
  // Chat.running + Chat.pending_messages fields. Mobile backgrounding, an
  // old service-worker client, or a queue POST that acks without canonical
  // pending_message can leave the mounted view showing a stale Stop button
  // until some unrelated local event (like focusing the composer) causes a
  // refresh. While a turn or visible queue exists, poll the small chat state
  // payload and hydrate only runtime fields — do not replace the transcript.
  const reconcileRuntimeState = useCallback(async () => {
    if (hiddenRef.current) return
    const gen = fetchGenRef.current
    try {
      const res = await apiFetch(
        `/chats/${chatId}/runtime`,
        { timeoutMs: CHAT_FETCH_TIMEOUT_MS },
      )
      const data = await jsonOrThrow(res, 'Runtime refresh failed')
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
      // Apply local UI state directly, then publish the complete runtime
      // snapshot once. The side-effecting field setters are for independent
      // optimistic transitions; using them here made one poll emit up to three
      // persisted-cache updates for a single server response.
      setServerRunningLocalState(!!data.running)
      const cachedGoalObjective = queryClient.getQueryData(
        chatMessagesQueryKey(chatId),
      )?.activeGoalObjective
      const runtimeGoalObjective = goalObjectiveFromRuntime(
        data, cachedGoalObjective,
      )
      setActiveGoalObjective(runtimeGoalObjective)
      setLiveQuestionId(data.pending_question_id || null)
      updateChatRuntimeCache(queryClient, chatMessagesQueryKey(chatId), {
        running: !!data.running,
        activeGoalObjective: runtimeGoalObjective,
        pending_messages: serverPending,
        pending_question_id: data.pending_question_id || null,
      })
      // Don't let the fallback poll add/clobber the queue while a turn is live
      // (localAuthoritative, above) — the optimistic queue + confirmQueued
      // own it during a turn; hydrate only when the stream is dead.
      if (!localAuthoritative) {
        pendingQueue.hydrate(serverPending)
      }
    } catch { /* background reconciliation is best-effort */ }
  }, [
    chatId,
    pendingQueue.hydrate,
    queryClient,
  ])

  const handleCompactionStored = useCallback(
    () => fetchMessages({ force: true }),
    [fetchMessages],
  )

  // Provider selection and automatic-resume persistence form one per-chat
  // policy owner. Transcript refresh remains an injected settled outcome.
  const {
    autoResumeEnabled,
    autoResumeError,
    autoResumeErrorSource,
    autoResumeSaving,
    chatInfo,
    clearAutoResumeError,
    handleAutoResumeChange,
    handleAutoResumeSettingsChange,
    handleRestartResumeChange,
    mergeChatInfo,
    providerSwitchState,
    providerSwitching,
    restartResumeEnabled,
    restartResumeError,
    restartResumeSaving,
    setChatInfo,
  } = useChatRuntimePolicy({
    chatId,
    cached,
    hidden,
    onProviderSwitchSettled: handleCompactionStored,
    request: apiFetch,
  })

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
    onCatchUpSettled: acceptInitialStreamCatchUp,
    onConnectionLost: () => {
      // Browser transport ownership is uncertain here: the backend turn may
      // still be parked on a question or producing output. Preserve the
      // last-good assistant payload without retiring any run-owned state;
      // onNeedsRefresh below is the authority for whether the run truly ended.
      promoteStreamToMessages({ keepTurnOpen: true })
    },
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
        const continuation = queuedContinuationRef.current
        queuedContinuationRef.current = null
        const localPromoted = continuation?.rows || null
        const continuationPinIntent = continuation?.intent || null
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
          commitMessages(prev => appendMessageBatch(prev, promotedRows))
          promotedRef.current = false
          landSentMessage(pinCid, {
            intent: continuationPinIntent,
            // A missing delayed intent (for example after remount) degrades to
            // hold; the first-visible-message exception is the only fallback.
            fallbackWillPin: contIsFirstUser,
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
        queuedContinuationRef.current = null
        setSending(false)
        sendingRef.current = false
        setServerRunningState(false)
        setActiveGoalState('')
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
      setActiveGoalState(goalObjectiveAtRunStart(
        message?.content,
        messagesRef.current,
      ))
      const consumedCids = message?._consumed_cids
      const serverRows = Array.isArray(message?._messages)
        ? message._messages.map(stripInternalUserMessageFields).filter(Boolean)
        : null
      const localPromoted = Array.isArray(consumedCids)
        ? pendingQueue.promoteManyByCid(consumedCids)
        : pendingQueue.promoteAll()
      const continuationRows = serverRows?.length ? serverRows : localPromoted
      // The pin intent was stamped at submit under the queued row's cid; the
      // backend echoes those cids back as _consumed_cids (or the promoted row
      // carries the head cid). Look it up by the head cid.
      const pinCid = cidOf(
        (serverRows && serverRows[0])
        || localPromoted
        || (Array.isArray(consumedCids) ? { cid: consumedCids[0] } : null),
      )
      queuedContinuationRef.current = {
        rows: continuationRows,
        intent: takeSendIntent(pinCid),
      }
      forgetSendIntent({ cidList: consumedCids })
    },
    onLiveQuestion: setLiveQuestionId,
    onSteeredIntoTurn: ({ ts, content, messages: steeredBatch }) => {
      // The steer's transcript split has COMMITTED (fired for both providers,
      // including when Stop is pressed with a queued message): the backend has
      // sealed the assistant text streamed up to the split, persisted the user
      // message after it, and reset for the continuation. Mirror that exact
      // shape locally: first promote the current live stream segment into
      // `messages`, then append the steered user row, then let future text
      // deltas build a fresh streaming assistant block.
      //
      // The split is owned by the live provider handle through the sink, so
      // this event always arrives AFTER the last block belonging to the sealed
      // segment. That ordering is what makes promoting the live
      // stream here correct; it is not a guess about where the server cut.
      //
      // It still follows the one visible-row scroll rule. Queue promotion and
      // explicit fast-forward keep the original submit snapshot until a real
      // reader scroll replaces it; tray/footer reflow alone is not intent.
      // Whether it pins or holds, the row gets the same permanent bottom
      // reservation as a normal send.
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
      const pinIntent = takeSendIntent(pinCid)
      promoteStreamToMessages({ keepTurnOpen: true })
      const steeredIsFirstUser = isFirstVisibleUserMessage()
      // Arm the scroll mode BEFORE rendering the steered row. EventSource
      // callbacks are outside React's synthetic event layer, and query-cache
      // listeners can observe the transcript update immediately; setting the
      // mode first prevents a one-frame "row appears low, then snaps up" steer.
      landSentMessage(pinCid, {
        intent: pinIntent,
        // Never infer a delayed pin from the reader's later position.
        fallbackWillPin: steeredIsFirstUser,
      })
      const keyboardDismissRequest = steerKeyboardDismissRequestRef.current
      if (keyboardDismissRequest
          && keyboardDismissRequest.chatId === String(chatId)
          && keyboardDismissRequest.cid === pinCid) {
        steerKeyboardDismissRequestRef.current = null
        // This state update batches with the transcript commit below. The
        // layout effect waits for the exact row, after useScrollMode has
        // applied its pin/hold, before allowing keyboard geometry to change.
        setCommittedSteerKeyboardDismiss(keyboardDismissRequest)
      }
      // Dedup by ts so a reconnect's catch-up replay of the same event
      // can't double-insert the steered user message. Insert by transcript ts
      // instead of blindly appending: if a fetch/replay already committed the
      // post-steer assistant row, the steered user still belongs before it.
      commitMessages(prev => insertMessageBatchByTs(prev, steeredMessages))
      // The rows have now genuinely left `chat.pending_messages`, so retire the
      // tray entries the deferred-cut window kept durably queued but hidden.
      // The cut is the one place that owns the hand-off for both providers.
      for (const msg of steeredMessages) {
        const cid = cidOf(msg)
        if (cid != null) pendingQueue.cancelByCid(cid)
      }
      forgetSendIntent({ cidList: steeredMessages.map(cidOf) })
      // This event is the backend's authoritative transcript commit. Refresh
      // the shell's chat list here so a deferred steer advances drawer recency
      // at the cut, rather than waiting for the entire agent turn to finish.
      onOwnerActivityRef.current?.()
    },
    onSteerDeliveryFailed: ({ consumePendingCids } = {}) => {
      const cids = Array.isArray(consumePendingCids)
        ? consumePendingCids
        : []
      if (cids.includes(steerKeyboardDismissRequestRef.current?.cid)) {
        steerKeyboardDismissRequestRef.current = null
      }
      pendingQueue.releaseSteerReservation(cids)
      forgetSendIntent({ cidList: cids })
      // A direct Cmd/Ctrl+Enter steer has no local tray row. The same
      // authoritative refresh restores it from the durable reserve; existing
      // fast-forward rows simply become actionable again.
      fetchMessages({ force: true })
    },
  })

  // useScrollMode's layout effect is registered before this one. At a steer
  // cut it therefore commits the new row's PIN_USER_MSG/ANCHOR_AT position
  // first; only then may a still-focused, otherwise-unchanged touch composer
  // close its keyboard. A draft edit or focus move during a deferred provider
  // cut is newer owner intent and must not be interrupted by the old tap.
  useLayoutEffect(() => {
    const request = committedSteerKeyboardDismiss
    if (!request) return
    if (request.chatId !== String(chatId)) {
      setCommittedSteerKeyboardDismiss(null)
      return
    }
    const scrollEl = scrollRef.current
    const escapedCid = typeof CSS !== 'undefined' && CSS.escape
      ? CSS.escape(request.cid)
      : request.cid
    const committedRow = scrollEl?.querySelector(
      `.chat__msg--user[data-cid="${escapedCid}"]`,
    )
    if (!committedRow) return

    setCommittedSteerKeyboardDismiss(null)
    const inputEl = inputRef.current
    if (document.activeElement === inputEl
        && inputValueRef.current === request.draft) {
      inputEl.blur()
    }
  }, [chatId, committedSteerKeyboardDismiss, inputValueRef])
  // The composer clears before this boundary, so a slow picker save delays
  // transport without swallowing text entered after Send.
  const sendAfterSettingsSaved = useCallback(async (text, attachments, options) => {
    await settingsSaveTailRef.current
    return streamSend(text, attachments, options)
  }, [streamSend])

  useEffect(() => {
    if (retiredAssistantItemsRef.current !== streamItems) {
      retiredAssistantItemsRef.current = null
    }
  }, [streamItems])

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
    // A retained surface from the other workspace world is layout state, not a
    // second chat runtime. Its visible twin owns fetch/stream reconciliation.
    if (hiddenRef.current) return
    if (externalReconcileInFlightRef.current) return
    externalReconcileInFlightRef.current = true
    try {
      while (
        !hiddenRef.current &&
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
          shouldAttachRunningStream({
            running: running === true || (snapshot === null && delta.started),
            pendingQuestionId: snapshot?.pendingQuestionId,
          })
          && !delta.finished
          && !isStreamingRef.current
        ) {
          await Promise.resolve(connectToStream(true)).catch(() => {})
        }
      }
    } finally {
      externalReconcileInFlightRef.current = false
      if (
        !hiddenRef.current &&
        processedExternalSignalRef.current.seq
        < externalSignalRef.current.seq
      ) {
        queueMicrotask(reconcileExternalActivity)
      }
    }
  }, [connectToStream, embedded, fetchMessages, isStreamingRef])
  useEffect(() => {
    if (hidden) return
    reconcileExternalActivity()
  }, [effectiveRunSignal.seq, hidden, reconcileExternalActivity])

  const ensureRuntimeStreamConnected = useCallback(() => {
    if (hiddenRef.current) return
    if (connectionError === 'disconnected') return
    if (!shouldAttachRunningStream({
      running: serverRunningRef.current,
      pendingQuestionId: liveQuestionId,
    })) return
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
  }, [connectToStream, connectionError, isStreamingRef, liveQuestionId])

  const wasHiddenRef = useRef(hidden)
  useLayoutEffect(() => {
    const becameVisible = wasHiddenRef.current && !hidden
    wasHiddenRef.current = hidden
    if (!becameVisible) return
    // Composer drafts are chat-scoped across workspace worlds. A hidden retained
    // owner does not receive input events, so reconcile from the durable draft at
    // the visibility boundary before its first painted frame.
    restoreDurableDraft()
  }, [
    chatId,
    hidden,
    restoreDurableDraft,
  ])

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

  // Abort voice capture the moment this chat is hidden behind Settings, matching
  // the pre-unification unmount behavior — the mic must never stay hot off-screen.
  useEffect(() => {
    if (hidden && listeningRef.current) stopVoiceRef.current?.()
  }, [hidden, listeningRef])

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
    // commitMessages publishes through the query cache synchronously. Mark the
    // exact painted array before that publish so a render in the narrow
    // publish→clear gap cannot show both the durable row and its retired live
    // surface. This deliberately records streamItems (painted state), not
    // latestItemsRef (which may already contain a newer buffered frame).
    commitAssistantPromotion({
      retiredItemsRef: retiredAssistantItemsRef,
      paintedItems: streamItems,
      promotedItems: items,
      bridgeTs,
      commitMessages,
    })
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

  // Text changes through input, restores, voice, send cleanup, and
  // authoritative foreground reconciliation. Reconcile after every committed
  // value — including the empty value — so no programmatic clear can retain a
  // previous multi-line inline height. Hidden retained panes have no useful
  // scrollHeight; they reconcile when `hidden` flips back to false.
  useLayoutEffect(() => {
    const el = inputRef.current
    if (el && !hidden) reconcileComposerTextarea(el, input)
  }, [chatId, hidden, input])

  // Composer room depends on the pane and visual viewport, not footer
  // geometry. The scroll controller observes the actual footer and scroll
  // viewport so transcript clearance and anchoring share one owner.
  useEffect(() => {
    const reconcileForegroundGeometry = () => {
      // Chromium can restore form/layout state independently when a document
      // returns from background or the back-forward cache. Reconcile the
      // textarea before publishing the restored room.
      reconcileComposerTextarea(inputRef.current, inputValueRef.current)
      publishComposerRoom()
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') reconcileForegroundGeometry()
    }

    publishComposerRoom()
    // A workspace split can resize this pane while the window stands still.
    const paneRo = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(publishComposerRoom)
      : null
    if (chatRef.current) paneRo?.observe(chatRef.current)
    window.addEventListener('resize', publishComposerRoom)
    window.addEventListener('pageshow', reconcileForegroundGeometry)
    window.visualViewport?.addEventListener('resize', publishComposerRoom)
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      paneRo?.disconnect()
      window.removeEventListener('resize', publishComposerRoom)
      window.removeEventListener('pageshow', reconcileForegroundGeometry)
      window.visualViewport?.removeEventListener('resize', publishComposerRoom)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [publishComposerRoom])

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
    // A hidden retained pane is not an active runtime. Returning to visibility
    // changes this dependency and re-runs the version + stream handshake
    // without losing the pane's DOM identity.
    if (hidden) return
    setActivationSettled(false)
    let cancelled = false
    const initialLoadController = new AbortController()
    const queryKey = chatMessagesQueryKey(chatId)
    const activationCache = queryClient.getQueryData(queryKey)
    const savedAnchorKey = savedReadingAnchorKey(chatId)
    const searchAnchorKey = searchReveal?.anchorKey || null
    // A search selection is a deliberate one-shot navigation, so it wins over
    // the stored reader coordinate for this activation. It never remaps or
    // retires that saved position.
    const activationAnchorKey = searchAnchorKey || savedAnchorKey
    const searchActivation = !!searchAnchorKey
    const anchorMatchIn = (snapshot) => {
      if (!activationAnchorKey || !Array.isArray(snapshot?.messages)) return null
      const baseOffset = Number.isInteger(snapshot.offset) ? snapshot.offset : 0
      const localIndex = snapshot.messages.findIndex((message, index) => (
        messageMatchesKey(message, baseOffset + index, activationAnchorKey)
      ))
      if (localIndex < 0) return null
      const messageIndex = baseOffset + localIndex
      return {
        canonicalKey: messageKey(snapshot.messages[localIndex], messageIndex),
        localIndex,
      }
    }
    const remapAnchorMatch = (match) => {
      if (!searchActivation && match?.canonicalKey && match.canonicalKey !== savedAnchorKey) {
        remapSavedReadingAnchor(chatId, savedAnchorKey, match.canonicalKey)
      }
    }
    const activationAnchorMatch = anchorMatchIn(activationCache)
    const cacheCoversSavedAnchor = !activationAnchorKey || !!activationAnchorMatch
    const activationCacheEntryState = chatCacheEntryState(
      activationCache,
      activationAnchorKey,
      !searchActivation && savedReadingAnchorHasNestedPart(chatId),
    )
    remapAnchorMatch(activationAnchorMatch)
    chatIdStaleRef.current = false
    setLoadError(false)
    setLoading(activationCacheEntryState === 'missing')
    const activationEntryPhase = activationCacheEntryState === 'paintable'
      ? 'cached'
      : activationCacheEntryState === 'validating'
        ? 'cache-validating'
        : activationCacheEntryState
    setInitialEntryPhase(current => (
      // Mount-time layout validation can finish before this passive activation
      // effect runs. Do not re-close a cache gate the scroll owner just proved.
      activationEntryPhase === 'cache-validating' && current === 'cached'
        ? current
        : activationEntryPhase
    ))

    const gen = fetchGenRef.current
    const requestJson = async (path, label) => {
      const response = await apiFetch(path, {
        timeoutMs: CHAT_FETCH_TIMEOUT_MS,
        signal: initialLoadController.signal,
      })
      if (response.status === 404) throw new Error('CHAT_NOT_FOUND')
      if (!response.ok) throw new Error(`${label}_${response.status}`)
      return response.json()
    }

    const settleRuntime = (runtime, visibleMessages) => {
      const running = !!runtime.running
      const attachesToStream = shouldAttachRunningStream({
        running,
        pendingQuestionId: runtime.pending_question_id,
      })
      setServerRunningLocalState(running)
      setActiveGoalObjective(goalObjectiveFromRuntime(
        runtime, latestGoalObjective(visibleMessages),
      ))
      hadMessagesRef.current = visibleMessages.length > 0
      setLiveQuestionId(runtime.pending_question_id || null)
      setBridgeMountInputs({
        runningAtMount: running,
        lastMsgAtMount: visibleMessages.length > 0
          ? visibleMessages[visibleMessages.length - 1]
          : null,
      })
      // Persisted rows are not the complete surface of a running turn. Keep
      // the outgoing chat visible until the subscribe-time replay commits.
      setInitialEntryPhase(attachesToStream ? 'stream-catchup' : 'ready')
      setLoading(false)
      setActivationSettled(true)
      pendingQueue.hydrate(runtime.pending_messages || [])
      if (running) {
        setSending(true)
        if (attachesToStream) {
          connectToStream(false)
        }
      } else {
        setSending(false)
        sendingRef.current = false
      }
    }

    const loadActivation = async () => {
      let runtime = null
      let detailCache = null
      let reused = false
      let anchorRetired = false

      if (cacheCoversSavedAnchor && typeof activationCache?.updated_at === 'string') {
        runtime = await requestJson(
          `/chats/${chatId}/runtime`,
          'CHAT_RUNTIME_FAILED',
        )
        // A terminal background refresh can win while this tiny runtime read is
        // in flight. Re-read the cache before accepting reuse so an older
        // captured object can never overwrite the fresher publication.
        const latestCache = queryClient.getQueryData(queryKey)
        const latestAnchorMatch = anchorMatchIn(latestCache)
        const latestCoversSavedAnchor = !activationAnchorKey || !!latestAnchorMatch
        remapAnchorMatch(latestAnchorMatch)
        if (latestCoversSavedAnchor && chatSnapshotMatchesRuntime(latestCache, runtime)) {
          detailCache = latestCache
          reused = true
        }
      }
      if (!reused) {
        const anchorParam = activationAnchorKey
          ? `&anchor=${encodeURIComponent(activationAnchorKey)}`
          : ''
        runtime = await requestJson(
          `/chats/${chatId}?limit=20&compact=1${anchorParam}`,
          'CHAT_LOAD_FAILED',
        )
        const runtimeAnchorMatch = anchorMatchIn(runtime)
        if (activationAnchorKey && runtime.requested_anchor_found === true) {
          if (!runtimeAnchorMatch) {
            throw new Error('CHAT_READING_ANCHOR_NOT_FOUND')
          }
          remapAnchorMatch(runtimeAnchorMatch)
        } else if (activationAnchorKey && runtime.requested_anchor_found === false) {
          // Only the authoritative false + absent-row combination proves that
          // the durable coordinate is gone. A contradictory response is a
          // protocol error; retiring on it would destroy a valid location.
          if (runtimeAnchorMatch) {
            throw new Error('CHAT_READING_ANCHOR_NOT_FOUND')
          }
          if (!searchActivation) {
            retireSavedReadingPosition(chatId)
            anchorRetired = true
          } else {
            clearChatSearchReveal(chatId, searchReveal?.id)
          }
        } else {
          // Rolling servers may omit the coverage bit. A row that is present
          // can still be canonicalized safely; absence without the bit cannot.
          remapAnchorMatch(runtimeAnchorMatch)
        }
        detailCache = chatDetailCacheValue(runtime)
      }

      if (cancelled || fetchGenRef.current !== gen) return
      const msgs = detailCache.messages
      const failedAttempt = failedSendAttemptRef.current
      if (failedAttempt) {
        if (sendAttemptIsDurable(
          failedAttempt,
          msgs,
          runtime.pending_messages,
        )) {
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

      if (reused) {
        // One narrow cache publication updates queue/liveness only. Reconcile
        // the mounted hidden owner from the newest version-matched cache object
        // before readiness so a concurrent terminal refresh wins this race.
        const runtimeGoalObjective = goalObjectiveFromRuntime(
          runtime, latestGoalObjective(msgs),
        )
        updateChatRuntimeCache(queryClient, queryKey, {
          running: !!runtime.running,
          activeGoalObjective: runtimeGoalObjective,
          pending_messages: runtime.pending_messages || [],
          pending_question_id: runtime.pending_question_id || null,
        })
        applyMessagesToView(msgs, detailCache.offset)
        settleRuntime(runtime, msgs)
        return
      }

      // Snapshot provider/model settings before checking for optimistic local
      // rows. Runtime config belongs to this server response even when the
      // mounted transcript is temporarily ahead of it.
      setChatInfo(detailCache.chatInfo)
      if (!anchorRetired && serverSnapshotBehindLocal(msgs, messagesRef.current)) {
        queryClient.setQueryData(queryKey, existing => {
          const handoffWindow = optimisticHandoffWindow(
            existing,
            messagesRef.current,
            offsetRef.current,
          )
          return {
            ...detailCache,
            // The local suffix is not proven by this server version. Preserve
            // the latest cache/mounted owner for the optimistic handoff but
            // make the next activation take the authoritative detail path.
            updated_at: null,
            activeGoalObjective: goalObjectiveFromRuntime(
              runtime, latestGoalObjective(messagesRef.current),
            ),
            ...handoffWindow,
          }
        })
        settleRuntime(runtime, messagesRef.current)
        return
      }

      // Keep an already-loaded older prefix while replacing its overlapping
      // recent page. Publish the complete detail snapshot once, then update the
      // mounted view without a second query-cache notification.
      const refreshed = anchorRetired
        ? {
            messages: msgs,
            offset: detailCache.offset,
          }
        : mergeRecentMessagesIntoLoadedWindow({
            loadedMessages: messagesRef.current,
            loadedOffset: offsetRef.current,
            recentMessages: msgs,
            recentOffset: runtime.offset || 0,
          })
      queryClient.setQueryData(queryKey, {
        ...detailCache,
        activeGoalObjective: goalObjectiveFromRuntime(
          runtime, latestGoalObjective(refreshed.messages),
        ),
        messages: refreshed.messages,
        offset: refreshed.offset,
      })

      // A return with a complete local window is a warm restoration even when
      // the version changed while away. Apply the authoritative replacement
      // and readiness in the same React batch; the cold prefix scheduler must
      // never turn a warm return into a delayed all-at-once burst.
      if (activationCache && cacheCoversSavedAnchor && !anchorRetired) {
        applyMessagesToView(refreshed.messages, refreshed.offset)
        settleRuntime(runtime, refreshed.messages)
        return
      }

      const renderFrames = coldTranscriptRenderFrames(refreshed.messages)
      if (renderFrames.length === 1) {
        // An ordinary cold transcript stays one interruptible commit. Readiness
        // remains in the SAME transition, so the shell cannot reveal a partial
        // transcript; cached activations above remain immediate.
        startTransition(() => {
          applyMessagesToView(refreshed.messages, refreshed.offset)
          settleRuntime(runtime, refreshed.messages)
        })
        return
      }

      // A single agentic turn can contain hundreds of interleaved worklog,
      // activity, and report blocks. React cannot interrupt one DOM commit, so
      // prepare that hidden destination in prefix-complete frame-sized slices.
      // Each await yields a real paint opportunity to the outgoing chat and
      // drawer. Only the final authoritative frame settles loading/readiness,
      // preserving the existing hide-then-reveal scroll contract.
      setInitialEntryPhase('preparing')
      for (const frame of renderFrames) {
        await yieldToMainThread()
        if (cancelled) return
        if (fetchGenRef.current !== gen) {
          // A newer generation owns the runtime now (a fresh send, or Stop
          // clearing the queue), so this fetch must NOT apply its own runtime
          // state — settleRuntime would re-hydrate the queue Stop just
          // cleared. But 'preparing' is a hidden gate that only this path
          // sets, and neither superseding path releases it: returning here
          // without releasing it strands the chat blank until remount.
          setInitialEntryPhase('ready')
          setLoading(false)
          return
        }
        // React may batch state updates across async task yields and discard
        // every intermediate prefix. Commit each hidden slice explicitly; the
        // flush is scoped to this cold, off-screen preparation path only.
        flushSync(() => applyMessagesToView(frame, refreshed.offset))
      }
      settleRuntime(runtime, refreshed.messages)
    }

    loadActivation()
      .catch((err) => {
        if (cancelled) return
        // Offline degradation may use a complete cached restoration window,
        // but never a truncated one that cannot resolve the saved address.
        const cacheIsSafeFallback = activationCache
          && cacheCoversSavedAnchor
          && err?.message !== 'CHAT_READING_ANCHOR_NOT_FOUND'
        if (cacheIsSafeFallback) {
          applyMessagesToView(activationCache.messages, activationCache.offset)
        } else {
          // The mounted state was seeded from cache before validation. Clear an
          // incomplete or server-rejected window before making the error state
          // paintable; otherwise those old rows would leak through this branch.
          applyMessagesToView([], 0)
        }
        setInitialEntryPhase('ready')
        setLoadError(!cacheIsSafeFallback)
        setLoading(false)
        setActivationSettled(true)
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
      initialLoadController.abort()
      chatIdStaleRef.current = true
      loadingOlder.current = false
      disconnect()
    }
  }, [chatId, loadNonce, hidden, searchReveal?.id, searchReveal?.anchorKey])


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
    apiFetch(
      `/chats/${chatId}?limit=20&before=${offset}&compact=1`,
      { timeoutMs: CHAT_FETCH_TIMEOUT_MS },
    )
      .then(r => jsonOrThrow(r, 'Earlier messages failed to load'))
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

  // Jump-to-latest visibility (contract R5a): a pure geometry READ — it never
  // writes scrollTop, so it lives outside the scroll controller's ownership
  // gates. Recomputed from every scroll event (gesture or programmatic) and
  // from every commit via the dependency-less layout effect below: a stream
  // growing beneath a held ANCHOR_AT emits no scroll event, but each growth
  // tick re-renders this component. The guarded setState keeps those
  // recomputes render-free until the boolean actually flips.
  const [awayFromLatest, setAwayFromLatest] = useState(false)
  const updateJumpToLatest = useCallback(() => {
    const el = scrollRef.current
    const away = !!el && !isNearContentBottom(el, JUMP_TO_LATEST_GAP_PX)
    setAwayFromLatest(prev => (prev === away ? prev : away))
  }, [])
  useLayoutEffect(updateJumpToLatest)

  function handleScroll() {
    updateJumpToLatest()
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
  // Modified-Enter is a single durable direct-steer request. Claim it
  // synchronously so repeated keydowns cannot submit a duplicate before the
  // request settles.
  const submitSteerInFlightRef = useRef(false)
  // Queue POSTs are optimistic: the tray row exists before the backend has
  // confirmed it. The primary Steer control is visible immediately, and one
  // early tap waits on these exact requests rather than becoming an inert
  // click or briefly showing Stop.
  const queuedSendRequestsRef = useRef(new Map())

  const doSend = useCallback(async (text, opts = {}) => {
    if (isProviderSwitchBlocking(chatId)) return

    // Callers can pre-supply attachments (e.g. handleStop collapsing
    // a queue that had files attached to queued items). When provided,
    // they replace the pendingFiles-derived list so data isn't lost.
    // Resolve and validate the payload before ANY submit-time side effect:
    // a stale callback or failed-file-only draft must not close the keyboard,
    // claim scroll ownership, or freeze queue geometry when nothing will send.
    const usesComposerFiles = !Array.isArray(opts.attachments)
    const composerFileSnapshot = usesComposerFiles ? [...pendingFiles] : []
    const attachments = Array.isArray(opts.attachments)
      ? opts.attachments
      : pendingFiles
          .filter(f => f.status === 'done')
          .map(f => ({ name: f.name, size: f.size, mime_type: f.mime_type }))
    if (pendingFiles.some(c => c.status === 'uploading')) return
    if (!hasSendablePayload(text, attachments)) return

    const pin = opts.pin !== false  // default true
    const continuation = opts.continuation === 'manual' ? 'manual' : undefined
    setSendFailure(null)

    // Stop voice recognition so a late onresult doesn't refill input
    // after we clear it.
    if (listeningRef.current) stopVoiceRef.current?.()

    // Resolve the ONE direct/queued/steered pin rule BEFORE blurring the
    // textarea. The real-content geometry is authoritative; mode can lag a
    // gesture/layout by a frame. Mobile blur can resize/clamp the viewport, so
    // capture the complete decision before it.
    const isFirstUserMsgAtSubmit = isFirstVisibleUserMessage()
    const sendPinIntent = captureSendIntent({
      canPin: pin,
      isFirstUserMsg: isFirstUserMsgAtSubmit,
    })
    // captureSendIntent atomically snapshots current geometry and supersedes
    // the older gesture that positioned it. Any input begun after this point
    // opens fresh reader ownership and still wins normally.

    const queuesBehindActiveTurn = !!(
      sendingRef.current
      || isStreamingRef.current
      || serverRunningRef.current
      || pendingQueue.pendingMessagesRef.current.length > 0
    )
    const directSteer = opts.directSteer === true && queuesBehindActiveTurn
    if (queuesBehindActiveTurn && !directSteer) {
      // Queueing changes the footer immediately (new chip, cleared composer)
      // but adds no transcript row yet. Freeze the exact visible message
      // before that layout change. The captured submit intent above is kept
      // for the later promotion/steer, while the current in-flight answer
      // stays where the reader left it now.
      freezeQueuedSubmission()
    }

    // Keep the mobile keyboard open for queue-only sends so another follow-up
    // can be typed immediately. Fresh sends and explicit queue+steer submits
    // dismiss it; desktop retains its existing cursor-ready behaviour.
    if (shouldDismissComposerKeyboardOnSubmit({
      isTouchPrimary: _isTouchPrimary,
      queuesBehindActiveTurn,
      directSteer,
    })) {
      inputRef.current?.blur()
    }

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
      // Resume is a product action whose provider-facing prompt never belonged
      // in the composer. A failed request keeps the resumable card in place;
      // restoring the internal word "continue" as a draft would misattribute
      // it to the owner and make a retry look like ordinary prose.
      if (!continuation) {
        restoreComposerText(text, { preserveFailedAttempt: true })
      }
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

    // ACTIVE-TURN PATH: ordinary sends add an optimistic queue row immediately.
    // A direct steer does not: the backend durably reserves and steers this cid
    // in the same request, so pending_messages is an invisible safety reserve.
    // Only a `queued` response exposes that reserve as the fallback tray row.
    //
    // Read from refs (not React state) so doSend stays closure-safe.
    // Callers like handleStop invoke doSend AFTER calling
    // setSending(false) — the captured `sending` state would still
    // be `true` in this render's closure, sending the message to the
    // queue path instead of the fresh-send path. Refs reflect the
    // latest commit and dodge that.
    if (queuesBehindActiveTurn) {
      const queuedMsg = { role: 'user', content: text, ts: Date.now(), cid, queued: true }
      if (continuation) {
        queuedMsg.kind = 'continuation'
        queuedMsg.continuation_reason = continuation
      }
      if (attachments.length > 0) queuedMsg.attachments = attachments
      if (!directSteer) pendingQueue.add(queuedMsg, { inFlight: true })
      // The shared send decision was captured AT SEND TIME, before blur or the
      // POST. If this queued send is promoted into the active turn (the backend
      // returns started, either as `queued+started` or the `started` race),
      // it becomes a new visible user message and must follow the same pin
      // rule as a fresh send. The at-bottom / following decision and the
      // first-user check must reflect the moment of sending — reading them
      // AFTER `await streamSend(...)` lets a scroll during the POST flip the
      // decision. The opaque controller intent detects such a scroll and yields
      // to it (a user-driven scroll after send is the newer intent).
      const queuedPinIntent = sendPinIntent
      rememberSendIntent(cid, queuedPinIntent)
      setComposerInput('')
      clearComposerFilesForSend()
      if (inputRef.current) {
        resetComposerTextarea(inputRef.current)
        // Drop the multi-line `.chat__pill--tall` class so send/mic
        // re-center vertically. Without this, the pill stays in
        // flex-end alignment after a send-from-tall and the freshly
        // empty textarea renders pinned to the bottom — text appears
        // off-center (lower than its resting position) until the
        // user types again. Shared textarea sizing re-evaluates this
        // on each committed value, but the synchronous reset keeps the
        // send transition correct before React commits the empty value.
      }
      let queueRequest = null
      try {
        queueRequest = sendAfterSettingsSaved(
          text,
          attachments.length > 0 ? attachments : undefined,
          directSteer
            ? { directSteer: true, cid, continuation }
            : { queueOnly: true, cid, continuation },
        )
        if (!directSteer) queuedSendRequestsRef.current.set(cid, queueRequest)
        const result = await queueRequest
        clearFailedAttempt()
        releaseComposerFilesAfterAccepted()
        if (result?.status === 'duplicate') {
          // A stale local queue decision can race an already-durable retry.
          // Remove only this send's optimistic tray row; an unrelated live
          // turn may still be streaming and must remain attached.
          if (!directSteer) pendingQueue.cancelByCid(queuedMsg.cid)
          forgetSendIntent({ cid: queuedMsg.cid })
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
          if (
            directSteer
            && !pendingQueue.pendingMessagesRef.current.some(
              row => cidOf(row) === cid
            )
          ) {
            // The one-request steer could not be accepted. Its server-reserved
            // row is now a real queue fallback, so reveal it only at this point.
            pendingQueue.add({
              ...queuedMsg,
              ...(canonicalPending || {}),
              cid,
              ts: canonicalPending?.ts ?? result.ts ?? queuedMsg.ts,
              position: result.position,
              queued: true,
              serverTs: !!canonicalPending,
            })
          }
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
            setActiveGoalState(goalObjectiveAtRunStart(
              text,
              messagesRef.current,
            ))
            setSending(true)
            setServerRunningState(true)
            // The queued send was promoted straight into the active turn, so
            // it's a new visible user message and follows the send rule just
            // like a fresh send. The pin targets the stable cid (the started
            // row carries the same cid the client minted).
            landSentMessage(cid, { intent: queuedPinIntent })
            forgetSendIntent({
              cid,
              cidList: result.message?._consumed_cids,
            })
            bridgeHook.markBridged()
          }
        }
        // Mid-turn steer: the backend delivered the send into the live provider
        // turn. Where the row LIVES right now is what `cut_deferred` states.
        if (result?.status === 'steered') {
          if (directSteer) {
            // No tray row was ever created. The deferred cut will make the
            // message inline when provider delivery settles. Until then the
            // durable server reserve stays intentionally invisible rather
            // than flashing as queued. Keep its cid-keyed intent until that
            // authoritative cut consumes it; response and SSE delivery can
            // arrive in either order.
          } else if (result.cut_deferred) {
            // The transcript split waits for provider acknowledgement, so the
            // row is STILL queued server-side and its tray entry stays.
            // Resolve THIS send's own row and nothing else: confirm it by cid
            // against the server's echoed entry (which carries the durable ts
            // fast-forward needs).
            //
            // Not `hydrate(result.pending_messages)`: that list is a snapshot
            // taken at steer time, and a wholesale reconcile against it is
            // wrong in both directions. It would DROP a row queued and
            // confirmed while this 202 was in flight (absent from the snapshot,
            // no longer in-flight), and it would RESURRECT this row if the
            // runner's cut landed first (the cut retires the tray entry, then
            // the snapshot puts it back while it is already inline). Confirming
            // one cid is a no-op once the cut has retired it, so the two can
            // land in either order.
            const serverRow = Array.isArray(result.pending_messages)
              ? result.pending_messages.find(m => cidOf(m) === queuedMsg.cid)
              : null
            pendingQueue.confirmQueued(queuedMsg.cid, {
              ts: serverRow?.ts ?? queuedMsg.ts,
              position: serverRow?.position,
              serverMsg: serverRow,
            })
            // Its cid-keyed intent remains beside the durable queued row until
            // the cut consumes both, regardless of response/event order.
          } else {
            // Compatibility with an older immediate-cut backend: the row is in
            // the transcript and `steered_into_turn` renders it inline.
            pendingQueue.cancelByCid(queuedMsg.cid)
          }
        }
        if (result?.status === 'queued' || result?.status === 'steered') {
          // The server has accepted deliberate owner activity. In particular,
          // queueing behind an active turn does not emit a new-run event, so
          // the drawer must refresh from this commit boundary instead of
          // remaining stale until the current run ends.
          onOwnerActivityRef.current?.()
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
          setActiveGoalState(goalObjectiveAtRunStart(
            text,
            messagesRef.current,
          ))
          // Apply the shared send-intent rule before appending. A message that
          // raced into a started turn
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
          landSentMessage(cid, { intent: queuedPinIntent })
          forgetSendIntent({
            cid,
            cidList: result.message?._consumed_cids,
          })
          // This is a NEW turn (not the bridge turn from mount).
          // Retire the bridge gate so the upcoming promote appends a
          // fresh assistant instead of replacing whichever message
          // is currently last.
          bridgeHook.markBridged()
        }
        // Invariant: every observable queue-path status must resolve
        // the optimistic entry's in-flight flag. queued/steered/started
        // each clear it above, unconditionally, via confirmQueued or
        // cancelByCid — including BOTH steered branches (confirmQueued when
        // the cut is deferred, cancelByCid when the route already split), so
        // no response shape can slip through leaving the mark set. Any
        // other status — e.g. streamSend's `not_steered` — leaves the
        // entry as an ordinary queued row, so clear the flag here or it
        // leaks forever and a later hydrate would wrongly preserve it.
        if (
          !directSteer
          && result?.status !== 'queued'
          && result?.status !== 'steered'
          && result?.status !== 'started'
        ) {
          pendingQueue.clearInFlight(queuedMsg.cid)
        }
      } catch (err) {
        // Roll back optimistic + restore input.
        if (!directSteer) pendingQueue.cancelByCid(queuedMsg.cid)
        forgetSendIntent({ cid: queuedMsg.cid })
        if (!continuation) {
          rememberFailedAttempt({
            cid,
            draftIdentity,
            text,
            attachments: composerFileSnapshot,
          })
        }
        restoreComposerAfterFailedSend()
        setSendFailure(sendFailureMessage(err, { online: getOnlineSnapshot() }))
      } finally {
        if (!directSteer
            && queuedSendRequestsRef.current.get(cid) === queueRequest) {
          queuedSendRequestsRef.current.delete(cid)
        }
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
    setActiveGoalState(goalObjectiveAtRunStart(text, messagesRef.current))

    // Direct sends use the same submit-time decision as queued/steered sends.
    // A legitimate pin changes FOLLOW_BOTTOM to PIN_USER_MSG, so reply growth
    // stays below the prompt until the user manually scrolls to the bottom.
    // The send-time pin intent, carried across the async POST so a user scroll
    // that lands during it can still win. The pinned row's identity is the
    // minted `cid`, which the optimistic row and the confirmed server row
    // share — so the pin never needs to be retargeted across a ts swap.
    const freshPinIntent = sendPinIntent

    const userMsg = { role: 'user', content: text, ts: Date.now(), cid, optimistic: true }
    if (continuation) {
      userMsg.kind = 'continuation'
      userMsg.continuation_reason = continuation
    }
    if (attachments.length > 0) userMsg.attachments = attachments
    commitMessages(prev => [...prev, userMsg])
    setComposerInput('')
    clearComposerFilesForSend()
    if (inputRef.current) {
      resetComposerTextarea(inputRef.current)
      // Drop the multi-line `.chat__pill--tall` class — see queue-path
      // comment above for the full rationale.
    }
    setSending(true)
    setServerRunningState(true)
    // Pin per the R2 send rule via the funnel: it arms the reservation spacer
    // on every send and, when not pinning, retires any stale PIN to the
    // reader's anchor so their viewport stays fixed. The row carries its final
    // cid from mint, so the pin lands on the first apply.
    landSentMessage(cid, { intent: freshPinIntent })
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
      const result = await sendAfterSettingsSaved(
        sendText,
        attachments.length > 0 ? attachments : undefined,
        // The minted cid rides the POST so the durable row carries the same
        // identity the optimistic row (and its pin) already use — without it
        // the server row derives legacy-<ts> and the strict data-cid pin
        // selector goes blind after the ack re-render.
        { cid, continuation },
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
          landSentMessage(cid, { intent: freshPinIntent })
          if (startedMessages) {
            commitMessages(prev => appendMessageBatch(prev, startedMessages))
          }
          return
        }
        if (!result.started) {
          settleSendIntent({
            intent: freshPinIntent,
            retireFollow: pin,
            event: 'send:not-started-hold',
          })
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
        landSentMessage(cid, { intent: freshPinIntent })
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
      if (!continuation) {
        rememberFailedAttempt({
          cid,
          draftIdentity,
          text,
          attachments: composerFileSnapshot,
        })
      }
      restoreComposerAfterFailedSend()
      // Ambiguity recovery already verified reachability and safely replayed
      // this exact cid once. If even that acknowledgement was lost, keep the
      // same cid with the restored draft so a manual retry can only reconcile
      // the existing server row, never create a duplicate turn.
      commitMessages(prev => {
        const next = [...prev]
        const idx = findUserIndexByCid(next, cid)
        if (idx >= 0) next.splice(idx, 1)
        return next
      })
      setSendFailure(sendFailureMessage(err, { online: getOnlineSnapshot() }))
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
    sendAfterSettingsSaved,
    pendingFiles,
    commitMessages,
    fetchMessages,
    clearFiles,
    restoreFiles,
    releaseFiles,
    online,
    setActiveGoalState,
  ])

  useEffect(() => {
    if (hidden) return
    const request = pendingComposerSubmit
    if (!request || submittedComposerRequestTokenRef.current === request.token) return
    if (loading || loadError) return
    const text = request.text.trim()
    if (!text) {
      setPendingComposerSubmit(null)
      if (!request.storedHandoff) onComposerRequestHandled?.(request.token)
      return
    }
    submittedComposerRequestTokenRef.current = request.token
    setPendingComposerSubmit(null)
    if (request.storedHandoff) {
      // Consume before sending so a failed/reloaded attempt becomes a visible
      // recoverable draft, never an automatic retry loop.
      consumeComposerHandoff(chatId, request.text, { autoSend: true })
    }
    doSend(text)
    if (!request.storedHandoff) onComposerRequestHandled?.(request.token)
  }, [
    pendingComposerSubmit,
    loading,
    loadError,
    doSend,
    chatId,
    hidden,
    onComposerRequestHandled,
  ])

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
    // A question-card answer resumes the SAME assistant row. Freeze the
    // currently visible message and its exact viewport offset synchronously,
    // before QuestionCard's pending state commits or the POST can resume live
    // output. Staying in FOLLOW_BOTTOM here caused the card-to-stream handoff
    // to drag the screen upward after Submit.
    if (resolvedAnswers) freezeQuestionSubmission()
    // Block a simultaneous composer send synchronously, but do not paint the
    // whole chat as a new active turn until the answer POST commits. On a
    // parked/durable question that premature parent transition swaps the
    // history row into the active-assistant surface, remounting QuestionCard
    // while it awaits the request and discarding its local retry error.
    const wasSending = sendingRef.current
    const wasServerRunning = serverRunningRef.current
    sendingRef.current = true
    promotedRef.current = false
    // Hidden answer is a continuation, NOT a new visible send. The
    // user may be reading somewhere else; don't yank them with a PIN.
    // freezeQuestionSubmission above already converted the exact visible
    // position into ANCHOR_AT, so resumed output grows inside the existing
    // assistant row without creating tail-follow intent.
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
      // The transport boundary above is the commit point. Only now advertise
      // the resumed/recovered turn to the shell; successful answer settlement
      // patches the card below in the same React batch, so a source handoff
      // cannot expose an unanswered replacement card.
      onMessageStartRef.current?.()
      setSending(true)
      setServerRunningState(true)
      // The 202 means the answer write committed. Settle the durable and live
      // card sources only now; an optimistic pre-request answer made transient
      // failures look final and erased the retryable per-tab question draft.
      const keepsCurrentTurn = answerKeepsCurrentTurn(response)
      const recoveredRows = keepsCurrentTurn
        ? []
        : (startedMessagesFromResponse(response) || [])
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
          // Recovery commits a hidden continuation before its reply starts.
          // Mirror the backend's canonical rows so the live reply mounts at
          // the same transcript position its durable row will occupy.
          return appendMessageBatch(updated, recoveredRows)
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
      if (!keepsCurrentTurn) {
        bridgeHook.markBridged()
        activeAssistantDataKeyRef.current = null
      }
      // The answer write has committed before the 202 response. Refreshing the
      // owner's chat list now makes this deliberate interaction visible in
      // drawer recency immediately, instead of waiting for the resumed turn to
      // finish and emit its terminal refresh.
      onOwnerActivityRef.current?.()
      if (questionId) setLiveQuestionId(prev => prev === questionId ? null : prev)
      return true
    } catch (err) {
      // Restore the exact pre-submit turn state. In particular, reset the
      // synchronous ref even when React state was already false; otherwise a
      // failed answer silently blocks every later composer send. A question
      // submitted while a live turn is parked keeps that live turn attached.
      // Deliberately keep the reader anchor: the failed card remains the retry
      // target, and an error-label reflow must not recreate live following.
      sendingRef.current = wasSending
      setSending(wasSending)
      setServerRunningState(wasServerRunning)
      if (err.message === 'HTTP 410') {
        // The backend refused this answer because the durable transcript no
        // longer has that open question (for example Stop cancelled it, or a
        // newer question superseded it). Refetch authoritative state rather
        // than keeping the optimistic answer locally.
        setLiveQuestionId(null)
        fetchMessages({ force: true })
        throw err
      }
      // QuestionCard owns this transient failure notice and retains the
      // selection for retry. Never append an assistant-looking error row here:
      // doing so makes the question no longer be the durable tail, disables
      // its card, and used to erase the saved choice during reconnect.
      throw err
    } finally {
      sendSilentInFlightRef.current = false
    }
  }, [streamSend, commitMessages, fetchMessages, freezeQuestionSubmission])

  function handleSubmit(e) {
    e.preventDefault()
    if (isProviderSwitchBlocking(chatId)) return
    doSend(input.trim())
  }

  function handleSubmitSteer(e) {
    e.preventDefault()
    if (isProviderSwitchBlocking(chatId)) return
    if (submitSteerInFlightRef.current) return
    submitSteerInFlightRef.current = true
    void doSend(input.trim(), { directSteer: true })
      .finally(() => { submitSteerInFlightRef.current = false })
  }

  // Cancel one queued message via DELETE. Keep reconciliation scoped to that
  // CID: full queue snapshots can arrive out of order when two rows are
  // cancelled quickly and would otherwise resurrect a sibling cancellation.
  const handleCancelPending = useCallback(async (cid) => {
    const currentQueue = pendingQueue.pendingMessagesRef.current
    const cancelledIndex = currentQueue.findIndex(row => cidOf(row) === cid)
    const cancelledRow = cancelledIndex >= 0 ? currentQueue[cancelledIndex] : null
    pendingQueue.cancelByCid(cid)
    forgetSendIntent({ cid })
    try {
      const res = await apiFetch(`/chats/${chatId}/pending/${encodeURIComponent(cid)}`, {
        method: 'DELETE',
        timeoutMs: CHAT_FETCH_TIMEOUT_MS,
      })
      await jsonOrThrow(res, 'Queued-message cancellation failed')
    } catch {
      // A failed response is ambiguous: the DELETE may still have committed.
      // Read server truth, but restore only this operation's row so an older
      // response cannot overwrite unrelated queue mutations.
      try {
        const res = await apiFetch(
          `/chats/${chatId}/runtime`,
          { timeoutMs: CHAT_FETCH_TIMEOUT_MS },
        )
        const data = await jsonOrThrow(res, 'Queue refresh failed')
        const serverQueue = Array.isArray(data.pending_messages) ? data.pending_messages : []
        const serverIndex = serverQueue.findIndex(row => cidOf(row) === cid)
        if (serverIndex >= 0) {
          const serverRow = serverQueue[serverIndex]
          pendingQueue.restoreByCid({
            ...serverRow,
            cid: cidOf(serverRow),
            queued: true,
            serverTs: true,
          }, serverIndex)
        }
      } catch {
        // Both the mutation and its authoritative read are inconclusive. Put
        // back only this row, preserving any newer queue changes made while
        // the two requests were pending.
        pendingQueue.restoreByCid(cancelledRow, cancelledIndex)
      }
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
      // An in-flight steer POST must settle before Stop snapshots: its
      // outcome determines whether those rows still belong to the queue or
      // have already moved into the transcript. Bounded so a hung POST can't
      // wedge Stop; on timeout we continue with the durable queue snapshot.
      const steerInFlight = steerInFlightRef.current
      if (steerInFlight) {
        await Promise.race([
          steerInFlight.catch(() => {}),
          new Promise(resolve => setTimeout(resolve, 4000)),
        ])
      }
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
      forgetAllSendIntents()
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
          // double-send.
          if (clearedPendingCids === null && Array.isArray(data?.cleared_pending_cids)) {
            clearedPendingCids = data.cleared_pending_cids
          }
        }
        return stopRequestSucceeded({ responseOk: stopRes.ok, data })
      }
      const confirmStopIdle = async () => {
        try {
          const res = await apiFetch(`/chats/${chatId}/runtime`, { timeoutMs: 5000 })
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
        if (hasSendablePayload(resendText, resendAttachments)) {
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
      setActiveGoalState('')
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
      // set → nothing re-sent (the natural-finish-races-Stop double-send),
      // exact match → that subset, partial/legacy → full combined.
      const { text: resendText, attachments: resendAttachments } =
        resolveResend(clearedPendingCids)

      if (hasSendablePayload(resendText, resendAttachments)) {
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
  // The in-flight steer POST as an awaitable, so Stop can serialize behind
  // it. Stop awaiting the steer first sees post-steer truth in both
  // resolutions: steered rows have moved into the transcript, or they remain
  // in the durable queue after a rejected request.
  const steerInFlightRef = useRef(null)
  const [steerBusy, setSteerBusy] = useState(false)

  // STEER (fast-forward): inject the queued messages into the LIVE turn
  // at the next natural boundary, instead of hard-stopping (handleStop)
  // or waiting for turn-end (the default queue drain). Mirrors handleStop's
  // structure — re-entry guard, snapshot-before-await — but never
  // interrupts the running turn. The backend force-steers (bypassing the
  // steer_enabled opt-in) for BOTH providers; the rows render inline when the
  // `steered_into_turn` SSE event reports the transcript split (onSteeredIntoTurn
  // above), and THAT is when they leave the local tray. Codex splits at the
  // route, so the split is already done when the POST resolves; Claude splits at
  // its next content-block boundary, so its 202 comes back `cut_deferred` and
  // the rows stay durably queued—but presentation-reserved—until the cut lands.
  // The shared force-steer core: given serverTs-CONFIRMED queue rows (in
  // queue order), reserve them from further queue actions, POST one
  // force_steer selecting them by cid, and reconcile the accepted cut. The
  // durable rows stay in pendingQueue until the backend commits the transcript
  // cut; only their tray presentation is hidden in that window. A rejected
  // request releases the reservation and shows the unchanged queue again.
  async function steerRows(steerRowsList) {
    // A Stop that has already begun owns the queue's fate (it clears and
    // resends); starting a steer under it would race the teardown.
    if (handlingStopRef.current) return
    const run = steerRowsImpl(steerRowsList)
    steerInFlightRef.current = run
    try {
      await run
    } finally {
      if (steerInFlightRef.current === run) steerInFlightRef.current = null
    }
  }

  async function steerRowsImpl(steerRowsList) {
    const steerTexts = steerRowsList
      .map(m => (m.content || '').trim())
      .filter(Boolean)
    const content = steerTexts.join('\n\n')
    const consumePendingCids = steerRowsList.map(m => cidOf(m))
    // De-dupe attachments by name, exactly like handleStop/resolveStopResend.
    const seenNames = new Set()
    const attachments = []
    for (const m of steerRowsList) {
      for (const a of (m.attachments || [])) {
        if (a && a.name && !seenNames.has(a.name)) {
          seenNames.add(a.name)
          attachments.push(a)
        }
      }
    }
    if (!hasSendablePayload(content, attachments)) return

    const steerCid = consumePendingCids[0] || null
    let explicitSteerIntent = null
    let previousSendIntent = null
    try {
      previousSendIntent = sendIntentByCidRef.current.get(steerCid) || null
      // Preserve queue-time intent through tray reflow. The controller replaces
      // it after real reader movement and retires any older gesture settlement.
      explicitSteerIntent = captureSendIntent({
        isFirstUserMsg: isFirstVisibleUserMessage(),
        previousIntent: previousSendIntent,
      })
      rememberSendIntent(steerCid, explicitSteerIntent)
      // Queue-only sends deliberately retain mobile focus. Remember a touch
      // fast-forward's focus/draft now, but do not blur yet: the authoritative
      // cut must render and pin the steered row before keyboard geometry
      // changes. Both composer and per-row steer actions share this path.
      const inputEl = inputRef.current
      steerKeyboardDismissRequestRef.current = null
      if (_isTouchPrimary && document.activeElement === inputEl) {
        steerKeyboardDismissRequestRef.current = {
          chatId: String(chatId),
          cid: steerCid,
          draft: inputValueRef.current,
        }
      }
      // The queued tray is part of the footer height. Reserve these rows from
      // presentation before the request so the tray closes once, at the
      // deliberate steer action. The records remain in pendingQueue until the
      // authoritative cut, which keeps Stop/reconnect recovery honest while a
      // provider cut is deferred.
      pendingQueue.reserveForSteer(consumePendingCids)
      const result = await streamSend(content, attachments, {
        forceSteer: true,
        consumePendingCids,
        steeredMessages: steerRowsList.map(m => ({
          ts: m.ts,
          cid: cidOf(m),
          content: m.content || '',
          ...(m.attachments ? { attachments: m.attachments } : {}),
        })),
      })
      if (result?.status === 'steered') {
        if (result.cut_deferred) {
          // The handle owns provider settlement. Keep accepted rows reserved
          // (and therefore out of the tray) until onSteeredIntoTurn retires
          // them at the authoritative cut.
        } else if (Array.isArray(result.pending_messages)) {
          // Compatibility with an older immediate-cut backend.
          pendingQueue.hydrate(result.pending_messages)
        } else {
          // A backend that echoes no queue at all: remove exactly the steered
          // cids.
          for (const c of consumePendingCids) pendingQueue.cancelByCid(c)
        }
        // This can expose the row's already-durable enqueue recency
        // immediately. The later steered_into_turn event refreshes again at
        // the authoritative transcript cut, when steer recency itself moves.
        onOwnerActivityRef.current?.()
      }
      if (result?.status !== 'steered') {
        if (steerKeyboardDismissRequestRef.current?.cid === steerCid) {
          steerKeyboardDismissRequestRef.current = null
        }
        restoreReplacedSendIntent(
          steerCid,
          explicitSteerIntent,
          previousSendIntent,
        )
        pendingQueue.releaseSteerReservation(consumePendingCids)
      }
      // not_steered (the turn closed between the gate and the POST) or any
      // other status: release the unchanged queue back to the tray and let it
      // drain at turn-end.
    } catch {
      if (steerKeyboardDismissRequestRef.current?.cid === steerCid) {
        steerKeyboardDismissRequestRef.current = null
      }
      restoreReplacedSendIntent(
        steerCid,
        explicitSteerIntent,
        previousSendIntent,
      )
      pendingQueue.releaseSteerReservation(consumePendingCids)
      // Network/POST error — show the unchanged queue for the turn-end drain.
    }
  }

  // STEER (fast-forward): inject the queued messages into the LIVE turn
  // at the next natural boundary, instead of hard-stopping (handleStop)
  // or waiting for turn-end (the default queue drain). Mirrors handleStop's
  // structure — re-entry guard, snapshot-before-await — but never
  // interrupts the running turn. The backend force-steers (bypassing the
  // steer_enabled opt-in) for BOTH providers; the rows render inline when the
  // `steered_into_turn` SSE event reports the transcript split (onSteeredIntoTurn
  // above), and THAT is when they leave the local tray. Both providers defer
  // the cut until their live handle settles delivery.
  async function handleSteer() {
    if (handlingSteerRef.current) return
    handlingSteerRef.current = true
    setSteerBusy(true)
    try {
      // The UI changes Send → Steer in the same render that adds the
      // optimistic queue row. If the owner taps during its persistence
      // round-trip, wait for those exact writes; doSend's continuation
      // confirms/removes each row before this continuation reads the queue.
      const queueWrites = [...queuedSendRequestsRef.current.values()]
      if (queueWrites.length > 0) await Promise.allSettled(queueWrites)
      const snapshot = pendingQueue.getVisiblePendingMessages()
      // Only server-confirmed entries can be force-steered: the backend
      // reconstructs the durable rows from chat.pending_messages, so an
      // optimistic-only entry whose queue-POST hasn't acked yet is not visible
      // there and its cid selects nothing. We take the simpler-correct option:
      // only steer when EVERY queued entry is serverTs-confirmed (usePendingQueue
      // sets that flag on the confirmQueued / hydrate paths). The awaited writes
      // above should establish that state, so this is belt-and-suspenders — if a
      // rejected or otherwise stray optimistic entry slips in, bail
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
      const confirmedSnapshot = pendingQueue.getVisiblePendingMessages()
      const allServerConfirmed = confirmedSnapshot.length > 0 && confirmedSnapshot.every(
        m => typeof m.ts === 'number' && m.serverTs === true,
      )
      if (!allServerConfirmed) return
      await steerRows(confirmedSnapshot)
    } finally {
      handlingSteerRef.current = false
      setSteerBusy(false)
    }
  }

  // Per-row steer (owner ask, 2026-07-17): the tray's arrow beside a row's
  // cancel-X sends exactly THAT queued message into the live turn, leaving
  // its siblings queued. Same core as the fast-forward button — one cid in
  // consume_pending_cids instead of all of them; the backend already selects
  // pending rows by cid. The arrow renders with the optimistic row. If it is
  // tapped before persistence settles, wait for that row's exact queue write
  // before reading it; the existing reconcile remains the recovery path for
  // a stale mounted client whose local confirmation flag lagged the server.
  async function handleSteerOne(cid) {
    if (handlingSteerRef.current) return
    handlingSteerRef.current = true
    setSteerBusy(true)
    try {
      const queueWrite = queuedSendRequestsRef.current.get(cid)
      if (queueWrite) await Promise.allSettled([queueWrite])
      const findRow = () => (pendingQueue.pendingMessagesRef.current || [])
        .find(m => cidOf(m) === cid)
      let row = findRow()
      if (row && !(typeof row.ts === 'number' && row.serverTs === true)) {
        await reconcileRuntimeState()
        row = findRow()
      }
      if (!row || !(typeof row.ts === 'number' && row.serverTs === true)) return
      await steerRows([row])
    } finally {
      handlingSteerRef.current = false
      setSteerBusy(false)
    }
  }
  // Re-anchor the scroll mode when the tab returns to the foreground
  // (visibilitychange/pageshow/online) while a turn is active, so a
  // backgrounded-then-resumed streaming chat doesn't snap away from where the
  // user was reading. A chat must return to exactly where it was — never to a
  // NEW tail that grew while hidden, even if it had been following before it
  // left. Returning freezes hold; only a later manual bottom gesture can
  // re-enter FOLLOW_BOTTOM. No-op when the turn isn't active or the tab is hidden.
  // (The fast-forward identity/readiness gates are computed separately below.)
  const turnActive = sending || isStreaming || serverRunning
  useEffect(() => {
    if (!turnActive) return
    // Ordinary live turns set this synchronously at their run-start seam. This
    // branch is the cold remount/reconnect recovery path. The query cache
    // retains a known goal across keyed chat switches, including after a
    // steer; latestGoalObjective covers a truly cold attach before this client
    // has seen that run.
    if (!activeGoalObjective) {
      const recovered = latestGoalObjective(messages)
      if (recovered) setActiveGoalState(recovered)
    }
  }, [turnActive, messages, activeGoalObjective, setActiveGoalState])
  useEffect(() => {
    function freezeStreamingReturn(event) {
      if (!shouldFreezeStreamingReturn({
        eventType: event?.type,
        pagePersisted: event?.persisted === true,
        visibilityState: typeof document !== 'undefined'
          ? document.visibilityState
          : 'visible',
        turnActive,
      })) return
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
    reapplyActiveMode()
  }, [catchUpCommitSeq, reapplyActiveMode])

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
  // A connection failure owns the action slot until Retry succeeds. Keep both
  // the visible fast-forward affordance and its keyboard shortcut inert while
  // the stream is unavailable; otherwise the tray disappears but the composer
  // can still offer an action whose request cannot reach the running turn.
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
    if (hidden) return
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
    hidden,
    turnActive,
    pendingQueue.pendingMessages.length,
    reconcileRuntimeState,
  ])

  useEffect(() => {
    if (hidden) return
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
  }, [ensureRuntimeStreamConnected, hidden, reconcileRuntimeState])

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

  // A safe cached window can prepare while its freshness check runs. History
  // and progressive preparation remain hidden; `cached` is granted only after
  // the saved-coordinate coverage check above.
  const transcriptPaintable = (
    initialEntryPhase === 'cached' || initialEntryPhase === 'ready'
  ) && revealed
  const displayReady = activationSettled
    && !loading
    && (transcriptPaintable || showEmpty || showLoadError)

  // The requested server window contains the matching row through the tail.
  // Resolve the result's alias only after validation made the visible transcript
  // paintable. The saved reading coordinate stays untouched until the owner
  // next scrolls or otherwise chooses a new position.
  useLayoutEffect(() => {
    if (!searchReveal || searchRevealConsumed || !displayReady) return
    if (hidden) {
      // More than one physical ChatView can retain this logical chat. A hidden
      // copy must neither reveal nor consume the one intent; the visible copy
      // owns it, and the TTL clears a navigation that never becomes visible.
      return
    }
    const localIndex = messages.findIndex((message, index) => (
      messageMatchesKey(message, offset + index, searchReveal.anchorKey)
    ))
    if (localIndex < 0) return
    const canonicalKey = messageKey(messages[localIndex], offset + localIndex)
    const row = [...(scrollRef.current?.querySelectorAll('.chat__msg[data-key]') || [])]
      .find(element => element.dataset.key === canonicalKey)
    if (!canonicalKey || !row) return

    searchRevealCleanupRef.current()
    row.classList.add('chat__msg--search-reveal')
    const highlight = highlightSearchTerms(row, searchReveal.terms)
    row.focus({ preventScroll: true })
    if (!revealAnchor(canonicalKey, 96, highlight.firstRange)) {
      row.classList.remove('chat__msg--search-reveal')
      highlight.clear()
      return
    }
    const timer = setTimeout(() => {
      row.classList.remove('chat__msg--search-reveal')
      highlight.clear()
    }, 2600)
    searchRevealCleanupRef.current = () => {
      clearTimeout(timer)
      row.classList.remove('chat__msg--search-reveal')
      highlight.clear()
    }
    searchActivationRef.current = consumeChatSearchActivation(
      searchActivationRef.current,
      searchReveal.id,
    )
    clearChatSearchReveal(chatId, searchReveal.id)
  }, [
    chatId,
    displayReady,
    hidden,
    messages,
    offset,
    revealAnchor,
    searchReveal,
    searchRevealConsumed,
  ])

  useLayoutEffect(() => {
    if (displayReady) onDisplayReady?.(chatId)
  }, [chatId, displayReady, onDisplayReady])
  const lastUserIdx = messages.reduce((acc, m, i) => (
    isOwnerUserMessage(m) ? i : acc
  ), -1)
  // The captured bridge partial enters the active row before catch-up emits a
  // single item. Source comparison can walk and normalize the complete live
  // block list several times, so memoize it on transcript/stream ownership:
  // composer input cannot change which assistant source owns this row.
  const {
    activeMirrorMsg,
    activeMirrorMsgIdx,
    bridgeMsgIdx,
    hasLiveAssistantPayload,
    showActiveAssistantSurface,
    trailingAssistantPartialIdx,
    useDbActivePayload,
    activeAssistantIsStreaming,
  } = useMemo(() => deriveActiveAssistantSelection({
    turnActive,
    messages,
    streamItems,
    liveItemsRetired: retiredAssistantItemsRef.current === streamItems,
    findBridgeIndex: bridgeHook.findBridgeIndex,
  }), [
    bridgeMountInputs,
    messages,
    streamItems,
    turnActive,
  ])

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

  // A live question parks Codex's JSON-RPC reader inside request_user_input.
  // turn/steer cannot be acknowledged until that question is released, so a
  // steer button here is a dead end. Keep the existing deterministic Stop path
  // available instead: Stop cancels the question first, interrupts the turn,
  // and re-sends the queued rows as one fresh continuation.
  const showSteer = !hasPendingQuestion
    && connectionError !== 'disconnected'
    && turnActive
    && pendingQueue.visiblePendingMessages.length > 0
  const canRequestSteer = showSteer && !steerBusy
  const canSteer = canRequestSteer
    && canFastForwardQueue(pendingQueue.visiblePendingMessages, turnActive)
  const canSubmitSteer = !hasPendingQuestion
    && connectionError !== 'disconnected'
    && !steerBusy
    && turnActive

  // ── Sticky "tap to resume" affordance ──────────────────────────────
  // A turn paused by a drain-gated restart or a provider-limit park
  // persists a resumable error block at the tail of the last assistant message
  // (the same tail invariant MsgContent's Resume gate enforces). Like a pending
  // question, that card can sit outside the viewport after a scroll — the chat
  // then just looks stopped. Detect the tail resumable block so the offscreen
  // nudge + SR status can name the recovery. A pause is terminal (the turn has
  // ended), so it only ever lives in `messages`, never in a live stream item.
  const pendingResumeBlock = tailResumableBlock(messages)
  const hasPendingResume = !!pendingResumeBlock
  const pendingLimitResetAt = pendingResumeBlock?.pause?.resets_at || null
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
  // an enabled limit park is waiting (and through its observed run), rather
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
    clearAutoResumeError()
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
  }, [clearAutoResumeError, pendingLimitResetAt])

  // Visibility of either card is a pure viewport question — an
  // IntersectionObserver rooted at the scroll container is the signal, no
  // scroll math and no interaction with the spacer machinery. ChatView must
  // NOT look the card up: the pending question moves between two rendering
  // surfaces (the live streaming <li> and the durable message row) at a moment
  // ChatView cannot enumerate, and a lookup taken at bind time then observes a
  // node React has since detached — with the turn parked on the answer nothing
  // re-renders, so the cue would stick forever. Instead the element that IS
  // the card publishes its node through these refs (see useNudgeTargetRef);
  // both surfaces publish through the same channel, so the live→durable
  // handoff reaches the observer as an ordinary node swap.
  const [pendingQuestionEl, pendingQuestionRef] = useNudgeTargetRef()
  const pendingCardOffscreen = useOffscreenNudge(
    scrollRef, hasPendingQuestion, pendingQuestionEl,
  )

  // The resume card publishes the same way, from the TAIL resumable note only
  // — the same block tailResumableBlock arms the cue on. (MsgContent renders a
  // Resume button on every resumable block of the last message, so the tail
  // gate lives at the publication site; one shared ref can only hold one
  // node.) A tap on the nudge scrolls that node in.
  const [resumeCardEl, resumeCardRef] = useNudgeTargetRef()
  const resumeCardOffscreen = useOffscreenNudge(
    scrollRef, hasPendingResume, resumeCardEl,
  )

  // The ONE active <li> carries this data-key for both DB and live payloads.
  // ANCHOR_AT resolves `[data-key]`, so source selection must never change it.
  // The first committed source owns the key: a DB-first bridge seeds the
  // partial's durable key, while a live-first answer keeps its absolute
  // transcript-position alias even if a related DB partial arrives later.
  //
  // Fast-forward can insert a user row AFTER the mounted partial while the
  // stream remains live. Therefore bridge identity is ts-based across the
  // full message list, not "last message only." For multi-turn flow (no
  // bridge), the previous assistant is rendered alongside the streaming
  // <li> (different turns). The live row therefore uses the absolute index its
  // eventual durable row will occupy, not the previous assistant's key.
  const streamingDataKey = chooseActiveAssistantDataKey({
    latched: activeAssistantDataKeyRef.current,
    mirroredMsg: activeMirrorMsg,
    mirrorIndex: activeMirrorMsgIdx,
    hasLivePayload: hasLiveAssistantPayload,
    appendIndex: offset + messages.length,
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
    ? (activeGoalObjective
        ? `Following goal: ${activeGoalObjective}.`
        : 'Assistant is responding…')
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
  // Goal ownership comes from explicit run boundaries and authoritative
  // runtime reconciliation, never a momentary browser transport signal.
  const visibleGoalObjective = activeGoalObjective
  const progressRail = progressRailViewModel(
    visibleGoalObjective,
    buildPhaseRail,
  )
  let lastVisibleMessageIndex = -1
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (!messages[i].hidden) {
      lastVisibleMessageIndex = i
      break
    }
  }

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
      {showEmpty && (
        <div className="chat__empty-wrap">
          {embedded ? (
            // Guidance is the current contextual instruction and therefore
            // takes precedence. Older apps keep their quick-action chips until
            // they deliberately migrate; an app may also choose a bare composer.
            typeof guidance === 'string' && guidance.trim() ? (
              <div className="chat__empty chat__empty--embed chat__empty--guidance">
                <p className="chat__empty-guidance">{guidance}</p>
              </div>
            ) : Array.isArray(quickActions) && quickActions.length > 0 ? (
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
              <img className="chat__empty-glyph" src="/moebius.png" alt="" width="76" height="76" />
              <p className="chat__empty-title">What's on your mind?</p>
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
        style={transcriptPaintable ? undefined : { visibility: 'hidden' }}
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
            const continuationMarker = isContinuationMessage(msg)
            const isLastMsg = i === lastVisibleMessageIndex
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
            const dataKey = messageKey(msg, offset + i)
            const anchorKey = msg.role === 'assistant'
              ? assistantAnchorKey(offset + i)
              : null
            // User rows key + pin on the stable cid so the optimistic→confirm
            // display-ts update never remounts the row (which would drop the
            // pin target mid-swap). data-ts stays for the revealed metadata row.
            const ownerUserMessage = isOwnerUserMessage(msg)
            const userCid = ownerUserMessage ? cidOf(msg) : null
            const copyText = messageCopyText(msg)
            const hasMessageMeta = Boolean(copyText || (ownerUserMessage && msg.ts))
            return (
            <li
              key={userCid || msg.id || msg.ts || `${msg.role}-${i}`}
              className={`chat__msg chat__msg--${continuationMarker ? 'marker' : msg.role}`}
              tabIndex={-1}
              ref={i === lastUserIdx ? setLastUserMsgRef : null}
              data-key={dataKey}
              data-anchor-key={anchorKey === dataKey ? undefined : anchorKey}
              data-cid={userCid || undefined}
              data-ts={ownerUserMessage && msg.ts ? String(msg.ts) : undefined}
              onClick={hasMessageMeta
                ? (event) => showMessageMeta(event, dataKey)
                : undefined}
            >
              <MsgContent
                msg={msg}
                chatId={chatId}
                messageKey={dataKey}
                onQuestionAnswer={doSendSilent}
                onResume={doSend}
                onInternalNav={internalNav}
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
                pendingQuestionRef={pendingQuestionRef}
                resumeCardRef={resumeCardRef}
              />
              <MessageMetaRow
                timestamp={ownerUserMessage ? msg.ts : null}
                copyText={copyText}
                speechText={msg.role === 'assistant' ? copyText : ''}
                speechKey={dataKey}
                speechChatId={chatId}
                visible={visibleMessageMetaKey === dataKey}
              />
            </li>
          )})}

          {showActiveAssistantSurface && (
            <ActiveAssistantSurface
              key={streamingDataKey}
              activeMirrorMsg={activeMirrorMsg}
              useDbActivePayload={useDbActivePayload}
              hasLivePayload={hasLiveAssistantPayload}
              streamItems={streamItems}
              dataKey={streamingDataKey}
              chatId={chatId}
              onAnswer={doSendSilent}
              onResume={activeAssistantIsStreaming ? undefined : doSend}
              onInternalNav={internalNav}
              autoResumeEnabled={autoResumeEnabled}
              autoResumeAvailable={showAutoResumeControl}
              autoResumeSaving={autoResumeSaving}
              autoResumeError={
                autoResumeErrorSource === 'card' ? autoResumeError : ''
              }
              onAutoResumeChange={handleAutoResumeChange}
              submissionBlocked={providerSwitching}
              liveQuestionId={liveQuestionId}
              // Same publication channel as the durable rows above: while the
              // turn is live THIS surface owns the pending question card, so
              // the offscreen observer follows the handoff automatically.
              pendingQuestionRef={pendingQuestionRef}
              resumeCardRef={resumeCardRef}
              // Liveness for the ACTIVE surface follows the TURN, not the
              // payload source: when a richer DB partial wins source selection
              // (useDbActivePayload, e.g. through the reconnect catch-up
              // window) the turn is still running, and its trailing activity
              // must keep the in-progress face — shimmer, progressive tense,
              // ", in progress" — instead of settling early. Source selection
              // still gates resume/question routing above (review 2026-07-17).
              isStreaming={activeAssistantIsStreaming || turnActive}
              messageMetaVisible={visibleMessageMetaKey === streamingDataKey}
              onMessageMetaClick={showMessageMeta}
            />
          )}

          {turnActive && streamItems.length === 0 && !loading && !showActiveAssistantSurface && (
            <li className="chat__msg chat__msg--assistant">
              {/* The placeholder occupies the same wrapper and shared header as
                  the first real activity stretch, so event promotion cannot
                  change its alignment. The hook remains a presence probe only. */}
              <div className="chat__tools chat__thinking">
                <div className="chat__activity chat__activity--running">
                  <ActivityLineHeader
                    text="Thinking"
                    displayState="running"
                    iconKind="reasoning"
                    ariaLabel="Thinking, in progress"
                    reserveInteractiveGeometry
                  />
                </div>
              </div>
            </li>
          )}
        </ul>

        <div className="spacer-dynamic" ref={spacerRef} aria-hidden="true" />
      </div>
      )}

      <div ref={footRef} className="chat__foot">
        {/* Foot order, top to bottom:
            attention actions → build-progress rail → connection/retry → queued
            messages → composer. The shell owns the one persistent offline
            explanation; the composer retains contextual send-failure copy. */}
        {/* A LOST connection hides transient actions: while the terminal
            'disconnected' state is set, nudges hide so the one thing on screen
            is the problem and its Retry
            (owner ask, 2026-07-17). ONLY 'disconnected' gates: 'retrying'
            is a transient bare-EOF auto-reconnect that clears itself in
            ~300ms — blanking and popping the whole stack on every mobile
            blip would be flicker, not signal (review 2026-07-17; the
            reconnect effect keys on the same distinction). The healthy
            sleep/wake reattach note (`reconnecting`) likewise hides
            nothing — the stream is being replaced, not failing. */}
        {connectionError !== 'disconnected' && (
          <>
          {openAppCtas.length > 0 && (
            <div className="chat__open-app">
              {openAppCtas.map(({ app, vm }) => {
                const pulsing = pulsedAppId === Number(app.id)
                return (
                  <button
                    key={app.id}
                    className={`chat__open-app-btn${pulsing ? ' chat__open-app-btn--pulse' : ''}`}
                    aria-label={pulsing ? `Preview updated for ${app.name || 'app'}` : vm.ariaLabel}
                    onClick={() => onOpenApp?.(app, { final: !turnActive })}
                  >
                    {pulsing ? 'Preview updated ✓' : `${vm.label} →`}
                  </button>
                )
              })}
            </div>
          )}
          <div className="chat__offscreen-nudges">
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
            {/* Jump-to-latest: same one-shot tail navigation as the nudges
                (contract R5a — lands as a settled hold, never live-follow),
                shown once the reader has scrolled away from the end. Yields
                to a visible nudge, which goes to the same place with more
                context. */}
            {jumpToLatestShown({
              awayFromTail: awayFromLatest,
              questionNudgeShown: hasPendingQuestion && pendingCardOffscreen,
              resumeNudgeShown: hasPendingResume && resumeCardOffscreen,
            }) && (
              <button
                type="button"
                className="chat__jump-latest"
                aria-label="Jump to the latest message"
                title="Jump to latest"
                onClick={revealConversationTail}
              >
                <ArrowDown size={18} strokeWidth={2.25} aria-hidden="true" />
              </button>
            )}
          </div>
          </>
        )}
        <ProgressRail
          items={progressRail}
          ariaLabel={visibleGoalObjective ? 'Goal progress' : 'Build progress'}
        />
        {/* Contribution staged from THIS chat: approve it where the work
            happened. Renders nothing unless something is actually waiting.
            Owner-shell only: an app-embedded chat runs on a capability token
            that is deliberately scoped to one chat, so it can neither list apps
            nor take a public GitHub action, and there is no owner surface there
            to approve one. */}
        {!embedded && (
          <ContributionReviewCard
            chatId={chatId}
            turnActive={turnActive}
            onOpenApp={onOpenApp}
          />
        )}
        <ConnectionStatus
          error={connectionError}
          reconnecting={reconnecting}
          onRetry={retry}
        />
        {connectionError !== 'disconnected' && (
          <QueuedMessages
            items={pendingQueue.visiblePendingMessages}
            onCancel={handleCancelPending}
            onSteerOne={handleSteerOne}
            steerActive={turnActive && !hasPendingQuestion}
            steerBusy={steerBusy}
          />
        )}
        <ChatInputBar
          chatId={chatId}
          input={input}
          onInputChange={handleComposerInputChange}
          onInputIntent={composerEdited}
          onSubmit={handleSubmit}
          onSubmitSteer={handleSubmitSteer}
          inputRef={inputRef}
          sending={composerBusy}
          listening={listening}
          listeningRef={listeningRef}
          onManualVoiceEdit={acceptManualEdit}
          onToggleVoice={toggleVoice}
          onStop={handleStop}
          onSteer={handleSteer}
          canSteer={canSteer}
          showSteer={showSteer}
          steerReady={!steerBusy}
          canRequestSteer={canRequestSteer}
          canSubmitSteer={canSubmitSteer}
          offline={!online}
          sendFailure={sendFailure}
          submissionBlocked={providerSwitching}
          pendingFiles={pendingFiles}
          onAddFiles={handleComposerAddFiles}
          onRemoveFile={handleComposerRemoveFile}
          attachTriggerRef={attachTriggerRef}
          messageHistory={messageHistory}
          provider={chatInfo?.provider}
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
                restartResumeEnabled={restartResumeEnabled}
                restartResumeSaving={restartResumeSaving}
                restartResumeError={restartResumeError}
                onRestartResumeChange={
                  embedded ? undefined : handleRestartResumeChange
                }
                onChangeChatInfo={mergeChatInfo}
                providerSwitchState={providerSwitchState}
                settingsSaveTailRef={settingsSaveTailRef}
                composerInputRef={inputRef}
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
