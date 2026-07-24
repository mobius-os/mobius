import { lazy, Suspense, useState, useEffect, useLayoutEffect, useCallback, useMemo, useReducer, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Minimize2 from 'lucide-react/dist/esm/icons/minimize-2.mjs'
import Drawer from '../Drawer/Drawer.jsx'
import Toast from '../ui/Toast.jsx'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import WalkthroughOverlay from '../Walkthrough/WalkthroughOverlay.jsx'
import {
  api, apiFetch, jsonOrThrow, probeDeletion, BASE, clearAppRuntimeData,
  invalidateShellListCache,
} from '../../api/client.js'
import usePushSubscription from '../../hooks/usePushSubscription.js'
import useNavigation, {
  coldRestoredCanvasAppId,
  deepLink,
} from '../../hooks/useNavigation.js'
import { replaceNavEntry } from '../../lib/navHistory.js'
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
import { appVersionKey } from '../../lib/appVersion.js'
import { immersiveReducer, isImmersiveActive } from '../../lib/immersive.js'
import { bumpChatRunSignal } from '../../lib/chatRunSignal.js'
import { clearAppFrameStorage, clearCachedAppToken } from '../../lib/appFrameStorage.js'
import {
  APP_LRU_STORAGE_KEY, mergeAppLru, parseStoredAppLru, requestAppCodeWarm,
  selectAppsToWarm,
} from '../../lib/appPrecache.js'
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
import { BEFORE_SHELL_RELOAD_EVENT } from '../../lib/shellReloadEvents.js'
import {
  acknowledgeAppActivity,
  appAttentionIds,
  freshChatBuiltApps,
  freshAppIds,
  withAppActivitySeen,
  withAppsFlagged,
  withoutAppFlagged,
} from './newAppAttention.js'
import { shouldDeferShellReload } from './shellReloadPolicy.js'
import {
  addCreatedChatToList,
  createdChatDetailCache,
  currentReusableEmptyChat,
  enteredEmptySingleScreen,
  mergeChatListWithCreatedGuards,
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
  reloadWhenWorkerTakesOver,
  shouldRearmShellApply,
  watchForShellUpdateOnForeground,
} from './swHandoff.js'
import {
  awaitCacheFlushBeforeReload,
  flushPersistedQueryCache,
} from '../../queryClient.js'
import './Shell.css'
import './workspace.css'
import WorkspaceChrome from './WorkspaceChrome.jsx'
import useWorkspaceDrag from './useWorkspaceDrag.js'
import useModeController from './useModeController.js'
import * as modeMachine from './modeMachine.js'
import { undoKeyPressed, isEditableTarget } from './workspaceOnboarding.js'
import PaneChatView from './PaneChatView.jsx'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import {
  deriveContentVisibility, deriveExitPlan, deriveEnterPlan, projectFocusedPane,
  transitionSignature, MODE_MOTION, EMPTY_SINGLE_SURFACE_KEY,
} from './workspaceView.js'
import NewChatLanding from './NewChatLanding.jsx'
import {
  PaneTab, panePanelDomId, paneTabDomId, scrollStripWheel, stripKeyDown,
} from './PaneStrip.jsx'
import useAppIntentNavigation from './useAppIntentNavigation.js'
import useDesktopSidebar from './useDesktopSidebar.js'
import ShellBrand from './ShellBrand.jsx'

const SHELL_RELOAD_RECHECK_MS = 6000
// The builder mode beat durations live in workspaceView.js (MODE_MOTION); the
// reconcile clock reads the latched plan's totalMs, and completion is keyed to the
// beat's Web-Animations `finished` promises, not a bare timer — so no BUILDER_ENTER
// / BUILDER_EXIT Shell constants exist (exit-design v2; INV 7/14).
const SettingsView = lazy(() => import('../SettingsView/SettingsView.jsx'))

export default function Shell() {
  const {
    desktop: desktopSidebarMode,
    open: desktopSidebarOpen,
    setOpen: setDesktopSidebarOpen,
  } = useDesktopSidebar()

  // ── Workspace reducer — the single live authority for pane contents, per-pane
  // active tabs, and focus (design §1). Declared ABOVE useNavigation so the
  // adapter derives its legacy triple from it. Init: forgiving read of the
  // versioned blob (readWorkspaceRaw guards the throwing sessionStorage.getItem
  // before parseWorkspace's own try/catch), else the legacy flat seed.
  // Capture the legacy projection once. Besides seeding a missing workspace, an
  // empty value distinguishes the implicit home tab from a strip the user had
  // actually engaged. The workspace still owns rendering either way.
  const [legacyOpenTabs] = useState(() => tabModel.readOpenTabs())
  const [workspaceState, dispatchWorkspaceRaw] = useReducer(
    paneModel.workspaceReducer,
    undefined,
    () => paneModel.initialWorkspaceState(paneModel.parseWorkspace(
      paneModel.readWorkspaceRaw(sessionStorage),
      { fallbackTabs: legacyOpenTabs },
    )),
  )
  const workspace = workspaceState.ws
  // Whether a VALID persisted workspace blob booted this session (not a flat-tab
  // fallback). The nav adapter uses it to make the blob authoritative over the
  // legacy shell-reload triple, seeding from that triple only when absent/invalid
  // (contract §5.3.10). Read once — sessionStorage is fixed for the mount.
  const [blobValid] = useState(
    () => paneModel.isValidWorkspaceBlob(paneModel.readWorkspaceRaw(sessionStorage)),
  )
  // A lone workspace tab with no legacy pinned projection is the shell's
  // implicit home surface ONLY when there was no valid workspace blob. A valid
  // single-screen workspace also deliberately dual-writes an empty legacy list;
  // treating that durable state as implicit would RESET_FLAT on a cold deep link
  // and silently flip its preserved viewMode back to the builder default.
  const replaceImplicitBootTab = !blobValid
    && legacyOpenTabs.length === 0
    && Object.keys(workspace.panes).length === 1
    && paneModel.flatten(workspace).length <= 1
  // Ref-side reducer preview: this wrapper advances a ref copy of the reducer
  // state SYNCHRONOUSLY before the raw React dispatch, so two navigation/
  // placement events in one React 18 batch observe each other (design §1). Every
  // caller uses this wrapper — no raw dispatch survives it.
  const workspaceStateRef = useRef(workspaceState)
  workspaceStateRef.current = workspaceState
  // Shared "a workspace drag is live" flag. Declared ABOVE useNavigation so the
  // drawer's OPEN path can stand down on it (useWorkspaceDrag sets it on arm and
  // the Drawer's swipe-CLOSE handlers already read it).
  const dragActiveRef = useRef(false)
  // Set after useNavigation (needs navStackRef): reconciles in-memory restorable
  // route hints against every workspace transition (design §5.1.3).
  const onWorkspaceTransitionRef = useRef(null)
  // Set once the New Chat policy has its chat/query dependencies. The synchronous
  // workspace boundary can then own every edge into an empty single screen without
  // making early navigation hooks depend on a callback declared later in the render.
  const requestEmptySingleNewChatRef = useRef(null)
  // Ephemeral presentation state only. Focusing one pane must never rewrite the
  // persisted split tree or ratios, so this id lives outside the workspace blob.
  const [focusedPaneViewId, setFocusedPaneViewIdState] = useState(null)
  const focusedPaneViewIdRef = useRef(null)
  const setFocusedPaneViewId = useCallback((paneId) => {
    focusedPaneViewIdRef.current = paneId
    setFocusedPaneViewIdState(paneId)
  }, [])
  const dispatchWorkspace = useCallback((action) => {
    const prev = workspaceStateRef.current
    const next = paneModel.workspaceReducer(prev, action)
    workspaceStateRef.current = next
    // ANY tab-placement/pane transition can strand a route's paneId hint — a
    // cross-pane move (even when the source pane survives) leaves the moved tab's
    // routes pointing at the old pane, and a pane collapse leaves dead-pane hints.
    // Reconcile them synchronously (using prev/next), before any restore reads
    // them, so a hint always names the pane that now holds its item.
    const enteredEmptySingle = next.ws !== prev.ws
      && enteredEmptySingleScreen(
        prev.ws, next.ws, paneModel.WORKSPACE_SPLITS_ENABLED,
      )
    if (next.ws !== prev.ws) {
      onWorkspaceTransitionRef.current?.(prev.ws, next.ws)
      const expanded = focusedPaneViewIdRef.current
      if (expanded != null) {
        const paneIds = Object.keys(next.ws.panes)
        if (paneIds.length <= 1) {
          setFocusedPaneViewId(null)
        } else if (next.ws.focusedPaneId !== prev.ws.focusedPaneId
            || !next.ws.panes[expanded]) {
          setFocusedPaneViewId(next.ws.focusedPaneId)
        }
      }
    }
    dispatchWorkspaceRaw(action)
    // Queue the reducer commit before policy state. React batches both, while the
    // already-advanced ref still lets the request capture the pre-render chat target.
    if (enteredEmptySingle) requestEmptySingleNewChatRef.current?.()
  }, [setFocusedPaneViewId])

  // ── Multi-pane projection (design §2/§4) — computed BEFORE useNavigation so
  // the adapter learns the committed visible pane set. A ResizeObserver on
  // .shell__content drives the mode + geometry; projection is the single
  // geometry authority (one visible leaf is the pixel-identical single-pane
  // sentinel and the renderer emits today's DOM).
  const contentElRef = useRef(null)
  const [contentRect, setContentRect] = useState({ w: 0, h: 0 })
  // Read at placement-dispatch time (below) so the resolver derives the current
  // device mode + pane rects without re-creating placeInWorkspace every resize.
  const contentRectRef = useRef(contentRect)
  contentRectRef.current = contentRect
  useEffect(() => {
    const el = contentElRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    // No layout-bloom transition to suppress anymore (v2 deleted it + its guard
    // class + this 200ms timer): a resize rewrites pane rects, which now snap. A
    // resize during an exit beat instead invalidates the latched plan's content
    // dimensions and cancels the beat (the exit-signature watcher below).
    const ro = new ResizeObserver(() => {
      const w = Math.round(el.clientWidth)
      const h = Math.round(el.clientHeight)
      setContentRect(prev => {
        if (prev.w === w && prev.h === h) return prev
        // Flag-off single-pane never tiles, so a content-size change would
        // re-render Shell for a projection nothing reads. Skip it while the
        // splits flag is off and the tree is a lone leaf (finding F).
        if (!paneModel.WORKSPACE_SPLITS_ENABLED
            && Object.keys(workspaceStateRef.current.ws.panes).length <= 1) return prev
        return { w, h }
      })
    })
    ro.observe(el)
    return () => { ro.disconnect() }
  }, [])
  const workspaceMode = useMemo(() => paneModel.modeForRect(contentRect), [contentRect])
  const baseProjection = useMemo(
    () => paneModel.projectLayout(workspace, workspaceMode, contentRect),
    [workspace, workspaceMode, contentRect],
  )
  const projection = useMemo(
    () => projectFocusedPane(
      baseProjection, workspace, focusedPaneViewId, contentRect,
    ),
    [baseProjection, workspace, focusedPaneViewId, contentRect],
  )
  // The committed visible pane set the nav adapter reads. Settings-open is
  // applied separately inside isVisibleApp, so it is NOT excluded here.
  const visiblePaneIds = useMemo(() => new Set(projection.visibleLeaves), [projection])

  const {
    activeView,
    activeAppId,
    activeChatId,
    drawerOpen, settingsOverlayOpen, settingsOpenRaw, openDrawer, closeDrawer,
    navTo, tabRevealRevision, applyModeDestination, dismissSettings,
    backFiredRef, drawerPushedRef, navStackRef, navigationEpochRef,
    activeViewRef, activeChatIdRef, activeAppIdRef,
    drawerOpenRef,
    appNavPush, appNavPop, appNavReset, appNavForwardResult,
    retireAppHistory, tombstoneRoute,
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
  const closeDrawerRef = useRef(closeDrawer)
  closeDrawerRef.current = closeDrawer
  useEffect(() => {
    if (desktopSidebarMode && drawerOpen) closeDrawerRef.current()
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
  // Builder mode is the tiled 'panes' view-mode (only meaningful when splits can
  // exist). The logo itself is the persistent mode indicator and gesture surface;
  // there is deliberately no separate header control.
  // ── Mode transition machine (modeMachine.js / useModeController) ───────────
  // The ONE descriptor { committedMode, transition } that replaces the old
  // dragPreviewBuilder / builderExiting / builderEntering booleans, their two
  // bare timers, and the ad-hoc effectiveViewMode expression. workspace.viewMode
  // stays the persisted authority; the controller mirrors it and owns only the
  // transient beat, driving completion by epoch (INV 12/15) rather than a timer.
  // Codex review §3: EVERY mode-dependent render fact below derives from
  // mode.state (INV 4), so the reducer and the render can never disagree past one
  // beat — the P0 wedge the review opened with.
  const SPLITS = paneModel.WORKSPACE_SPLITS_ENABLED
  const shellRootRef = useRef(null)
  const mode = useModeController({
    committedMode: workspace.viewMode,
    splitsEnabled: SPLITS,
    rootRef: shellRootRef,
  })
  const modeState = mode.state
  // Keep the focused presentation through a Builder -> Standard exit so the
  // latched beat animates exactly what the user saw. Once the descriptor idles,
  // discard it; Standard mode has no pane-focus presentation to restore.
  useEffect(() => {
    if (workspace.viewMode === 'single' && !modeState.transition
        && focusedPaneViewIdRef.current != null) {
      setFocusedPaneViewId(null)
    }
  }, [workspace.viewMode, modeState.transition, setFocusedPaneViewId])

  const toggleFocusedPaneView = useCallback((paneId) => {
    const ws = workspaceStateRef.current.ws
    if (!ws.panes[paneId] || Object.keys(ws.panes).length <= 1) {
      setFocusedPaneViewId(null)
      return
    }
    if (focusedPaneViewIdRef.current === paneId) {
      setFocusedPaneViewId(null)
      return
    }
    dispatchWorkspace({ type: 'FOCUS', paneId })
    setFocusedPaneViewId(paneId)
  }, [dispatchWorkspace, setFocusedPaneViewId])
  // Builder mode = the committed 'panes' world (logo twist + living halo + power
  // chrome), clamped off by the splits kill switch (INV 16). Flips synchronously
  // with the toggle, matching the gesture's own spring/snap.
  const builderModeActive = modeMachine.builderModeActive(modeState, { splitsEnabled: SPLITS })
  // The mode the render paints: 'panes' while an exit beat OR a single-mode drag
  // preview holds the tiled world; the committed mode otherwise. The single source
  // (INV 4) — no scattered override.
  const effectiveViewMode = modeMachine.effectiveViewMode(modeState, { splitsEnabled: SPLITS })
  const multiPaneBuilderVisible = effectiveViewMode === 'panes'
    && paneModel.paneIdsInOrder(workspace).length > 1
  const multiPaneBuilderVisibleRef = useRef(multiPaneBuilderVisible)
  multiPaneBuilderVisibleRef.current = multiPaneBuilderVisible
  // The latched presentation plan for the live animated beat. Either direction may
  // carry a stationary single-world underlay while panes scatter or assemble over it.
  const beatPlan = modeMachine.transitionPresentation(modeState)
  // A mode beat is live: moving surfaces are inert and app frames cannot intercept
  // input while their compositor layer is in flight.
  const modeBeatActive = !!beatPlan
  const modeUnderlayKey = beatPlan ? beatPlan.underlayKey : null
  // The logo spring-back window on the shell root while an animated beat is live
  // (round 4 item 1): the mark holds .84 through the beat and releases over the
  // terminal logoReleaseMs so its first full-size frame lands at completion. `both`
  // fill on the CSS keyframe holds .84 through the release DELAY. The twist rides
  // --mode-total so rotation, panes, and logo settle together. A short plan clamps
  // the release to the whole beat. Null (no vars) when idle.
  const beatRootVars = useMemo(() => {
    if (!beatPlan) return null
    const total = beatPlan.totalMs
    const release = Math.min(MODE_MOTION.logoReleaseMs, total)
    return {
      '--mode-total': `${total}ms`,
      '--logo-release-ms': `${release}ms`,
      '--logo-release-delay': `${Math.max(0, total - MODE_MOTION.logoReleaseMs)}ms`,
    }
  }, [beatPlan])
  // The key SINGLE mode paints beneath / within either directional beat. It drives
  // destination AppCanvas insets before the first frame so neither direction jumps.
  const beatTargetKey = beatPlan ? beatPlan.target : null
  // key → latched participant, for the render's data-mode-motion + inline vars.
  const beatParticipants = useMemo(() => {
    const m = new Map()
    if (beatPlan) for (const p of beatPlan.participants) m.set(p.key, p)
    return m
  }, [beatPlan])
  // The inline compositor-motion attrs a wrapper (or its strip) carries THIS beat,
  // or null. Only transform/opacity + FLIP variables — never a layout property.
  const wrapperMotion = useCallback((key) => {
    const p = beatParticipants.get(key)
    if (!p) return null
    const vars = { '--mode-duration': `${p.durationMs}ms`, '--mode-delay': `${p.delayMs}ms` }
    if (p.motion === 'promote' && p.flip) {
      vars['--flip-x'] = `${p.flip.x}px`
      vars['--flip-y'] = `${p.flip.y}px`
      vars['--flip-sx'] = p.flip.sx
      vars['--flip-sy'] = p.flip.sy
    }
    if ((p.motion === 'deal-in' || p.motion === 'deal-out') && p.offset) {
      vars['--mode-offset-x'] = `${p.offset.x}px`
      vars['--mode-offset-y'] = `${p.offset.y}px`
    }
    return { motion: p.motion, vars }
  }, [beatParticipants])
  // The wrapper matching the stationary world underlay paints full-bleed beneath.
  const isUnderlay = useCallback(
    (key) => modeBeatActive && modeUnderlayKey != null && key === modeUnderlayKey,
    [modeBeatActive, modeUnderlayKey],
  )
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
  // HONEST EXIT DESTINATION (M2): the exit-plan classifier needs to know what the
  // SINGLE world will actually paint on completion, not just the slot the tree
  // seeds. A suspended single-world takeover (settingsOpenRaw) paints Settings over
  // the slot; a retained immersive request (immersiveAppId) solos the holder over
  // the viewport. Mirrored into refs so the toggle callback (stable identity, no
  // dep churn) and the undo keydown handler (deps [dispatchWorkspace, mode]) both
  // read the LIVE values without a stale closure or re-registering their listeners.
  const settingsDestinationRef = useRef(settingsOpenRaw)
  settingsDestinationRef.current = settingsOpenRaw
  const immersiveHolderRef = useRef(immersiveAppId)
  immersiveHolderRef.current = immersiveAppId

  // INV 10 / H2: a topology, geometry, OR DESTINATION change DURING either animated
  // beat makes its latched FLIP/edge transforms stale. Cancel rather than retarget a
  // live transform: recompute the shared transition signature from the same projection
  // authority and overlay classification both plan builders use. The live overlay state
  // is in the deps, so a mid-beat Settings/immersive destination change also snaps.
  useEffect(() => {
    const t = modeState.transition
    if (!t?.presentation) return
    const live = transitionSignature({
      workspace, projection, contentRect,
      settingsDestination: settingsOpenRaw,
      immersiveHolderId: immersiveAppId,
    })
    if (live !== t.presentation.snapshotSignature) mode.cancelBeat()
  }, [workspace, projection, contentRect, settingsOpenRaw, immersiveAppId, modeState, mode])

  // The single derivation of what content the render paints and where (design
  // §2/§4/§5). Pure + memoized so the immersive-solo and Settings-overlay
  // branches are unit-tested in workspaceView.test.js, and so one commit flips
  // every dependent flag together.
  const contentVisibility = useMemo(
    () => deriveContentVisibility({
      workspace, projection, settingsOverlayOpen: settingsActive,
      immersiveActive, immersiveAppId,
      viewMode: effectiveViewMode, // 'panes' during a single-mode drag preview
      // World-reveal exit: paint the mounted destination beneath the deal (adds its
      // app to visibleAppIds so the underlay is not a blank frame).
      exitUnderlayKey: modeUnderlayKey,
      focusedPaneView: focusedPaneViewId != null,
    }),
    [workspace, projection, settingsActive, immersiveActive, immersiveAppId,
      effectiveViewMode, modeUnderlayKey, focusedPaneViewId],
  )
  const { multiPane, single, focusedActiveKey, fullBleedKey, visibleAppIds } = contentVisibility
  // The EFFECTIVE-mode-gated Settings takeover flag (finding F3): true only when the
  // takeover actually PAINTS — false in builder AND during a single-mode drag
  // preview / exit beat (effectiveViewMode 'panes'). Every PAINT gate below reads
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
  // Reconcile in-memory route hints after every workspace transition (design
  // §5.1.3). navStackRef is stable, so recreating this closure each render is
  // behaviourally identical. reconcileRoutePanes points each hint at the pane
  // that now holds its item (a cross-pane move follows its tab even when the
  // source pane survived) and degrades a dead-pane hint to the structural
  // sibling the collapse chose — NOT global focus, since a background split can
  // be removed while focus is elsewhere. Physical history hints self-correct at
  // restore time (OPEN_TAB dedups an open item to its true pane).
  onWorkspaceTransitionRef.current = (prevWs, nextWs) => {
    navStackRef.current = paneModel.reconcileRoutePanes(navStackRef.current, prevWs, nextWs)
  }

  const { loadTheme } = useTheme()
  const queryClient = useQueryClient()
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
  const apps = appsQuery.data ?? []
  const chats = chatsQuery.data ?? []
  // Warm the model registry as soon as a chat is open so the composer's
  // model picker is instant on the first '+'. The /api/models fetch
  // otherwise runs cold on the first picker open (it's 5-min cached after
  // that); this just moves that one fetch to chat-open time, in the
  // background. Shares the cache key, so the picker's own useQuery reuses it.
  modelQueries.registry.useQuery({ enabled: !!activeChatId })
  modelQueries.prefs.useQuery({ enabled: !!activeChatId })

  // Cache key from app.updated_at (server-side). Stable across reloads.
  const versionForApp = useCallback((id) => {
    const app = apps.find(a => String(a.id) === String(id))
    return appVersionKey(app?.updated_at)
  }, [apps])
  // Warm LRU of recently-VISIBLE app ids (most-recent first) — the unpinned
  // remainder of the iframe budget. Each rendered app stays mounted as a hidden
  // iframe so re-opening it is instant (no module re-fetch, no WebGL re-init).
  // A ref + version counter (not state) because the rendered set is DERIVED
  // synchronously from visibleAppIds ∪ this: visible ids are always pinned, and
  // a post-commit effect would blank a pane whose newly-activated app was never
  // in the LRU (design §2/§4, finding B). Bounded to keep phone memory
  // predictable (each Three.js / WebGL app can hold tens of MB).
  const APP_CACHE_MAX = 4
  const warmLruRef = useRef(
    coldRestoredCanvasAppId != null ? [String(coldRestoredCanvasAppId)] : []
  )
  const [warmVersion, setWarmVersion] = useState(0)
  // Drop every warm-LRU id matching `matches` and bump the version so the
  // synchronous rendered-set derivation re-runs. The version bump is load-bearing
  // (renderedAppIds deps on it); funnelling all four eviction sites through one
  // helper makes it impossible to drop an id without the bump (finding: warm-LRU
  // pattern hand-repeated four times).
  const dropFromWarmLru = useCallback((matches) => {
    if (!warmLruRef.current.some(matches)) return
    warmLruRef.current = warmLruRef.current.filter(id => !matches(id))
    setWarmVersion(v => v + 1)
  }, [])
  const [appIntents, setAppIntents] = useState({})
  // Ids ever observed PRESENT in a fetched /api/apps list. The eviction
  // effect below treats an app as uninstalled only on a genuine
  // present→absent transition (it was here, now it's gone), never on a
  // never-yet-seen id. That distinction is load-bearing: opening an app
  // whose install raced ahead of the apps query (the moebius:open-app
  // stale-list path — refreshApps resolves the new id, navTo adds it to
  // the LRU, but the `apps` derived value lags one render behind) would
  // otherwise look "absent from the live list" for that one render and
  // get wrongly evicted the instant it was opened. Tracking observed-
  // present ids closes the window without a timer: a freshly-opened id
  // hasn't been seen present yet, so it's exempt until the list catches
  // up; a real uninstall flips a previously-seen id to absent and evicts.
  const seenAppIdsRef = useRef(new Set())
  // One-shot guard for the M5 pre-upgrade-blob slot-app validation: the present->
  // absent eviction below only fires for ids SEEN present this session, so a slot
  // app uninstalled while the browser was CLOSED (never seen present) needs a
  // first-authoritative-fetch check, exactly like the cold-restore probe.
  const initialSlotReconciledRef = useRef(false)
  // toast state: null | { message, variant, duration, action }
  // variant: 'info' | 'error'  (see components/ui/Toast.jsx)
  const toastSequenceRef = useRef(0)
  const [toast, setToast] = useState(null)
  const [settingsFocusTarget, setSettingsFocusTarget] = useState(null)
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
  const pendingShellReloadRef = useRef(false)
  // False wins when several requests coalesce: an explicit shell_apply_now
  // promotes an already-pending passive watcher rebuild to deliberate apply.
  const pendingShellReloadPassiveRef = useRef(false)
  const shellReloadTimerRef = useRef(null)
  const lastShellInteractionAtRef = useRef(0)
  // Guards the once-per-mount deferred shell-update pickup effect below.
  const shellUpdatePickupRef = useRef(false)
  const shellUpdatePickupCheckStartedRef = useRef(false)
  const [composerFocusRequest, setComposerFocusRequest] = useState(null)
  const composerFocusTokenRef = useRef(0)

  function requestComposerFocus(chatId) {
    if (chatId == null) return
    composerFocusTokenRef.current += 1
    setComposerFocusRequest({
      chatId,
      token: composerFocusTokenRef.current,
    })
  }

  const handleComposerFocusHandled = useCallback((token) => {
    setComposerFocusRequest(prev => (
      prev?.token === token ? null : prev
    ))
  }, [])

  function shellReloadState() {
    // Derive the compatibility triple from the freshest workspace at WRITE time,
    // not the render-lagging active*Refs (§5.3.10): a workspace action previewed
    // in the same React batch must not persist a fresh blob beside a stale triple.
    // Settings is a global overlay tracked separately from pane content.
    // CURRENT-WORLD projection (activeContentRoute): in single mode the reload
    // triple must name the slot, not the hidden builder focus — the triple only
    // seeds a boot with no valid workspace blob, and that boot lands in single
    // mode showing the slot.
    const content = paneModel.activeContentRoute(workspaceStateRef.current.ws)
    return {
      activeView: activeViewRef.current === 'settings' ? 'settings' : content.view,
      activeAppId: content.appId,
      activeChatId: content.chatId,
      drawerOpen: drawerOpenRef.current,
    }
  }

  async function performShellReload({ passive = false } = {}) {
    let stalePrecache = false
    try { stalePrecache = sessionStorage.getItem('sw-stale-precache-pending') === '1' } catch { /* ignore */ }
    if (stalePrecache && navigator.onLine === false) {
      // An offline reload is safe only while the existing precache remains
      // intact; purging Workbox offline can strand an installed PWA on the
      // fallback page, so stale recovery waits for an online idle boundary.
      deferShellReload({ passive })
      return
    }
    pendingShellReloadRef.current = false
    pendingShellReloadPassiveRef.current = false
    if (shellReloadTimerRef.current) {
      clearTimeout(shellReloadTimerRef.current)
      shellReloadTimerRef.current = null
    }
    // Capture view-owned transient state synchronously, before the async query
    // flush or service-worker handoff can let layout/data change underneath it.
    // ChatView uses this to persist the exact visible message anchor.
    window.dispatchEvent(new Event(BEFORE_SHELL_RELOAD_EVENT))
    // ChatView promotes terminal stream items into the in-memory query cache
    // synchronously before it marks the shell idle. Normal IndexedDB writes
    // are throttled, so a deferred rebuild can otherwise reload between those
    // two phases and hydrate the previous partial. Flush the exact terminal
    // cache as the reload handoff; the backend remains authoritative on the
    // immediate background revalidation. This follows the synchronous event
    // above so view-owned anchors are captured before the first await.
    await awaitCacheFlushBeforeReload(flushPersistedQueryCache(queryClient))
    // Workspace-first restore (§5.3.10): synchronously persist the latest
    // workspace blob (tree/focus/active tabs are authoritative on boot) and the
    // compatibility triple derived from its focused pane, so a valid blob wins
    // over shellReload.active* and this handoff never destroys the just-persisted
    // pane state.
    try {
      sessionStorage.setItem(
        paneModel.STORAGE_KEY,
        paneModel.serializeWorkspace(workspaceStateRef.current.ws),
      )
    } catch { /* private mode / quota — the in-memory workspace still boots */ }
    sessionStorage.setItem('shell-reload', JSON.stringify(shellReloadState()))
    // Match the manifest scope so the post-reload page lands inside
    // the installed PWA's declared scope — writing `/` here would
    // briefly put the page out of scope and Chromium can refuse the
    // next manifest update in-place.
    replaceNavEntry('base', '/shell/')
    // SW UPDATE LEASH (sw.js): the new service worker installed and is WAITING
    // — it never skipWaiting()s on its own. THIS is the one moment we hand it
    // control, so the SW generation flips exactly when the page generation does.
    // Mark the reload page-initiated first so index.html's controllerchange
    // handler treats any resulting controllerchange as OURS (an expected apply),
    // not a spontaneous background SW flip to suppress.
    try { sessionStorage.setItem('sw-skip-initiated', '1') } catch { /* ignore */ }

    // Deferred stale-precache recovery (flagged by index.html at boot): a
    // Chromium bug can keep serving the old precached index even after sw.js
    // advertises a new bundle. Clearing Workbox's precache forces the reload to
    // fall through to the network for index.html + the new hashed assets. Done
    // HERE, at the same idle boundary as the reload, instead of index.html's old
    // boot-time force-reload — so it can never blank a live turn.
    if (stalePrecache) {
      if (typeof caches !== 'undefined') {
        try {
          const keys = await caches.keys()
          await Promise.all(
            keys.filter(k => k.startsWith('workbox-precache-')).map(k => caches.delete(k)),
          )
        } catch { /* best-effort — the reload still self-heals via updateViaCache */ }
      }
      try { sessionStorage.removeItem('sw-stale-precache-pending') } catch { /* ignore */ }
      // Loop-prevention: index.html's boot check reads this and skips
      // re-flagging on the recovered load (then clears it).
      try { sessionStorage.setItem('sw-stale-precache-recovering', '1') } catch { /* ignore */ }
    }

    // Hand control to the waiting worker (if any) and reload only once it has
    // actually TAKEN OVER — the waiting worker reaching 'activated' (or a
    // controllerchange), with a bounded fallback if the SW wedges. A blind
    // ~220ms timer here used to reload before skipWaiting()->activate finished
    // on a client's first update cycle, so the navigation was answered by the
    // OUTGOING worker's precache and the page came back on the old generation
    // and stuck (feature 207). No waiting worker (unchanged sw.js, e.g. a
    // backend-only rebuild) → reload immediately: the reload alone re-fetches
    // the current generation. The boot-time re-arm net (shouldRearmShellApply,
    // mount effect below) still catches a stale landing if the fallback fires.
    const doReload = () => window.location.reload()
    if (navigator.serviceWorker?.getRegistration) {
      navigator.serviceWorker.getRegistration()
        .then(reg => reloadWhenWorkerTakesOver({
          registration: reg,
          serviceWorker: navigator.serviceWorker,
          reload: doReload,
        }))
        .catch(doReload)
    } else {
      doReload()
    }
  }

  function shellReloadWouldDisruptUser({ passive = false } = {}) {
    return shouldDeferShellReload({
      activeElement: document.activeElement,
      activeView: activeViewRef.current,
      activeChatId: activeChatIdRef.current,
      multiPaneBuilderVisible: multiPaneBuilderVisibleRef.current,
      streamingChatIds: streamingChatIdsRef.current,
      passiveRebuild: passive,
      voiceDictationActive: voiceDictationActiveRef.current,
      lastUserInteractionAt: lastShellInteractionAtRef.current,
      visibilityState: document.visibilityState,
    })
  }

  function shellReloadHasStableVisibleHold(passive) {
    if (document.visibilityState === 'hidden') return false
    if (multiPaneBuilderVisibleRef.current) return true
    return passive
      && activeViewRef.current === 'chat'
      && activeChatIdRef.current != null
  }

  function checkPendingShellReload() {
    if (!pendingShellReloadRef.current) return
    const passive = pendingShellReloadPassiveRef.current
    if (shellReloadWouldDisruptUser({ passive })) {
      // Stable visible holds (the whole Builder workspace, or a passive watcher
      // while reading a chat) have no deadline. Wait for the view/mode/visibility
      // effects below instead of waking the page every six seconds.
      if (!shellReloadHasStableVisibleHold(passive)) scheduleShellReloadCheck()
    } else {
      performShellReload({ passive })
    }
  }

  function scheduleShellReloadCheck() {
    if (shellReloadTimerRef.current) clearTimeout(shellReloadTimerRef.current)
    shellReloadTimerRef.current = setTimeout(() => {
      shellReloadTimerRef.current = null
      checkPendingShellReload()
    }, SHELL_RELOAD_RECHECK_MS)
  }

  function deferShellReload({ passive = false } = {}) {
    pendingShellReloadPassiveRef.current = pendingShellReloadRef.current
      ? (pendingShellReloadPassiveRef.current && passive)
      : passive
    pendingShellReloadRef.current = true
    if (!shellReloadHasStableVisibleHold(pendingShellReloadPassiveRef.current)) {
      scheduleShellReloadCheck()
    }
  }

  function requestShellReload({ passive = false } = {}) {
    if (shellReloadWouldDisruptUser({ passive })) {
      deferShellReload({ passive })
    } else {
      performShellReload({ passive })
    }
  }

  useEffect(() => {
    const record = () => { lastShellInteractionAtRef.current = Date.now() }
    const releaseWhenHidden = () => {
      if (document.visibilityState === 'hidden') checkPendingShellReload()
    }
    const opts = { capture: true, passive: true }
    window.addEventListener('pointerdown', record, opts)
    window.addEventListener('touchstart', record, opts)
    window.addEventListener('keydown', record, opts)
    window.addEventListener('input', record, opts)
    window.addEventListener('focusin', record, opts)
    document.addEventListener('visibilitychange', releaseWhenHidden)
    return () => {
      window.removeEventListener('pointerdown', record, opts)
      window.removeEventListener('touchstart', record, opts)
      window.removeEventListener('keydown', record, opts)
      window.removeEventListener('input', record, opts)
      window.removeEventListener('focusin', record, opts)
      document.removeEventListener('visibilitychange', releaseWhenHidden)
      if (shellReloadTimerRef.current) clearTimeout(shellReloadTimerRef.current)
    }
  }, [])

  // Release a stable visible hold as soon as the owner leaves the chat surface
  // or returns from Builder to Standard. Switching between chats remains
  // protected for a passive generation.
  useEffect(() => {
    checkPendingShellReload()
  }, [activeView, activeChatId, multiPaneBuilderVisible])
  // Global connectivity indicator. The composer already disables send when
  // offline (ChatView); this surfaces the state shell-wide so the user is
  // never tapping in the dark about whether they're connected.
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
  // validation + creation runs only AFTER the mode descriptor idles, so the slot write
  // never drifts a live exit signature and cancels its own beat. The request is a
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
  const [newChatLandingOffline, setNewChatLandingOffline] = useState(false)
  // Live mirror of the mode descriptor so the async materialize can re-check for a
  // beat that started during its await (writing the slot mid-beat would cancel it).
  const modeTransitionRef = useRef(modeState.transition)
  modeTransitionRef.current = modeState.transition
  // Every mounted chat pane derives its OWN built-app CTA list per chatId inside
  // PaneChatView (builtAppState.js), so Shell no longer holds a global builtApps
  // bound to a single activeChatId.

  // ── Tabs: the flat projection of the workspace (the reducer + wrapper are
  // declared above useNavigation). openTabs is the in-order flat walk that
  // today's single top strip renders.
  const openTabs = useMemo(() => paneModel.flatten(workspace), [workspace])
  // Becoming a two-tab workspace engages the strip; returning to zero resets it.
  // A single implicit home tab on a fresh session stays visually identical to
  // the pre-workspace shell. State (rather than a render-time ref mutation) keeps
  // this safe under replayed or abandoned concurrent renders.
  const [tabStripEngaged, setTabStripEngaged] = useState(legacyOpenTabs.length > 0)
  useEffect(() => {
    if (openTabs.length >= 2) setTabStripEngaged(true)
    else if (openTabs.length === 0) setTabStripEngaged(false)
  }, [openTabs.length])
  // Dual-write on every workspace commit: the versioned blob is authoritative on
  // boot, and the legacy flat key is mirrored for one release so a rolled-back
  // client still finds its tabs. readOpenTabs keeps the LAST MAX_TABS, so the
  // rollback ordering puts the most relevant tabs (focused pane, active last).
  useEffect(() => {
    try {
      sessionStorage.setItem(paneModel.STORAGE_KEY, paneModel.serializeWorkspace(workspace))
    } catch { /* private mode / quota — workspace stays in memory only */ }
    tabModel.writeOpenTabs(
      tabStripEngaged
        ? paneModel.flattenRollbackPriority(workspace)
        : [],
    )
  }, [tabStripEngaged, workspace])
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
  // AFTER the mode descriptor idles (the materialize watcher below), so the slot write
  // never drifts a live exit signature and cancels its own beat. The candidate is
  // captured from the PRE-transition active chat but NOT targeted synchronously — the
  // reuse policy (newChatPolicy) is deliberately provisional (has_messages can be
  // stale cross-client), so it must survive its detail validation before it becomes
  // the slot. The workspace dispatch boundary calls this for every edge into an empty
  // single screen; the old "null is legitimate only at zero chats" invariant is
  // retired.
  const requestEmptySingleNewChat = useCallback(() => {
    const ws = workspaceStateRef.current.ws
    const single = !paneModel.WORKSPACE_SPLITS_ENABLED || ws.viewMode === 'single'
    if (!single || ws.singleScreen != null) return
    const candidate = currentReusableEmptyChat(chatsRef.current, {
      activeChatId: activeChatIdRef.current,
      recoveredChatIds: recoveredChatIdsRef.current,
      streamingChatIds: streamingChatIdsRef.current,
    })
    const token = newChatRequestSeqRef.current + 1
    newChatRequestSeqRef.current = token
    pendingNewChatRef.current = { token, candidateId: candidate ? candidate.id : null }
    setNewChatLandingOffline(false)
    setPendingNewChatToken(token)
  }, [workspaceStateRef, activeChatIdRef])
  requestEmptySingleNewChatRef.current = requestEmptySingleNewChat
  const closeTab = useCallback((tab, { reason } = {}) => {
    const key = tabModel.tabKey(tab)
    const ws = workspaceStateRef.current.ws
    // R3 (auto-return through the descriptor): a user close that EMPTIES the builder
    // tree auto-returns to single (the reducer's autoReturnIfEmptied). Arm the SAME
    // flip on the mode descriptor in the SAME batch (cause 'auto') so committedMode
    // flips to single NOW — not a frame later via the passive sync-committed
    // reconcile, which left the logo twisted for one intermediate frame. An emptied
    // tree has no pane to deal out, so the exit is instant (null presentation); the
    // tree's coupled undo re-enters builder as one gesture. A sole Settings tab is
    // no exception — closing it empties the tree the same way. reason:'deleted' does
    // not auto-return (the reducer skips it), so it is excluded here too.
    if (SPLITS && reason !== 'deleted' && ws.viewMode === 'panes'
        && !paneModel.isEmptyTree(ws) && paneModel.isEmptyTree(paneModel.closeTab(ws, key))) {
      mode.toggle({ cause: 'auto', to: 'single' })
    }
    dispatchWorkspace({ type: 'CLOSE_TAB', tabKey: key, reason })
    // If this auto-returned into a never-seeded single slot, dispatchWorkspace owns
    // the New Chat request. The coupled undo still restores tab + builder together.
  }, [dispatchWorkspace, mode, workspaceStateRef])
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
    const mode = paneModel.modeForRect(contentRect)
    const liveApps = appsRef.current
    // R2: a FOREGROUND agent open in the SINGLE world writes the slot (via the pure
    // resolver's F4 branch) BENEATH an open Settings takeover, so the item would be
    // invisible. Dismiss the takeover alongside the placement — exactly as a
    // user-initiated open does — so the foregrounded item is actually shown. Only in
    // single (in builder the takeover is suspended, and clearing settingsOpen there
    // would unmount the mounted-hidden SettingsView). dismissSettings no-ops when no
    // takeover is open.
    const currentWs = workspaceStateRef.current.ws
    const world = paneModel.WORKSPACE_SPLITS_ENABLED ? currentWs.viewMode : 'single'
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
      resolve: (ws) => resolveWorkspaceRequests(ws, requests, { mode, contentRect, liveApps }),
    })
  }, [dispatchWorkspace, dismissSettings])
  // The tab strip is the BUILDER SURFACE: with splits ON it follows the
  // EFFECTIVE builder world exactly — always present in builder (even at a
  // single leaf, where this single-pane .shell__tabstrip stands in for the
  // tiled WorkspaceChrome strips, giving phone users the drag source), riding
  // an exit beat or a single-mode drag preview with the rest of the tiled
  // presentation, and NEVER rendered in single mode OR over an immersive lease
  // (the shell exit replaces every builder navigation surface). The legacy
  // tabStripEngaged latch is
  // the KILL-SWITCH world's rule only (engaged after 2+ tabs) — letting it
  // leak into the flag-ON formula painted the parked builder tree's strip
  // over single mode whenever the latch was set. An empty workspace (no tabs)
  // shows nothing either way — the >= 1 gate stays.
  const tabStripVisible = !immersiveActive
    && (SPLITS ? effectiveViewMode === 'panes' : tabStripEngaged)
    && openTabs.length >= 1

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

  // (v2: the exit-beat wrapper-rect substitution is DELETED. Panes hold their tiled
  // content rect through the beat; a promote pane FLIPs via transform to cover the
  // full box while departures deal out, so computed top/left/width/height never
  // change until the descriptor clears and the destination snaps to full-bleed in
  // one commit. visibleTabRects is now the sole tiled-geometry authority.)

  // ── The ONE Settings wrapper (design §4: overlay-or-pane geometry) ─────────
  // A single, stable SettingsView mount that is positioned like any chat/app
  // content when Settings is a visible builder tab, and full-bleed when the
  // takeover overlay is up. Keeping it ONE element (never two conditional mounts)
  // preserves component identity across the tab<->overlay mode conversion, so the
  // scroll position and transient Settings state survive the flip.
  const SETTINGS_KEY = tabModel.SETTINGS_TAB_KEY
  // Visible as a builder TAB: the takeover is not PAINTING AND some visible pane has
  // the Settings tab active. Gated on the effective `settingsOverlay` (finding F3),
  // so a single-mode drag preview / exit beat can paint a stray Settings tab as a
  // pane. (Blind to a BACKGROUND Settings tab — not painted.)
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
  // The flat, chatId-sorted set of visible CHAT panes to mount as PaneChatViews
  // — for EVERY mode including single-pane (finding A): DOM identity across 1↔2
  // panes is the invariant, so the first split never remounts the visible chat.
  // Stable order (same no-reparent rule as the app iframes). Panes stay mounted
  // (hidden) behind the Settings overlay, exactly like the app iframes.
  const visibleChatPanes = useMemo(() => {
    const out = []
    const mountedChatIds = new Set()
    // While one pane is focused, keep the base projection's chats mounted-hidden.
    // Defocusing then changes geometry/visibility only — no transcript refetch,
    // stream teardown, or scroll-state loss for the sibling panes.
    const mountedPaneIds = new Set([
      ...baseProjection.visibleLeaves,
      ...projection.visibleLeaves,
    ])
    for (const paneId of mountedPaneIds) {
      const pane = workspace.panes[paneId]
      const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
      if (active && active.kind === 'chat') {
        out.push({ paneId, chatId: active.id })
        mountedChatIds.add(String(active.id))
      }
    }
    // TWO-WORLDS mount identity: union the single-screen SLOT chat, mounted under a
    // stable synthetic single-world owner even while builder shows, so a world
    // switch changes VISIBILITY not mounts (no ChatView remount, no stream flush,
    // no scroll loss). Deduped against the tree-visible chats — never two ChatViews
    // for one chat (design: no duplicate mounts). When the slot chat is already a
    // visible tree pane, that pane's mount covers it and no synthetic mount is added.
    const slot = workspace.singleScreen
    if (slot && slot.kind === 'chat' && !mountedChatIds.has(String(slot.id))) {
      out.push({ paneId: paneModel.SINGLE_SLOT_PANE, chatId: slot.id })
    }
    return out.sort((a, b) => String(a.chatId).localeCompare(String(b.chatId)))
  }, [baseProjection, projection, workspace])
  // The chat keys that actually PAINT (as opposed to merely being mounted): in
  // single mode ONLY the slot's chat (fullBleedKey), in builder every visible
  // pane's active chat. Separating painting from mounting is what lets the slot
  // chat sit mounted-but-hidden in builder and become visible on a world switch
  // without a remount (two-worlds design).
  const visibleChatKeys = useMemo(() => {
    const set = new Set()
    if (settingsOverlay) return set
    if (single) {
      if (fullBleedKey && fullBleedKey.startsWith('chat:')) set.add(fullBleedKey)
      return set
    }
    for (const paneId of projection.visibleLeaves) {
      const pane = workspace.panes[paneId]
      const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
      if (active && active.kind === 'chat') set.add(`chat:${active.id}`)
    }
    // World-reveal exit: paint the (tree-absent) underlay chat beneath the deal.
    if (modeUnderlayKey && modeUnderlayKey.startsWith('chat:')) set.add(modeUnderlayKey)
    return set
  }, [single, settingsOverlay, fullBleedKey, projection, workspace, modeUnderlayKey])

  // Last chat that reached a stable painted frame in each visible pane. On a
  // chat-tab change, keep that outgoing ChatView mounted as an inert cover while
  // the incoming chat runs its existing hide/restore/reveal transaction below.
  // The map advances only from the incoming ChatView's layout-ready callback,
  // so rapid A -> B -> C navigation keeps A painted and replaces only staging B.
  const [presentedChatByPane, setPresentedChatByPane] = useState(() => new Map())
  const visibleChatPaneSignature = visibleChatPanes
    .map(({ paneId, chatId }) => `${paneId}:${chatId}`)
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
    const pane = workspaceStateRef.current.ws.panes[paneId]
      || workspaceStateRef.current.ws.panes[paneKey]
    // Ignore a late ready signal from staging B after rapid navigation reached C.
    if (pane?.activeTabKey !== `chat:${id}`) return
    setPresentedChatByPane(prev => {
      if (String(prev.get(paneKey) ?? '') === id) return prev
      const next = new Map(prev)
      next.set(paneKey, id)
      return next
    })
  }, [workspaceStateRef])

  // At most two ChatViews per transitioning pane: the last painted chat and the
  // current active chat. Cross-pane moves are deduped by chat id, preserving the
  // workspace's no-reparent identity rule rather than manufacturing a cover.
  const chatPaneLayers = useMemo(() => {
    const desiredIds = new Set(visibleChatPanes.map(({ chatId }) => String(chatId)))
    const layers = []
    for (const { paneId, chatId } of visibleChatPanes) {
      const paneKey = String(paneId)
      const activeId = String(chatId)
      const previousId = presentedChatByPane.get(paneKey)
      const transitioning = previousId && previousId !== activeId
      if (transitioning && !desiredIds.has(previousId)) {
        layers.push({ paneId, chatId: previousId, role: 'held' })
      }
      layers.push({
        paneId,
        chatId: activeId,
        role: transitioning ? 'staging' : 'active',
      })
    }
    return layers.sort((a, b) => String(a.chatId).localeCompare(String(b.chatId)))
  }, [presentedChatByPane, visibleChatPanes])

  // ── Synchronous pinned iframe-cache derivation (design §2/§4) ─────────────
  // renderedAppIds = sortById(visibleAppIds ∪ boundedWarmLRU). Visible ids come
  // from the projection REGARDLESS of LRU membership and are never evicted, so a
  // MOVE_TAB that makes a never-visited app visible materializes its wrapper in
  // the SAME commit — no post-commit effect, no blank pane (finding B). The set is
  // bounded by APP_CACHE_MAX so it never renders five frames to preserve history
  // (§4.1.4). AppCanvas retires physical history in a layout-effect cleanup as a
  // live frame is swapped or unmounted. Keeping retirement out of this derivation
  // is load-bearing: React may replay or abandon a render, and render-time registry
  // mutation would retire a frame that remains committed.
  const renderedAppIds = useMemo(() => {
    const result = new Set()
    for (const id of visibleAppIds) result.add(String(id))
    // TWO-WORLDS mount identity (design risk 1): PIN the single-screen slot app
    // even while builder shows, so a world switch never LRU-evicts its iframe or
    // retires its history. Added BEFORE the warm cap, so with four visible builder
    // apps the earned maximum becomes five pinned frames (visible + 1); the warm
    // LRU then fills only the remaining capacity.
    const slot = workspace.singleScreen
    if (slot && slot.kind === 'app') result.add(String(slot.id))
    for (const id of warmLruRef.current) {
      if (result.size >= APP_CACHE_MAX) break
      result.add(String(id))
    }
    return [...result].sort((a, b) => Number(a) - Number(b))
  }, [visibleAppIds, warmVersion, workspace.singleScreen])

  // Maintain the warm LRU as the visible set changes: currently-visible apps are
  // the most-recent entries, and a just-hidden app slides into the warm remainder
  // (capped). Retirement is NOT done here — it happens synchronously in the
  // derivation above, before the unmount (§4.1.2). This effect only rotates the
  // bounded warm list and bumps the version so the memo re-derives. AppCanvas's
  // layout cleanup owns retirement when a resulting eviction actually commits.
  useEffect(() => {
    const visible = [...visibleAppIds].map(String)
    const prevWarm = warmLruRef.current
    const merged = [...visible, ...prevWarm.filter(id => !visible.includes(id))].slice(0, APP_CACHE_MAX)
    const changed = merged.length !== prevWarm.length || merged.some((id, i) => id !== prevWarm[i])
    if (changed) {
      warmLruRef.current = merged
      setWarmVersion(v => v + 1)
    }
  }, [visibleAppIds])

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
    const single = !paneModel.WORKSPACE_SPLITS_ENABLED || ws.viewMode === 'single'
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

  // The tab context menu is the ONLY split path in PR2. Split/Move items exist
  // only when the workspace-splits flag is on (stage-A inert default); Close tab
  // is always offered. The top strip attaches this handler only when the flag is
  // on, so single-pane right-click keeps today's native menu (parity).
  const [tabMenu, setTabMenu] = useState(null)
  const tabMenuRef = useRef(null)
  const tabMenuReturnFocusRef = useRef(null)
  const openTabMenu = useCallback((e, tab, paneId) => {
    e.preventDefault()
    const owner = paneId || paneModel.paneOf(workspace, tabModel.tabKey(tab))?.id
    if (!owner) return
    tabMenuReturnFocusRef.current = e.currentTarget
    setTabMenu({ x: e.clientX, y: e.clientY, tab, tabKey: tabModel.tabKey(tab), paneId: owner })
  }, [workspace])
  // Coordinate variant for the drag controller's touch lift→release-in-place
  // path (design §3.1) — same menu, opened at a point with no trigger element to
  // restore focus to. Reads the workspace from the ref so an async open (a
  // settled drag) sees the current tree.
  const openTabMenuAt = useCallback((x, y, tab, paneId) => {
    const owner = paneId || paneModel.paneOf(workspaceStateRef.current.ws, tabModel.tabKey(tab))?.id
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
  // A context menu must be keyboard-ready when it appears, and pointer
  // coordinates near a viewport edge must not place actions off-screen.
  useLayoutEffect(() => {
    if (!tabMenu || !tabMenuRef.current) return
    const menu = tabMenuRef.current
    const rect = menu.getBoundingClientRect()
    const gutter = 8
    menu.style.left = `${Math.max(gutter, Math.min(tabMenu.x, window.innerWidth - rect.width - gutter))}px`
    menu.style.top = `${Math.max(gutter, Math.min(tabMenu.y, window.innerHeight - rect.height - gutter))}px`
    menu.querySelector('[role="menuitem"]')?.focus()
  }, [tabMenu])
  const handleTabMenuKeyDown = useCallback((e) => {
    const items = [...(tabMenuRef.current?.querySelectorAll('[role="menuitem"]') || [])]
    if (items.length === 0) return
    const current = Math.max(0, items.indexOf(document.activeElement))
    let next = null
    if (e.key === 'ArrowDown') next = (current + 1) % items.length
    else if (e.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = items.length - 1
    if (next == null) return
    e.preventDefault()
    items[next].focus()
  }, [])
  useEffect(() => {
    if (!tabMenu) return
    const onDown = (e) => { if (!e.target.closest?.('.workspace__menu')) closeTabMenu(false) }
    const onKey = (e) => { if (e.key === 'Escape') closeTabMenu() }
    document.addEventListener('pointerdown', onDown, true)
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('pointerdown', onDown, true)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [closeTabMenu, tabMenu])

  // ── Workspace drag controller wiring (design §3, PR3) ─────────────────────
  // All of this is gated behind WORKSPACE_SPLITS_ENABLED — the hook installs no
  // listeners when the flag is off, so the default build is byte-unchanged.
  // Volatile inputs travel through refs so the hook installs its single
  // document-level pointerdown listener exactly once (never re-registers).
  // dragActiveRef is declared above useNavigation (the drawer OPEN path reads it).
  const sceneInputsRef = useRef(null)
  sceneInputsRef.current = { projection, mode: workspaceMode, contentRect }
  const labelForTabRef = useRef(labelForTab)
  labelForTabRef.current = labelForTab
  const openTabMenuAtRef = useRef(openTabMenuAt)
  openTabMenuAtRef.current = openTabMenuAt
  // A single-mode drag previews the builder world through the ONE descriptor
  // (INV 5): arm is phase 'drag-preview', and the id it mints is carried to the
  // matching commit/cancel so a stale end-event from a superseded drag is
  // ignored. A COMMITTED drop dispatches drag-commit in the SAME pointerup
  // batch as the drop's OPEN_TAB_AT (which flips viewMode to 'panes'), so the
  // descriptor and the tree flip as ONE transaction (INV 7) — the passive
  // sync-committed reconcile stays a pure hydration net, never the beat path.
  // A rejected/no-op drop cancels and mutates nothing.
  const dragPreviewIdRef = useRef(null)
  const onModeDragPreview = useCallback((active, { committed = false } = {}) => {
    if (active) {
      dragPreviewIdRef.current = mode.dragArm()
    } else {
      if (committed) mode.dragCommit(dragPreviewIdRef.current)
      else mode.dragCancel(dragPreviewIdRef.current)
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
    // Build the latched presentation plan from the PROJECTION authority (exit-design
    // v2). The plan owns ALL of the classification the old handler computed inline:
    // promote a genuinely-shared pane vs reveal the single world underneath, the
    // FLIP rects, shared short timing, and the completion contract. The machine treats
    // it opaquely; the controller drops it under reduced motion (commit directly). A
    // null plan (empty tree) is an instant flip. Settings needs no conversion — its
    // tab SURVIVES the flip and single mode paints its own slot (never Settings).
    // Durable flip FIRST. The synchronous dispatch boundary requests the New Chat
    // landing if this enters a null slot (e.g. a Settings-focused builder never
    // seeded one). The slot stays null through the beat, so exitTargetKey reveals
    // home:new-chat and materialization waits for the descriptor to idle. THEN derive
    // the plan from the already-advanced workspace (INV 2/3).
    dispatchWorkspace({ type: 'SET_VIEW_MODE', mode: 'toggle' })
    const settled = workspaceStateRef.current.ws
    const presentation = leavingBuilder
      ? deriveExitPlan({
        // The tree is identical across the flip; only viewMode/slot advanced.
        workspace: settled, projection, contentRect,
        // M2: reveal to Settings / classify immersive instant, not the slot the
        // takeover or immersive-solo covers at completion.
        settingsDestination: settingsDestinationRef.current,
        immersiveHolderId: immersiveHolderRef.current,
      })
      : deriveEnterPlan({
        workspace: settled, projection, contentRect,
        settingsDestination: settingsDestinationRef.current,
        immersiveHolderId: immersiveHolderRef.current,
      })
    // The honest `cause` ('hold'|'swipe'|'keyboard') threads from the caller;
    // an omitted cause falls back to 'toggle'. RETURN the
    // toggle receipt so the logo gesture can tell an animated beat from an instant
    // flip and hand its compression to the descriptor (round 4 item 1).
    return mode.toggle({ cause, presentation })
  }, [dispatchWorkspace, mode, projection, contentRect])
  // The single-tap navigation toggle passed to ShellBrand (which now owns the logo
  // gesture + living halo). The HOLD / swipe / Shift+Enter mode toggle is
  // handleToggleViewMode above, passed to ShellBrand as onToggleMode.
  const handleToggleNavigation = useCallback(() => {
    if (persistentDrawer) {
      setDesktopSidebarOpen(!desktopSidebarOpen)
      return
    }
    drawerOpen ? closeDrawer() : openDrawer()
  }, [persistentDrawer, desktopSidebarOpen, setDesktopSidebarOpen, drawerOpen, closeDrawer, openDrawer])
  useWorkspaceDrag({
    enabled: paneModel.WORKSPACE_SPLITS_ENABLED,
    contentElRef,
    sceneInputsRef,
    workspaceStateRef,
    dispatchWorkspace,
    labelForTabRef,
    dragActiveRef,
    drawerOpenRef,
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
    if (!paneModel.WORKSPACE_SPLITS_ENABLED) return undefined
    const onKey = (e) => {
      if (!undoKeyPressed(e) || isEditableTarget(document.activeElement)) return
      e.preventDefault()
      // A mode-restoring undo (single-leaf drop, empty-builder auto-return) routes
      // through the controller FIRST (INV 2/3) so its re-entry/exit deal fires as one
      // gesture, not a passive sync a render later. undo.restoreViewMode reverts the
      // snapshot's mode; every other undo carries the current mode forward
      // (restoredMode === current), so mode.undo is a no-op there. The presentation
      // plan is built from the tree the beat animates: re-entering builder deals in
      // the RESTORED tree; exiting to single deals the CURRENT tiled tree out.
      const wsState = workspaceStateRef.current
      const undoSlot = wsState.undo
      if (undoSlot) {
        const restoredMode = undoSlot.restoreViewMode
          ? undoSlot.ws.viewMode : wsState.ws.viewMode
        if (restoredMode !== wsState.ws.viewMode) {
          const scene = sceneInputsRef.current
          const presentation = restoredMode === 'panes'
            ? deriveEnterPlan({
              workspace: undoSlot.ws,
              projection: paneModel.projectLayout(undoSlot.ws, scene.mode, scene.contentRect),
              contentRect: scene.contentRect,
              settingsDestination: settingsDestinationRef.current,
              immersiveHolderId: immersiveHolderRef.current,
            })
            : deriveExitPlan({
              workspace: wsState.ws, projection: scene.projection, contentRect: scene.contentRect,
              // M2: same honest-destination classification for an undo that exits
              // builder — Settings/immersive still own the single world it lands in.
              settingsDestination: settingsDestinationRef.current,
              immersiveHolderId: immersiveHolderRef.current,
            })
          mode.undo({ restoredMode, presentation })
        }
      }
      dispatchWorkspace({ type: 'UNDO_LAST' })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dispatchWorkspace, mode])

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
      if (chat.running || chat.run_status === 'running') next.add(chat.id)
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

  // Cross-session recency for SW cache warming. The persisted LRU read
  // once at mount (useState initializer, so the persist effect below
  // can't clobber it first) feeds the warm-on-load effect; every rendered-set
  // change then MERGES into storage rather than overwriting, keeping depth
  // WARM_APP_LIMIT across sessions. Failures degrade to pinned-only warming.
  const [initialAppLru] = useState(() => {
    try {
      return parseStoredAppLru(localStorage.getItem(APP_LRU_STORAGE_KEY))
    } catch { return [] }
  })
  useEffect(() => {
    // The empty mount state carries no recency information — persisting
    // it would erase the previous session's signal before it's used.
    if (renderedAppIds.length === 0) return
    try {
      const stored = parseStoredAppLru(localStorage.getItem(APP_LRU_STORAGE_KEY))
      localStorage.setItem(
        APP_LRU_STORAGE_KEY, JSON.stringify(mergeAppLru(renderedAppIds, stored)),
      )
    } catch { /* storage unavailable (private mode) — warming degrades */ }
  }, [renderedAppIds])

  // Posts a precache-warming message to the service worker for one app.
  // The SW handler (moebius:precache-app in sw.js) fetches frame + module
  // with cache:'reload' and stores them under token-stripped keys, so the
  // next open of the app is a pure cache read. The module endpoint 401s
  // without a token, so one is resolved through the SAME query key the
  // open path uses (priming that cache is a free side benefit); passing
  // it as a query param mirrors the controlled AppCanvas module broker.
  // Safe to call speculatively — the SW skips already-cached entries.
  const warmAppCode = useCallback(async (app) => {
    try {
      const token = await queryClient.fetchQuery({
        queryKey: appQueries.token.key(app.id),
        queryFn: () => appQueries.token.fetch(app.id),
        staleTime: 5 * 60_000,
      })
      const version = appVersionKey(app.updated_at)
      // Mirror AppCanvas exactly: fold the frame-file content rev
      // (meta[mobius-frame-rev]) into `?v=` so the SW pre-warms the SAME
      // cache key the open path opens. Without it a frame-file redeploy
      // leaves the pre-warm a miss on first open (AppCanvas still loads
      // correctly via its own revved URL).
      const frameRev =
        (typeof document !== 'undefined' &&
          document.querySelector('meta[name="mobius-frame-rev"]')?.content) || ''
      const frameUrl =
        `${BASE}/api/apps/${app.id}/frame?v=${encodeURIComponent(version)}${frameRev ? '-' + frameRev : ''}`
      const moduleUrl =
        `${BASE}/api/apps/${app.id}/module?v=${encodeURIComponent(version)}`
        + `&token=${encodeURIComponent(token)}`
      await requestAppCodeWarm({ frameUrl, moduleUrl })
    } catch { /* best-effort — warming must never break the shell */ }
  }, [queryClient])

  // Pane-aware tombstone eviction (design §1). When an app is uninstalled out of
  // band (feature 110 soft-delete) it drops out of /api/apps and the server 404s
  // its /module + /frame, but its iframe stays mounted (a workspace tab and/or a
  // warm cached frame). Reconcile against the live list: a confirmed-gone app has
  // its history retired, its nav-stack routes scrubbed, and its tab CLOSED in ITS
  // OWN pane — the reducer activates that pane's neighbour or collapses it, never
  // globally demoting the focused pane unless that IS the pane it closes.
  //
  // Gate on a live-confirmed list (isSuccess + isFetchedAfterMount): a
  // transiently-empty `apps` (cold cache, a refetch that resolved to []) must not
  // evict valid apps.
  const appsLiveFetched = appsQuery.isSuccess && appsQuery.isFetchedAfterMount
  useEffect(() => {
    if (!appsLiveFetched) return
    const liveIds = new Set(apps.map(a => a.id))
    // Record everything the live list currently shows, so a later
    // disappearance reads as a real uninstall rather than a never-seen id.
    for (const id of liveIds) seenAppIdsRef.current.add(id)
    // NOTE (H1): the single-world SLOT app is pinned even while builder paints and is
    // never "seen present" when it was uninstalled while the browser was CLOSED, so
    // this present->absent eviction can't cover it. Its one-shot validation lives in
    // the dedicated 404-probe effect below — an AUTHORITATIVE per-app check, never a
    // trust of this NetworkFirst list's absence (a stale SW cache fallback would else
    // delete a still-installed slot app's tab/slot/history on a slow/offline launch).
    // Candidates: every mounted app frame (rendered set) plus every app tab.
    const candidates = new Set(renderedAppIds.map(String))
    for (const tab of openTabs) if (tab.kind === 'app') candidates.add(String(tab.id))
    if (candidates.size === 0) return
    // Never evict an app a back-stack entry still points at (a NetworkFirst
    // /api/apps refetch can transiently omit a still-installed app; a real LOCAL
    // uninstall scrubs the stack via deleteApp). A currently-VISIBLE tombstone is
    // NOT exempt — it must be closed even if an earlier visit also left it on the
    // stack (contract §5.1.2). String-normalized comparison throughout.
    const navHeld = new Set(
      navStackRef.current
        .filter(e => e.view === 'canvas' && e.appId != null)
        .map(e => String(e.appId))
    )
    for (const vid of visibleAppIds) navHeld.delete(vid)
    // Confirmed stale: seen present before, gone now, and not a protected back
    // target. A just-opened app not yet seen present survives (stale-list race).
    const stale = [...candidates].filter(sid => {
      const nid = Number(sid)
      return !navHeld.has(sid) && !liveIds.has(nid) && seenAppIdsRef.current.has(nid)
    })
    if (stale.length === 0) return
    const staleSet = new Set(stale)
    navStackRef.current = navStackRef.current.filter(
      e => !(e.view === 'canvas' && staleSet.has(String(e.appId)))
    )
    for (const sid of stale) {
      retireAppHistory(sid, 'uninstalled')
      tombstoneRoute('app', sid)
      dispatchWorkspace({
        type: 'CLOSE_TAB',
        tabKey: tabModel.tabKey(tabModel.makeTab('app', sid)),
        reason: 'deleted',
      })
    }
    // Drop any warm-only stale frame (not a tab, so CLOSE_TAB was a no-op for it)
    // so its 404'ing iframe unmounts.
    dropFromWarmLru(id => staleSet.has(String(id)))
  }, [apps, appsLiveFetched, openTabs, renderedAppIds, visibleAppIds,
      navStackRef, retireAppHistory, dispatchWorkspace])

  // One-shot slot-app reconcile (H1). A slot app uninstalled while the browser was
  // CLOSED is never "seen present" this session, so the eviction above can't reach
  // it. But its absence from the FIRST live list is only a HINT, never deletion
  // evidence: /api/apps/ is NetworkFirst (sw.js), so a slow or offline cold launch
  // can resolve the list from a stale SW cache fallback that TanStack cannot
  // distinguish from a live response. Per the platform DELETION-EVIDENCE CONTRACT
  // (probeDeletion), only a real per-app GET /api/apps/{id} 404 (live_app_or_404
  // tombstone) proves the slot app is gone. This deliberately RHYMES with the chat
  // cold-restore probe below: list absence hints, the authoritative per-resource 404
  // decides. A 'deleted' verdict triggers CLOSE_TAB reason:'deleted' (the reducer
  // clears the slot + scrubs history), retires its physical nav history, drops its
  // warm frame, and lands the empty single world on the New Chat surface; anything else
  // (present, offline, timeout) leaves the still-installed slot app pinned.
  useEffect(() => {
    if (!appsLiveFetched || initialSlotReconciledRef.current) return
    initialSlotReconciledRef.current = true
    const slot = workspaceStateRef.current.ws.singleScreen
    if (!slot || slot.kind !== 'app') return
    // The live list already vouches for the slot app → installed, no probe needed.
    if (apps.some(a => Number(a.id) === Number(slot.id))) return
    const slotId = slot.id
    let cancelled = false
    ;(async () => {
      const verdict = await probeDeletion(`/apps/${encodeURIComponent(slotId)}`)
      // Stale-guard: the single-world slot can change while the probe is in flight,
      // so a verdict for an old slot must never delete the new one.
      const current = workspaceStateRef.current.ws.singleScreen
      if (cancelled || !current || current.kind !== 'app'
          || Number(current.id) !== Number(slotId)) return
      // Only authoritative deletion evidence tears the slot down; 'exists'/'unknown'
      // (present, offline, timeout, non-404) leave the slot/tab/history untouched.
      if (verdict !== 'deleted') return
      const sid = String(slotId)
      retireAppHistory(sid, 'uninstalled')
      tombstoneRoute('app', sid)
      dispatchWorkspace({
        type: 'CLOSE_TAB',
        tabKey: tabModel.tabKey(tabModel.makeTab('app', sid)),
        reason: 'deleted',
      })
      dropFromWarmLru(id => String(id) === sid)
    })()
    return () => { cancelled = true }
  }, [appsLiveFetched, apps, retireAppHistory, tombstoneRoute, dispatchWorkspace,
      dropFromWarmLru, workspaceStateRef])

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

    // Durable-list fallback for an app-created event missed during reconnect.
    // Convert server relationships into the same pane-neutral
    // requests used by the live event path; the flat resolver is only today's
    // one-pane projection.
    const builtArrivals = freshChatBuiltApps(apps, fresh)
    if (builtArrivals.length > 0) {
      placeInWorkspace(workspaceRequestsForBuiltApps(builtArrivals))
    }
  }, [apps, appsLiveFetched, placeInWorkspace])

  // One-shot: a cold-restored canvas (moebius_active_app) is OPTIMISTIC —
  // useNavigation can't see the apps list. Once the live list lands, if the
  // restored app is gone (uninstalled since), demote the canvas to chat.
  // The present->absent eviction effect above can't cover this: a restored
  // id was never 'seen present' this session. See ARCHITECTURE.md (Navigation back-stack + drawer model).
  const coldRestoreCheckedRef = useRef(false)
  useEffect(() => {
    if (!appsLiveFetched || coldRestoreCheckedRef.current) return
    coldRestoreCheckedRef.current = true
    if (coldRestoredCanvasAppId == null) return
    const live = new Set(apps.map(a => a.id))
    if (live.has(coldRestoredCanvasAppId)) return
    // Restored app is gone (uninstalled since): retire its history and evict the
    // seeded warm frame so it can't sit stuck-mounted (the present->absent
    // eviction above never fires for an id never seen present this session). If a
    // tab was seeded for it (fallback boot), close it in its pane; if the
    // authoritative workspace never contained it, this is a no-op (contract
    // §1.4.6).
    retireAppHistory(coldRestoredCanvasAppId, 'cold-restore-gone')
    tombstoneRoute('app', coldRestoredCanvasAppId)
    dispatchWorkspace({
      type: 'CLOSE_TAB',
      tabKey: tabModel.tabKey(tabModel.makeTab('app', coldRestoredCanvasAppId)),
      reason: 'deleted',
    })
    const sid = String(coldRestoredCanvasAppId)
    dropFromWarmLru(id => String(id) === sid)
  }, [appsLiveFetched, apps, retireAppHistory, dispatchWorkspace])

  // Warm the SW app-code cache once per shell load for the apps the user
  // is most likely to open next — pinned + most-recent (the persisted
  // LRU) — so the first app-open of the session is served from cache.
  // Deliberately off the critical path: waits for a live-confirmed apps
  // list, then runs at browser idle (with a timeout so a busy page still
  // warms eventually). Skipped entirely under data-saver. The ref flips
  // BEFORE scheduling so apps-list refetches can't re-trigger the pass;
  // once scheduled it is never cancelled — priming the cache after a
  // view change (or even unmount) is exactly the point.
  const warmedOnLoadRef = useRef(false)
  useEffect(() => {
    if (warmedOnLoadRef.current || !appsLiveFetched || apps.length === 0) return
    warmedOnLoadRef.current = true
    if (navigator.connection?.saveData) return
    const toWarm = selectAppsToWarm(apps, initialAppLru)
    if (toWarm.length === 0) return
    const idle = typeof requestIdleCallback === 'function'
      ? (fn) => requestIdleCallback(fn, { timeout: 5000 })
      : (fn) => setTimeout(fn, 1500)
    idle(() => { for (const app of toWarm) warmAppCode(app) })
  }, [appsLiveFetched, apps, initialAppLru, warmAppCode])

  usePushSubscription()

  // Stable refresh callbacks. Earlier versions used
  // `appsQuery.refetch` directly, but React Query returns a new
  // QueryObserverResult ref on every subscription tick — that made
  // these `useCallback`s recreate identity each render, and the
  // drawer-open effect below would re-fire on every SSE tick,
  // hammering `/api/apps` and `/api/chats` while streaming.
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

  const { openAppWithIntent, handleChatInternalNav } = useAppIntentNavigation({
    appsRef,
    refreshApps,
    showToast,
    setAppIntents,
    navToRef,
  })

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
  // AppCanvas. STABLE `useCallback([])`: it reads the live apps/chats through
  // refs and calls the current newChat through `newChatRef`, so its identity
  // never changes and AppCanvas's message listener (which deps on it) never
  // re-registers.
  const handleAppError = useCallback((appId, error, chatId) => {
    const appEntry = appsRef.current.find(a => String(a.id) === String(appId))
    const appName = appEntry?.name || `app ${appId}`
    const report = `The app "${appName}" crashed with this error:\n\`\`\`\n${error}\n\`\`\`\nPlease investigate and fix.`
    const buildingChatId = appEntry?.chat_id || chatId || null
    const buildingChat = buildingChatId
      && chatsRef.current.find(c => c.id === buildingChatId)
    if (buildingChat) {
      try {
        sessionStorage.setItem('pending-draft', report)
        sessionStorage.setItem(`draft:${buildingChatId}`, report)
        sessionStorage.removeItem('pending-draft-autosend')
        sessionStorage.removeItem(`draft-autosend:${buildingChatId}`)
      } catch {}
      // Open the building chat in the crashed app's OWN pane (fallback: focused
      // pane) so a background app's crash report lands beside it (contract §1.4.7).
      const ownerPane = paneModel.paneOf(
        workspaceStateRef.current.ws,
        tabModel.tabKey(tabModel.makeTab('app', appId)),
      )
      navToRef.current('chat', { chatId: buildingChatId, paneId: ownerPane?.id })
      refreshChats()
    } else {
      newChatRef.current?.({ draft: report, forceNew: true })
    }
  }, [refreshChats, workspaceStateRef])

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
      const single = !paneModel.WORKSPACE_SPLITS_ENABLED || ws.viewMode === 'single'
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
        const single = !paneModel.WORKSPACE_SPLITS_ENABLED || ws.viewMode === 'single'
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

  useEffect(() => {
    if (navigationOpen) { refreshApps(); refreshChats() }
  }, [navigationOpen, refreshApps, refreshChats])

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
    } else if (ev.type === 'app_deleted') {
      if (ev.appId) confirmAppDeleted(ev.appId)
      void invalidateShellListCache('apps')
    } else if (ev.type === 'app_recovered') {
      if (ev.appId) confirmAppRecovered(ev.appId)
      void invalidateShellListCache('apps').then(refreshApps)
    } else if (ev.type === 'app_updated' || ev.type === 'app_created') {
      const placementRequest = workspaceRequestFromSystemEvent(ev)
      // app_updated is also the reinstall event for a tombstoned store app,
      // while app_created may carry an integer id freed by TTL purge and reused
      // for a different installation. A direct resource probe—not a staleable
      // list—is the proof that either id is live again.
      const reconcileIdentity = ev.appId
        ? confirmAppIdentityIsLive(ev.appId)
        : Promise.resolve(false)
      // Refresh server truth before warming or placing. app_updated is
      // refresh-only; app_created may additionally issue one background
      // workspace placement after the returned row confirms the relationship.
      // `updated_at` drives the iframe cache-buster and the derived built-app
      // CTA, so neither needs a separate client mirror.
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
        // `app_created` is emitted only after the first runnable compile. Check
        // the refreshed row before honoring it, then place in the background;
        // a malformed/spoofed event cannot open an absent or unrelated app.
        if (placementRequest) {
          const app = updatedApps.find(a => (
            String(a.id) === placementRequest.item.id
            && String(a.chat_id) === placementRequest.source.id
          ))
          if (app) placeInWorkspace(placementRequest)
        }
      })
    } else if (ev.type === 'open_item') {
      // An explicit agent-initiated open (design §6.3), system-bus-only so it
      // fires exactly once. Confirm the item actually exists in fresh server
      // truth before placing — mirror the app_created confirm-guard so a spoofed
      // or absent id is a silent no-op. App items also warm their code cache.
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
      // A failed background build leaves the previous app version running.
      // The owner has no useful action here, and a burst of watcher retries
      // must never cover the composer. Keep the diagnostic in backend logs;
      // actionable update drift uses app_update_stale below with a direct path
      // back to the App Store.
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
      }
      refreshChats()
    } else if (ev.type === 'chat_run_finished') {
      const chatId = ev.chatId
      if (chatId) {
        // Finish is activity too: if start was missed during a reconnect, or
        // both events batch together, the active ChatView still fetches the
        // final durable transcript.
        markChatRunFinished(chatId)
        markStreamingEnd(chatId)
        // Attention iff the finished chat is NOT visible in ANY pane — membership
        // in the visible set, not equality with one global id, so a chat visible
        // in a background split gets no false dot (finding D-iii).
        if (!visibleChatIdsRef.current.has(String(chatId))) {
          setAttentionChatIds(prev => {
            if (prev.has(chatId)) return prev
            const next = new Set(prev)
            next.add(chatId)
            return next
          })
        }
      }
      refreshChats()
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
      requestShellReload({ passive: ev.type === 'shell_rebuilt' })
    } else if (ev.type === 'shell_rebuild_failed') {
      // Deliberately silent in the owner UI. The atomic publisher keeps the
      // previous shell running, and watcher failures commonly describe a
      // transient intermediate state during a multi-file agent edit. The
      // producer logs the diagnostic and retries; an explicit operation such
      // as a platform update reports its own failure where it was initiated.
    }
  }, [
    // Scalar state removed: shell_rebuilt now reads from refs (activeViewRef,
    // activeAppIdRef, activeChatIdRef, drawerOpenRef) so stale closure values
    // can't be serialized. Refs themselves don't need to be in deps (they're
    // stable objects whose .current is read at call time, not at capture time).
    confirmAppDeleted, confirmAppIdentityIsLive, confirmAppRecovered,
    confirmChatDeleted, confirmChatRecovered,
    loadTheme, markChatRunActivity, markChatRunFinished,
    markStreamingEnd, markStreamingStart,
    placeInWorkspace, refreshApps, refreshChats, warmAppCode,
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
  // through the same idempotent placement resolver as live app_created events.
  const reconcileSystemStateOnOpen = useCallback(() => {
    void Promise.all([
      invalidateShellListCache('apps'),
      invalidateShellListCache('chats'),
      reconcileDeletedAppIdentities(),
    ]).then(() => {
      refreshApps()
      refreshChats()
    })
  }, [reconcileDeletedAppIdentities, refreshApps, refreshChats])
  useSystemEventStream(handleSystemEvent, { onOpen: reconcileSystemStateOnOpen })

  // Listen for postMessage events from mini-app iframes:
  //   moebius:app-error — route crash report to the chat that built the app
  //     (stored as chat_id on the app record). Falls back to a new chat if
  //     the building chat was deleted. Error is set as a draft (not auto-sent)
  //     so the user can review before sending.
  //   moebius:new-chat — open a new chat with optional pre-filled draft text.
  //     Payload may include autoSend:true, which sends that exact draft after
  //     ChatView mounts. Used only for explicit app approval flows.
  //   moebius:open-chat — open an existing chat, optionally pre-filling a draft.
  //   moebius:open-app — switch the shell to an installed app. Payload
  //     {appId} accepts either the numeric DB id or the slug; we match
  //     against the installed apps list and silently ignore unknown ids
  //     (don't crash the shell on a stale or malicious payload). Mirrors
  //     the drawer's onApp wiring (navTo('canvas', { appId })) so the
  //     existing iframe LRU + back-stack behavior applies.
  //   moebius:open-settings — switch to Settings and focus a known section.
  //     Used by setup prompts inside catalog apps; unknown section names
  //     degrade to the provider area.
  useEffect(() => {
    const settingsSections = new Set([
      'ai-providers',
      'background-agents',
      'models',
    ])
    async function onMessage(e) {
      // window 'message' events are for cross-frame postMessage from app
      // frames. NOT service-worker messages —
      // those arrive on navigator.serviceWorker, handled separately
      // below.
      //
      // Sandboxed app frames intentionally have the opaque `null` origin. A
      // null origin alone is not identity, so require the event source to be
      // one of the AppCanvas windows currently mounted by this shell. This also
      // keeps same-origin popups or stale frames from driving navigation.
      if (e.origin !== 'null' && e.origin !== window.location.origin) return
      const fromMountedApp = [...document.querySelectorAll('iframe.canvas')]
        .some((frame) => frame.contentWindow === e.source)
      if (!fromMountedApp) return
      if (e.data?.type === 'moebius:new-chat') {
        newChat({
          draft: e.data.draft,
          forceNew: true,
          autoSend: e.data.autoSend === true,
        })
      } else if (e.data?.type === 'moebius:open-chat') {
        if (typeof e.data.chatId !== 'string' || !e.data.chatId) return
        if (e.data.draft) {
          const draftText = String(e.data.draft)
          try {
            sessionStorage.setItem('pending-draft', draftText)
            sessionStorage.setItem(`draft:${e.data.chatId}`, draftText)
            sessionStorage.removeItem('pending-draft-autosend')
            sessionStorage.removeItem(`draft-autosend:${e.data.chatId}`)
          } catch {}
        }
        navTo('chat', { chatId: e.data.chatId })
        refreshChats()
      } else if (e.data?.type === 'moebius:open-app') {
        // Match against installed apps by numeric id OR slug, so the
        // sender can use whichever it has on hand. String() coercion
        // covers the numeric-id case without trusting the payload's type.
        //
        // App installs can complete while the shell is holding a stale
        // persisted /api/apps snapshot (common in installed PWAs). In that
        // state the App Store iframe knows the installed DB id, but this
        // handler's current list does not, and a silent return leaves the
        // user on the previous chat. Refetch once before giving up so
        // newly-installed external apps open from their own detail screen.
        await openAppWithIntent(e.data.appId, e.data.intent)
      } else if (e.data?.type === 'moebius:open-settings') {
        const rawSection = typeof e.data.section === 'string' ? e.data.section : ''
        const section = settingsSections.has(rawSection) ? rawSection : 'ai-providers'
        setSettingsFocusTarget({ section, nonce: Date.now() })
        if (activeViewRef.current !== 'settings') {
          navTo('settings')
        }
      }
    }

    function onSwMessage(e) {
      // Service-worker client.postMessage delivers here via
      // navigator.serviceWorker — NOT via window.message. (Subtle
      // browser API split: the SW spec routes them through the SW
      // container, not the global.) sw.js fires this on
      // notificationclick when an existing client is focused.
      if (e.data?.type !== 'notification-click') return
      const target = e.data.target
      if (typeof target !== 'string' || !target) return
      let path = target
      let search = ''
      try {
        if (/^https?:\/\//.test(target)) {
          const u = new URL(target)
          path = u.pathname
          search = u.search
        } else {
          const q = target.indexOf('?')
          if (q !== -1) { path = target.slice(0, q); search = target.slice(q) }
        }
      } catch { /* keep target as-is */ }
      // In-scope shell deep-link `/shell/?app=<id>` (cold-start-safe form,
      // _safeTarget normalizes to this). Parse the query so a warm tap on
      // the new target lands on the right view, same as the legacy paths.
      if (/^\/shell\/?$/.test(path)) {
        let app = null, chat = null, intent = null
        try {
          const params = new URLSearchParams(search)
          app = params.get('app')
          chat = params.get('chat')
          intent = params.get('intent')
        } catch { /* no query */ }
        if (app) void openAppWithIntent(app, intent)
        else if (chat) navTo('chat', { chatId: chat })
      }
    }

    window.addEventListener('message', onMessage)
    if (navigator.serviceWorker) {
      navigator.serviceWorker.addEventListener('message', onSwMessage)
    }
    return () => {
      window.removeEventListener('message', onMessage)
      if (navigator.serviceWorker) {
        navigator.serviceWorker.removeEventListener('message', onSwMessage)
      }
    }
  }, [navTo, openAppWithIntent, refreshChats])

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
  // Runs ONLY after the mode descriptor idles (the watcher below gates it), so the slot
  // write never drifts a live exit signature and cancels its own beat. Stale-guarded
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
      const { chatId } = pending.resolvedChatId != null
        ? { chatId: pending.resolvedChatId }
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
      const single = !paneModel.WORKSPACE_SPLITS_ENABLED || ws.viewMode === 'single'
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
        setNewChatLandingOffline(true)
        return
      }
      pendingNewChatRef.current = null
      setNewChatLandingOffline(false)
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
    const { chatId, reason } = await resolveNewChatId({ draft, forceNew, exclude })
    if (chatId == null) {
      // Don't leave a dead, drawer-still-open tap. Offline / failed create surface a
      // toast; an in-flight second tap just closes the drawer (the first create lands).
      if (reason === 'offline') showToast("You're offline.")
      else if (reason === 'error') showToast("Couldn't start a new chat — please try again.", { variant: 'error' })
      closeDrawer()
      return
    }

    const changesRoute = activeViewRef.current !== 'chat'
      || String(activeChatIdRef.current) !== String(chatId)
    const recordsHistory = changesRoute
      && !!(draft || forceNew || drawerPushedRef.current || recordHistory)
    if (draft) {
      const draftText = String(draft)
      try {
        sessionStorage.setItem('pending-draft', draftText)
        sessionStorage.setItem(`draft:${chatId}`, draftText)
        if (autoSend) {
          sessionStorage.setItem('pending-draft-autosend', draftText)
          sessionStorage.setItem(`draft-autosend:${chatId}`, draftText)
        } else {
          sessionStorage.removeItem('pending-draft-autosend')
          sessionStorage.removeItem(`draft-autosend:${chatId}`)
        }
      } catch {}
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
    if (focusComposer) requestComposerFocus(chatId)
  }
  // Keep the latest-newChat ref current so handleAppError's crash-report
  // fallback starts a chat with this render's live closure.
  newChatRef.current = newChat
  // Keep the latest-materialize ref current so the watcher effect (stable deps) always
  // runs this render's live closure without depending on the function's identity.
  materializeNewChatHomeRef.current = materializeNewChatHome

  // ── Deferred New Chat materialization watcher (round 4 item 3) ─────────────
  // A pending New Chat request (recorded by requestEmptySingleNewChat) materializes
  // only once the mode descriptor is IDLE and the slot is still an empty single — so
  // the slot write can never drift a live exit signature and cancel its own beat. When
  // the beat completes, modeState.transition flips to null and this re-runs.
  useEffect(() => {
    if (!pendingNewChatToken) return
    if (modeState.transition) return // wait for the descriptor to idle
    const pending = pendingNewChatRef.current
    if (!pending || pending.token !== pendingNewChatToken) return
    const ws = workspaceStateRef.current.ws
    const single = !paneModel.WORKSPACE_SPLITS_ENABLED || ws.viewMode === 'single'
    if (!single || ws.singleScreen != null) {
      // No longer an empty single slot (re-toggled to builder, or a slot was set by
      // another path) — drop the request.
      pendingNewChatRef.current = null
      return
    }
    materializeNewChatHomeRef.current?.(pending)
  }, [pendingNewChatToken, materializeNewChatRevision, modeState.transition,
      workspace.viewMode, workspace.singleScreen, workspaceStateRef])

  function selectChat(id) {
    clearChatAttention(id)
    navTo('chat', { chatId: id })
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
    try { sessionStorage.removeItem(`draft:${id}`) } catch {}
    // Evict the cached messages so a future chat-ID collision (e.g.
    // recovery) can't surface stale content.
    chatQueries.messages.remove(queryClient, id)
    // Scrub any navStack entries pointing at the deleted chat —
    // otherwise pressing back would navigate into a chat that returns
    // 404, leaving the user staring at an empty view. Soft-deleted
    // chats are recoverable for 7 days via /recover; once recovered
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
    const single = !paneModel.WORKSPACE_SPLITS_ENABLED || wsAfterClose.viewMode === 'single'
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
      const single = !paneModel.WORKSPACE_SPLITS_ENABLED || ws.viewMode === 'single'
      if (single && ws.singleScreen == null) requestEmptySingleNewChat()
      else newChat()
    }
  }, [chats, activeChatId, activeView, chatsQuery.isSuccess,
      chatsQuery.isFetchedAfterMount, requestEmptySingleNewChat, workspaceStateRef])

  return (
    <div
      ref={shellRootRef}
      // The logo-release timing vars for the live beat (round 4 item 1); absent when
      // idle so the .shell__logo rotate/scale transitions fall back to their defaults.
      style={beatRootVars || undefined}
      // The live transition phase + epoch, surfaced for observability + tests: the
      // completion is epoch-keyed in the controller closure (INV 12/15), and this
      // data-epoch documents which beat the DOM belongs to (a drag preview has no
      // beat class, so this is the only external signal it is armed). idle otherwise.
      data-mode-phase={modeState.transition ? modeState.transition.phase : 'idle'}
      data-mode-epoch={modeState.transition ? modeState.transition.id : undefined}
      // The ONE transient beat class comes from the descriptor (INV 1/4): exactly
      // one of entering/exiting is ever present, and the keyed animationend on
      // this root completes the beat (the controller's listener). No separate
      // entering/exiting booleans emit here anymore.
      className={`shell${immersiveActive ? ' shell--immersive' : ''}`
      + `${persistentDrawer && desktopSidebarOpen ? ' shell--drawer-docked' : ''}`
      + `${modeMachine.transitionRootClass(modeState, { splitsEnabled: SPLITS })
        ? ` ${modeMachine.transitionRootClass(modeState, { splitsEnabled: SPLITS })}` : ''}`
      + `${builderModeActive && paneModel.BUILDER_POWER_CHROME ? ' shell--builder-power' : ''}`}>
      {/* The existing brand toggle remains the visible close affordance while the
          mobile drawer is modal. Keep the workspace inert below, but do not inert
          the header: doing so lets the scrim intercept the toggle and strands the
          drawer without the close path its label and aria-expanded state promise. */}
      <header className="shell__bar">
        <ShellBrand
          brandRef={brandButtonRef}
          splitsEnabled={paneModel.WORKSPACE_SPLITS_ENABLED}
          navigationOpen={navigationOpen}
          builderModeActive={builderModeActive}
          // The LIVING HALO runs only once a beat has settled: pause it during ANY
          // transition (entry, exit, or a drag preview) so it adds no per-frame style
          // writes while panes are animating (exit-design v2 §Background isolation).
          haloActive={builderModeActive && !modeState.transition}
          // The live descriptor drives the logo's hold→completion spring (round 4
          // item 1): a hold-owned animated beat holds the mark compressed and releases
          // it as the beat completes, instead of an immediate ignite/snap.
          transition={modeState.transition}
          backFiredRef={backFiredRef}
          onToggleMode={handleToggleViewMode}
          onToggleNavigation={handleToggleNavigation}
        />
        <div className="shell__bar-actions">
          {!online && (
            <span className="shell__offline" role="status" aria-live="polite">
              Offline
            </span>
          )}
        </div>
      </header>

      <Drawer
        open={navigationOpen}
        persistent={persistentDrawer}
        interactionLocked={drawerModeTransitioning}
        onClose={drawerModeTransitioning ? undefined : closeDrawer}
        apps={apps}
        activeView={activeView}
        activeAppId={activeAppId}
        chats={chats}
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
        streamingChatIds={streamingChatIds}
        attentionChatIds={attentionChatIds}
        newAppIds={appAttentionSet}
        settingsWarning={providerAuth.needsAttention}
        dragActiveRef={dragActiveRef}
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
        // Single-leaf beat: the sole strip deals in / out / clears WITH its pane
        // (there is no WorkspaceChrome at one leaf). Its active key is the beat
        // participant key.
        const navMotion = wrapperMotion(focusedActiveKey)
        return (
        <nav
          className="shell__tabstrip"
          onWheel={scrollStripWheel}
          // INV 9 (inert beat): the single-pane strip clears WITH its pane during
          // an exit beat, so it is pointer/keyboard inert throughout — not just under
          // the drawer (M4). It matches the WorkspaceChrome strips, which already go
          // inert for the full mode beat.
          inert={modalDrawerOpen || modeBeatActive}
          aria-label="Open tabs"
          // The single-pane strip is the PRIMARY drag source once the flag is on
          // Tag it with the sole pane's id so the drag controller resolves a
          // source pane exactly as it does for a WorkspaceChrome strip; dragging
          // a tab out with ≥2 tabs present splits the pane.
          data-pane-strip={paneModel.WORKSPACE_SPLITS_ENABLED ? workspace.focusedPaneId : undefined}
          data-mode-motion={navMotion ? navMotion.motion : undefined}
          style={navMotion ? navMotion.vars : undefined}
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
                dragKey={paneModel.WORKSPACE_SPLITS_ENABLED ? key : undefined}
                onActivate={() => {
                  const { view, opts } = tabModel.tabNavTarget(tab)
                  navTo(view, opts)
                }}
                onClose={() => closeTab(tab)}
                onContextMenu={paneModel.WORKSPACE_SPLITS_ENABLED
                  ? (e) => openTabMenu(e, tab, null)
                  : undefined}
              />
            )
          })}
        </nav>
        )
      })()}
      <main className="shell__content" inert={modalDrawerOpen} ref={contentElRef}>
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
          const underlay = isUnderlay(tabKey)
          const paned = !underlay && workspaceChromeActive ? visibleTabRects.get(tabKey) : null
          const fullBleed = !underlay && !paned && tabKey === fullBleedKey
          const motion = wrapperMotion(tabKey)
          const posStyle = paned ? { top: paned.y, left: paned.x, width: paned.w, height: paned.h } : null
          const app = apps.find(a => String(a.id) === String(id))
          return (
          <div
            key={id}
            id={paned ? panePanelDomId(paned.paneId, tabKey) : undefined}
            role={paned ? 'tabpanel' : undefined}
            aria-labelledby={paned ? paneTabDomId(paned.paneId, tabKey) : undefined}
            data-tab-key={(multiPane || focusedPaneViewId != null) && !underlay ? tabKey : undefined}
            // COMPOSITOR-ONLY beat motion (v2): the wrapper HOLDS its tiled content
            // rect and animates only transform/opacity via data-mode-motion + the
            // latched --flip/--mode vars. A mid-beat focus change cannot retarget it
            // (the plan is latched, INV 2/10). The world-reveal underlay paints
            // full-bleed beneath the deal (INV 5).
            data-mode-motion={motion ? motion.motion : undefined}
            className={underlay
              ? 'shell__view shell__view--exit-underlay'
              : (paned
                ? 'shell__view shell__view--paned'
                : `shell__view ${fullBleed ? 'shell__view--active' : ''}`)}
            style={motion ? { ...(posStyle || {}), ...motion.vars } : (posStyle || undefined)}
            // INV 9 (inert beat): every moving surface — a participant pane OR the
            // underlay — is pointer/keyboard inert so a tap on an in-flight / covered
            // surface cannot dispatch FOCUS mid-animation.
            inert={(modeBeatActive && (!!motion || underlay)) || undefined}
            // Clicking a visible pane focuses it (chat panes are not opaque; app
            // iframes swallow interior clicks, so this catches wrapper padding —
            // interior app focus rides the runtime bridge later). Only in the
            // tiled path (finding D-i), and never during the exit beat.
            onPointerDownCapture={paned && !modeBeatActive
              ? () => dispatchWorkspace({ type: 'FOCUS', paneId: paned.paneId }) : undefined}
          >
            <ErrorBoundary key={`ab-${id}`} variant="inline" label="app">
            <AppCanvas
              appId={id}
              // Focused-pane-only: gates safe-area insets + the immersive holder
              // (global last-writer-wins). During the exit beat the DESTINATION
              // (beatTargetKey) is also driven active so its insets are correct
              // before completion, not jumping only after (exit-design §Visibility).
              active={tabKey === focusedActiveKey || (modeBeatActive && tabKey === beatTargetKey)}
              // Visible in ANY pane: gates frame-visibility + nav-push (§5). A
              // background split's app keeps running and can install sentinels;
              // Settings/immersive-solo/hidden panes exclude it (visibleAppIds).
              visible={visibleAppIds.has(String(id))}
              // Every visible pane remains painted beneath the modal scrim, but
              // suspend its iframe interaction while the drawer is open OR during any
              // exit beat (INV 9: cross-origin app interaction is inert throughout).
              interactive={visibleAppIds.has(String(id)) && !modalDrawerOpen && !modeBeatActive}
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
            />
            </ErrorBoundary>
          </div>
          )
        })}
        {/* Chat panes — normally one PaneChatView per visible chat pane. During
            a chat change the last painted chat remains as an inert opaque cover
            over the incoming staging chat until its existing scroll controller
            reports a stable frame. Layers remain chatId-sorted, so adding or
            removing the bounded cover never reparents another chat wrapper. */}
        {chatPaneLayers.map(({ paneId, chatId, role }) => {
          const tabKey = `chat:${chatId}`
          const paneActiveKey = workspace.panes[paneId]?.activeTabKey || tabKey
          const isActiveLayer = role === 'active'
          // Beat motion + underlay apply only to the ACTIVE layer (a held/staging
          // handoff cover is orthogonal to a mode beat). Keyed by the pane's active
          // key, exactly how deriveExitPlan keys its participants.
          const underlay = isActiveLayer && isUnderlay(paneActiveKey)
          const paned = !underlay && workspaceChromeActive ? visibleTabRects.get(paneActiveKey) : null
          const fullBleed = !underlay && !paned && paneActiveKey === fullBleedKey
          const motion = isActiveLayer ? wrapperMotion(paneActiveKey) : null
          const tabPanel = role !== 'held' && paned
          const handoffClass = !settingsOverlay && role !== 'active'
            ? ` shell__chat-view--${role}`
            : ''
          const posStyle = paned ? { top: paned.y, left: paned.x, width: paned.w, height: paned.h } : null
          return (
            <div
              key={chatId}
              id={tabPanel ? panePanelDomId(paneId, tabKey) : undefined}
              role={tabPanel ? 'tabpanel' : undefined}
              aria-labelledby={tabPanel ? paneTabDomId(paneId, tabKey) : undefined}
              data-tab-key={(multiPane || focusedPaneViewId != null) && role !== 'held' && !underlay
                ? tabKey : undefined}
              // Compositor-only beat motion (v2): see the app wrapper. The world-
              // reveal underlay chat paints full-bleed beneath the deal.
              data-mode-motion={motion ? motion.motion : undefined}
              className={underlay
                ? `shell__view shell__view--exit-underlay shell__chat-view${handoffClass}`
                : (paned
                  ? `shell__view shell__view--paned shell__chat-view${handoffClass}`
                  : `shell__view shell__chat-view ${fullBleed ? 'shell__view--active' : ''}${handoffClass}`)}
              style={motion ? { ...(posStyle || {}), ...motion.vars } : (posStyle || undefined)}
              // Inert while covered/handing-off OR while participating in / underlying
              // the exit beat (INV 9 inert beat).
              inert={settingsOverlay || role !== 'active' || (modeBeatActive && (!!motion || underlay))}
              aria-hidden={settingsOverlay || role !== 'active' ? 'true' : undefined}
              onPointerDownCapture={paned && role === 'active' && !modeBeatActive
                ? () => dispatchWorkspace({ type: 'FOCUS', paneId })
                : undefined}
            >
              <PaneChatView
                chatId={chatId}
                paneId={paneId}
                apps={apps}
                // Single view-mode paints only the focused pane full-bleed, so every
              // NON-focused chat pane stops doing work (streaming/scroll) — the
              // chat analogue of visibleAppIds soloing the focused app. Panes mode
              // keeps every visible chat pane doing work.
              visible={chatPanesVisible && role !== 'held' && visibleChatKeys.has(`chat:${chatId}`)}
                paneContentHeight={paned ? paned.h : null}
                chatRunSignals={chatRunSignals}
                composerFocusRequest={role === 'active' ? composerFocusRequest : null}
                onComposerFocusHandled={role === 'active'
                  ? handleComposerFocusHandled
                  : null}
                onSystemEvent={handleSystemEvent}
                markStreamingStart={markStreamingStart}
                markStreamingEnd={markStreamingEnd}
                markVoiceListening={markVoiceListening}
                refreshApps={refreshApps}
                refreshChats={refreshChats}
                loadTheme={loadTheme}
                navTo={navTo}
                onInternalNav={handleChatInternalNav}
                onChatMissing={handlePaneChatMissing}
                onFirstMessage={handlePaneChatFirstMessage}
                onDisplayReady={role === 'held'
                  ? null
                  : handlePaneChatDisplayReady}
              />
            </div>
          )
        })}
        {/* Settings surface — ONE wrapper, positioned like a chat/app content
            wrapper (paned) when it is a visible builder tab, full-bleed when the
            takeover overlay is up. Keyed 'settings' so React reconciles it by key
            regardless of the sibling app/chat arrays' lengths, preserving
            SettingsView identity across the tab<->overlay conversion. */}
        {settingsMounted && (() => {
          // Settings plays TWO possible exit-beat roles. As a builder Settings TAB
          // it is a DEAL-OUT participant (settingsMotion). As the honest destination
          // of a suspended single-world takeover (M2) it is the world-reveal
          // UNDERLAY: the mounted-hidden surface painted full-bleed BENEATH the
          // dealing tree, so the takeover no longer snaps over a revealed slot at
          // completion. The classifier excludes the settings leaf from participants
          // when it is the underlay, so these two roles never coincide.
          const settingsUnderlay = isUnderlay(SETTINGS_KEY)
          const settingsMotion = settingsUnderlay ? null : wrapperMotion(SETTINGS_KEY)
          const settingsPos = (!settingsUnderlay && settingsPaned)
            ? { top: settingsPaned.y, left: settingsPaned.x, width: settingsPaned.w, height: settingsPaned.h }
            : null
          const settingsTabPanel = !settingsUnderlay && settingsPaned
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
            data-tab-key={(!settingsUnderlay && settingsPaned) ? SETTINGS_KEY : undefined}
            data-mode-motion={settingsMotion ? settingsMotion.motion : undefined}
            className={settingsUnderlay
              ? 'shell__view shell__view--exit-underlay shell__settings-view'
              : (settingsPaned
                ? 'shell__view shell__view--paned shell__settings-view'
                : `shell__view shell__settings-view ${settingsFullBleed ? 'shell__view--active' : ''}`)}
            style={settingsMotion
              ? { ...(settingsPos || {}), ...settingsMotion.vars }
              : (settingsPos || undefined)}
            inert={(modeBeatActive && (!!settingsMotion || settingsUnderlay)) || undefined}
            onPointerDownCapture={settingsPaned && !settingsUnderlay && !modeBeatActive
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
              />
            </Suspense>
          </div>
          )
        })()}
        {/* New Chat landing (round 4 item 3) — the first-class surface a null single
            slot paints, and the stationary world-reveal underlay while an exit beat lands
            on it. Rendered only when it is actually the surface (fullBleedKey) or the
            reveal underlay, so it never sits mounted-hidden behind real content. The row
            materializes into a real chat after the descriptor idles; until then this is
            the honest destination (never chats[0], never a blank <main>). */}
        {(() => {
          const newChatUnderlay = isUnderlay(EMPTY_SINGLE_SURFACE_KEY)
          const newChatSurface = fullBleedKey === EMPTY_SINGLE_SURFACE_KEY
          if (!newChatUnderlay && !newChatSurface) return null
          return (
            <div
              key="home-new-chat"
              className={newChatUnderlay
                ? 'shell__view shell__view--exit-underlay shell__chat-view'
                : 'shell__view shell__view--active shell__chat-view'}
              inert={(modeBeatActive && newChatUnderlay) || undefined}
            >
              <NewChatLanding
                // Retry state only on the resting surface — never mid-reveal.
                offline={newChatSurface && newChatLandingOffline}
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
            // Enter/arrow input while invisibly dealing away.
            inert={modalDrawerOpen || modeBeatActive}
            workspace={workspace}
            projection={projection}
            mode={workspaceMode}
            contentRect={contentRect}
            contentElRef={contentElRef}
            dispatchWorkspace={dispatchWorkspace}
            navTo={navTo}
            labelForTab={labelForTab}
            onTabContextMenu={openTabMenu}
            // The ONE shared user-close action (INV 13) + the per-pane beat motion
            // (each strip deals with its pane) — WorkspaceChrome owns no private
            // close dispatcher and no motion knowledge of its own.
            onCloseTab={closeTab}
            focusedPaneViewId={focusedPaneViewId}
            onTogglePaneFocus={toggleFocusedPaneView}
            revealKey={tabRevealRevision}
            stripMotion={wrapperMotion}
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
          inert={modalDrawerOpen}
          onClick={() => dispatchImmersive({ type: 'exit' })}
        >
          <Minimize2 size={18} aria-hidden="true" />
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
      {/* Tab context menu — the ONLY split path in PR2. Fixed-position at the
          pointer; dismisses on outside pointerdown/Escape (effect above). Split
          and Move items exist only when the workspace-splits flag is on, so with
          the flag off (stage-A default) the menu never even opens (the strip
          handlers are omitted). */}
      {tabMenu && (() => {
        const menuPane = workspace.panes[tabMenu.paneId]
        const otherPaneIds = Object.keys(workspace.panes).filter(pid => pid !== tabMenu.paneId)
        const canOfferSplit = paneModel.WORKSPACE_SPLITS_ENABLED
          && menuPane && menuPane.tabs.length >= 2
        return (
          <div
            ref={tabMenuRef}
            className="workspace__menu"
            role="menu"
            aria-label="Tab actions"
            style={{ left: tabMenu.x, top: tabMenu.y }}
            onKeyDown={handleTabMenuKeyDown}
          >
            {canOfferSplit && [
              ['right', 'Split right'], ['left', 'Split left'],
              ['top', 'Split up'], ['bottom', 'Split down'],
            ]
              .filter(([edge]) => paneModel.canSplit(workspace, tabMenu.paneId, edge, workspaceMode, contentRect))
              .map(([edge, label]) => (
                <button
                  key={edge}
                  type="button"
                  role="menuitem"
                  className="workspace__menu-item"
                  onClick={() => {
                    dispatchWorkspace({ type: 'MOVE_TAB', tabKey: tabMenu.tabKey, target: { paneId: tabMenu.paneId, edge } })
                    closeTabMenu()
                  }}
                >
                  {label}
                </button>
              ))}
            {paneModel.WORKSPACE_SPLITS_ENABLED && otherPaneIds.length >= 1 && otherPaneIds.map(pid => {
              const pane = workspace.panes[pid]
              const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
              return (
                <button
                  key={pid}
                  type="button"
                  role="menuitem"
                  className="workspace__menu-item"
                  onClick={() => {
                    dispatchWorkspace({ type: 'MOVE_TAB', tabKey: tabMenu.tabKey, target: { paneId: pid } })
                    closeTabMenu()
                  }}
                >
                  Move to {active ? labelForTab(active) : 'pane'}
                </button>
              )
            })}
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
            {/* Close pane — a keyboard/menu affordance so a multi-tab pane need
                not be dismissed one ✕ at a time (design §3.6). Only when there is
                another pane to fall back to (never the single-pane strip). */}
            {paneModel.WORKSPACE_SPLITS_ENABLED && tabMenu.paneId != null && otherPaneIds.length >= 1 && (
              <button
                type="button"
                role="menuitem"
                className="workspace__menu-item"
                onClick={() => {
                  dispatchWorkspace({ type: 'CLOSE_PANE', paneId: tabMenu.paneId })
                  closeTabMenu()
                }}
              >
                Close pane
              </button>
            )}
          </div>
        )
      })()}
    </div>
  )
}
