import { memo, useCallback, useMemo } from 'react'
import ChatView from '../ChatView/ChatView.jsx'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import { chatRunSignal } from '../../lib/chatRunSignal.js'
import { builtAppsSignature, derivedBuiltApps } from './builtAppState.js'

// Per-chat binding for a tiled pane (design §2, M13). The single-mount ChatView
// in Shell closes every callback over the ONE global `activeChatId`; a second
// mounted ChatView bound to those closures would fire its stream-end, CTA,
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
  apps,
  visible = true,
  paneContentHeight,
  chatRunSignals,
  composerRequest,
  onComposerRequestHandled,
  onSystemEvent,
  markStreamingStart,
  markStreamingEnd,
  markVoiceListening,
  refreshApps,
  acknowledgeAppPreview,
  refreshChats,
  loadTheme,
  navTo,
  onInternalNav,
  onChatMissing,
  onFirstMessage,
  onDisplayReady,
}) {
  // builtApps is derived PER chatId, memoized on the same signature Shell uses
  // for the primary chat — an unrelated app's refetch is a no-op for this pane.
  const builtApps = useMemo(
    () => derivedBuiltApps(apps, chatId),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [builtAppsSignature(apps, chatId)],
  )

  const handleStreamEnd = useCallback(({ continues } = {}) => {
    if (!continues) markStreamingEnd(chatId)
    refreshApps()
    loadTheme()
    refreshChats()
  }, [chatId, markStreamingEnd, refreshApps, loadTheme, refreshChats])

  const handleFirstMessage = useCallback(() => {
    onFirstMessage?.(chatId)
    refreshChats()
  }, [chatId, onFirstMessage, refreshChats])

  const handleMessageStart = useCallback(() => {
    markStreamingStart(chatId)
  }, [chatId, markStreamingStart])

  // Open the app the CTA points at into THIS pane (design §5, finding D-ii), so
  // a background chat's "Open app" lands beside it rather than in the globally
  // focused pane.
  const handleOpenApp = useCallback((app, { final = false } = {}) => {
    navTo('canvas', { appId: app.id, paneId })
    acknowledgeAppPreview?.(app, final)
  }, [navTo, paneId, acknowledgeAppPreview])

  const handleChatMissing = useCallback((missingId) => {
    onChatMissing?.(missingId, chatId)
  }, [chatId, onChatMissing])

  const handleDisplayReady = useCallback((readyChatId) => {
    onDisplayReady?.(paneId, readyChatId)
  }, [onDisplayReady, paneId])

  return (
    <ErrorBoundary key={chatId} variant="inline" label="chat">
      <ChatView
        key={chatId}
        chatId={chatId}
        hidden={!visible}
        paneContentHeight={paneContentHeight}
        externalRunSignal={chatRunSignal(chatRunSignals, chatId)}
        onStreamEnd={handleStreamEnd}
        onFirstMessage={handleFirstMessage}
        onSystemEvent={onSystemEvent}
        onChatMissing={handleChatMissing}
        builtApps={builtApps}
        onOpenApp={handleOpenApp}
        onInternalNav={onInternalNav}
        onMessageStart={handleMessageStart}
        onQuestionAnswered={refreshChats}
        onVoiceListeningChange={markVoiceListening}
        composerRequest={composerRequest}
        onComposerRequestHandled={onComposerRequestHandled}
        onDisplayReady={onDisplayReady ? handleDisplayReady : null}
      />
    </ErrorBoundary>
  )
}

function samePaneChatProps(previous, next) {
  for (const key of Object.keys(previous)) {
    if (key === 'apps') continue
    if (!Object.is(previous[key], next[key])) return false
  }
  for (const key of Object.keys(next)) {
    if (!(key in previous)) return false
  }
  // App-list refetches commonly replace rows unrelated to this chat. Avoid
  // rerendering its large transcript unless its own built-app projection
  // actually changed.
  return builtAppsSignature(previous.apps, previous.chatId)
    === builtAppsSignature(next.apps, next.chatId)
}

export default memo(PaneChatView, samePaneChatProps)
