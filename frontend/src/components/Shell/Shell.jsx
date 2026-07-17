import { lazy, Suspense, useState, useEffect, useLayoutEffect, useCallback, useMemo, useReducer, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Minimize2 from 'lucide-react/dist/esm/icons/minimize-2.mjs'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import Drawer from '../Drawer/Drawer.jsx'
import Toast from '../ui/Toast.jsx'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import WalkthroughOverlay from '../Walkthrough/WalkthroughOverlay.jsx'
import { api, apiFetch, BASE, clearAppRuntimeData } from '../../api/client.js'
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
import { appQueries, chatQueries, modelQueries, ownerQueries } from '../../hooks/queries.js'
import { appVersionKey } from '../../lib/appVersion.js'
import { immersiveReducer, isImmersiveActive } from '../../lib/immersive.js'
import { bumpChatRunSignal } from '../../lib/chatRunSignal.js'
import { clearAppFrameStorage, clearCachedAppToken } from '../../lib/appFrameStorage.js'
import {
  APP_LRU_STORAGE_KEY, mergeAppLru, parseStoredAppLru, selectAppsToWarm,
} from '../../lib/appPrecache.js'
import * as tabModel from './tabModel.js'
import * as paneModel from './paneModel.js'
import {
  attentionForRequest,
  resolveWorkspaceRequests,
  workspaceRequestFromSystemEvent,
  workspaceRequestsForBuiltApps,
} from './workspacePlacement.js'
import { appBuildFailureMessage } from '../../lib/appBuildFailure.js'
import { BEFORE_SHELL_RELOAD_EVENT } from '../../lib/shellReloadEvents.js'
import {
  freshChatBuiltApps,
  freshAppIds,
  withAppsFlagged,
  withoutAppFlagged,
} from './newAppAttention.js'
import { shouldDeferShellReload } from './shellReloadPolicy.js'
import {
  currentReusableEmptyChat,
  detailIsUntouchedEmptyChat,
} from './newChatPolicy.js'
import {
  reloadWhenWorkerTakesOver,
  shouldRearmShellApply,
} from './swHandoff.js'
import {
  awaitCacheFlushBeforeReload,
  flushPersistedQueryCache,
} from '../../queryClient.js'
import './Shell.css'
import './workspace.css'
import WorkspaceChrome from './WorkspaceChrome.jsx'
import useWorkspaceDrag from './useWorkspaceDrag.js'
import {
  HINT_KEY, coachmarkArmed, coachmarkDismissed, undoKeyPressed, isEditableTarget,
} from './workspaceOnboarding.js'
import PaneChatView from './PaneChatView.jsx'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import { deriveContentVisibility } from './workspaceView.js'
import { PaneTab, stripKeyDown } from './PaneStrip.jsx'

// Resolves the service worker to post warm-up messages to. The page is
// uncontrolled on its very first load (clientsClaim only takes over once
// the SW activates), so falling back to `ready.active` lets the first
// session still prime the cache. `ready` never resolves when no SW is
// registered (dev server) — the early null return guards that, and the
// callers treat a hanging promise as a harmless no-op anyway.
function _warmTargetSw() {
  if (!navigator.serviceWorker) return Promise.resolve(null)
  const ctrl = navigator.serviceWorker.controller
  if (ctrl) return Promise.resolve(ctrl)
  return navigator.serviceWorker.ready
    .then(reg => reg.active)
    .catch(() => null)
}

const SHELL_RELOAD_RECHECK_MS = 6000
const SettingsView = lazy(() => import('../SettingsView/SettingsView.jsx'))

function findAppForOpenTarget(list, target) {
  if (target == null) return null
  return (list || []).find(app =>
    String(app.id) === String(target) || app.slug === target) || null
}

export default function Shell() {
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
  // A lone workspace tab with no legacy pinned projection is the shell's
  // implicit home surface. An explicit cold deep link should replace it rather
  // than turning ordinary navigation into a two-item pinned strip.
  const replaceImplicitBootTab = legacyOpenTabs.length === 0
    && Object.keys(workspace.panes).length === 1
    && paneModel.flatten(workspace).length <= 1
  // Whether a VALID persisted workspace blob booted this session (not a flat-tab
  // fallback). The nav adapter uses it to make the blob authoritative over the
  // legacy shell-reload triple, seeding from that triple only when absent/invalid
  // (contract §5.3.10). Read once — sessionStorage is fixed for the mount.
  const [blobValid] = useState(
    () => paneModel.isValidWorkspaceBlob(paneModel.readWorkspaceRaw(sessionStorage)),
  )
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
  const dispatchWorkspace = useCallback((action) => {
    const prev = workspaceStateRef.current
    const next = paneModel.workspaceReducer(prev, action)
    workspaceStateRef.current = next
    // ANY tab-placement/pane transition can strand a route's paneId hint — a
    // cross-pane move (even when the source pane survives) leaves the moved tab's
    // routes pointing at the old pane, and a pane collapse leaves dead-pane hints.
    // Reconcile them synchronously (using prev/next), before any restore reads
    // them, so a hint always names the pane that now holds its item.
    if (next.ws !== prev.ws) onWorkspaceTransitionRef.current?.(prev.ws, next.ws)
    dispatchWorkspaceRaw(action)
  }, [])

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
    let resizeClassTimer = 0
    const ro = new ResizeObserver(() => {
      const w = Math.round(el.clientWidth)
      const h = Math.round(el.clientHeight)
      // A window/keyboard resize rewrites pane rects every frame; suppress the
      // layout-bloom transition while it is in flight and restore it 200ms after
      // the last resize, so a subsequent discrete commit (drop/split) still
      // blooms but a continuous resize stays crisp.
      el.classList.add('workspace--resizing')
      clearTimeout(resizeClassTimer)
      resizeClassTimer = setTimeout(() => el.classList.remove('workspace--resizing'), 200)
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
    return () => { ro.disconnect(); clearTimeout(resizeClassTimer) }
  }, [])
  const workspaceMode = useMemo(() => paneModel.modeForRect(contentRect), [contentRect])
  const projection = useMemo(
    () => paneModel.projectLayout(workspace, workspaceMode, contentRect),
    [workspace, workspaceMode, contentRect],
  )
  // The committed visible pane set the nav adapter reads. Settings-open is
  // applied separately inside isVisibleApp, so it is NOT excluded here.
  const visiblePaneIds = useMemo(() => new Set(projection.visibleLeaves), [projection])

  const {
    activeView,
    activeAppId,
    activeChatId,
    drawerOpen, openDrawer, closeDrawer,
    navTo, backFiredRef, drawerPushedRef, navStackRef,
    activeViewRef, activeChatIdRef, activeAppIdRef,
    drawerOpenRef,
    appNavPush, appNavPop, appNavReset, retireAppHistory, tombstoneRoute,
  } = useNavigation({
    workspace,
    workspaceStateRef,
    dispatchWorkspace,
    visiblePaneIds,
    blobValid,
    replaceImplicitBootTab,
    dragActiveRef,
  })

  // Settings is a full-workspace overlay (§9) — while it is up we suppress the
  // chrome and positioned rects (panes stay mounted but hidden).
  const settingsActive = activeView === 'settings'

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
  const immersiveActive = isImmersiveActive(immersiveAppId, activeView, activeAppId)

  // The single derivation of what content the render paints and where (design
  // §2/§4/§5). Pure + memoized so the immersive-solo and Settings-overlay
  // branches are unit-tested in workspaceView.test.js, and so one commit flips
  // every dependent flag together.
  const contentVisibility = useMemo(
    () => deriveContentVisibility({
      workspace, projection, settingsActive, immersiveActive, immersiveAppId,
    }),
    [workspace, projection, settingsActive, immersiveActive, immersiveAppId],
  )
  const { multiPane, focusedActiveKey, fullBleedKey, visibleAppIds } = contentVisibility
  const workspaceChromeActive = contentVisibility.chromeActive
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
  const appsQuery = appQueries.list.useQuery()
  const chatsQuery = chatQueries.list.useQuery()
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
  // toast state: null | { message, variant, duration, action }
  // variant: 'info' | 'error'  (see components/ui/Toast.jsx)
  const [toast, setToast] = useState(null)
  const [settingsFocusTarget, setSettingsFocusTarget] = useState(null)
  function showToast(message, { variant = 'info', duration = 4000, action } = {}) {
    setToast({ message, variant, duration, action })
  }
  function dismissToast() { setToast(null) }
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
    const content = paneModel.focusedContentRoute(workspaceStateRef.current.ws)
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
      streamingChatIds: streamingChatIdsRef.current,
      passiveRebuild: passive,
      voiceDictationActive: voiceDictationActiveRef.current,
      lastUserInteractionAt: lastShellInteractionAtRef.current,
      visibilityState: document.visibilityState,
    })
  }

  function passiveChatReloadIsReadingHold(passive) {
    return passive
      && document.visibilityState !== 'hidden'
      && activeViewRef.current === 'chat'
      && activeChatIdRef.current != null
  }

  function checkPendingShellReload() {
    if (!pendingShellReloadRef.current) return
    const passive = pendingShellReloadPassiveRef.current
    if (shellReloadWouldDisruptUser({ passive })) {
      // A passive watcher generation has no deadline while somebody is reading
      // a visible chat. Wait for the view/visibility effects below instead of
      // waking the page every six seconds for the whole reading session.
      if (!passiveChatReloadIsReadingHold(passive)) scheduleShellReloadCheck()
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
    if (!passiveChatReloadIsReadingHold(pendingShellReloadPassiveRef.current)) {
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

  // A passive generation held for a visible chat should land as soon as the
  // owner leaves the chat surface. Switching between chats remains protected.
  useEffect(() => {
    checkPendingShellReload()
  }, [activeView, activeChatId])
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
  // `apps` itself is `appsQuery.data ?? []`, a fresh array every render, so a
  // ref mirror is the only way a []-dep callback can see the live list.
  const appsRef = useRef(apps)
  useEffect(() => { appsRef.current = apps }, [apps])
  // Latest-`newChat` ref so the stable handleAppError can start a fresh chat
  // for a crash report without depending on newChat's identity (newChat is a
  // per-render function declaration with volatile inputs — chats, streaming,
  // online — that would churn any callback listing it as a dep).
  const newChatRef = useRef(null)
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
  // Close only the tab; the derived triple follows the workspace, so dropping the
  // focused pane's active tab activates its neighbour (paneModel.closeTab).
  const closeTab = useCallback((kind, id) => {
    // In the single-pane parity path, closing the sole strip item means
    // "unpin this surface", not "remove the content currently on screen". The
    // workspace keeps its implicit authority tab while the legacy projection is
    // cleared, exactly matching the pre-workspace shell.
    if (openTabs.length === 1) {
      setTabStripEngaged(false)
      tabModel.writeOpenTabs([])
      return
    }
    dispatchWorkspace({ type: 'CLOSE_TAB', tabKey: tabModel.tabKey(tabModel.makeTab(kind, id)) })
  }, [openTabs.length])
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
  }, [dispatchWorkspace])
  const tabStripVisible = tabStripEngaged && openTabs.length >= 1

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
    if (settingsActive) return set
    for (const paneId of projection.visibleLeaves) {
      const pane = workspace.panes[paneId]
      const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
      if (active && active.kind === 'chat') set.add(String(active.id))
    }
    return set
  }, [settingsActive, workspace, projection])
  const visibleChatIdsRef = useRef(visibleChatIds)
  useEffect(() => { visibleChatIdsRef.current = visibleChatIds }, [visibleChatIds])
  // The flat, chatId-sorted set of visible CHAT panes to mount as PaneChatViews
  // — for EVERY mode including single-pane (finding A): DOM identity across 1↔2
  // panes is the invariant, so the first split never remounts the visible chat.
  // Stable order (same no-reparent rule as the app iframes). Panes stay mounted
  // (hidden) behind the Settings overlay, exactly like the app iframes.
  const visibleChatPanes = useMemo(() => {
    const out = []
    for (const paneId of projection.visibleLeaves) {
      const pane = workspace.panes[paneId]
      const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
      if (active && active.kind === 'chat') out.push({ paneId, chatId: active.id })
    }
    return out.sort((a, b) => String(a.chatId).localeCompare(String(b.chatId)))
  }, [projection, workspace])

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
    for (const id of warmLruRef.current) {
      if (result.size >= APP_CACHE_MAX) break
      result.add(String(id))
    }
    return [...result].sort((a, b) => Number(a) - Number(b))
  }, [visibleAppIds, warmVersion])

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
    if (tab.kind === 'chat') return chatById.get(tab.id)?.title || 'Chat'
    return appById.get(tab.id)?.name || 'App'
  }, [chatById, appById])

  // Per-chat repair callback for a mounted chat pane (design §2 M13). A pane
  // whose chat reports a real 404 drops its tab; the derived triple follows the
  // workspace. If that collapses the workspace to the sole empty root and a live
  // chat remains, seed the current first chat into it (contract §1.4.12).
  const handlePaneChatMissing = useCallback((missingId) => {
    knownExistingOffListChatIdsRef.current.delete(missingId)
    dispatchWorkspace({
      type: 'CLOSE_TAB',
      tabKey: tabModel.tabKey(tabModel.makeTab('chat', missingId)),
      reason: 'deleted',
    })
    const ws = workspaceStateRef.current.ws
    const focused = ws.panes[ws.focusedPaneId]
    if (Object.keys(ws.panes).length === 1 && !focused?.activeTabKey) {
      const fallback = chatsRef.current.find(c => String(c.id) !== String(missingId))
      if (fallback) {
        dispatchWorkspace({
          type: 'OPEN_TAB', paneId: ws.focusedPaneId,
          tab: tabModel.makeTab('chat', fallback.id), activate: true,
        })
      }
    }
  }, [dispatchWorkspace, workspaceStateRef])
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
  // Filled by the first-use coachmark (§7); the first real drag dismisses it.
  const coachmarkDismissRef = useRef(null)
  const onWorkspaceDragStart = useCallback(() => { coachmarkDismissRef.current?.() }, [])
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
    onDragStart: onWorkspaceDragStart,
  })

  // ── Undo chord + first-use coachmark (design §3.5 / §7) ───────────────────
  // Workspace mutations update the reducer's single undo slot SILENTLY; the
  // owner found the "Moved X · Undo" / "Agent arranged your workspace" toasts
  // noise, so there is no per-mutation toast (owner call, live testing). Undo is
  // the Cmd/Ctrl+Z chord below plus the discoverability coachmark.
  const [wsCoachmarkDismissed, setWsCoachmarkDismissed] = useState(
    () => coachmarkDismissed(typeof localStorage !== 'undefined'
      ? localStorage
      : { getItem: () => '1' }),
  )
  const dismissWorkspaceCoachmark = useCallback(() => {
    setWsCoachmarkDismissed(true)
    try { localStorage.setItem(HINT_KEY, '1') } catch { /* private mode — shows once in memory */ }
  }, [])
  // The first real drag dismisses it (wired through the ref the drag hook calls).
  coachmarkDismissRef.current = dismissWorkspaceCoachmark
  const workspaceCoachmarkVisible = coachmarkArmed({
    enabled: paneModel.WORKSPACE_SPLITS_ENABLED,
    tabCount: openTabs.length,
    dismissed: wsCoachmarkDismissed,
  })
  // Auto-dismiss after 12s — deliberately NOT on an unrelated pointerdown (§7.2).
  useEffect(() => {
    if (!workspaceCoachmarkVisible) return undefined
    const t = setTimeout(dismissWorkspaceCoachmark, 12000)
    return () => clearTimeout(t)
  }, [workspaceCoachmarkVisible, dismissWorkspaceCoachmark])
  // A coarse pointer gets the hold-to-move copy; a fine pointer, drag-to-split.
  const [coarsePointer] = useState(
    () => typeof matchMedia !== 'undefined' && matchMedia('(pointer: coarse)').matches,
  )
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
      dispatchWorkspace({ type: 'UNDO_LAST' })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dispatchWorkspace])

  // No per-mutation undo toast: the reducer still mints a fresh undo slot on
  // every workspace mutation (its `toast` label included, for the reducer's own
  // tests), but the shell deliberately does NOT surface it — the owner found the
  // "Moved X · Undo" and "Agent arranged your workspace" toasts noisy. Recovery
  // stays on the Cmd/Ctrl+Z chord above.
  // Ids of apps that appeared in the fetched list AFTER this session's
  // baseline — the drawer renders a subtle accent dot until each is opened.
  const [newAppIds, setNewAppIds] = useState(() => new Set())
  // First-sign-in walkthrough. The query result is the source of
  // truth — backend persists completion via
  // POST /api/owner/walkthrough/complete. We render the overlay iff
  // the query has resolved AND `completed` is false; both gates
  // matter (rendering before resolution shows a flash for users who
  // are already past it).
  const walkthroughQuery = ownerQueries.walkthrough.useQuery()
  const showWalkthrough = walkthroughQuery.isFetched
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
  // Drives the small warning dot on the drawer's Settings row when
  // any registered provider is disconnected — surfacing the "silent
  // dead provider" failure mode without polling.
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
  // it as a query param mirrors how the iframe itself loads the module.
  // Safe to call speculatively — the SW skips already-cached entries.
  const warmAppCode = useCallback(async (app) => {
    try {
      const sw = await _warmTargetSw()
      if (!sw) return
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
      sw.postMessage({ type: 'moebius:precache-app', frameUrl, moduleUrl })
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
        queryFn: appQueries.list.fetch,
        staleTime: 0,
      }))
      .then(data => data || [])
      .catch(() => queryClient.getQueryData(appQueries.keys.all) || [])
  }, [queryClient])
  const refreshChats = useCallback(() => {
    return queryClient.refetchQueries({ queryKey: chatQueries.keys.all })
      .then(() => queryClient.getQueryData(chatQueries.keys.all) || [])
      .catch(() => [])
  }, [queryClient])

  const openAppWithIntent = useCallback(async (target, rawIntent) => {
    let app = findAppForOpenTarget(appsRef.current, target)
    if (!app) {
      const updatedApps = await refreshApps()
      app = findAppForOpenTarget(updatedApps, target)
    }
    if (!app) {
      setToast({
        message: 'App is not installed yet.',
        variant: 'info',
        duration: 6000,
      })
      return
    }
    const intent = typeof rawIntent === 'string' ? rawIntent.trim() : ''
    if (intent) {
      setAppIntents((prev) => ({
        ...prev,
        [String(app.id)]: { intent, nonce: Date.now() },
      }))
    }
    navTo('canvas', { appId: app.id })
  }, [navTo, refreshApps])

  const handleChatInternalNav = useCallback((url) => {
    const app = url.searchParams.get('app')
    const chat = url.searchParams.get('chat')
    const intent = url.searchParams.get('intent')
    if (app) {
      void openAppWithIntent(app, intent)
    } else if (chat) {
      navTo('chat', { chatId: chat })
    }
  }, [navTo, openAppWithIntent])

  const coldDeepLinkHandledRef = useRef(false)
  useEffect(() => {
    if (coldDeepLinkHandledRef.current) return
    if (deepLink?.view !== 'canvas' || !deepLink.app) return
    // Raw app targets are resolved here because navigation's boot parser can
    // open numeric ids directly but cannot validate or resolve app slugs.
    coldDeepLinkHandledRef.current = true
    void openAppWithIntent(deepLink.app, deepLink.intent)
  }, [openAppWithIntent])

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
      // No restored chat target. Seed the newest listed chat into the focused/
      // root pane ONLY if it has no active tab — never overwrite an active app
      // pane merely to maintain a remembered chat id (contract §1.4.8). If the
      // owner has no listed chats, the bootstrap effect creates one.
      const ws = workspaceStateRef.current.ws
      const focused = ws.panes[ws.focusedPaneId]
      if (!focused?.activeTabKey && chats[0]) {
        dispatchWorkspace({
          type: 'OPEN_TAB', paneId: ws.focusedPaneId,
          tab: tabModel.makeTab('chat', chats[0].id), activate: true,
        })
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

    // Drawer-list absence is not deletion evidence because /api/chats is a
    // filtered view that hides app-attributed chats and can lag a new chat.
    // Only a direct /api/chats/{id} 404 proves that the restored target should
    // be demoted.
    let cancelled = false
    const probedChatId = prev
    ;(async () => {
      try {
        const res = await apiFetch(`/chats/${encodeURIComponent(probedChatId)}?limit=1`, { timeoutMs: 15000 })
        // Stale-guard: the active chat can change while the probe is in
        // flight, so a verdict for an old restore target must never navigate.
        if (cancelled || activeChatIdRef.current !== probedChatId) return
        if (res.status === 404) {
          knownExistingOffListChatIdsRef.current.delete(probedChatId)
          // The restored chat is genuinely gone: close its tab in its pane. If
          // that leaves the sole empty root and a live chat remains, seed the
          // fallback into it (contract §1.4.9).
          dispatchWorkspace({
            type: 'CLOSE_TAB',
            tabKey: tabModel.tabKey(tabModel.makeTab('chat', probedChatId)),
            reason: 'deleted',
          })
          const ws = workspaceStateRef.current.ws
          const focused = ws.panes[ws.focusedPaneId]
          const fallback = chats.find(c => c.id !== probedChatId)
          if (!focused?.activeTabKey && fallback) {
            dispatchWorkspace({
              type: 'OPEN_TAB', paneId: ws.focusedPaneId,
              tab: tabModel.makeTab('chat', fallback.id), activate: true,
            })
          }
        } else if (res.ok) {
          // Exists but is unlisted because it is app-attributed or the drawer
          // list is lagging a fresh chat. Memoize only the positive off-list
          // result so future list refetches do not repeatedly probe it.
          knownExistingOffListChatIdsRef.current.add(probedChatId)
        }
        // Any other status is not deletion evidence, so the restored target
        // stays mounted until a later list refetch retries the probe.
      } catch {
        // Offline, network, timeout, and auth-expiry paths are not deletion
        // evidence, so the restored target stays mounted.
      } finally {
        if (!cancelled && activeChatIdRef.current === probedChatId) chatsLoadedRef.current = true
      }
    })()
    return () => { cancelled = true }
  }, [chats, chatsQuery.isFetched, chatsQuery.isSuccess,
      chatsQuery.isFetchedAfterMount, chatsQuery.isFetching,
      refreshChats, dispatchWorkspace, workspaceStateRef, activeChatIdRef])

  useEffect(() => { if (drawerOpen) { refreshApps(); refreshChats() } }, [drawerOpen, refreshApps, refreshChats])

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
    } else if (ev.type === 'app_updated' || ev.type === 'app_created') {
      const placementRequest = workspaceRequestFromSystemEvent(ev)
      // Refresh server truth before warming or placing. app_updated is
      // refresh-only; app_created may additionally issue one background
      // workspace placement after the returned row confirms the relationship.
      // `updated_at` drives the iframe cache-buster and the derived built-app
      // CTA, so neither needs a separate client mirror.
      refreshApps().then(updatedApps => {
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
      // System-bus-only (app_watcher publishes it there alone), so it arrives
      // exactly once — no dedup stamp needed.
      showToast(appBuildFailureMessage(ev), {
        variant: 'error',
        duration: 10000,
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
  useSystemEventStream(handleSystemEvent, { onOpen: refreshApps })

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

  async function newChat({ draft, forceNew, exclude, autoSend, focusComposer } = {}) {
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
    let chatId
    let empty = currentReusableEmptyChat(chatsRef.current, {
      activeChatId: activeChatIdRef.current,
      draft: !!draft,
      exclude,
      forceNew: !!forceNew,
      recoveredChatIds: recoveredChatIdsRef.current,
      streamingChatIds: streamingChatIdsRef.current,
    })

    // The list is intentionally only a candidate source. Cross-client sends
    // can make has_messages stale, so online reuse needs one fresh, bounded
    // detail read. Any error or unfamiliar response fails closed to creating
    // a new row rather than opening somebody else's newly-running chat.
    if (empty && online) {
      try {
        const res = await apiFetch(
          `/chats/${encodeURIComponent(empty.id)}?limit=1`,
          { timeoutMs: 5000 },
        )
        const detail = res.ok ? await res.json() : null
        if (!detailIsUntouchedEmptyChat(detail)) {
          empty = null
          void refreshChats()
        }
      } catch {
        empty = null
      }
    }
    if (empty) {
      chatId = empty.id
    } else {
      // Creating a fresh chat needs the server (POST allocates the row,
      // and a chat is only useful once the server-side agent can run).
      // Offline the POST below throws into the `catch { return }` — a
      // dead "New chat" tap with no feedback. Tell the user instead.
      // (The reuse-existing-empty branch above already handled the
      // offline-friendly case, so reaching here means we truly need
      // the network.)
      if (!online) {
        showToast("You're offline.")
        closeDrawer()
        return
      }
      // Spam-click guard: when no empty exists, two rapid taps would
      // race two POSTs and leave an extra empty behind. The in-flight
      // ref short-circuits the second call until the first resolves.
      if (creatingChatRef.current) {
        // A 2nd rapid tap while the first create is in flight — that
        // create will land and navigate; close the drawer so the tap is
        // acknowledged instead of leaving the menu hanging open.
        closeDrawer()
        return
      }
      creatingChatRef.current = true
      try {
        const res = await api.chats.create({ title: 'New chat' })
        const chat = await res.json()
        chatId = chat.id
        await refreshChats()
      } catch {
        // Don’t leave a dead, drawer-still-open tap on a failed create.
        showToast("Couldn't start a new chat — please try again.", { variant: 'error' })
        closeDrawer()
        return
      } finally {
        creatingChatRef.current = false
      }
    }

    const recordsHistory = !!(draft || forceNew || drawerPushedRef.current)
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
      // renders — open the new chat into the focused pane (contract §1.4.10).
      closeDrawer()
      const ws = workspaceStateRef.current.ws
      dispatchWorkspace({
        type: 'OPEN_TAB', paneId: ws.focusedPaneId,
        tab: tabModel.makeTab('chat', chatId), activate: true,
      })
    }
    if (focusComposer) requestComposerFocus(chatId)
  }
  // Keep the latest-newChat ref current so handleAppError's crash-report
  // fallback starts a chat with this render's live closure.
  newChatRef.current = newChat

  function selectChat(id) {
    clearChatAttention(id)
    navTo('chat', { chatId: id })
    refreshChats()
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
      // Other non-2xx (404 = already gone, etc.) — fall through to
      // local cleanup so a 404 doesn't leave a phantom in the UI.
    }
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
    const focusedAfterClose = workspaceStateRef.current.ws.panes[workspaceStateRef.current.ws.focusedPaneId]
    if (!focusedAfterClose?.activeTabKey) {
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
            await api.chats.recover(id)
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
      // Other non-2xx (e.g. 404 = already gone) — fall through to local
      // cleanup so the app doesn't linger as a phantom in the UI.
    }
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
            await api.apps.recover(id)
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
      newChat()
    }
  }, [chats, activeChatId, activeView, chatsQuery.isSuccess, chatsQuery.isFetchedAfterMount])

  return (
    <div className={`shell${immersiveActive ? ' shell--immersive' : ''}`}>
      {/* inert on the header while the drawer is open so keyboard / AT
          focus cannot reach shell-chrome behind the open drawer. The
          drawer itself gains focus on open (Drawer.jsx focus-management
          effect) and restores it here on close. React 19 reflects the
          boolean `inert` prop to the boolean attribute (present when true,
          absent when false); the old `drawerOpen ? '' : undefined` form was
          a no-op because React 19 normalizes the known boolean attribute and
          an empty string serializes as falsy, so it never applied. */}
      <header className="shell__bar" inert={drawerOpen}>
        {/* The brand area (logo + wordmark) is the only drawer trigger. A
            native button provides pointer, keyboard, and assistive-technology
            behavior without recreating it with a role and key handler. */}
        <button
          type="button"
          className="shell__brand"
          aria-label="Toggle navigation"
          aria-controls="navigation-drawer"
          aria-expanded={drawerOpen}
          /* Android may synthesize a bare click over the logo after an OS Back
             gesture. backFiredRef still filters that compatibility click, but
             a deliberate new interaction starts with pointerdown/keydown and
             must immediately clear the guard — never make the owner wait out a
             blanket 400ms dead zone before the drawer responds. */
          onPointerDown={() => { backFiredRef.current = false }}
          onKeyDown={() => { backFiredRef.current = false }}
          onClick={() => { if (backFiredRef.current) return; drawerOpen ? closeDrawer() : openDrawer() }}
        >
          <img className="shell__logo" src={`${BASE}/moebius.png`} alt="" width="30" height="30" />
          <span className="shell__wordmark">Möbius</span>
        </button>
        {!online && (
          <span className="shell__offline" role="status" aria-live="polite">
            Offline
          </span>
        )}
      </header>

      <Drawer
        open={drawerOpen}
        onClose={closeDrawer}
        apps={apps}
        activeView={activeView}
        activeAppId={activeAppId}
        chats={chats}
        activeChatId={activeChatId}
        onChat={selectChat}
        onApp={(id) => navTo('canvas', { appId: id })}
        onNewChat={() => newChat({ focusComposer: true })}
        onDeleteChat={deleteChat}
        onDeleteApp={deleteApp}
        onDeleteAppData={deleteAppData}
        onSettings={() => {
          setSettingsFocusTarget(null)
          navTo('settings')
        }}
        streamingChatIds={streamingChatIds}
        attentionChatIds={attentionChatIds}
        newAppIds={newAppIds}
        settingsWarning={providerAuth.anyDisconnected}
        dragActiveRef={dragActiveRef}
      />

      {showWalkthrough && (
        <WalkthroughOverlay
          onDone={() => {
            // Query invalidation inside WalkthroughOverlay flips
            // `showWalkthrough` to false on the next render. Nothing
            // else to do here.
          }}
        />
      )}

      {/* inert on the main content while the drawer is open — mirrors
          the drawer's own inert-when-closed contract, but inverted.
          Prevents pointer / keyboard events from reaching the chat or
          app canvas while the drawer is overlaid in front of it. Boolean
          prop form — see the header's inert note for why the old
          `? '' : undefined` form was a React 19 no-op. */}
      {/* Tab strip: pinned chats/apps to swap between with one tap.
          Switching a tab is ordinary navTo, so back works through the
          existing navStack. The strip shrinks .shell__content by one row;
          the chat re-measures its spacer at the new height on the next
          layout event (a ~1-row imprecision on the 0<->1 crossing that
          self-corrects). Deliberately NOT a ChatView remount — that would
          reset the send-reservation and freeze stream-follow (the reason the
          bespoke split view was parked). */}
      {tabStripVisible && !multiPane && (
        <nav
          className="shell__tabstrip"
          inert={drawerOpen}
          aria-label="Open tabs"
          // The single-pane strip is the PRIMARY drag source once the flag is on
          // (the coachmark teaches "drag tabs to split" here). Tag it with the
          // sole pane's id so the drag controller resolves a source pane exactly
          // as it does for a WorkspaceChrome strip; dragging a tab out with ≥2
          // tabs present splits the pane.
          data-pane-strip={paneModel.WORKSPACE_SPLITS_ENABLED ? workspace.focusedPaneId : undefined}
          onKeyDown={(e) => stripKeyDown(e, openTabs, (tab) => closeTab(tab.kind, tab.id))}
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
                tabIndex={active ? 0 : -1}
                dragKey={paneModel.WORKSPACE_SPLITS_ENABLED ? key : undefined}
                onActivate={() => {
                  const { view, opts } = tabModel.tabNavTarget(tab)
                  navTo(view, opts)
                }}
                onClose={() => closeTab(tab.kind, tab.id)}
                onContextMenu={paneModel.WORKSPACE_SPLITS_ENABLED
                  ? (e) => openTabMenu(e, tab, null)
                  : undefined}
              />
            )
          })}
        </nav>
      )}
      <main className="shell__content" inert={drawerOpen} ref={contentElRef}>
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
          const app = apps.find(a => String(a.id) === String(id))
          return (
          <div
            key={id}
            data-tab-key={multiPane ? tabKey : undefined}
            className={paned
              ? 'shell__view shell__view--paned'
              : `shell__view ${fullBleed ? 'shell__view--active' : ''}`}
            style={paned
              ? { top: paned.y, left: paned.x, width: paned.w, height: paned.h }
              : undefined}
            // Clicking a visible pane focuses it (chat panes are not opaque; app
            // iframes swallow interior clicks, so this catches wrapper padding —
            // interior app focus rides the runtime bridge later). Only in the
            // tiled path (finding D-i).
            onPointerDownCapture={paned ? () => dispatchWorkspace({ type: 'FOCUS', paneId: paned.paneId }) : undefined}
          >
            <ErrorBoundary key={`ab-${id}`} variant="inline" label="app">
            <AppCanvas
              appId={id}
              // Focused-pane-only: gates safe-area insets + the immersive holder
              // (global last-writer-wins). Single-pane: active === visible.
              active={tabKey === focusedActiveKey}
              // Visible in ANY pane: gates frame-visibility + nav-push (§5). A
              // background split's app keeps running and can install sentinels;
              // Settings/immersive-solo/hidden panes exclude it (visibleAppIds).
              visible={visibleAppIds.has(String(id))}
              // Every visible pane remains painted beneath the modal scrim, but
              // suspend its iframe interaction while the drawer is open. This
              // cancels kinetic scrolling already in flight, in addition to the
              // shell blocking new pointer input.
              interactive={visibleAppIds.has(String(id)) && !drawerOpen}
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
          const paned = workspaceChromeActive ? visibleTabRects.get(paneActiveKey) : null
          const fullBleed = !paned && paneActiveKey === fullBleedKey
          const handoffClass = !settingsActive && role !== 'active'
            ? ` shell__chat-view--${role}`
            : ''
          return (
            <div
              key={chatId}
              data-tab-key={multiPane && role !== 'held' ? tabKey : undefined}
              className={paned
                ? `shell__view shell__view--paned shell__chat-view${handoffClass}`
                : `shell__view shell__chat-view ${fullBleed ? 'shell__view--active' : ''}${handoffClass}`}
              style={paned
                ? { top: paned.y, left: paned.x, width: paned.w, height: paned.h }
                : undefined}
              inert={settingsActive || role !== 'active'}
              aria-hidden={settingsActive || role !== 'active' ? 'true' : undefined}
              onPointerDownCapture={paned && role === 'active'
                ? () => dispatchWorkspace({ type: 'FOCUS', paneId })
                : undefined}
            >
              <PaneChatView
                chatId={chatId}
                paneId={paneId}
                apps={apps}
                visible={chatPanesVisible && role !== 'held'}
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
        {activeView === 'settings' && (
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
        )}
        {/* Chrome layer — sibling AFTER the content wrappers, over the whole
            content box, carrying its own inert. Only at ≥2 visible leaves and
            never while Settings overlays. Draws per-pane strips, dividers, and
            the phone overflow chip; no content lives here. */}
        {workspaceChromeActive && (
          <WorkspaceChrome
            inert={drawerOpen}
            workspace={workspace}
            projection={projection}
            mode={workspaceMode}
            contentRect={contentRect}
            contentElRef={contentElRef}
            dispatchWorkspace={dispatchWorkspace}
            navTo={navTo}
            labelForTab={labelForTab}
            onTabContextMenu={openTabMenu}
            streamingChatIds={streamingChatIds}
            attentionChatIds={attentionChatIds}
            newAppIds={newAppIds}
          />
        )}
      </main>
      {/* SHELL-provided immersive exit. With the top bar gone the drawer
          toggle is unreachable, so this floating button is the guaranteed
          way back — an app can never trap the user in immersive mode.
          Exit only clears the shell-side request; the app re-enters by
          posting again (which a mounted app won't do until it remounts),
          so the user's choice sticks for the rest of the visit. */}
      {immersiveActive && (
        <button
          type="button"
          className="shell__immersive-exit"
          aria-label="Exit full screen"
          inert={drawerOpen}
          onClick={() => dispatchImmersive({ type: 'exit' })}
        >
          <Minimize2 size={18} aria-hidden="true" />
        </button>
      )}
      {/* First-use coachmark (design §7.2) — the discoverability path for the
          existing user base (the walkthrough never re-fires). Arms at ≥2 tabs
          with the splits flag on; dismissed by a drag, its ✕, or 12s — never by
          an unrelated tap. */}
      {workspaceCoachmarkVisible && (
        <div className="workspace__coachmark" role="status" aria-live="polite" inert={drawerOpen}>
          <span className="workspace__coachmark-text">
            {coarsePointer ? 'Hold a tab to move it' : 'Drag tabs to split the view'}
          </span>
          <button
            type="button"
            className="workspace__coachmark-close"
            aria-label="Dismiss hint"
            onClick={dismissWorkspaceCoachmark}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      )}
      <Toast
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
                dispatchWorkspace({ type: 'CLOSE_TAB', tabKey: tabMenu.tabKey })
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
