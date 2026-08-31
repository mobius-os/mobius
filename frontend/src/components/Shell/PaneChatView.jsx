import { memo, useCallback, useEffect, useMemo, useRef } from 'react'
import ChatView from '../ChatView/ChatView.jsx'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import { chatAppArtifactQueries } from '../../hooks/queries.js'
import { projectChatAppArtifacts } from './chatAppArtifactState.js'
import { samePaneChatProps } from './paneChatProps.js'
import { scheduleAfterBrowserPaint } from './scheduleAfterBrowserPaint.js'

// Per-chat binding for a tiled pane (design §2, M13). The single-mount ChatView
// in Shell closes every callback over the ONE global `activeChatId`; a second
// mounted ChatView bound to those closures would fire its stream-end, artifact,
// attention, and repair logic against the wrong chat. This wrapper parameterizes
// every such callback by its OWN chatId so each visible chat pane is self-bound.
//
// Rendered as a chatId-sorted flat sibling list in Shell (same stable-order rule
// as the app iframes): a cross-pane move or divider drag changes only the
// wrapper's rect, never its DOM position, so the ChatView never remounts and its
// stream + scroll survive. The wrapper `<div>` (rect, visibility, data-tab-key)
// lives in Shell; this component is only the ChatView + its error boundary.
//
// The FOCUSED pane's chatId equals today's activeChatId, so its wiring is
// byte-identical to the single-mount path — it simply arrives via chatId instead
// of the global. paneContentHeight forwards committed pane-geometry to the
// scroll controller (design §2, constraint 1).
function PaneChatView({
  chatId,
  paneId,
  artifactsAppId = null,
  runtimeActive = true,
  keepTranscriptPainted = false,
  focusedPresentation = false,
  paneContentHeight,
  // Shell selects this chat's stable signal before React.memo compares props.
  // An unrelated chat can replace the global signal Map without crossing this
  // pane's render boundary.
  externalRunSignal,
  composerRequest,
  onComposerRequestHandled,
  onSystemEvent,
  markStreamingStart,
  markStreamingEnd,
  refreshApps,
  refreshChats,
  markChatOwnerActivity,
  loadTheme,
  openAppWithIntent,
  onInternalNav,
  onChatMissing,
  onFirstMessage,
  onDisplayReady,
  onChatBoundaryError,
}) {
  const appArtifactsQuery = chatAppArtifactQueries.detail.useQuery(chatId)
  const builtApps = useMemo(
    () => projectChatAppArtifacts(appArtifactsQuery.data),
    [appArtifactsQuery.data],
  )

  const handleStreamEnd = useCallback(({ continues } = {}) => {
    if (!continues) markStreamingEnd(chatId)
    // Every idle chat probes its broadcast once on activation and receives a
    // terminal 204. That is not a completed turn and must not turn ordinary
    // chat switching into an unrelated full apps/theme reconciliation. A real
    // `done` event always carries the boolean continuation fact; keep the
    // refresh only as a fallback for that committed-turn boundary (live app /
    // theme events normally update their own projections first).
    if (continues !== undefined) {
      refreshApps()
      loadTheme()
    }
  }, [chatId, markStreamingEnd, refreshApps, loadTheme])

  const handleFirstMessage = useCallback(() => {
    onFirstMessage?.(chatId)
    markChatOwnerActivity(chatId)
    // The server commits the fallback title with the first message. This one
    // fresh read restores that exact row (New chat -> message preview) without
    // bringing back the per-run start/finish refetches removed for performance.
    refreshChats()
  }, [chatId, markChatOwnerActivity, onFirstMessage, refreshChats])

  const handleOwnerActivity = useCallback(() => {
    markChatOwnerActivity(chatId)
  }, [chatId, markChatOwnerActivity])

  const handleMessageStart = useCallback(() => {
    markStreamingStart(chatId)
  }, [chatId, markStreamingStart])

  // Artifact opens stay pane-local, so a background chat never steals the
  // globally focused pane.
  const handleOpenApp = useCallback((app, { intent = '' } = {}) => {
    const target = app?.id ?? app?.slug ?? app
    void openAppWithIntent(target, intent, () => true, { paneId })
  }, [openAppWithIntent, paneId])

  const handleOpenArtifact = useCallback((artifactId) => {
    if (artifactsAppId == null || artifactId == null) return
    void openAppWithIntent(
      artifactsAppId,
      `artifact:${artifactId}`,
      () => true,
      { paneId },
    )
  }, [artifactsAppId, openAppWithIntent, paneId])

  const handleChatMissing = useCallback((missingId) => {
    onChatMissing?.(missingId, chatId)
  }, [chatId, onChatMissing])

  const displayReadyCancelRef = useRef(() => {})
  const handleDisplayReady = useCallback((readyChatId) => {
    displayReadyCancelRef.current()

    // ChatView reports layout readiness before the transcript's first paint.
    // Prepare that frame beneath the outgoing cover before promotion.
    displayReadyCancelRef.current = scheduleAfterBrowserPaint(
      () => onDisplayReady(paneId, readyChatId, focusedPresentation),
    )
  }, [focusedPresentation, onDisplayReady, paneId])

  useEffect(() => () => displayReadyCancelRef.current(), [])

  return (
    <ErrorBoundary
      key={chatId}
      variant="inline"
      label="chat"
      recoveryKey={`chat:${chatId}`}
      onError={onChatBoundaryError}
    >
      <ChatView
        key={chatId}
        chatId={chatId}
        hidden={!runtimeActive}
        keepTranscriptPainted={keepTranscriptPainted}
        paneContentHeight={paneContentHeight}
        externalRunSignal={externalRunSignal}
        onStreamEnd={handleStreamEnd}
        onFirstMessage={handleFirstMessage}
        onSystemEvent={onSystemEvent}
        onChatMissing={handleChatMissing}
        builtApps={builtApps}
        builtAppsReady={appArtifactsQuery.isSuccess}
        onOpenApp={handleOpenApp}
        artifactsAppId={artifactsAppId}
        onOpenArtifact={handleOpenArtifact}
        onInternalNav={onInternalNav}
        onMessageStart={handleMessageStart}
        onOwnerActivity={handleOwnerActivity}
        composerRequest={composerRequest}
        onComposerRequestHandled={onComposerRequestHandled}
        onDisplayReady={onDisplayReady ? handleDisplayReady : null}
      />
    </ErrorBoundary>
  )
}

export default memo(PaneChatView, samePaneChatProps)
