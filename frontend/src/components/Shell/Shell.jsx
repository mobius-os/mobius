import { lazy, Suspense, useState, useEffect, useLayoutEffect, useCallback, useMemo, useReducer, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { CollapseSm } from '@openai/apps-sdk-ui/components/Icon'
import {
  AppsNavIcon,
  NewChatNavIcon,
  SettingsNavIcon,
} from '../navigationIcons.js'
import Drawer from '../Drawer/Drawer.jsx'
import Toast from '../ui/Toast.jsx'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import WalkthroughOverlay from '../Walkthrough/WalkthroughOverlay.jsx'
import NotificationCenter from '../NotificationBell/NotificationCenter.jsx'
import {
  api, apiFetch, jsonOrThrow, probeDeletion, clearAppRuntimeData,
  invalidateShellListCache,
} from '../../api/client.js'
import usePushSubscription from '../../hooks/usePushSubscription.js'
import useNavigation, { deepLink } from '../../hooks/useNavigation.js'
import useContextMenuOutsideDismiss from '../../hooks/useContextMenuOutsideDismiss.js'
import { placeContextMenu } from '../../lib/contextMenuGeometry.js'
import { captureLayoutSpace, clientPointToLayout } from '../../lib/layoutSpace.js'
import { parseNotificationTarget } from '../../lib/notificationTarget.js'
import useSystemEventStream from '../../hooks/useSystemEventStream.js'
import useTheme from '../../hooks/useTheme.js'
import useProviderAuthStatus from '../../hooks/useProviderAuthStatus.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import {
  appQueries,
  chatMessagesQueryKey,
  chatQueries,
  modelQueries,
  ownerQueries,
} from '../../hooks/queries.js'
import { immersiveReducer, isImmersiveActive } from '../../lib/immersive.js'
import { bumpChatRunSignal, chatRunSignal } from '../../lib/chatRunSignal.js'
import { clearAppFrameStorage, clearCachedAppToken } from '../../lib/appFrameStorage.js'
import * as tabModel from './tabModel.js'
import * as paneModel from './paneModel.js'
import {
  attentionForRequest,
  resolveWorkspaceRequests,
  workspaceRequestFromSystemEvent,
  workspaceRequestsForBuiltApps,
  ACTIVATE_FOREGROUND,
} from './workspacePlacement.js'
import {
  appUpdateStaleMessage,
  findAppStoreApp,
} from '../../lib/appRecovery.js'
import {
  acknowledgeAppActivity,
  appAttentionIds,
  freshChatBuiltApps,
  freshAppIds,
  withAppActivitySeen,
  withAppsFlagged,
  withoutAppFlagged,
} from './newAppAttention.js'
import {
  addCreatedChatToList,
  createdChatDetailCache,
  currentReusableEmptyChat,
  mergeChatListWithCreatedGuards,
  mostRecentConcreteChatId,
  newChatPresentationIsCurrent,
  reconcileCreatedChatGuard,
  rememberCreatedChat,
  reusableChatDetailVerdict,
} from './newChatPolicy.js'
import {
  forgetConfirmedDeletion,
  forgetConfirmedDeletionIfExists,
  rememberConfirmedDeletion,
  withoutConfirmedDeletions,
} from './confirmedDeletion.js'
import {
  withChatOwnerActivity,
  withChatRename,
  withChatRunState,
} from './chatListProjection.js'
import {
  clearComposerDraft,
  consumeComposerHandoff,
  stageComposerHandoff,
} from '../ChatView/composerDraft.js'
import {
  beginTouchComposerFocusLease,
  releaseComposerFocusLease,
} from './composerFocusLease.js'
import {
  shouldRearmShellApply,
  watchForShellUpdateOnForeground,
} from './swHandoff.js'
import './Shell.css'
import './workspace.css'
import WorkspaceChrome from './WorkspaceChrome.jsx'
import useWorkspaceDrag from './useWorkspaceDrag.js'
import useModeController from './useModeController.js'
import useModeViewTransition, { modeViewTransitionStyle } from './useModeViewTransition.js'
import * as modeMachine from './modeMachine.js'
import { undoKeyPressed, isEditableTarget } from './workspaceOnboarding.js'
import PaneChatView from './PaneChatView.jsx'
import {
  BUILDER_CHAT_WORLD,
  STANDARD_CHAT_WORLD,
  deriveChatSurfaceLayers,
  deriveChatSurfaceOwners,
} from './chatSurfaceModel.js'
import { deriveWorkspaceVisualState } from './visualReadiness.js'
import {
  shouldFocusComposerAfterPanePointer,
  supportsDesktopPaneComposerFocus,
} from './paneChatFocus.js'
import {
  acknowledgeAppPreview,
  withAppPreviewSeen,
} from './builtAppState.js'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import {
  deriveContentVisibility, deriveModeSnapshotPlan,
  MODE_MOTION, EMPTY_SINGLE_SURFACE_KEY,
} from './workspaceView.js'
import NewChatLanding from './NewChatLanding.jsx'
import { recentChatsToPrefetch } from './chatPrefetch.js'
import {
  PaneTab, panePanelDomId, paneTabDomId, scrollStripWheel, stripKeyDown,
} from './PaneStrip.jsx'
import useAppIntentNavigation from './useAppIntentNavigation.js'
import useDesktopSidebar, {
  desktopContentWidthAfterSidebarToggle,
} from './useDesktopSidebar.js'
import useWorkspaceSession from './useWorkspaceSession.js'
import useShellReloadController from './useShellReloadController.js'
import useAppFrameCache from './useAppFrameCache.js'
import useShellVisualViewport from './useShellVisualViewport.js'
import ShellBrand from './ShellBrand.jsx'
import { createMediaSessionOwner } from './mediaSessionOwner.js'
import { HistoryDismissProvider } from '../../hooks/useHistoryDismiss.jsx'

const APP_SETTINGS_SECTIONS = new Set([
  'ai-providers',
  'background-agents',
  'models',
])
const EMPTY_LIST = Object.freeze([])
// Mode timing lives with the pure snapshot geometry in workspaceView.js; browser
// transition completion owns its lifetime, so Shell has no animation timers.
const SettingsView = lazy(() => import('../SettingsView/SettingsView.jsx'))

export default function Shell() {
  const {
    desktop: desktopSidebarMode,
    open: desktopSidebarOpen,
    setOpen: setDesktopSidebarOpen,
    width: desktopSidebarWidth,
    setWidth: setDesktopSidebarWidth,
  } = useDesktopSidebar()

  const {
    workspace,
    workspaceStateRef,
    dispatchWorkspace,
    blobValid,
    replaceImplicitBootTab,
    dragActiveRef,
    onWorkspaceTransitionRef,
    requestEmptySingleNewChatRef,
    focusedPaneViewId,
    focusedPaneViewIdRef,
    setFocusedPaneViewId,
    toggleFocusedPaneView,
    contentElRef,
    contentRect,
    contentRectRef,
    primeContentRect,
    syncContentRect,
    workspaceMode,
    baseProjection,
    projection,
    visiblePaneIds,
    persistWorkspaceSnapshot,
  } = useWorkspaceSession({
    storage: localStorage,
    legacyStorage: sessionStorage,
  })

  const {
    activeView,
    activeAppId,
    activeChatId,
    drawerOpen, settingsOverlayOpen, settingsOpenRaw, openDrawer, closeDrawer,
    drawerNavigationCover, finishDrawerNavigationPresentation,
    navTo, tabRevealRevision, applyModeDestination, dismissSettings,
    backFiredRef, drawerPushedRef, navStackRef, navigationEpochRef,
    activeViewRef, activeChatIdRef, activeAppIdRef,
    drawerOpenRef,
    appNavPush, appNavPop, appNavReset, appNavForwardResult,
    retireAppHistory, tombstoneRoute,
    openHistoryDismiss, closeHistoryDismiss, unregisterHistoryDismiss,
  } = useNavigation({
    workspace,
    workspaceStateRef,
    dispatchWorkspace,
    visiblePaneIds,
    blobValid,
    replaceImplicitBootTab,
    dragActiveRef,
  })

  // A mobile drawer is a history-backed virtual route. A desktop sidebar is a
  // saved layout preference. Keep those state machines separate: while a mobile
  // sentinel is being consumed after a resize, the UI remains modal and inert;
  // only once it closes does the desktop layout become interactive.
  const persistentDrawer = desktopSidebarMode && !drawerOpen
  const drawerModeTransitioning = desktopSidebarMode && drawerOpen
  const navigationOpen = persistentDrawer ? desktopSidebarOpen : drawerOpen
  const modalDrawerOpen = !persistentDrawer && drawerOpen
  const [appsDirectoryHost, setAppsDirectoryHost] = useState(null)
  // This is the single semantic owner of reserved desktop navigation space.
  // Both the root class and the content-geometry transaction below read it.
  const desktopSidebarReserved = persistentDrawer && desktopSidebarOpen
  const primeDesktopSidebarContentRect = useCallback((nextReserved) => {
    const el = contentElRef.current
    if (!el) return
    const w = desktopContentWidthAfterSidebarToggle(el.clientWidth, {
      currentReserved: desktopSidebarReserved,
      nextReserved,
      sidebarWidth: desktopSidebarWidth,
    })
    const h = Math.round(el.clientHeight)
    primeContentRect({ w, h })
  }, [desktopSidebarReserved, desktopSidebarWidth, primeContentRect])
  const closeDrawerRef = useRef(closeDrawer)
  closeDrawerRef.current = closeDrawer
  useEffect(() => {
    if (desktopSidebarMode && drawerOpen) {
      closeDrawerRef.current({ preserveModalUntilTraversal: true })
    }
  }, [desktopSidebarMode, drawerOpen])

  const brandButtonRef = useRef(null)
  const immersiveExitRef = useRef(null)
  const previousPersistentDrawerRef = useRef(persistentDrawer)
  useLayoutEffect(() => {
    const wasPersistent = previousPersistentDrawerRef.current
    previousPersistentDrawerRef.current = persistentDrawer
    const focused = document.activeElement
    const drawer = document.getElementById('navigation-drawer')
    if (!drawer?.contains(focused)) return
    if ((persistentDrawer && !navigationOpen) || (wasPersistent && !persistentDrawer)) {
      brandButtonRef.current?.focus()
    }
  }, [navigationOpen, persistentDrawer])

  // The COMMITTED-mode-gated overlay flag from the nav adapter — the DERIVATION
  // INPUT to deriveContentVisibility only (finding F3). It counts the takeover
  // wherever the COMMITTED world is single; deriveContentVisibility re-gates it by
  // the EFFECTIVE mode and returns `settingsOverlay`, which every PAINT gate reads.
  // The render never uses `activeView === 'settings'` for pane suppression (that
  // would hide every pane behind a builder Settings tab — the named risk).
  const settingsActive = settingsOverlayOpen
  // Builder mode is the tiled 'panes' view-mode. The logo itself is the
  // persistent mode indicator and gesture surface;
  // there is deliberately no separate header control.
  // Durable mode and drag-preview stay in the small mode reducer. The visual
  // Standard ↔ Builder scene belongs independently to the browser transition.
  const shellRootRef = useRef(null)
  useShellVisualViewport(shellRootRef)
  const mode = useModeController({
    committedMode: workspace.viewMode,
  })
  const modeView = useModeViewTransition({
    rootRef: shellRootRef,
    durationMs: MODE_MOTION.slideMs,
  })
  const modeState = mode.state
  // Keep the focused presentation through a Builder -> Standard scene so the
  // captured old world is exactly what the user saw. Once the scene idles,
  // discard it; Standard mode has no pane-focus presentation to restore.
  useEffect(() => {
    if (workspace.viewMode === 'single' && !modeView.active && !modeState.transition
        && focusedPaneViewIdRef.current != null) {
      setFocusedPaneViewId(null)
    }
  }, [workspace.viewMode, modeView.active, modeState.transition, setFocusedPaneViewId])

  // Builder mode = the committed 'panes' world. It flips synchronously with the
  // toggle, matching the gesture's own spring/snap.
  const builderModeActive = modeMachine.builderModeActive(modeState)
  // Drag-preview alone may project the tiled world before it is committed.
  const effectiveViewMode = modeMachine.effectiveViewMode(modeState)
  const multiPaneBuilderVisible = effectiveViewMode === 'panes'
    && paneModel.paneIdsInOrder(workspace).length > 1
  const multiPaneBuilderVisibleRef = useRef(multiPaneBuilderVisible)
  multiPaneBuilderVisibleRef.current = multiPaneBuilderVisible
  // The final React world is already committed during this window; only browser
  // snapshots move. Shield that settled DOM until the scene transaction ends.
  const modeBeatActive = !!modeView.active
  // The logo spring-back window on ShellBrand while an animated beat is live
  // (round 4 item 1): the mark holds .84 through the beat and releases over the
  // terminal logoReleaseMs so its first full-size frame lands at completion. `both`
  // fill on the CSS keyframe holds .84 through the release DELAY. The twist rides
  // --mode-total so rotation, panes, and logo settle together. A short plan clamps
  // the release to the whole beat. Null (no vars) when idle.
  const brandBeatStyle = useMemo(() => {
    const visualBeat = modeView.active
    if (!visualBeat) return null
    const total = visualBeat.totalMs
    const release = Math.min(MODE_MOTION.logoReleaseMs, total)
    return {
      '--mode-total': `${total}ms`,
      '--logo-release-ms': `${release}ms`,
      '--logo-release-delay': `${Math.max(0, total - MODE_MOTION.logoReleaseMs)}ms`,
    }
  }, [modeView.active])
  const shellRootStyle = useMemo(() => ({
    '--desktop-sidebar-width': `${desktopSidebarWidth}px`,
    '--shell-tabstrip-height': `${paneModel.STRIP_H}px`,
  }), [desktopSidebarWidth])
  // Immersive mode (moebius:immersive, .pm/128). The state is the id of the app
  // holding an immersive request (or null); it's APPLIED — bar hidden, canvas
  // full-viewport — only while that app is the active canvas of the FOCUSED
  // pane, so switching to chat/settings/another app restores chrome
  // automatically and switching back re-enters without a re-post. The request
  // reaches us through AppCanvas, which verifies the message's event.source
  // against its own iframe before forwarding — the ACTIVE-iframe-only guarantee
  // lives there. Declared here (before the content-visibility derivation) so
  // immersive can solo its pane over the whole workspace (§4/§9). Full contract:
  // lib/immersive.js.
  const [immersiveAppId, dispatchImmersive] = useReducer(immersiveReducer, null)
  const [nowPlaying, setNowPlaying] = useState(null)
  const mediaSessionOwnerRef = useRef(null)
  if (!mediaSessionOwnerRef.current) {
    mediaSessionOwnerRef.current = createMediaSessionOwner(setNowPlaying)
  }
  const handleMediaSession = useCallback((appId, event, sendControl) => {
    mediaSessionOwnerRef.current.receive(appId, event, sendControl)
  }, [])
  const handleNowPlayingControl = useCallback((action) => {
    mediaSessionOwnerRef.current.control(action)
  }, [])
  // Stable identity — AppCanvas's message-listener effect depends on it.
  const handleImmersive = useCallback((appId, value) => {
    dispatchImmersive({ type: 'request', appId, value })
  }, [])
  // Immersive is a temporary overlay lease, independent of the durable builder /
  // single worlds. A verified request from the focused app may therefore solo
  // that app over EITHER world; clearing the lease reveals the exact world below
  // without changing its workspace mode, pane tree, tabs, or single-screen slot.
  // Settings keeps its builder invariant because isImmersiveActive additionally
  // requires the active shell view to be the requesting canvas, and AppCanvas
  // forwards live requests only from its focused active frame.
  const immersiveActive = isImmersiveActive(immersiveAppId, activeView, activeAppId)
  useLayoutEffect(() => {
    if (!immersiveActive) return
    const drawer = document.getElementById('navigation-drawer')
    if (drawer?.contains(document.activeElement)) immersiveExitRef.current?.focus()
  }, [immersiveActive])
  // The single derivation of what content the render paints and where (design
  // §2/§4/§5). Pure + memoized so the immersive-solo and Settings-overlay
  // branches are unit-tested in workspaceView.test.js, and so one commit flips
  // every dependent flag together.
  const contentVisibility = useMemo(
    () => deriveContentVisibility({
      workspace, projection, settingsOverlayOpen: settingsActive,
      immersiveActive, immersiveAppId,
      viewMode: effectiveViewMode, // 'panes' during a single-mode drag preview
      focusedPaneView: focusedPaneViewId != null,
    }),
    [workspace, projection, settingsActive, immersiveActive, immersiveAppId,
      effectiveViewMode, focusedPaneViewId],
  )
  const { multiPane, single, focusedActiveKey, fullBleedKey, visibleAppIds } = contentVisibility
  // The EFFECTIVE-mode-gated Settings takeover flag (finding F3): true only when the
  // takeover actually PAINTS — false in builder AND during a single-mode drag
  // preview (effectiveViewMode 'panes'). Every PAINT gate below reads
  // THIS, not the committed-gated `settingsActive` (which is only the derivation
  // INPUT now), so those transient windows paint the tiled world with Settings
  // suspended exactly as the derived flags assume. MOUNT keys off `settingsOpenRaw`.
  const settingsOverlay = contentVisibility.settingsOverlay
  const workspaceChromeActive = contentVisibility.chromeActive
  // (v2: multiPaneRef / visibleLeavesRef are gone — handleToggleViewMode now builds
  // the whole latched plan from the live projection via deriveExit/EnterPlan, and the
  // undo path reads sceneInputsRef, so no stale-closure ref latch is needed here.)
  const chatPanesVisible = contentVisibility.chatPanesVisible
  // navTo is a per-render function; stable callbacks (handleAppError, passed to
  // AppCanvas's []-dep message listener) reach the latest one through this ref
  // so their identity never churns and the listener never re-registers.
  const navToRef = useRef(navTo)
  navToRef.current = navTo
  // PaneChatView is memoized, so do not defeat its per-chat run-signal boundary
  // with the per-render navTo identity. Pane handlers call through this stable
  // facade and still reach the latest navigation implementation via the ref.
  const stablePaneNavTo = useCallback((view, opts) => navToRef.current(view, opts), [])
  const handleNowPlayingOpen = useCallback((appId) => {
    navToRef.current('canvas', { appId })
  }, [])
  // Reconcile in-memory route hints after every workspace transition (design
  // §5.1.3). navStackRef is stable, so recreating this closure each render is
  // behaviourally identical. reconcileRoutePanes points each hint at the pane
  // that now holds its item (a cross-pane move follows its tab even when the
  // source pane survived) and degrades a dead-pane hint to the structural
  // sibling the collapse chose — NOT global focus, since a background split can
  // be removed while focus is elsewhere. Physical history hints self-correct at
  // restore time (OPEN_TAB dedups an open item to its true pane).
  onWorkspaceTransitionRef.current = (prevWs, nextWs) => {
    if (prevWs.viewMode !== nextWs.viewMode) {
      mode.syncCommitted(nextWs.viewMode)
    }
    navStackRef.current = paneModel.reconcileRoutePanes(navStackRef.current, prevWs, nextWs)
  }

  const { loadTheme } = useTheme()
  const queryClient = useQueryClient()
  const recencyMarkedAppRef = useRef(null)
  useEffect(() => {
    if (activeView !== 'canvas' || activeAppId == null) {
      recencyMarkedAppRef.current = null
      return
    }
    const appId = Number(activeAppId)
    if (!Number.isSafeInteger(appId) || appId <= 0) return
    const key = String(appId)
    if (recencyMarkedAppRef.current === key) return
    recencyMarkedAppRef.current = key

    // The navigation is already authoritative locally, so move the app in
    // Recents immediately while the durable cross-session marker catches up.
    const lastOpenedAt = new Date().toISOString()
    const promoteCachedApp = () => {
      queryClient.setQueryData(appQueries.keys.all, rows => (
        Array.isArray(rows)
          ? rows.map(app => (
            Number(app.id) === appId
              ? { ...app, last_opened_at: lastOpenedAt }
              : app
          ))
          : rows
      ))
    }
    promoteCachedApp()
    void api.apps.markOpened(appId)
      .then(response => {
        if (response.ok) promoteCachedApp()
        else appQueries.list.invalidate(queryClient)
      })
      // Offline navigation remains useful and keeps the optimistic order for
      // this session; the next live list fetch restores server truth.
      .catch(() => {})
  }, [activeAppId, activeView, queryClient])
  const notificationCenterActionsRef = useRef(null)
  const reconcileNotifications = useCallback(() => {
    notificationCenterActionsRef.current?.reconcile()
  }, [])
  const onNotificationCreated = useCallback(() => {
    notificationCenterActionsRef.current?.onCreated()
  }, [])
  // Confirmed writes outrank offline-capable list reads. These session-scoped
  // tombstones filter every query completion (including an in-flight,
  // pre-delete NetworkFirst fallback) until a recovery succeeds.
  const deletedChatIdsRef = useRef(new Set())
  const deletedAppIdsRef = useRef(new Set())
  const reconcileApps = useCallback(
    rows => withoutConfirmedDeletions(rows, deletedAppIdsRef.current),
    [],
  )
  const appsQuery = appQueries.list.useQuery({ reconcile: reconcileApps })
  // Create responses are authoritative even when the next NetworkFirst list
  // request has to fall back to a just-stale service-worker copy. Reconcile at
  // the query function boundary so the protected row never disappears from
  // cache/render between fetch settlement and an after-the-fact patch.
  const recentlyCreatedChatsRef = useRef(new Map())
  const reconcileCreatedChats = useCallback(
    rows => withoutConfirmedDeletions(
      mergeChatListWithCreatedGuards(
        rows, recentlyCreatedChatsRef.current,
      ),
      deletedChatIdsRef.current,
    ),
    [],
  )
  const chatsQuery = chatQueries.list.useQuery({
    reconcile: reconcileCreatedChats,
  })
  const apps = appsQuery.data ?? EMPTY_LIST
  const chats = chatsQuery.data ?? EMPTY_LIST
  const appsStatus = apps.length > 0 || appsQuery.isSuccess
    ? 'success'
    : (appsQuery.isError ? 'error' : 'loading')
  const chatsStatus = chats.length > 0 || chatsQuery.isSuccess
    ? 'success'
    : (chatsQuery.isError ? 'error' : 'loading')
  // Prime only the two most-recent chats that are not already open, including
  // active chats: their cached transcript is useful while stream catch-up runs.
  // ChatView still revalidates on mount, but this gives its synchronous cache
  // read a real transcript so a later chat switch can paint immediately. Run
  // once after the live drawer projection arrives, at browser idle, and stand
  // down under data-saver so speed never creates surprise background transfer.
  const warmedChatsOnLoadRef = useRef(false)
  useEffect(() => {
    if (
      warmedChatsOnLoadRef.current
      || !chatsQuery.isSuccess
      || !chatsQuery.isFetchedAfterMount
    ) return
    warmedChatsOnLoadRef.current = true
    if (navigator.connection?.saveData) return
    const candidates = recentChatsToPrefetch(chats, activeChatId)
    if (candidates.length === 0) return
    const warm = async () => {
      for (const chat of candidates) {
        await chatQueries.messages.prefetch(queryClient, chat.id)
      }
    }
    if (typeof requestIdleCallback === 'function') {
      requestIdleCallback(() => { void warm() }, { timeout: 3000 })
    } else {
      setTimeout(() => { void warm() }, 1000)
    }
  }, [
    activeChatId,
    chats,
    chatsQuery.isFetchedAfterMount,
    chatsQuery.isSuccess,
    queryClient,
  ])
  const appPreviewAckRef = useRef(new Set())
  const handleAppPreviewSeen = useCallback((app, final) => {
    acknowledgeAppPreview({
      app,
      final,
      inFlight: appPreviewAckRef.current,
      request: api.apps.markPreviewSeen,
      clearCached: (appId, updatedAt, seenAsFinal) => {
        queryClient.setQueryData(
          appQueries.keys.all,
          rows => withAppPreviewSeen(
            rows, appId, updatedAt, seenAsFinal,
          ),
        )
      },
      restoreServerTruth: () => appQueries.list.invalidate(queryClient),
    })
  }, [queryClient])
  // Warm the model registry as soon as a chat is open so the composer's
  // model picker is instant on the first '+'. The /api/models fetch
  // otherwise runs cold on the first picker open (it's 5-min cached after
  // that); this just moves that one fetch to chat-open time, in the
  // background. Shares the cache key, so the picker's own useQuery reuses it.
  modelQueries.registry.useQuery({ enabled: !!activeChatId })
  modelQueries.prefs.useQuery({ enabled: !!activeChatId })

  const [appIntents, setAppIntents] = useState({})
  // toast state: null | { message, variant, duration, action }
  // variant: 'info' | 'error'  (see components/ui/Toast.jsx)
  const toastSequenceRef = useRef(0)
  const [toast, setToast] = useState(null)
  const [settingsFocusTarget, setSettingsFocusTarget] = useState(null)
  // Settings stays mounted across workspace transitions. An explicit shell
  // apply can therefore complete a platform-conflict repair without remounting
  // Settings; this token lets that live instance re-read authoritative status
  // even when a multi-pane workspace deliberately defers the full-page reload.
  const [settingsRefreshToken, setSettingsRefreshToken] = useState(0)
  const showToast = useCallback((
    message,
    { variant = 'info', duration = 4000, action } = {},
  ) => {
    toastSequenceRef.current += 1
    setToast({
      message, variant, duration, action, sequence: toastSequenceRef.current,
    })
  }, [])
  // Stable identity is part of Toast's timer contract. Recreating this callback
  // on every Shell render resets the effect timer while chats stream, making a
  // nominal five-second notice linger indefinitely.
  const dismissToast = useCallback(() => { setToast(null) }, [])
  const handleAppIntentDelivered = useCallback((appId, delivered) => {
    setAppIntents((prev) => {
      const key = String(appId)
      if (!prev[key] || prev[key].nonce !== delivered?.nonce) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }, [])
  // Guards the once-per-mount deferred shell-update pickup effect below.
  const shellUpdatePickupRef = useRef(false)
  const shellUpdatePickupCheckStartedRef = useRef(false)
  const [composerRequest, setComposerRequest] = useState(null)
  const composerRequestTokenRef = useRef(0)
  const composerFocusLeaseRef = useRef(null)
  // A user-initiated New-chat tap should acknowledge the destination before
  // row allocation, cache maintenance, or a degraded network can finish. Keep
  // the first-class empty surface above the outgoing chat until the resolved
  // ChatView reports a painted frame; the focus lease below carries any early
  // typing across that ID-less interval.
  const [newChatPresentation, setNewChatPresentation] = useState(null)
  const newChatPresentationRef = useRef(null)
  // A slow New-chat allocation replaces the modal drawer visually without
  // consuming its history entry. Destination navigation owns that entry once
  // the concrete chat exists; avoiding an early Back traversal also keeps the
  // temporary phone composer focused until the real composer accepts it.
  const displayedNavigationOpen = navigationOpen && (
    persistentDrawer || newChatPresentation == null
  )
  const navigationSurfaceOpen = modalDrawerOpen && newChatPresentation == null

  const requestComposer = useCallback((chatId, {
    draft, focus = false,
  } = {}) => {
    if (chatId == null) return
    if (draft == null && !focus) return
    composerRequestTokenRef.current += 1
    setComposerRequest({
      chatId,
      token: composerRequestTokenRef.current,
      draft: draft == null ? null : String(draft),
      focus: focus === true,
    })
  }, [])

  function focusDesktopChatPaneComposer(chatId) {
    if (!supportsDesktopPaneComposerFocus()) return
    requestComposer(chatId, { focus: true })
  }

  // A restored single-screen chat has no click handler to request focus. Keep
  // that one startup intent separate from later workspace projection changes:
  // entering Standard mode must not turn a mode toggle into composer focus.
  const startupChatComposerFocusPendingRef = useRef(
    activeView === 'chat' && effectiveViewMode === 'single',
  )
  useEffect(() => {
    if (!startupChatComposerFocusPendingRef.current) return
    if (activeView !== 'chat' || activeChatId == null) return
    startupChatComposerFocusPendingRef.current = false
    focusDesktopChatPaneComposer(activeChatId)
  }, [activeView, activeChatId])

  const handleComposerRequestHandled = useCallback((token) => {
    setComposerRequest(prev => {
      if (prev?.token !== token) return prev
      if (typeof prev.draft === 'string') {
        consumeComposerHandoff(prev.chatId, prev.draft)
      }
      return null
    })
  }, [])

  // One shell-wide indicator owns the persistent offline explanation. Chat
  // still disables sends while unavailable, but does not repeat this status
  // beside the composer.
  const online = useOnlineStatus()
  const chatsLoadedRef = useRef(false)
  const knownExistingOffListChatIdsRef = useRef(new Set())
  // Always-current chats, for reading inside callbacks that may hold a stale
  // closure. ChatView's onChatMissing fires from an async /chats/{id} 404 and
  // captures `chats` from whenever its load effect was set up — which can be
  // the empty first-render list. Reading `chats[0]` from that stale closure
  // would demote to null instead of the newest live chat; read this ref
  // instead so we always demote to the current most-recent chat.
  const chatsRef = useRef(chats)
  useEffect(() => { chatsRef.current = chats }, [chats])
  // Always-current apps, read by the STABLE handleAppError callback (below) so
  // it can stay `useCallback([])` — required to keep AppCanvas's message
  // listener registered once per appId mount (it lists onAppError in its deps).
  // The ref mirror lets a []-dep callback see later query results without
  // re-registering every mounted AppCanvas message listener.
  const appsRef = useRef(apps)
  useEffect(() => { appsRef.current = apps }, [apps])
  // Latest-`newChat` ref so the stable handleAppError can start a fresh chat
  // for a crash report without depending on newChat's identity (newChat is a
  // per-render function declaration with volatile inputs — chats, streaming,
  // online — that would churn any callback listing it as a dep).
  const newChatRef = useRef(null)
  // Latest-materialize ref so the deferred-New-Chat watcher (stable deps) runs this
  // render's live closure without depending on the function's identity (round 4 item 3).
  const materializeNewChatHomeRef = useRef(null)
  // In-flight guard for newChat. The function POSTs unconditionally now
  // (the old empty-chat-reuse path was the implicit deduper); without
  // this guard a rapid double-tap on "+ New chat" before the API
  // returns races two creates and leaves an extra empty chat behind.
  const creatingChatRef = useRef(false)
  // Recently-recovered chat ids: excluded from the empty-chat-reuse scan
  // in newChat() until they receive their first message. Without this, an
  // Undo that recovers a chat C (which has no messages in the live cache
  // yet because refreshChats hasn't propagated has_messages=true yet) lets
  // a subsequent newChat() reuse C instead of a genuine empty. The id
  // stays in this set until ChatView reports a first message, which
  // guarantees the has_messages flag is now true and the reuse guard
  // (which reads has_messages from the chats query) is reliable again.
  const recoveredChatIdsRef = useRef(new Set())
  // ── Deferred New Chat materialization (round 4 item 3) ─────────────────────
  // A null single-screen slot renders the New Chat landing NOW; the reusable-empty
  // validation + creation runs only AFTER the visual scene idles, so its settled
  // pixels cannot be replaced beneath a captured transition. The request is a
  // monotonic token + a candidate captured from the pre-transition active chat; a
  // watcher effect materializes it once the descriptor is idle, stale-guarded on token
  // + still-single + still-null. offline/failed creation leaves the landing with a
  // retry affordance — never a blank <main>, never chats[0].
  const newChatRequestSeqRef = useRef(0)
  const pendingNewChatRef = useRef(null) // { token, candidateId, resolvedChatId? } | null
  const materializingNewChatRef = useRef(false)
  const [pendingNewChatToken, setPendingNewChatToken] = useState(0)
  // A superseding request can arrive while the prior token is awaiting the server.
  // One revision bump after that await releases is enough to drain the latest token;
  // this is event-driven and only renders in that rare collision (no polling loop).
  const [materializeNewChatRevision, setMaterializeNewChatRevision] = useState(0)
  const [newChatLandingFailure, setNewChatLandingFailure] = useState(null)
  // Live mirror so async materialization cannot replace the captured New Chat
  // landing while a browser scene transition is still displaying it.
  const modeTransitionRef = useRef(modeView.active || modeState.transition)
  modeTransitionRef.current = modeView.active || modeState.transition
  // Every mounted chat pane derives its OWN built-app CTA list per chatId inside
  // PaneChatView (builtAppState.js), so Shell no longer holds a global builtApps
  // bound to a single activeChatId.

  // ── Tabs: the flat projection of the workspace (the reducer + wrapper are
  // declared above useNavigation). openTabs is the in-order flat walk that
  // today's single top strip renders.
  const openTabs = useMemo(() => paneModel.flatten(workspace), [workspace])
  const {
    appsLiveFetched,
    dropFromWarmLru,
    renderedAppIds,
    versionForApp,
    warmAppCode,
  } = useAppFrameCache({
    apps,
    appsQuery,
    visibleAppIds,
    workspace,
    openTabs,
    queryClient,
    navStackRef,
    workspaceStateRef,
    retireAppHistory,
    tombstoneRoute,
    dispatchWorkspace,
  })
  // Becoming a two-tab workspace engages the strip; returning to zero resets it.
  // A single implicit home tab on a fresh session stays visually identical to
  // the pre-workspace shell. State (rather than a render-time ref mutation) keeps
  // this safe under replayed or abandoned concurrent renders.
  // Pointer events inside an iframe do not bubble to its positioned shell
  // wrapper. The verified live frame sends a tiny focus signal so app panes have
  // the same click-to-focus semantics as native chat panes.
  const focusAppPane = useCallback((appId) => {
    const ws = workspaceStateRef.current.ws
    const pane = paneModel.paneOf(
      ws,
      tabModel.tabKey(tabModel.makeTab('app', appId)),
    )
    if (pane) dispatchWorkspace({ type: 'FOCUS', paneId: pane.id })
  }, [dispatchWorkspace])
  // Request the New Chat landing for an emptied single slot (round 4 item 3). A null
  // slot is a DEFINITE New Chat destination now — never the freshest chat — so this
  // leaves the slot null (the render paints the New Chat surface) and records a
  // tokenized pending request. The reusable-empty validation + creation runs only
  // AFTER the visual scene idles (the materialize watcher below). The candidate is
  // captured from the PRE-transition active chat but NOT targeted synchronously — the
  // reuse policy (newChatPolicy) is deliberately provisional (has_messages can be
  // stale cross-client), so it must survive its detail validation before it becomes
  // the slot. The workspace dispatch boundary calls this for every edge into an empty
  // single screen; the old "null is legitimate only at zero chats" invariant is
  // retired.
  const requestEmptySingleNewChat = useCallback(() => {
    const ws = workspaceStateRef.current.ws
    const single = ws.viewMode === 'single'
    if (!single || ws.singleScreen != null) return
    const candidate = currentReusableEmptyChat(chatsRef.current, {
      activeChatId: activeChatIdRef.current,
      recoveredChatIds: recoveredChatIdsRef.current,
      streamingChatIds: streamingChatIdsRef.current,
    })
    const token = newChatRequestSeqRef.current + 1
    newChatRequestSeqRef.current = token
    pendingNewChatRef.current = { token, candidateId: candidate ? candidate.id : null }
    setNewChatLandingFailure(null)
    setPendingNewChatToken(token)
  }, [workspaceStateRef, activeChatIdRef])
  requestEmptySingleNewChatRef.current = requestEmptySingleNewChat
  const closeTab = useCallback((tab, { reason } = {}) => {
    const key = tabModel.tabKey(tab)
    dispatchWorkspace({ type: 'CLOSE_TAB', tabKey: key, reason })
  }, [dispatchWorkspace])
  const placeInWorkspace = useCallback((requestOrRequests) => {
    const requests = Array.isArray(requestOrRequests)
      ? requestOrRequests
      : [requestOrRequests]
    // The device mode + live app list are stable within one React batch, so read
    // them once at dispatch time (keeping this callback stable). Prefer the live
    // element size over the ResizeObserver-committed ref while it is still the
    // {0,0} boot value — a placement dispatched in the sliver before the observer
    // first fires would otherwise resolve in phone mode on a wide screen. Pane
    // rects are re-derived per-workspace inside resolveWorkspaceRequests.
    let contentRect = contentRectRef.current
    if ((!contentRect.w || !contentRect.h) && contentElRef.current) {
      contentRect = { w: contentElRef.current.clientWidth, h: contentElRef.current.clientHeight }
    }
    const deviceMode = paneModel.modeForRect(contentRect)
    const liveApps = appsRef.current
    // R2: a FOREGROUND agent open in the SINGLE world writes the slot (via the pure
    // resolver's F4 branch) BENEATH an open Settings takeover, so the item would be
    // invisible. Dismiss the takeover alongside the placement — exactly as a
    // user-initiated open does — so the foregrounded item is actually shown. Only in
    // single (in builder the takeover is suspended, and clearing settingsOpen there
    // would unmount the mounted-hidden SettingsView). dismissSettings no-ops when no
    // takeover is open.
    const currentWs = workspaceStateRef.current.ws
    const world = currentWs.viewMode
    if (world === 'single'
        && requests.some(r => r && r.item && r.activation === ACTIVATE_FOREGROUND)) {
      dismissSettings()
    }
    // Dispatch the resolver as a FUNCTION (workspace → workspace): the reducer
    // runs it against the CURRENT reducer workspace, so placements landing in one
    // React batch compose (the second sees the first, splits and all) instead of
    // clobbering each other from a stale render snapshot. resolveWorkspaceRequests
    // folds FORWARD so a batch reaches the same result as the same requests
    // delivered one dispatch at a time (batch == sequential).
    dispatchWorkspace({
      type: 'APPLY_PLACEMENT',
      resolve: (ws) => resolveWorkspaceRequests(ws, requests, {
        mode: deviceMode,
        contentRect,
        liveApps,
      }),
    })
  }, [dispatchWorkspace, dismissSettings])
  // The tab strip is the BUILDER SURFACE: with splits ON it follows the
  // EFFECTIVE builder world exactly — always present in builder (even at a
  // single leaf, where this single-pane .shell__tabstrip stands in for the
  // tiled WorkspaceChrome strips, giving phone users the drag source), riding
  // a single-mode drag preview with the rest of the tiled presentation, and NEVER
  // rendered in single mode OR over an immersive lease (the shell exit replaces
  // every builder navigation surface). An empty workspace shows no strip.
  const tabStripVisible = !immersiveActive
    && effectiveViewMode === 'panes'
    && openTabs.length >= 1
  const shellTabStripVisible = tabStripVisible && !workspaceChromeActive

  // Reconcile other React-owned chrome changes from committed DOM. Desktop
  // drawer toggles do not depend on this effect for atomicity: their event
  // handler primes contentRect in the same state batch.
  useLayoutEffect(() => {
    syncContentRect({ settlePending: true })
  }, [
    desktopSidebarReserved,
    desktopSidebarWidth,
    immersiveActive,
    shellTabStripVisible,
    syncContentRect,
  ])

  // tabKey -> { paneId, CONTENT rect } (pane rect minus its strip) of the active
  // tab of each visible pane. A content wrapper matching a key is positioned +
  // shown; every other wrapper keeps the full-bleed hidden pattern.
  const visibleTabRects = useMemo(() => {
    const map = new Map()
    if (!workspaceChromeActive) return map
    for (const paneId of projection.visibleLeaves) {
      const pane = workspace.panes[paneId]
      const rect = projection.rects[paneId]
      if (!pane || !pane.activeTabKey || !rect) continue
      map.set(pane.activeTabKey, {
        paneId,
        x: rect.x, y: rect.y + paneModel.STRIP_H,
        w: rect.w, h: Math.max(0, rect.h - paneModel.STRIP_H),
      })
    }
    return map
  }, [workspaceChromeActive, projection, workspace])

  // Builder chat mounts keep their own projected geometry even while Standard
  // paints. They remain visibility:hidden in that world, but their ChatView
  // layout never borrows Standard's full-bleed box and therefore never resizes
  // at the mode boundary. A normal one-leaf Builder uses the flow strip and a
  // full-bleed wrapper, so only real tiled/focused projections need staged rects.
  const builderChatTabRects = useMemo(() => {
    const map = new Map()
    if (projection.visibleLeaves.length < 2 && !projection.focusedPaneView) return map
    for (const paneId of projection.visibleLeaves) {
      const pane = workspace.panes[paneId]
      const rect = projection.rects[paneId]
      if (!pane || !pane.activeTabKey || !rect) continue
      map.set(pane.activeTabKey, {
        paneId,
        x: rect.x,
        y: rect.y + paneModel.STRIP_H,
        w: rect.w,
        h: Math.max(0, rect.h - paneModel.STRIP_H),
      })
    }
    return map
  }, [projection, workspace])

  // ── The ONE Settings wrapper (design §4: overlay-or-pane geometry) ─────────
  // A single, stable SettingsView mount that is positioned like any chat/app
  // content when Settings is a visible builder tab, and full-bleed when the
  // takeover overlay is up. Keeping it ONE element (never two conditional mounts)
  // preserves component identity across the tab<->overlay mode conversion, so the
  // scroll position and transient Settings state survive the flip.
  const SETTINGS_KEY = tabModel.SETTINGS_TAB_KEY
  // Visible as a builder TAB: the takeover is not PAINTING AND some visible pane has
  // the Settings tab active. Gated on the effective `settingsOverlay` so a
  // single-mode drag preview cannot paint a stray Settings tab as a pane. (Blind to
  // a BACKGROUND Settings tab — not painted.)
  const settingsVisibleAsTab = !settingsOverlay
    && projection.visibleLeaves.some(id => workspace.panes[id]?.activeTabKey === SETTINGS_KEY)
  // MOUNT (finding F3): keyed off the RAW suspended overlay intent, NOT the
  // committed/effective PAINT flag, so SettingsView stays mounted-hidden across a
  // world flip (mount-identity rule, exactly like the slot chat) and its transient
  // state survives — the old `settingsActive` gate unmounted it on a builder flip
  // with no Settings tab.
  const settingsMounted = settingsOpenRaw || settingsVisibleAsTab
  // Positioned into its pane's content rect only in the tiled multi-pane render.
  const settingsPaned = (workspaceChromeActive && settingsVisibleAsTab)
    ? visibleTabRects.get(SETTINGS_KEY)
    : null
  // Full-bleed for the PAINTING takeover overlay (effective-gated, finding F3), and
  // for single-pane builder where the Settings tab is the sole full-bleed surface
  // (fullBleedKey === settings key).
  const settingsFullBleed = !settingsPaned
    && (settingsOverlay || (settingsVisibleAsTab && SETTINGS_KEY === fullBleedKey))
  // Apps is a normal canonical workspace item — no takeover state. It follows
  // the same full-bleed/paned projection as chats and installed apps.
  const APPS_KEY = tabModel.APPS_TAB_KEY
  const appsVisibleAsTab = fullBleedKey === APPS_KEY
    || projection.visibleLeaves.some(id => workspace.panes[id]?.activeTabKey === APPS_KEY)
  const appsPaned = workspaceChromeActive ? visibleTabRects.get(APPS_KEY) : null
  const appsFullBleed = !appsPaned && fullBleedKey === APPS_KEY
  // focusedActiveKey / fullBleedKey / visibleAppIds are derived once by
  // deriveContentVisibility above: focusedActiveKey drives the AppCanvas
  // focused-pane-only `active` prop (insets + immersive holder); fullBleedKey is
  // the single wrapper painted over the whole box (single-pane, or the immersive
  // holder); visibleAppIds is the app set that paints + stays frame-visible
  // (Settings hides all; immersive solos the holder so every sibling frame goes
  // visibility:false).
  // The chat ids that are the active tab of a visible pane — membership, not
  // equality with one global id, is what a pane-aware attention/repair rule
  // tests (design §2 M13, finding D-iii).
  const visibleChatIds = useMemo(() => {
    const set = new Set()
    if (settingsOverlay) return set
    for (const paneId of projection.visibleLeaves) {
      const pane = workspace.panes[paneId]
      const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
      if (active && active.kind === 'chat') set.add(String(active.id))
    }
    return set
  }, [settingsOverlay, workspace, projection])
  const visibleChatIdsRef = useRef(visibleChatIds)
  useEffect(() => { visibleChatIdsRef.current = visibleChatIds }, [visibleChatIds])
  // Retained chat surfaces for BOTH layout worlds. A chat selected in Standard's
  // slot and a Builder pane has two physical layout owners: Standard remains
  // full-bleed and Builder remains pane-sized, so neither world's ResizeObserver /
  // scroll controller reacts to the other's mode transition. Within Builder the
  // surface key remains chat-based, preserving identity across cross-pane moves.
  const visibleChatPanes = useMemo(() => {
    return deriveChatSurfaceOwners({ workspace, baseProjection, projection })
  }, [baseProjection, projection, workspace])
  // Last chat that reached a stable painted frame in each visible pane. On a
  // chat-tab change, keep that outgoing ChatView mounted as an inert cover while
  // the incoming chat runs its existing hide/restore/reveal transaction below.
  // The map advances only from the incoming ChatView's layout-ready callback,
  // so rapid A -> B -> C navigation keeps A painted and replaces only staging B.
  const [presentedChatByPane, setPresentedChatByPane] = useState(() => new Map())
  const visibleChatPaneSignature = visibleChatPanes
    .map(({ world, paneId, chatId }) => `${world}:${paneId}:${chatId}`)
    .join('|')

  // Drop state for panes whose active visible surface is no longer a chat.
  // Same-pane A -> B deliberately keeps A until B reports display-ready.
  useEffect(() => {
    const livePaneIds = new Set(visibleChatPanes.map(({ paneId }) => String(paneId)))
    setPresentedChatByPane(prev => {
      let changed = false
      const next = new Map(prev)
      for (const paneId of next.keys()) {
        if (!livePaneIds.has(String(paneId))) {
          next.delete(paneId)
          changed = true
        }
      }
      return changed ? next : prev
    })
    // The primitive signature is the intentional dependency: visibleChatPanes
    // is rebuilt from workspace objects and should not churn this cleanup.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleChatPaneSignature])

  const handlePaneChatDisplayReady = useCallback((paneId, readyChatId) => {
    const id = String(readyChatId)
    const paneKey = String(paneId)
    // Ignore a late ready signal from staging B after rapid navigation reached C.
    // Surface owners include both real builder panes and the single world's
    // synthetic slot, so resolve the selected key through their shared boundary.
    if (paneModel.activeKeyForOwner(workspaceStateRef.current.ws, paneKey) !== `chat:${id}`) return
    setPresentedChatByPane(prev => {
      if (String(prev.get(paneKey) ?? '') === id) return prev
      const next = new Map(prev)
      next.set(paneKey, id)
      return next
    })
    const presentation = newChatPresentationRef.current
    if (String(presentation?.chatId ?? '') === id) {
      newChatPresentationRef.current = null
      setNewChatPresentation(current => (
        current === presentation ? null : current
      ))
      releaseComposerFocusLease(composerFocusLeaseRef.current)
    }
    finishDrawerNavigationPresentation()
  }, [finishDrawerNavigationPresentation, workspaceStateRef])

  // A route change, Back gesture, drawer reopen, or mode switch supersedes a
  // pending New-chat tap. Retire its cover and keyboard lease together so its
  // eventual network result cannot repaint or navigate over the newer intent.
  useLayoutEffect(() => {
    const presentation = newChatPresentationRef.current
    if (!presentation || newChatPresentationIsCurrent(presentation, {
      navigationEpoch: navigationEpochRef.current,
      viewMode: workspace.viewMode,
      drawerEntryOpen: drawerPushedRef.current && navigationOpen,
      activeView,
      activeChatId,
    })) return
    newChatPresentationRef.current = null
    setNewChatPresentation(current => (
      current === presentation ? null : current
    ))
    releaseComposerFocusLease(composerFocusLeaseRef.current)
  }, [
    activeChatId,
    activeView,
    drawerPushedRef,
    navigationEpochRef,
    navigationOpen,
    newChatPresentation,
    workspace.viewMode,
  ])

  // At most two ChatViews per transitioning owner: the last painted chat and the
  // current active chat. Handoff dedupe is world-local: Standard's retained copy
  // must never suppress Builder's outgoing cover for the same underlying chat.
  const chatPaneLayers = useMemo(() => {
    return deriveChatSurfaceLayers(visibleChatPanes, presentedChatByPane)
  }, [presentedChatByPane, visibleChatPanes])
  // Shell is the only layer that knows which retained workspace world is
  // actually painted. Publish one stable readiness contract for visual tools;
  // they must not learn private handoff classes or compositor attributes.
  const workspaceVisualState = deriveWorkspaceVisualState({
    modeTransition: modeView.active || modeState.transition,
    chatPanesVisible,
    chatPaneLayers,
    paintedChatWorld: effectiveViewMode === 'single'
      ? STANDARD_CHAT_WORLD
      : BUILDER_CHAT_WORLD,
  })

  // Id → row Maps, rebuilt only when the chat/app lists change. labelForTab and
  // the single-pane strip previously ran a linear chats.find/apps.find PER tab
  // PER render — thousands of scans on an instance with hundreds of chats and a
  // 3-4 pane strip (finding: labelForTab O(tabs × chats/apps)). One O(1) lookup.
  const chatById = useMemo(() => {
    const m = new Map()
    for (const c of chats) m.set(String(c.id), c)
    return m
  }, [chats])
  const appById = useMemo(() => {
    const m = new Map()
    for (const a of apps) m.set(String(a.id), a)
    return m
  }, [apps])
  const labelForTab = useCallback((tab) => {
    if (tab.kind === 'apps') return 'Apps'
    if (tab.kind === 'settings') return 'Settings'
    if (tab.kind === 'chat') return chatById.get(tab.id)?.title || 'Chat'
    return appById.get(tab.id)?.name || 'App'
  }, [chatById, appById])

  // Per-chat repair callback for a mounted chat pane (design §2 M13). A pane
  // whose chat reports a real 404 drops its tab; the derived triple follows the
  // workspace. Builder mode may seed a surviving chat into its sole empty root;
  // an emptied single slot is owned by the New Chat policy boundary.
  const handlePaneChatMissing = useCallback((missingId) => {
    knownExistingOffListChatIdsRef.current.delete(missingId)
    dispatchWorkspace({
      type: 'CLOSE_TAB',
      tabKey: tabModel.tabKey(tabModel.makeTab('chat', missingId)),
      reason: 'deleted',
    })
    const ws = workspaceStateRef.current.ws
    // Only builder repair falls back to a historical chat. In single mode the
    // deleted-close edge already requested the explicit New Chat destination;
    // selecting chats[0] here would overwrite it with an unrelated transcript.
    const single = ws.viewMode === 'single'
    const builderEmpty = !single
      && Object.keys(ws.panes).length === 1
      && !ws.panes[ws.focusedPaneId]?.activeTabKey
    if (builderEmpty) {
      const fallback = chatsRef.current.find(c => String(c.id) !== String(missingId))
      if (fallback) {
        // R1: a background 404-repair preserves an open Settings takeover — it seeds
        // the visible slot beneath it rather than dismissing the owner's Settings view.
        applyModeDestination({ view: 'chat', chatId: fallback.id, appId: null, paneId: ws.focusedPaneId }, { preserveSettings: true })
      }
    }
  }, [applyModeDestination, dispatchWorkspace, workspaceStateRef])
  const handlePaneChatFirstMessage = useCallback((chatId) => {
    recoveredChatIdsRef.current.delete(chatId)
  }, [])

  // Tabs expose one compact browser-style close menu without adding permanent
  // chrome: right-click or the keyboard menu key on desktop, and a stationary
  // hold on touch. Movement after the touch hold still enters tab dragging. The
  // same state drives both the single strip and tiled pane strips.
  const [tabMenu, setTabMenu] = useState(null)
  const tabMenuRef = useRef(null)
  const tabMenuReturnFocusRef = useRef(null)
  const openTabMenu = useCallback((event, tab, paneId) => {
    event.preventDefault()
    const owner = paneId || paneModel.paneOf(workspace, tabModel.tabKey(tab))?.id
    if (!owner) return
    const triggerRect = event.currentTarget.getBoundingClientRect()
    const keyboardPlacement = event.clientX === 0 && event.clientY === 0
    tabMenuReturnFocusRef.current = event.currentTarget
    setTabMenu({
      x: keyboardPlacement
        ? triggerRect.left + Math.min(triggerRect.width / 2, 28)
        : event.clientX,
      y: keyboardPlacement ? triggerRect.bottom : event.clientY,
      tab,
      tabKey: tabModel.tabKey(tab),
      paneId: owner,
    })
  }, [workspace])
  const openTabMenuAt = useCallback((x, y, tab, paneId) => {
    if (!tab) return
    const owner = paneId
      || paneModel.paneOf(workspaceStateRef.current.ws, tabModel.tabKey(tab))?.id
    if (!owner) return
    tabMenuReturnFocusRef.current = null
    setTabMenu({ x, y, tab, tabKey: tabModel.tabKey(tab), paneId: owner })
  }, [])
  const closeTabMenu = useCallback((restoreFocus = true) => {
    setTabMenu(null)
    if (!restoreFocus) return
    const returnTarget = tabMenuReturnFocusRef.current
    queueMicrotask(() => returnTarget?.focus?.({ preventScroll: true }))
  }, [])
  const closeTabMenuFromOutside = useCallback(() => {
    closeTabMenu(false)
  }, [closeTabMenu])
  useContextMenuOutsideDismiss({
    open: Boolean(tabMenu),
    menuRef: tabMenuRef,
    onDismiss: closeTabMenuFromOutside,
  })
  useLayoutEffect(() => {
    if (!tabMenu || !tabMenuRef.current) return
    const menu = tabMenuRef.current
    const root = document.documentElement
    const rootSpace = captureLayoutSpace(root)
    const position = placeContextMenu({
      point: clientPointToLayout({ x: tabMenu.x, y: tabMenu.y }, rootSpace),
      viewport: { width: rootSpace.width, height: rootSpace.height },
      menuSize: { width: menu.offsetWidth, height: menu.offsetHeight },
    })
    menu.style.setProperty('--workspace-menu-x', `${position.x}px`)
    menu.style.setProperty('--workspace-menu-y', `${position.y}px`)
    menu.dataset.positioned = 'true'
    menu.querySelector('[role="menuitem"]')?.focus()
  }, [tabMenu])
  const handleTabMenuKeyDown = useCallback((event) => {
    const items = [...(tabMenuRef.current?.querySelectorAll('[role="menuitem"]') || [])]
    if (items.length === 0) return
    const current = Math.max(0, items.indexOf(document.activeElement))
    let next = null
    if (event.key === 'ArrowDown') next = (current + 1) % items.length
    else if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = items.length - 1
    if (next == null) return
    event.preventDefault()
    items[next].focus()
  }, [])
  useEffect(() => {
    if (!tabMenu) return undefined
    const onKey = (event) => {
      if (event.key === 'Escape') closeTabMenu()
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [closeTabMenu, tabMenu])

  // ── Workspace drag controller wiring (design §3, PR3) ─────────────────────
  // One shared controller owns the shipped workspace's document-level gesture.
  // Volatile inputs travel through refs so the hook installs its single
  // document-level pointerdown listener exactly once (never re-registers).
  // dragActiveRef is declared above useNavigation (the drawer OPEN path reads it).
  const sceneInputsRef = useRef(null)
  sceneInputsRef.current = { projection, mode: workspaceMode, contentRect }
  const labelForTabRef = useRef(labelForTab)
  labelForTabRef.current = labelForTab
  const openTabMenuAtRef = useRef(openTabMenuAt)
  openTabMenuAtRef.current = openTabMenuAt
  const drawerRowGesturesRef = useRef(new Map())
  // A single-mode drag previews the builder world through the ONE descriptor
  // (INV 5): arm is phase 'drag-preview', and the id it mints is carried to the
  // matching end so a stale event from a superseded drag is ignored.
  // OPEN_TAB_AT owns any committed mode flip. Ending the drag can only clear
  // its transient preview; the shared workspace transition boundary above
  // synchronizes presentation from the reducer's actual result in that same
  // pointerup batch. A rejected/no-op drop cancels and mutates nothing.
  const dragPreviewIdRef = useRef(null)
  const onModeDragPreview = useCallback((active) => {
    if (active) {
      dragPreviewIdRef.current = mode.dragArm()
    } else {
      mode.dragCancel(dragPreviewIdRef.current)
      dragPreviewIdRef.current = null
    }
  }, [mode, workspaceStateRef])
  // Builder mode deliberately has no standalone header button. It is entered via
  // the logo hold/swipe, drawer drag, or keyboard path. Toggling is a pure state
  // flip: Settings needs NO
  // conversion (v2 deleted it) — its tab survives the flip and single mode paints
  // its own slot, never Settings. It never opens/closes the drawer, and the
  // reducer's SET_VIEW_MODE preserves the undo slot and never touches focus.
  const handleToggleViewMode = useCallback((cause) => {
    const ws = workspaceStateRef.current.ws
    const leavingBuilder = ws.viewMode !== 'single'
    const requestedTo = leavingBuilder ? 'single' : 'panes'
    const to = paneModel.setViewMode(ws, requestedTo).viewMode
    // Builder has no empty state. The model seeds an empty tree from Standard's
    // current screen; only the New Chat landing has no concrete tab to seed. Do
    // not arm a browser scene or twist the logo for that honest no-op.
    if (to === ws.viewMode) {
      return { animated: false, totalMs: 0, transitionId: null, to, changed: false }
    }
    const plan = deriveModeSnapshotPlan({ workspace: ws, projection, contentRect })

    // One browser-owned scene transaction commits BOTH durable authorities. The
    // old world is captured first; React then renders the final world once, and the
    // browser animates settled snapshots. No temporary underlay, live FLIP, divider
    // fade, or second projection commit exists for the panes to race against.
    return modeView.run({
      direction: leavingBuilder ? 'exit' : 'enter',
      to,
      cause,
      plan,
      update: () => {
        dispatchWorkspace({ type: 'SET_VIEW_MODE', mode: to })
      },
    })
  }, [dispatchWorkspace, modeView, projection, contentRect])
  // The single-tap navigation toggle passed to ShellBrand (which owns the logo
  // gesture and static Builder cue). The HOLD / swipe / Shift+Enter mode toggle is
  // handleToggleViewMode above, passed to ShellBrand as onToggleMode.
  const handleToggleNavigation = useCallback(() => {
    if (persistentDrawer) {
      const nextOpen = !desktopSidebarOpen
      primeDesktopSidebarContentRect(nextOpen)
      setDesktopSidebarOpen(nextOpen)
      return
    }
    drawerOpen ? closeDrawer() : openDrawer()
  }, [
    persistentDrawer,
    desktopSidebarOpen,
    primeDesktopSidebarContentRect,
    setDesktopSidebarOpen,
    drawerOpen,
    closeDrawer,
    openDrawer,
  ])
  useWorkspaceDrag({
    contentElRef,
    sceneInputsRef,
    workspaceStateRef,
    dispatchWorkspace,
    labelForTabRef,
    dragActiveRef,
    drawerOpenRef,
    drawerRowGesturesRef,
    closeDrawer,
    openDrawer,
    openTabMenuAtRef,
    onPreviewBuilder: onModeDragPreview,
  })

  // ── Workspace undo chord (design §3.5) ────────────────────────────────────
  // Workspace mutations update the reducer's single undo slot SILENTLY; the
  // owner found the "Moved X · Undo" / "Agent arranged your workspace" toasts
  // noise, so there is no per-mutation toast (owner call, live testing). Undo
  // remains available through Cmd/Ctrl+Z while focus is outside an editor.
  // Cmd/Ctrl+Z restores the single-slot pre-mutation snapshot while no input is
  // focused (design §3.5). Flag-gated; a text field's own undo always wins.
  // Documented limitation (PR3): key events do not cross the iframe boundary, so
  // the chord is inert while a cross-origin app iframe holds focus — in that
  // case click into the shell chrome (a strip tab or the divider) first, then
  // press the chord.
  useEffect(() => {
    const onKey = (e) => {
      if (!undoKeyPressed(e) || isEditableTarget(document.activeElement)) return
      e.preventDefault()
      // A mode-restoring undo (single-leaf drop, empty-builder auto-return) routes
      // through the controller FIRST (INV 2/3) so its re-entry/exit deal fires as one
      // gesture, not a passive sync a render later. undo.restoreViewMode reverts the
      // snapshot's mode; every other undo carries the current mode forward
      // (restoredMode === current), so no mode presentation change is needed there. The presentation
      // plan is built from the tree the beat animates: re-entering builder deals in
      // the RESTORED tree; exiting to single deals the CURRENT tiled tree out.
      const wsState = workspaceStateRef.current
      const undoSlot = wsState.undo
      if (undoSlot) {
        const restoredMode = undoSlot.restoreViewMode
          ? undoSlot.ws.viewMode : wsState.ws.viewMode
        if (restoredMode !== wsState.ws.viewMode) {
          const scene = sceneInputsRef.current
          const paneWorld = restoredMode === 'panes' ? undoSlot.ws : wsState.ws
          const paneProjection = restoredMode === 'panes'
            ? paneModel.projectLayout(paneWorld, scene.mode, scene.contentRect)
            : scene.projection
          const plan = deriveModeSnapshotPlan({
            workspace: paneWorld,
            projection: paneProjection,
            contentRect: scene.contentRect,
          })
          modeView.run({
            direction: restoredMode === 'panes' ? 'enter' : 'exit',
            to: restoredMode,
            cause: 'undo',
            plan,
            update: () => {
              dispatchWorkspace({ type: 'UNDO_LAST' })
            },
          })
          return
        }
      }
      dispatchWorkspace({ type: 'UNDO_LAST' })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dispatchWorkspace, modeView])

  // No per-mutation undo toast: the reducer still mints a fresh undo slot on
  // every workspace mutation (its `toast` label included, for the reducer's own
  // tests), but the shell deliberately does NOT surface it — the owner found the
  // "Moved X · Undo" and "Agent arranged your workspace" toasts noisy. Recovery
  // stays on the Cmd/Ctrl+Z chord above.
  // Ids of apps that appeared in the fetched list AFTER this session's
  // baseline — the drawer renders a subtle accent dot until each is opened.
  const [newAppIds, setNewAppIds] = useState(() => new Set())
  const appAttentionSet = useMemo(
    () => appAttentionIds(apps, newAppIds, visibleAppIds),
    [apps, newAppIds, visibleAppIds],
  )
  // First-sign-in walkthrough. The query result is the source of
  // truth — backend persists completion via
  // POST /api/owner/walkthrough/complete. We render the overlay iff
  // the query has resolved AND `completed` is false; both gates
  // matter (rendering before resolution shows a flash for users who
  // are already past it).
  const walkthroughQuery = ownerQueries.walkthrough.useQuery()
  let visualContentOnly = false
  try {
    visualContentOnly = sessionStorage.getItem('mobius:visual-content-only') === '1'
  } catch (_) {}
  const showWalkthrough = !visualContentOnly
    && walkthroughQuery.isFetched
    && walkthroughQuery.data
    && !walkthroughQuery.data.completed

  // Local streaming ids come from the mounted ChatView immediately at send
  // time. The computed streamingChatIds below merges those with durable
  // `running` flags from /api/chats, so drawer dots survive navigation,
  // reloads, and PWA reopen even when the streaming ChatView is unmounted.
  // attentionChatIds is separate: it marks a background-finished chat until
  // the user opens it, without pretending the turn is still streaming.
  const [localStreamingChatIds, setLocalStreamingChatIds] = useState(() => new Set())
  // Monotonic per-chat activity survives a start+finish pair delivered in one
  // system-stream chunk. A running boolean can end the React batch exactly as
  // it began (false) and lose the fact that the transcript changed.
  const [chatRunSignals, setChatRunSignals] = useState(() => new Map())
  // Voice dictation is a single boolean — is the (single-mount) ChatView's mic
  // active right now — not a per-chat Set: nothing ever read which chat was
  // dictating, only whether any dictation is live, so the shell-reload policy
  // just needs "hold the reload while the mic is on."
  const [voiceDictationActive, setVoiceDictationActive] = useState(false)
  const [attentionChatIds, setAttentionChatIds] = useState(() => new Set())
  const streamingChatIds = useMemo(() => {
    const next = new Set(localStreamingChatIds)
    for (const chat of chats) {
      if (chat.running) next.add(chat.id)
    }
    return next
  }, [localStreamingChatIds, chats])
  const streamingChatIdsRef = useRef(streamingChatIds)
  useEffect(() => { streamingChatIdsRef.current = streamingChatIds }, [streamingChatIds])
  // The reload check runs inside a setTimeout (scheduleShellReloadCheck), which
  // reads render-time state through a ref, so the boolean still needs a ref
  // mirror even though it is no longer a Set.
  const voiceDictationActiveRef = useRef(voiceDictationActive)
  useEffect(() => {
    voiceDictationActiveRef.current = voiceDictationActive
  }, [voiceDictationActive])

  const { requestShellReload } = useShellReloadController({
    win: window,
    doc: document,
    nav: navigator,
    storage: sessionStorage,
    queryClient,
    persistWorkspaceSnapshot,
    workspaceStateRef,
    activeViewRef,
    activeChatIdRef,
    drawerOpenRef,
    multiPaneBuilderVisibleRef,
    streamingChatIdsRef,
    voiceDictationActiveRef,
    activeView,
    activeChatId,
    multiPaneBuilderVisible,
  })

  // Stable callbacks for ChatView — identity must not change across
  // renders or ChatView's onStreamEnd-handler memoization breaks. The
  // setter form lets us avoid depending on the previous state.
  const markStreamingStart = useCallback((chatId) => {
    if (!chatId) return
    setLocalStreamingChatIds(prev => {
      if (prev.has(chatId)) return prev
      const next = new Set(prev)
      next.add(chatId)
      return next
    })
    setAttentionChatIds(prev => {
      if (!prev.has(chatId)) return prev
      const next = new Set(prev)
      next.delete(chatId)
      return next
    })
  }, [])
  const markStreamingEnd = useCallback((chatId) => {
    if (!chatId) return
    setLocalStreamingChatIds(prev => {
      if (!prev.has(chatId)) return prev
      const next = new Set(prev)
      next.delete(chatId)
      return next
    })
  }, [])

  const markChatRunActivity = useCallback((chatId) => {
    setChatRunSignals(prev => bumpChatRunSignal(prev, chatId, 'chat_run_started'))
  }, [])

  const markChatRunFinished = useCallback((chatId) => {
    setChatRunSignals(prev => bumpChatRunSignal(prev, chatId, 'chat_run_finished'))
  }, [])

  const markVoiceListening = useCallback((listening) => {
    setVoiceDictationActive(!!listening)
  }, [])

  const clearChatAttention = useCallback((chatId) => {
    if (!chatId) return
    setAttentionChatIds(prev => {
      if (!prev.has(chatId)) return prev
      const next = new Set(prev)
      next.delete(chatId)
      return next
    })
  }, [])

  // Clear the attention dot for EVERY visible chat pane — membership in the
  // visible set, not equality with one global id (design §2 M13).
  useEffect(() => {
    for (const cid of visibleChatIds) clearChatAttention(cid)
  }, [visibleChatIds, clearChatAttention])

  // New-app arrival dot. `appBaselineRef` holds every id the session has
  // already accounted for (the apps present at the first live fetch, plus any
  // arrival we've since flagged), so a freshly built or App-Store-installed
  // app — which lands at the bottom of the oldest-first drawer list with no
  // affordance — gets a subtle accent dot until it's opened. Separate from
  // `seenAppIdsRef`, which starts empty and drives eviction: keying the dot
  // off that would mark every app "new" on first boot.
  const appBaselineRef = useRef(null)
  const clearAppAttention = useCallback((appId) => {
    setNewAppIds(prev => withoutAppFlagged(prev, appId))
  }, [])
  // The detection effect lives beside the apps-eviction effect below, where
  // `appsLiveFetched` is in scope. Opening an app clears its dot on any path
  // (drawer tap, back-nav, moebius:open-app) because it keys on the active
  // canvas rather than a single onSelect handler.
  useEffect(() => {
    for (const id of visibleAppIds) clearAppAttention(Number(id))
  }, [visibleAppIds, clearAppAttention])

  // Opening an app acknowledges its durable background activity. Optimistic
  // cache clearing removes the dot immediately; server truth is restored on a
  // failed request. In-flight keys include the observed activity version:
  // duplicate renders share one request, while genuinely newer activity can
  // be acknowledged independently without waiting for an older request.
  const appActivityAckRef = useRef(new Set())
  useEffect(() => {
    for (const rawId of visibleAppIds) {
      const appId = Number(rawId)
      if (Number.isNaN(appId)) continue
      const app = apps.find(row => Number(row.id) === appId)
      if (!app?.has_unseen_activity || !app?.unseen_activity_version) continue
      const observedActivityVersion = app.unseen_activity_version
      acknowledgeAppActivity({
        appId,
        activityVersion: observedActivityVersion,
        inFlight: appActivityAckRef.current,
        request: api.apps.markActivitySeen,
        clearCached: (seenAppId, seenThroughVersion) => {
          queryClient.setQueryData(
            appQueries.keys.all,
            rows => withAppActivitySeen(rows, seenAppId, seenThroughVersion),
          )
        },
        restoreServerTruth: () => appQueries.list.invalidate(queryClient),
      })
    }
  }, [visibleAppIds, apps, queryClient])

  // Immersive games request OS fullscreen to also drop the Android status bar
  // and paint under the notch — but ENTER must come from the app, because the
  // Fullscreen API needs the user gesture, and the gameplay tap lands in the
  // app's iframe, not here (see the building-apps "immersive" notes). EXIT
  // needs no gesture, so the shell owns it: when immersive is released (app
  // switch, exit button, or leaving the canvas) we drop fullscreen from the
  // top document. Guarded on fullscreenElement so we never call
  // exitFullscreen() with nothing fullscreen (it would reject). Fullscreen and
  // immersive are loosely coupled on purpose — a system swipe that exits
  // fullscreen leaves immersive applied (bar stays hidden); the app re-enters
  // on the next tap. iOS has no element fullscreen, so this is a no-op there.
  useEffect(() => {
    if (immersiveActive) return
    if (typeof document !== 'undefined' && document.fullscreenElement) {
      document.exitFullscreen?.().catch(() => {})
    }
  }, [immersiveActive])

  // Passive auth-status check. Reads /api/auth/providers/status with
  // a 5-minute TanStack cache + a visibilitychange-driven invalidation.
  // Drives the small warning dot on the drawer's Settings row when local
  // provider credentials are missing or their status cannot be checked.
  const providerAuth = useProviderAuthStatus()

  // The warm LRU is now maintained by the synchronous cache-derivation effect
  // above (keyed on visibleAppIds), which pins every visible app and retires an
  // evicted frame's history before unmount. No separate activeAppId-rotation
  // effect is needed.

  // New-app dot detection (state + open-clear live up beside the chat
  // attention machinery). First live list = the session baseline; anything
  // appearing after it is a genuine arrival and gets flagged.
  useEffect(() => {
    if (!appsLiveFetched) return
    const ids = apps.map(a => a.id)
    if (appBaselineRef.current === null) {
      appBaselineRef.current = new Set(ids.map(Number))
      return
    }
    const fresh = freshAppIds(appBaselineRef.current, ids)
    if (fresh.length === 0) return
    for (const id of fresh) appBaselineRef.current.add(id)
    setNewAppIds(prev => withAppsFlagged(prev, fresh))

    // Durable-list fallback for a live-preview event missed during reconnect.
    // Convert server relationships into the same pane-neutral
    // requests used by the live event path; the flat resolver is only today's
    // one-pane projection.
    const builtArrivals = freshChatBuiltApps(apps, fresh)
    if (builtArrivals.length > 0) {
      placeInWorkspace(workspaceRequestsForBuiltApps(builtArrivals))
    }
  }, [apps, appsLiveFetched, placeInWorkspace])

  usePushSubscription()

  // Stable refresh callbacks. Earlier versions used
  // `appsQuery.refetch` directly, but React Query returns a new
  // QueryObserverResult ref on every subscription tick — that made
  // these `useCallback`s recreate identity each render and made every
  // effect/caller that consumes them vulnerable to duplicate fetches.
  // Driving the refetch via the query client's stable
  // `refetchQueries` keeps the callback identity steady.
  const refreshApps = useCallback(() => {
    // Force a genuinely fresh fetch and return THAT fetch's result.
    // refetchQueries alone can coalesce with an initial mount fetch that's
    // still in flight (React Query dedups), then resolve against the stale
    // in-flight value — so a moebius:open-app that arrives while the apps
    // list is mid-load would read the pre-install list and wrongly conclude
    // the just-installed app "is not installed yet". cancelQueries aborts any
    // in-flight fetch first; fetchQuery(staleTime:0) then guarantees a new
    // request and returns its data directly (not a getQueryData re-read,
    // which can still observe the canceled fetch's stale snapshot).
    return queryClient.cancelQueries({ queryKey: appQueries.keys.all })
      .then(() => queryClient.fetchQuery({
        queryKey: appQueries.keys.all,
        queryFn: async () => reconcileApps(await appQueries.list.fetch()),
        staleTime: 0,
      }))
      .then(data => data || [])
      .catch(() => queryClient.getQueryData(appQueries.keys.all) || [])
  }, [queryClient, reconcileApps])
  const refreshChats = useCallback(() => {
    return queryClient.refetchQueries({ queryKey: chatQueries.keys.all })
      .then(() => queryClient.getQueryData(chatQueries.keys.all) || [])
      .catch(() => [])
  }, [queryClient])
  const projectChatList = useCallback((project) => {
    queryClient.setQueryData(chatQueries.keys.all, current => {
      const next = project(Array.isArray(current) ? current : [])
      chatsRef.current = next
      return next
    })
  }, [queryClient])
  const markChatOwnerActivity = useCallback((chatId) => {
    const at = new Date().toISOString()
    projectChatList(rows => withChatOwnerActivity(rows, chatId, at))
  }, [projectChatList])
  const markChatRunState = useCallback((chatId, running) => {
    const at = running ? new Date().toISOString() : null
    projectChatList(rows => withChatRunState(
      running ? withChatOwnerActivity(rows, chatId, at) : rows,
      chatId,
      running,
    ))
  }, [projectChatList])
  const applyChatRenameEvent = useCallback((event) => {
    projectChatList(rows => withChatRename(rows, event.chatId, {
      title: event.title,
      updatedAt: event.updatedAt,
    }))
  }, [projectChatList])

  const confirmChatDeleted = useCallback((id) => {
    const sid = String(id)
    rememberConfirmedDeletion(deletedChatIdsRef.current, sid)
    recentlyCreatedChatsRef.current.delete(sid)
    queryClient.setQueryData(chatQueries.keys.all, current => {
      const next = withoutConfirmedDeletions(
        Array.isArray(current) ? current : [],
        deletedChatIdsRef.current,
      )
      chatsRef.current = next
      return next
    })
  }, [queryClient])

  const confirmAppDeleted = useCallback((id) => {
    const sid = String(id)
    rememberConfirmedDeletion(deletedAppIdsRef.current, sid)
    queryClient.setQueryData(appQueries.keys.all, current => {
      const next = withoutConfirmedDeletions(
        Array.isArray(current) ? current : [],
        deletedAppIdsRef.current,
      )
      appsRef.current = next
      return next
    })
  }, [queryClient])

  const confirmChatRecovered = useCallback((id) => {
    forgetConfirmedDeletion(deletedChatIdsRef.current, id)
  }, [])

  const confirmChatIdentityIsLive = useCallback((id) => (
    forgetConfirmedDeletionIfExists(
      deletedChatIdsRef.current,
      id,
      chatId => probeDeletion(`/chats/${encodeURIComponent(chatId)}`),
    )
  ), [])

  const confirmAppRecovered = useCallback((id) => {
    forgetConfirmedDeletion(deletedAppIdsRef.current, id)
  }, [])

  const confirmAppIdentityIsLive = useCallback((id) => (
    forgetConfirmedDeletionIfExists(
      deletedAppIdsRef.current,
      id,
      appId => probeDeletion(`/apps/${encodeURIComponent(appId)}`),
    )
  ), [])

  const reconcileDeletedAppIdentities = useCallback(() => Promise.all(
    [...deletedAppIdsRef.current].map(confirmAppIdentityIsLive),
  ), [confirmAppIdentityIsLive])

  const reconcileDeletedChatIdentities = useCallback(() => Promise.all(
    [...deletedChatIdsRef.current].map(confirmChatIdentityIsLive),
  ), [confirmChatIdentityIsLive])

  const { openAppWithIntent, handleChatInternalNav } = useAppIntentNavigation({
    appsRef,
    refreshApps,
    showToast,
    setAppIntents,
    navToRef,
  })

  const handleNotificationOpen = useCallback((target) => {
    if (target?.view === 'canvas') {
      void openAppWithIntent(target.app, target.intent)
    } else if (target?.view === 'chat') {
      navToRef.current('chat', { chatId: target.chatId })
    }
  }, [openAppWithIntent])

  const coldDeepLinkHandledRef = useRef(false)
  useEffect(() => {
    if (coldDeepLinkHandledRef.current) return
    if (deepLink?.view !== 'canvas' || !deepLink.app) return
    coldDeepLinkHandledRef.current = true
    if (Number.isFinite(deepLink.appId)) {
      // useNavigation owns numeric cold-boot navigation and its single history
      // edge. Shell only queues the opaque intent for the already-opened app.
      const intent = typeof deepLink.intent === 'string' ? deepLink.intent.trim() : ''
      if (intent) {
        setAppIntents((prev) => ({
          ...prev,
          [String(deepLink.appId)]: { intent, nonce: Date.now() },
        }))
      }
      return
    }
    // Navigation cannot resolve a slug without the apps list. If that requires
    // a refresh, abandon the delayed open after any intervening shell route.
    const startedAtEpoch = navigationEpochRef.current
    void openAppWithIntent(
      deepLink.app,
      deepLink.intent,
      () => navigationEpochRef.current === startedAtEpoch,
    )
  }, [navigationEpochRef, openAppWithIntent])

  // Route a mini-app crash report to the chat that built the app (its
  // `chat_id`), falling back to a new chat when that chat was deleted. The
  // report is set as a DRAFT (not auto-sent) so the owner reviews before
  // sending. AppCanvas forwards ONLY its LIVE frame's app-error here (it
  // swallows a hidden incoming preview frame's), so there is no window-level
  // e.source guard to make — source attribution now lives entirely in
  // AppCanvas. This stable callback reads the live apps/chats through
  // refs and calls the current newChat through `newChatRef`, so its identity
  // never changes and AppCanvas's message listener (which deps on it) never
  // re-registers. Its dependencies are stable owners rather than render data.
  const handleAppError = useCallback((appId, error, chatId) => {
    const appEntry = appsRef.current.find(a => String(a.id) === String(appId))
    const appName = appEntry?.name || `app ${appId}`
    const report = `The app "${appName}" crashed with this error:\n\`\`\`\n${error}\n\`\`\`\nPlease investigate and fix.`
    const buildingChatId = appEntry?.chat_id || chatId || null
    const buildingChat = buildingChatId
      && chatsRef.current.find(c => c.id === buildingChatId)
    if (buildingChat) {
      stageComposerHandoff(buildingChatId, report)
      // Open the building chat in the crashed app's OWN pane (fallback: focused
      // pane) so a background app's crash report lands beside it (contract §1.4.7).
      const ownerPane = paneModel.paneOf(
        workspaceStateRef.current.ws,
        tabModel.tabKey(tabModel.makeTab('app', appId)),
      )
      navToRef.current('chat', { chatId: buildingChatId, paneId: ownerPane?.id })
      requestComposer(buildingChatId, { draft: report })
      refreshChats()
    } else {
      newChatRef.current?.({ draft: report, forceNew: true })
    }
  }, [refreshChats, requestComposer, workspaceStateRef])

  // AppCanvas owns exact-window attribution and wire-format narrowing for
  // every frame request. This callback owns the workspace outcome only, so the
  // standalone host and workspace cannot drift into separate message routers.
  const handleAppHostRequest = useCallback((_appId, request) => {
    void (async () => {
      if (request.type === 'moebius:new-chat') {
        await newChatRef.current?.({
          draft: request.draft || undefined,
          forceNew: true,
          autoSend: request.autoSend,
        })
        return
      }
      if (request.type === 'moebius:open-chat') {
        const draftText = request.draft || null
        // Do not stage a dead chat and wait for ChatView's later 404 repair:
        // that briefly paints a destination which immediately disappears. The
        // direct resource probe is the shell's authoritative deletion signal.
        // Unknown (offline/timeout/auth) is deliberately not deletion evidence.
        const targetState = await probeDeletion(
          `/chats/${encodeURIComponent(request.chatId)}?limit=1&compact=1`,
        )
        if (targetState === 'deleted') {
          await newChatRef.current?.({
            draft: draftText || undefined,
            forceNew: true,
          })
          refreshChats()
          return
        }
        if (draftText != null) {
          stageComposerHandoff(request.chatId, draftText)
        }
        navToRef.current('chat', { chatId: request.chatId })
        // Storage covers an unmounted target. The explicit request also updates
        // an already-retained ChatView, whose controlled composer state would
        // otherwise keep showing its old value until a full remount.
        if (draftText != null) requestComposer(request.chatId, { draft: draftText })
        refreshChats()
        return
      }
      if (request.type === 'moebius:open-app') {
        await openAppWithIntent(request.appId, request.intent)
        return
      }
      const section = APP_SETTINGS_SECTIONS.has(request.section)
        ? request.section
        : 'ai-providers'
      setSettingsFocusTarget({ section, nonce: Date.now() })
      if (activeViewRef.current !== 'settings') navToRef.current('settings')
    })()
  }, [openAppWithIntent, refreshChats, requestComposer])

  // Restore the active chat after Shell mount. Two cache layers can
  // satisfy this effect: (1) the persisted TanStack cache hydrated
  // from IndexedDB (flips `isFetched` to true with `dataUpdatedAt`
  // from the prior session), and (2) the live network fetch.
  //
  // If `prev` (the localStorage-restored activeChatId) is present in
  // the current `chats` list, we keep it immediately — both cache
  // layers agree and there's nothing to wait for. The user's chat
  // stays mounted and ChatView's spacer/scroll restore proceeds
  // without remounting.
  //
  // If `prev` is NOT in the list, we MUST distinguish "the chat
  // genuinely no longer exists" from "the persisted cache is stale
  // and hasn't seen the live list yet". Demoting to chats[0]
  // prematurely (on the stale-cache path) silently switches the
  // user to a different chat, remounts ChatView under a new key,
  // and destroys the spacer state from the previous session.
  // Gate the demotion on `isSuccess && isFetchedAfterMount` — both
  // conditions mean the live fetch has resolved at least once since
  // this Shell mounted. `isFetchedAfterMount` is TanStack's
  // observer-mount-vs-fetch-completion bool, semantically exact for
  // this need. The prior heuristic was `dataUpdatedAt > mountTime`,
  // which was clock-fragile: a same-tick fast response made the
  // strict `>` permanently false, trapping fresh containers in a
  // no-chat / no-ChatView state. The fragility went unnoticed until
  // the offline-feature merge added a SW SWR cache on `/api/chats`,
  // which made same-tick responses the common case and broke
  // auth.setup.mjs on every CI push afterward. Bootstrap (`prev ===
  // null`) is fine to run from either cache layer; ChatView only
  // mounts when a real chatId is set, so there's no premature-
  // remount cost.
  //
  // chatsLoadedRef gates the bootstrap-empty-chat effect below. We
  // flip it as soon as `isFetched` is true (regardless of cache
  // layer): the bootstrap effect's own check (chats.length === 0 &&
  // activeChatId === null) is conservative enough — if persisted
  // chats happen to be empty AND activeChatId is null AND the live
  // fetch confirms the same, creating a bootstrap chat is correct.
  // Holding chatsLoadedRef past first hydration would just delay an
  // already-correct call.
  //
  // Defensive refetch: TanStack's default refetchOnMount + staleTime
  // (30s in queryClient.js) can leave the persisted snapshot serving
  // beyond a reload — if the snapshot was written <30s before the
  // reload, the on-mount refetch is skipped as "fresh". When `prev`
  // isn't in that snapshot, we'd otherwise wait forever for a live
  // confirmation that never comes. Force a refetch in that case so
  // `isFetchedAfterMount` eventually flips and demotion (or
  // confirmation) actually runs.
  useEffect(() => {
    if (!chatsQuery.isFetched) return
    const liveFetched = chatsQuery.isSuccess
      && chatsQuery.isFetchedAfterMount
    const prev = activeChatIdRef.current
    const prevInChats = prev && chats.some(c => c.id === prev)
    if (prevInChats) {
      // Cached data shows `prev` is valid. Keep it mounted as-is so
      // ChatView's scroll/spacer restore proceeds without remounting.
      // BUT: if we're still on stale-cache hydration (not liveFetched),
      // also nudge a refetch — the persisted snapshot can be a stale
      // FALSE POSITIVE too (a chat the user deleted in another tab
      // before reload still appears in the cache). Without the nudge,
      // ChatView would mount on `prev`, fetch `/api/chats/{prev}`,
      // 404, and show an error state for the full 30s staleTime
      // window. The nudge resolves the situation in one round-trip.
      knownExistingOffListChatIdsRef.current.delete(prev)
      if (!liveFetched && !chatsQuery.isFetching) refreshChats()
      chatsLoadedRef.current = true
      return
    }
    if (!prev) {
      // No restored chat target. A null single slot is a deliberate New Chat
      // destination even when historical chats exist; never replace it with chats[0].
      // Builder mode retains its legacy seed into an actually empty focused pane.
      // A zero-chat install waits for the live-confirmed bootstrap effect below so a
      // stale empty list cannot manufacture a server row.
      const ws = workspaceStateRef.current.ws
      const single = ws.viewMode === 'single'
      const focusedPaneEmpty = !ws.panes[ws.focusedPaneId]?.activeTabKey
      if (single && ws.singleScreen == null && chats.length > 0
          && pendingNewChatRef.current == null) {
        requestEmptySingleNewChat()
      } else if (!single && focusedPaneEmpty && chats[0]) {
        applyModeDestination({ view: 'chat', chatId: chats[0].id, appId: null, paneId: ws.focusedPaneId }, { preserveSettings: true })
      }
      chatsLoadedRef.current = true
      return
    }
    if (!liveFetched) {
      // Persisted snapshot is missing `prev` but we haven't heard
      // from the server yet. Hold `prev` as a tentative restore —
      // ChatView mounts on it, and if it's gone server-side, the
      // 404 from ChatView's own fetch surfaces a retryable error
      // instead of a silent chat-switch. Nudge the chats query in
      // case TanStack's staleTime (30s in queryClient.js) skipped
      // the on-mount refetch — without that nudge a fresh persisted
      // snapshot pins us here indefinitely.
      if (!chatsQuery.isFetching) refreshChats()
      chatsLoadedRef.current = true
      return
    }
    if (knownExistingOffListChatIdsRef.current.has(prev)) {
      chatsLoadedRef.current = true
      return
    }

    // Drawer-list absence is not deletion evidence: /api/chats is a filtered view
    // that hides app-attributed chats and can lag a new chat, and (like every list
    // route) is NetworkFirst, so a stale SW cache fallback reads like live data. Per
    // the platform DELETION-EVIDENCE CONTRACT (probeDeletion), only a direct
    // /api/chats/{id} 404 proves the restored target should be demoted — the same
    // contract the slot-app reconcile above uses, applied to chats.
    let cancelled = false
    const probedChatId = prev
    ;(async () => {
      const verdict = await probeDeletion(`/chats/${encodeURIComponent(probedChatId)}?limit=1`)
      // Stale-guard: the active chat can change while the probe is in flight, so a
      // verdict for an old restore target must never navigate.
      if (cancelled || activeChatIdRef.current !== probedChatId) return
      if (verdict === 'deleted') {
        knownExistingOffListChatIdsRef.current.delete(probedChatId)
        // The restored chat is genuinely gone: close its tab in its pane. Builder
        // mode may seed a surviving chat into an empty root; an emptied single slot
        // is the explicit New Chat destination owned by dispatchWorkspace.
        dispatchWorkspace({
          type: 'CLOSE_TAB',
          tabKey: tabModel.tabKey(tabModel.makeTab('chat', probedChatId)),
          reason: 'deleted',
        })
        const ws = workspaceStateRef.current.ws
        const single = ws.viewMode === 'single'
        const builderEmpty = !single && !ws.panes[ws.focusedPaneId]?.activeTabKey
        const fallback = chats.find(c => c.id !== probedChatId)
        if (builderEmpty && fallback) {
          // R1: a background 404-repair preserves an open Settings takeover — it seeds
          // the visible slot beneath it rather than dismissing the owner's Settings view.
          applyModeDestination({ view: 'chat', chatId: fallback.id, appId: null, paneId: ws.focusedPaneId }, { preserveSettings: true })
        }
      } else if (verdict === 'exists') {
        // Present but unlisted because it is app-attributed or the drawer list is
        // lagging a fresh chat. Memoize only the positive off-list result so future
        // list refetches do not repeatedly probe it.
        knownExistingOffListChatIdsRef.current.add(probedChatId)
      }
      // 'unknown' (offline / timeout / non-404) is not deletion evidence, so the
      // restored target stays mounted until a later list refetch retries the probe.
      chatsLoadedRef.current = true
    })()
    return () => { cancelled = true }
  }, [chats, chatsQuery.isFetched, chatsQuery.isSuccess,
      chatsQuery.isFetchedAfterMount, chatsQuery.isFetching,
      refreshChats, dispatchWorkspace, applyModeDestination,
      requestEmptySingleNewChat, workspaceStateRef, activeChatIdRef])

  // Deferred shell-update pickup: a service worker that finished installing and
  // is now WAITING (leashed — it never took over on its own), or index.html's
  // boot-time stale-precache flag. Route it through the SAME hold-until-idle
  // path as a live shell_rebuilt (requestShellReload → apply if idle, else hold
  // the reload until the running turn ends). This recovers a lost apply race:
  // the SW generation that installed just after an earlier apply signal, a
  // stale precache the boot check spotted, or an ACTIVE worker newer than the
  // page's controller (feature 207 — reg.waiting is null in that settled
  // state, so a waiting-only check misses it). Gate on a live-confirmed chats
  // list, so streamingChatIds reflects any running background turn — a cold mount's
  // empty pre-fetch list would otherwise read as idle and reload straight
  // through a reconnecting turn. Runs at most once per mount. Do not key this
  // recovery on TanStack's observer-relative `isFetchedAfterMount`: a fetch can
  // complete in the same mount turn (especially through the SW cache) without
  // that observer flag producing another usable effect pass. Instead, force one
  // staleTime:0 query completion here, then yield a task so the query observer
  // has committed the fresh durable run set before requestShellReload reads its
  // refs. This is both a live-confirmation gate and deterministic mount pickup.
  useEffect(() => {
    if (shellUpdatePickupRef.current || shellUpdatePickupCheckStartedRef.current) return
    if (!chatsQuery.isSuccess) return
    shellUpdatePickupCheckStartedRef.current = true
    let cancelled = false
    ;(async () => {
      // Snapshot the stale-generation signal before the live chat query. A
      // waiting worker can activate and claim this page while that fetch is in
      // flight; active === controller would then make a later re-check look
      // current even though this document is still executing the old bundle.
      let flagged = false
      try { flagged = sessionStorage.getItem('sw-stale-precache-pending') === '1' } catch { /* ignore */ }
      let rearm = flagged
      if (navigator.serviceWorker?.getRegistration) {
        try {
          const reg = await navigator.serviceWorker.getRegistration()
          rearm = shouldRearmShellApply({
            stalePrecacheFlagged: flagged,
            waiting: reg?.waiting || null,
            active: reg?.active || null,
            controller: navigator.serviceWorker.controller || null,
          })
        } catch { /* ignore */ }
      }
      if (cancelled || !rearm) return
      try {
        await queryClient.fetchQuery({
          queryKey: chatQueries.keys.all,
          queryFn: chatQueries.list.fetch,
          staleTime: 0,
        })
      } catch {
        // A failed live confirmation is not permission to reload through a
        // possibly-running turn. A later mount/online recovery can try again.
        return
      }
      await new Promise(resolve => setTimeout(resolve, 0))
      if (cancelled) return
      shellUpdatePickupRef.current = true
      // requestShellReload reads streaming/view state from refs at call time, so
      // the captured closure is fresh even though it isn't in this effect's deps.
      // This is recovery, not watcher noise: the page has just mounted and a
      // waiting/mismatched worker must not remain stranded behind a restored
      // chat (especially when another tab keeps the outgoing worker alive).
      requestShellReload()
    })()
    return () => {
      cancelled = true
      // React StrictMode immediately runs mount effects through one synthetic
      // setup/cleanup cycle. Let the real setup own the check when that first
      // async pass was cancelled before it could claim the pickup.
      if (!shellUpdatePickupRef.current) shellUpdatePickupCheckStartedRef.current = false
    }
  }, [chatsQuery.isSuccess, queryClient])

  // Foreground-return shell-update pickup. The boot re-arm net above runs once per
  // MOUNT, and a live `shell_rebuilt` reaches only a page with a live EventSource.
  // An installed PWA BACKGROUNDED across a deploy hits neither: it misses the
  // transient broadcast (its stream was suspended and the event is not replayed on
  // reconnect) and never re-mounts, so it keeps running the OLD bundle until a cold
  // start — the "still broken after the deploy" report from a warm install. This
  // watch is the missing apply trigger: on every return to visible (and on
  // regaining connectivity) it forces a fresh sw.js fetch and, once a newer
  // generation is waiting/mismatched, routes it through the SAME apply-on-idle
  // reload as a live shell_rebuilt — silent, and deferred while a turn streams or
  // the owner is typing (requestShellReload reads streaming/view state from refs,
  // so this closure staying out of the deps is correct). Gated by
  // shouldRearmShellApply inside the watch, so a return with no new generation is a
  // no-op — no toast, no spurious reload.
  useEffect(() => watchForShellUpdateOnForeground({
    doc: typeof document !== 'undefined' ? document : null,
    win: typeof window !== 'undefined' ? window : null,
    serviceWorker: typeof navigator !== 'undefined' ? navigator.serviceWorker : null,
    readStaleFlag: () => {
      try { return sessionStorage.getItem('sw-stale-precache-pending') === '1' } catch { return false }
    },
    rearm: () => requestShellReload({ passive: true }),
  }), [])

  // Handle non-content SSE events: theme changes, app updates, shell rebuilds.
  const handleSystemEvent = useCallback((ev) => {
    if (ev.type === 'theme_updated') {
      // Theme is dynamic in iframes since the token-free frame
      // refactor: AppCanvas re-broadcasts the theme via
      // `moebius:frame-theme` postMessage on every theme change,
      // and the frame applies it without remounting. We do NOT need
      // to bump appVersions / cycle iframe keys — that would tear
      // down running apps for a CSS swap and lose their state.
      loadTheme()
    } else if (ev.type === 'app_activity') {
      // The durable marker was committed with an app-attributed notification.
      // A refetch surfaces the dot; if the app is already visible, the effect
      // above immediately acknowledges it instead of leaving a stale nudge.
      void invalidateShellListCache('apps').then(refreshApps)
    } else if (ev.type === 'chat_deleted') {
      // Exact mutation evidence from this or another live tab. Update the
      // in-memory drawer synchronously; the normal missing-active-chat effect
      // owns any route/view repair in tabs that happened to have it open.
      if (ev.chatId) confirmChatDeleted(ev.chatId)
      void invalidateShellListCache('chats')
    } else if (ev.type === 'chat_recovered') {
      // Recovery is the sole operation allowed to clear the session tombstone.
      if (ev.chatId) confirmChatRecovered(ev.chatId)
      void invalidateShellListCache('chats').then(refreshChats)
    } else if (ev.type === 'chat_renamed') {
      // The committed event carries the exact changed row fields. Apply those
      // in place so renaming one chat cannot parse and reconcile all hundreds
      // of drawer rows underneath typing/scrolling. Drop the offline fallback
      // copy asynchronously; the next ordinary reconnect can repopulate it.
      applyChatRenameEvent(ev)
      void invalidateShellListCache('chats')
    } else if (ev.type === 'app_deleted') {
      if (ev.appId) confirmAppDeleted(ev.appId)
      void invalidateShellListCache('apps')
    } else if (ev.type === 'app_recovered') {
      if (ev.appId) confirmAppRecovered(ev.appId)
      void invalidateShellListCache('apps').then(refreshApps)
    } else if (
      ev.type === 'app_updated'
      || ev.type === 'app_created'
      || ev.type === 'app_preview_ready'
    ) {
      const placementRequest = workspaceRequestFromSystemEvent(ev)
      // app_updated is also the reinstall event for a tombstoned store app,
      // while app_created may carry an integer id freed by TTL purge and reused
      // for a different installation. A direct resource probe—not a staleable
      // list—is the proof that either id is live again.
      const reconcileIdentity = ev.appId
        ? confirmAppIdentityIsLive(ev.appId)
        : Promise.resolve(false)
      // Refresh server truth before warming or placing. app_updated/app_created
      // remain lifecycle refreshes; app_preview_ready is the explicit
      // build-session action that reveals either a new app or an updated one.
      // `updated_at` drives the iframe live-swap and derived built-app CTA, so
      // neither needs a separate client mirror.
      Promise.all([
        invalidateShellListCache('apps'),
        reconcileIdentity,
      ]).then(() => refreshApps()).then(updatedApps => {
        // Warm the SW cache for the updated app immediately — the edit
        // rotated the `?v=` cache key, so without this the next open pays
        // the network round trip. Every app's read path is cached now
        // (not just offline-capable ones), so no flag gate here.
        if (ev.appId) {
          const app = updatedApps.find(a => String(a.id) === String(ev.appId))
          if (app) warmAppCode(app)
        }
        // A live-preview event is emitted only after a coherent revision
        // committed. Confirm both named resources before honoring it. The
        // requesting chat is deliberately NOT compared with app.chat_id: an
        // existing app keeps its original ownership/error-routing relationship
        // while a later chat may be the one modifying it.
        if (placementRequest) {
          const app = updatedApps.find(a => (
            String(a.id) === placementRequest.item.id
          ))
          if (app) {
            refreshChats().then(updatedChats => {
              const chatExists = updatedChats.some(
                chat => String(chat.id) === placementRequest.source.id,
              )
              if (chatExists) placeInWorkspace(placementRequest)
            })
          }
        }
      })
    } else if (ev.type === 'open_item') {
      // An explicit agent-initiated open (design §6.3), system-bus-only so it
      // fires exactly once. Confirm the item actually exists in fresh server
      // truth before placing — mirror the live-preview confirm-guard so a
      // spoofed or absent id is a silent no-op. App items also warm their cache.
      const request = workspaceRequestFromSystemEvent(ev)
      if (request) {
        // A background open lands as an inactive tab, so it earns the drawer/tab
        // "new content" dot (design §6.2). Foreground opens are on screen → none.
        const attn = attentionForRequest(request)
        const confirmAndPlace = async () => {
          if (request.item.kind === 'app') {
            const updatedApps = await refreshApps()
            const app = updatedApps.find(a => String(a.id) === request.item.id)
            if (!app) return
            warmAppCode(app)
          } else {
            const updatedChats = await refreshChats()
            if (!updatedChats.some(c => String(c.id) === request.item.id)) return
          }
          // Reuse the app_created / chat-attention plumbing for the background dot.
          if (attn?.kind === 'app') {
            setNewAppIds(prev => withAppsFlagged(prev, [attn.id]))
          } else if (attn?.kind === 'chat') {
            setAttentionChatIds(prev => {
              if (prev.has(attn.id)) return prev
              const next = new Set(prev)
              next.add(attn.id)
              return next
            })
          }
          placeInWorkspace(request)
        }
        confirmAndPlace()
      }
    } else if (ev.type === 'app_build_failed') {
      // Explicit apply reports compile failures synchronously to its caller and
      // keeps the previous app version live. A legacy/external diagnostic must
      // not cover the composer; actionable update drift uses app_update_stale.
      return
    } else if (ev.type === 'app_update_stale') {
      // The reviewed candidate changed while a conflict was being resolved.
      // Keep the prior live version explicit and take the owner back to the
      // canonical review surface when the bootstrapped store is available.
      const appStore = findAppStoreApp(appsRef.current)
      showToast(appUpdateStaleMessage(ev), {
        variant: 'error',
        duration: 12000,
        action: appStore ? {
          label: 'Open App Store',
          onAction: () => navToRef.current('canvas', { appId: appStore.id }),
        } : undefined,
      })
    } else if (ev.type === 'chat_run_started') {
      if (ev.chatId) {
        markChatRunActivity(ev.chatId)
        markStreamingStart(ev.chatId)
        markChatRunState(ev.chatId, true)
      }
    } else if (ev.type === 'chat_run_finished') {
      const chatId = ev.chatId
      if (chatId) {
        // Finish is activity too: if start was missed during a reconnect, or
        // both events batch together, the active ChatView still fetches the
        // final durable transcript.
        markChatRunFinished(chatId)
        markStreamingEnd(chatId)
        markChatRunState(chatId, false)
        // Attention iff the finished chat is NOT visible in ANY pane — membership
        // in the visible set, not equality with one global id, so a chat visible
        // in a background split gets no false dot (finding D-iii).
        if (!visibleChatIdsRef.current.has(String(chatId))) {
          // Do not fetch or parse the hidden transcript here. Several agents can
          // finish while the owner is reading another chat, and those unsolicited
          // detail responses contend with the first native scroll frame. The
          // retained ChatView consumes this run signal when it becomes visible;
          // an unmounted chat uses the existing versioned activation read.
          setAttentionChatIds(prev => {
            if (prev.has(chatId)) return prev
            const next = new Set(prev)
            next.add(chatId)
            return next
          })
        }
      }
    } else if (ev.type === 'shell_rebuilt' || ev.type === 'shell_apply_now') {
      // A new shell generation is available. `shell_rebuilt` fires automatically
      // when the frontend rebuilds; `shell_apply_now` is the agent's EXPLICIT
      // "look now" signal (design §1.5). A watcher rebuild is passive and
      // coalesces while an idle chat is visible; apply-now is deliberate and
      // uses the ordinary apply-on-idle policy. This prevents source-save
      // bursts from repeatedly refreshing a transcript someone is reading.
      //
      // These are system-bus-only (frontend_watcher / notify skip the per-chat
      // fan-out) and SystemBroadcast has no replay, so each reaches the Shell
      // exactly once — no dedup stamp needed to avoid reload loops.
      //
      // Apply-on-idle: the streaming view is sacred. requestShellReload reads
      // view + streaming state from refs (not closure-captured scalars, which
      // can lag concurrent updates by a render) and applies immediately when
      // idle, or holds the refresh quietly until the page is idle when the
      // owner is typing, steering, or reading a running chat
      // (shellReloadPolicy.shouldDeferShellReload) — no focus stealing. The SW
      // leash rides the same moment: performShellReload posts SKIP_WAITING to
      // the waiting worker so the SW generation flips exactly when the page
      // reloads.
      if (ev.type === 'shell_apply_now') {
        setSettingsRefreshToken(token => token + 1)
      }
      requestShellReload({ passive: ev.type === 'shell_rebuilt' })
    } else if (ev.type === 'shell_rebuild_failed') {
      // Deliberately silent in the owner UI. The atomic publisher keeps the
      // previous shell running, and watcher failures commonly describe a
      // transient intermediate state during a multi-file agent edit. The
      // producer logs the diagnostic and retries; an explicit operation such
      // as a platform update reports its own failure where it was initiated.
    } else if (ev.type === 'notification_created') {
      // The event is only a nudge; the durable list/count remain authoritative.
      // Keeping this behind the notification-center interface prevents the
      // shell event switch from learning preview implementation details.
      onNotificationCreated()
    }
  }, [
    // Scalar state removed: shell_rebuilt now reads from refs (activeViewRef,
    // activeAppIdRef, activeChatIdRef, drawerOpenRef) so stale closure values
    // can't be serialized. Refs themselves don't need to be in deps (they're
    // stable objects whose .current is read at call time, not at capture time).
    applyChatRenameEvent,
    confirmAppDeleted, confirmAppIdentityIsLive, confirmAppRecovered,
    confirmChatDeleted, confirmChatIdentityIsLive, confirmChatRecovered,
    loadTheme, markChatRunActivity, markChatRunFinished,
    markChatRunState, markStreamingEnd, markStreamingStart,
    onNotificationCreated, placeInWorkspace, queryClient,
    refreshApps, refreshChats, warmAppCode,
  ])

  // Shell-level SSE subscription for system events. Stays open for
  // the lifetime of the Shell so theme/app/shell-rebuild updates
  // reach handleSystemEvent regardless of which view the user is on.
  // The active chat's SSE stream still forwards the same events for
  // in-chat catch-up coherence — handlers are idempotent (theme
  // reload, refreshApps, version bump) so the duplicate is harmless.
  // A system-bus event can be lost while the stream is disconnected. Refetch
  // the durable app list after every initial connection/reconnect; after the
  // first list establishes the session baseline, fresh chat-owned rows flow
  // through the same idempotent placement resolver as live app_preview_ready events.
  const reconcileSystemStateOnOpen = useCallback(() => {
    reconcileNotifications()
    void Promise.all([
      invalidateShellListCache('apps'),
      invalidateShellListCache('chats'),
      reconcileDeletedAppIdentities(),
      reconcileDeletedChatIdentities(),
    ]).then(() => {
      refreshApps()
      refreshChats()
    })
  }, [
    reconcileNotifications,
    reconcileDeletedAppIdentities,
    reconcileDeletedChatIdentities,
    refreshApps,
    refreshChats,
  ])
  useSystemEventStream(handleSystemEvent, { onOpen: reconcileSystemStateOnOpen })

  // Service-worker messages arrive on navigator.serviceWorker, not the window
  // message bus used by AppCanvas. Keep this listener limited to notification
  // routing; frame requests are source-attributed and normalized by AppCanvas.
  useEffect(() => {
    function onSwMessage(e) {
      // Service-worker client.postMessage delivers here via
      // navigator.serviceWorker — NOT via window.message. (Subtle
      // browser API split: the SW spec routes them through the SW
      // container, not the global.) sw.js fires this on
      // notificationclick when an existing client is focused.
      if (e.data?.type !== 'notification-click') return
      const target = parseNotificationTarget(e.data.target)
      if (target?.view === 'canvas') void openAppWithIntent(target.app, target.intent)
      else if (target?.view === 'chat') navTo('chat', { chatId: target.chatId })
    }

    if (navigator.serviceWorker) {
      navigator.serviceWorker.addEventListener('message', onSwMessage)
    }
    return () => {
      if (navigator.serviceWorker) {
        navigator.serviceWorker.removeEventListener('message', onSwMessage)
      }
    }
  }, [navTo, openAppWithIntent])

  // Resolve the chat id a New-chat action lands on: a validated reusable empty row, or
  // a freshly created one. Split out of newChat (round 4 item 3) so BOTH the ordinary
  // user navigation (newChat) and the deferred slot materialization
  // (materializeNewChatHome) share ONE reuse-and-create policy. Returns
  // { chatId, reason }: reason is 'offline' | 'inflight' | 'error' when chatId is null,
  // so each caller can react appropriately (a toast vs a retry surface).
  //
  // `candidate`: an explicitly pre-captured reusable row (the materialize path, which
  // captured it from the pre-transition active chat). When undefined, derive it fresh
  // from the current active chat (the user newChat path). The list is only a candidate
  // source — cross-client sends can make has_messages stale — so online reuse needs one
  // fresh, bounded detail read; any error/unfamiliar response fails closed to creating.
  async function resolveNewChatId({ candidate, draft, forceNew, exclude } = {}) {
    let empty = candidate !== undefined
      ? candidate
      : currentReusableEmptyChat(chatsRef.current, {
        activeChatId: activeChatIdRef.current,
        draft: !!draft,
        exclude,
        forceNew: !!forceNew,
        recoveredChatIds: recoveredChatIdsRef.current,
        streamingChatIds: streamingChatIdsRef.current,
      })
    if (empty && online) {
      try {
        const staleEmptyId = empty.id
        const res = await apiFetch(
          `/chats/${encodeURIComponent(empty.id)}?limit=1`,
          { timeoutMs: 5000 },
        )
        let detail = null
        if (res.ok) detail = await res.json()
        const verdict = reusableChatDetailVerdict({
          ok: res.ok,
          status: res.status,
          detail,
        })
        if (verdict !== 'empty') {
          empty = null
          reconcileCreatedChatGuard(
            recentlyCreatedChatsRef.current,
            staleEmptyId,
            verdict,
          )
          if (verdict === 'missing') {
            // A 404 is authoritative deletion, not evidence of content.
            knownExistingOffListChatIdsRef.current.delete(String(staleEmptyId))
            queryClient.setQueryData(chatQueries.keys.all, current => {
              if (!Array.isArray(current)) return current
              const next = current.filter(
                chat => String(chat.id) !== String(staleEmptyId),
              )
              chatsRef.current = next
              return next
            })
          } else if (verdict === 'occupied') {
            // The complete successful detail read has given us the only fact
            // New Chat needs: this row is no longer reusable. Publish that
            // narrow correction instead of launching a drawer list beside the
            // create request. Uncertain/malformed responses leave it unchanged.
            queryClient.setQueryData(chatQueries.keys.all, current => {
              if (!Array.isArray(current)) return current
              const next = current.map(chat => (
                String(chat.id) === String(staleEmptyId)
                  ? { ...chat, has_messages: true }
                  : chat
              ))
              chatsRef.current = next
              return next
            })
          }
        }
      } catch {
        empty = null
      }
    }
    if (empty) return { chatId: empty.id, reason: null }
    // Creating a fresh chat needs the server (POST allocates the row, and a chat is
    // only useful once the server-side agent can run). The reuse branch already handled
    // the offline-friendly case, so reaching here offline means we truly need network.
    if (!online) return { chatId: null, reason: 'offline' }
    // Spam-click guard: when no empty exists, two rapid taps would race two POSTs and
    // leave an extra empty behind. The in-flight ref short-circuits until the first
    // resolves — the caller acknowledges the tap without a second create.
    if (creatingChatRef.current) return { chatId: null, reason: 'inflight' }
    creatingChatRef.current = true
    try {
      // Opening the drawer may already have started a list read whose snapshot
      // predates this POST. Cancel it before creation so it cannot land later
      // and overwrite the optimistic row with a stale list. fetchChats consumes
      // TanStack's AbortSignal, making this a real network cancellation rather
      // than merely ignoring the query result.
      await queryClient.cancelQueries({
        queryKey: chatQueries.keys.all,
        exact: true,
      })
      const res = await api.chats.create({ title: 'New chat' })
      const chat = await jsonOrThrow(res, 'Chat creation failed')
      rememberCreatedChat(recentlyCreatedChatsRef.current, chat)
      const detailCache = createdChatDetailCache(chat)
      if (detailCache) {
        queryClient.setQueryData(chatMessagesQueryKey(chat.id), detailCache)
      }
      queryClient.setQueryData(chatQueries.keys.all, current => {
        const next = addCreatedChatToList(current, chat)
        chatsRef.current = next
        return next
      })
      // Navigation, drawer membership, and first paint all come from the
      // authoritative create response. Do not immediately replace it with a
      // second list read; ordinary drawer/run events revalidate later.
      return { chatId: chat.id, reason: null }
    } catch {
      return { chatId: null, reason: 'error' }
    } finally {
      creatingChatRef.current = false
    }
  }

  // Materialize the deferred New Chat landing into a real chat slot (round 4 item 3).
  // Runs ONLY after the visual scene idles (the watcher below gates it). Stale-guarded
  // against a superseding request, a re-toggle back to builder, and a slot filled by
  // another path. On offline/failed creation it leaves the New Chat landing visible
  // with a retry affordance — never a blank <main>, never chats[0].
  async function materializeNewChatHome(pending) {
    if (materializingNewChatRef.current) return
    materializingNewChatRef.current = true
    try {
      // Re-look-up the captured candidate by id (the list may have changed since the
      // request). Missing → no reuse, straight to create. Explicit candidate (may be
      // null) so resolveNewChatId does not re-derive from the now-different active chat.
      const candidate = pending.candidateId != null
        ? (chatsRef.current.find(c => String(c.id) === String(pending.candidateId)) || null)
        : null
      const { chatId, reason } = pending.resolvedChatId != null
        ? { chatId: pending.resolvedChatId, reason: null }
        : await resolveNewChatId({ candidate })
      // Stale-guard: if a newer empty-single request arrived during the await, hand
      // it this already-validated/created untouched row. That preserves latest-token
      // ownership without abandoning a server row or issuing a duplicate POST.
      if (newChatRequestSeqRef.current !== pending.token) {
        const latest = pendingNewChatRef.current
        if (chatId != null && latest
            && latest.token === newChatRequestSeqRef.current
            && latest.resolvedChatId == null) {
          latest.resolvedChatId = chatId
        }
        return
      }
      // Preserve a successfully resolved row if a beat began during the await. The
      // watcher will retry after that beat without issuing another detail/POST call.
      if (chatId != null) pending.resolvedChatId = chatId
      const ws = workspaceStateRef.current.ws
      const single = ws.viewMode === 'single'
      if (!single || ws.singleScreen != null) {
        if (pendingNewChatRef.current && pendingNewChatRef.current.token === pending.token) {
          pendingNewChatRef.current = null
        }
        return
      }
      // Keep the request (and any resolved row) while a newer beat is live. Clearing
      // it here strands the landing because the descriptor-idle watcher has nothing
      // left to resume.
      if (modeTransitionRef.current) return
      if (chatId == null) {
        // offline / failed — keep the landing + the pending request for a retry.
        setNewChatLandingFailure(reason === 'offline' ? 'offline' : 'error')
        return
      }
      pendingNewChatRef.current = null
      setNewChatLandingFailure(null)
      // Guarded, history-free slot write: applyModeDestination never pushes history;
      // preserveSettings so a background repair doesn't yank an open Settings takeover;
      // no composer focus — a mode toggle must not summon the mobile keyboard.
      applyModeDestination(
        { view: 'chat', chatId, appId: null, paneId: ws.focusedPaneId },
        { preserveSettings: true },
      )
    } finally {
      materializingNewChatRef.current = false
      const latest = pendingNewChatRef.current
      if (latest && latest.token !== pending.token) {
        setMaterializeNewChatRevision(revision => revision + 1)
      }
    }
  }

  async function newChat({ draft, forceNew, exclude, autoSend, focusComposer, recordHistory } = {}) {
    // Keep the active chat when it is still an untouched blank; only POST a
    // fresh row when this explicit New-chat action needs one. Never borrow an
    // off-screen blank: another browser may have started it while this tab's
    // chat-list cache still says has_messages=false.
    //
    // `forceNew` bypasses reuse for callers that NEED a fresh row —
    // moebius:new-chat events (the ChatView wouldn't remount on the
    // same chatId, so the pending-draft useState initializer wouldn't
    // run) and the app-crash routing (the report draft is keyed to a
    // fresh chat). Also used below to distinguish user-initiated calls
    // from automatic ones (bootstrap, deletion-induced re-create) for
    // the nav-stack push.
    //
    // Resolve chatId BEFORE switching views — setting activeView='chat'
    // with the old chatId causes a visible flash of the previous chat.
    // Standard mode has one foreground surface. If the owner temporarily
    // replaced an unfinished blank chat with an app, "New chat" means return to
    // that in-progress compose surface rather than silently allocate another
    // blank and strand its saved draft. Builder mode deliberately does NOT take
    // this branch: opening another chat there is additive by design.
    //
    // The history route only supplies a candidate id. The existing list guards
    // plus fresh detail probe still prove that it is untouched before reuse, so
    // a send from another browser cannot turn this convenience into reopening a
    // conversation that has already started.
    //
    const ws = workspaceStateRef.current.ws
    // Standard is one destination surface, so acknowledge an explicit New-chat
    // tap immediately instead of leaving the drawer/old transcript painted for
    // the whole async allocation. Builder stays additive and therefore waits
    // for the concrete id before opening its new tab.
    const presentsImmediately = focusComposer && ws.viewMode === 'single'
    if (presentsImmediately && newChatPresentationRef.current) return
    const presentation = presentsImmediately
      ? {
          chatId: null,
          navigationEpoch: navigationEpochRef.current,
          viewMode: ws.viewMode,
          drawerEntryOpen: drawerPushedRef.current,
        }
      : null
    if (presentation) {
      newChatPresentationRef.current = presentation
      setNewChatPresentation(presentation)
    }
    const retirePresentation = () => {
      if (!presentation || newChatPresentationRef.current !== presentation) return
      newChatPresentationRef.current = null
      setNewChatPresentation(current => (
        current === presentation ? null : current
      ))
    }
    // A phone keyboard can only be raised from the tap's live user-activation
    // task. The modal drawer remains history-open but is no longer displayed,
    // so no asynchronous traversal can blur this lease before the chat-bound
    // composer accepts it. The lease also carries any early typing.
    const touchFocusLeased = !!focusComposer && beginTouchComposerFocusLease(
      composerFocusLeaseRef.current,
    )
    const resumeId = (
      (ws.viewMode === 'single')
      && activeChatIdRef.current == null
      && !draft
      && !forceNew
    )
      ? mostRecentConcreteChatId(navStackRef.current)
      : null
    const resumeCandidate = resumeId == null
      ? undefined
      : currentReusableEmptyChat(chatsRef.current, {
        activeChatId: resumeId,
        exclude,
        recoveredChatIds: recoveredChatIdsRef.current,
        streamingChatIds: streamingChatIdsRef.current,
      })
    const { chatId, reason } = await resolveNewChatId(
      resumeCandidate === undefined
        ? { draft, forceNew, exclude }
        : { candidate: resumeCandidate, draft, forceNew, exclude },
    )
    if (presentation && (
      newChatPresentationRef.current !== presentation
      || !newChatPresentationIsCurrent(presentation, {
        navigationEpoch: navigationEpochRef.current,
        viewMode: workspaceStateRef.current.ws.viewMode,
        drawerEntryOpen: drawerPushedRef.current,
        activeView: activeViewRef.current,
        activeChatId: activeChatIdRef.current,
      })
    )) {
      retirePresentation()
      if (touchFocusLeased) {
        releaseComposerFocusLease(composerFocusLeaseRef.current)
      }
      return
    }
    if (chatId == null) {
      retirePresentation()
      if (touchFocusLeased) {
        releaseComposerFocusLease(composerFocusLeaseRef.current)
      }
      // Don't leave a dead, drawer-still-open tap. Offline / failed create surface a
      // toast; an in-flight second tap just closes the drawer (the first create lands).
      if (reason === 'offline') showToast("You're offline.")
      else if (reason === 'error') showToast("Couldn't start a new chat — please try again.", { variant: 'error' })
      closeDrawer()
      return
    }

    const alreadyPresented = presentation
      && activeViewRef.current === 'chat'
      && String(activeChatIdRef.current) === String(chatId)

    const changesRoute = activeViewRef.current !== 'chat'
      || String(activeChatIdRef.current) !== String(chatId)
    const recordsHistory = changesRoute
      && !!(draft || forceNew || drawerPushedRef.current || recordHistory)
    const suppliedDraft = draft ? String(draft) : ''
    const leasedDraft = touchFocusLeased ? composerFocusLeaseRef.current?.value || '' : ''
    const draftText = suppliedDraft || leasedDraft
    if (draftText) {
      stageComposerHandoff(chatId, draftText, {
        autoSend: suppliedDraft ? autoSend : false,
      })
    }
    // Keep history writes inside useNavigation so the entry gets its route,
    // unique identity, and monotonic cursor synchronously. The former direct
    // push left an immediate Back/Forward race before React's route effect ran.
    if (recordsHistory) navTo('chat', { chatId })
    else {
      // Non-history path: no back-target push, but the workspace still owns what
      // renders. Route through the ONE decision point (finding 4; INV 2/4) so a
      // single-world new chat sets the SLOT — never OPEN_TAB into the hidden pane
      // tree, which would leave the created chat invisible.
      closeDrawer()
      const ws = workspaceStateRef.current.ws
      applyModeDestination({ view: 'chat', chatId, appId: null, paneId: ws.focusedPaneId })
    }
    if (presentation) {
      if (alreadyPresented) {
        retirePresentation()
      } else {
        const resolvedPresentation = {
          ...presentation,
          chatId: String(chatId),
        }
        newChatPresentationRef.current = resolvedPresentation
        setNewChatPresentation(current => (
          current === presentation ? resolvedPresentation : current
        ))
      }
    }
    if (focusComposer) {
      requestComposer(chatId, {
        draft: draftText || undefined,
        focus: true,
      })
    }
  }
  // Keep the latest-newChat ref current so handleAppError's crash-report
  // fallback starts a chat with this render's live closure.
  newChatRef.current = newChat
  // Keep the latest-materialize ref current so the watcher effect (stable deps) always
  // runs this render's live closure without depending on the function's identity.
  materializeNewChatHomeRef.current = materializeNewChatHome

  // ── Deferred New Chat materialization watcher (round 4 item 3) ─────────────
  // A pending New Chat request (recorded by requestEmptySingleNewChat) materializes
  // only once the visual scene is IDLE and the slot is still an empty single.
  useEffect(() => {
    if (!pendingNewChatToken) return
    if (modeView.active || modeState.transition) return
    const pending = pendingNewChatRef.current
    if (!pending || pending.token !== pendingNewChatToken) return
    const ws = workspaceStateRef.current.ws
    const single = ws.viewMode === 'single'
    if (!single || ws.singleScreen != null) {
      // No longer an empty single slot (re-toggled to builder, or a slot was set by
      // another path) — drop the request.
      pendingNewChatRef.current = null
      return
    }
    materializeNewChatHomeRef.current?.(pending)
  }, [pendingNewChatToken, materializeNewChatRevision, modeView.active, modeState.transition,
      workspace.viewMode, workspace.singleScreen, workspaceStateRef])

  function selectChat(id, { focusComposer = true } = {}) {
    const chatId = String(id)
    const paintedWorld = effectiveViewMode === 'single'
      ? STANDARD_CHAT_WORLD
      : BUILDER_CHAT_WORLD
    const destinationAlreadyPainted = visibleChatPanes.some(owner => (
      owner.world === paintedWorld
      && String(owner.chatId) === chatId
      && String(presentedChatByPane.get(String(owner.paneId)) ?? '') === chatId
    ))
    const preserveDrawerPresentation = modalDrawerOpen
      && !(activeView === 'chat' && String(activeChatId) === chatId)
      && !destinationAlreadyPainted
    clearChatAttention(id)
    navTo('chat', { chatId: id, preserveDrawerPresentation })
    if (focusComposer) focusDesktopChatPaneComposer(id)
  }

  async function deleteChat(id) {
    // 409 means the agent is still running and stop_chat_for couldn't
    // interrupt it within the timeout. We MUST NOT clear local state
    // in that case — doing so would leave a phantom chat that's gone
    // from the UI but still has a runner writing to the DB. Surface
    // the error and bail; the user can retry once the runner settles.
    let res
    try {
      res = await api.chats.remove(id)
    } catch {
      // Network error — treat as inconclusive, don't touch local state.
      showToast("Couldn't delete — check your connection.", { variant: 'error' })
      return
    }
    if (!res.ok) {
      if (res.status === 409) {
        showToast('Agent is still working in this chat — stop it first.', { duration: 6000 })
        return
      }
      if (res.status !== 404) {
        showToast("Couldn't delete this chat — please try again.", { variant: 'error' })
        return
      }
      // A 404 means the server row is already gone; remove the local phantom.
    }
    // DELETE/404 is authoritative. Publish that fact into the drawer before
    // any navigation work; every later list completion is filtered by the same
    // session tombstone until recovery succeeds.
    confirmChatDeleted(id)
    clearComposerDraft(id)
    // Evict the cached messages so a future chat-ID collision (e.g.
    // recovery) can't surface stale content.
    chatQueries.messages.remove(queryClient, id)
    // Scrub any navStack entries pointing at the deleted chat —
    // otherwise pressing back would navigate into a chat that returns
    // 404, leaving the user staring at an empty view. Soft-deleted
    // chats are recoverable for 7 days via Undo/the chat recovery API; once
    // recovered
    // they re-enter the chat list normally and rebuild navStack via
    // user navigation.
    navStackRef.current = navStackRef.current.filter(e => e.chatId !== id)
    // Tombstone the route so a Back/Forward landing on a surviving PHYSICAL
    // history entry for this chat cannot recreate the tab via the branch-(5)
    // route fallback (§5.1.1) — the in-memory scrub above only covers navStackRef.
    tombstoneRoute('chat', id)
    // Drop the tab pinned to this chat (local delete only — see deleteApp).
    // reason:'deleted' clears the undo slot so Cmd/Z can't resurrect a
    // tombstoned chat outside the backend recovery path. CLOSE_TAB already
    // activates the pane's neighbour tab when one exists; only if that leaves
    // the focused pane EMPTY (we deleted its sole/active tab) do we open a fresh
    // chat — so a background sibling tab is preserved rather than overridden.
    dispatchWorkspace({
      type: 'CLOSE_TAB',
      tabKey: tabModel.tabKey(tabModel.makeTab('chat', id)),
      reason: 'deleted',
    })
    const wsAfterClose = workspaceStateRef.current.ws
    const single = wsAfterClose.viewMode === 'single'
    const focusedAfterClose = wsAfterClose.panes[wsAfterClose.focusedPaneId]
    if (!single && !focusedAfterClose?.activeTabKey) {
      // Exclude the just-deleted id: it's still in `chats` until the
      // refreshChats below, and the reuse filter would otherwise pick it
      // (empty + was active) and navigate straight back into a 404 chat.
      await newChat({ exclude: id })
    }
    await refreshChats()
    // 5-second Undo toast: calls POST /api/chats/{id}/recover then
    // refreshes the chat list so the recovered chat re-appears.
    showToast('Chat deleted', {
      duration: 5000,
      action: {
        label: 'Undo',
        onAction: async () => {
          try {
            const recoverRes = await api.chats.recover(id)
            await jsonOrThrow(recoverRes, 'Chat recovery failed')
            confirmChatRecovered(id)
            // Guard against the newChat() reuse scan picking up this
            // recovered chat before its has_messages=true propagates from
            // the server. The guard is cleared once ChatView fires
            // onFirstMessage (meaning the server confirmed the chat has
            // content and has_messages is reliably true).
            recoveredChatIdsRef.current.add(id)
            await refreshChats()
          } catch {
            showToast("Couldn't undo — chat may be gone.", { variant: 'error' })
          }
        },
      },
    })
  }

  // App delete lives here (not in Drawer) so we have access to showToast.
  // The Drawer's local deleteApp swallowed all errors silently — 409 means
  // the agent is still working and the app cannot be safely removed yet;
  // network errors must not leave the UI in an ambiguous state.
  async function deleteApp(id) {
    let res
    try {
      res = await api.apps.remove(id)
    } catch {
      showToast("Couldn't delete — check your connection.", { variant: 'error' })
      return
    }
    if (!res.ok) {
      if (res.status === 409) {
        showToast('Agent is still working in this app — stop it first.', { duration: 6000 })
        return
      }
      if (res.status !== 404) {
        showToast("Couldn't delete this app — please try again.", { variant: 'error' })
        return
      }
      // A 404 means the server row is already gone; remove the local phantom.
    }
    confirmAppDeleted(id)
    // Retire this app's physical history + evict any warm frame before unmount
    // (contract §4.1.5), tombstone its route so Back can't recreate the tab
    // (§5.1.1), then scrub the nav-stack, then close its tab. The
    // CLOSE_TAB(reason:'deleted') owns the view transition — the derived triple
    // follows the workspace to the pane's neighbour/collapse; no global demote.
    retireAppHistory(id, 'deleted')
    tombstoneRoute('app', id)
    const sid = String(id)
    dropFromWarmLru(cid => String(cid) === sid)
    navStackRef.current = navStackRef.current.filter(
      e => !(e.view === 'canvas' && String(e.appId) === sid)
    )
    // Drop the tab pinned to this app. Only LOCAL deletes prune the strip; an
    // out-of-band delete leaves the tab, which degrades gracefully (clicking it
    // 404s the iframe). reason:'deleted' clears the undo slot so Cmd/Z can't
    // resurrect a tombstoned app outside the backend recovery path.
    dispatchWorkspace({
      type: 'CLOSE_TAB',
      tabKey: tabModel.tabKey(tabModel.makeTab('app', id)),
      reason: 'deleted',
    })
    await refreshApps()
    showToast('App deleted', {
      duration: 5000,
      action: {
        label: 'Undo',
        onAction: async () => {
          try {
            const recoverRes = await api.apps.recover(id)
            await jsonOrThrow(recoverRes, 'App recovery failed')
            confirmAppRecovered(id)
            await refreshApps()
          } catch {
            showToast("Couldn't undo — app may be gone.", { variant: 'error' })
          }
        },
      },
    })
  }

  // Wipes an app's stored data back to empty while KEEPING it installed —
  // a separate, additive action from deleteApp (which tombstones the whole
  // app). Lives here, like deleteApp, so it has access to showToast and
  // refreshApps. The app STAYS in the list; refreshApps picks up the bumped
  // updated_at, which rotates versionForApp's cache-buster so an open iframe
  // remounts against its now-empty storage — no manual cache eviction.
  async function deleteAppData(id) {
    let res
    try {
      res = await api.apps.deleteData(id)
    } catch {
      showToast("Couldn't delete app data — check your connection.", { variant: 'error' })
      return
    }
    if (!res.ok) {
      if (res.status === 409) {
        showToast('Agent is still working in this app — stop it first.', { duration: 6000 })
        return
      }
      showToast("Couldn't delete app data.", { variant: 'error' })
      return
    }
    // The server rotated this app's immutable storage generation under the write
    // lock. The remount rides versionForApp's bump (refreshApps below); we just
    // retire the old frame's physical history — its replacement starts with an
    // empty internal nav stack (contract §4.1.5) — and drop any warm-only frame.
    retireAppHistory(id, 'data-reset')
    const sid = String(id)
    dropFromWarmLru(cid => String(cid) === sid)
    clearAppFrameStorage(id)
    clearCachedAppToken(id)
    await clearAppRuntimeData(id)
    await appQueries.token.invalidate(queryClient, id)
    await refreshApps()
    showToast('App data deleted')
  }

  // Bootstrap: create an initial chat once the server confirms zero
  // chats exist. Gate on live-fetch confirmation, not just any
  // chatsLoadedRef flip — a stale persisted snapshot with chats=[]
  // could be lying if a sibling session (other tab, other device)
  // created chats server-side after the snapshot was written. Without
  // the liveFetched guard, this effect would POST a spurious empty
  // chat before the live refetch arrives.
  //
  // `activeChatId` is in the deps array because the demote-cached-
  // chat effect above this one can transition it from a real id to
  // null on the same chats reference (live fetch confirms the
  // restored chat is gone server-side, so it sets chats[0]?.id || null
  // which can be null if the list emptied). Without activeChatId in
  // deps, that transition wouldn't re-run this bootstrap effect, and
  // a user whose last chat was deleted out-of-band (another tab,
  // backend cleanup) would land in a no-chat / no-ChatView state with
  // an empty `<main>` until the next refresh. newChat is intentionally
  // NOT in deps — it's a plain function declaration recreated every
  // render, so adding it would re-fire the effect every render. The
  // call site doesn't depend on its identity, only on invoking it
  // once when the guards line up.
  useEffect(() => {
    if (!chatsLoadedRef.current) return
    const liveFetched = chatsQuery.isSuccess
      && chatsQuery.isFetchedAfterMount
    if (!liveFetched) return
    // Only bootstrap a starter chat while the chat view is what's
    // showing. A deep-link to /app/:id (push-notification tap, PWA
    // launch-at-app) sets activeView='canvas' with activeChatId still
    // null; without the activeView guard this fires newChat(), which
    // flips activeView to 'chat' and buries the deep-linked app behind
    // the empty chat. It only bites a zero-chat instance — a populated
    // instance skips it on the length===0 guard, which is why apps
    // deep-link fine in practice but the empty-list app-canvas tests
    // failed. When the user later opens chat, activeView flips to
    // 'chat' and this effect re-runs (activeView is in deps) to create
    // the starter chat then.
    if (chats.length === 0 && activeChatId === null && activeView === 'chat') {
      const ws = workspaceStateRef.current.ws
      const single = ws.viewMode === 'single'
      if (single && ws.singleScreen == null) requestEmptySingleNewChat()
      else newChat()
    }
  }, [chats, activeChatId, activeView, chatsQuery.isSuccess,
      chatsQuery.isFetchedAfterMount, requestEmptySingleNewChat, workspaceStateRef])

  return (
    <HistoryDismissProvider
      openHistoryDismiss={openHistoryDismiss}
      closeHistoryDismiss={closeHistoryDismiss}
      unregisterHistoryDismiss={unregisterHistoryDismiss}
    >
    <div
      ref={shellRootRef}
      // Stable shell geometry only. Beat-local custom properties live on the header
      // or moving pane itself, never this ancestor of every retained transcript.
      style={shellRootStyle}
      data-mode-phase={modeView.active?.phase || modeState.transition?.phase || 'idle'}
      data-mode-epoch={modeView.active?.id || modeState.transition?.id || undefined}
      data-workspace-visual-state={workspaceVisualState}
      className={`shell${immersiveActive ? ' shell--immersive' : ''}${desktopSidebarReserved ? ' shell--drawer-docked' : ''}`}>
      <a
        className="shell__skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault()
          contentElRef.current?.focus({ preventScroll: true })
        }}
      >
        Skip to content
      </a>
      <textarea
        ref={composerFocusLeaseRef}
        className="shell__composer-focus-lease"
        tabIndex={-1}
        aria-label="New chat message"
        autoComplete="off"
        onBlur={(event) => { event.currentTarget.value = '' }}
      />
      {/* The existing brand toggle remains the visible close affordance while the
          mobile drawer is modal. Keep the workspace inert below, but do not inert
          the header: doing so lets the scrim intercept the toggle and strands the
          drawer without the close path its label and aria-expanded state promise. */}
      <header
        className="shell__bar"
        style={brandBeatStyle || undefined}
        data-mode-phase={modeView.active?.phase || undefined}
      >
        <ShellBrand
          brandRef={brandButtonRef}
          navigationOpen={navigationOpen}
          builderModeActive={builderModeActive}
          // The live descriptor drives the logo's hold→completion spring (round 4
          // item 1): a hold-owned animated beat holds the mark compressed and releases
          // it as the beat completes, instead of an immediate ignite/snap.
          transition={modeView.active || modeState.transition}
          backFiredRef={backFiredRef}
          onToggleMode={handleToggleViewMode}
          onToggleNavigation={handleToggleNavigation}
        />
        <nav className="shell__rail-actions" aria-label="Quick actions">
          <button
            type="button"
            className="shell__rail-action"
            aria-label="New chat shortcut"
            title="New chat"
            onClick={() => newChat({ focusComposer: true, recordHistory: true })}
          >
            <NewChatNavIcon aria-hidden="true" />
          </button>
          <button
            type="button"
            className={`shell__rail-action${activeView === 'apps' ? ' shell__rail-action--active' : ''}`}
            aria-label="Apps shortcut"
            title="Apps"
            aria-current={activeView === 'apps' ? 'page' : undefined}
            onClick={() => navTo('apps')}
          >
            <AppsNavIcon aria-hidden="true" />
          </button>
          <button
            type="button"
            className={`shell__rail-action shell__rail-action--bottom${activeView === 'settings' ? ' shell__rail-action--active' : ''}`}
            aria-label="Settings shortcut"
            title="Settings"
            aria-current={activeView === 'settings' ? 'page' : undefined}
            onClick={() => {
              setSettingsFocusTarget(null)
              navTo('settings')
            }}
          >
            <SettingsNavIcon aria-hidden="true" />
          </button>
        </nav>
        <div className="shell__bar-actions">
          {!online && (
            <span className="shell__offline" role="status" aria-live="polite">
              Offline
            </span>
          )}
          <NotificationCenter
            ref={notificationCenterActionsRef}
            onOpenTarget={handleNotificationOpen}
          />
        </div>
      </header>

      <Drawer
        open={displayedNavigationOpen}
        persistent={persistentDrawer}
        width={desktopSidebarWidth}
        onWidthChange={setDesktopSidebarWidth}
        interactionLocked={drawerModeTransitioning || drawerNavigationCover}
        onClose={drawerModeTransitioning || drawerNavigationCover
          ? undefined
          : closeDrawer}
        apps={apps}
        appsStatus={appsStatus}
        onRetryApps={() => appsQuery.refetch()}
        activeView={activeView}
        activeAppId={activeAppId}
        chats={chats}
        chatsStatus={chatsStatus}
        onRetryChats={() => chatsQuery.refetch()}
        activeChatId={activeChatId}
        onChat={selectChat}
        onApp={(id) => navTo('canvas', { appId: id })}
        onNewChat={() => newChat({ focusComposer: true, recordHistory: true })}
        onDeleteChat={deleteChat}
        onDeleteApp={deleteApp}
        onDeleteAppData={deleteAppData}
        onSettings={() => {
          setSettingsFocusTarget(null)
          navTo('settings')
        }}
        appsActive={appsVisibleAsTab}
        onAppsOpen={() => navTo('apps')}
        appsHost={appsDirectoryHost}
        nowPlaying={nowPlaying}
        onNowPlayingOpen={handleNowPlayingOpen}
        onNowPlayingControl={handleNowPlayingControl}
        streamingChatIds={streamingChatIds}
        attentionChatIds={attentionChatIds}
        newAppIds={appAttentionSet}
        settingsWarning={providerAuth.needsAttention}
        dragActiveRef={dragActiveRef}
        drawerRowGesturesRef={drawerRowGesturesRef}
      />

      {showWalkthrough && (
        <WalkthroughOverlay
          onOpenSettings={() => {
            setSettingsFocusTarget({ section: 'ai-providers', nonce: Date.now() })
            navTo('settings')
          }}
          onExploreApps={() => {
            const appStore = findAppStoreApp(apps)
            if (appStore) navTo('canvas', { appId: appStore.id })
            else openDrawer()
          }}
          onDone={() => {
            // Query invalidation inside WalkthroughOverlay flips
            // `showWalkthrough` to false on the next render. Nothing
            // else to do here.
          }}
        />
      )}

      {/* inert on the main content while the modal drawer is open — mirrors
          the drawer's own inert-when-closed contract, but inverted.
          Prevents pointer / keyboard events from reaching the chat or
          app canvas while the drawer is overlaid in front of it. React 19's
          boolean prop form emits the attribute only while this is true. */}
      {/* Tab strip: pinned chats/apps to swap between with one tap.
          Switching a tab is ordinary navTo, so back works through the
          existing navStack. The strip shrinks .shell__content by one row;
          the chat re-measures its spacer at the new height on the next
          layout event (a ~1-row imprecision on the 0<->1 crossing that
          self-corrects). Deliberately NOT a ChatView remount — that would
          reset the send-reservation and freeze stream-follow (the reason the
          bespoke split view was parked). */}
      {tabStripVisible && !workspaceChromeActive && (() => {
        const navPaneId = workspace.focusedPaneId
        const navViewStyle = navPaneId
          ? modeViewTransitionStyle('strip', navPaneId, 'single')
          : null
        return (
        <nav
          className="shell__tabstrip"
          onWheel={scrollStripWheel}
          // INV 9 (inert beat): the single-pane strip clears WITH its pane during
          // a mode scene, so it is pointer/keyboard inert throughout — not just under
          // the drawer (M4). It matches the WorkspaceChrome strips, which already go
          // inert for the full mode beat.
          inert={navigationSurfaceOpen || modeBeatActive}
          aria-label="Open tabs"
          // The single-pane strip is the PRIMARY drag source once the flag is on
          // Tag it with the sole pane's id so the drag controller resolves a
          // source pane exactly as it does for a WorkspaceChrome strip; dragging
          // a tab out with ≥2 tabs present splits the pane.
          data-pane-strip={workspace.focusedPaneId}
          data-mode-pane-vt={navViewStyle ? navPaneId : undefined}
          style={navViewStyle || undefined}
          onKeyDown={(e) => stripKeyDown(e, openTabs, (tab) => closeTab(tab))}
        >
          {openTabs.map(tab => {
            // Active-ness comes from the workspace's OWN focused active tab, not
            // the legacy nav triple (retires tabModel.isTabActive); label, target,
            // drag key, and close route through the shared PaneTab, so the
            // .shell__tab chrome is defined once for both strips.
            const key = tabModel.tabKey(tab)
            const active = key === focusedActiveKey
            return (
              <PaneTab
                key={key}
                tab={tab}
                label={labelForTab(tab)}
                active={active}
                revealKey={tabRevealRevision}
                tabIndex={active ? 0 : -1}
                dragKey={key}
                onActivate={() => {
                  const { view, opts } = tabModel.tabNavTarget(tab)
                  navTo(view, opts)
                  if (tab.kind === 'chat') focusDesktopChatPaneComposer(tab.id)
                }}
                onClose={() => closeTab(tab)}
                onContextMenu={(event) => openTabMenu(event, tab, null)}
              />
            )
          })}
        </nav>
        )
      })()}
      <main className="shell__content" id="main-content" tabIndex={-1} inert={navigationSurfaceOpen} ref={contentElRef}>
        {/* Content layer (design §2): app-iframe wrappers (id-sorted) and chat
            wrappers (chatId-sorted) as ONE flat sibling set, never reparented.
            A wrapper is positioned (--paned) when its tab is a visible pane's
            active tab in the tiled path, full-bleed (--active) when it is the
            focused pane's active tab in single-pane, else hidden. DOM identity
            is preserved across 1↔2 panes — the first split changes rects, never
            remounts (finding A). */}

        {/* App iframes — the rendered set is derived synchronously (visibleAppIds
            ∪ warm LRU), id-sorted so React never reparents (a sandbox reparent =
            reload). */}
        {renderedAppIds.map(id => {
          const tabKey = `app:${id}`
          const paned = workspaceChromeActive ? visibleTabRects.get(tabKey) : null
          const fullBleed = !paned && tabKey === fullBleedKey
          const surfaceVisible = !!(paned || fullBleed)
          const appSurfaceInert = !surfaceVisible
          const posStyle = paned ? {
            top: paned.y,
            left: paned.x,
            width: paned.w,
            height: paned.h,
            ...modeViewTransitionStyle('pane', paned.paneId, tabKey),
          } : null
          const app = apps.find(a => String(a.id) === String(id))
          return (
          <div
            key={id}
            id={paned ? panePanelDomId(paned.paneId, tabKey) : undefined}
            role={paned ? 'tabpanel' : undefined}
            aria-labelledby={paned ? paneTabDomId(paned.paneId, tabKey) : undefined}
            data-tab-key={(multiPane || focusedPaneViewId != null) ? tabKey : undefined}
            data-mode-pane-vt={paned ? paned.paneId : undefined}
            className={paned
              ? 'shell__view shell__app-view shell__view--paned'
              : `shell__view shell__app-view ${fullBleed ? 'shell__view--active' : ''}`}
            style={posStyle || undefined}
            inert={appSurfaceInert || undefined}
            aria-hidden={appSurfaceInert ? 'true' : undefined}
            // Clicking a visible pane focuses it (chat panes are not opaque; app
            // iframes swallow interior clicks, so this catches wrapper padding —
            // interior app focus rides the runtime bridge later). Only in the
            // tiled path (finding D-i), and never during a mode scene.
            onPointerDownCapture={paned && !modeBeatActive
              ? () => dispatchWorkspace({ type: 'FOCUS', paneId: paned.paneId }) : undefined}
          >
            <ErrorBoundary
              key={`ab-${id}`}
              variant="inline"
              label="app"
              recoveryKey={`app:${id}`}
            >
            <AppCanvas
              appId={id}
              // Focused-pane-only: gates safe-area insets + the immersive holder
              // (global last-writer-wins).
              active={tabKey === focusedActiveKey}
              // Visible in ANY pane: gates frame-visibility + nav-push (§5). A
              // background split's app keeps running and can install sentinels;
              // Settings/immersive-solo/hidden panes exclude it (visibleAppIds).
              visible={visibleAppIds.has(String(id))}
              // Every visible pane remains painted beneath the modal scrim, but
              // suspend its iframe interaction while the drawer is open OR during any
              // mode scene (cross-origin app interaction is inert throughout).
              interactive={visibleAppIds.has(String(id)) && !navigationSurfaceOpen && !modeBeatActive}
              version={versionForApp(id)}
              appName={app?.name}
              appSlug={app?.slug}
              offlineCapable={!!app?.offline_capable}
              capabilityContract={app?.capability_contract || null}
              pendingIntent={appIntents[String(id)] || null}
              immersive={immersiveActive && String(immersiveAppId) === String(id)}
              onNavPush={appNavPush}
              onNavPop={appNavPop}
              onNavReset={appNavReset}
              onAppFocus={focusAppPane}
              onNavForwardResult={appNavForwardResult}
              onImmersive={handleImmersive}
              onIntentDelivered={handleAppIntentDelivered}
              onAppError={handleAppError}
              onHostRequest={handleAppHostRequest}
              onMediaSession={handleMediaSession}
            />
            </ErrorBoundary>
          </div>
          )
        })}
        {/* Chat surfaces — one retained owner per world. Standard's synthetic
            owner always keeps the full content-box geometry; Builder owners keep
            their projected pane geometry even while hidden. During a chat change
            the last painted chat remains as an inert opaque same-world cover until
            the incoming chat reports a stable frame. */}
        {chatPaneLayers.map(({ world, paneId, chatId, role, surfaceKey }) => {
          const tabKey = `chat:${chatId}`
          const standardOwner = world === STANDARD_CHAT_WORLD
          const paneActiveKey = paneModel.activeKeyForOwner(workspace, paneId) || tabKey
          const builderRect = standardOwner ? null : builderChatTabRects.get(paneActiveKey)
          const builderPainted = !standardOwner
            && effectiveViewMode === 'panes'
            && chatPanesVisible
          const paned = builderPainted ? builderRect : null
          const fullBleed = paneActiveKey === fullBleedKey
            && (standardOwner
              ? effectiveViewMode === 'single'
              : (builderPainted && !paned))
          const surfaceVisible = !!(paned || fullBleed)
          const tabPanel = role !== 'held' && paned
          // A retained owner may belong to the hidden workspace world. Its
          // layers stay mounted for continuity, but only a surface painted by
          // its own world may expose held/staging handoff classes.
          const handoffClass = !settingsOverlay && surfaceVisible && role !== 'active'
            ? ` shell__chat-view--${role}`
            : ''
          // Keep hidden Builder ChatViews laid out at their pane size before a
          // transition. Clearing right/bottom is the geometry half of
          // .shell__view--paned without making the hidden wrapper paint.
          const posStyle = builderRect
            ? {
              top: builderRect.y,
              left: builderRect.x,
              width: builderRect.w,
              height: builderRect.h,
              right: 'auto',
              bottom: 'auto',
            }
            : null
          const chatViewStyle = builderRect
            ? {
              ...posStyle,
              ...(paned ? modeViewTransitionStyle('pane', paneId, surfaceKey) : {}),
            }
            : undefined
          return (
            <div
              key={surfaceKey}
              id={tabPanel ? panePanelDomId(paneId, tabKey) : undefined}
              role={tabPanel ? 'tabpanel' : undefined}
              aria-labelledby={tabPanel ? paneTabDomId(paneId, tabKey) : undefined}
              data-chat-world={world}
              data-chat-id={chatId}
              data-mode-pane-vt={paned ? paneId : undefined}
              data-tab-key={!standardOwner && (multiPane || focusedPaneViewId != null)
                && role !== 'held'
                ? tabKey : undefined}
              // A ChatView can stay mounted in the parked workspace world so its
              // stream, draft, and scroll controller survive a world flip. Expose
              // one explicit page-level selector for the settled surface that is
              // actually interactive; browser contracts must not accidentally
              // target a retained hidden world or an in-flight handoff layer.
              data-chat-surface={surfaceVisible && role === 'active' ? 'painted' : undefined}
              className={paned
                ? `shell__view shell__view--paned shell__chat-view${handoffClass}`
                : `shell__view shell__chat-view ${fullBleed ? 'shell__view--active' : ''}${handoffClass}`}
              style={chatViewStyle}
              // A retained owner that belongs to the other world is physically
              // inert as well as visibility-hidden.
              inert={!surfaceVisible || settingsOverlay || role !== 'active' || undefined}
              aria-hidden={!surfaceVisible || settingsOverlay || role !== 'active'
                ? 'true' : undefined}
              onPointerDownCapture={paned && role === 'active' && !modeBeatActive
                ? (event) => {
                  const wasFocused = workspaceStateRef.current.ws.focusedPaneId === paneId
                  dispatchWorkspace({ type: 'FOCUS', paneId })
                  if (supportsDesktopPaneComposerFocus()
                    && shouldFocusComposerAfterPanePointer({
                      wasFocused,
                      pointerType: event.pointerType,
                      button: event.button,
                      target: event.target,
                    })) {
                    requestComposer(chatId, { focus: true })
                  }
                }
                : undefined}
            >
              <PaneChatView
                chatId={chatId}
                paneId={paneId}
                apps={apps}
                // Runtime activity and painting are independent during a handoff:
                // staging owns the work while held remains the visual cover.
                runtimeActive={surfaceVisible && chatPanesVisible && role !== 'held'}
                keepTranscriptPainted={surfaceVisible && role === 'held'}
                paneContentHeight={builderRect ? builderRect.h : null}
                // Select before the memo boundary. Passing the replacement Map
                // would rerender every visible chat pane for another chat's run.
                externalRunSignal={chatRunSignal(chatRunSignals, chatId)}
                composerRequest={role === 'active' && surfaceVisible ? composerRequest : null}
                onComposerRequestHandled={role === 'active' && surfaceVisible
                  ? handleComposerRequestHandled
                  : null}
                onSystemEvent={handleSystemEvent}
                markStreamingStart={markStreamingStart}
                markStreamingEnd={markStreamingEnd}
                markVoiceListening={markVoiceListening}
                refreshApps={refreshApps}
                acknowledgeAppPreview={handleAppPreviewSeen}
                refreshChats={refreshChats}
                markChatOwnerActivity={markChatOwnerActivity}
                loadTheme={loadTheme}
                navTo={stablePaneNavTo}
                onInternalNav={handleChatInternalNav}
                onChatMissing={handlePaneChatMissing}
                onFirstMessage={handlePaneChatFirstMessage}
                onDisplayReady={role === 'held' || !surfaceVisible
                  ? null
                  : handlePaneChatDisplayReady}
              />
            </div>
          )
        })}
        {/* Apps launcher — one canonical workspace surface, positioned and
            moved by the same tab/pane projection as chats and installed apps.
            Drawer owns the launcher interactions and portals them into this
            stable host so app management remains one implementation. */}
        {(() => {
          const appsPos = appsPaned
            ? {
              top: appsPaned.y,
              left: appsPaned.x,
              width: appsPaned.w,
              height: appsPaned.h,
              ...modeViewTransitionStyle('pane', appsPaned.paneId, APPS_KEY),
            }
            : null
          const appsTabPanel = appsPaned
          const appsSurfaceVisible = !!(appsPaned || appsFullBleed)
          const appsSurfaceInert = !appsSurfaceVisible
          return (
            <div
              key="apps"
              id={appsTabPanel ? panePanelDomId(appsPaned.paneId, APPS_KEY) : undefined}
              role={appsTabPanel ? 'tabpanel' : undefined}
              aria-labelledby={appsTabPanel ? paneTabDomId(appsPaned.paneId, APPS_KEY) : undefined}
              data-tab-key={appsTabPanel ? APPS_KEY : undefined}
              data-mode-pane-vt={appsPaned ? appsPaned.paneId : undefined}
              className={appsPaned
                ? 'shell__view shell__view--paned shell__apps-view'
                : `shell__view shell__apps-view ${appsFullBleed ? 'shell__view--active' : ''}`}
              style={appsPos || undefined}
              inert={appsSurfaceInert || undefined}
              aria-hidden={appsSurfaceInert ? 'true' : undefined}
              onPointerDownCapture={appsPaned && !modeBeatActive
                ? () => dispatchWorkspace({ type: 'FOCUS', paneId: appsPaned.paneId })
                : undefined}
            >
              <div className="shell__apps-host" ref={setAppsDirectoryHost} />
            </div>
          )
        })()}
        {/* Settings surface — ONE wrapper, positioned like a chat/app content
            wrapper (paned) when it is a visible builder tab, full-bleed when the
            takeover overlay is up. Keyed 'settings' so React reconciles it by key
            regardless of the sibling app/chat arrays' lengths, preserving
            SettingsView identity across the tab<->overlay conversion. */}
        {settingsMounted && (() => {
          const settingsPos = settingsPaned
            ? {
              top: settingsPaned.y,
              left: settingsPaned.x,
              width: settingsPaned.w,
              height: settingsPaned.h,
              ...modeViewTransitionStyle('pane', settingsPaned.paneId, SETTINGS_KEY),
            }
            : null
          const settingsTabPanel = settingsPaned
          const settingsSurfaceVisible = !!(settingsPaned || settingsFullBleed)
          const settingsSurfaceInert = !settingsSurfaceVisible
          return (
          <div
            key="settings"
            id={settingsTabPanel
              ? panePanelDomId(settingsPaned.paneId, SETTINGS_KEY)
              : undefined}
            role={settingsTabPanel ? 'tabpanel' : undefined}
            aria-labelledby={settingsTabPanel
              ? paneTabDomId(settingsPaned.paneId, SETTINGS_KEY)
              : undefined}
            data-tab-key={settingsPaned ? SETTINGS_KEY : undefined}
            data-mode-pane-vt={settingsPaned ? settingsPaned.paneId : undefined}
            className={settingsPaned
              ? 'shell__view shell__view--paned shell__settings-view'
              : `shell__view shell__settings-view ${settingsFullBleed ? 'shell__view--active' : ''}`}
            style={settingsPos || undefined}
            inert={settingsSurfaceInert || undefined}
            aria-hidden={settingsSurfaceInert ? 'true' : undefined}
            onPointerDownCapture={settingsPaned && !modeBeatActive
              ? () => dispatchWorkspace({ type: 'FOCUS', paneId: settingsPaned.paneId })
              : undefined}
          >
            <Suspense fallback={(
              <div className="shell__settings-loading" role="status" aria-label="Loading settings">
                <span className="shell__settings-loading-dot" aria-hidden="true" />
              </div>
            )}>
              <SettingsView
                onThemeChange={loadTheme}
                onOpenChat={selectChat}
                focusTarget={settingsFocusTarget}
                active={settingsFullBleed || !!settingsPaned}
                refreshToken={settingsRefreshToken}
              />
            </Suspense>
          </div>
          )
        })()}
        {/* One New Chat landing owns both the resting null slot and the immediate
            user-initiated cover while its chat row is allocated. */}
        {(() => {
          const allocatingNewChat = newChatPresentation != null
          const newChatSurface = fullBleedKey === EMPTY_SINGLE_SURFACE_KEY
            || allocatingNewChat
          if (!newChatSurface) return null
          return (
            <div
              key="home-new-chat"
              className={`shell__view shell__view--active shell__chat-view${allocatingNewChat ? ' shell__new-chat-presentation' : ''}`}
              data-new-chat-presentation={allocatingNewChat
                ? newChatPresentation.chatId || 'allocating'
                : undefined}
              aria-busy={allocatingNewChat || undefined}
            >
              <NewChatLanding
                failure={allocatingNewChat ? null : newChatLandingFailure}
                onRetry={requestEmptySingleNewChat}
              />
            </div>
          )
        })()}
        {/* Chrome layer — sibling AFTER the content wrappers, over the whole
            content box, carrying its own inert. Only at ≥2 visible leaves and
            never while Settings overlays. Draws per-pane strips and dividers;
            no content lives here. */}
        {workspaceChromeActive && (
          <WorkspaceChrome
            // INV 9 / finding 10: during the exit deal the chrome is not just
            // pointer-transparent (CSS) but fully INERT — keyboard-unfocusable and
            // aria-hidden — so a tab/divider that already held focus can't process
            // Enter/arrow input while its captured scene is moving.
            inert={navigationSurfaceOpen || modeBeatActive}
            workspace={workspace}
            projection={projection}
            mode={workspaceMode}
            contentRect={contentRect}
            contentElRef={contentElRef}
            dispatchWorkspace={dispatchWorkspace}
            navTo={navTo}
            labelForTab={labelForTab}
            onTabContextMenu={openTabMenu}
            // The ONE shared user-close action — WorkspaceChrome owns no private
            // close dispatcher or transition timing.
            onCloseTab={closeTab}
            focusedPaneViewId={focusedPaneViewId}
            onTogglePaneFocus={toggleFocusedPaneView}
            onChatPaneSelected={focusDesktopChatPaneComposer}
            revealKey={tabRevealRevision}
          />
        )}
      </main>
      {/* SHELL-provided immersive exit. With the top bar gone the drawer
          toggle is unreachable, so this floating button is the guaranteed
          way back — an app can never trap the user in immersive mode.
          Exit only clears the shell-side request; re-entry requires another
          explicit app post, so the user remains in control. */}
      {immersiveActive && (
        <button
          ref={immersiveExitRef}
          type="button"
          className="shell__immersive-exit"
          aria-label="Exit full screen"
          inert={navigationSurfaceOpen}
          onClick={() => dispatchImmersive({ type: 'exit' })}
        >
          <CollapseSm width={18} height={18} aria-hidden="true" />
        </button>
      )}
      <Toast
        key={toast?.sequence || 'toast-empty'}
        message={toast?.message}
        variant={toast?.variant}
        duration={toast?.duration}
        action={toast?.action}
        onDismiss={dismissToast}
      />
      {tabMenu && (() => {
        const menuPane = workspace.panes[tabMenu.paneId]
        const menuTabIndex = menuPane?.tabs.findIndex(
          tab => tabModel.tabKey(tab) === tabMenu.tabKey,
        ) ?? -1
        const hasSiblingTabs = Boolean(menuPane && menuPane.tabs.length > 1)
        const hasTabsToRight = Boolean(
          menuPane && menuTabIndex >= 0 && menuTabIndex < menuPane.tabs.length - 1,
        )
        return (
          <div className="workspace__menu-layer">
            <div
              ref={tabMenuRef}
              className="workspace__menu"
              role="menu"
              aria-label="Tab actions"
              style={{
                '--workspace-menu-x': `${tabMenu.x}px`,
                '--workspace-menu-y': `${tabMenu.y}px`,
              }}
              onKeyDown={handleTabMenuKeyDown}
            >
              <div className="workspace__menu-items">
                <button
                  type="button"
                  role="menuitem"
                  className="workspace__menu-item"
                  onClick={() => {
                    closeTab(tabMenu.tab)
                    closeTabMenu()
                  }}
                >
                  Close tab
                </button>
                {hasSiblingTabs && (
                  <button
                    type="button"
                    role="menuitem"
                    className="workspace__menu-item"
                    onClick={() => {
                      dispatchWorkspace({
                        type: 'CLOSE_OTHER_TABS',
                        tabKey: tabMenu.tabKey,
                      })
                      closeTabMenu()
                    }}
                  >
                    Close all other tabs
                  </button>
                )}
                {hasTabsToRight && (
                  <button
                    type="button"
                    role="menuitem"
                    className="workspace__menu-item"
                    onClick={() => {
                      dispatchWorkspace({
                        type: 'CLOSE_TABS_TO_RIGHT',
                        tabKey: tabMenu.tabKey,
                      })
                      closeTabMenu()
                    }}
                  >
                    Close tabs to the right
                  </button>
                )}
              </div>
            </div>
          </div>
        )
      })()}
    </div>
    </HistoryDismissProvider>
  )
}
